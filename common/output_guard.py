"""Last check before generated text can leave the machine (resume / cover letter go to employers).

Rejects output that contains
- links or e-mail addresses that are not the candidate's own (candidate.json). Links that appear
  in the JD are NOT allowed either: the JD is attacker-controlled, so "it was in the JD" is exactly
  how an injected URL would try to get through,
- strings that look like credentials or keys,
- markers of the worker's own prompt (embedded file blocks, rule-file names, input wrappers),
  i.e. signs that a prompt-injected JD made the model dump its instructions or knowledge files.

This is defence in depth: single-turn workers have no tools, so they cannot read files or open
URLs; this guard catches the remaining channel, which is the generated text itself.
"""
from __future__ import annotations

import json
import re

from common import candidate

# Explicit links (scheme or www.) and bare domains followed by a path. Bare names such as
# "ASP.NET" or "Socket.IO" are technology names, not links.
URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"')\]]+"
                    r"|\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|ai|ca|dev|app|co|me|xyz|info|biz|ly|gg|sh|site|"
                    r"link|page|cloud|tech|ru|cn|uk|de|us)/[^\s<>\"')\]]+")
TECH_NAMES = ("asp.net", "vb.net", "ado.net", "socket.io")  # look like domains, are not links
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
SECRET_RES = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bssh-(?:rsa|ed25519) AAAA"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|xox[abp]-[A-Za-z0-9-]{10,})"),
)
PROMPT_MARKERS = ("<FILE path=", "</FILE>", "<JOB_INPUT>", "</JOB_INPUT>", "knowledge/", "instructions/",
                  "candidate.json", "bullet_bank", "SCREENING_RULES", "CANDIDATE_CAPABILITY_PROFILE",
                  "system prompt", "system-prompt")


def _norm(s: str) -> str:
    return re.sub(r"^(?:https?://)?(?:www\.)?", "", s.strip().lower()).rstrip("/.")


def allowed_references() -> set[str]:
    allowed = set()
    for item in candidate.load()["contact"]:
        for v in (item.get("text", ""), item.get("href", "")):
            if v:
                allowed.add(_norm(v.replace("mailto:", "")))
    return allowed


def check(output, jd: str = "") -> str | None:
    """None if `output` (str or JSON-serializable) is safe to publish, else the reason.
    `jd` is accepted for call-site symmetry but deliberately grants nothing."""
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    for marker in PROMPT_MARKERS:
        if marker.lower() in text.lower():
            return f"output contains prompt marker {marker!r}"
    for rx in SECRET_RES:
        if rx.search(text):
            return "output contains a credential-like string"
    allowed = allowed_references()
    for m in EMAIL_RE.findall(text):
        if _norm(m) not in allowed:
            return f"output contains an e-mail address not in candidate.json: {m}"
    for m in URL_RE.findall(text):
        if m in EMAIL_RE.findall(text) or any(m in e for e in EMAIL_RE.findall(text)):
            continue
        n = _norm(m)
        if n.split("/", 1)[0] in TECH_NAMES:
            continue
        if n not in allowed and not any(n.startswith(a) or a.startswith(n) for a in allowed):
            return f"output contains a link not in candidate.json: {m}"
    return None
