"""Build and validate a reproducible, source-only AstrBot plugin archive."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

NAME = "astrbot_plugin_jm_download"
MAX_PACKAGE_BYTES = 16 * 1024 * 1024
FILES = (
    "__init__.py",
    "main.py",
    "delivery.py",
    "downloader.py",
    "service.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/DEPLOYMENT.md",
    "docs/COMPATIBILITY.md",
    "docs/MARKETPLACE.md",
)


def metadata(root: Path) -> dict:
    """Validate the public identity and version at the single source of truth."""
    import yaml

    data = yaml.safe_load((root / "metadata.yaml").read_text(encoding="utf-8"))
    if data["name"] != NAME or not re.fullmatch(r"v\d+\.\d+\.\d+", data["version"]):
        raise ValueError("Invalid plugin identity or release version")
    if data["author"] != "黎沈华(listen-hua)":
        raise ValueError("Unexpected public author")
    if data["repo"] != f"https://github.com/listen-hua/{NAME}":
        raise ValueError("Unexpected public repository")
    return data


def validate_package(archive: Path, root: Path) -> None:
    """Reject oversized, unexpected, corrupted or stale package contents."""
    if archive.stat().st_size > MAX_PACKAGE_BYTES:
        raise ValueError("Plugin package exceeds 16 MiB")
    with ZipFile(archive) as source:
        expected = {f"{NAME}/{name}" for name in FILES}
        names = source.namelist()
        if len(names) != len(expected) or set(names) != expected:
            raise ValueError("Unexpected package contents")
        if source.testzip() is not None:
            raise ValueError("Corrupted package")
        for name in FILES:
            if source.read(f"{NAME}/{name}") != (root / name).read_bytes():
                raise ValueError(f"Stale package file: {name}")


def build_package(root: Path, output: Path, expected_tag: str | None = None) -> Path:
    """Produce a versioned ZIP, checksum and changelog-derived release notes."""
    version = metadata(root)["version"]
    if expected_tag is not None and expected_tag != version:
        raise ValueError("Git tag does not match metadata.yaml version")
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"{NAME}-{version}.zip"
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for name in FILES:
            info = ZipInfo(f"{NAME}/{name}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (root / name).read_bytes())
    validate_package(destination, root)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    (output / "SHA256SUMS").write_text(
        f"{digest}  {destination.name}\n", encoding="utf-8"
    )
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = f"## {version}\n"
    if heading not in changelog:
        raise ValueError("Missing release changelog")
    notes = changelog.split(heading, 1)[1].split("\n## ", 1)[0].strip()
    (output / "RELEASE_NOTES.md").write_text(notes + "\n", encoding="utf-8")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-tag")
    parser.add_argument("--check", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.check:
        validate_package(args.check, root)
        print(f"Validated: {args.check}")
    else:
        print(build_package(root, args.output_dir or root / "dist", args.expected_tag))


if __name__ == "__main__":
    main()
