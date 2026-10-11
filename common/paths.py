"""Repository-wide path resolution.

Code, prompts and shipped config live in the repository. Everything a run produces
(discovery outputs, batches, checkpoints, logs, resumes, application queue) lives in a
workspace directory that is never committed. Candidate facts (profile, bullet banks,
skills) live in a candidate directory; the repository ships a fictional candidate in
examples/candidate/, and a real one is configured outside the repository.

Resolution order for each setting: environment variable, then config/local.toml
(or the file named by LJP_CONFIG), then the default below.

    [paths]
    workspace     = "~/job-pipeline-data"       # LJP_WORKSPACE
    candidate_dir = "~/job-pipeline-candidate"  # LJP_CANDIDATE_DIR

Relative values are resolved against the repository root. Importing this module has no
side effects: it reads config but never creates directories.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.version_info < (3, 11):  # fail with a clear message instead of an ImportError below
    raise SystemExit(f"llm-job-pipeline needs Python 3.11+ (running {sys.version.split()[0]} at "
                     f"{sys.executable}); see pyproject.toml")

import tomllib  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_config() -> dict:
    path = Path(os.environ.get("LJP_CONFIG") or REPO_ROOT / "config" / "local.toml").expanduser()
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


_CONFIG = _load_config()


def _setting(env: str, key: str, default: Path) -> Path:
    value = os.environ.get(env) or _CONFIG.get("paths", {}).get(key)
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


WORKSPACE = _setting("LJP_WORKSPACE", "workspace", REPO_ROOT / "workspace")
CANDIDATE_DIR = _setting("LJP_CANDIDATE_DIR", "candidate_dir", REPO_ROOT / "examples" / "candidate")


def data_dir(*parts: str) -> Path:
    """Directory inside the workspace for one pipeline component (not created here)."""
    return WORKSPACE.joinpath(*parts)
