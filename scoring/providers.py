"""Provider selection: built-in names or an external `module:factory` (e.g. a private adapter
kept outside this repository and put on PYTHONPATH). The factory receives keyword options."""
from __future__ import annotations

import importlib
from pathlib import Path

from scoring.models import ResumeScorer

BUILTIN = ("mock", "manual")


def load_provider(spec: str, *, inbox: Path | None = None) -> ResumeScorer:
    if spec == "mock":
        from scoring.mock import MockResumeScorer
        return MockResumeScorer()
    if spec == "manual":
        from scoring.manual import ManualFileScorer
        if inbox is None:
            raise ValueError("manual provider needs an inbox directory")
        return ManualFileScorer(inbox)
    if ":" not in spec:
        raise ValueError(f"unknown provider {spec!r}: use {', '.join(BUILTIN)} or module:factory")
    module_name, attr = spec.split(":", 1)
    factory = getattr(importlib.import_module(module_name), attr)
    provider = factory()
    if not isinstance(provider, ResumeScorer):
        raise TypeError(f"{spec} does not implement ResumeScorer (name, submit, collect)")
    return provider
