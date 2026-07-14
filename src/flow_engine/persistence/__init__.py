"""Persistence layer: SQLite connection, migrations, and transactions."""

from flow_engine.persistence.connection import Kernel, open_connection

__all__ = ["Kernel", "open_connection"]
