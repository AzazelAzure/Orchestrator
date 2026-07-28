"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Fail-open for unit tests: attestation fixture path + in-process runner stubs.
os.environ.setdefault("ORCH_TESTING", "1")

# pytest-django is optional; required only for R4 API tests when [control-plane] is installed.
try:
    import pytest_django  # noqa: F401

    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE", "flow_engine.control_plane.settings"
    )
    pytest_plugins = ["pytest_django"]
except ImportError:
    pytest_plugins = []

from flow_engine.persistence import Kernel


@pytest.fixture
def kernel_db(tmp_path: Path) -> Kernel:
    db_path = tmp_path / "state.db"
    kernel = Kernel.init(db_path)
    yield kernel
    kernel.close()
