"""Download complete albums and build deterministic, contained ZIP archives."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, fields
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import jmcomic
import pyzipper

from .delivery import validate_remote_jobs_dir


@dataclass(frozen=True)
class Settings:
    """Configuration values captured once when the plugin is loaded."""

    max_concurrent: int = 1
    max_queue: int = 5
    cooldown_seconds: int = 60
    download_timeout_seconds: int = 600
    max_zip_mib: int = 50
    retention_hours: int = 24
    proxy: str = ""
    api_domains: tuple[str, ...] = ()
    zip_password: str = field(default="", repr=False)
    onebot_remote_jobs_dir: str = ""

    @classmethod
    def from_config(cls, config: dict) -> Settings:
        """Validate dashboard values rather than accepting unbounded work.

        Args:
            config: AstrBot's plugin configuration mapping.

        Returns:
            Validated settings with defaults for absent fields.

        Raises:
            ValueError: A configured field has an invalid type or range.
        """
        values = {
            field.name: config[field.name]
            for field in fields(cls)
            if field.name in config
        }
        ranges = {
            "max_concurrent": (1, 8),
            "max_queue": (0, 100),
            "cooldown_seconds": (0, 3600),
            "download_timeout_seconds": (5, 3600),
            "max_zip_mib": (1, 1024),
            "retention_hours": (1, 168),
        }
        for name, (low, high) in ranges.items():
            value = values.get(name, getattr(cls(), name))
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer between {low} and {high}")
        if not isinstance(values.get("proxy", ""), str):
            raise ValueError("proxy must be a string")
        if not isinstance(values.get("zip_password", ""), str):
            raise ValueError("zip_password must be a string")
        validate_remote_jobs_dir(values.get("onebot_remote_jobs_dir", ""))
        domains = values.get("api_domains", ())
        if not isinstance(domains, (list, tuple)) or any(
            not isinstance(domain, str)
            or not domain
            or any(c in domain for c in "/\\ :\t\n")
            for domain in domains
        ):
            raise ValueError(
                "api_domains must contain hostnames without schemes or paths"
            )
        values["api_domains"] = tuple(domains)
        return cls(**values)


class IncompleteDownloadError(Exception):
    """The downloader returned missing, empty or incomplete image records."""


class ArchiveError(Exception):
    """The archive could not be safely created from the downloaded manifest."""


@dataclass(frozen=True)
class DownloadedImage:
    """An image's source path and authoritative one-based reading position."""

    path: Path
    chapter_index: int
    page_index: int


async def download_album(
    album_id: str,
    images_root: Path,
    settings: Settings,
    control: jmcomic.DownloadControl,
) -> list[DownloadedImage]:
    """Download every chapter with the pinned library's native async API.

    Args:
        album_id: Validated positive decimal album identifier.
        images_root: Unique per-job download directory.
        settings: Plugin configuration.
        control: Cancellation signal shared with the library's decode workers.

    Returns:
        The complete image manifest with original chapter and page positions.

    Raises:
        IncompleteDownloadError: Any chapter or image is missing.
        jmcomic.JmcomicException: Metadata or image retrieval fails.
    """
    await asyncio.to_thread(images_root.mkdir, parents=True, exist_ok=True)
    metadata = {"timeout": 30}
    if settings.proxy:
        metadata["proxies"] = settings.proxy
    client = {
        "impl": "api",
        "async_impl": "async_api",
        "retry_times": 3,
        "postman": {"meta_data": metadata},
    }
    if settings.api_domains:
        client["domain"] = {"api": list(settings.api_domains)}
    option = jmcomic.JmOption.construct(
        {
            "log": False,
            "client": client,
            "dir_rule": {
                "base_dir": str(images_root),
                "rule": "Bd_Aid_Pindex",
            },
            "download": {
                "cache": False,
                "image": {"decode": True},
                "threading": {"image": 4, "photo": 1},
            },
        }
    )
    result = await jmcomic.download_album_async(
        album_id, option, check_exception=True, control=control
    )
    recorded = result.downloader.download_success_dict.get(result.detail, {})
    if not result.downloader.all_success or len(recorded) != len(result.detail):
        raise IncompleteDownloadError("Not all chapters and images were downloaded")
    paths = [Path(path) for path in result.manifest.image_filepath_list]
    expected_count = sum(len(chapter) for chapter in recorded)
    if not paths or len(paths) != expected_count or len(set(paths)) != len(paths):
        raise IncompleteDownloadError("The image manifest is incomplete or duplicated")
    images = []
    positions = set()
    chapters = set()
    for chapter, records in recorded.items():
        chapter_index = chapter.index
        if (
            type(chapter_index) is not int
            or chapter_index <= 0
            or chapter_index in chapters
        ):
            raise IncompleteDownloadError("Invalid or duplicate chapter index")
        chapters.add(chapter_index)
        if len(records) != len(chapter):
            raise IncompleteDownloadError("The chapter's image records are incomplete")
        for path, image in records:
            page_index = image.index
            position = (chapter_index, page_index)
            if (
                type(page_index) is not int
                or not 1 <= page_index <= len(chapter)
                or position in positions
            ):
                raise IncompleteDownloadError("Invalid or duplicate page index")
            positions.add(position)
            images.append(DownloadedImage(Path(path), chapter_index, page_index))
    if len(images) != len(paths) or {image.path for image in images} != set(paths):
        raise IncompleteDownloadError(
            "Image records do not match the completed manifest"
        )
    return sorted(images, key=lambda image: (image.chapter_index, image.page_index))


def build_archive(
    images: list[DownloadedImage], images_root: Path, archive: Path, password: str = ""
) -> None:
    """Package only nonempty manifest files within the job's image root.

    Args:
        images: Complete manifest with source chapter and page indices.
        images_root: Directory that all manifest files must belong to.
        archive: ZIP destination outside the image directory.
        password: Optional UTF-8 password; an empty string disables encryption.

    Raises:
        IncompleteDownloadError: A file or reading position is missing or duplicated.
        ArchiveError: A file is unsafe or the ZIP cannot be created or encrypted.
    """
    try:
        root = images_root.resolve()
        entries = {}
        source_paths = set()
        album_ids = set()
        for image in images:
            resolved = image.path.resolve()
            relative = resolved.relative_to(root)
            # Numeric album and chapter directories avoid Windows title/path issues.
            if len(relative.parts) != 3 or not all(
                p.isascii() and p.isdecimal() for p in relative.parts[:2]
            ):
                raise ValueError("Unexpected chapter path in the manifest")
            if not resolved.is_file() or resolved.stat().st_size == 0:
                raise IncompleteDownloadError("Missing or empty image in the manifest")
            position = (image.chapter_index, image.page_index)
            if any(type(index) is not int or index <= 0 for index in position):
                raise IncompleteDownloadError("Invalid reading position")
            if position in entries or resolved in source_paths:
                raise IncompleteDownloadError("Duplicate image or reading position")
            if int(relative.parts[1]) != image.chapter_index:
                raise IncompleteDownloadError(
                    "Chapter directory does not match metadata"
                )
            source_paths.add(resolved)
            album_ids.add(relative.parts[0])
            entries[position] = resolved
        if not entries:
            raise IncompleteDownloadError("The album has no image files")
        if len(album_ids) != 1:
            raise IncompleteDownloadError("The archive contains more than one album")
        album_id = next(iter(album_ids))
        chapter_width = max(4, len(str(max(chapter for chapter, _ in entries))))
        page_width = max(6, len(str(max(page for _, page in entries))))
        archive_type = pyzipper.AESZipFile if password else ZipFile
        with archive_type(
            str(archive), "w", compression=ZIP_DEFLATED, compresslevel=1
        ) as output:
            if password:
                output.setpassword(password.encode("utf-8"))
                output.setencryption(pyzipper.WZ_AES, nbits=256)
            for (chapter, page), path in sorted(entries.items()):
                name = (
                    f"{album_id}/{chapter:0{chapter_width}d}/"
                    f"{page:0{page_width}d}{path.suffix}"
                )
                output.write(str(path), name)
    except IncompleteDownloadError:
        raise
    except Exception as exc:
        if password:
            # Do not allow crypto exception details to echo a password into logs.
            raise ArchiveError("Unable to create an encrypted ZIP archive") from None
        raise ArchiveError("Unable to create a complete ZIP archive") from exc


async def finish_thread(func, *args):
    """Drain a disk worker before propagating coroutine cancellation.

    Args:
        func: Blocking archive or filesystem operation.
        *args: Arguments passed to the operation.

    Returns:
        The operation's return value.

    Raises:
        asyncio.CancelledError: The caller was cancelled after the worker finished.
    """
    task = asyncio.create_task(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await drain_task(task)
        raise


async def drain_task(task: asyncio.Task) -> None:
    """Wait for I/O cleanup even if the parent receives repeated cancellation.

    Args:
        task: A task whose exception will be consumed after it has stopped.
    """
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if not task.cancelled():
        try:
            task.result()
        except Exception:
            pass
