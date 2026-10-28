"""Public packages and explicit-path upgrades preserve only intended files."""

import hashlib
import importlib
import json
import shutil
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def builder(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("build")


def test_reproducible_package_checksum_and_content(builder, tmp_path):
    version = builder.metadata(ROOT)["version"]
    archive = builder.build_package(ROOT, tmp_path / "first", version)
    repeated = builder.build_package(ROOT, tmp_path / "second", version)
    assert archive.read_bytes() == repeated.read_bytes()
    assert archive.stat().st_size < 16_000_000
    assert (archive.parent / "SHA256SUMS").read_text().split()[0] == hashlib.sha256(
        archive.read_bytes()
    ).hexdigest()
    with ZipFile(archive) as package:
        assert len(package.namelist()) == len(builder.FILES)
        assert f"{builder.NAME}/LICENSE" in package.namelist()
        assert f"{builder.NAME}/delivery.py" in package.namelist()
        assert all(".env" not in name for name in package.namelist())
    with pytest.raises(ValueError, match="tag"):
        builder.build_package(ROOT, tmp_path, "v0.0.0")
    with ZipFile(archive, "a") as package:
        package.writestr("private-config.json", "must never ship")
    with pytest.raises(ValueError, match="contents"):
        builder.validate_package(archive, ROOT)


def test_package_limit_and_stale_contents(builder, tmp_path, monkeypatch):
    archive = builder.build_package(ROOT, tmp_path)
    monkeypatch.setattr(builder, "MAX_PACKAGE_BYTES", 1)
    with pytest.raises(ValueError, match="16 MiB"):
        builder.validate_package(archive, ROOT)
    monkeypatch.undo()
    with ZipFile(archive, "w") as package:
        for name in builder.FILES:
            data = b"wrong" if name == "main.py" else (ROOT / name).read_bytes()
            package.writestr(f"{builder.NAME}/{name}", data)
    with pytest.raises(ValueError, match="Stale"):
        builder.validate_package(archive, ROOT)


def test_installer_requires_explicit_target(builder, monkeypatch):
    installer = importlib.import_module("install")
    monkeypatch.setattr(sys, "argv", ["install.py"])
    with pytest.raises(SystemExit) as error:
        installer.main()
    assert error.value.code == 2


def test_upgrade_in_path_with_spaces_preserves_configuration_and_data(
    builder, tmp_path, monkeypatch
):
    installer = importlib.import_module("install")
    core = tmp_path / "my bot instance" / "core"
    (core / "astrbot").mkdir(parents=True)
    (core / "astrbot" / "__init__.py").touch()
    (core / "main.py").touch()
    interpreter = core.parent / "venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    monkeypatch.setattr(sys, "executable", str(interpreter))
    monkeypatch.setattr(
        sys, "argv", ["install.py", "--core-path", str(core), "--skip-deps"]
    )
    config = core / "data" / "config" / f"{builder.NAME}_config.json"
    config.parent.mkdir(parents=True)
    old_config = {"zip_password": "test-only-secret", "max_queue": 7}
    config.write_text(json.dumps(old_config))
    retained = core / "data" / "plugin_data" / builder.NAME / "retained.zip"
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"saved administrator data")
    installer.main()
    target = core / "data" / "plugins" / builder.NAME
    (target / "main.py").write_bytes(b"previous version")
    installer.main()
    assert (target / "main.py").read_bytes() == (ROOT / "main.py").read_bytes()
    backups = list((core / "data" / "backups" / "jm-plugin").glob("*.zip"))
    assert len(backups) == 1
    with ZipFile(backups[0]) as backup:
        assert backup.read("main.py") == b"previous version"
    assert json.loads(config.read_text()) == old_config
    assert retained.read_bytes() == b"saved administrator data"
    # Removing only the source directory must leave administrator data available.
    shutil.rmtree(target)
    assert config.is_file() and retained.is_file()
