"""JD sectioning and retrieval-query construction. No model call.

The JD is data, never instructions. Company introductions, benefits, EEO / accommodation text and
application logistics are dropped so they cannot dominate retrieval. Separate, weighted queries
are built for responsibilities, required and preferred qualifications, skills named in the JD
(matched against the candidate's vocabulary), business context and the job title.
"""
from __future__ import annotations

import re

from .corpus import term_in

SECTION_RULES = [  # first match wins; tested on the lower-cased heading
    ("benefits", r"benefit|perks|what we offer|total rewards|compensation|pay (details|type|range)|salary|"
                 r"why join|why you'll love|wellness"),
    ("eeo", r"equal (opportunity|employment)|accommodat|diversity|inclusion|disabilit|eeo|accessib|privacy|"
            r"background check"),
    ("process", r"how to apply|interview process|next steps|application|hours|work location|location|schedule|"
                r"work environment|travel|start date|duration"),
    ("company", r"^about (us|the company|[a-z0-9&.,' ]+)$|who we are|our (purpose|story|mission|values|culture|company)|"
                r"the company|company overview"),
    ("preferred", r"prefer|nice to have|bonus|asset|desirable|plus|good to have|additional skills"),
    ("required", r"qualification|requirement|what you bring|who you are|what you('|’)ll need|"
                 r"what we('|’)re looking for|must[- ]have|minimum|skills|experience|education|competenc|you have|"
                 r"about you|ideal candidate|knowledge"),
    ("responsibilities", r"responsib|what you('|’)ll do|what you will do|what you('|’)ll be doing|duties|"
                         r"accountabilit|day[- ]to[- ]day|the role|role overview|key (activities|tasks)|you will|"
                         r"in this role|job description|position summary|summary|overview|the opportunity|"
                         r"description|the team|impact"),
]
DROP_SECTIONS = {"benefits", "eeo", "process", "company"}
DROP_SENTENCE = re.compile(
    r"equal opportunit|accommodation|disabilit|benefit|salary|compensation|pay range|\$\s?\d{2,3},?\d{3}|"
    r"apply (now|today|online)|vacation|pension|dental|bonus|perks|we thank all applicants|"
    r"only those selected|background check|hybrid work|in[- ]office|days a week", re.IGNORECASE)
REQ_UNIT = re.compile(r"\b(?:experience (?:with|in|using|building|working)|proficien\w*|knowledge of|familiar\w*|"
                      r"degree|bachelor\w*|master'?s|\d\+? ?years?|years of|certif\w*|must[- ]have|"
                      r"understanding of|expertise|skills? in|ability to|comfortable with|track record|"
                      r"hands[- ]on|experienced)\b", re.IGNORECASE)
RESP_UNIT = re.compile(r"^(develop|build|design|analy[sz]e|partner|support|create|manage|lead|own|deliver|"
                       r"collaborate|maintain|monitor|perform|prepare|conduct|work|drive|identify|translate|"
                       r"automate|report|implement|ensure|provide|assist|participate|communicate|extract|clean)\w*\b|"
                       r"\byou will\b|\bresponsible for\b", re.IGNORECASE)
INLINE_HEADINGS = [
    "Key Responsibilities", "Responsibilities", "What You Will Do", "What You'll Do", "What you'll do",
    "What you will do", "The Role", "About the Role", "About the role", "Job Description", "Duties",
    "Qualifications", "Required Qualifications", "Preferred Qualifications", "Minimum Qualifications",
    "Requirements", "Required Skills", "Must-have", "Must have", "Nice-to-have", "Nice to have",
    "What You Bring", "What you bring", "Who You Are", "Who you are", "Technical Skills", "Assets",
    "Preferred", "About Us", "About us", "Who We Are", "Benefits", "What We Offer", "What we offer",
    "Compensation", "Salary", "Equal Opportunity", "Accommodation",
]
_INLINE_RE = re.compile(r"(?:(?<=[.!?)\]])\s+|^\s*)(" + "|".join(re.escape(h) for h in
                        sorted(INLINE_HEADINGS, key=len, reverse=True)) + r")\s*(?::\s*|\s+(?=[A-Z]))")
_RUNON_SPLIT = re.compile(r"(?<=[a-z0-9)])\s+(?=(?:Design|Develop|Build|Translate|Perform|Ensure|Implement|Work|"
                          r"Support|Optimize|Create|Manage|Maintain|Analy[sz]e|Partner|Collaborate|Lead|Own|"
                          r"Deliver|Drive|Identify|Monitor|Prepare|Conduct|Provide|Strong|Experience|Knowledge|"
                          r"Proficiency|Familiarity|Ability|Bachelor|Master|Excellent|Understanding|Hands-on)\b)")


def classify_heading(text: str) -> str:
    t = text.strip().strip("*#:").strip().lower()
    for name, pat in SECTION_RULES:
        if re.search(pat, t):
            return name
    return "other"


def _is_heading(line: str) -> bool:
    s = line.strip().strip("*#").strip()
    if not s or len(s) > 70 or s.startswith(("-", "•", "*")):
        return False
    if len(s.split()) <= 6 and not s.endswith((".", ",", ";")) and classify_heading(s) != "other" \
            and not re.search(r"\d{3,}|\$", s):
        return True
    if s.endswith(":"):
        return len(s.split()) <= 9
    if s.endswith((".", ",", ";")):
        return False
    words = s.split()
    if len(words) > 8:
        return False
    caps = sum(1 for w in words if w[:1].isupper() or not w[:1].isalpha())
    return caps >= max(1, len(words) - 1)


def normalize_inline(jd: str) -> str:
    """A flattened JD (few, very long lines) gets line breaks before known inline headings."""
    lines = [l for l in (jd or "").splitlines() if l.strip()]
    if lines and len(lines) >= 6 and max(map(len, lines)) < 600:
        return jd
    return _INLINE_RE.sub(lambda m: "\n" + m.group(1) + ":\n", jd or "")


def classify_unit(u: str) -> str:
    if REQ_UNIT.search(u):
        return "requirement"
    if RESP_UNIT.search(u.strip()):
        return "responsibility"
    return "business"


def sections(jd: str) -> list[dict]:
    """[{'heading', 'kind', 'lines'}]; text before the first heading is 'intro'."""
    out = [{"heading": "", "kind": "intro", "lines": []}]
    for raw in normalize_inline(jd).splitlines():
        line = raw.strip()
        if not line:
            continue
        if _is_heading(line):
            kind = classify_heading(line)
            if kind == "other" and out[-1]["kind"] in ("responsibilities", "required", "preferred"):
                kind = out[-1]["kind"]  # sub-heading inside a responsibilities / requirements block
            out.append({"heading": line.strip("*#:").strip(), "kind": kind, "lines": []})
            continue
        out[-1]["lines"].append(line)
    return [s for s in out if s["lines"] or s["heading"]]


def _units(lines: list[str]) -> list[str]:
    units = []
    for line in lines:
        s = re.sub(r"^[\-•*·▪◦]+\s*", "", line).strip()
        if not s:
            continue
        if line.lstrip()[:1] in "-•*·▪◦" or len(s) < 220:
            parts = [s]
        else:
            parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+(?=[A-Z])", s) if p.strip()]
        for p in parts:
            units += [x.strip() for x in _RUNON_SPLIT.split(p)] if len(p) > 300 else [p]
    return [u for u in units if u]


def build_queries(jd: str, position: str, vocabulary: list[str], cfg: dict) -> dict:
    """-> {'queries': [{'qid', 'type', 'text', 'weight', 'section'}], 'dropped': [...], 'skills_in_jd': [...]}"""
    secs = sections(jd)
    queries, dropped, seen = [], [], set()

    def add(qtype, text, section):
        key = re.sub(r"\W+", " ", text.lower()).strip()
        if len(key) < 2 or key in seen or (qtype not in ("skill", "title") and len(key) < 8):
            return
        seen.add(key)
        queries.append({"qid": f"Q{len(queries) + 1:03d}", "type": qtype, "text": text[:400],
                        "weight": cfg["query_type_weights"][qtype], "section": section})

    for s in secs:
        kind = "unclassified" if s["kind"] in ("intro", "other") else s["kind"]
        if kind in DROP_SECTIONS:
            dropped.append({"heading": s["heading"], "kind": kind, "units": len(s["lines"])})
            continue
        for u in _units(s["lines"]):
            if DROP_SENTENCE.search(u):
                dropped.append({"heading": s["heading"], "kind": "sentence_filter", "text": u[:80]})
                continue
            qtype = {"responsibilities": "responsibility", "required": "requirement",
                     "preferred": "preferred"}.get(kind) or classify_unit(u)
            add(qtype, u, s["heading"] or "unsectioned")
    skills = [t for t in vocabulary if term_in(t, jd)]
    kept = []  # longest first: drop a skill contained in an already kept longer one
    for t in sorted(set(skills), key=lambda x: (-len(x), x.lower())):
        if not any(term_in(t, k) for k in kept):
            kept.append(t)
    for t in sorted(kept, key=str.lower)[: cfg["max_skill_queries"]]:
        add("skill", t, "skills_lexicon")
    add("title", position, "position")
    caps, by_type, final = cfg["max_queries_per_type"], {}, []
    for q in queries:
        n = by_type.get(q["type"], 0)
        if n < caps.get(q["type"], 999):
            by_type[q["type"]] = n + 1
            final.append(q)
    return {"queries": final, "dropped": dropped, "skills_in_jd": sorted(kept, key=str.lower),
            "section_kinds": [(s["heading"], s["kind"]) for s in secs]}
