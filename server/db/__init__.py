"""Postgres archive layer: write-once persistence for finished orchestrations.

- session.py:   asyncpg connection pool + schema bootstrap (fail-open).
- repository.py: archive_orchestration() — single-transaction write of
  the result snapshot plus the full event list.
"""

from .repository import archive_orchestration

__all__ = ["archive_orchestration"]