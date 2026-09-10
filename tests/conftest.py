from pathlib import Path

import pytest

from sql_exporter.config import load_config
from sql_exporter.mapping import load_mapping

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mapping():
    return load_mapping(ROOT / "examples/mapping.xlsx")


@pytest.fixture
def config(tmp_path):
    config = load_config(ROOT / "examples/demo.json")
    config["output_dir"] = str(tmp_path / "export")
    return config

