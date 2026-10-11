#!/usr/bin/env python3
"""On-demand Cover Letter for ONE job: job.json -> fresh cover-letter-worker -> validate -> HTML -> PDF.

Triggered by Browser Lab only after an application page offers a Cover Letter (or extra
supporting document) upload. Browser Claude passes the job folder and nothing else:

    job_dir/job.json -> company, position, jd, resume_v1, resume_v2 (+ optional location)
    -> one JSON object -> fresh cover-letter-worker (instructions/cover_letter.md)
    -> {"paragraphs": {opening, technical_fit, company_fit, closing}} -> validate (retry once)
    -> + date / recipient_lines / salutation / signature (deterministic)
    -> job_dir/cover_letter.html (structure + CSS of templates/coverletter_format_example.html)
    -> job_dir/cover_letter.pdf (04's headless Chrome render)

Reads job.json only; never runs or rewrites upstream steps (JD, quality review, Resume V1/V2, trim)
and never writes job.json or the tracker. An existing valid cover_letter.pdf is kept unless
--force. One job per process; no queue, no pool, no browser control.

Usage:
    python3 script/generate_cover_letter.py --job-dir "/abs/jobs/<batch>/<job_folder>"
    python3 script/generate_cover_letter.py --job-dir ... --force          # regenerate
    python3 script/generate_cover_letter.py --job-dir ... --research       # allow WebSearch (off by default)
    python3 script/generate_cover_letter.py --job-dir ... --dry-run        # print worker input

Output protocol (line-buffered):
    [DONE|SKIPPED|BUSY|FAILED] Company — Position [| detail]
    COVER_LETTER_PDF <abs path>                       (DONE / SKIPPED only)
    PROCESS_EXIT code=<code>                          (always last; 0 = PDF ready)
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import importlib.util
import json
import re
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _load_stage4():
    """Reuse 04's worker call, JSON parsing, resume validators and Chrome PDF render."""
    spec = importlib.util.spec_from_file_location("generate_final", SCRIPT_DIR / "04_generate_final.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


p04 = _load_stage4()
p02 = p04.p02

ROOT = p04.ROOT  # resume/ (templates); workers run in p02.worker_cwd()
candidate = p02.candidate
TEMPLATE = ROOT / "templates" / "coverletter_format_example.html"
TEMPLATE_BODY_MARKER = "  <!-- DATE -->"

AGENT = "cover-letter-worker"
DEFAULT_TIMEOUT = 420           # per Claude attempt (generation + optional web research)
MAX_ATTEMPTS = p04.MAX_ATTEMPTS  # first try + 1 retry with a fresh worker, as in 04

HTML_NAME, PDF_NAME = "cover_letter.html", "cover_letter.pdf"
LOCK_DIR = p02.DATA_ROOT / ".runtime" / "locks" / "cover_letter"  # one lock file per job folder
REQUIRED_FIELDS = ("company", "position", "jd", "resume_v1")

PARAGRAPH_KEYS = ("opening", "technical_fit", "company_fit", "closing")
TARGET_WORDS = (300, 400)        # generation target sent to the worker
WORD_TOLERANCE = (270, 450)      # validator: no retry for small misses like 405
MIN_PARAGRAPH_WORDS = 15

SALUTATION = "Dear Hiring Manager,"

PLACEHOLDER_RE = re.compile(
    r"\{\{.*?\}\}|\{[A-Za-z_][\w ]*\}|"
    r"\[(?:company|position|role|job title|hiring manager|name|date|location|your name)[^\]]*\]",
    re.I)
HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>|&[a-z]+;|&#\d+;")
MARKDOWN_RE = re.compile(r"```|\*\*|__|^\s*(?:#{1,6}\s|[-*+]\s|\d+\.\s)", re.M)
# Present-/future-tense study claims; invalid once every degree's end date is before the letter date.
IN_PROGRESS_STUDY_RE = re.compile(
    r"\b(?:am|I'm|currently)\s+(?:completing|pursuing|finishing|studying|enrolled)\b"
    r"|\b(?:will|expect to|expected to)\s+graduate\b|\bupcoming graduation\b"
    r"|\bcurrent(?:ly)?\s+(?:a\s+)?(?:graduate\s+|master'?s\s+)?student\b", re.I)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


class CoverLetterError(Exception):
    """Fail-closed error for this job."""


# --------------------------------------------------------------- job input

def load_job(job_dir: Path) -> dict:
    """Read job.json and return the fields this stage needs. Never guesses the schema."""
    if not job_dir.is_dir():
        raise CoverLetterError(f"job dir not found: {job_dir}")
    path = job_dir / "job.json"
    if not path.is_file():
        raise CoverLetterError(f"job.json not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise CoverLetterError(f"job.json unreadable: {e}") from e
    if not isinstance(data, dict):
        raise CoverLetterError("job.json root is not an object")
    bad = [f for f in REQUIRED_FIELDS if not p04._nonempty_str(data.get(f))]
    if bad:
        raise CoverLetterError(f"job.json missing/empty: {', '.join(bad)}")
    err = p02.validate_resume(data["resume_v1"], data["position"])
    if err:
        raise CoverLetterError(f"resume_v1 invalid ({err})")
    if "resume_v2" not in data:
        raise CoverLetterError("job.json has no resume_v2 (run 04_generate_final.py first)")
    err = p04.validate_resume_v2_stored(data["resume_v2"])
    if err:
        raise CoverLetterError(f"resume_v2 invalid ({err})")
    location = data.get("location")  # optional; current 01 schema has no location field
    return {"company": data["company"].strip(), "position": data["position"].strip(),
            "location": location.strip() if p04._nonempty_str(location) else None,
            "jd": data["jd"], "resume_v1": data["resume_v1"], "resume_v2": data["resume_v2"]}


def lock_path(job_dir: Path) -> Path:
    """<batch>__<job_folder>__<hash of abs path>.lock — readable and unique per job folder."""
    digest = hashlib.sha1(str(job_dir).encode("utf-8")).hexdigest()[:12]
    name = f"{job_dir.parent.name}__{job_dir.name}"[:180]
    return LOCK_DIR / f"{name}__{digest}.lock"


def _end_of(date_text: str) -> tuple[int, int] | None:
    """'Sep 2024 – Apr 2026' -> (2026, 4); None if the end is 'Present' or unparseable."""
    parts = re.split(r"\s*[–—-]\s*", str(date_text or ""))
    m = re.match(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{4})", parts[-1].strip()) if parts else None
    if not m or m.group(1).lower() not in MONTHS:
        return None
    return int(m.group(2)), MONTHS[m.group(1).lower()]


def education_status(job: dict, today: date) -> list[dict]:
    """Deterministic graduated / in_progress flag per resume_v2 education entry."""
    out = []
    for e in (job["resume_v2"].get("education") or []):
        end = _end_of(e.get("date", ""))
        done = end is not None and end < (today.year, today.month)
        out.append({"school": e.get("school"), "degree": e.get("degree"), "date": e.get("date"),
                    "status": "graduated" if done else "in_progress"})
    return out


def worker_input(job: dict, allow_research: bool, today: date | None = None) -> dict:
    today = today or date.today()
    return {
        "job": {k: job[k] for k in ("position", "company", "location", "jd")},
        "candidate": {"resume_v1": job["resume_v1"], "resume_v2": job["resume_v2"],
                      "education_status": education_status(job, today)},
        "generation": {"target_words_min": TARGET_WORDS[0], "target_words_max": TARGET_WORDS[1],
                       "allow_company_research": allow_research,
                       "letter_date": today.isoformat()},
    }


def build_prompt(payload: dict) -> str:
    return p02.single_turn.wrap_job(
        "请严格按照 instructions/cover_letter.md 和 cover-letter-worker 的 Execution Contract，"
        "为下面这 1 个职位生成 Cover Letter 正文。只返回 1 个 JSON object："
        '{"paragraphs": {"opening": "...", "technical_fit": "...", "company_fit": "...", '
        '"closing": "..."}}（paragraphs 是 object，不是 array），不要输出任何其他内容。', payload)


# --------------------------------------------------------------- validation

def word_count(text: str) -> int:
    return len(text.split())


def validate_cover_letter(obj, edu_status: list[dict] | None = None) -> str | None:
    """Lightweight deterministic check of the worker output. None if valid, else a reason."""
    if not isinstance(obj, dict):
        return "worker result is not a JSON object"
    if set(obj) != {"paragraphs"}:
        return f"root keys must be exactly ['paragraphs'], got {sorted(obj)}"
    paras = obj["paragraphs"]
    if not isinstance(paras, dict):
        return f"paragraphs must be an object, got {type(paras).__name__}"
    if set(paras) != set(PARAGRAPH_KEYS):
        missing = [k for k in PARAGRAPH_KEYS if k not in paras]
        extra = sorted(set(paras) - set(PARAGRAPH_KEYS))
        return f"paragraphs keys must be {list(PARAGRAPH_KEYS)} (missing {missing}, extra {extra})"
    for k in PARAGRAPH_KEYS:
        text = paras[k]
        if not p04._nonempty_str(text):
            return f"paragraphs.{k} must be a non-empty string"
        if (m := PLACEHOLDER_RE.search(text)):
            return f"paragraphs.{k} contains a placeholder: {m.group()!r}"
        if (m := HTML_TAG_RE.search(text)):
            return f"paragraphs.{k} contains HTML: {m.group()!r}"
        if (m := MARKDOWN_RE.search(text)):
            return f"paragraphs.{k} contains Markdown: {m.group().strip()!r}"
        if "\n\n" in text.strip():
            return f"paragraphs.{k} contains more than one paragraph"
        if word_count(text) < MIN_PARAGRAPH_WORDS:
            return f"paragraphs.{k} has only {word_count(text)} words"
    if edu_status and all(e["status"] == "graduated" for e in edu_status):
        for k in PARAGRAPH_KEYS:
            if (m := IN_PROGRESS_STUDY_RE.search(paras[k])):
                return (f"paragraphs.{k} describes studies as in progress ({m.group()!r}) but every "
                        f"degree ended before the letter date — use past tense (e.g. 'recently completed')")
    total = sum(word_count(paras[k]) for k in PARAGRAPH_KEYS)
    if not WORD_TOLERANCE[0] <= total <= WORD_TOLERANCE[1]:
        return f"body has {total} words, allowed {WORD_TOLERANCE[0]}-{WORD_TOLERANCE[1]}"
    return None


# ------------------------------------------------------------------ worker

def worker_command(claude_bin: str, model: str | None, allow_research: bool,
                   mode: str | None = None) -> list[str]:
    """single-turn (default): rules in a system-prompt file, no Read, no WebFetch; WebSearch only
    when research is explicitly allowed. The JD is untrusted, so no tool may fetch an arbitrary
    URL (a URL can carry data out). agent mode (rollback) keeps Read for the rule file."""
    if p02.resolve_worker_mode(mode) == p02.WORKER_SINGLE_TURN:
        return p02.worker_command(claude_bin, model, agent=AGENT, mode=mode,
                                  tools="WebSearch" if allow_research else "")
    tools = "Read,WebSearch" if allow_research else "Read"
    cmd = [claude_bin, "-p",
           "--agent", AGENT,
           "--output-format", "json",
           "--no-session-persistence",
           "--strict-mcp-config",       # no MCP / browser
           "--disable-slash-commands",
           "--tools", tools,
           "--allowedTools", tools,
           "--permission-mode", "dontAsk"]
    if model:
        cmd += ["--model", model]
    return cmd


def generate_paragraphs(payload: dict, cmd: list[str], timeout: int, label: str) -> dict:
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            obj = p04.call_worker(cmd, build_prompt(payload), timeout)
            err = (validate_cover_letter(obj, payload["candidate"].get("education_status"))
                   or p02.output_guard.check(obj, payload.get("job", {}).get("jd", "")))
            if err:
                raise p04.JobError(f"invalid cover letter: {err}")
            return obj["paragraphs"]
        except p04.JobError as e:
            last = e
            if attempt < MAX_ATTEMPTS:
                log(f"  attempt {attempt} failed for {label}: {e} — retrying with a fresh worker")
    raise CoverLetterError(str(last))


# ------------------------------------------------------------------ render

def letter_date(today: date) -> str:
    return f"{today:%B} {today.day}, {today.year}"  # "October 4, 2026", as in the reference


def build_cover_letter_data(job: dict, paragraphs: dict, today: date) -> dict:
    recipient = ["Hiring Manager", job["company"]]
    if job["location"]:  # only a location job.json actually has; never a searched address
        recipient.append(job["location"])
    return {"date": letter_date(today), "recipient_lines": recipient, "salutation": SALUTATION,
            "paragraphs": {k: " ".join(paragraphs[k].split()) for k in PARAGRAPH_KEYS},
            "signature": candidate.load()["name"]}


def template_prefix() -> str:
    """Reference head (fixed CSS) + fixed header, verbatim up to the date block."""
    text = TEMPLATE.read_text(encoding="utf-8")
    if (text.count(TEMPLATE_BODY_MARKER) != 1 or "<style>" not in text
            or len(re.findall(r"<title>[^<]*</title>", text)) != 1):
        raise CoverLetterError(f"reference structure changed; markers not found once: {TEMPLATE}")
    prefix = candidate.fill_header(text.split(TEMPLATE_BODY_MARKER)[0])
    title = f"{candidate.load()['file_stem']}_Cover_Letter"
    prefix = re.sub(r"<title>[^<]*</title>", f"<title>{title}</title>", prefix, count=1)
    return re.sub(r"\n  <!--\s*REFERENCE SPEC.*?-->\n", "\n", prefix, count=1, flags=re.S)


def render_html(d: dict, prefix: str) -> str:
    """Fill the reference structure with cover_letter_data. No CSS changes."""
    e = html.escape
    lines = d["recipient_lines"]
    recipient = [f"    {e(x)}<br>" for x in lines[:-1]] + [f"    {e(lines[-1])}"]
    out = [prefix + TEMPLATE_BODY_MARKER,
           f'  <div class="letter-date" data-field="date">{e(d["date"])}</div>',
           "",
           "  <!-- RECIPIENT -->",
           '  <div class="recipient" data-repeat="recipient_lines">',
           *recipient,
           "  </div>",
           "",
           "  <!-- SALUTATION -->",
           f'  <div class="salutation" data-field="salutation">{e(d["salutation"])}</div>',
           "",
           "  <!-- BODY -->",
           '  <div class="letter-body" data-section="paragraphs">']
    for k in PARAGRAPH_KEYS:
        out += ["", f'    <p data-field="paragraphs.{k}">{e(d["paragraphs"][k])}</p>']
    out += ["",
            "  </div>",
            "",
            "  <!-- CLOSING -->",
            '  <div class="closing">',
            "    Sincerely,<br>",
            f'    <div class="signature" data-field="signature">{e(d["signature"])}</div>',
            "  </div>",
            "",
            "</body>",
            "</html>",
            ""]
    return "\n".join(out)


def write_outputs(job_dir: Path, d: dict, prefix: str, chrome: str) -> int:
    """Drop the old PDF first (a crash never leaves a stale PDF next to new HTML), then render."""
    html_path, pdf_path = job_dir / HTML_NAME, job_dir / PDF_NAME
    pdf_path.unlink(missing_ok=True)
    p04.write_text_atomic(html_path, render_html(d, prefix))
    try:
        p04.render_pdf(chrome, html_path, pdf_path)
    except p04.JobError as e:
        raise CoverLetterError(str(e)) from e
    pages = p04.pdf_page_count(pdf_path)
    if not pages:
        raise CoverLetterError(f"rendered PDF unreadable: {pdf_path}")
    return pages


# --------------------------------------------------------------------- main

def log(msg: str) -> None:
    print(msg, flush=True)


def run(args) -> tuple[str, str, int]:
    job_dir = Path(args.job_dir).expanduser().resolve()
    job = load_job(job_dir)
    label = f"{job['company']} — {job['position']}"
    pdf_path = job_dir / PDF_NAME
    payload = worker_input(job, args.research)
    prefix = template_prefix()  # fail before any worker if the reference changed

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return "DRY_RUN", label, 0
    if not args.force and pdf_path.is_file() and p04.pdf_page_count(pdf_path):
        return "SKIPPED", f"{label} | existing {pdf_path} (use --force to regenerate)", 0

    # One run per job folder at a time; flock is released automatically if the process dies.
    lock_file = lock_path(job_dir)
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock = lock_file.open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return "BUSY", f"{label} | another cover letter run holds {lock_file}", 1
    try:
        if not args.force and pdf_path.is_file() and p04.pdf_page_count(pdf_path):
            return "SKIPPED", f"{label} | existing {pdf_path} (finished by another run)", 0
        chrome = p04.find_chrome()
        cmd = worker_command(p02.find_claude(), args.model, args.research, args.worker)
        log(f"Job: {job_dir}")
        log(f"Generating cover letter for {label} | research: {args.research} | "
            f"timeout {args.timeout}s x {MAX_ATTEMPTS} attempts")
        paragraphs = generate_paragraphs(payload, cmd, args.timeout, label)
        data = build_cover_letter_data(job, paragraphs, date.today())
        pages = write_outputs(job_dir, data, prefix, chrome)
        words = sum(word_count(data["paragraphs"][k]) for k in PARAGRAPH_KEYS)
        return "DONE", f"{label} | {words} words | {pages} page(s)", 0
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate cover_letter.html/.pdf for one job folder.")
    ap.add_argument("--job-dir", required=True, help="absolute path of one job folder (contains job.json)")
    ap.add_argument("--force", action="store_true",
                    help="regenerate and overwrite existing cover_letter.html / cover_letter.pdf")
    ap.add_argument("--research", action="store_true",
                    help="allow company research with WebSearch (off by default; never WebFetch)")
    ap.add_argument("--worker", choices=p02.WORKER_MODES,
                    help=f"worker mode (default: ${p02.WORKER_ENV} or single-turn)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help=f"seconds per Claude attempt (default {DEFAULT_TIMEOUT})")
    ap.add_argument("--model", help="override worker model")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate job.json and print the worker input JSON; no Claude, no writes")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    if args.timeout < 1:
        ap.error("--timeout must be >= 1")

    try:
        status, detail, code = run(args)
    except (CoverLetterError, p04.PipelineError, p02.PipelineError, OSError) as e:
        print(f"[FAILED] {args.job_dir} | {e}")
        print("PROCESS_EXIT code=1")
        return 1
    except KeyboardInterrupt:
        print("\n[FAILED] interrupted")
        print("PROCESS_EXIT code=130")
        return 130
    if status != "DRY_RUN":
        print(f"[{status}] {detail}")
        if status in ("DONE", "SKIPPED"):
            print(f"COVER_LETTER_PDF {Path(args.job_dir).expanduser().resolve() / PDF_NAME}")
    print(f"PROCESS_EXIT code={code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
