"""Bounded background jobs with per-user admission and owned-file cleanup."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import jmcomic

from .delivery import DeliveryError, DeliveryTarget
from .downloader import (
    ArchiveError,
    DownloadedImage,
    IncompleteDownloadError,
    Settings,
    build_archive,
    download_album,
    drain_task,
    finish_thread,
)

PLUGIN_NAME = "astrbot_plugin_jm_download"
JOB_NAME = re.compile(r"job-[a-f0-9]{32}")
Sender = Callable[[DeliveryTarget, str, Path | None], Awaitable[None]]
Backend = Callable[
    [str, Path, Settings, jmcomic.DownloadControl], Awaitable[list[DownloadedImage]]
]


@dataclass
class Job:
    """One in-memory request; queue state deliberately does not survive restart."""

    id: str
    album_id: str
    user_key: str
    target: DeliveryTarget
    control: jmcomic.DownloadControl
    archive: Path | None = None
    phase: str = "排队中"


class DownloadService:
    """Own admission, execution and shutdown for a single plugin instance."""

    def __init__(
        self,
        data_dir: Path,
        settings: Settings,
        sender: Sender,
        logger: logging.Logger,
        backend: Backend = download_album,
    ) -> None:
        self.jobs_root = data_dir.resolve() / "jobs"
        self.settings = settings
        self.sender = sender
        self.logger = logger
        self.backend = backend
        self.jobs: dict[str, Job] = {}
        self.users: set[str] = set()
        self.cooldowns: dict[str, float] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.slots = asyncio.Semaphore(settings.max_concurrent)
        self.closing = False
        self.sweeper: asyncio.Task | None = None
        # In-memory receipts contain routing strings only, never event callbacks.
        self.retained: dict[tuple[str, ...], tuple[str, float]] = {}

    @staticmethod
    def _receipt_key(
        album_id: str, user_key: str, target: DeliveryTarget
    ) -> tuple[str, ...]:
        """Bind a retained ZIP to its requesting user and exact conversation.

        Args:
            album_id: Requested album identifier.
            user_key: Original platform instance and sender.
            target: Delivery routing; the reply callback is deliberately excluded.

        Returns:
            Immutable routing strings suitable for the in-memory receipt index.
        """
        return (
            album_id,
            user_key,
            target.origin,
            target.platform_id,
            target.group_id,
            target.user_id,
        )

    async def initialize(self) -> None:
        """Clean abandoned jobs before accepting requests and start expiry checks."""
        if self.jobs_root.is_symlink() or self.jobs_root.is_junction():
            raise ValueError("The plugin jobs root must not be a symlink or junction")
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        await finish_thread(self._sweep, set(), True)
        self.sweeper = asyncio.create_task(self._sweep_loop(), name="jm-cleanup")

    def reserve(
        self, album_id: str, user_key: str, target: DeliveryTarget
    ) -> tuple[Job | None, str]:
        """Atomically reserve admission on AstrBot's event loop without awaiting.

        Args:
            album_id: Validated positive decimal album ID.
            user_key: Platform instance ID combined with sender ID.
            target: Original platform instance and recipient captured by the handler.

        Returns:
            An optional admitted job and the immediate user-facing response.
        """
        if self.closing:
            return None, "下载插件正在重载，请稍后重试。"
        for active in self.jobs.values():
            if (
                active.user_key == user_key
                and active.album_id == album_id
                and active.target == target
            ):
                # A new incoming command renews the official passive reply context.
                active.target = target
                return None, f"JM{album_id} {active.phase}，请等待；不会重复下载。"
        if user_key in self.users:
            return None, "你已有一个未完成的下载任务，请等待完成。"
        now = time.monotonic()
        self.cooldowns = {
            key: expiry for key, expiry in self.cooldowns.items() if expiry > now
        }
        if user_key in self.cooldowns:
            return (
                None,
                "命令冷却中，请在 "
                f"{math.ceil(self.cooldowns[user_key] - now)} 秒后重试。",
            )
        if len(self.jobs) >= self.settings.max_concurrent + self.settings.max_queue:
            return None, "下载队列已满，请稍后重试。"
        ahead = max(0, len(self.jobs) - self.settings.max_concurrent + 1)
        key = self._receipt_key(album_id, user_key, target)
        self.retained = {
            key: receipt
            for key, receipt in self.retained.items()
            if receipt[1] > time.time()
        }
        receipt = self.retained.get(key)
        archive = None
        if receipt:
            candidate = self.jobs_root / receipt[0] / f"JM{album_id}.zip"
            if candidate.is_file() and candidate.resolve().is_relative_to(
                self.jobs_root
            ):
                archive = candidate
            else:
                self.retained.pop(key, None)
        job = Job(
            receipt[0] if archive else f"job-{uuid4().hex}",
            album_id,
            user_key,
            target,
            jmcomic.DownloadControl(),
            archive,
        )
        self.jobs[job.id] = job
        self.users.add(user_key)
        self.cooldowns[user_key] = now + self.settings.cooldown_seconds
        state = f"已排队，前面有 {ahead} 个等待任务" if ahead else "已受理"
        if archive:
            return (
                job,
                f"JM{album_id} 已受理领取，将重新发送已完成的 ZIP，不会重复下载。",
            )
        return job, (
            f"JM{album_id} {state}，完成后将 ZIP 发送到本会话。"
            "等待较久时可再次发送同一命令，查看进度或领取已完成文件。"
        )

    def launch(self, job: Job) -> None:
        """Start a reserved job after the command acknowledgement was attempted.

        Args:
            job: A job returned by reserve.
        """
        if self.closing:
            self.jobs.pop(job.id, None)
            self.users.discard(job.user_key)
            return
        if job.id not in self.tasks or self.tasks[job.id].done():
            task = asyncio.create_task(self._run(job), name=f"jm-{job.album_id}")
            self.tasks[job.id] = task

            def forget(finished):
                if self.tasks.get(job.id) is finished:
                    self.tasks.pop(job.id, None)

            task.add_done_callback(forget)

    async def _run(self, job: Job) -> None:
        directory = self.jobs_root / job.id
        retain = job.archive is not None
        try:
            async with self.slots:
                images = directory / "images"
                archive = job.archive or directory / f"JM{job.album_id}.zip"
                if job.archive is None:
                    directory.mkdir()
                    marker = directory / ".job.json"
                    marker.write_text(
                        json.dumps({"plugin": PLUGIN_NAME, "retained_at": None}),
                        encoding="utf-8",
                    )
                    job.phase = "正在下载"
                    self.logger.info("Download started for %s", job.id)
                    download = asyncio.create_task(
                        self.backend(job.album_id, images, self.settings, job.control)
                    )
                    try:
                        paths = await asyncio.wait_for(
                            asyncio.shield(download),
                            self.settings.download_timeout_seconds,
                        )
                    except (TimeoutError, asyncio.CancelledError):
                        job.control.cancel("download timed out or plugin stopped")
                        download.cancel()
                        await drain_task(download)
                        raise
                    job.phase = "正在打包"
                    self.logger.info("Archive creation started for %s", job.id)
                    await finish_thread(
                        build_archive,
                        paths,
                        images,
                        archive,
                        self.settings.zip_password,
                    )
                retain = True
                if archive.stat().st_size > self.settings.max_zip_mib * 1024 * 1024:
                    await self._notify(
                        job,
                        f"JM{job.album_id} 下载完成，但 ZIP 超过 "
                        f"{self.settings.max_zip_mib} MiB 的插件发送上限。"
                        f"文件保留 {self.settings.retention_hours} 小时，"
                        "请联系管理员从本机获取。",
                    )
                    return
                try:
                    job.phase = "正在发送文件"
                    self.logger.info("ZIP delivery started for %s", job.id)
                    await asyncio.wait_for(
                        self.sender(
                            job.target, f"JM{job.album_id} 下载完成。", archive
                        ),
                        timeout=180,
                    )
                except Exception as exc:
                    self.logger.exception("ZIP send failed for %s", job.id)
                    reason = (
                        str(exc)
                        if isinstance(exc, DeliveryError)
                        else "请联系管理员检查 QQ 文件权限和日志。"
                    )
                    await self._notify(
                        job,
                        f"JM{job.album_id} 下载完成，但 ZIP 发送失败。"
                        f"文件保留 {self.settings.retention_hours} 小时，"
                        + reason
                        + " 可在本会话再次发送同一 /jm编号 领取，不会重复下载。",
                    )
                    return
                retain = False
                self.logger.info("ZIP delivery completed for %s", job.id)
        except asyncio.CancelledError:
            job.control.cancel("plugin stopped")
            raise
        except TimeoutError:
            self.logger.warning("Download timed out for %s", job.id)
            await self._notify(
                job, f"JM{job.album_id} 下载超时，任务已停止，请稍后重试。"
            )
        except jmcomic.MissingAlbumPhotoException:
            self.logger.exception("Album or chapter not found for %s", job.id)
            await self._notify(job, f"JM{job.album_id} 不存在或无法访问，请检查编号。")
        except (jmcomic.PartialDownloadFailedException, IncompleteDownloadError):
            self.logger.exception("Incomplete album for %s", job.id)
            await self._notify(
                job,
                f"JM{job.album_id} 部分章节或图片下载失败，"
                "未发送不完整作品，请稍后重试。",
            )
        except ArchiveError:
            self.logger.exception("Archive creation failed for %s", job.id)
            await self._notify(
                job,
                f"JM{job.album_id} ZIP 打包失败，请联系管理员检查磁盘空间及写入权限。",
            )
        except jmcomic.JmcomicException:
            self.logger.exception("JM request failed for %s", job.id)
            await self._notify(
                job,
                f"JM{job.album_id} 下载失败：网络或站点接口异常，请稍后重试；"
                "管理员可检查代理、API 域名及日志。",
            )
        except Exception:
            self.logger.exception("Download job failed for %s", job.id)
            await self._notify(
                job, f"JM{job.album_id} 处理失败，请联系管理员检查插件日志和磁盘权限。"
            )
        finally:
            try:
                if directory.exists():
                    if retain:
                        (directory / ".job.json").write_text(
                            json.dumps(
                                {"plugin": PLUGIN_NAME, "retained_at": time.time()}
                            ),
                            encoding="utf-8",
                        )
                        self.logger.warning(
                            "Retained ZIP for %s at %s", job.id, directory
                        )
                        self.retained[
                            self._receipt_key(job.album_id, job.user_key, job.target)
                        ] = (job.id, time.time() + self.settings.retention_hours * 3600)
                        if (directory / "images").exists():
                            await finish_thread(shutil.rmtree, directory / "images")
                    else:
                        self.retained.pop(
                            self._receipt_key(job.album_id, job.user_key, job.target),
                            None,
                        )
                        await finish_thread(self._remove_owned, directory)
            except Exception:
                self.logger.exception("Job cleanup failed for %s", job.id)
            finally:
                self.jobs.pop(job.id, None)
                self.users.discard(job.user_key)

    async def _notify(self, job: Job, text: str) -> None:
        if self.closing:
            return
        try:
            await asyncio.wait_for(self.sender(job.target, text, None), timeout=30)
        except Exception:
            self.logger.exception("Status send failed for %s", job.id)

    def _remove_owned(self, directory: Path) -> None:
        if (
            not JOB_NAME.fullmatch(directory.name)
            or directory.is_symlink()
            or directory.is_junction()
        ):
            raise ValueError("Refusing to remove an unowned job path")
        if directory.resolve().parent != self.jobs_root.resolve():
            raise ValueError("Job directory escaped the plugin jobs root")
        marker = directory / ".job.json"
        if (
            marker.is_symlink()
            or json.loads(marker.read_text(encoding="utf-8")).get("plugin")
            != PLUGIN_NAME
        ):
            raise ValueError("Job ownership marker is invalid")
        shutil.rmtree(directory)

    def _sweep(self, active_ids: set[str], remove_abandoned: bool = False) -> None:
        for directory in self.jobs_root.iterdir():
            if directory.name in active_ids or not JOB_NAME.fullmatch(directory.name):
                continue
            try:
                if (
                    directory.is_symlink()
                    or directory.is_junction()
                    or not directory.is_dir()
                ):
                    continue
                marker = json.loads(
                    (directory / ".job.json").read_text(encoding="utf-8")
                )
                if marker.get("plugin") != PLUGIN_NAME:
                    continue
                retained_at = marker.get("retained_at")
                if (retained_at is None and remove_abandoned) or (
                    retained_at is not None
                    and time.time() - float(retained_at)
                    >= self.settings.retention_hours * 3600
                ):
                    self._remove_owned(directory)
            except Exception:
                self.logger.exception("Expiry cleanup failed for %s", directory.name)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(600)
            try:
                await finish_thread(self._sweep, set(self.jobs))
            except Exception:
                self.logger.exception("Periodic cleanup failed")

    async def terminate(self) -> None:
        """Reject new work, cancel queued jobs, and drain active I/O before unload."""
        self.closing = True
        for job in self.jobs.values():
            job.control.cancel("plugin stopped")
        tasks = list(self.tasks.values())
        if self.sweeper:
            tasks.append(self.sweeper)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
        self.users.clear()
