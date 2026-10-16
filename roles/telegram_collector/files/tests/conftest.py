import json
import re
from pathlib import Path

import pytest

from tg_archive.config import load_settings


@pytest.fixture
def project(tmp_path):
    source = Path(__file__).resolve().parents[1] / "tg-archive.toml"
    text = re.sub(
        r"(?m)^local_dir = .*",
        lambda _: "local_dir = " + json.dumps(str(tmp_path / "fetch")),
        source.read_text(encoding="utf-8"),
    )
    (tmp_path / "tg-archive.toml").write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture
def settings(project):
    return load_settings(project / "tg-archive.toml")
