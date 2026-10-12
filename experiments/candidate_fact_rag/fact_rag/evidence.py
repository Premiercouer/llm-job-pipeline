"""Render facts into the <candidate_evidence> block for either context mode.

  full  every fact of the corpus (the full-text arm, "A")
  rag   only the facts retrieval selected (the retrieval arm, "B")

Both modes use the same renderer and the same corpus order, so the only difference between the
two prompts is which facts appear. The returned bundle records exactly which fact IDs were
provided, which the citation checks later compare against.
"""
from __future__ import annotations

import math

from .corpus import sha256_text

CHARS_PER_TOKEN = 3.8  # budgeting approximation only; real counts come from the model's usage report


def approx_tokens(text: str) -> int:
    return int(math.ceil(len(text) / CHARS_PER_TOKEN))


def fact_line(f: dict, terms_override: list[str] | None = None) -> str:
    fid = f["fact_id"]
    if f["fact_type"] in ("skill_vocab_core", "skill_vocab_ats"):
        kind = "core skill vocabulary" if f["fact_type"] == "skill_vocab_core" else "ATS expansion vocabulary"
        terms = f["terms"] if terms_override is None else terms_override
        return f"[{fid}] ({kind}, NOT experience evidence) {f['category']}: {', '.join(terms)}"
    if f["fact_type"] == "keyword_tags":
        return f"[{fid}] (keyword tags, NOT experience evidence) {f['text']}"
    line = f"[{fid}] ({f['fact_type']}) " + f["text"].replace("\n", "\n    ")
    if f.get("keyword_tags") and f["fact_type"] == "canonical_bullet":
        line += f"\n    tags (NOT evidence): {', '.join(f['keyword_tags'])}"
    return line


def fact_cost(f: dict) -> int:
    return approx_tokens(fact_line(f)) + 1


def render(snapshot: dict, fact_ids: list[str] | None = None, vocab_terms: dict | None = None) -> str:
    """fact_ids None = full corpus. vocab_terms {fact_id: [terms]} narrows vocabulary lines (rag)."""
    wanted = None if fact_ids is None else set(fact_ids)
    ents = {e["entity_id"]: e for e in snapshot["entities"]}
    out, cur = [], None
    for f in snapshot["facts"]:
        if wanted is not None and f["fact_id"] not in wanted:
            continue
        if f["entity_id"] != cur:
            cur = f["entity_id"]
            e = ents[cur]
            head = f"=== {cur} | {e['entity_name']}"
            if e["entity_type"] == "project":
                head += f" | {e.get('timeline', '')} | {e.get('project_type') or 'type not listed'}"
            elif e["entity_type"] == "work_experience":
                head += f" | {e.get('role', '')} | {e.get('date', '')}"
            head += f" | type: {e['entity_type']} | implementation_status: {e['implementation_status']}"
            out += ["", head]
            out += [f"LIMITATION [{l['code']}]: {l['text']}" for l in e.get("limitations", [])]
        out.append(fact_line(f, (vocab_terms or {}).get(f["fact_id"])))
    return "\n".join(out).strip() + "\n"


def bundle(snapshot: dict, mode: str, retrieval: dict | None = None) -> dict:
    """The evidence package handed to the generator, with everything needed to check citations."""
    if mode == "full":
        ids, vocab = [f["fact_id"] for f in snapshot["facts"]], None
    elif mode == "rag":
        if retrieval is None:
            raise ValueError("mode 'rag' needs a retrieval result")
        ids, vocab = retrieval["selected_fact_ids"] + list(retrieval["vocab_terms"]), retrieval["vocab_terms"]
    else:
        raise ValueError(f"unknown context mode {mode!r}")
    text = render(snapshot, None if mode == "full" else ids, vocab)
    return {"mode": mode, "corpus_hash": snapshot["corpus_hash"], "provided_fact_ids": ids,
            "text": text, "context_hash": sha256_text(text), "token_estimate": approx_tokens(text),
            "retrieval_config_hash": (retrieval or {}).get("retrieval_config_hash")}
