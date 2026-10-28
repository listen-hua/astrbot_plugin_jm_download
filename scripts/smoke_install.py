"""Load source and the built ZIP in isolated AstrBot runtime directories."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

from build import NAME, metadata, validate_package

CHILD = """
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    'smoke_plugin', root / '__init__.py', submodule_search_locations=[str(root)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
from smoke_plugin.main import JmDownloadPlugin
schema = json.loads((root / '_conf_schema.json').read_text(encoding='utf-8'))
config = {name: item['default'] for name, item in schema.items()}
async def verify():
    plugin = JmDownloadPlugin(SimpleNamespace(), config)
    await plugin.initialize()
    assert plugin.service.jobs_root.is_dir()
    await plugin.terminate()
asyncio.run(verify())
print('Plugin initialized and terminated successfully')
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, nargs="?")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    archive = args.archive or root / "dist" / f"{NAME}-{metadata(root)['version']}.zip"
    validate_package(archive, root)
    with tempfile.TemporaryDirectory(prefix="jm-install-check-") as temporary:
        working = Path(temporary)
        extracted = working / "unpacked"
        with ZipFile(archive) as source:
            source.extractall(extracted)
        for index, plugin_root in enumerate((root, extracted / NAME)):
            environment = os.environ.copy()
            environment["ASTRBOT_ROOT"] = str(working / f"runtime-{index}")
            environment["ASTRBOT_DISABLE_METRICS"] = "1"
            subprocess.run(
                [sys.executable, "-c", CHILD, str(plugin_root)],
                cwd=working,
                env=environment,
                check=True,
            )


if __name__ == "__main__":
    main()
