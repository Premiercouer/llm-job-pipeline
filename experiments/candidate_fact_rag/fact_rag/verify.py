"""Automatic checks of a cited, structured output against the facts it cites.

Two layers, both deterministic and both AUTO (heuristics, not ground truth):

  citations  every fact_id exists in this corpus version, was actually provided in the context
             (so a rag output cannot cite a fact it never saw), belongs to the same experience /
             project as the claim, and is not vocabulary-only.
  content    the claim is re-checked against the cited facts only: every number must appear in
             a cited fact with the same unit class (% vs percentage points differ), every tool
             named must appear in a cited fact, a skill must be used in a cited evidence fact,
             and entity limitations (for example "framework only, not implemented") must not be
             contradicted.

Severity: SEVERE = invented or transplanted number / tool, limitation violated, citation to an
unknown or unprovided fact. MAJOR = unit changed, number or tool present in the corpus but not in
the cited facts, skill outside the vocabulary. UNKNOWN = cannot be decided here (free paraphrase,
vocabulary-only skills, small counts). UNKNOWN is reported separately and never counted as a
pass. What these checks cannot see: whether a paraphrase is faithful, whether two facts were
merged into a misleading sentence, and whether the most relevant facts were chosen at all.
"""
from __future__ import annotations

import re

from . import quant
from .corpus import ats_vocabulary, core_vocabulary, facts_by_id, term_in
from .retrieval import tokenize

SMALL_COUNT_MAX = 12          # bare counts up to this value are UNKNOWN when unmatched (e.g. "three teams")
SUPPORTED_RECALL = 0.6        # share of a claim's content words found in its cited facts
GENERIC = set(tokenize("built used developed created applied using delivered supporting support across through "
                       "enabling strong experience skilled proficient hands data analysis analytics"))


class Verifier:
    def __init__(self, snapshot: dict, lexicon: dict, overlay: dict):
        self.snap = snapshot
        self.facts = facts_by_id(snapshot)
        self.evidence = [f for f in snapshot["facts"] if f["is_evidence"]]
        self.vocab = {v.lower() for v in core_vocabulary(snapshot["facts"]) + ats_vocabulary(snapshot["facts"])}
        self.tools = sorted(set(lexicon["tools"]), key=lambda s: (-len(s), s))
        self.aliases = lexicon.get("aliases", {})
        self.limits = {e["entity_id"]: e.get("limitations", []) for e in snapshot["entities"]}
        for eid, ov in overlay.get("entities", {}).items():
            self.limits[eid] = ov.get("limitations", [])

    # -------------------------------------------------------------- helpers
    def _names(self, tool: str) -> list[str]:
        return [tool] + self.aliases.get(tool, [])

    @staticmethod
    def _has(name: str, text: str) -> bool:
        if len(name) <= 2:  # 'R', 'Go': case-sensitive, standalone
            return re.search(rf"(?<![A-Za-z0-9&]){re.escape(name)}(?![A-Za-z0-9&+#²])", text) is not None
        return term_in(name, text)

    def _tool_in(self, tool: str, facts: list[dict]) -> bool:
        return any(self._has(n, f["text"]) for f in facts for n in self._names(tool))

    def _tool_hits(self, text: str) -> list[str]:
        hits, masked = [], text
        for t in self.tools:
            if self._has(t, masked):
                hits.append(t)
                masked = re.sub(re.escape(t), " ", masked, flags=0 if len(t) <= 2 else re.I)
        return hits

    # -------------------------------------------------------------- checks
    def citations(self, claim: dict, provided: set, bundle_hash: str) -> dict:
        out = {"uncited": not claim["cited"], "unknown_ids": [], "not_provided": [], "wrong_entity": [],
               "version_ok": bundle_hash == self.snap["corpus_hash"]}
        cited = []
        for fid in claim["cited"]:
            f = self.facts.get(fid)
            if f is None:
                out["unknown_ids"].append(fid)
                continue
            if fid not in provided:
                out["not_provided"].append(fid)
            if claim["section"] in ("experience", "project", "project_tools") and f["entity_id"] != claim["entity_id"]:
                out["wrong_entity"].append(fid)
            cited.append(f)
        out["vocab_only"] = bool(cited) and not any(f["is_evidence"] for f in cited)
        out["valid"] = (not out["uncited"] and not out["unknown_ids"] and not out["not_provided"]
                        and not out["wrong_entity"] and out["version_ok"])
        return out, [f for f in cited if f["is_evidence"]]

    def check_numbers(self, text: str, basis: list[dict], section: str, eid: str | None) -> list[dict]:
        flags = []
        for n in quant.extract(text):
            same = [(f, m) for f in basis for m in f["numbers"] if quant.same_value(n["value"], m["value"])]
            if any(quant.compatible_units(n["unit_class"], m["unit_class"]) for _, m in same):
                continue
            if same:
                flags.append({"code": "NUMBER_UNIT_CHANGED", "severity": "MAJOR", "number": n["raw"],
                              "claim_unit": n["unit_class"], "fact_unit": same[0][1]["unit_class"]})
                continue
            small = n["unit_class"] == "count" and n["value"] <= SMALL_COUNT_MAX
            elsewhere = sorted({f["entity_id"] for f in self.evidence for m in f["numbers"]
                                if quant.same_value(n["value"], m["value"])})
            if small:
                flags.append({"code": "SMALL_COUNT_UNVERIFIED", "severity": "UNKNOWN", "number": n["raw"]})
            elif elsewhere and section in ("experience", "project") and eid not in elsewhere:
                flags.append({"code": "NUMBER_WRONG_ENTITY", "severity": "SEVERE", "number": n["raw"],
                              "belongs_to": elsewhere[:3]})
            elif elsewhere:
                flags.append({"code": "NUMBER_NOT_IN_CITED", "severity": "MAJOR", "number": n["raw"]})
            else:
                flags.append({"code": "NUMBER_NOT_IN_CORPUS", "severity": "SEVERE", "number": n["raw"]})
        return flags

    def check_tools(self, text: str, basis: list[dict], section: str, eid: str | None) -> list[dict]:
        flags = []
        for t in self._tool_hits(text):
            if self._tool_in(t, basis):
                continue
            if not self._tool_in(t, self.evidence):
                flags.append({"code": "TOOL_NOT_IN_CORPUS", "severity": "SEVERE", "tool": t})
            elif section in ("experience", "project", "project_tools") and eid and \
                    not self._tool_in(t, [f for f in self.evidence if f["entity_id"] == eid]):
                flags.append({"code": "TOOL_WRONG_ENTITY", "severity": "SEVERE", "tool": t})
            else:
                flags.append({"code": "TOOL_NOT_IN_CITED", "severity": "MAJOR", "tool": t})
        return flags

    def check_skill(self, name: str, basis: list[dict]) -> list[dict]:
        pool = [f for f in basis if f["fact_type"] != "project_header"]
        if any(term_in(name, f["text"]) or self._tool_in(name, [f]) for f in pool):
            return []
        is_tool = any(name.lower() == t.lower() for t in self.tools)
        if is_tool and not self._tool_in(name, self.evidence) and name.lower() not in self.vocab:
            return [{"code": "SKILL_TOOL_NOT_IN_CORPUS", "severity": "SEVERE", "skill": name}]
        if name.lower() not in self.vocab and not is_tool:
            return [{"code": "SKILL_OUTSIDE_VOCABULARY", "severity": "MAJOR", "skill": name}]
        return [{"code": "SKILL_NOT_SHOWN_IN_CITED_EVIDENCE", "severity": "UNKNOWN", "skill": name}]

    def check_limitations(self, text: str, entity_ids: set) -> list[dict]:
        flags = []
        for eid in sorted(e for e in entity_ids if e):
            for lim in self.limits.get(eid, []):
                for pat in lim.get("forbidden_patterns", []):
                    m = re.search(pat, text, re.I)
                    if m:
                        flags.append({"code": f"LIMITATION_{lim['code']}", "severity": lim.get("severity", "MAJOR"),
                                      "entity_id": eid, "match": m.group(0)[:60]})
                        break
        return flags

    @staticmethod
    def recall(text: str, basis: list[dict]) -> float:
        words = [t for t in tokenize(text) if t not in GENERIC]
        if not words:
            return 1.0
        known = set()
        for f in basis:
            known |= set(tokenize(f["entity_name"] + " " + f["text"]))
        return sum(t in known for t in words) / len(words)

    # -------------------------------------------------------------- output
    def check(self, claims: list[dict], bundle: dict) -> dict:
        provided = set(bundle["provided_fact_ids"])
        rows = []
        for c in claims:
            cit, basis = self.citations(c, provided, bundle["corpus_hash"])
            if c["section"] == "project_tools":  # the tools line is checked against the project's provided facts
                basis = [f for f in self.evidence if f["entity_id"] == c["entity_id"] and f["fact_id"] in provided]
            flags = []
            if cit["unknown_ids"] or cit["not_provided"]:
                flags.append({"code": "CITATION_NOT_IN_CONTEXT", "severity": "SEVERE",
                              "ids": cit["unknown_ids"] + cit["not_provided"]})
            if cit["wrong_entity"]:
                flags.append({"code": "CITATION_WRONG_ENTITY", "severity": "SEVERE", "ids": cit["wrong_entity"]})
            if not cit["version_ok"]:
                flags.append({"code": "CORPUS_VERSION_MISMATCH", "severity": "SEVERE"})
            if cit["uncited"] or cit["vocab_only"]:
                flags.append({"code": "NO_EVIDENCE_CITED", "severity": "UNKNOWN"})
            if c["section"].startswith("skill_"):
                flags += self.check_skill(c["text"], basis) if basis else []
                rec = 1.0
            else:
                flags += self.check_numbers(c["text"], basis, c["section"], c["entity_id"])
                flags += self.check_tools(c["text"], basis, c["section"], c["entity_id"])
                ents = {c["entity_id"]} | {f["entity_id"] for f in basis}
                flags += self.check_limitations(c["text"], ents)
                rec = 1.0 if c["section"] == "project_tools" else self.recall(c["text"], basis)
            sev = {f["severity"] for f in flags}
            status = ("FAIL_AUTO" if "SEVERE" in sev else
                      "UNKNOWN" if sev & {"MAJOR", "UNKNOWN"} or rec < SUPPORTED_RECALL else "SUPPORTED_AUTO")
            rows.append({"claim_id": c["claim_id"], "section": c["section"], "status": status,
                         "lexical_recall": round(rec, 3), "citations_valid": cit["valid"], "flags": flags})
        st = [r["status"] for r in rows]
        return {"claims": rows, "summary": {
            "claims": len(rows), "supported_auto": st.count("SUPPORTED_AUTO"), "unknown": st.count("UNKNOWN"),
            "fail_auto": st.count("FAIL_AUTO"),
            "citation_valid_rate": round(sum(r["citations_valid"] for r in rows) / max(1, len(rows)), 4),
            "severe": sum(f["severity"] == "SEVERE" for r in rows for f in r["flags"]),
            "major": sum(f["severity"] == "MAJOR" for r in rows for f in r["flags"]),
            "flag_codes": sorted({f["code"] for r in rows for f in r["flags"]})}}
