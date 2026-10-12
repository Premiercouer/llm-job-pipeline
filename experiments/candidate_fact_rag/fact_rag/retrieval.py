"""Local hybrid retrieval over the fact corpus: BM25 + LSA vectors, grouped selection, token budget.

Only evidence facts are indexed. Selection is grouped by entity rather than a flat top-k:
  1. every work experience gets a quota of its best facts (min / max), so the generator can
     always write the required bullets for each employer;
  2. projects are ranked by their best fact plus the mean of their top three, and the top
     n_projects enter with their header and tech stack (mandatory) plus a per-rank number of
     their best facts; near-duplicate facts are skipped;
  3. if the bundle is over the token budget, the lowest-scoring non-mandatory facts are dropped.
Every candidate fact records its BM25 / dense / hybrid scores, its best query and the reason it
was selected or rejected. A sufficiency check flags the bundle (RETRIEVAL_INSUFFICIENT) when
quotas are unfilled, no project fits, or too many queries that the corpus can answer are no
longer covered by the selected facts. Nothing here calls a model.
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter, defaultdict

import numpy as np

from . import embed, evidence, jd as jdmod
from .corpus import ats_vocabulary, canonical_hash, core_vocabulary, term_in

STOP = set("""a an and are as at be by for from has have in into is it its of on or that the this to
was were will with you your we our their they using use used via per across within over under than
able ability strong experience including include etc e g i ie role team work""".split())


def tokenize(text: str) -> list[str]:
    out = []
    for t in re.findall(r"[a-z0-9][a-z0-9+#.]*[a-z0-9+#]|[a-z0-9]", text.lower()):
        if t in STOP or (len(t) < 2 and not t.isdigit()):
            continue
        if len(t) > 4 and t.endswith("ies"):
            t = t[:-3] + "y"
        elif len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.75):
        self.k1, self.b, self.docs = k1, b, docs
        self.N = len(docs)
        self.avgdl = sum(map(len, docs)) / max(1, self.N)
        self.tf = [Counter(d) for d in docs]
        df = Counter(t for d in docs for t in set(d))
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    def scores(self, query: list[str]) -> np.ndarray:
        out = np.zeros(self.N)
        q = set(query)
        for i, tf in enumerate(self.tf):
            norm = self.k1 * (1 - self.b + self.b * len(self.docs[i]) / self.avgdl)
            out[i] = sum(self.idf[t] * tf[t] * (self.k1 + 1) / (tf[t] + norm) for t in q if t in tf)
        return out


def config_hash(cfg: dict) -> str:
    return canonical_hash({k: v for k, v in cfg.items() if not k.startswith("_")})


class Index:
    """Built once per (corpus, retrieval config): BM25 statistics + fact vectors."""

    def __init__(self, snapshot: dict, cfg: dict):
        t0 = time.perf_counter()
        self.snapshot, self.cfg = snapshot, cfg
        self.entities = {e["entity_id"]: e for e in snapshot["entities"]}
        prj_tags = {f["entity_id"]: f["keyword_tags"] for f in snapshot["facts"] if f["fact_type"] == "keyword_tags"}
        self.facts = [f for f in snapshot["facts"] if f["is_evidence"]]
        texts = []
        for f in self.facts:
            parts = [self.entities[f["entity_id"]]["entity_name"], f["text"]]
            if cfg.get("index_keyword_tags"):
                parts.append(" ".join(f.get("keyword_tags") or []))
                if f["fact_type"] == "project_header":  # tags help find a project, never prove anything
                    parts.append(" ".join(prj_tags.get(f["entity_id"], [])))
            texts.append(". ".join(p for p in parts if p))
        self.texts = texts
        self.bm25 = BM25([tokenize(t) for t in texts], **cfg["bm25"])
        t1 = time.perf_counter()
        self.embedder = embed.make(cfg["embedding"]).fit(texts)
        self.doc_emb = self.embedder.encode(texts, "doc")
        t2 = time.perf_counter()
        self.timing = {"bm25_build_ms": round((t1 - t0) * 1000, 2), "embedding_build_ms": round((t2 - t1) * 1000, 2),
                       "embedding_model_id": self.embedder.model_id, "doc_count": len(texts)}

    def score_queries(self, queries: list[dict]) -> dict:
        texts = [q["text"] for q in queries]
        if not texts:
            z = np.zeros((0, len(self.facts)))
            return {"bm25_norm": z, "dense": z, "hybrid": z}
        bm = np.stack([self.bm25.scores(tokenize(t)) for t in texts])
        mx = bm.max(axis=1, keepdims=True)
        mx[mx == 0] = 1.0
        bm_n = bm / mx
        dense = np.clip(self.embedder.encode(texts, "query") @ self.doc_emb.T, 0.0, 1.0)
        fusion = self.cfg["fusion"]
        if fusion["method"] == "weighted":
            a = fusion["bm25_weight"]
            hyb = a * bm_n + (1 - a) * dense
        elif fusion["method"] == "rrf":
            k, hyb = fusion["rrf_k"], np.zeros_like(bm_n)
            for m in (bm_n, dense):
                hyb += 1.0 / (k + (-m).argsort(axis=1).argsort(axis=1) + 1)
            hyb = hyb / hyb.max(axis=1, keepdims=True).clip(min=1e-12)
        elif fusion["method"] == "bm25_only":
            hyb = bm_n
        elif fusion["method"] == "dense_only":
            hyb = dense
        else:
            raise ValueError(f"unknown fusion method {fusion['method']!r}")
        return {"bm25_norm": bm_n, "dense": dense, "hybrid": hyb}


def aggregate(scores: dict, queries: list[dict], cfg: dict) -> list[dict]:
    """Per fact: best weighted query score + beta * mean of its top-m query scores."""
    if not queries:
        return [{"agg": 0.0, "best_query": None, "best_bm25_norm": 0.0, "best_dense": 0.0}
                for _ in range(scores["hybrid"].shape[1])]
    w = np.array([q["weight"] for q in queries])[:, None]
    hyb = scores["hybrid"] * w
    top_m, beta = cfg["aggregate"]["top_m"], cfg["aggregate"]["mean_top_weight"]
    out = []
    for j in range(hyb.shape[1]):
        col = hyb[:, j]
        order = np.argsort(-col)
        out.append({"agg": round(float(col[order[0]]) + beta * float(col[order[:top_m]].mean()), 6),
                    "best_query": queries[order[0]]["qid"],
                    "best_bm25_norm": round(float(scores["bm25_norm"][:, j].max()), 4),
                    "best_dense": round(float(scores["dense"][:, j].max()), 4)})
    return out


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(tokenize(a)), set(tokenize(b))
    return len(sa & sb) / max(1, len(sa | sb))


def _quota(sel_cfg: dict, order: int) -> dict:
    """Quota of the order-th work experience (candidate.json order); historical configs set one per employer."""
    by_order = sel_cfg.get("experience_quota_by_order") or []
    return by_order[order] if order < len(by_order) else sel_cfg["experience_quota"]


def select(index: Index, queries: list[dict], scores: dict, cfg: dict, jd_skills: list[str]) -> dict:
    snap, sel_cfg = index.snapshot, cfg["selection"]
    cand = [{"fact_id": f["fact_id"], "entity_id": f["entity_id"], "fact_type": f["fact_type"],
             "entity_type": f["entity_type"], "cost": evidence.fact_cost(f), **a, "selected": False,
             "mandatory": False, "reason": None, "text": f["text"]}
            for f, a in zip(index.facts, aggregate(scores, queries, cfg))]

    def take(c, reason):
        if not c["selected"]:
            c["selected"], c["reason"], c["mandatory"] = True, reason, reason.startswith("mandatory")

    # 1. work-experience quotas
    exp_ids = [e["entity_id"] for e in snap["entities"] if e["entity_type"] == "work_experience"]
    for order, eid in enumerate(exp_ids):
        q = _quota(sel_cfg, order)
        group = sorted([c for c in cand if c["entity_id"] == eid], key=lambda c: -c["agg"])
        for i, c in enumerate(group[: q["max"]]):
            take(c, f"mandatory_quota_{eid}" if i < q["min"] else f"quota_ranked_{eid}")
        for c in group[q["max"]:]:
            c["reason"] = f"below_quota_cutoff_{eid}"

    # 2. projects: rank by best fact + 0.5 * mean of the top-3 facts (low-value fact types excluded)
    prj = defaultdict(list)
    for c in cand:
        if c["entity_type"] == "project":
            prj[c["entity_id"]].append(c)
    low = set(sel_cfg["project_low_value_types"])
    prj_rank = []
    for eid, cs in prj.items():
        vals = sorted((c["agg"] for c in cs if c["fact_type"] not in low), reverse=True) or [0.0]
        prj_rank.append((vals[0] + 0.5 * float(np.mean(vals[:3])), eid))
    prj_rank.sort(reverse=True)
    chosen = [eid for _, eid in prj_rank[: sel_cfg["n_projects"]]]
    caps = sel_cfg["per_project_facts"]
    for rank, eid in enumerate(chosen):
        for c in prj[eid]:
            if c["fact_type"] in ("project_header", "tech_stack"):
                take(c, "mandatory_project_header_or_stack")
        pool = sorted([c for c in prj[eid] if not c["selected"] and c["fact_type"] not in sel_cfg["project_excluded_types"]],
                      key=lambda c: -c["agg"])
        n, kept = caps[min(rank, len(caps) - 1)], 0
        for c in pool:
            if kept >= n:
                c["reason"] = c["reason"] or "below_per_project_cap"
                continue
            if c["agg"] < sel_cfg["min_fact_score"]:
                c["reason"] = "below_min_score"
                continue
            dup = next((o for o in prj[eid] if o["selected"] and _jaccard(o["text"], c["text"]) >= sel_cfg["dedup_jaccard"]), None)
            if dup:
                c["reason"] = f"redundant_with_{dup['fact_id']}"
                continue
            take(c, f"project_rank{rank + 1}_top_fact")
            kept += 1
    for eid, cs in prj.items():
        if eid not in chosen:
            for c in cs:
                c["reason"] = c["reason"] or "project_not_in_top_n"

    # 3. token budget: drop the lowest-scoring non-mandatory facts first
    budget = sel_cfg["evidence_token_budget"]
    vocab_cost, vocab_terms = _vocab_block(snap, jd_skills, cfg)
    total = sum(c["cost"] for c in cand if c["selected"]) + vocab_cost + _entity_overhead(snap, cand)
    dropped = []
    for c in sorted([c for c in cand if c["selected"] and not c["mandatory"]], key=lambda c: c["agg"]):
        if total <= budget:
            break
        c["selected"], c["reason"] = False, "dropped_token_budget"
        total -= c["cost"]
        dropped.append(c["fact_id"])

    # 4. sufficiency: queries the corpus can answer that the selected facts no longer cover
    thr, hyb = sel_cfg["cover_threshold"], scores["hybrid"]
    sel_idx = [i for i, c in enumerate(cand) if c["selected"]]
    supportable, lost = [], []
    for qi, q in enumerate(queries):
        if q["type"] in ("requirement", "responsibility", "skill") and hyb.shape[1] and hyb[qi].max() >= thr:
            supportable.append(q["qid"])
            if not sel_idx or hyb[qi, sel_idx].max() < thr:
                lost.append(q["qid"])
    reasons = [f"quota_unfilled_{eid}" for order, eid in enumerate(exp_ids)
               if sum(c["selected"] and c["entity_id"] == eid for c in cand) < _quota(sel_cfg, order)["min"]]
    if not chosen:
        reasons.append("no_project")
    if supportable and len(lost) / len(supportable) > sel_cfg["max_lost_supportable_ratio"]:
        reasons.append(f"lost_supportable_queries_{len(lost)}/{len(supportable)}")
    if total > budget:
        reasons.append("over_budget_after_mandatory")
    return {"selected_fact_ids": [c["fact_id"] for c in cand if c["selected"]], "vocab_terms": vocab_terms,
            "chosen_projects": chosen, "project_ranking": [{"entity_id": e, "score": round(s, 6)} for s, e in prj_rank],
            "candidates": [{k: v for k, v in c.items() if k != "text"} for c in cand],
            "token_estimate": total, "token_budget": budget, "dropped_for_budget": dropped,
            "supportable_queries": supportable, "lost_supportable_queries": lost,
            "status": "RETRIEVAL_INSUFFICIENT" if reasons else "OK", "insufficient_reasons": reasons}


def _entity_overhead(snap: dict, cand: list[dict]) -> int:
    ents = {e["entity_id"]: e for e in snap["entities"]}
    cost = 0
    for eid in {c["entity_id"] for c in cand if c["selected"]}:
        e = ents[eid]
        cost += evidence.approx_tokens(e["entity_name"] + " " + e.get("timeline", "") + " " * 60)
        cost += sum(evidence.approx_tokens(l["text"]) + 6 for l in e.get("limitations", []))
    return cost


def _vocab_block(snap: dict, jd_skills: list[str], cfg: dict) -> tuple[int, dict]:
    """Core vocabulary always (small, fixed); ATS vocabulary narrowed to JD-matched terms."""
    vc = cfg["selection"]["vocabulary"]
    terms_by_fact = {}
    for f in snap["facts"]:
        if f["fact_type"] == "skill_vocab_core" and vc["include_core"]:
            terms_by_fact[f["fact_id"]] = f["terms"]
        elif f["fact_type"] == "skill_vocab_ats" and vc["ats_mode"] != "none":
            hit = f["terms"] if vc["ats_mode"] == "all" else \
                [t for t in f["terms"] if any(term_in(t, s) or term_in(s, t) for s in jd_skills)]
            if hit:
                terms_by_fact[f["fact_id"]] = hit
    cost = sum(evidence.approx_tokens(fid + " ".join(t)) + 12 for fid, t in terms_by_fact.items())
    return cost, terms_by_fact


def retrieve_for_job(index: Index, job: dict, cfg: dict) -> dict:
    t0 = time.perf_counter()
    snap = index.snapshot
    vocab = sorted(set(core_vocabulary(snap["facts"])) | set(ats_vocabulary(snap["facts"])))
    q = jdmod.build_queries(job["jd"], job["position"], vocab, cfg["queries"])
    t1 = time.perf_counter()
    scores = index.score_queries(q["queries"])
    t2 = time.perf_counter()
    sel = select(index, q["queries"], scores, cfg, q["skills_in_jd"])
    t3 = time.perf_counter()
    return {"corpus_hash": snap["corpus_hash"], "retrieval_config_hash": config_hash(cfg),
            "embedding_model_id": index.embedder.model_id, "queries": q["queries"],
            "dropped_jd_sections": q["dropped"], "skills_in_jd": q["skills_in_jd"], **sel,
            "timing_ms": {"query_build": round((t1 - t0) * 1000, 2), "scoring": round((t2 - t1) * 1000, 2),
                          "selection": round((t3 - t2) * 1000, 2), "total_per_job": round((t3 - t0) * 1000, 2)}}
