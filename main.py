"""AstrBot command adapter for complete-album ZIP downloads."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Plain
from astrbot.api.star import Context, Star, StarTools

from .delivery import DeliveryError, DeliveryTarget, onebot_file_path
from .downloader import Settings
from .service import PLUGIN_NAME, DownloadService

# AstrBot strips wake_prefix before regex filters run; mentions may retain it.
COMMAND_FILTER = r"(?i)^/?jm(?![A-Za-z_])"
COMMAND = re.compile(r"/?jm\s*([0-9]+)", re.IGNORECASE)
HELP = (
    "用法：/jm123456 或 /jm 123456。编号必须为正整数；"
    "下载全部章节后返回 ZIP。每人最多一个未完成任务。"
)
SUPPORTED_PLATFORMS = {"qq_official", "qq_official_webhook", "aiocqhttp"}


class JmDownloadPlugin(Star):
    """Route command events to the bounded background download service."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.settings = Settings.from_config(config)
        self.service: DownloadService | None = None

    async def initialize(self) -> None:
        """Initialize owned storage and the background service after plugin loading."""
        self.service = DownloadService(
            StarTools.get_data_dir(PLUGIN_NAME), self.settings, self._send, self.logger
        )
        await self.service.initialize()

    @filter.regex(COMMAND_FILTER)
    async def on_jm(self, event: AstrMessageEvent) -> None:
        """Handle supported syntax without forwarding the command to the LLM.

        Args:
            event: Incoming QQ group or private conversation event.
        """
        event.should_call_llm(False)
        event.stop_event()
        if event.get_platform_name() not in SUPPORTED_PLATFORMS:
            await event.send(
                event.plain_result(
                    "此插件支持 QQ 官方机器人及 NapCat/OneBot V11，请通过 QQ 使用。"
                )
            )
            return
        match = COMMAND.fullmatch(event.get_message_str().strip())
        if match is None or len(match[1]) > 18 or int(match[1]) <= 0:
            await event.send(event.plain_result(HELP))
            return
        if self.service is None:
            await event.send(event.plain_result("下载插件尚未准备好，请稍后重试。"))
            return
        album_id = str(int(match[1]))
        user_key = f"{event.get_platform_id()}:{event.get_sender_id()}"
        target = DeliveryTarget(
            event.unified_msg_origin,
            event.get_platform_id(),
            str(event.get_group_id() or ""),
            str(event.get_sender_id()),
        )
        job, text = self.service.reserve(album_id, user_key, target)
        try:
            await event.send(event.plain_result(text))
        except Exception:
            self.logger.exception("Command acknowledgement failed")
        finally:
            if job is not None:
                self.service.launch(job)

    async def _send(
        self, target: DeliveryTarget, text: str, archive: Path | None
    ) -> None:
        """Send completion or errors through AstrBot's conversation routing.

        Args:
            target: Original platform instance, conversation and recipient IDs.
            text: User-facing status.
            archive: Optional ZIP path that must stay available until send returns.

        Raises:
            RuntimeError: The original platform is unavailable or rejects the file.
        """
        platform = self.context.get_platform_inst(target.platform_id)
        if platform is None:
            raise DeliveryError("原机器人连接已不可用，请检查平台连接。")
        if archive is not None and platform.meta().name == "aiocqhttp":
            if self.service is None:
                raise RuntimeError("Download storage is unavailable")
            try:
                file_path = await asyncio.to_thread(
                    onebot_file_path,
                    archive,
                    self.service.jobs_root,
                    self.settings.onebot_remote_jobs_dir,
                )
            except (OSError, ValueError) as exc:
                raise DeliveryError("本机 ZIP 不可访问或路径映射无效。") from exc
            recipient = target.group_id or target.user_id
            if (
                not recipient.isascii()
                or not recipient.isdecimal()
                or int(recipient) <= 0
            ):
                raise ValueError("OneBot requires a positive numeric recipient ID")
            action = "upload_group_file" if target.group_id else "upload_private_file"
            recipient_key = "group_id" if target.group_id else "user_id"
            try:
                result = await platform.get_client().call_action(
                    action,
                    **{
                        recipient_key: recipient,
                        "file": file_path,
                        "name": archive.name,
                    },
                )
            except Exception as exc:
                raise DeliveryError(
                    "OneBot 上传接口失败，请检查 NapCat 连接、共享目录及文件权限。"
                ) from exc
            if isinstance(result, dict) and (
                result.get("status") == "failed"
                or result.get("retcode", 0) != 0
                or ("file_id" in result and not result["file_id"])
            ):
                raise DeliveryError("OneBot 拒绝上传，请检查共享目录及 QQ 文件权限。")
            # A successful upload is the delivery result; a status failure must
            # not turn a delivered file into a failed download or trigger retries.
            if text:
                try:
                    await self.context.send_message(
                        target.origin, MessageChain(chain=[Plain(text)])
                    )
                except Exception:
                    self.logger.exception(
                        "Completion status failed after OneBot upload"
                    )
            return
        components = [Plain(text)]
        if archive is not None:
            components.append(File(name=archive.name, file=str(archive)))
        if not await self.context.send_message(
            target.origin, MessageChain(chain=components)
        ):
            raise RuntimeError("The original messaging platform is unavailable")

    async def terminate(self) -> None:
        """Drain active downloads before plugin reload, disable or uninstall."""
        if self.service is not None:
            await self.service.terminate()
