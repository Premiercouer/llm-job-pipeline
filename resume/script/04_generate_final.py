#!/usr/bin/env python3
"""Stage 4: Final Resume — Resume V2 -> fixed HTML template -> PDF -> one-page check -> trim.

feedback_ready.json -> validate manifest / job.json / tracker -> CHECKPOINT / REUSE / NEEDS_V2
-> per job (jobs in parallel, default 3 workers; each job strictly serial):
   resume_v2 (fresh resume-v2-worker, or reuse) -> validate -> commit to job.json
   -> resume.html -> resume.pdf (headless Chrome) -> PyMuPDF page count
   -> 1 page: DONE | >1 page: pdf_validation -> resume-trim-worker (one pass)
      -> validate -> commit -> render again ... up to --max-trim-iterations.

04 never scans the jobs root or a batch folder: the job set is the manifest's jobs[].
Tracker columns owned by this stage: final_status, pdf_pages, last_error, updated_at.
Tracker writes go through a disk-backed event queue (.runtime/tracker_queue/) drained under
an fcntl.flock on .runtime/tracker.lock, so several 04 processes can run at the same time.

Usage:
    python3 04_generate_final.py --input /abs/.runtime/handoff/<batch>/feedback_ready.json
    python3 04_generate_final.py                     # newest feedback_ready by batch name
    python3 04_generate_final.py --limit 1 --workers 1
    python3 04_generate_final.py --force             # regenerate Resume V2
    python3 04_generate_final.py --dry-run

Progress protocol (line-buffered):
    [DONE|FAILED|LAYOUT_FAILED|SKIPPED] Company — Position [| detail]
    PROGRESS FINAL i/N | DONE d FAILED f LAYOUT_FAILED l SKIPPED s
    DONE exit=<code>                                  (always last)
"""

from __future__ import annotations

import argparse
import fcntl
import html
import itertools
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import pipeline_telemetry as telemetry  # noqa: E402  (worker usage JSONL; never fails a stage)


def _load_stage3():
    """Reuse 03's manifest / quality-feedback validators (and 02/01 helpers through it)."""
    spec = importlib.util.spec_from_file_location("review_resumes", SCRIPT_DIR / "03_review_resumes.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


p03 = _load_stage3()
p02 = p03.p02
p01 = p03.p01

ROOT = p02.ROOT  # resume/ (templates); workers run in p02.worker_cwd()
candidate, paths = p02.candidate, p02.paths
DEFAULT_JOBS_ROOT = p02.DEFAULT_JOBS_ROOT
HANDOFF_ROOT = p02.HANDOFF_ROOT
FEEDBACK_NAME = p03.FEEDBACK_NAME
REVIEW_BLOCKED_MARKER = p03.REVIEW_BLOCKED_MARKER  # written by 03 (provider unavailable), removed by its next run
TEMPLATE = ROOT / "templates" / "resume_format_example.html"
TEMPLATE_BODY_MARKER = "  <!-- JOB TITLE + SUMMARY -->"

V2_AGENT = "resume-v2-worker"
TRIM_AGENT = "resume-trim-worker"
DEFAULT_WORKERS = 3
DEFAULT_TIMEOUT = 300           # per Claude attempt
DEFAULT_MAX_TRIM_ITERATIONS = 6
MAX_ATTEMPTS = 2                # fresh-worker retry for invalid V2 / trim output
RENDER_TIMEOUT = 90             # per headless Chrome PDF render

TRACKER_QUEUE_DIR = p02.DATA_ROOT / ".runtime" / "tracker_queue"
TRACKER_LOCK = p02.DATA_ROOT / ".runtime" / "tracker.lock"

SOURCE_FIELDS = ("company", "position", "jd", "apply_url", "source_key", "resume_v1", "quality_feedback")
OWNED_COLUMNS = ("final_status", "pdf_pages", "last_error", "updated_at")

# Resume V2 Output Contract (instructions/resume_v2.md)
V2_KEYS = {"job_title", "summary", "qualifications", "skills", "experience", "project", "education"}
EXPERIENCE_KEYS = {"role", "company", "date", "bullets"}
PROJECT_KEYS = {"name", "tools", "date", "bullet"}
EDUCATION_KEYS = {"degree", "school", "details", "date", "coursework"}
# Experience identity (role / company / date) and education are fixed facts from candidate.json:
# workers choose bullets and coursework, never these fields.


class PipelineError(Exception):
    """Fail-closed error for the whole run (before any worker starts)."""


class JobError(Exception):
    """Failure of a single job; other jobs continue."""


# --------------------------------------------------------------- validation

def _nonempty_str(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _same(a, b) -> bool:
    """Identity comparison tolerant to dash / whitespace variants only."""
    norm = lambda s: " ".join(re.sub(r"[‐-―−-]", "-", s).split())
    return isinstance(a, str) and isinstance(b, str) and norm(a) == norm(b)


def _coursework_ok(value, original: str) -> bool:
    """Coursework may stay, be emptied, or drop items (in order) — never rewritten."""
    if not isinstance(value, str):
        return False
    if not value.strip():
        return True
    items = [x.strip() for x in original.split(",")]
    kept = [x.strip() for x in value.split(",")]
    it = iter(items)
    return all(any(k == x for x in it) for k in kept)


def _check_structure(r, min_bullets: int, max_bullets: int) -> str | None:
    """Shared schema check; bullet range differs between initial and trimmed V2."""
    if not isinstance(r, dict):
        return "resume_v2 is not an object"
    if set(r) != V2_KEYS:
        return f"resume_v2 keys must be {sorted(V2_KEYS)}, got {sorted(r)}"
    for k in ("job_title", "summary"):
        if not _nonempty_str(r[k]):
            return f"{k} must be a non-empty string"
    q = r["qualifications"]
    if not isinstance(q, list) or not 3 <= len(q) <= 4 or not all(_nonempty_str(x) for x in q):
        return "qualifications must be 3-4 non-empty strings"
    s = r["skills"]
    if not isinstance(s, dict) or set(s) != {"technical", "professional"}:
        return "skills must have exactly technical, professional"
    for k in ("technical", "professional"):
        if not isinstance(s[k], list) or not s[k] or not all(_nonempty_str(x) for x in s[k]):
            return f"skills.{k} must be a non-empty list of skill names"
    e = r["experience"]
    identity = candidate.experience_identity()
    if not isinstance(e, list) or len(e) != len(identity):
        return f"experience must have exactly {len(identity)} entries"
    for i, (entry, ident) in enumerate(zip(e, identity)):
        if not isinstance(entry, dict) or set(entry) != EXPERIENCE_KEYS:
            return f"experience[{i}] keys must be {sorted(EXPERIENCE_KEYS)}"
        for k in ("role", "company", "date"):
            if not _same(entry[k], ident[k]):
                return f"experience[{i}].{k} must be {ident[k]!r}, got {entry[k]!r}"
        b = entry["bullets"]
        if (not isinstance(b, list) or not min_bullets <= len(b) <= max_bullets
                or not all(_nonempty_str(x) for x in b)):
            n = f"exactly {max_bullets}" if min_bullets == max_bullets else f"{min_bullets}-{max_bullets}"
            return f"experience[{i}] ({ident['company']}) needs {n} non-empty bullets"
        if any(x.lstrip().startswith("•") for x in b):
            return f"experience[{i}] bullets must not include '•'"
    p = r["project"]
    if not isinstance(p, dict) or set(p) != PROJECT_KEYS:
        return f"project must be one object with keys {sorted(PROJECT_KEYS)}"
    if not all(_nonempty_str(p[k]) for k in PROJECT_KEYS):
        return "project name/tools/date/bullet must be non-empty strings"
    if p["bullet"].lstrip().startswith("•") or any(x.lstrip().startswith("•") for x in q):
        return "bullets must not include '•'"
    ed = r["education"]
    education = candidate.education_fixed()
    if not isinstance(ed, list) or len(ed) != len(education):
        return f"education must have exactly {len(education)} entries"
    for i, (entry, fixed) in enumerate(zip(ed, education)):
        if not isinstance(entry, dict) or set(entry) != EDUCATION_KEYS:
            return f"education[{i}] keys must be {sorted(EDUCATION_KEYS)}"
        for k in ("degree", "school", "date"):
            if not _same(entry[k], fixed[k]):
                return f"education[{i}].{k} must be {fixed[k]!r}"
        if not (_same(entry["details"], fixed["details"]) or entry["details"] == fixed["details"] == ""):
            return f"education[{i}].details must be {fixed['details']!r}"
        if not _coursework_ok(entry["coursework"], fixed["coursework"]):
            return f"education[{i}].coursework may only keep items from {fixed['coursework']!r}"
    return None


def validate_resume_v2_initial(r) -> str | None:
    """Fresh V2 from resume-v2-worker: full resume_v2.md contract (3+3 bullets, fixed education)."""
    err = _check_structure(r, 3, 3)
    if err:
        return err
    for i, (entry, fixed) in enumerate(zip(r["education"], candidate.education_fixed())):
        if not (_same(entry["coursework"], fixed["coursework"]) or entry["coursework"] == fixed["coursework"] == ""):
            return f"education[{i}].coursework must be {fixed['coursework']!r}"
    return None


def validate_resume_v2_stored(r) -> str | None:
    """An existing job.json resume_v2 may be an initial or an already-trimmed version."""
    return _check_structure(r, 2, 3)


def visible_strings(r: dict) -> list[str]:
    out = [r["job_title"], r["summary"], *r["qualifications"],
           *r["skills"]["technical"], *r["skills"]["professional"]]
    for e in r["experience"]:
        out += [e["role"], e["company"], e["date"], *e["bullets"]]
    out += [r["project"][k] for k in ("name", "tools", "date", "bullet")]
    for e in r["education"]:
        out += [e[k] for k in ("degree", "school", "details", "date", "coursework")]
    return out


def visible_chars(r: dict) -> int:
    return sum(len(s) for s in visible_strings(r))


def validate_resume_v2_trimmed(new, prev: dict) -> str | None:
    """One trim pass: same schema, identity unchanged, nothing added, not longer, not identical."""
    err = _check_structure(new, 2, 3)
    if err:
        return f"trimmed resume invalid: {err}"
    if new["job_title"] != prev["job_title"]:
        return "trim changed job_title"
    for i, (a, b) in enumerate(zip(new["experience"], prev["experience"])):
        if any(a[k] != b[k] for k in ("role", "company", "date")):
            return f"trim changed experience[{i}] role/company/date"
        if len(a["bullets"]) > len(b["bullets"]):
            return f"trim added bullets to experience[{i}]"
    for k in ("name", "date"):
        if new["project"][k] != prev["project"][k]:
            return f"trim changed project.{k}"
    for i, (a, b) in enumerate(zip(new["education"], prev["education"])):
        if any(a[k] != b[k] for k in ("degree", "school", "details", "date")):
            return f"trim changed education[{i}] degree/school/details/date"
        prev_cw = b["coursework"]
        ok = _coursework_ok(a["coursework"], prev_cw) if prev_cw.strip() else not a["coursework"].strip()
        if not ok:
            return f"trim rewrote education[{i}].coursework"
    if len(new["qualifications"]) > len(prev["qualifications"]):
        return "trim added qualifications"
    for k in ("technical", "professional"):
        added = [x for x in new["skills"][k] if x not in prev["skills"][k]]
        if added:
            return f"trim added skills.{k}: {added}"
    if new == prev:
        return "trim returned an unchanged resume"
    if visible_chars(new) > visible_chars(prev):
        return f"trim made the resume longer ({visible_chars(prev)} -> {visible_chars(new)} chars)"
    return None


# ------------------------------------------------------------------ render

def _template_prefix() -> str:
    """Template head (fixed CSS) + fixed header, verbatim up to the job-title block."""
    text = TEMPLATE.read_text(encoding="utf-8")
    if text.count(TEMPLATE_BODY_MARKER) != 1 or "<style>" not in text:
        raise PipelineError(f"template structure changed; marker not found once: {TEMPLATE}")
    return candidate.fill_header(text.split(TEMPLATE_BODY_MARKER)[0])


def render_html(r: dict, prefix: str) -> str:
    """Fill the fixed template structure with Resume V2 content. No CSS changes."""
    e = html.escape
    lis = lambda items, pad: "\n".join(f"{pad}<li>{e(x)}</li>" for x in items)
    out = [prefix + TEMPLATE_BODY_MARKER,
           f'  <div class="job-title" data-field="job_title">{e(r["job_title"])}</div>',
           f'  <p class="summary-text" data-field="summary">{e(r["summary"])}</p>',
           "",
           '  <div class="section" data-section="qualifications">',
           '    <div class="section-title">Summary of Qualifications</div>',
           '    <ul data-repeat="qualifications">',
           lis(r["qualifications"], "      "),
           "    </ul>",
           "  </div>",
           "",
           '  <div class="section" data-section="skills">',
           '    <div class="section-title">Core Skills</div>',
           '    <div class="skills-block">',
           f'      <p><span class="label">Technical:</span> <span data-field="skills.technical">'
           f'{e(", ".join(r["skills"]["technical"]))}</span></p>',
           f'      <p><span class="label">Professional:</span> <span data-field="skills.professional">'
           f'{e(", ".join(r["skills"]["professional"]))}</span></p>',
           "    </div>",
           "  </div>",
           "",
           '  <div class="section" data-section="professional_experience">',
           '    <div class="section-title">Professional Experience</div>']
    for x in r["experience"]:
        out += ["",
                '    <div class="entry" data-repeat-item="professional_experience">',
                '      <div class="entry-header">',
                f'        <div class="entry-title"><span class="role" data-field="role">{e(x["role"])}</span>, '
                f'<span data-field="company">{e(x["company"])}</span></div>',
                f'        <div class="entry-date" data-field="date">{e(x["date"])}</div>',
                "      </div>",
                '      <ul data-repeat="bullets">',
                lis(x["bullets"], "        "),
                "      </ul>",
                "    </div>"]
    p = r["project"]
    title = f'{p["name"]} – {p["tools"]}' if p["tools"].strip() else p["name"]
    out += ["  </div>",
            "",
            '  <div class="section" data-section="project_experience">',
            '    <div class="section-title">Project Experience</div>',
            "",
            '    <div class="entry" data-repeat-item="project_experience">',
            '      <div class="entry-header">',
            f'        <div class="entry-title" data-field="title">{e(title)}</div>',
            f'        <div class="entry-date" data-field="date">{e(p["date"])}</div>',
            "      </div>",
            '      <ul data-repeat="bullets">',
            lis([p["bullet"]], "        "),
            "      </ul>",
            "    </div>",
            "  </div>",
            "",
            '  <div class="section" data-section="education">',
            '    <div class="section-title">Education</div>']
    for i, x in enumerate(r["education"]):
        degree = f'{x["degree"]}, {x["school"]}' + (f' ({x["details"]})' if x["details"].strip() else "")
        style = ' style="margin-top: 3pt;"' if i else ""  # as in the template's second entry
        out += ["",
                f'    <div class="edu-entry" data-repeat-item="education"{style}>',
                '      <div class="entry-header">',
                f'        <div class="degree" data-field="degree">{e(degree)}</div>',
                f'        <div class="entry-date" data-field="date">{e(x["date"])}</div>',
                "      </div>"]
        if x["coursework"].strip():
            out.append(f'      <div class="coursework" data-field="coursework">'
                       f'Relevant coursework: {e(x["coursework"])}</div>')
        out.append("    </div>")
    out += ["  </div>", "", "</body>", "</html>", ""]
    return "\n".join(out)


def write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def find_chrome() -> str:
    explicit = os.environ.get("CHROME_BIN")
    candidates = [explicit] if explicit else [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"), shutil.which("chromium")]
    for c in candidates:
        if c and os.access(c, os.X_OK):
            return c
    raise PipelineError("Chrome/Chromium not found; set CHROME_BIN")


def render_pdf(chrome: str, html_path: Path, pdf_path: Path) -> None:
    """Headless Chrome print-to-PDF with a throwaway profile (never a personal browser profile)."""
    tmp_pdf = pdf_path.with_name(pdf_path.stem + ".tmp.pdf")  # resume.pdf -> resume.tmp.pdf
    tmp_pdf.unlink(missing_ok=True)
    profile = tempfile.mkdtemp(prefix="resume-pdf-chrome-")
    cmd = [chrome, "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
           "--disable-extensions", f"--user-data-dir={profile}", "--no-pdf-header-footer",
           f"--print-to-pdf={tmp_pdf}", html_path.as_uri()]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    # Headless Chrome may keep running after writing the PDF, so wait for the file
    # itself: present, same size on two consecutive polls, and readable.
    deadline = time.monotonic() + RENDER_TIMEOUT
    last_size, done = -1, False
    try:
        while time.monotonic() < deadline:
            size = tmp_pdf.stat().st_size if tmp_pdf.is_file() else -1
            if size > 0 and size == last_size:
                try:
                    pymupdf.open(tmp_pdf).close()
                    done = True
                    break
                except Exception:
                    pass
            if proc.poll() is not None and size <= 0:
                break  # Chrome exited without writing anything
            last_size = size
            time.sleep(0.5)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # Chrome and its helper processes
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        shutil.rmtree(profile, ignore_errors=True)
    if not done:
        tmp_pdf.unlink(missing_ok=True)
        raise JobError(f"PDF render failed or timed out after {RENDER_TIMEOUT}s "
                       f"(chrome exit {proc.returncode})")
    os.replace(tmp_pdf, pdf_path)


def pdf_lines(path: Path) -> list[list[str]]:
    """Visible rendered text lines per page (PyMuPDF dict layout, no OCR).

    PyMuPDF splits one visual row into several line objects (e.g. a degree on the
    left and its date on the right); line objects sharing a baseline are one line.
    """
    pages = []
    with pymupdf.open(path) as doc:
        for page in doc:
            raw = []
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block["lines"]:
                    text = "".join(s["text"] for s in line["spans"]).strip()
                    if text:
                        raw.append((line["bbox"][3], line["bbox"][0], text))
            raw.sort()
            rows: list[list[tuple]] = []
            for item in raw:
                if rows and abs(item[0] - rows[-1][0][0]) <= 2.0:
                    rows[-1].append(item)
                else:
                    rows.append([item])
            pages.append([" ".join(t for _, _, t in sorted(row, key=lambda x: x[1])) for row in rows])
    return pages


def build_pdf_validation(pages: list[list[str]], trim_iteration: int) -> dict:
    by_page = {str(i + 1): len(lines) for i, lines in enumerate(pages) if i > 0}
    return {
        "page_count": len(pages),
        "overflow_pages": [i + 1 for i in range(1, len(pages))],
        "overflow_lines": sum(by_page.values()),
        "overflow_lines_by_page": by_page,
        "overflow_text": [line for lines in pages[1:] for line in lines],
        "trim_iteration": trim_iteration,
    }


# ------------------------------------------------------------------ claude

def worker_command(agent: str, claude_bin: str, model: str | None) -> list[str]:
    cmd = [claude_bin, "-p",
           "--agent", agent,
           "--output-format", "json",
           "--no-session-persistence",
           "--strict-mcp-config",       # no MCP / browser
           "--disable-slash-commands",
           "--tools", "Read",
           "--allowedTools", "Read",
           "--permission-mode", "dontAsk"]
    if model:
        cmd += ["--model", model]
    return cmd


def call_worker(cmd: list[str], prompt: str, timeout: int, tele=None) -> dict:
    """One fresh worker. `tele` (pipeline_telemetry.Invocation, optional) only records counters;
    generate_cover_letter.py calls this without it."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              cwd=p02.worker_cwd(), env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        if tele:
            tele.cli("TIMEOUT", duration_s=time.monotonic() - t0, error_type="timeout")
        raise JobError(f"Claude timeout after {timeout}s")
    if tele:
        try:
            envelope = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError):
            envelope = None
        if not isinstance(envelope, dict):
            envelope = None
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
            detail = ((proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"])[0]
        raise JobError(f"claude exited {proc.returncode}: {detail[:200]}")
    try:
        return p03.parse_response(proc.stdout)  # envelope + fenced/plain JSON object
    except p03.JobError as e:
        if tele and tele.rec["cli_status"] == "SUCCESS":
            tele.validation("INVALID", "invalid_result")
        raise JobError(str(e)) from e


def v2_prompt(job: dict) -> str:
    payload = {k: job[k] for k in ("company", "position", "jd", "resume_v1", "quality_feedback")}
    return ("请严格按照 instructions/resume_v2.md，为下面这 1 个职位生成 Resume V2。"
            "只返回 Output Contract 定义的 1 个 JSON object（不要外层包装），不要输出任何其他内容。\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2))


def trim_prompt(resume: dict, quality_feedback: dict, validation: dict) -> str:
    payload = {"resume_v2": resume, "quality_feedback": quality_feedback, "pdf_validation": validation}
    return ("Python 已实际渲染并确认当前 Resume 的 PDF 超过一页。请严格按照 instructions/resume_trim.md "
            "执行一次 trim pass，只返回完整的 trimmed Resume JSON object（与输入 resume_v2 相同 schema，"
            "不要外层包装），不要输出任何其他内容。\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2))


# ------------------------------------------------------------------- jobs

@dataclass
class Job:
    folder: str
    path: Path
    data: dict
    state: str = ""          # CHECKPOINT | REUSE | NEEDS_V2
    note: str = ""
    source: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.data['company']} — {self.data['position']}"

    @property
    def html_path(self) -> Path:
        return self.path.parent / "resume.html"

    @property
    def pdf_path(self) -> Path:
        return self.path.parent / "resume.pdf"


def find_latest_feedback() -> Path:
    if not HANDOFF_ROOT.is_dir():
        raise PipelineError(f"handoff root not found: {HANDOFF_ROOT}")
    # A batch whose review is blocked counts as the newest one, so 04 never
    # silently falls back to an older batch while 03 is unfinished.
    found = [(key, d) for d in HANDOFF_ROOT.iterdir()
             if d.is_dir() and (key := p02.batch_sort_key(d.name)) is not None
             and ((d / FEEDBACK_NAME).is_file() or (d / REVIEW_BLOCKED_MARKER).is_file())]
    if not found:
        raise PipelineError(f"no <batch>/{FEEDBACK_NAME} under {HANDOFF_ROOT}")
    newest = max(found, key=lambda f: f[0])[1]
    check_review_gate(newest)
    return newest / FEEDBACK_NAME


def check_review_gate(batch_dir: Path) -> None:
    """Stage 3 stopped because the review provider was unavailable: wait until 03 is rerun."""
    marker = batch_dir / REVIEW_BLOCKED_MARKER
    if marker.is_file():
        raise PipelineError(f"quality review blocked for batch {batch_dir.name} "
                            f"({marker}); resolve the provider issue and rerun 03 first")


def pdf_page_count(path: Path) -> int | None:
    try:
        with pymupdf.open(path) as doc:
            return doc.page_count
    except Exception:
        return None


def classify(job: Job, force: bool, prefix: str) -> None:
    """Read-only state check: full checkpoint, reuse existing V2, or generate V2."""
    v2 = job.data.get("resume_v2")
    if force:
        job.state, job.note = "NEEDS_V2", "force"
        return
    if v2 is None or validate_resume_v2_stored(v2) is not None:
        job.state = "NEEDS_V2"
        job.note = "no resume_v2" if v2 is None else f"invalid resume_v2 ({validate_resume_v2_stored(v2)})"
        return
    job.state = "REUSE"
    current_html = job.html_path.is_file() and job.html_path.read_text(encoding="utf-8") == render_html(v2, prefix)
    pages = pdf_page_count(job.pdf_path) if job.pdf_path.is_file() else None
    if current_html and pages == 1:
        job.state, job.note = "CHECKPOINT", "1-page final resume"
    elif current_html and pages and pages > 1:
        job.note = f"existing PDF has {pages} pages -> would trim"
    else:
        job.note = "valid resume_v2, artifacts missing/stale -> would render"


def load_jobs(entries: list[dict], force: bool, prefix: str) -> list[Job]:
    jobs, errors = [], []
    for item in entries:
        folder, path = item["job_folder"], Path(item["job_json"])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            errors.append(f"{folder}: job.json unreadable ({e})")
            continue
        if not isinstance(data, dict):
            errors.append(f"{folder}: job.json root is not an object")
            continue
        bad = [f for f in p03.REQUIRED_FIELDS if not _nonempty_str(data.get(f))]
        if bad:
            errors.append(f"{folder}: missing/empty {', '.join(bad)}")
            continue
        err = p02.validate_resume(data["resume_v1"], data["position"])
        if err:
            errors.append(f"{folder}: resume_v1 no longer valid ({err})")
            continue
        err = p03.validate_feedback(data.get("quality_feedback"))
        if err:
            errors.append(f"{folder}: quality_feedback invalid ({err})")
            continue
        job = Job(folder, path, data, source={k: data[k] for k in SOURCE_FIELDS})
        classify(job, force, prefix)
        jobs.append(job)
    if errors:
        raise PipelineError("manifest/job state inconsistent:\n  " + "\n  ".join(errors))
    return jobs


# ------------------------------------------------------------------ tracker

def check_tracker(tracker: Path, batch: str, jobs: list[Job]) -> None:
    if not tracker.is_file():
        raise PipelineError(f"tracker not found: {tracker}")
    try:
        rows = p01.read_tracker(tracker)
    except p01.PrepareError as e:
        raise PipelineError(str(e)) from e
    problems = []
    for j in jobs:
        m = [r for r in rows if r["batch"] == batch and r["job_folder"] == j.folder]
        if len(m) != 1:
            problems.append(f"{j.folder}: {len(m)} tracker rows")
        elif m[0]["review_status"] != "DONE":
            problems.append(f"{j.folder}: review_status is {m[0]['review_status']!r}, not DONE")
    if problems:
        raise PipelineError("tracker preflight failed:\n  " + "\n  ".join(problems))


# Disk-backed tracker update queue. Workers never read-modify-write the CSV themselves:
# they enqueue one event file each, and whoever holds the cross-process flock applies
# all pending events in one serialized CSV write. Any 04 process can drain.

_event_seq = itertools.count()


def enqueue_tracker_event(tracker: Path, batch: str, folder: str, updates: dict,
                          queue_dir: Path | None = None) -> Path:
    """Write one event atomically (unique name, .tmp -> fsync -> os.replace)."""
    assert set(updates) <= set(OWNED_COLUMNS), updates
    queue_dir = queue_dir or TRACKER_QUEUE_DIR
    queue_dir.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns():020d}_{os.getpid()}_{next(_event_seq):06d}_{uuid.uuid4().hex}"
    event = {"event_id": name, "created_at": p02.now_str(), "pid": os.getpid(),
             "tracker": str(Path(tracker).resolve()), "batch": batch, "job_folder": folder,
             "updates": updates}
    tmp, final = queue_dir / f"{name}.json.tmp", queue_dir / f"{name}.json"
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(event, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)
    return final


def _event_error(event) -> str | None:
    if not isinstance(event, dict):
        return "event is not an object"
    for k in ("event_id", "tracker", "batch", "job_folder", "updates"):
        if k not in event:
            return f"event missing {k}"
    if p02.batch_sort_key(str(event["batch"])) is None:
        return f"invalid batch {event['batch']!r}"
    u = event["updates"]
    if not isinstance(u, dict) or not u or not set(u) <= set(OWNED_COLUMNS):
        return f"updates must only use {OWNED_COLUMNS}"
    if not all(isinstance(v, str) for v in u.values()):
        return "update values must be strings"
    return None


def _reject_event(path: Path, queue_dir: Path, reason: str) -> None:
    """Invalid events are kept for inspection, never applied or silently deleted."""
    rejected = queue_dir / "rejected"
    rejected.mkdir(exist_ok=True)
    os.replace(path, rejected / path.name)
    (rejected / f"{path.name}.reason.txt").write_text(reason + "\n", encoding="utf-8")
    log(f"  WARNING tracker event rejected ({path.name}): {reason}")


def drain_tracker_queue(queue_dir: Path | None = None, lock_path: Path | None = None) -> int:
    """Apply all pending events under an exclusive cross-process lock. Returns events applied.

    Order: lock -> read events (sorted by name = time_ns, pid, seq) -> per tracker file:
    read CSV once, validate + apply events in memory, atomic write -> delete applied
    events -> unlock. If the CSV write fails the events stay queued for a later drain.
    """
    queue_dir = queue_dir or TRACKER_QUEUE_DIR
    lock_path = lock_path or TRACKER_LOCK
    queue_dir.mkdir(parents=True, exist_ok=True)
    applied_total = 0
    with open(lock_path, "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            by_tracker: dict[str, list[tuple[Path, dict]]] = {}
            for path in sorted(queue_dir.glob("*.json")):
                try:
                    event = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
                    _reject_event(path, queue_dir, f"unreadable: {e}")
                    continue
                err = _event_error(event)
                if err:
                    _reject_event(path, queue_dir, err)
                    continue
                by_tracker.setdefault(event["tracker"], []).append((path, event))
            for tracker_str, events in by_tracker.items():
                tracker = Path(tracker_str)
                if not tracker.is_file():
                    for path, _ in events:
                        _reject_event(path, queue_dir, f"tracker not found: {tracker}")
                    continue
                rows = p01.read_tracker(tracker)
                index: dict[tuple, list[dict]] = {}
                for r in rows:
                    index.setdefault((r["batch"], r["job_folder"]), []).append(r)
                applied = []
                for path, event in events:
                    match = index.get((event["batch"], event["job_folder"]), [])
                    if len(match) != 1:
                        _reject_event(path, queue_dir, f"{len(match)} tracker rows for "
                                      f"{event['batch']}/{event['job_folder']}")
                        continue
                    match[0].update(event["updates"])
                    applied.append(path)
                if applied:
                    p01.write_tracker_atomic(tracker, rows)  # raises -> events stay queued
                    for path in applied:
                        path.unlink(missing_ok=True)
                    applied_total += len(applied)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return applied_total


def pending_tracker_events(queue_dir: Path | None = None) -> int:
    return len(list((queue_dir or TRACKER_QUEUE_DIR).glob("*.json")))


def queue_tracker_update(tracker: Path, batch: str, folder: str, **fields) -> None:
    """Enqueue this stage's columns for one row, then try to drain right away."""
    assert set(fields) <= set(OWNED_COLUMNS), fields
    try:
        enqueue_tracker_event(tracker, batch, folder, {**fields, "updated_at": p02.now_str()})
    except OSError as e:
        raise JobError(f"tracker event enqueue failed: {e}") from e
    try:
        drain_tracker_queue()
    except Exception as e:  # event is durable; a later drain applies it
        log(f"  WARNING tracker drain failed (event kept in queue): {type(e).__name__}: {e}")


def sync_checkpoint(tracker: Path, batch: str, job: Job) -> None:
    row = next(r for r in p01.read_tracker(tracker)
               if r["batch"] == batch and r["job_folder"] == job.folder)
    if row["final_status"] != "DONE" or row["pdf_pages"] != "1":
        queue_tracker_update(tracker, batch, job.folder, final_status="DONE", pdf_pages="1", last_error="")


# ------------------------------------------------------------------- per job

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def commit_resume_v2(job: Job, resume: dict, expected_prev) -> None:
    """Re-read job.json; write only if the inputs and the previous resume_v2 are unchanged.

    Callers pass an already validated resume. Order: verify source state -> unlink old
    resume.pdf -> atomic job.json write. A failed V2 generation never reaches this point.
    """
    try:
        current = json.loads(job.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise JobError(f"cannot re-read job.json: {e}") from e
    if not isinstance(current, dict):
        raise JobError("job.json root is no longer an object")
    changed = [k for k in SOURCE_FIELDS if current.get(k) != job.source[k]]
    if changed:
        raise JobError(f"job.json inputs changed during Final stage: {', '.join(changed)}")
    if current.get("resume_v2") != expected_prev:
        raise JobError("resume_v2 changed by another process during Final stage")
    # The new V2 is validated; drop the PDF of the old V2 before committing, so a crash
    # between commit and the next render can never leave an old 1-page PDF that looks final.
    try:
        job.pdf_path.unlink(missing_ok=True)
    except OSError as e:
        raise JobError(f"cannot invalidate old resume.pdf: {e}") from e
    current["resume_v2"] = resume
    try:
        p01.write_json_atomic(job.path, current)
    except OSError as e:
        raise JobError(f"job.json write failed: {e}") from e


def generate_v2(job: Job, ctx: dict) -> dict:
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        tele = telemetry.Invocation("04_v2", ctx["v2_cmd"], batch=ctx.get("batch"),
                                    job_folder=job.folder, attempt=attempt)
        try:
            resume = call_worker(ctx["v2_cmd"], v2_prompt(job.data), ctx["timeout"], tele)
            err = validate_resume_v2_initial(resume)
            if err:
                tele.validation("INVALID", "validation")
                raise JobError(f"invalid Resume V2: {err}")
            tele.validation("VALID")
            return resume
        except JobError as e:
            last = e
            if attempt < MAX_ATTEMPTS:
                log(f"  attempt {attempt} V2 failed for {job.label}: {e} — retrying with a fresh worker")
        finally:
            tele.emit()
    raise last


def trim_once(job: Job, ctx: dict, resume: dict, validation: dict) -> dict:
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        tele = telemetry.Invocation("04_trim", ctx["trim_cmd"], batch=ctx.get("batch"),
                                    job_folder=job.folder, attempt=attempt,
                                    trim_iteration=validation.get("trim_iteration"))
        try:
            trimmed = call_worker(ctx["trim_cmd"],
                                  trim_prompt(resume, job.data["quality_feedback"], validation),
                                  ctx["timeout"], tele)
            err = validate_resume_v2_trimmed(trimmed, resume)
            if err:
                tele.validation("INVALID", "validation")
                raise JobError(err)
            tele.validation("VALID")
            return trimmed
        except JobError as e:
            last = e
            if attempt < MAX_ATTEMPTS:
                log(f"  attempt {attempt} trim failed for {job.label}: {e} — retrying with a fresh worker")
        finally:
            tele.emit()
    raise last


def process_job(job: Job, ctx: dict) -> tuple[str, str]:
    """One job, strictly serial: V2 -> HTML -> PDF -> check -> (trim -> HTML -> PDF -> check)*."""
    tracker, batch = ctx["tracker"], ctx["batch"]
    try:
        queue_tracker_update(tracker, batch, job.folder, final_status="RUNNING", last_error="")
        previous = job.data.get("resume_v2")
        if job.state == "NEEDS_V2":
            resume = generate_v2(job, ctx)
            commit_resume_v2(job, resume, previous)
        else:
            resume = previous
        trims = 0
        while True:
            write_text_atomic(job.html_path, render_html(resume, ctx["prefix"]))
            render_pdf(ctx["chrome"], job.html_path, job.pdf_path)
            pages = pdf_lines(job.pdf_path)
            if len(pages) == 1:
                queue_tracker_update(tracker, batch, job.folder,
                               final_status="DONE", pdf_pages="1", last_error="")
                return "DONE", f"1 page | trim passes {trims}"
            if trims >= ctx["max_trims"]:
                msg = f"still exceeds one page after {trims} trim passes"
                queue_tracker_update(tracker, batch, job.folder, final_status="LAYOUT_FAILED",
                               pdf_pages=str(len(pages)), last_error=msg)
                return "LAYOUT_FAILED", f"{len(pages)} pages after {trims} trim passes"
            trims += 1
            validation = build_pdf_validation(pages, trims)
            log(f"  {job.label}: {validation['page_count']} pages, "
                f"{validation['overflow_lines']} overflow lines -> trim pass {trims}")
            trimmed = trim_once(job, ctx, resume, validation)
            commit_resume_v2(job, trimmed, resume)
            resume = trimmed
    except Exception as e:
        reason = str(e) if isinstance(e, (JobError, PipelineError)) else f"{type(e).__name__}: {e}"
        try:
            queue_tracker_update(tracker, batch, job.folder, final_status="FAILED", last_error=reason[:300])
        except Exception as te:
            reason += f"; tracker update failed: {te}"
        return "FAILED", reason


# --------------------------------------------------------------------- main

def run(args, stats: dict) -> int:
    jobs_root = Path(args.jobs_root).expanduser().resolve()
    manifest = (Path(args.input).expanduser().resolve() if args.input else find_latest_feedback())
    try:
        batch, entries = p03.load_manifest(manifest, jobs_root)
    except p03.PipelineError as e:
        raise PipelineError(str(e)) from e
    check_review_gate(manifest.parent)
    check_review_gate(HANDOFF_ROOT / batch)
    prefix = _template_prefix()
    jobs = load_jobs(entries, args.force, prefix)       # all before any worker starts
    tracker = jobs_root / p01.TRACKER_NAME
    if not args.dry_run:  # recover events left by a crashed 04 process (dry-run never writes)
        try:
            drained = drain_tracker_queue()
        except Exception as e:
            raise PipelineError(f"tracker queue drain failed at startup: {e}") from e
        if drained:
            print(f"Tracker queue: applied {drained} pending event(s) at startup")
    check_tracker(tracker, batch, jobs)

    checkpoints = [j for j in jobs if j.state == "CHECKPOINT"]
    pending = [j for j in jobs if j.state != "CHECKPOINT"]
    to_run = pending[:args.limit] if args.limit is not None else pending
    stats.update(manifest=str(manifest), batch=batch, jobs=len(jobs),
                 deferred=len(pending) - len(to_run))

    print(f"Manifest: {manifest}")
    print(f"Batch: {batch}")
    print(f"Tracker: {tracker}")
    print(f"Jobs in handoff: {len(jobs)}\n")
    print(f"CHECKPOINT: {len(checkpoints)}")
    print(f"REUSE V2: {sum(j.state == 'REUSE' for j in pending)}")
    print(f"NEEDS V2: {sum(j.state == 'NEEDS_V2' for j in pending)}")
    print(f"THIS RUN: {len(to_run)}")
    print(f"DEFERRED: {len(pending) - len(to_run)}")
    print(f"Workers: {args.workers} | Max trim iterations: {args.max_trim_iterations} | "
          f"Timeout: {args.timeout}s | Force: {args.force}\n")

    if args.dry_run:
        for j in checkpoints:
            print(f"[CHECKPOINT] {j.folder} | {j.note}")
        for j in to_run:
            print(f"[WOULD {'GENERATE V2' if j.state == 'NEEDS_V2' else 'REUSE V2'}] {j.folder} | {j.note}")
        for j in pending[len(to_run):]:
            print(f"[DEFERRED] {j.folder}")
        stats["dry_run"] = True
        return 0

    claude = p02.find_claude() if to_run else None
    ctx = {"tracker": tracker, "batch": batch, "timeout": args.timeout, "prefix": prefix,
           "max_trims": args.max_trim_iterations,
           "chrome": find_chrome() if to_run else None,
           "v2_cmd": worker_command(V2_AGENT, claude, args.model) if to_run else None,
           "trim_cmd": worker_command(TRIM_AGENT, claude, args.model) if to_run else None}
    total = len(checkpoints) + len(to_run)

    def progress():
        done = sum(stats[k] for k in ("DONE", "FAILED", "LAYOUT_FAILED", "SKIPPED"))
        log(f"PROGRESS FINAL {done}/{total} | DONE {stats['DONE']} FAILED {stats['FAILED']} "
            f"LAYOUT_FAILED {stats['LAYOUT_FAILED']} SKIPPED {stats['SKIPPED']}")

    for j in checkpoints:
        try:
            sync_checkpoint(tracker, batch, j)
            stats["SKIPPED"] += 1
            log(f"[SKIPPED] {j.label} | existing valid 1-page final resume")
        except Exception as e:
            stats["FAILED"] += 1
            log(f"[FAILED] {j.label} | checkpoint tracker sync failed: {e}")
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

    try:
        drain_tracker_queue()  # final flush before exit
    except Exception as e:
        log(f"WARNING final tracker drain failed: {type(e).__name__}: {e}")
    stats["queue_pending"] = pending_tracker_events()
    if stats["queue_pending"]:
        return 1
    return 1 if stats["FAILED"] or stats["LAYOUT_FAILED"] else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Final Resume: V2 -> HTML -> PDF -> one-page check -> trim.")
    ap.add_argument("--input", help=f"feedback manifest (default: newest <batch>/{FEEDBACK_NAME})")
    ap.add_argument("--jobs-root", default=str(DEFAULT_JOBS_ROOT),
                    help="jobs root the manifest paths must live under")
    ap.add_argument("--limit", type=int, help="process only the first N non-checkpoint jobs")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"jobs processed in parallel (default {DEFAULT_WORKERS})")
    ap.add_argument("--max-trim-iterations", type=int, default=DEFAULT_MAX_TRIM_ITERATIONS,
                    help=f"trim passes per job before LAYOUT_FAILED (default {DEFAULT_MAX_TRIM_ITERATIONS})")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help=f"seconds per Claude attempt (default {DEFAULT_TIMEOUT})")
    ap.add_argument("--model", help="override worker model")
    ap.add_argument("--force", action="store_true", help="regenerate Resume V2 even if a valid one exists")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and plan only; no Claude, no rendering, no writes")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    for name in ("workers", "max_trim_iterations", "timeout"):
        if getattr(args, name) < 1:
            ap.error(f"--{name.replace('_', '-')} must be >= 1")
    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be >= 1")

    stats = {"manifest": "?", "batch": "?", "jobs": 0, "DONE": 0, "FAILED": 0,
             "LAYOUT_FAILED": 0, "SKIPPED": 0, "deferred": 0, "dry_run": False}
    try:
        code = run(args, stats)
    except (PipelineError, p02.PipelineError) as e:
        print(f"FAILED: {e}")
        print("DONE exit=1")
        return 1
    except KeyboardInterrupt:
        print("\nFAILED: interrupted; committed resume_v2 versions are kept, rerun resumes from them")
        print("DONE exit=130")
        return 130

    if stats["dry_run"]:
        print("\nDry run: no Claude, no rendering, nothing written")
    else:
        print("\nFinal Resume stage complete\n")
        print(f"Manifest: {stats['manifest']}")
        print(f"Batch: {stats['batch']}")
        print(f"Jobs in handoff: {stats['jobs']}")
        print(f"DONE: {stats['DONE']}")
        print(f"SKIPPED: {stats['SKIPPED']}")
        print(f"LAYOUT_FAILED: {stats['LAYOUT_FAILED']}")
        print(f"FAILED: {stats['FAILED']}")
        print(f"DEFERRED: {stats['deferred']}")
        if stats.get("queue_pending"):
            print(f"FAILED: {stats['queue_pending']} tracker event(s) still queued in {TRACKER_QUEUE_DIR}")
    print(f"\nDONE exit={code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
