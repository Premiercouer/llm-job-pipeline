#!/usr/bin/env python3
"""Pre-publish safety scan: every file git would publish (tracked + untracked, not ignored).

Generic rules (in this repo, run in CI):
  secrets        API keys, tokens, private keys
  home paths     /Users/<name>/..., /home/<name>/...
  e-mails        anything except example.com and GitHub / Anthropic noreply addresses
  phone numbers  anything except the 555-01xx fictional range
  file names     browser profiles, cookies, application lists, saved answers, PDFs outside examples/
  size           files over 1 MB

Private rules: a deny-list kept OUTSIDE the repository (real names, employers, product names),
one case-insensitive regex per line, given by $LJP_PRIVATE_DENYLIST or --denylist. Putting those
words in this file would publish exactly what the scan is meant to keep out.

Exit 0 = clean, 1 = findings (printed as path:line: rule: excerpt).
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MAX_BYTES = 1_000_000

SECRETS = [re.compile(p) for p in (
    r"sk-ant-[A-Za-z0-9_-]{10,}", r"\bsk-[A-Za-z0-9]{32,}", r"AKIA[0-9A-Z]{16}",
    r"gh[pousr]_[A-Za-z0-9]{30,}", r"github_pat_[A-Za-z0-9_]{20,}", r"xox[abp]-[A-Za-z0-9-]{10,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d)\s*[:=]\s*[\"'][^\"'\s]{12,}[\"']")]
HOME = re.compile(r"/(?:Users|home)/(?!<|\$|\{)[A-Za-z0-9._-]+/")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
EMAIL_OK = re.compile(r"(?i)(?:^|\.)(?:example\.(?:com|org|net)|users\.noreply\.github\.com|anthropic\.com|x\.test)$")
PHONE = re.compile(r"\(?\b\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b")
PHONE_OK = re.compile(r"\b555[ .-]01\d\d\b")  # NANP fictional range XXX-555-01XX
BAD_NAMES = re.compile(r"(?i)(?:^|/)(?:cookies|login data|history|local storage|application_list[^/]*\.json|"
                       r"saved_qa[^/]*\.json|pipeline_tracker[^/]*\.csv|transcript[^/]*|[^/]*profile[^/]*/)")


def publishable_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                         cwd=REPO, capture_output=True, text=True, check=True).stdout
    return [REPO / p for p in out.split("\0") if p]


def load_denylist(path: str | None) -> list[re.Pattern]:
    if not path:
        return []
    lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    return [re.compile(l.strip(), re.I) for l in lines if l.strip() and not l.lstrip().startswith("#")]


def scan(files: list[Path], private: list[re.Pattern]) -> list[str]:
    findings = []
    for f in files:
        rel = f.relative_to(REPO).as_posix()
        if BAD_NAMES.search(rel):
            findings.append(f"{rel}: file name looks like personal / browser data")
        if f.suffix.lower() == ".pdf" and not rel.startswith("examples/"):
            findings.append(f"{rel}: PDF outside examples/")
        if not f.is_file():
            continue
        if f.stat().st_size > MAX_BYTES:
            findings.append(f"{rel}: larger than {MAX_BYTES} bytes")
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            where = f"{rel}:{n}"
            if any(r.search(line) for r in SECRETS):
                findings.append(f"{where}: secret-like string")
            if HOME.search(line):
                findings.append(f"{where}: absolute home path: {HOME.search(line).group(0)}")
            for m in EMAIL.finditer(line):
                if not EMAIL_OK.search(m.group(1)):
                    findings.append(f"{where}: e-mail address: {m.group(0)}")
            for m in PHONE.finditer(line):
                if not PHONE_OK.search(m.group(0)):
                    findings.append(f"{where}: phone-like number: {m.group(0)}")
            for r in private:
                if r.search(line):
                    findings.append(f"{where}: private deny-list match ({r.pattern})")
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description="Scan publishable files for personal data and secrets.")
    ap.add_argument("--denylist", default=os.environ.get("LJP_PRIVATE_DENYLIST"),
                    help="private regex deny-list outside the repo (default: $LJP_PRIVATE_DENYLIST)")
    args = ap.parse_args()
    files = publishable_files()
    findings = scan(files, load_denylist(args.denylist))
    for line in findings:
        print(line)
    note = "" if args.denylist else " (generic rules only; no private deny-list given)"
    print(f"{len(files)} files scanned, {len(findings)} finding(s){note}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
