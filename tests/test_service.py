"""Verify admission, cancellation ordering, retention and cleanup boundaries."""

import asyncio
import json
import logging
import os
import time
from dataclasses import replace
from zipfile import ZipFile

import jmcomic
import pytest
import pyzipper
from astrbot_plugin_jm_download.delivery import DeliveryTarget
from astrbot_plugin_jm_download.downloader import ArchiveError, Settings, finish_thread
from astrbot_plugin_jm_download.service import PLUGIN_NAME, DownloadService

from .test_downloader import make_images


def reserve(service, album_id, user_key, origin):
    """Construct explicit targets for queue tests that do not use a real adapter."""
    return service.reserve(
        album_id, user_key, DeliveryTarget(origin, "platform", user_id=user_key)
    )


async def finish_jobs(service):
    await asyncio.wait_for(asyncio.gather(*list(service.tasks.values())), 3)


@pytest.fixture
async def factory(tmp_path):
    services = []

    async def create(backend=None, sender=None, settings=None):
        async def default_backend(album_id, images, *_):
            return make_images(images, album_id)

        async def default_sender(*_):
            pass

        service = DownloadService(
            tmp_path / f"service-{len(services)}",
            settings or Settings(),
            sender or default_sender,
            logging.getLogger("test-jm"),
            backend or default_backend,
        )
        await service.initialize()
        services.append(service)
        return service

    yield create
    for service in services:
        await service.terminate()


async def test_success_sends_original_origin_then_cleans(factory):
    observed = []

    async def sender(origin, text, archive):
        assert archive.is_file()
        with ZipFile(archive) as result:
            observed.append((origin.origin, result.namelist()))

    service = await factory(sender=sender)
    job, response = reserve(service, "123456", "platform:user", "platform:group:room")
    assert "已受理" in response
    service.launch(job)
    await finish_jobs(service)
    assert observed[0][0] == "platform:group:room"
    assert len(observed[0][1]) == 4
    assert not list(service.jobs_root.iterdir())
    assert not service.users


async def test_admission_limits_concurrency_and_queue(factory):
    gate = asyncio.Event()
    active, peak = 0, 0

    async def backend(album_id, images, *_):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await gate.wait()
        paths = make_images(images, album_id)
        active -= 1
        return paths

    service = await factory(backend=backend, settings=replace(Settings(), max_queue=1))
    first, _ = reserve(service, "1", "p:a", "p:group:a")
    service.launch(first)
    duplicate, text = reserve(service, "2", "p:a", "p:group:b")
    assert duplicate is None and "未完成" in text
    second, text = reserve(service, "2", "p:b", "p:group:b")
    assert second and "排队" in text
    service.launch(second)
    third, text = reserve(service, "3", "p:c", "p:group:c")
    assert third is None and "队列已满" in text
    await asyncio.sleep(0.02)
    assert peak == 1
    gate.set()
    await finish_jobs(service)
    assert peak == 1
    blocked, text = reserve(service, "4", "p:a", "p:private:a")
    assert blocked is None and "冷却" in text
    service.cooldowns["p:a"] = time.monotonic() - 1
    next_job, _ = reserve(service, "4", "p:a", "p:private:a")
    assert next_job


async def test_timeout_waits_for_pending_writes_before_cleanup(factory):
    drained = asyncio.Event()
    notices = []

    async def backend(album_id, images, settings, control):
        images.mkdir()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert control.is_cancelled
            await asyncio.sleep(0.03)
            assert images.is_dir()
            (images / "last-write").write_text("drained", encoding="utf-8")
            drained.set()
            raise

    async def sender(origin, text, archive):
        assert drained.is_set()
        assert archive is None
        notices.append(text)

    service = await factory(
        backend, sender, replace(Settings(), download_timeout_seconds=0.02)
    )
    job, _ = reserve(service, "1", "p:a", "p:private:a")
    service.launch(job)
    await finish_jobs(service)
    assert drained.is_set() and "超时" in notices[0]
    assert not list(service.jobs_root.iterdir())


async def test_shutdown_drains_active_and_discards_queued_jobs(factory):
    started = asyncio.Event()
    drained = asyncio.Event()
    calls = []

    async def backend(album_id, images, settings, control):
        calls.append(album_id)
        images.mkdir()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.02)
            assert images.is_dir() and control.is_cancelled
            drained.set()

    service = await factory(backend)
    for album in ("1", "2"):
        job, _ = reserve(service, album, f"p:{album}", "p:group:g")
        service.launch(job)
    await started.wait()
    await service.terminate()
    assert drained.is_set() and calls == ["1"]
    assert not service.jobs and not service.users
    assert not list(service.jobs_root.iterdir())
    assert reserve(service, "3", "p:3", "p:group:g")[0] is None


async def test_send_failure_keeps_zip_and_removes_images(factory):
    notices = []

    async def sender(origin, text, archive):
        if archive:
            raise RuntimeError("QQ denied the file")
        notices.append(text)

    service = await factory(sender=sender)
    job, _ = reserve(service, "123456", "p:u", "p:group:g")
    service.launch(job)
    await finish_jobs(service)
    directory = service.jobs_root / job.id
    assert (directory / "JM123456.zip").is_file()
    assert not (directory / "images").exists()
    assert json.loads((directory / ".job.json").read_text())["retained_at"]
    assert "发送失败" in notices[0]


async def test_oversized_archive_is_retained_without_sending(factory):
    notices = []

    async def backend(album_id, images, *_):
        paths = make_images(images, album_id)
        paths[0].path.write_bytes(os.urandom(1100000))
        return paths

    async def sender(origin, text, archive):
        assert archive is None
        notices.append(text)

    service = await factory(backend, sender, replace(Settings(), max_zip_mib=1))
    job, _ = reserve(service, "1", "p:u", "p:group:g")
    service.launch(job)
    await finish_jobs(service)
    assert "超过" in notices[0]
    assert (service.jobs_root / job.id / "JM1.zip").is_file()


@pytest.mark.parametrize(
    "error,expected",
    [
        (jmcomic.MissingAlbumPhotoException("missing", {}), "不存在"),
        (jmcomic.PartialDownloadFailedException("partial", {}), "不完整"),
        (jmcomic.RequestRetryAllFailException("network", {}), "网络"),
        (ArchiveError("disk full"), "打包失败"),
        (PermissionError("denied"), "处理失败"),
    ],
)
async def test_failures_never_send_an_archive(factory, error, expected):
    notices = []

    async def backend(*_):
        raise error

    async def sender(origin, text, archive):
        assert archive is None
        notices.append(text)

    service = await factory(backend, sender)
    job, _ = reserve(service, "1", "p:u", "p:private:u")
    service.launch(job)
    await finish_jobs(service)
    assert expected in notices[0]
    assert not list(service.jobs_root.iterdir())


async def test_cleanup_only_owns_marked_jobs_and_respects_retention(factory, tmp_path):
    service = await factory()
    directories = []
    for index, (owner, retained_at) in enumerate(
        [
            (PLUGIN_NAME, time.time() - 90000),
            (PLUGIN_NAME, time.time()),
            ("other-plugin", None),
            (PLUGIN_NAME, None),
        ]
    ):
        directory = service.jobs_root / f"job-{index:032x}"
        directory.mkdir()
        (directory / ".job.json").write_text(
            json.dumps({"plugin": owner, "retained_at": retained_at})
        )
        directories.append(directory)
    unrelated = service.jobs_root / "user-files"
    unrelated.mkdir()
    service._sweep(set())
    assert not directories[0].exists()
    assert all(d.exists() for d in directories[1:])
    service._sweep(set(), True)
    assert not directories[3].exists()
    assert directories[1].exists() and directories[2].exists() and unrelated.exists()
    outside = tmp_path / ("job-" + "e" * 32)
    outside.mkdir()
    (outside / ".job.json").write_text(json.dumps({"plugin": PLUGIN_NAME}))
    with pytest.raises(ValueError):
        service._remove_owned(outside)
    assert outside.exists()


async def test_cancelled_disk_worker_is_drained_before_return():
    import threading

    started, release, finished = (threading.Event() for _ in range(3))

    def disk_operation():
        started.set()
        release.wait(2)
        finished.set()

    task = asyncio.create_task(finish_thread(disk_operation))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


async def test_shutdown_during_timeout_cleanup_does_not_cancel_writes_twice(factory):
    draining, finished = asyncio.Event(), asyncio.Event()

    async def backend(album_id, images, settings, control):
        images.mkdir()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            draining.set()
            await asyncio.sleep(0.04)
            assert images.exists()
            (images / "final-write").write_bytes(b"done")
            finished.set()
            raise

    service = await factory(
        backend, settings=replace(Settings(), download_timeout_seconds=0.01)
    )
    job, _ = reserve(service, "1", "p:u", "p:private:u")
    service.launch(job)
    await asyncio.wait_for(draining.wait(), 1)
    await service.terminate()
    assert finished.is_set()
    assert not list(service.jobs_root.iterdir())


async def test_password_reaches_archive_without_leaking_into_status_or_metadata(
    factory, caplog
):
    password = "service-fixture-password"
    notices = []

    async def sender(origin, text, archive):
        notices.append(text)
        if archive:
            with pyzipper.AESZipFile(str(archive)) as result:
                result.setpassword(password.encode())
                assert result.read(result.namelist()[0]).startswith(b"\xff\xd8")
            raise RuntimeError("mock QQ refusal")

    service = await factory(
        sender=sender, settings=replace(Settings(), zip_password=password)
    )
    job, _ = reserve(service, "123456", "p:u", "p:group:g")
    service.launch(job)
    await finish_jobs(service)
    directory = service.jobs_root / job.id
    assert (directory / "JM123456.zip").is_file()
    assert password not in (directory / ".job.json").read_text()
    assert password not in " ".join(notices)
    assert password not in caplog.text
