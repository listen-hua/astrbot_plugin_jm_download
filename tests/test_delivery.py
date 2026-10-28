"""Validate cross-platform paths without depending on the machine's mount layout."""

from pathlib import Path

import pytest
from astrbot_plugin_jm_download.delivery import (
    onebot_file_path,
    validate_remote_jobs_dir,
)
from astrbot_plugin_jm_download.downloader import Settings


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "with spaces" / "jobs"
    file = root / "job-fixture" / "JM123.zip"
    file.parent.mkdir(parents=True)
    file.write_bytes(b"archive fixture")
    return file, root


def test_default_path_and_cross_container_mapping(archive):
    file, root = archive
    assert onebot_file_path(file, root, "") == str(file.resolve())
    assert (
        onebot_file_path(file, root, "/napcat/shared jobs")
        == "/napcat/shared jobs/job-fixture/JM123.zip"
    )
    assert (
        onebot_file_path(file, root, "C:\\NapCat\\jobs")
        == "C:\\NapCat\\jobs\\job-fixture\\JM123.zip"
    )


@pytest.mark.parametrize(
    "value",
    [
        "relative/jobs",
        "../jobs",
        "/jobs/../elsewhere",
        "C:jobs",
        "https://host/jobs",
        "/jobs\x00",
        None,
    ],
)
def test_invalid_remote_roots_are_rejected(value):
    with pytest.raises(ValueError):
        validate_remote_jobs_dir(value)
    with pytest.raises(ValueError):
        Settings.from_config({"onebot_remote_jobs_dir": value})


def test_missing_or_escaped_archive_never_reaches_napcat(archive, tmp_path):
    file, root = archive
    missing = root / "missing.zip"
    with pytest.raises(FileNotFoundError):
        onebot_file_path(missing, root, "/remote")
    outside = tmp_path / "private.zip"
    outside.write_bytes(b"must not upload")
    with pytest.raises(ValueError):
        onebot_file_path(outside, root, "/remote")
    with pytest.raises(ValueError):
        onebot_file_path(file, Path(file.parent), "../remote")
