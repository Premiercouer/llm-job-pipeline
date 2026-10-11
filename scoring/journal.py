"""Crash-safe per-job journal: one JSON file per job, rewritten atomically on every transition.

Atomic write = write a temp file in the same directory, fsync it, os.replace() it over the old
file, fsync the directory. A reader therefore sees either the previous or the new version,
never a torn file, even across a power loss. A journal that cannot be parsed or fails the
schema check is CORRUPT: it is never overwritten and its job is blocked for an operator.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from scoring import states as S

SCHEMA = 1
REQUIRED_KEYS = {"schema", "job_id", "idempotency_key", "input_fingerprint", "state",
                 "attempt", "receipt", "report", "report_fingerprint", "history"}


class JournalError(Exception):
    """The journal file could not be written (the transition did not happen)."""


class JournalCorrupt(Exception):
    """The journal file exists but is unreadable or invalid; never overwritten."""


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class Journal:
    """In-memory view of one job's journal; `transition()` persists before returning."""

    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data

    # ------------------------------------------------------------ load / create
    @classmethod
    def load(cls, path: Path) -> "Journal | None":
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise JournalCorrupt(f"{path.name}: unreadable ({type(e).__name__})") from e
        if not isinstance(data, dict) or not REQUIRED_KEYS <= set(data):
            raise JournalCorrupt(f"{path.name}: missing keys")
        if data["schema"] != SCHEMA or data["state"] not in S.STATES:
            raise JournalCorrupt(f"{path.name}: schema {data.get('schema')!r} / state {data.get('state')!r}")
        return cls(path, data)

    @classmethod
    def new(cls, path: Path, job_id: str, idempotency_key: str, input_fp: str) -> "Journal":
        data = {"schema": SCHEMA, "job_id": job_id, "idempotency_key": idempotency_key,
                "input_fingerprint": input_fp, "state": S.READY, "attempt": 0, "receipt": None,
                "report": None, "report_fingerprint": None, "detail": "", "history": [],
                "created_at": time.time(), "updated_at": time.time()}
        return cls(path, data)

    # ------------------------------------------------------------------ fields
    @property
    def state(self) -> str:
        return self.data["state"]

    @property
    def detail(self) -> str:
        return self.data.get("detail", "")

    # -------------------------------------------------------------- transition
    def transition(self, target: str, detail: str = "", **fields) -> None:
        """Validate, then persist the new state (with any changed fields) atomically.
        On a write failure the in-memory view is rolled back and JournalError is raised."""
        S.check_transition(self.state, target)
        before = json.loads(json.dumps(self.data))
        now = time.time()
        self.data.update(fields)
        self.data["history"] = self.data["history"] + [
            {"from": before["state"], "to": target, "at": now, "detail": detail}]
        self.data.update(state=target, detail=detail, updated_at=now)
        try:
            atomic_write_json(self.path, self.data)
        except OSError as e:
            self.data = before
            raise JournalError(f"{self.path.name}: write failed ({e})") from e
