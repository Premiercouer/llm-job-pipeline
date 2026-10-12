"""Number extraction and comparison for fact checks.

A quantity is (value, unit_class, unit_raw, object). `object` is the 1-3 words after the number
('120M claims records' -> 'claims records'). Checks compare value + unit class; percent and
percentage points, or GB and TB, are deliberately not interchangeable.
"""
from __future__ import annotations

import re

_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "mn": 1e6, "b": 1e9, "billion": 1e9, "bn": 1e9}
UNIT_CLASSES = {
    "%": "percent", "percent": "percent", "percentage": "percent",
    "percentage points": "pp", "pp": "pp", "points": "pp",
    "gb": "bytes_gb", "tb": "bytes_tb", "mb": "bytes_mb",
    "seconds": "seconds", "second": "seconds", "sec": "seconds",
    "x": "multiple", "$": "usd",
}
NUM_RE = re.compile(
    r"(?P<cur>\$)?\s?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<suf>(?:\s?%|[KMB](?![A-Za-z])|\s?(?:thousand|million|billion|bn|mn)\b|×|x(?![A-Za-z])|\+)*)",
    re.IGNORECASE,
)
_NEXT_WORD = re.compile(r"\s*([A-Za-z][A-Za-z\-]*)")
_UNIT_WORDS = ("percentage points", "percentage", "percent", "gb", "tb", "mb", "seconds", "second", "sec",
               "points", "pp")
_MONTHS = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_DATE_CTX = re.compile(rf"{_MONTHS}\.?\s*$", re.IGNORECASE)
_LABEL_CTX = re.compile(r"\b(phase|cluster|class|project|step|tier|version|#)\s*#?$", re.IGNORECASE)
_WORD_NUMS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
              "ten": 10, "eleven": 11, "twelve": 12}
_WORD_RE = re.compile(r"\b(" + "|".join(_WORD_NUMS) + r")\b(?:[\s-]+([a-z]+))?", re.IGNORECASE)


def _value(num: str, suffix: str) -> float:
    v = float(num.replace(",", ""))
    s = re.sub(r"[\s+%×]", "", suffix.lower())
    if s.endswith("x"):
        s = s[:-1]
    return v * _MULT.get(s, 1.0)


def extract(text: str, words: bool = True) -> list[dict]:
    out = _extract_digits(text or "")
    if words:
        for m in _WORD_RE.finditer(text or ""):
            out.append({"raw": m.group(0), "value": float(_WORD_NUMS[m.group(1).lower()]), "unit_raw": "",
                        "unit_class": "count", "object": (m.group(2) or "").lower(), "approx_marker": False,
                        "from_word": True})
    return out


def _extract_digits(text: str) -> list[dict]:
    out = []
    for m in NUM_RE.finditer(text):
        num, suf, cur = m.group("num"), m.group("suf") or "", m.group("cur") or ""
        start, end = m.start("num"), m.end()
        before = text[max(0, start - 12):start]
        raw_val = float(num.replace(",", ""))
        if "." not in num and "," not in num and 1900 <= raw_val <= 2100 and not suf.strip():
            continue  # calendar year
        if _DATE_CTX.search(before) and raw_val <= 31:
            continue  # "Sep 3"
        if start > 0 and (text[start - 1].isalpha() or text[start - 1] in "-_"):
            if not (start > 1 and text[start - 1] == "-" and text[start - 2].isdigit()):
                continue  # identifier such as FAB-B01 or ABC6010
        if _LABEL_CTX.search(before):
            continue  # ordinal label: "Phase 1", "Project 2"
        rest = text[end:end + 40]
        sl = suf.strip().lower()
        unit_raw = "%" if "%" in sl else ("x" if "×" in sl or sl.endswith("x") else cur)
        low_rest = rest.lower().lstrip()
        for w in _UNIT_WORDS:
            if low_rest.startswith(w) and (len(low_rest) == len(w) or not low_rest[len(w)].isalpha()):
                if not unit_raw or (unit_raw == "%" and w.startswith("percentage points")):
                    unit_raw = w
                rest = low_rest[len(w):]
                break
        obj, pos = [], 0
        for _ in range(3):
            wm = _NEXT_WORD.match(rest, pos)
            if not wm:
                break
            obj.append(wm.group(1).lower())
            pos = wm.end()
        out.append({"raw": text[m.start():m.end()].strip(), "value": _value(num, suf), "unit_raw": unit_raw,
                    "unit_class": UNIT_CLASSES.get(unit_raw, "count" if not unit_raw else unit_raw),
                    "object": " ".join(obj), "approx_marker": "+" in suf})
    return out


def same_value(a: float, b: float, rel: float = 0.005) -> bool:
    return a == b or abs(a - b) <= rel * max(abs(a), abs(b))


def compatible_units(a: str, b: str) -> bool:
    return a == b
