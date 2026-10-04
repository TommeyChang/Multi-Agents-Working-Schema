"""共用夹具。"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bg_coordinator.models import Actor, Line, Role  # noqa: E402
from bg_coordinator.storage import Store  # noqa: E402


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    """状态根在临时目录——**绝不碰真实工作区**。"""
    s = Store(root=tmp_path / "coordinator", repo=None)
    s.init()
    yield s
    # 临时目录由 pytest 清理


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    return r


def actor(role: Role = Role.TECH_LEAD, name: str = "tl-D", line: Line | None = Line.D) -> Actor:
    return Actor(role=role, name=name, line=line)
