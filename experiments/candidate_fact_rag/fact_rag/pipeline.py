"""Wiring for one job: corpus -> retrieval -> evidence bundle -> output -> checks.

Used by the offline demo and the (opt-in) real-call script; neither the production pipeline nor
its tests import it.
"""
from __future__ import annotations

from pathlib import Path

from . import contract, corpus, evidence, retrieval, verify

EXPERIMENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = EXPERIMENT_DIR.parents[1]
CONFIG_DIR = EXPERIMENT_DIR / "config"
DEFAULT_CANDIDATE = REPO_ROOT / "examples" / "candidate"
DEFAULT_RETRIEVAL = CONFIG_DIR / "retrieval.demo.json"


class Setup:
    def __init__(self, candidate_dir: Path = DEFAULT_CANDIDATE, retrieval_config: Path = DEFAULT_RETRIEVAL,
                 embedding_backend: str | None = None):
        self.candidate_dir = Path(candidate_dir)
        self.overlay = corpus.load_json(CONFIG_DIR / "limitations.json")
        self.lexicon = corpus.load_json(CONFIG_DIR / "tool_lexicon.json")
        self.snapshot = corpus.build(self.candidate_dir, CONFIG_DIR / "limitations.json",
                                     CONFIG_DIR / "tool_lexicon.json")
        self.cfg = {k: v for k, v in corpus.load_json(retrieval_config).items() if not k.startswith("_")}
        if embedding_backend:
            self.cfg["embedding"] = {"backend": embedding_backend, "params": {}}
        self.index = retrieval.Index(self.snapshot, self.cfg)
        self.identity = contract.load_identity(self.candidate_dir)
        self.experience_ids = corpus.experience_ids(self.snapshot)
        self.verifier = verify.Verifier(self.snapshot, self.lexicon, self.overlay)

    def context(self, job: dict, mode: str) -> tuple[dict, dict]:
        """-> (retrieval result, evidence bundle). Retrieval also runs in full mode, for diagnostics."""
        r = retrieval.retrieve_for_job(self.index, job, self.cfg)
        return r, evidence.bundle(self.snapshot, mode, r if mode == "rag" else None)

    def check(self, obj: dict, bundle: dict) -> dict:
        contract.validate(obj, self.experience_ids)
        return {"resume_text": contract.render(obj, self.identity, self.snapshot),
                "provenance": contract.provenance(obj, self.snapshot, bundle),
                "verification": self.verifier.check(contract.claims(obj), bundle)}
