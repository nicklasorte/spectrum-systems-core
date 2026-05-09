from __future__ import annotations

from .model import ALLOWED_STATUSES


def ensure_valid_status(status: str) -> None:
    if status not in ALLOWED_STATUSES:
        raise ValueError(
            f"invalid artifact status {status!r}; "
            f"allowed: {sorted(ALLOWED_STATUSES)}"
        )
