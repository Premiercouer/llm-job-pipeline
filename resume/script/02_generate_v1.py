#!/usr/bin/env python3
"""Stage 2: generate Resume V1 for every job in a batch.

job.json -> fresh `claude -p --agent resume-v1-worker` -> validate
         -> resume_v1 written back into job.json -> pipeline_tracker.csv

1 job = 1 fresh Claude process. Claude only generates text; this script owns
every file and tracker write. Tracker columns owned by this stage:
v1_status, last_error, updated_at.

Usage:
    python3 02_generate_v1.py                          # newest batch
    python3 02_generate_v1.py --batch /abs/path/to/batch
    python3 02_generate_v1.py --limit 3 --workers 3
    python3 02_generate_v1.py --force                  # regenerate existing V1
    python3 02_generate_v1.py --batch B --job FOLDER --force   # one job only
    python3 02_generate_v1.py --dry-run

After a real (non-dry) run, writes the 02 -> 03 handoff manifest
.runtime/handoff/<batch>/v1_ready.json listing every job in the batch whose
job.json has a valid resume_v1 and whose tracker row has v1_status == DONE.

Progress protocol (line-buffered):
    [DONE|FAILED|SKIPPED] Company — Position [| reason]
    PROGRESS V1 i/N | DONE d FAILED f SKIPPED s
    DONE exit=<code>                                   (always last)
"""

from __future__ import annotations

import argparse
import importlib.util
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent  # resume/: agents + instructions staged into the worker cwd
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import pipeline_telemetry as telemetry  # noqa: E402  (worker usage JSONL; never fails a stage)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repository root
from common import candidate, output_guard, paths, pseudonym, single_turn, worker_dir  # noqa: E402
DATA_ROOT = paths.data_dir("resume")
DEFAULT_JOBS_ROOT = DATA_ROOT / "jobs"
HANDOFF_ROOT = DATA_ROOT / ".runtime" / "handoff"
HANDOFF_NAME = "v1_ready.json"

AGENT = "resume-v1-worker"
DEFAULT_TIMEOUT = 300
DEFAULT_WORKERS = 3
MAX_ATTEMPTS = 2  # first try + 1 retry with a fresh worker

BATCH_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{4})(?:_(\d{2,}))?$")
BASE_FIELDS = ("company", "position", "jd", "apply_url", "source_key")
WORKER_FIELDS = ("company", "position", "jd")
OWNED_COLUMNS = ("v1_status", "last_error", "updated_at")

SECTION_MARKERS = (
    "SUMMARY OF QUALIFICATIONS", "CORE SKILLS",
    "PROFESSIONAL EXPERIENCE", "PROJECT EXPERIENCE", "EDUCATION",
)


def required_markers() -> tuple[str, ...]:
    """Section headings + the candidate's name, employers and schools (candidate.json)."""
    return SECTION_MARKERS + candidate.resume_markers()


@functools.lru_cache(maxsize=1)
def worker_cwd() -> Path:
    """One staged, read-only cwd per process for every agent worker of stages 02 / 04 / cover letter."""
    candidate.load()  # fail fast on a malformed candidate.json / missing knowledge file
    return worker_dir.stage(ROOT, candidate.knowledge_dir(),
                            {"knowledge/candidate.json": candidate.candidate_file()})
HTML_MARKERS = ("<html", "<head", "<body", "<div")


def _load_stage1():
    """Reuse 01's tracker schema and atomic-write helpers (single source)."""
    spec = importlib.util.spec_from_file_location(
        "prepare_jobs", SCRIPT_DIR / "01_prepare_jobs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


p01 = _load_stage1()
TRACKER_NAME = p01.TRACKER_NAME


class PipelineError(Exception):
    """Fail-closed error for the whole run (before any Claude call)."""


class JobError(Exception):
    """Failure of a single job; other jobs continue."""


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- discovery

def batch_sort_key(name: str):
    m = BATCH_NAME_RE.match(name)
    if not m:
        return None
    try:
        stamp = datetime.strptime(m.group(1), "%Y-%m-%d_%H%M")
    except ValueError:
        return None
    return (stamp, int(m.group(2) or 1))


def find_latest_batch(jobs_root: Path) -> Path:
    if not jobs_root.is_dir():
        raise PipelineError(f"jobs root not found: {jobs_root}")
    batches = [(key, p) for p in jobs_root.iterdir()
               if p.is_dir() and not p.name.startswith(".")
               and (key := batch_sort_key(p.name)) is not None]
    if not batches:
        raise PipelineError(f"no batch folders (YYYY-MM-DD_HHMM[_NN]) in {jobs_root}")
    return max(batches, key=lambda b: b[0])[1]


def resolve_batch(arg: str | None, jobs_root: Path) -> Path:
    if not arg:
        return find_latest_batch(jobs_root)
    path = Path(arg).expanduser()
    if not path.is_absolute():
        path = jobs_root / path
    path = path.resolve()
    if not path.is_dir():
        raise PipelineError(f"batch folder not found: {path}")
    if batch_sort_key(path.name) is None:
        raise PipelineError(f"not a batch folder name (YYYY-MM-DD_HHMM[_NN]): {path.name}")
    return path


@dataclass
class Job:
    folder: str
    path: Path          # job.json
    data: dict
    has_valid_v1: bool

    @property
    def label(self) -> str:
        return f"{self.data['company']} — {self.data['position']}"


def validate_job_data(data, where: str) -> None:
    if not isinstance(data, dict):
        raise PipelineError(f"{where}: root must be a JSON object")
    for field in BASE_FIELDS:
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise PipelineError(f"{where}: '{field}' must be a non-empty string")


def load_jobs(batch: Path) -> list[Job]:
    folders = sorted(p for p in batch.iterdir()
                     if p.is_dir() and not p.name.startswith("."))
    if not folders:
        raise PipelineError(f"no job folders in {batch}")
    jobs = []
    for folder in folders:
        path = folder / "job.json"
        where = f"{folder.name}/job.json"
        if not path.is_file():
            raise PipelineError(f"{where}: missing")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise PipelineError(f"{where}: unreadable ({e})") from e
        validate_job_data(data, where)
        existing = data.get("resume_v1")
        valid = (isinstance(existing, str)
                 and validate_resume(existing, data["position"]) is None)
        jobs.append(Job(folder.name, path, data, valid))
    return jobs


# ------------------------------------------------------------------ tracker

def tracker_path_for(batch: Path) -> Path:
    return batch.parent / TRACKER_NAME


def read_tracker(tracker: Path) -> list[dict]:
    if not tracker.is_file():
        raise PipelineError(f"tracker not found: {tracker}")
    try:
        return p01.read_tracker(tracker)
    except p01.PrepareError as e:
        raise PipelineError(str(e)) from e


def check_tracker_rows(tracker: Path, batch: str, jobs: list[Job]) -> None:
    """Every job must map to exactly one (batch, job_folder) row."""
    counts: dict[str, int] = {}
    for row in read_tracker(tracker):
        if row["batch"] == batch:
            counts[row["job_folder"]] = counts.get(row["job_folder"], 0) + 1
    problems = [f"{job.folder}: {counts.get(job.folder, 0)} rows"
                for job in jobs if counts.get(job.folder, 0) != 1]
    if problems:
        raise PipelineError(
            f"tracker inconsistency for batch {batch} (need exactly 1 row each):\n  "
            + "\n  ".join(problems))


_tracker_lock = threading.Lock()


def update_tracker(tracker: Path, batch: str, folder: str, **fields) -> None:
    """Locked read-modify-write of this stage's columns on one row."""
    assert set(fields) <= set(OWNED_COLUMNS), fields
    with _tracker_lock:
        rows = p01.read_tracker(tracker)
        matches = [r for r in rows
                   if r["batch"] == batch and r["job_folder"] == folder]
        if len(matches) != 1:
            raise JobError(f"tracker has {len(matches)} rows for {batch}/{folder}")
        matches[0].update(fields, updated_at=now_str())
        p01.write_tracker_atomic(tracker, rows)


def get_tracker_row(tracker: Path, batch: str, folder: str) -> dict:
    with _tracker_lock:
        for row in p01.read_tracker(tracker):
            if row["batch"] == batch and row["job_folder"] == folder:
                return row
    raise JobError(f"tracker row missing for {batch}/{folder}")


# --------------------------------------------------------------- validation

# Common title abbreviations, so 'App. Data & BI Developer' matches
# 'Application Data & BI Developer' and vice versa.
_ABBREV = {"app": "application", "apps": "application", "applications": "application", "sr": "senior", "jr": "junior",
           "mgr": "manager", "eng": "engineer", "dev": "developer",
           "assoc": "associate", "coord": "coordinator", "spec": "specialist"}


# Level suffixes a resume title may drop: 'Business Analyst I' -> 'BUSINESS ANALYST'.
_LEVEL_WORDS = ("i", "ii", "iii", "iv", "1", "2", "3", "4")
# Words that name a program or term, not a role ('Internship | Winter 2027').
_NO_ROLE_WORDS = {"internship", "intern", "co", "op", "coop", "student", "program",
                  "winter", "summer", "fall", "spring", "autumn", "term", "months", "month"}


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.casefold().replace("&", " and "))
    return [_ABBREV.get(w, w) for w in words]


def _contains_run(haystack: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return n > 0 and any(haystack[i:i + n] == needle
                         for i in range(len(haystack) - n + 1))


def title_present(resume: str, position: str) -> bool:
    """Case/space/punctuation-insensitive title match.

    Passes if some resume line contains the full position, if some line is
    a contiguous run of at least half of the position's words (handles
    upstream noise such as 'Analyst (1)' or a trailing posting date), or if
    some line is exactly the position's leading segment before a separator
    ('Data Scientist II - QuantumBlack, AI by McKinsey' -> 'DATA SCIENTIST II'),
    or if some line is exactly the position's first two or more words, or if
    some line is two or more position words directly followed in the position
    by a level token ('RQ11615 - Business Analyst I Toronto' -> 'BUSINESS ANALYST').
    A position made only of program/term words ('Internship | Winter 2027')
    names no role, so any title passes.
    """
    pos = _tokens(position)
    if not pos:
        return False
    if all(w in _NO_ROLE_WORDS or re.fullmatch(r"(19|20)\d\d", w) for w in pos):
        return True
    min_run = max(1 if len(pos) == 1 else 2, -(-len(pos) // 2))
    lead = _tokens(re.split(r"\s[-–—|:]\s|[,(|:]", position, maxsplit=1)[0])
    for line in resume.splitlines():
        words = _tokens(line)
        if _contains_run(words, pos):
            return True
        if len(words) >= min_run and _contains_run(pos, words):
            return True
        if lead and words == lead:
            return True
        # 'Business Analyst in Toronto, ON - Onsite' -> 'BUSINESS ANALYST'
        if len(words) >= 2 and words == pos[:len(words)]:
            return True
        if len(words) >= 2 and any(_contains_run(pos, words + [lvl]) for lvl in _LEVEL_WORDS):
            return True
    return False


def validate_resume(resume, position: str) -> str | None:
    """Return None if structurally valid, else a short reason."""
    if not isinstance(resume, str) or not resume.strip():
        return "resume_v1 is empty or not a string"
    flat = " ".join(resume.split()).casefold()
    missing = [m for m in required_markers() if m.casefold() not in flat]
    if missing:
        return "resume missing: " + ", ".join(missing)
    html = [m for m in HTML_MARKERS if m in flat]
    if html:
        return "resume contains HTML: " + ", ".join(html)
    if not title_present(resume, position):
        head = " / ".join([l.strip() for l in resume.splitlines() if l.strip()][3:6])[:160]
        return f"resume title does not match position {position!r} (title area: {head!r})"
    return None


# ------------------------------------------------------------------ claude

def find_claude() -> str:
    """Locate the Claude Code CLI: $CLAUDE_BIN, then PATH, then the desktop-app bundle."""
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit:
        if not os.access(explicit, os.X_OK):
            raise PipelineError(f"CLAUDE_BIN is not executable: {explicit}")
        return explicit
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    bundle_root = Path.home() / "Library/Application Support/Claude/claude-code"
    # Layout is <version>/claude.app/... or, in newer builds, <version>/<build-hash>/claude.app/...
    bundled = [p for pattern in ("*/claude.app/Contents/MacOS/claude",
                                 "*/*/claude.app/Contents/MacOS/claude")
               for p in bundle_root.glob(pattern) if os.access(p, os.X_OK)]

    def version_key(p):
        version = p.relative_to(bundle_root).parts[0]
        return tuple(int(m.group()) if (m := re.match(r"\d+", part)) else 0
                     for part in version.split("."))

    if bundled:
        return str(max(bundled, key=version_key))
    raise PipelineError("Claude Code CLI not found; set CLAUDE_BIN to its path")


WORKER_SINGLE_TURN, WORKER_AGENT = "single-turn", "agent"
WORKER_MODES = (WORKER_SINGLE_TURN, WORKER_AGENT)
WORKER_ENV = "LJP_RESUME_WORKER"  # default mode for 02 / 04 / cover letter when --worker is not given
# agent -> (rule file, needs candidate knowledge). trim and cover letter never see the knowledge files.
WORKER_RULES = {"resume-v1-worker": ("resume_v1.md", True), "resume-v2-worker": ("resume_v2.md", True),
                "resume-trim-worker": ("resume_trim.md", False),
                "cover-letter-worker": ("cover_letter.md", False)}


def resolve_worker_mode(cli_value: str | None = None) -> str:
    mode = cli_value or os.environ.get(WORKER_ENV) or WORKER_SINGLE_TURN
    if mode not in WORKER_MODES:
        raise PipelineError(f"unknown worker mode {mode!r} (choose from {', '.join(WORKER_MODES)})")
    return mode


@functools.lru_cache(maxsize=1)
def prompt_files() -> "single_turn.PromptFiles":
    """Private run dir with one system-prompt file per worker kind and an empty cwd."""
    return single_turn.PromptFiles("resume")


def worker_command(claude_bin: str, model: str | None = None, agent: str = AGENT,
                   mode: str | None = None, tools: str = "") -> list[str]:
    """single-turn (default): no tools, rules (+ knowledge for V1 / V2) in a system-prompt file.
    agent (rollback): `claude -p --agent <agent>` reading its files with Read from a staged cwd."""
    if resolve_worker_mode(mode) == WORKER_SINGLE_TURN:
        rule_file, with_knowledge = WORKER_RULES[agent]
        if with_knowledge:
            candidate.load()  # fail fast on a malformed candidate.json / missing knowledge file
        path = prompt_files().file_for(agent, lambda: single_turn.build_system_prompt(
            ROOT, agent, rule_file, with_knowledge))
        return single_turn.command(claude_bin, path, model, tools)
    return _agent_command(claude_bin, model, agent)


def run_cwd(cmd: list[str]) -> Path:
    return prompt_files().cwd if single_turn.is_single_turn(cmd) else worker_cwd()


def worker_env(cmd: list[str]) -> dict:
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # allow launching from inside a Claude Code terminal
    if single_turn.is_single_turn(cmd):
        env.update(single_turn.ENV)
    return env


def _agent_command(claude_bin: str, model: str | None, agent: str) -> list[str]:
    cmd = [
        claude_bin, "-p",
        "--agent", agent,
        "--output-format", "json",       # envelope with is_error + result text
        "--no-session-persistence",      # nothing saved, nothing resumable
        "--strict-mcp-config",           # no MCP servers in the worker
        "--disable-slash-commands",
        "--tools", "Read",
        "--allowedTools", "Read",
        "--permission-mode", "dontAsk",  # anything not pre-allowed is denied
    ]
    if model:
        cmd += ["--model", model]
    return cmd


def build_prompt(job: dict) -> str:
    payload = {field: job[field] for field in WORKER_FIELDS}
    return single_turn.wrap_job(
        "请严格按照 instructions/resume_v1.md，为下面这 1 个职位生成 Resume V1。"
        '只返回 1 个 JSON object：{"resume_v1": "..."}，不要输出任何其他内容。', payload)


def parse_worker_output(stdout: str) -> dict:
    """Unwrap the CLI JSON envelope and parse the worker's result JSON."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        raise JobError("CLI output is not JSON")
    if (not isinstance(envelope, dict) or envelope.get("is_error")
            or envelope.get("subtype") != "success"):
        detail = envelope.get("subtype") if isinstance(envelope, dict) else "?"
        raise JobError(f"worker reported error ({detail})")
    text = (envelope.get("result") or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        raise JobError("invalid JSON in worker result")
    if not isinstance(result, dict):
        raise JobError("worker result is not a JSON object")
    if set(result) != {"resume_v1"}:
        raise JobError(f"worker result keys must be exactly ['resume_v1'], got {sorted(result)}")
    return result


def _envelope(stdout: str):
    """The CLI JSON envelope, or None (telemetry only; parsing errors are handled below)."""
    try:
        env = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    return env if isinstance(env, dict) else None


def generate_once(job: dict, cmd: list[str], timeout: int, tele=None) -> str:
    """One fresh worker. `tele` (pipeline_telemetry.Invocation) only records counters."""
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, input=build_prompt(job), capture_output=True,
                              text=True, cwd=run_cwd(cmd), env=worker_env(cmd), timeout=timeout)
    except subprocess.TimeoutExpired:
        if tele:
            tele.cli("TIMEOUT", duration_s=time.monotonic() - t0, error_type="timeout")
        raise JobError(f"timeout after {timeout}s")
    if tele:
        envelope = _envelope(proc.stdout)
        if envelope is None:
            status, etype = ("ERROR", "nonzero_exit") if proc.returncode else ("MALFORMED_OUTPUT", "invalid_envelope")
        elif proc.returncode or envelope.get("is_error") or envelope.get("subtype") != "success":
            status, etype = "ERROR", "nonzero_exit" if proc.returncode else "cli_error"
        else:
            status, etype = "SUCCESS", None
        tele.cli(status, returncode=proc.returncode, envelope=envelope,
                 duration_s=time.monotonic() - t0, error_type=etype)
    if proc.returncode != 0:
        try:
            detail = str(json.loads(proc.stdout).get("result") or "")
        except (json.JSONDecodeError, AttributeError):
            detail = ""
        if not detail:
            detail = ((proc.stderr or proc.stdout).strip().splitlines()[-1:]
                      or ["no output"])[0]
        raise JobError(f"claude exited {proc.returncode}: {detail[:200]}")
    violation = single_turn.check_envelope(cmd, _envelope(proc.stdout))
    if violation:
        if tele:
            tele.validation("INVALID", "single_turn_guardrail")
        raise JobError(f"single-turn guardrail: {violation}")
    try:
        resume = parse_worker_output(proc.stdout)["resume_v1"]
    except JobError:
        if tele and tele.rec["cli_status"] == "SUCCESS":
            tele.validation("INVALID", "invalid_result")
        raise
    err = validate_resume(resume, job["position"])
    if err:
        if tele:
            tele.validation("INVALID", "validation")
        raise JobError(err)
    leak = output_guard.check(resume, job["jd"])
    if leak:
        if tele:
            tele.validation("INVALID", "output_guard")
        raise JobError(f"output guard: {leak}")
    if tele:
        tele.validation("VALID")
    return resume


_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def generate_with_retry(job: Job, cmd: list[str], timeout: int, batch: str | None = None) -> str:
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        tele = telemetry.Invocation("02_v1", cmd, batch=batch, job_folder=job.folder, attempt=attempt)
        try:
            return generate_once(job.data, cmd, timeout, tele)
        except JobError as e:
            last = e
            if attempt < MAX_ATTEMPTS:
                log(f"  attempt {attempt} failed for {job.label}: {e} — retrying with a fresh worker")
        finally:
            tele.emit()
    raise last


# ------------------------------------------------------------------- commit

def commit_resume(job: Job, resume: str) -> None:
    """Re-read job.json, keep every field, set resume_v1, atomic write."""
    try:
        current = json.loads(job.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise JobError(f"cannot re-read job.json: {e}") from e
    if not isinstance(current, dict) or any(
            current.get(f) != job.data[f] for f in BASE_FIELDS):
        raise JobError("job.json changed during generation; not overwriting")
    current["resume_v1"] = resume
    try:
        p01.write_json_atomic(job.path, current)
    except OSError as e:
        raise JobError(f"job.json write failed: {e}") from e


def process_job(job: Job, ctx: dict) -> tuple[str, str]:
    """Run one job end to end. Returns (status, detail)."""
    tracker, batch = ctx["tracker"], ctx["batch"]
    try:
        update_tracker(tracker, batch, job.folder, v1_status="RUNNING", last_error="")
        resume = generate_with_retry(job, ctx["cmd"], ctx["timeout"], batch)
        commit_resume(job, resume)
    except Exception as e:  # JobError or unexpected; isolate to this job
        reason = str(e) if isinstance(e, JobError) else f"{type(e).__name__}: {e}"
        if job.has_valid_v1:
            reason += " (previous resume_v1 kept)"
        try:
            update_tracker(tracker, batch, job.folder,
                           v1_status="FAILED", last_error=reason[:300])
        except Exception as te:
            reason += f"; tracker update failed: {te}"
        return "FAILED", reason
    try:
        update_tracker(tracker, batch, job.folder, v1_status="DONE", last_error="")
    except Exception as e:
        return "FAILED", f"resume_v1 saved but tracker update failed: {e}"
    return "DONE", ""


def mark_checkpoint(job: Job, ctx: dict) -> None:
    row = get_tracker_row(ctx["tracker"], ctx["batch"], job.folder)
    if row["v1_status"] != "DONE":
        update_tracker(ctx["tracker"], ctx["batch"], job.folder,
                       v1_status="DONE", last_error="")


# ------------------------------------------------------------------ handoff

def handoff_path(batch: str) -> Path:
    return HANDOFF_ROOT / batch / HANDOFF_NAME


def build_v1_ready(batch_dir: Path, folders: list[str], tracker: Path) -> dict:
    """Snapshot of jobs ready for the quality review (stage 3): valid resume_v1 + v1_status DONE.

    Re-reads every job.json and the tracker; read-only. Order follows `folders`
    (sorted folder names from load_jobs), so the manifest is deterministic.
    """
    status = {r["job_folder"]: r["v1_status"]
              for r in p01.read_tracker(tracker) if r["batch"] == batch_dir.name}
    entries = []
    for folder in folders:
        path = batch_dir / folder / "job.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            validate_job_data(data, folder)
        except (OSError, ValueError, PipelineError):
            continue
        if (status.get(folder) == "DONE"
                and validate_resume(data.get("resume_v1"), data["position"]) is None):
            entries.append({"job_folder": folder, "job_json": str(path)})
    return {"batch": batch_dir.name, "generated_at": now_str(),
            "job_count": len(entries), "jobs": entries}


def write_handoff(path: Path, manifest: dict) -> None:
    jobs = manifest["jobs"]
    assert manifest["job_count"] == len(jobs)
    assert len({j["job_folder"] for j in jobs}) == len(jobs)
    assert len({j["job_json"] for j in jobs}) == len(jobs)
    path.parent.mkdir(parents=True, exist_ok=True)
    p01.write_json_atomic(path, manifest)  # v1_ready.json.tmp -> fsync -> replace


# --------------------------------------------------------------------- main

def run(args, stats: dict) -> int:
    jobs_root = Path(args.jobs_root).expanduser().resolve()
    batch_dir = resolve_batch(args.batch, jobs_root)
    batch = batch_dir.name
    tracker = tracker_path_for(batch_dir)
    stats["batch"] = batch

    jobs = load_jobs(batch_dir)
    check_tracker_rows(tracker, batch, jobs)  # before any Claude call
    all_folders = [j.folder for j in jobs]  # handoff covers the whole batch
    if args.job:
        known = {j.folder for j in jobs}
        unknown = [f for f in args.job if f not in known]
        if unknown:
            raise PipelineError(f"--job not found in {batch}: {', '.join(unknown)}")
        jobs = [j for j in jobs if j.folder in set(args.job)]
    stats["jobs"] = len(jobs)

    checkpoints = [j for j in jobs if j.has_valid_v1 and not args.force]
    eligible = [j for j in jobs if not (j.has_valid_v1 and not args.force)]
    to_run = eligible[:args.limit] if args.limit is not None else eligible
    stats["deferred"] = len(eligible) - len(to_run)

    print(f"Batch: {batch_dir}")
    print(f"Tracker: {tracker}")
    print(f"Jobs: {len(jobs)} | checkpoint {len(checkpoints)} | "
          f"eligible {len(eligible)} | this run {len(to_run)}")
    print(f"Workers: {args.workers} | Limit: {args.limit} | Timeout: {args.timeout}s"
          f" | Attempts: {MAX_ATTEMPTS} | Force: {args.force}\n")

    if args.dry_run:
        for j in checkpoints:
            print(f"[CHECKPOINT] {j.folder}")
        for j in to_run:
            note = " (force: has valid resume_v1)" if j.has_valid_v1 else ""
            print(f"[WOULD PROCESS] {j.folder}{note}")
        for j in eligible[len(to_run):]:
            print(f"[PENDING, over limit] {j.folder}")
        current = build_v1_ready(batch_dir, all_folders, tracker)
        print(f"\nHandoff (not written): {handoff_path(batch)}")
        print(f"V1-ready jobs right now: {current['job_count']}")
        stats["dry_run"] = True
        return 0

    ctx = {"tracker": tracker, "batch": batch, "timeout": args.timeout,
           "cmd": worker_command(find_claude(), args.model, mode=args.worker) if to_run else None}
    total = len(checkpoints) + len(to_run)

    def progress():
        done = stats["DONE"] + stats["FAILED"] + stats["SKIPPED"]
        log(f"PROGRESS V1 {done}/{total} | DONE {stats['DONE']} "
            f"FAILED {stats['FAILED']} SKIPPED {stats['SKIPPED']}")

    for j in checkpoints:
        try:
            mark_checkpoint(j, ctx)
            stats["SKIPPED"] += 1
            log(f"[SKIPPED] {j.label} | existing valid resume_v1")
        except Exception as e:
            stats["FAILED"] += 1
            log(f"[FAILED] {j.label} | checkpoint tracker update failed: {e}")
        progress()

    if to_run:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(process_job, j, ctx): j for j in to_run}
            for future in as_completed(futures):
                job = futures[future]
                status, detail = future.result()
                stats[status] += 1
                log(f"[{status}] {job.label}" + (f" | {detail}" if detail else ""))
                progress()

    stats["handoff"] = handoff_path(batch)
    try:
        manifest = build_v1_ready(batch_dir, all_folders, tracker)
        write_handoff(stats["handoff"], manifest)
        stats["v1_ready"] = manifest["job_count"]
    except Exception as e:
        stats["handoff_error"] = f"{type(e).__name__}: {e}"
        return 1

    return 1 if stats["FAILED"] else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate Resume V1 for each job in a batch.")
    ap.add_argument("--batch", help="batch folder path (default: newest batch)")
    ap.add_argument("--jobs-root", default=str(DEFAULT_JOBS_ROOT),
                    help="jobs root used for default batch discovery")
    ap.add_argument("--limit", type=int, help="process only the first N eligible jobs")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"parallel Claude workers (default {DEFAULT_WORKERS})")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help=f"seconds per Claude attempt (default {DEFAULT_TIMEOUT})")
    ap.add_argument("--model", help="override worker model (default: agent's 'inherit')")
    ap.add_argument("--worker", choices=WORKER_MODES,
                    help=f"worker mode (default: ${WORKER_ENV} or single-turn; agent = rollback)")
    ap.add_argument("--job", action="append", metavar="FOLDER",
                    help="only this job folder in the batch (repeatable)")
    ap.add_argument("--force", action="store_true",
                    help="regenerate even when a valid resume_v1 exists")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and plan only; no Claude calls, no writes")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be >= 1")
    if args.workers < 1:
        ap.error("--workers must be >= 1")
    if args.timeout < 1:
        ap.error("--timeout must be >= 1")

    stats = {"batch": "?", "jobs": 0, "DONE": 0, "FAILED": 0, "SKIPPED": 0,
             "deferred": 0, "dry_run": False}
    try:
        code = run(args, stats)
    except PipelineError as e:
        print(f"FAILED: {e}")
        print("DONE exit=1")
        return 1
    except KeyboardInterrupt:
        print("\nFAILED: interrupted; committed resumes are kept, rerun resumes from checkpoints")
        print("DONE exit=130")
        return 130

    if stats["dry_run"]:
        print("\nDry run: nothing written, Claude not called")
    else:
        print("\nResume V1 complete\n")
        print(f"Batch: {stats['batch']}")
        print(f"Jobs: {stats['jobs']}")
        print(f"DONE: {stats['DONE']}")
        print(f"SKIPPED: {stats['SKIPPED']}")
        print(f"FAILED: {stats['FAILED']}")
        if stats["deferred"]:
            print(f"NOT RUN (over --limit): {stats['deferred']}")
        if stats.get("handoff_error"):
            print(f"\nFAILED: handoff not written ({stats['handoff']}): {stats['handoff_error']}")
        elif stats.get("handoff"):
            print(f"\nHandoff:\n{stats['handoff']}\n")
            print(f"V1-ready jobs: {stats['v1_ready']}")
    print(f"\nDONE exit={code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
