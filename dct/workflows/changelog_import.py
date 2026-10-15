"""Shared history-import boundary; never stamps or reconstructs a release."""
import re
from datetime import UTC, date, datetime

from dct.core.changelog import import_released_entry


def parse_released_at(raw: str) -> datetime | None:
    """Blank = import time; a date is midnight UTC, timestamps require an offset."""
    if not isinstance(raw, str):
        raise TypeError("released_at must be an ISO-8601 string")
    if not raw:
        return None
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
            return datetime.combine(date.fromisoformat(raw), datetime.min.time(), UTC)
        value = datetime.fromisoformat(raw)
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp needs an explicit UTC offset")
        return value.astimezone(UTC)
    except ValueError as exc:
        raise ValueError("released_at must be a valid ISO-8601 date or timestamp with an offset") from exc


def import_history(engine, project: str, category: str, entry: str, version: str,
                   released_at: datetime | None = None, *, component: str = "",
                   source_file: str = "", source_lines: str = "", related_item_id: int = 0) -> dict:
    """Import one approved row and return its saved history acknowledgement."""
    row = import_released_entry(engine, project, category, entry, version,
        released_at=released_at, component=component or None,
        source_file=source_file or None, source_lines=source_lines or None,
        related_item_id=related_item_id or None)
    saved_time = row["released_at"]
    if saved_time.tzinfo is None:
        saved_time = saved_time.replace(tzinfo=UTC)
    return {"id": row["id"], "project": project, "category": row["category"],
            "version": row["version"], "released_at": saved_time.isoformat(),
            "released_at_source": "stated" if released_at is not None else "import_time"}
