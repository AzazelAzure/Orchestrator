"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from flow_engine.persistence import Kernel


@pytest.fixture
def kernel_db(tmp_path: Path) -> Kernel:
    db_path = tmp_path / "state.db"
    kernel = Kernel.init(db_path)
    yield kernel
    kernel.close()
