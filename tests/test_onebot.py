"""OneBot file actions use captured recipients and the selected platform only."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_jm_download.delivery import DeliveryError, DeliveryTarget
from astrbot_plugin_jm_download.main import JmDownloadPlugin
from astrbot_plugin_jm_download.service import DownloadService

from .test_downloader import make_images


@pytest.fixture
def plugin(tmp_path):
    clients = {
        name: SimpleNamespace(
            call_action=AsyncMock(return_value={"file_id": "uploaded"})
        )
        for name in ("onebot-a", "onebot-b")
    }
    platforms = {
        name: SimpleNamespace(
            meta=lambda: SimpleNamespace(name="aiocqhttp"),
            get_client=lambda name=name: clients[name],
        )
        for name in clients
    }
    context = SimpleNamespace(
        get_platform_inst=Mock(side_effect=platforms.get),
        send_message=AsyncMock(return_value=True),
    )
    instance = JmDownloadPlugin(
        context, {"onebot_remote_jobs_dir": "/napcat/downloads"}
    )
    root = tmp_path / "jobs"
    archive = root / "job-example" / "JM123.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"zip fixture")
    instance.service = SimpleNamespace(jobs_root=root)
    return instance, clients, archive


@pytest.mark.parametrize(
    "platform_id,group_id,user_id,action,key",
    [
        ("onebot-a", "456", "789", "upload_group_file", "group_id"),
        ("onebot-b", "", "789", "upload_private_file", "user_id"),
    ],
)
async def test_explicit_recipient_and_platform(
    plugin, platform_id, group_id, user_id, action, key
):
    instance, clients, archive = plugin
    # Unique-session prefixes must not determine file recipients.
    target = DeliveryTarget(
        f"{platform_id}:GroupMessage:999_456", platform_id, group_id, user_id
    )
    await instance._send(target, "complete", archive)
    clients[platform_id].call_action.assert_awaited_once_with(
        action,
        **{
            key: group_id or user_id,
            "file": "/napcat/downloads/job-example/JM123.zip",
            "name": "JM123.zip",
        },
    )
    clients[
        "onebot-b" if platform_id == "onebot-a" else "onebot-a"
    ].call_action.assert_not_awaited()
    assert instance.context.send_message.call_args.args[0] == target.origin


@pytest.mark.parametrize(
    "result", [{"status": "failed"}, {"retcode": 100}, {"file_id": None}]
)
async def test_rejected_upload_is_not_reported_as_success(plugin, result):
    instance, clients, archive = plugin
    clients["onebot-a"].call_action.return_value = result
    with pytest.raises(RuntimeError):
        await instance._send(
            DeliveryTarget("umo", "onebot-a", "456", "789"), "done", archive
        )
    instance.context.send_message.assert_not_awaited()


async def test_upload_exception_and_missing_file_are_visible(plugin):
    instance, clients, archive = plugin
    clients["onebot-a"].call_action.side_effect = RuntimeError("not mounted in NapCat")
    target = DeliveryTarget("umo", "onebot-a", "456", "789")
    with pytest.raises(DeliveryError, match="OneBot"):
        await instance._send(target, "done", archive)
    clients["onebot-a"].call_action.reset_mock()
    with pytest.raises(DeliveryError, match="ZIP"):
        await instance._send(target, "done", archive.parent / "missing.zip")
    clients["onebot-a"].call_action.assert_not_awaited()


async def test_platform_disconnected_and_wrong_recipient_fail_safely(plugin):
    instance, clients, archive = plugin
    with pytest.raises(RuntimeError):
        await instance._send(DeliveryTarget("umo", "missing", "456"), "done", archive)
    with pytest.raises(ValueError):
        await instance._send(
            DeliveryTarget("umo", "onebot-a", "not-numeric"), "done", archive
        )
    clients["onebot-a"].call_action.assert_not_awaited()


async def test_status_failure_after_successful_upload_does_not_repeat_file(plugin):
    instance, clients, archive = plugin
    instance.context.send_message.side_effect = RuntimeError("status failed")
    await instance._send(DeliveryTarget("umo", "onebot-a", "456"), "done", archive)
    clients["onebot-a"].call_action.assert_awaited_once()


async def test_onebot_failure_retains_zip_and_explains_cause(plugin, tmp_path):
    instance, clients, _ = plugin

    async def backend(album_id, images, settings, control):
        return make_images(images, album_id)

    clients["onebot-a"].call_action.side_effect = RuntimeError(
        "remote permission denied"
    )
    service = DownloadService(
        tmp_path / "runtime",
        instance.settings,
        instance._send,
        instance.logger,
        backend,
    )
    instance.service = service
    await service.initialize()
    try:
        target = DeliveryTarget("onebot-a:GroupMessage:unique_456", "onebot-a", "456")
        job, _ = service.reserve("123", "onebot-a:789", target)
        service.launch(job)
        await asyncio.gather(*service.tasks.values())
        assert (service.jobs_root / job.id / "JM123.zip").is_file()
        assert not (service.jobs_root / job.id / "images").exists()
        notice = instance.context.send_message.call_args.args[1].chain[0].text
        assert "OneBot" in notice and "保留" in notice
        clients["onebot-a"].call_action.assert_awaited_once()
    finally:
        await service.terminate()
    assert (service.jobs_root / job.id / "JM123.zip").is_file()
