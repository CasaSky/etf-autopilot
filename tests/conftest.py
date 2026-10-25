from copy import deepcopy
from pathlib import Path

import pytest

from etf_autopilot.domain import load_json
from etf_autopilot.store import LocalStore

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config():
    return deepcopy(load_json(ROOT / "config/paper.json"))


@pytest.fixture
def events():
    return deepcopy(load_json(ROOT / "fixtures/monthly_events.json"))


@pytest.fixture
def store(tmp_path):
    return LocalStore(tmp_path / "paper.sqlite3")
