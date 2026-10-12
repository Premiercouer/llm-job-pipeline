"""Structured, cited Resume V1 output: schema check, plain-text rendering, provenance sidecar.

The generator returns content only. Every item carries the fact_ids it relies on:

    {"job_title": "...",
     "summary":        [{"text": "...", "fact_ids": [...]}],          2-3
     "qualifications": [{"text": "...", "fact_ids": [...]}],          3-4
     "skills": {"technical": [{"name": "...", "fact_ids": [...]}], "professional": [...]},
     "experience": {"EXP-FAB": [{"text": "...", "fact_ids": [...]}] x 3, ...},   one key per experience
     "project": {"entity_id": "PRJnn", "tools": "...", "bullet": {"text": "...", "fact_ids": [...]}}}

Identity (header, role / company / date lines, education) is never generated: Python fills it in
from candidate.json, so the model cannot alter it. The rendered text follows the layout of
resume/instructions/resume_v1.md; citations never appear in it and are kept in a sidecar that
records, for every cited fact, its source file, line range and content hash.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .corpus import facts_by_id


class ContractError(ValueError):
    pass


def _item(x, where: str, key: str = "text") -> None:
    if not isinstance(x, dict) or set(x) != {key, "fact_ids"}:
        raise ContractError(f"{where}: must be an object with exactly '{key}' and 'fact_ids'")
    if not isinstance(x[key], str) or not x[key].strip():
        raise ContractError(f"{where}.{key}: non-empty string required")
    if not isinstance(x["fact_ids"], list) or not all(isinstance(i, str) and i.strip() for i in x["fact_ids"]):
        raise ContractError(f"{where}.fact_ids: list of strings required")
    if re.search(r"<\s*/?\s*(html|div|span|b|p|br)\b", x[key], re.I):
        raise ContractError(f"{where}: HTML in text")


def validate(obj, experience_ids: list[str]) -> None:
    """Shape and counts only. Factual checks live in verify.py and never trigger a retry."""
    if not isinstance(obj, dict):
        raise ContractError("root must be an object")
    keys = {"job_title", "summary", "qualifications", "skills", "experience", "project"}
    if set(obj) != keys:
        raise ContractError(f"keys must be exactly {sorted(keys)}, got {sorted(obj)}")
    if not isinstance(obj["job_title"], str) or not obj["job_title"].strip():
        raise ContractError("job_title: non-empty string required")
    for name, lo, hi in (("summary", 2, 3), ("qualifications", 3, 4)):
        if not isinstance(obj[name], list) or not lo <= len(obj[name]) <= hi:
            raise ContractError(f"{name}: {lo}-{hi} items required")
        for i, x in enumerate(obj[name]):
            _item(x, f"{name}[{i}]")
    sk = obj["skills"]
    if not isinstance(sk, dict) or set(sk) != {"technical", "professional"}:
        raise ContractError("skills: object with technical / professional required")
    for line in ("technical", "professional"):
        if not isinstance(sk[line], list) or not sk[line]:
            raise ContractError(f"skills.{line}: non-empty list required")
        for i, x in enumerate(sk[line]):
            _item(x, f"skills.{line}[{i}]", key="name")
    ex = obj["experience"]
    if not isinstance(ex, dict) or set(ex) != set(experience_ids):
        raise ContractError(f"experience: keys {experience_ids} required")
    for eid in experience_ids:
        if not isinstance(ex[eid], list) or len(ex[eid]) != 3:
            raise ContractError(f"experience.{eid}: exactly 3 bullets required")
        for i, x in enumerate(ex[eid]):
            _item(x, f"experience.{eid}[{i}]")
    pr = obj["project"]
    if not isinstance(pr, dict) or set(pr) != {"entity_id", "tools", "bullet"}:
        raise ContractError("project: keys entity_id, tools, bullet required")
    if not isinstance(pr["entity_id"], str) or not re.fullmatch(r"PRJ\d{2}", pr["entity_id"]):
        raise ContractError("project.entity_id: PRJnn required")
    if not isinstance(pr["tools"], str):
        raise ContractError("project.tools: string required")
    _item(pr["bullet"], "project.bullet")


def parse_output(text: str, experience_ids: list[str]) -> dict:
    """Model text -> validated object. A code fence is tolerated; anything else around the JSON is not."""
    t = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", t, re.S)
    try:
        obj = json.loads(fenced.group(1) if fenced else t)
    except json.JSONDecodeError as e:
        raise ContractError(f"not a JSON object: {e}") from e
    validate(obj, experience_ids)
    return obj


def load_identity(candidate_dir: Path) -> dict:
    c = json.loads((Path(candidate_dir) / "candidate.json").read_text(encoding="utf-8"))
    edu = []
    for e in c["education"]:
        edu.append(f"{e['degree']}, {e['school']}" + (f" ({e['details']})" if e["details"] else "") + f" | {e['date']}")
        if e["coursework"]:
            edu.append(f"Relevant coursework: {e['coursework']}")
    return {"header": [c["name"], " | ".join(x["text"] for x in c["contact"])],
            "experience_lines": [f"{e['role']}, {e['company']} | {e['date']}" for e in c["experience"]],
            "education_lines": edu}


def render(obj: dict, identity: dict, snapshot: dict) -> str:
    ents = {e["entity_id"]: e for e in snapshot["entities"]}
    ent = ents.get(obj["project"]["entity_id"])
    if ent is None or ent["entity_type"] != "project":
        raise ContractError(f"project.entity_id {obj['project']['entity_id']} is not a project in the corpus")
    exp_ids = [e["entity_id"] for e in snapshot["entities"] if e["entity_type"] == "work_experience"]
    out = list(identity["header"]) + ["", obj["job_title"].strip().upper(),
                                      " ".join(s["text"].strip() for s in obj["summary"]), "",
                                      "SUMMARY OF QUALIFICATIONS"]
    out += [f"• {q['text'].strip()}" for q in obj["qualifications"]]
    out += ["", "CORE SKILLS",
            "Technical: " + ", ".join(s["name"].strip() for s in obj["skills"]["technical"]),
            "Professional: " + ", ".join(s["name"].strip() for s in obj["skills"]["professional"]),
            "", "PROFESSIONAL EXPERIENCE"]
    for i, (eid, line) in enumerate(zip(exp_ids, identity["experience_lines"])):
        out += ([""] if i else []) + [line] + [f"• {b['text'].strip()}" for b in obj["experience"][eid]]
    tools = obj["project"]["tools"].strip()
    name = re.sub(r"\s+[-—]\s+", " – ", ent["entity_name"])
    out += ["", "PROJECT EXPERIENCE", f"{name}{' – ' + tools if tools else ''} | {ent.get('timeline', '')}",
            f"• {obj['project']['bullet']['text'].strip()}", "", "EDUCATION"] + identity["education_lines"]
    return "\n".join(out) + "\n"


def claims(obj: dict) -> list[dict]:
    """Flatten the output into checkable claims, each with its section, entity and citations."""
    out = [{"claim_id": f"SUM{i}", "section": "summary", "entity_id": None, "text": s["text"], "cited": s["fact_ids"]}
           for i, s in enumerate(obj["summary"], 1)]
    out += [{"claim_id": f"QUAL{i}", "section": "qualification", "entity_id": None, "text": s["text"],
             "cited": s["fact_ids"]} for i, s in enumerate(obj["qualifications"], 1)]
    for line in ("technical", "professional"):
        out += [{"claim_id": f"SK{line[0].upper()}{i}", "section": f"skill_{line}", "entity_id": None,
                 "text": s["name"], "cited": s["fact_ids"]} for i, s in enumerate(obj["skills"][line], 1)]
    for eid, bullets in obj["experience"].items():
        out += [{"claim_id": f"{eid}-{i}", "section": "experience", "entity_id": eid, "text": b["text"],
                 "cited": b["fact_ids"]} for i, b in enumerate(bullets, 1)]
    pr = obj["project"]
    out.append({"claim_id": "PROJECT", "section": "project", "entity_id": pr["entity_id"],
                "text": pr["bullet"]["text"], "cited": pr["bullet"]["fact_ids"]})
    if pr["tools"].strip():
        out.append({"claim_id": "PROJECT-TOOLS", "section": "project_tools", "entity_id": pr["entity_id"],
                    "text": pr["tools"], "cited": pr["bullet"]["fact_ids"]})
    return out


def provenance(obj: dict, snapshot: dict, bundle: dict) -> dict:
    """Sidecar: every claim with the source of each fact it cites (unknown IDs are kept as such)."""
    facts = facts_by_id(snapshot)
    rows = []
    for c in claims(obj):
        sources = []
        for fid in c["cited"]:
            f = facts.get(fid)
            sources.append({"fact_id": fid, "known": f is not None} if f is None else
                           {"fact_id": fid, "known": True, "entity_id": f["entity_id"], "fact_type": f["fact_type"],
                            "is_evidence": f["is_evidence"], "file": f["source"]["file"],
                            "lines": [f["source"]["line_start"], f["source"]["line_end"]],
                            "content_hash": f["content_hash"][:16]})
        rows.append({**{k: c[k] for k in ("claim_id", "section", "entity_id", "text")}, "fact_ids": c["cited"],
                     "sources": sources})
    return {"corpus_hash": snapshot["corpus_hash"], "context_mode": bundle["mode"],
            "context_hash": bundle["context_hash"], "retrieval_config_hash": bundle.get("retrieval_config_hash"),
            "claims": rows}
