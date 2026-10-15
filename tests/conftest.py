import logging
import shutil
import subprocess
from collections.abc import Generator
from pathlib import Path

import jubilant
import pytest
from _pytest.config.argparsing import Parser

logger = logging.getLogger()


def pytest_addoption(parser: Parser):
    parser.addoption(
        "--charm-path",
        help="Pre-built charm file to deploy, rather than building from source",
    )


@pytest.fixture(scope="module")
def charm_path(request) -> str:
    charm_file = request.config.getoption("--charm-path")
    if charm_file:
        return str(Path(charm_file).resolve())

    charm_dir = Path(__file__).resolve().parent.parent
    charmcraft = shutil.which("charmcraft") or "/snap/bin/charmcraft"
    subprocess.run(
        [charmcraft, "pack", "--verbose", "--project-dir", str(charm_dir)],
        check=True,
        cwd=charm_dir,
        capture_output=True,
        text=True,
    )

    return str(next(charm_dir.glob("*.charm")).resolve())


@pytest.fixture(scope="module")
def juju() -> Generator[jubilant.Juju, None, None]:
    with jubilant.temp_model() as juju:
        yield juju