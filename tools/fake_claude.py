#!/usr/bin/env python3
"""Deterministic stand-in for `claude -p --output-format json` (tests, CI and the offline demo).

It reads the worker's system-prompt file and the user prompt exactly as the real CLI would get
them, and answers like a well-behaved single-turn worker: one turn, no tools, a plausible usage
block (cold first request, then a constant cached prefix) and an output that passes the stage's
validator. The "judgement" is rule-based, not a model:

  screening   REJECT on 5+ years / French / current-student requirements, else ACCEPT; every
              jd_anchor is copied verbatim from the JD, as the real contract requires
  resume V1   plain-text resume from candidate.json + the first bullets of each bullet bank
  resume V2   the same content in the V2 JSON schema
  trim        drops the last qualification, then the last project tool
  cover       four paragraphs within the word limits
  title       rejects titles with an obviously non-data keyword (sales, recruiter, nurse, ...),
              accepts the rest: an exact partition of the input source_keys

Usage: CLAUDE_BIN=tools/fake_claude.py  (the pipeline finds it like the real CLI)
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

MODEL = "fake-model"


def arg(name: str) -> str | None:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def files(system: str) -> dict[str, str]:
    return dict(re.findall(r'<FILE path="([^"]+)">\n(.*?)\n</FILE>', system, re.S))


def job_payload(prompt: str) -> dict:
    m = re.search(r"<JOB_INPUT>\n(.*)\n</JOB_INPUT>", prompt, re.S)
    body = m.group(1) if m else prompt.partition("\n\n")[2]
    return json.loads(body)


def sentences(jd: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", jd) if len(s.strip()) > 20]


# --------------------------------------------------------------- title screen
TITLE_REJECT = re.compile(r"(?i)\b(sales|account executive|recruit|nurse|warehouse|driver|cashier|"
                          r"legal counsel|bookkeep|designer|software engineer|devops)\b")


def title_screen(prompt: str) -> dict:
    jobs = json.loads(re.search(r"<TITLE_INPUT>\n(.*)\n</TITLE_INPUT>", prompt, re.S).group(1))
    rejected = [j["source_key"] for j in jobs if TITLE_REJECT.search(j["position"])]
    return {"accepted_ids": [j["source_key"] for j in jobs if j["source_key"] not in rejected],
            "rejected_ids": rejected}


# ------------------------------------------------------------------ screening
def screen(job: dict) -> dict:
    jd = job.get("jd") or ""
    rules = (("REQUIRED_EXPERIENCE", r"\b(?:[5-9]|1\d)\+?\s*(?:or more\s+)?years"),
             ("LANGUAGE", r"\bFrench\b|bilingu"),
             ("STUDENT_ELIGIBILITY", r"currently enrolled|co-?op student|returning to school"))
    for kind, rx in rules:
        hit = next((s for s in sentences(jd) if re.search(rx, s, re.I)), None)
        if hit:
            return {"source_key": job["source_key"],
                    "hard_reject": {"hit": True, "type": kind, "jd_anchor": hit[:120]},
                    "core_responsibilities": [], "anchor_direct_match": None,
                    "main_gap": {"type": "STRUCTURAL", "description": f"{kind.lower()} requirement not met"},
                    "decision": "REJECT", "reason": f"Hard reject ({kind}): {hit[:80]}"}
    picks = sentences(jd)[:3]
    crs = [{"responsibility": s[:60], "jd_anchor": s[:100], "match": "DIRECT" if i == 0 else "ADJACENT"}
           for i, s in enumerate(picks)]
    return {"source_key": job["source_key"], "hard_reject": {"hit": False, "type": None, "jd_anchor": None},
            "core_responsibilities": crs, "anchor_direct_match": picks[0][:60] if picks else None,
            "main_gap": {"type": "LEARNABLE", "description": "tooling differs from the candidate's stack"},
            "decision": "ACCEPT" if len(picks) == 3 else "UNABLE TO ASSESS",
            "reason": "Core responsibilities match the candidate's data pipeline and BI experience."}


# ------------------------------------------------------------------- resumes
def candidate_facts(kb: dict[str, str]) -> tuple[dict, dict[str, list[str]], dict]:
    c = json.loads(kb["knowledge/candidate.json"])
    banks = {e["company"]: re.findall(r"\*\*Bullet:\*\*\s*(.+)", kb[f"knowledge/{e['bullet_bank']}"])
             for e in c["experience"]}
    lib = kb[f"knowledge/{c['knowledge']['project_library']}"]
    m = re.search(r"### Project 1: (.+)\n\*\*Timeline\*\*: (.+?)\s*\n", lib)
    tools = re.search(r"#### Technical Stack\n- (.+)", lib)
    ach = re.search(r"#### Key Achievements\n- (.+)", lib)
    project = {"name": m.group(1).strip(), "date": m.group(2).strip(),
               "tools": (tools.group(1) if tools else "Python").strip(),
               "bullet": (ach.group(1) if ach else "Built an analytical project").strip()}
    return c, banks, project


def resume_v1(job: dict, kb: dict[str, str]) -> str:
    c, banks, p = candidate_facts(kb)
    lines = [c["name"], " | ".join(x["text"] for x in c["contact"]), "", job["position"].upper(),
             f"Data analyst with hands-on experience relevant to the {job['position']} role at {job['company']}.",
             "", "SUMMARY OF QUALIFICATIONS"]
    lines += [f"• {b}" for b in list(banks.values())[0][:3]]
    lines += ["", "CORE SKILLS", "Technical: Python, SQL, Power BI", "Professional: Stakeholder Engagement",
              "", "PROFESSIONAL EXPERIENCE"]
    for e in c["experience"]:
        lines += [f"{e['role']}, {e['company']} | {e['date']}"] + [f"• {b}" for b in banks[e["company"]][:3]]
    lines += ["", "PROJECT EXPERIENCE", f"{p['name']} – {p['tools']} | {p['date']}", f"• {p['bullet']}",
              "", "EDUCATION"]
    for ed in c["education"]:
        lines.append(f"{ed['degree']}, {ed['school']}" + (f" ({ed['details']})" if ed["details"] else "")
                     + f" | {ed['date']}")
        if ed["coursework"]:
            lines.append(f"Relevant coursework: {ed['coursework']}")
    return "\n".join(lines)


def resume_v2(job: dict, kb: dict[str, str]) -> dict:
    c, banks, p = candidate_facts(kb)
    first = list(banks.values())[0]
    return {"job_title": job["position"],
            "summary": f"Data analyst with hands-on pipeline, BI and modeling experience for {job['company']}.",
            "qualifications": [b.rstrip(".") for b in first[:3]],
            "skills": {"technical": ["Python", "SQL", "Power BI"], "professional": ["Stakeholder Engagement"]},
            "experience": [{**{k: e[k] for k in ("role", "company", "date")}, "bullets": banks[e["company"]][:3]}
                           for e in c["experience"]],
            "project": p,
            "education": [{k: ed[k] for k in ("degree", "school", "details", "date", "coursework")}
                          for ed in c["education"]]}


def trim(payload: dict) -> dict:
    r = json.loads(json.dumps(payload["resume_v2"]))
    if len(r["qualifications"]) > 3:
        r["qualifications"].pop()
    else:
        r["summary"] = r["summary"].split(",")[0].rstrip(".") + "."
    return r


def cover_letter(payload: dict) -> dict:
    job = payload["job"]
    words = ("I bring hands-on experience building data pipelines, cleaning multi-source data and "
             "delivering dashboards that stakeholders use to make decisions. ")
    para = lambda lead: (lead + " " + words * 3).strip()
    return {"paragraphs": {
        "opening": para(f"I am applying for the {job['position']} position at {job['company']}."),
        "technical_fit": para("My recent work combined SQL, Python and BI tooling on real business data."),
        "company_fit": para(f"The role at {job['company']} matches the analytical work I want to deepen."),
        "closing": para("Thank you for your time and consideration; I would welcome a conversation.")}}


# ------------------------------------------------------------------ envelope
def usage(system: str, prompt: str, state_dir: Path) -> dict:
    sys_tokens, job_tokens = int(0.47 * len(system)), int(0.45 * len(prompt)) + 300
    seen = state_dir / f".fake_cache_{hashlib.sha256(system.encode()).hexdigest()[:16]}"
    warm = seen.exists()
    seen.touch()
    return {"input_tokens": 2, "cache_creation_input_tokens": job_tokens if warm else sys_tokens + job_tokens,
            "cache_read_input_tokens": sys_tokens if warm else 0, "output_tokens": 800,
            "server_tool_use": {"web_search_requests": 0}}


def main() -> int:
    if "--version" in sys.argv:
        print("0.0.0 (fake_claude)")
        return 0
    prompt_file = arg("--system-prompt-file")
    prompt = sys.stdin.read()
    if not prompt_file:
        print(json.dumps({"type": "result", "subtype": "error", "is_error": True,
                          "result": "fake_claude supports single-turn workers only"}))
        return 1
    system = Path(prompt_file).read_text(encoding="utf-8")
    kb = files(system)
    if "instructions/title_screening.md" in kb:
        out = title_screen(prompt)
    elif "<SCREENING_RULES>" in system:
        out = screen(job_payload(prompt))
    elif "instructions/resume_v1.md" in kb:
        out = {"resume_v1": resume_v1(job_payload(prompt), kb)}
    elif "instructions/resume_v2.md" in kb:
        out = resume_v2(job_payload(prompt), kb)
    elif "instructions/resume_trim.md" in kb:
        out = trim(job_payload(prompt))
    elif "instructions/cover_letter.md" in kb:
        out = cover_letter(job_payload(prompt))
    else:
        out = {}
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                      "duration_ms": 5, "result": json.dumps(out, ensure_ascii=False), "session_id": "fake",
                      "total_cost_usd": 0.0, "usage": usage(system, prompt, Path(prompt_file).parent),
                      "modelUsage": {MODEL: {"inputTokens": 2, "outputTokens": 800}},
                      "permission_denials": []}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
