"""Migration journal: every move is recorded so it can be inspected and reverted."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Optional

_DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "CClear"
)
_JOURNAL = os.path.join(_DATA_DIR, "journal.json")
_BACKUP_DIR = os.path.join(_DATA_DIR, "backups")
LOG_NAME = "CClear.log"
_lock = threading.RLock()


def data_dir() -> str:
    os.makedirs(_DATA_DIR, exist_ok=True)
    return _DATA_DIR


def backup_dir() -> str:
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    return _BACKUP_DIR


def log_path() -> str:
    """Where main.py tees diagnostics for console-less builds (packaged exe)."""
    return os.path.join(data_dir(), LOG_NAME)


def _load() -> list[dict]:
    try:
        with open(_JOURNAL, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save(records: list[dict]) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = _JOURNAL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, _JOURNAL)


def add(record: dict) -> dict:
    with _lock:
        records = _load()
        record.setdefault("id", uuid.uuid4().hex[:10])
        record.setdefault("time", time.time())
        record.setdefault("status", "done")
        record.setdefault("reverted", False)
        records.append(record)
        _save(records)
        return record


def update(record_id: str, **fields) -> Optional[dict]:
    with _lock:
        records = _load()
        for r in records:
            if r.get("id") == record_id:
                r.update(fields)
                _save(records)
                return r
    return None


def all_records() -> list[dict]:
    with _lock:
        return _load()[::-1]


def get(record_id: str) -> Optional[dict]:
    for r in _load():
        if r.get("id") == record_id:
            return r
    return None


def stats() -> dict:
    records = _load()
    migrated = [r for r in records if r.get("action") in ("move_file", "move_dir", "move_app")]
    active = [r for r in migrated if not r.get("reverted")]
    return {
        "total": len(records),
        "migrated": len(migrated),
        "active": len(active),
        "reverted": len(migrated) - len(active),
        "freed_bytes": sum(int(r.get("size_bytes") or 0) for r in active),
        "clean_runs": len([r for r in records if r.get("action") == "clean"]),
        "cleaned_bytes": sum(int(r.get("size_bytes") or 0)
                             for r in records if r.get("action") == "clean"),
    }


def clear() -> int:
    with _lock:
        n = len(_load())
        _save([])
        return n
