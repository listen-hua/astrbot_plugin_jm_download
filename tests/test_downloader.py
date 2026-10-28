"""Exercise complete manifests, containment and pinned async library options."""

import io
import random
import traceback
from dataclasses import replace
from types import SimpleNamespace
from zipfile import ZipFile

import jmcomic
import pytest
import pyzipper
from astrbot_plugin_jm_download.downloader import (
    ArchiveError,
    DownloadedImage,
    IncompleteDownloadError,
    Settings,
    build_archive,
    download_album,
)
from PIL import Image


class SizedEntity:
    def __init__(self, count, index=None):
        self.count = count
        self.index = index

    def __len__(self):
        return self.count


def make_images(root, album="123456"):
    paths = []
    for chapter in (1, 2):
        for page in (1, 2):
            path = root / album / str(chapter) / f"{page:05}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (8, 8), (chapter * 40, page * 50, 0)).save(path)
            paths.append(DownloadedImage(path, chapter, page))
    return paths


def completed_result(images):
    chapters = {}
    for chapter_index in sorted(
        {image.chapter_index for image in images}, reverse=True
    ):
        entries = [image for image in images if image.chapter_index == chapter_index]
        chapter = SizedEntity(len(entries), chapter_index)
        chapters[chapter] = [
            (str(image.path), SimpleNamespace(index=image.page_index))
            for image in reversed(entries)
        ]
    album = SizedEntity(len(chapters))
    return SimpleNamespace(
        detail=album,
        downloader=SimpleNamespace(
            all_success=True, download_success_dict={album: chapters}
        ),
        manifest=SimpleNamespace(
            image_filepath_list=[str(image.path) for image in reversed(images)]
        ),
    )


def test_defaults_and_invalid_configuration():
    settings = Settings.from_config({})
    assert (settings.max_concurrent, settings.max_queue) == (1, 5)
    assert (settings.cooldown_seconds, settings.download_timeout_seconds) == (60, 600)
    assert (settings.max_zip_mib, settings.retention_hours) == (50, 24)
    assert settings.zip_password == ""
    for config in (
        {"max_concurrent": 0},
        {"max_queue": -1},
        {"max_zip_mib": True},
        {"api_domains": ["https://host"]},
        {"proxy": 42},
        {"zip_password": 42},
    ):
        with pytest.raises(ValueError):
            Settings.from_config(config)


async def test_real_option_and_complete_multichapter_manifest(tmp_path, monkeypatch):
    images = tmp_path / "含 空格" / "images"
    observed = {}

    async def fake_download(album_id, option, **kwargs):
        observed.update(id=album_id, option=option, **kwargs)
        paths = make_images(images)
        return completed_result(paths)

    monkeypatch.setattr(jmcomic, "download_album_async", fake_download)
    control = jmcomic.DownloadControl()
    paths = await download_album("123456", images, Settings(), control)
    assert observed["check_exception"] is True
    assert observed["control"] is control
    assert isinstance(observed["option"], jmcomic.JmOption)
    assert observed["option"].client.async_impl == "async_api"
    archive = tmp_path / "JM123456.zip"
    build_archive(paths, images, archive)
    with ZipFile(archive) as result:
        assert result.testzip() is None
        assert result.namelist() == [
            f"123456/{c:04}/{p:06}.jpg" for c in (1, 2) for p in (1, 2)
        ]
        assert all(
            result.read(name).startswith(b"\xff\xd8") for name in result.namelist()
        )


@pytest.mark.parametrize(
    "complete,chapter_count,manifest_count",
    [
        (False, 2, 4),
        (True, 1, 4),
        (True, 2, 3),
        (True, 2, 0),
    ],
)
async def test_incomplete_result_is_rejected(
    tmp_path, monkeypatch, complete, chapter_count, manifest_count
):
    album = SizedEntity(2)

    async def fake_download(*args, **kwargs):
        return SimpleNamespace(
            detail=album,
            downloader=SimpleNamespace(
                all_success=complete,
                download_success_dict={
                    album: {SizedEntity(2): [] for _ in range(chapter_count)}
                },
            ),
            manifest=SimpleNamespace(
                image_filepath_list=[
                    str(tmp_path / str(i)) for i in range(manifest_count)
                ]
            ),
        )

    monkeypatch.setattr(jmcomic, "download_album_async", fake_download)
    with pytest.raises(IncompleteDownloadError):
        await download_album(
            "1", tmp_path / "images", Settings(), jmcomic.DownloadControl()
        )


@pytest.mark.parametrize(
    "case", ["outside", "empty", "missing", "duplicate", "bad_chapter"]
)
def test_archive_rejects_unsafe_or_incomplete_files(tmp_path, case):
    root = tmp_path / "images"
    paths = make_images(root)
    if case == "outside":
        outside = tmp_path / "private.txt"
        outside.write_text("must not be sent", encoding="utf-8")
        paths.append(DownloadedImage(outside, 1, 3))
    elif case == "empty":
        paths[0].path.write_bytes(b"")
    elif case == "missing":
        paths[0].path.unlink()
    elif case == "duplicate":
        paths.append(paths[0])
    else:
        bad = root / "123456" / "title" / "page.jpg"
        bad.parent.mkdir()
        bad.write_bytes(b"unexpected")
        paths.append(DownloadedImage(bad, 3, 1))
    expected = (
        IncompleteDownloadError
        if case in {"empty", "missing", "duplicate"}
        else ArchiveError
    )
    with pytest.raises(expected):
        build_archive(paths, root, tmp_path / "album.zip")


async def test_reading_order_uses_metadata_across_twelve_chapters(
    tmp_path, monkeypatch
):
    root = tmp_path / "含 空格" / "images"
    records = []
    for chapter in range(1, 13):
        for page in range(1, 101):
            suffix = ".png" if page % 2 else ".PNG"
            path = root / "123456" / str(chapter) / f"raw-{page * 73 % 101}{suffix}"
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (2, 2), (chapter, page, 0)).save(path, format="PNG")
            records.append(DownloadedImage(path, chapter, page))
    random.Random(7).shuffle(records)

    async def fake_download(*args, **kwargs):
        return completed_result(records)

    monkeypatch.setattr(jmcomic, "download_album_async", fake_download)
    ordered = await download_album(
        "123456", root, Settings(), jmcomic.DownloadControl()
    )
    assert [(image.chapter_index, image.page_index) for image in ordered] == [
        (chapter, page) for chapter in range(1, 13) for page in range(1, 101)
    ]
    archive = tmp_path / "ordered.zip"
    build_archive(records, root, archive)
    extracted = tmp_path / "extracted"
    with ZipFile(archive) as result:
        assert len(result.namelist()) == 1200
        assert result.namelist() == sorted(result.namelist())
        result.extractall(extracted)
    for path in sorted((extracted / "123456").glob("*/*")):
        chapter, page = int(path.parent.name), int(path.stem)
        with Image.open(path) as image:
            assert image.getpixel((0, 0)) == (chapter, page, 0)
    assert all(record.path.exists() for record in records)


def test_padding_expands_uniformly_when_indices_exceed_minimum_width(tmp_path):
    records = []
    for chapter, page in ((2, 2), (10000, 1000000), (10, 10)):
        path = tmp_path / "images" / "123" / str(chapter) / "original.webp"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
        records.append(DownloadedImage(path, chapter, page))
    destination = tmp_path / "wide.zip"
    build_archive(records, tmp_path / "images", destination)
    with ZipFile(destination) as archive:
        assert archive.namelist() == [
            "123/00002/0000002.webp",
            "123/00010/0000010.webp",
            "123/10000/1000000.webp",
        ]


@pytest.mark.parametrize(
    "password", ["correct horse", "中文解压密码", "  spaces preserved  "]
)
def test_encryption_requires_exact_password_and_preserves_images(tmp_path, password):
    root = tmp_path / "images"
    records = make_images(root)
    destination = tmp_path / "protected.zip"
    build_archive(records, root, destination, password)
    with pyzipper.AESZipFile(str(destination)) as archive:
        assert all(info.flag_bits & 1 for info in archive.infolist())
        assert all(info.wz_aes_strength == 3 for info in archive.infolist())
        with pytest.raises(RuntimeError):
            archive.read(archive.namelist()[0])
        with pytest.raises(RuntimeError):
            archive.read(archive.namelist()[0], pwd=b"incorrect")
        archive.setpassword(password.encode("utf-8"))
        for record, name in zip(records, archive.namelist(), strict=True):
            assert archive.read(name) == record.path.read_bytes()
            with Image.open(io.BytesIO(archive.read(name))) as image:
                assert image.size == (8, 8)
        archive.extractall(str(tmp_path / "decrypted"))
    assert password.encode("utf-8") not in destination.read_bytes()
    assert password not in repr(Settings(zip_password=password))


def test_empty_password_keeps_standard_unencrypted_deflate_zip(tmp_path):
    records = make_images(tmp_path / "images")
    destination = tmp_path / "plain.zip"
    build_archive(records, tmp_path / "images", destination, "")
    with ZipFile(destination) as archive:
        assert all(
            not info.flag_bits & 1 and info.compress_type == 8
            for info in archive.infolist()
        )
        assert archive.testzip() is None


def test_crypto_failure_never_falls_back_or_exposes_password(tmp_path, monkeypatch):
    password = "local-test-secret"
    records = make_images(tmp_path / "images")

    def failed_encryption(*args, **kwargs):
        raise ValueError(f"fake crypto exception with {password}")

    monkeypatch.setattr(pyzipper, "AESZipFile", failed_encryption)
    with pytest.raises(ArchiveError) as caught:
        build_archive(records, tmp_path / "images", tmp_path / "failed.zip", password)
    assert password not in "".join(traceback.format_exception(caught.value))
    assert not (tmp_path / "failed.zip").exists()


@pytest.mark.parametrize(
    "case", ["duplicate_page", "duplicate_chapter", "zero_page", "wrong_manifest"]
)
async def test_corrupt_source_indices_are_not_packaged(tmp_path, monkeypatch, case):
    records = make_images(tmp_path / "images")
    result = completed_result(records)
    chapters = result.downloader.download_success_dict[result.detail]
    chapter_list = list(chapters)
    if case == "duplicate_page":
        chapters[chapter_list[0]][1][1].index = chapters[chapter_list[0]][0][1].index
    elif case == "duplicate_chapter":
        chapter_list[1].index = chapter_list[0].index
    elif case == "zero_page":
        chapters[chapter_list[0]][0][1].index = 0
    else:
        result.manifest.image_filepath_list[0] = str(tmp_path / "wrong.jpg")

    async def fake_download(*args, **kwargs):
        return result

    monkeypatch.setattr(jmcomic, "download_album_async", fake_download)
    with pytest.raises(IncompleteDownloadError):
        await download_album(
            "123456", tmp_path / "images", Settings(), jmcomic.DownloadControl()
        )


def test_duplicate_position_in_distinct_files_is_rejected(tmp_path):
    records = make_images(tmp_path / "images")
    records[1] = replace(records[1], page_index=records[0].page_index)
    with pytest.raises(IncompleteDownloadError):
        build_archive(records, tmp_path / "images", tmp_path / "duplicate.zip")
