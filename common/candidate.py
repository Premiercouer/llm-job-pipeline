"""Candidate identity: the fixed facts every generated resume and cover letter must carry.

Loaded from <candidate_dir>/candidate.json (see common/paths.py for how candidate_dir is
resolved). Workers may choose and rephrase content, but never these facts: the stage
validators check model output against them, and the HTML header is filled from them.

    {
      "name": "...",
      "file_stem": "First_Last",                       # used in HTML <title> / file names
      "contact": [{"text": "City, ST"},
                  {"text": "me@example.com", "href": "mailto:me@example.com"}, ...],
      "experience": [{"role": "...", "company": "...", "date": "...",
                      "bullet_bank": "<file in knowledge/>"}, ...],          # resume order
      "education":  [{"degree": "...", "school": "...", "details": "...",
                      "date": "...", "coursework": "..."}, ...],
      "knowledge":  {"project_library": "<file>", "skills": "<file>"}
    }

Knowledge files (bullet banks, project library, skills) live in <candidate_dir>/knowledge/.
Agent workers get a copy of candidate.json as knowledge/candidate.json and read the files it
names from there (see common/worker_dir.py); prompts never contain candidate facts.
"""
from __future__ import annotations

import html
import json
from functools import lru_cache
from pathlib import Path

from common import paths

CANDIDATE_FILE = "candidate.json"
KNOWLEDGE_DIR = "knowledge"
EXPERIENCE_FIELDS = ("role", "company", "date")
EDUCATION_FIELDS = ("degree", "school", "details", "date", "coursework")
NAME_PLACEHOLDER = "{{CANDIDATE_NAME}}"
CONTACT_PLACEHOLDER = "{{CANDIDATE_CONTACT}}"


class CandidateError(Exception):
    """candidate.json is missing or malformed: every stage that needs it stops."""


def candidate_file() -> Path:
    return paths.CANDIDATE_DIR / CANDIDATE_FILE


def knowledge_dir() -> Path:
    return paths.CANDIDATE_DIR / KNOWLEDGE_DIR


@lru_cache(maxsize=1)
def load() -> dict:
    path = candidate_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise CandidateError(f"cannot read {path}: {e}")
    problems = []
    for key in ("name", "file_stem"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            problems.append(f"{key} must be a non-empty string")
    contact = data.get("contact")
    if not isinstance(contact, list) or not all(isinstance(c, dict) and c.get("text") for c in contact):
        problems.append("contact must be a list of {text, href?}")
    for section, fields in (("experience", EXPERIENCE_FIELDS), ("education", EDUCATION_FIELDS)):
        entries = data.get(section)
        if not isinstance(entries, list) or not entries:
            problems.append(f"{section} must be a non-empty list")
            continue
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict) or any(not isinstance(entry.get(f), str) for f in fields):
                problems.append(f"{section}[{i}] needs string fields {', '.join(fields)}")
    knowledge = data.get("knowledge")
    named = [e.get("bullet_bank") for e in data.get("experience") or [] if isinstance(e, dict)]
    if isinstance(knowledge, dict):
        named += [knowledge.get("project_library"), knowledge.get("skills")]
    else:
        problems.append("knowledge must be {project_library, skills}")
    for name in named:
        if not isinstance(name, str) or not (knowledge_dir() / name).is_file():
            problems.append(f"knowledge file not found: {name!r}")
    if problems:
        raise CandidateError(f"{path}: " + "; ".join(problems))
    return data


def experience_identity() -> tuple[dict, ...]:
    return tuple({f: e[f] for f in EXPERIENCE_FIELDS} for e in load()["experience"])


def education_fixed() -> tuple[dict, ...]:
    return tuple({f: e[f] for f in EDUCATION_FIELDS} for e in load()["education"])


def resume_markers() -> tuple[str, ...]:
    """Strings a plain-text Resume V1 must contain: name, employers, schools."""
    c = load()
    return (c["name"], *(e["company"] for e in c["experience"]),
            *dict.fromkeys(e["school"] for e in c["education"]))


def contact_html() -> str:
    """Contact line: plain items share a line, each link starts a new line (same HTML as before)."""
    out = ""
    for i, item in enumerate(load()["contact"]):
        text, href = html.escape(item["text"]), item.get("href")
        if i:
            out += " |" + ("\n      " if href else " ")
        out += f'<a href="{html.escape(href, quote=True)}">{text}</a>' if href else text
    return out


def fill_header(template_prefix: str) -> str:
    """Replace the identity placeholders of a template header; fail if any is missing."""
    for marker in (NAME_PLACEHOLDER, CONTACT_PLACEHOLDER):
        if marker not in template_prefix:
            raise CandidateError(f"template header lacks {marker}")
    return (template_prefix.replace(NAME_PLACEHOLDER, html.escape(load()["name"]))
            .replace(CONTACT_PLACEHOLDER, contact_html()))
