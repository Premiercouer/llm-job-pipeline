"""Model-usage telemetry for the 02 / 03 / 04 Claude workers (one JSONL line per attempt).

Default file: .runtime/telemetry/worker_usage.jsonl
    PIPELINE_TELEMETRY_PATH=/abs/file.jsonl   write somewhere else
    PIPELINE_TELEMETRY=0                      disable completely

Only counters and identifiers are recorded: never the prompt, JD, resume, quality report,
the model's result text or tool arguments. A failed write never fails the pipeline: it
prints one short warning to stderr (stdout is the stages' progress protocol).
Appends are serialized by a thread lock plus fcntl.flock on the file itself, and each
record is written with a single os.write, so parallel workers and processes never
interleave lines.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repository root
from common import paths  # noqa: E402

DEFAULT_PATH = paths.data_dir("resume", ".runtime", "telemetry", "worker_usage.jsonl")

RUN_ID = str(uuid.uuid4())  # one per script process
STAGES = ("02_v1", "03_prepare", "03_scan", "04_v2", "04_trim")
CLI_STATUSES = ("SUCCESS", "ERROR", "TIMEOUT", "MALFORMED_OUTPUT")
VALIDATION_STATUSES = ("VALID", "INVALID", "NOT_REACHED", "UNKNOWN")

USAGE_FIELDS = ("input_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens", "output_tokens")

_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("PIPELINE_TELEMETRY", "1").strip() != "0"


def telemetry_path() -> Path:
    custom = os.environ.get("PIPELINE_TELEMETRY_PATH", "").strip()
    return Path(custom).expanduser() if custom else DEFAULT_PATH


def new_invocation_id() -> str:
    return str(uuid.uuid4())


def model_requested(cmd: list[str]) -> str:
    """`--model X` from a worker command line, else 'inherit' (agent / CLI default)."""
    if "--model" in cmd:
        i = cmd.index("--model")
        if i + 1 < len(cmd):
            return str(cmd[i + 1])
    return "inherit"


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _counters(obj, depth: int = 0):
    """Keep only numbers / short strings / booleans (nested up to 2 levels): no free text."""
    if not isinstance(obj, dict) or depth > 2:
        return {}
    out = {}
    for k, v in obj.items():
        k = str(k)[:80]
        if isinstance(v, bool) or _num(v) is not None:
            out[k] = v
        elif isinstance(v, str) and len(v) <= 40:
            out[k] = v
        elif isinstance(v, dict):
            out[k] = _counters(v, depth + 1)
    return out


# snake_case first; the camelCase spelling is used only when the snake_case value is absent.
USAGE_ALIASES = {
    "input_tokens": "inputTokens",
    "cache_creation_input_tokens": "cacheCreationInputTokens",
    "cache_read_input_tokens": "cacheReadInputTokens",
    "output_tokens": "outputTokens",
}


def _pick(d: dict, snake: str, camel: str):
    """Numeric value under `snake`, else under `camel`, else None (missing is never 0)."""
    if not isinstance(d, dict):
        return None
    v = _num(d.get(snake))
    return v if v is not None else _num(d.get(camel))


def _dict(d: dict, snake: str, camel: str) -> dict:
    v = d.get(snake)
    if not isinstance(v, dict):
        v = d.get(camel)
    return v if isinstance(v, dict) else {}


def _actual_model(model_usage: dict, fallback: str | None) -> str | None:
    """The model that did the work: the modelUsage entry with the most output tokens."""
    if fallback:
        return str(fallback)[:80]
    best, best_out = None, -1
    for name, u in (model_usage or {}).items():
        out = _pick(u, "output_tokens", "outputTokens")
        if (out or 0) > best_out:
            best, best_out = name, out or 0
    return str(best)[:80] if best else None


def usage_fields(envelope: dict | None, init_model: str | None = None) -> dict:
    """Counters from a CLI result envelope (`--output-format json` or stream-json `result`)."""
    env = envelope if isinstance(envelope, dict) else {}
    usage = env.get("usage") if isinstance(env.get("usage"), dict) else {}
    model_usage = _dict(env, "model_usage", "modelUsage")
    fields = {k: _pick(usage, k, USAGE_ALIASES[k]) for k in USAGE_FIELDS}
    fields.update(
        num_turns=_pick(env, "num_turns", "numTurns"),
        total_cost_usd=_pick(env, "total_cost_usd", "totalCostUSD"),
        model_actual=_actual_model(model_usage, init_model),
        usage_raw=_counters(usage),
        model_usage_raw={str(k)[:80]: _counters(v) for k, v in model_usage.items()
                         if isinstance(v, dict)},
    )
    return fields


def error_code(text: str | None) -> str | None:
    """A short machine code for error_type: 'LOGIN_REQUIRED: ...' -> 'LOGIN_REQUIRED'."""
    if not text:
        return None
    head = str(text).split(":", 1)[0].strip()
    if head and len(head) <= 40 and all(c.isupper() or c.isdigit() or c == "_" for c in head):
        return head
    return None


class Invocation:
    """Collects one attempt's fields; `emit()` appends the JSONL line exactly once."""

    def __init__(self, stage: str, cmd: list[str], *, batch=None, job_folder=None,
                 attempt: int | None = 1, trim_iteration: int | None = None):
        self.t0 = time.monotonic()
        self.done = False
        self.rec = {
            "schema_version": SCHEMA_VERSION,
            "recorded_at": None,
            "run_id": RUN_ID,
            "invocation_id": new_invocation_id(),
            "pid": os.getpid(),
            "stage": stage,
            "batch": batch,
            "job_folder": job_folder,
            "attempt": attempt,
            "trim_iteration": trim_iteration,
            "model_requested": model_requested(cmd),
            "model_actual": None,
            "cli_status": None,
            "validation_status": "NOT_REACHED",
            "returncode": None,
            "duration_ms": None,
            "num_turns": None,
            **{k: None for k in USAGE_FIELDS},
            "total_cost_usd": None,
            "tool_counts": {},
            "usage_raw": {},
            "model_usage_raw": {},
            "error_type": None,
        }

    def cli(self, status: str, *, returncode=None, envelope=None, init_model=None,
            tool_counts=None, error_type=None, duration_s=None) -> None:
        self.rec["cli_status"] = status
        self.rec["returncode"] = returncode
        if duration_s is not None:
            self.rec["duration_ms"] = int(duration_s * 1000)
        self.rec.update(usage_fields(envelope, init_model))
        if tool_counts:
            self.rec["tool_counts"] = {str(k)[:80]: int(v) for k, v in tool_counts.items()}
        if error_type:
            self.rec["error_type"] = error_type

    def validation(self, status: str, error_type: str | None = None) -> None:
        self.rec["validation_status"] = status
        if error_type and not self.rec["error_type"]:
            self.rec["error_type"] = error_type

    def emit(self) -> None:
        if self.done:
            return
        self.done = True
        if self.rec["duration_ms"] is None:
            self.rec["duration_ms"] = int((time.monotonic() - self.t0) * 1000)
        if self.rec["cli_status"] is None:  # aborted before the CLI result was seen
            self.rec["cli_status"] = "ERROR"
            self.rec["error_type"] = self.rec["error_type"] or "aborted"
        self.rec["recorded_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
        append(self.rec)


def append(record: dict) -> None:
    """Append one JSON line under a thread + file lock. Never raises."""
    if not enabled():
        return
    try:
        line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        path = telemetry_path()
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    os.write(fd, line)
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
    except Exception as e:  # telemetry must never break a stage
        try:
            print(f"warning: telemetry write failed ({type(e).__name__})", file=sys.stderr, flush=True)
        except Exception:
            pass
