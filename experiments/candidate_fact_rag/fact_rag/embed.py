"""Local dense vectors for hybrid retrieval. Nothing is downloaded and nothing leaves the machine.

  tfidf_lsa   TF-IDF (1-2 grams, sublinear tf) + truncated SVD fitted on the candidate corpus.
              This is latent semantic analysis: a linear, corpus-fitted projection, NOT a neural
              embedding model. It is the backend the historical experiment ran (dim 128, seed 13).
  hashing     deterministic signed hashing of words; for tests and environments without
              scikit-learn. Retrieval quality with it is not meaningful.
"""
from __future__ import annotations

import hashlib
import re

import numpy as np


def _l2(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


class HashingEmbedder:
    name = "hashing"

    def __init__(self, dim: int = 256, **_):
        self.dim = dim
        self.model_id = f"hashing-{dim}"

    def fit(self, docs):
        return self

    def encode(self, texts, kind="doc"):
        out = np.zeros((len(texts), self.dim))
        for i, t in enumerate(texts):
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                h = int(hashlib.md5(w.encode()).hexdigest(), 16)
                out[i, h % self.dim] += 1.0 if (h >> 8) % 2 else -1.0
        return _l2(out)


class TfidfLsaEmbedder:
    name = "tfidf_lsa"

    def __init__(self, dim: int = 128, seed: int = 13, **_):
        self.dim, self.seed = dim, seed
        self.model_id = f"tfidf_lsa-{dim}-seed{seed}"

    def fit(self, docs):
        try:
            from sklearn.decomposition import TruncatedSVD
            from sklearn.feature_extraction.text import TfidfVectorizer
        except ImportError as e:  # optional dependency: pip install ".[rag]"
            raise RuntimeError("tfidf_lsa needs scikit-learn (pip install '.[rag]'), or use backend 'hashing'") from e
        self.vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=1, lowercase=True,
                                   token_pattern=r"(?u)\b[\w+#.]{2,}\b")
        x = self.vec.fit_transform(docs)
        k = max(2, min(self.dim, x.shape[1] - 1, x.shape[0] - 1))
        self.svd = TruncatedSVD(n_components=k, random_state=self.seed)
        self.svd.fit(x)
        return self

    def encode(self, texts, kind="doc"):
        return _l2(self.svd.transform(self.vec.transform(texts)))


BACKENDS = {"hashing": HashingEmbedder, "tfidf_lsa": TfidfLsaEmbedder}


def make(cfg: dict):
    if cfg["backend"] not in BACKENDS:
        raise ValueError(f"unknown embedding backend {cfg['backend']!r} (choose from {', '.join(BACKENDS)})")
    return BACKENDS[cfg["backend"]](**cfg.get("params", {}))
