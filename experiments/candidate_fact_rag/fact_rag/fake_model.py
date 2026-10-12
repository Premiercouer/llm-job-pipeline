"""Deterministic stand-in for the generator. It exercises the mechanism, nothing else.

`generate` builds a contract-valid, cited output from the facts in an evidence bundle by copying
fact text verbatim (so its claims are trivially supported). `inject_errors` then plants known
defects of the kinds the checks are meant to catch. Neither says anything about how a real model
writes, which facts it would choose, or how often it makes mistakes.
"""
from __future__ import annotations

import copy
import re

from .corpus import facts_by_id, term_in

EVIDENCE_TYPES_FOR_PROJECT = ("achievement", "quantified_result", "deliverable", "finding", "project_overview")


def _rank(snapshot: dict, retrieval: dict | None) -> dict:
    """Fact score used to order choices: retrieval scores when available, corpus order otherwise."""
    if retrieval:
        return {c["fact_id"]: c["agg"] for c in retrieval["candidates"]}
    n = len(snapshot["facts"])
    return {f["fact_id"]: (n - i) / n for i, f in enumerate(snapshot["facts"])}


def _first_clause(text: str) -> str:
    return re.split(r",\s(?=[a-z])", text, maxsplit=1)[0].rstrip(".") + "."


def generate(snapshot: dict, bundle: dict, job: dict, lexicon: dict, retrieval: dict | None = None) -> dict:
    facts = facts_by_id(snapshot)
    provided = [facts[i] for i in bundle["provided_fact_ids"] if i in facts]
    score = _rank(snapshot, retrieval)
    by_score = lambda fs: sorted(fs, key=lambda f: -score.get(f["fact_id"], 0.0))  # noqa: E731
    exp_ids = [e["entity_id"] for e in snapshot["entities"] if e["entity_type"] == "work_experience"]

    experience = {}
    for eid in exp_ids:
        bullets = by_score([f for f in provided if f["entity_id"] == eid and f["fact_type"] == "canonical_bullet"])[:3]
        experience[eid] = [{"text": f["text"], "fact_ids": [f["fact_id"]]} for f in bullets]

    projects = [p["entity_id"] for p in (retrieval or {}).get("project_ranking", [])] or \
        [e["entity_id"] for e in snapshot["entities"] if e["entity_type"] == "project"]
    provided_ids = {f["fact_id"] for f in provided}
    pid, pfact = None, None
    for p in projects:
        cand = by_score([f for f in provided if f["entity_id"] == p and f["fact_type"] in EVIDENCE_TYPES_FOR_PROJECT])
        if cand and f"{p}-HDR" in provided_ids and snapshot_status(snapshot, p) != "framework_design_only":
            pid, pfact = p, cand[0]
            break
    stack = facts.get(f"{pid}-TECH")
    tools = [t for t in lexicon["tools"] if stack and term_in(t, stack["text"])][:4] if pid else []

    cited = [b for bs in experience.values() for b in bs] + ([{"fact_ids": [pfact["fact_id"]]}] if pfact else [])
    cited_facts = [facts[i] for c in cited for i in c["fact_ids"]]
    technical = []
    for t in lexicon["tools"]:
        hits = [f["fact_id"] for f in cited_facts if term_in(t, f["text"])]
        if hits and len(technical) < 8:
            technical.append({"name": t, "fact_ids": hits[:2]})
    soft = next((f for f in snapshot["facts"] if f["fact_type"] == "skill_vocab_core"
                 and "professional" in f["category"].lower()), None)
    professional = []
    for term in (soft["terms"] if soft else [])[:3]:
        hits = [f["fact_id"] for f in provided if f["is_evidence"] and term_in(term, f["text"])]
        professional.append({"name": term, "fact_ids": hits[:1] or [soft["fact_id"]]})

    top = by_score([f for f in provided if f["fact_type"] == "canonical_bullet"])
    names = [t["name"] for t in technical[:3]]
    summary = [{"text": f"Analyst with hands-on {', '.join(names[:-1])} and {names[-1]} work.",
                "fact_ids": sorted({i for t in technical[:3] for i in t["fact_ids"]})},
               {"text": _first_clause(top[0]["text"]), "fact_ids": [top[0]["fact_id"]]}]
    quals = [{"text": _first_clause(f["text"]), "fact_ids": [f["fact_id"]]} for f in top[1:4]]
    return {"job_title": job["position"].upper(), "summary": summary, "qualifications": quals,
            "skills": {"technical": technical, "professional": professional or [{"name": "Communication",
                                                                                 "fact_ids": []}]},
            "experience": experience,
            "project": {"entity_id": pid, "tools": ", ".join(tools),
                        "bullet": {"text": pfact["text"].replace("\n", " "), "fact_ids": [pfact["fact_id"]]}}}


def snapshot_status(snapshot: dict, eid: str) -> str:
    return next(e["implementation_status"] for e in snapshot["entities"] if e["entity_id"] == eid)


def inject_errors(obj: dict, snapshot: dict, bundle: dict) -> tuple[dict, list[dict]]:
    """Plant one defect of each kind; returns (flawed output, what was planted and where)."""
    out, planted = copy.deepcopy(obj), []
    exp_ids = list(out["experience"])

    # 1. a changed number (120M -> 180M): nowhere in the corpus
    b = out["experience"][exp_ids[0]][0]
    m = re.search(r"\d+", b["text"])
    if m:
        new = str(int(m.group()) * 3 // 2)
        b["text"] = b["text"][:m.start()] + new + b["text"][m.end():]
        planted.append({"claim_id": f"{exp_ids[0]}-1", "defect": f"number {m.group()} changed to {new}"})
    # 2. a tool the candidate never used
    out["qualifications"][0]["text"] = out["qualifications"][0]["text"].rstrip(".") + " in Snowflake."
    planted.append({"claim_id": "QUAL1", "defect": "added a tool that is not in the corpus (Snowflake)"})
    # 3. an experience bullet citing another employer's fact
    other = next(f for f in snapshot["facts"] if f["entity_id"] == exp_ids[1] and f["fact_type"] == "canonical_bullet")
    out["experience"][exp_ids[0]][1]["fact_ids"] = [other["fact_id"]]
    planted.append({"claim_id": f"{exp_ids[0]}-2", "defect": f"cites {other['fact_id']} from another experience"})
    # 4. a citation to a fact the context did not contain (or to an unknown ID in full mode)
    missing = next((f["fact_id"] for f in snapshot["facts"] if f["is_evidence"]
                    and f["fact_id"] not in set(bundle["provided_fact_ids"])), None)
    out["qualifications"][1]["fact_ids"] = [missing or f"{exp_ids[0][4:]}-B99"]
    planted.append({"claim_id": "QUAL2", "defect": f"cites {missing or 'an unknown ID'} which was not in the context"})
    # 5. a framework-only project described as deployed
    fw = next((e for e in snapshot["entities"] if e["implementation_status"] == "framework_design_only"), None)
    if fw:
        fid = next(f["fact_id"] for f in snapshot["facts"] if f["entity_id"] == fw["entity_id"]
                   and f["fact_type"] == "achievement")
        out["qualifications"][2] = {"text": f"Deployed the {fw['entity_name'].split(' — ')[0]} model to production.",
                                    "fact_ids": [fid]}
        planted.append({"claim_id": "QUAL3", "defect": f"{fw['entity_id']} is framework-only but described as deployed"})
    return out, planted
