"""Canonical candidate facts: parse a candidate folder into complete, citable facts.

Input is the same candidate layout the pipeline uses (examples/candidate/): candidate.json plus
the bullet banks, project library and skills file it names under knowledge/.

Units are complete facts, never fixed-size chunks:
  canonical_bullet   one work-experience bullet, keeping its existing ID (FAB-B01 ...)
  project_header     project name, timeline and type            -> PRJ02-HDR
  tech_stack         a project's whole technical-stack section   -> PRJ02-TECH
  achievement, ...   one list item / table / paragraph per fact  -> PRJ02-ACH01, PRJ02-QR ...
  keyword_tags       a project's keyword line                    (is_evidence = False)
  skill_vocab_*      one skills-file category                    (is_evidence = False)

Keyword lines and skill vocabulary are kept so a generator knows the allowed wording, but they
are marked is_evidence = False: they never prove that the candidate used a skill.

Every fact records its source (file, section, line range) and a content hash; the snapshot hash
covers facts, entities, the limitations overlay and the source-file hashes, so any change to the
candidate files produces a new corpus version that old citations can be checked against.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import quant

SCHEMA_VERSION = 1
SKILLS_ENTITY = {"entity_id": "SKILLS", "entity_name": "Skills vocabulary", "entity_type": "skill_vocabulary"}

# '#### Section title' -> (code, fact_type); first match wins
SECTION_CODES = [
    (r"^project overview", "OVR", "project_overview"),
    (r"^technical stack", "TECH", "tech_stack"),
    (r"^key achievements", "ACH", "achievement"),
    (r"^key deliverables", "DEL", "deliverable"),
    (r"^quantifiable results", "QR", "quantified_result"),
    (r"^(key )?results", "RES", "quantified_result"),
    (r"^model performance", "PERF", "quantified_result"),
    (r"^(key |analysis )?findings", "FND", "finding"),
    (r"^project context", "CTX", "project_context"),
    (r"^proposed (analytical )?framework", "PAF", "proposed_design"),
    (r"^team role", "TEAM", "team_role"),
    (r"^keywords", "KW", "keyword_tags"),
]
NON_EVIDENCE_TYPES = {"keyword_tags", "skill_vocab_core", "skill_vocab_ats"}


class CorpusError(ValueError):
    pass


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_hash(obj) -> str:
    return sha256_text(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def _clean(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text.replace(" ", " ")).strip()


def _strip_md(text: str) -> str:
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    return _clean(t)


def _terms(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


def term_in(term: str, text: str) -> bool:
    """Whole-term, case-insensitive match; spaces in the term also match hyphens."""
    t = re.escape(term.lower()).replace(r"\ ", r"[\s\-]+")
    return re.search(rf"(?<![a-z0-9]){t}(?![a-z0-9])", text.lower()) is not None


def _section_code(title: str, used: dict) -> tuple[str, str]:
    t = title.strip().lower()
    for pat, code, ftype in SECTION_CODES:
        if re.match(pat, t):
            break
    else:
        words = re.findall(r"[a-z]+", t)
        code, ftype = ("".join(w[0] for w in words[:4]).upper() or "SEC"), "project_misc"
    used[code] = used.get(code, 0) + 1
    return (code if used[code] == 1 else f"{code}{used[code]}"), ftype


# ------------------------------------------------------------------ parsers

def parse_bullet_bank(path: Path, entity: dict) -> list[dict]:
    """'## FAB-B01' + '**Bullet:** ...' + '**Keywords:** ...' -> one canonical_bullet per ID."""
    facts, cur = [], None
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = re.match(r"^##\s+([A-Z][A-Z0-9]*-B\d+)\s*$", line)
        if m:
            cur = {"fact_id": m.group(1), "id_origin": "existing", **entity, "fact_type": "canonical_bullet",
                   "text": "", "keyword_tags": [],
                   "source": {"file": path.name, "section": m.group(1), "line_start": i, "line_end": i}}
            facts.append(cur)
            continue
        if cur is None:
            continue
        b = re.match(r"^\*\*Bullet:\*\*\s*(.+)$", line)
        k = re.match(r"^\*\*Keywords:\*\*\s*(.+)$", line)
        if b:
            cur["text"] = _clean(b.group(1))
            cur["source"]["line_end"] = i
        elif k:
            cur["keyword_tags"] = _terms(k.group(1))
            cur["source"]["line_end"] = i
    if not facts:
        raise CorpusError(f"{path.name}: no '## <PREFIX>-Bnn' bullets found")
    for f in facts:
        if not f["text"]:
            raise CorpusError(f"{path.name}: {f['fact_id']} has no **Bullet:** text")
    return facts


def _blocks(lines: list[tuple[int, str]]):
    """Split a section body into complete units: a table, a list item (with nested items) or a paragraph."""
    blocks, cur, kind, sub = [], [], None, ""

    def flush():
        nonlocal cur, kind
        if cur:
            blocks.append((kind, sub, cur))
        cur, kind = [], None

    for ln, raw in lines:
        s = raw.rstrip()
        st = s.strip()
        if not st or st == "---":
            flush()
            continue
        label = re.fullmatch(r"\*\*([^*]+?):?\*\*:?", st)
        if label:  # bold sub-heading on its own line
            flush()
            sub = label.group(1).strip().rstrip(":")
            continue
        if st.startswith("|"):
            if kind != "table":
                flush()
                kind = "table"
            cur.append((ln, st))
            continue
        item = re.match(r"^(\s*)(?:[-*]\s+|\d+\.\s+)", s)
        if item and not item.group(1):
            flush()
            kind = "item"
            cur.append((ln, st))
            continue
        if item and kind == "item":  # nested item stays in the same unit
            cur.append((ln, st))
            continue
        if kind == "table":
            flush()
        kind = kind or "para"
        cur.append((ln, st))
    flush()
    return blocks


def parse_projects(path: Path, overlay: dict) -> tuple[list[dict], list[dict]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    starts = [(i, m) for i, l in enumerate(lines) if (m := re.match(r"^### Project (\d+):\s*(.+?)\s*$", l))]
    entities, facts = [], []
    for idx, (start, m) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        num, name = int(m.group(1)), _strip_md(m.group(2))
        eid = f"PRJ{num:02d}"
        body = lines[start + 1:end]
        meta, j = {}, 0
        while j < len(body) and not body[j].startswith("####"):
            mm = re.match(r"^\*\*(.+?)\*\*:\s*(.+?)\s*$", body[j].strip())
            if mm:
                meta[mm.group(1).strip()] = _strip_md(mm.group(2))
            j += 1
        ptype = meta.get("Project Type") or meta.get("Type", "")
        ov = overlay.get("entities", {}).get(eid)
        if ov is None:
            ov = overlay["default_course_project"] if ptype.lower().startswith("course") else overlay["default_project"]
        entity = {"entity_id": eid, "entity_name": name, "entity_type": "project",
                  "timeline": meta.get("Timeline", ""), "project_type": ptype,
                  "implementation_status": ov["implementation_status"], "limitations": ov.get("limitations", [])}
        entities.append(entity)
        base = {"entity_id": eid, "entity_name": name, "entity_type": "project"}
        header = " | ".join([name, f"Timeline: {entity['timeline']}"] + ([f"Type: {ptype}"] if ptype else []))
        facts.append({"fact_id": f"{eid}-HDR", "id_origin": "derived", **base, "fact_type": "project_header",
                      "text": header, "keyword_tags": [],
                      "source": {"file": path.name, "section": f"Project {num}", "line_start": start + 1,
                                 "line_end": start + 1 + j}})
        sections, title, sl = [], None, []
        for k in range(j, len(body)):
            h = re.match(r"^####\s+(.+?)\s*$", body[k])
            if h:
                if title is not None:
                    sections.append((title, sl))
                title, sl = h.group(1), []
            elif title is not None:
                sl.append((start + 2 + k, body[k]))
        if title is not None:
            sections.append((title, sl))
        used: dict = {}
        for title, sl in sections:
            code, ftype = _section_code(title, used)
            src = {"file": path.name, "section": f"Project {num} / {title}",
                   "line_start": sl[0][0] if sl else None, "line_end": sl[-1][0] if sl else None}
            if ftype == "keyword_tags":
                tags = re.findall(r"`([^`]+)`", " ".join(l for _, l in sl))
                facts.append({"fact_id": f"{eid}-{code}", "id_origin": "derived", **base, "fact_type": ftype,
                              "text": ", ".join(tags), "keyword_tags": tags, "source": src})
                continue
            blocks = _blocks(sl)
            if ftype == "tech_stack" and blocks:  # the whole stack is one fact
                blocks = [("section", "", [x for b in blocks for x in b[2]])]
            for bi, (kind, sub, bl) in enumerate(blocks, 1):
                text = "\n".join(t if kind == "table" else _strip_md(re.sub(r"^(?:[-*]\s+|\d+\.\s+)", "", t))
                                 for _, t in bl)
                if sub:
                    text = f"{sub}: {text}"
                facts.append({"fact_id": f"{eid}-{code}{bi:02d}" if len(blocks) > 1 else f"{eid}-{code}",
                              "id_origin": "derived", **base, "fact_type": ftype, "text": text.strip(),
                              "keyword_tags": [],
                              "source": {"file": path.name, "section": f"Project {num} / {title}",
                                         "line_start": bl[0][0], "line_end": bl[-1][0]}})
    if not entities:
        raise CorpusError(f"{path.name}: no '### Project N: name' sections found")
    return entities, facts


def _slug(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", text.upper()).strip("-")[:32]


def parse_skills(path: Path) -> list[dict]:
    """'## Core ...' with '### Category' + term lines, and '## ATS ...' with either '### Category'
    blocks or '- **Category**: terms' items. Prose lines in the ATS part are ignored."""
    facts, part, used = [], None, set()

    def add(kind, category, text, i):
        fid = f"SKL-{kind}-{_slug(category)}"
        if fid in used:
            raise CorpusError(f"{path.name}: duplicate skill category {category!r}")
        used.add(fid)
        facts.append({"fact_id": fid, "id_origin": "derived", **SKILLS_ENTITY,
                      "fact_type": "skill_vocab_core" if kind == "CORE" else "skill_vocab_ats",
                      "category": category, "text": text,
                      "keyword_tags": [], "source": {"file": path.name, "section": f"{kind} / {category}",
                                                     "line_start": i, "line_end": i}})

    open_block = False
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("## "):
            part = "CORE" if "core" in line.lower() else ("ATS" if "ats" in line.lower() else None)
            open_block = False
            continue
        if not part:
            continue
        h = re.match(r"^###\s+(.+?)\s*$", line)
        item = re.match(r"^\s*[-*]\s+\*\*(.+?)\*\*:\s*(.+?)\s*$", line)
        if h:
            add(part, h.group(1).strip(), "", i)
            open_block = True
        elif item:
            add(part, item.group(1).strip(), _clean(item.group(2)), i)
            open_block = False
        elif open_block and line.strip():
            facts[-1]["text"] = (facts[-1]["text"] + " " + line.strip()).strip()
            facts[-1]["source"]["line_end"] = i
    for f in facts:
        f["terms"] = _terms(f["text"])
    return facts


# ------------------------------------------------------------------ build

def core_vocabulary(facts: list[dict]) -> list[str]:
    return [t for f in facts if f["fact_type"] == "skill_vocab_core" for t in f["terms"]]


def ats_vocabulary(facts: list[dict]) -> list[str]:
    return [t for f in facts if f["fact_type"] == "skill_vocab_ats" for t in f["terms"]]


def _bank_prefix(path: Path) -> str:
    m = re.search(r"^##\s+([A-Z][A-Z0-9]*)-B\d+\s*$", path.read_text(encoding="utf-8"), re.M)
    if not m:
        raise CorpusError(f"{path.name}: no '## <PREFIX>-Bnn' bullets found")
    return m.group(1)


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build(candidate_dir: Path, overlay_path: Path, lexicon_path: Path) -> dict:
    """Parse one candidate folder into a corpus snapshot (a plain dict, JSON-serializable)."""
    candidate_dir = Path(candidate_dir)
    cand = load_json(candidate_dir / "candidate.json")
    kdir = candidate_dir / "knowledge"
    overlay, lexicon = load_json(overlay_path), load_json(lexicon_path)
    facts, entities, used_files = [], [], []
    for exp in cand["experience"]:
        bank = kdir / exp["bullet_bank"]
        entity = {"entity_id": f"EXP-{_bank_prefix(bank)}", "entity_name": exp["company"],
                  "entity_type": "work_experience"}
        ov = overlay.get("entities", {}).get(entity["entity_id"], overlay["default_work_experience"])
        entities.append({**entity, "role": exp["role"], "date": exp["date"],
                         "implementation_status": ov["implementation_status"],
                         "limitations": ov.get("limitations", [])})
        facts += parse_bullet_bank(bank, entity)
        used_files.append(bank)
    lib = kdir / cand["knowledge"]["project_library"]
    prj_entities, prj_facts = parse_projects(lib, overlay)
    entities += prj_entities
    facts += prj_facts
    skills = kdir / cand["knowledge"]["skills"]
    facts += parse_skills(skills)
    entities.append({**SKILLS_ENTITY, "implementation_status": "vocabulary_not_evidence", "limitations": []})
    used_files += [lib, skills]

    ent_by_id = {e["entity_id"]: e for e in entities}
    vocab = sorted(set(core_vocabulary(facts)) | set(ats_vocabulary(facts)) | set(lexicon["tools"]),
                   key=lambda s: (-len(s), s.lower()))
    seen = set()
    for f in facts:
        if f["fact_id"] in seen:
            raise CorpusError(f"duplicate fact_id {f['fact_id']}")
        seen.add(f["fact_id"])
        ent = ent_by_id[f["entity_id"]]
        f["is_evidence"] = f["fact_type"] not in NON_EVIDENCE_TYPES
        f["implementation_status"] = ent["implementation_status"]
        f["limitations"] = [{"code": l["code"], "text": l["text"]} for l in ent.get("limitations", [])]
        f["numbers"] = quant.extract(f["text"]) if f["is_evidence"] else []
        f["skills"] = [t for t in vocab if f["is_evidence"] and term_in(t, f["text"])]
        f["content_hash"] = sha256_text(f["text"])
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "candidate_name": cand["name"],
        "source_files": {p.name: sha256_text(p.read_text(encoding="utf-8")) for p in used_files},
        "overlay_hash": canonical_hash(overlay),
        "lexicon_hash": canonical_hash(lexicon),
        "entities": entities,
        "facts": facts,
    }
    snapshot["corpus_hash"] = corpus_hash(snapshot)
    return snapshot


def corpus_hash(snapshot: dict) -> str:
    return canonical_hash({k: snapshot[k] for k in ("schema_version", "source_files", "overlay_hash",
                                                    "lexicon_hash", "entities", "facts")})


def verify_snapshot(snapshot: dict) -> None:
    if corpus_hash(snapshot) != snapshot["corpus_hash"]:
        raise CorpusError("corpus snapshot content does not match its recorded hash")


def manifest(snapshot: dict) -> dict:
    by_type: dict = {}
    for f in snapshot["facts"]:
        by_type[f["fact_type"]] = by_type.get(f["fact_type"], 0) + 1
    return {"corpus_hash": snapshot["corpus_hash"], "facts": len(snapshot["facts"]),
            "evidence_facts": sum(f["is_evidence"] for f in snapshot["facts"]),
            "entities": [e["entity_id"] for e in snapshot["entities"]], "by_fact_type": by_type,
            "existing_ids": sum(f["id_origin"] == "existing" for f in snapshot["facts"])}


def facts_by_id(snapshot: dict) -> dict:
    return {f["fact_id"]: f for f in snapshot["facts"]}


def entities_by_id(snapshot: dict) -> dict:
    return {e["entity_id"]: e for e in snapshot["entities"]}


def experience_ids(snapshot: dict) -> list[str]:
    return [e["entity_id"] for e in snapshot["entities"] if e["entity_type"] == "work_experience"]
