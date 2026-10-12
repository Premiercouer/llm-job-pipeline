"""Candidate-fact retrieval experiment (evaluated alternative to full-text context; not the pipeline default).

Modules: corpus (canonical facts) -> jd (queries) -> retrieval (BM25 + LSA, grouped selection)
-> evidence (full / rag context) -> prompts -> contract (cited output, provenance) -> verify
(citation and fact checks). fake_model is a deterministic stand-in for offline runs.
The production pipeline never imports this package.
"""
