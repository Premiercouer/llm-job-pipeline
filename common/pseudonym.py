"""Pseudonymous job ids for telemetry and event logs.

Job folder names contain company and position, so they reveal which jobs the candidate is
targeting. Telemetry records `job_run_id` instead: an HMAC-SHA256 of "<batch>/<job_folder>"
keyed with a random per-workspace salt. A plain hash would not be enough (company + title are
guessable); without the salt file the id cannot be linked back to a job. The mapping lives
only where the folder name already is: the tracker and the job folders.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from functools import lru_cache

from common import paths

SALT_FILE = ("telemetry_salt",)


@lru_cache(maxsize=1)
def _salt() -> bytes:
    path = paths.data_dir(".runtime", *SALT_FILE)
    if path.is_file():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # another process created it first
        return path.read_bytes()
    with os.fdopen(fd, "wb") as fh:
        fh.write(salt)
    return salt


def job_run_id(batch: str | None, job_folder: str | None) -> str | None:
    if not job_folder:
        return None
    msg = f"{batch or ''}/{job_folder}".encode("utf-8")
    return hmac.new(_salt(), msg, hashlib.sha256).hexdigest()[:16]
