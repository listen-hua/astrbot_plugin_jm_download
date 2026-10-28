"""Explicit conversation routing and contained NapCat filesystem mapping."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


class DeliveryError(RuntimeError):
    """A fixed, public-safe explanation of a failed file delivery."""


@dataclass(frozen=True)
class DeliveryTarget:
    """The original platform and recipient, captured before background execution."""

    origin: str
    platform_id: str
    group_id: str = ""
    user_id: str = ""


def validate_remote_jobs_dir(value: str) -> None:
    """Require an absolute remote directory without parent traversal.

    Args:
        value: An empty value, absolute POSIX path, or absolute Windows path.

    Raises:
        ValueError: The configured root is relative, contains a URL or traversal.
    """
    if not isinstance(value, str):
        raise ValueError("onebot_remote_jobs_dir must be a string")
    if not value:
        return
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    if (
        "://" in value
        or "\x00" in value
        or not (posix.is_absolute() or windows.is_absolute())
        or ".." in windows.parts
        or ".." in posix.parts
    ):
        raise ValueError("onebot_remote_jobs_dir must be an absolute directory")


def onebot_file_path(archive: Path, jobs_root: Path, remote_jobs_dir: str) -> str:
    """Map only an existing ZIP within the plugin jobs directory.

    Args:
        archive: Completed local ZIP.
        jobs_root: Owned plugin jobs directory mounted into both processes.
        remote_jobs_dir: Its absolute location in NapCat, or empty for same-path access.

    Returns:
        The path passed to NapCat's file upload action.

    Raises:
        ValueError: The ZIP escapes owned storage or the mapping is invalid.
        FileNotFoundError: The completed ZIP is not a readable local file.
    """
    validate_remote_jobs_dir(remote_jobs_dir)
    resolved = archive.resolve(strict=True)
    relative = resolved.relative_to(jobs_root.resolve(strict=True))
    if not resolved.is_file() or resolved.suffix.lower() != ".zip":
        raise FileNotFoundError("The completed ZIP is unavailable")
    if not remote_jobs_dir:
        return str(resolved)
    if PureWindowsPath(remote_jobs_dir).is_absolute():
        return str(PureWindowsPath(remote_jobs_dir).joinpath(*relative.parts))
    return str(PurePosixPath(remote_jobs_dir).joinpath(*relative.parts))
