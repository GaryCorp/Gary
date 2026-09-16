"""Chief-of-Staff operations for Gary: projects, tasks, follow-ups,
commitments, approvals, actions, planning runs, and the audit log.

SQLite is the source of truth for this structured state. The model never runs
SQL: it calls the narrow tools in ``gary.tools``, which validate input with
Pydantic models, apply business rules in ``gary.services``, and persist through
parameterized queries in ``gary.db.repositories``.
"""

from gary.container import Gary, build_gary

__all__ = ["Gary", "build_gary"]
