"""Install this plugin into a selected Windows AstrBot launcher instance."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from build import FILES


def main():
    """Copy source and install dependencies with existing versions constrained.

    Raises:
        RuntimeError: The selected core, interpreter or installation is invalid.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-path", type=Path, required=True)
    parser.add_argument(
        "--root", type=Path, help="AstrBot runtime root, if different from core"
    )
    parser.add_argument("--skip-deps", action="store_true")
    args = parser.parse_args()
    core = args.core_path.resolve(strict=True)
    runtime = (args.root or core).resolve(strict=True)
    if (
        not (core / "astrbot" / "__init__.py").is_file()
        or not (core / "main.py").is_file()
    ):
        raise RuntimeError("The selected directory is not an AstrBot core checkout")
    interpreter = core.parent / "venv" / "Scripts" / "python.exe"
    if not interpreter.is_file():
        raise RuntimeError(
            "The selected AstrBot instance has no Windows venv interpreter"
        )
    if Path(sys.executable).resolve() != interpreter.resolve():
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        raise SystemExit(
            subprocess.call(
                [str(interpreter), str(Path(__file__).resolve()), *sys.argv[1:]],
                env=environment,
            )
        )
    root = Path(__file__).resolve().parents[1]
    target = runtime / "data" / "plugins" / "astrbot_plugin_jm_download"
    if target.is_symlink() or target.is_junction():
        raise RuntimeError("Refusing to replace a linked plugin directory")
    if not target.resolve().is_relative_to(runtime / "data" / "plugins"):
        raise RuntimeError("The target escaped the selected plugin directory")
    dependencies = runtime / "data" / "site-packages"
    dependencies.mkdir(parents=True, exist_ok=True)
    if not args.skip_deps:
        # AstrBot uses this dedicated directory; never upgrade the core venv.
        sys.path.insert(0, str(dependencies))
        versions = {}
        for distribution in importlib.metadata.distributions():
            name = distribution.metadata.get("Name")
            if name and not any(char.isspace() for char in name):
                versions.setdefault(
                    name.lower().replace("_", "-"), f"{name}=={distribution.version}"
                )
        with tempfile.TemporaryDirectory(prefix="jm-install-") as temporary:
            constraints = Path(temporary) / "constraints.txt"
            constraints.write_text("\n".join(versions.values()), encoding="utf-8")
            subprocess.run(
                [
                    str(interpreter),
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    "--target",
                    str(dependencies),
                    "--constraint",
                    str(constraints),
                    "--no-warn-script-location",
                    "-r",
                    str(root / "requirements.txt"),
                ],
                check=True,
            )
    if target.exists():
        from datetime import datetime

        backup = runtime / "data" / "backups" / "jm-plugin"
        backup.mkdir(parents=True, exist_ok=True)
        with ZipFile(
            backup / f"source-{datetime.now():%Y%m%d-%H%M%S-%f}.zip", "w", ZIP_DEFLATED
        ) as output:
            for name in FILES:
                if (target / name).is_file():
                    output.write(target / name, name)
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, target / name)
    print(f"Installed plugin source: {target}")
    plugin_data = runtime / "data" / "plugin_data" / "astrbot_plugin_jm_download"
    print(f"Plugin data directory: {plugin_data}")
    print("Reload plugins in the selected AstrBot dashboard, or restart that instance.")


if __name__ == "__main__":
    main()
