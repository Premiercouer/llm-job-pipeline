"""Shared fixtures. The experiment needs numpy (and scikit-learn for tfidf_lsa); without them the
suites are skipped, so the main test run never depends on the optional `rag` extra."""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

EXP = Path(__file__).resolve().parents[1]
REPO = EXP.parents[1]
if str(EXP) not in sys.path:
    sys.path.insert(0, str(EXP))

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
HAVE_SKLEARN = HAVE_NUMPY and importlib.util.find_spec("sklearn") is not None
needs_numpy = unittest.skipUnless(HAVE_NUMPY, "numpy not installed (pip install '.[rag]')")
needs_sklearn = unittest.skipUnless(HAVE_SKLEARN, "scikit-learn not installed (pip install '.[rag]')")
JOB = json.loads((EXP / "fixtures" / "illustrative_job.json").read_text(encoding="utf-8"))

_SETUP = {}


def setup(backend="hashing"):
    """One pipeline.Setup per backend for the whole test process (building the index is the slow part)."""
    from fact_rag import pipeline
    if backend not in _SETUP:
        _SETUP[backend] = pipeline.Setup(embedding_backend=backend)
    return _SETUP[backend]
