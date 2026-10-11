"""Read-only working directory for `claude -p --agent <name>` workers.

An agent worker resolves `.claude/agents/<name>.md`, `instructions/...` and `knowledge/...`
relative to its cwd. Instead of running inside the repository (where it would also pick up
CLAUDE.md files and see the workspace), each controller process stages exact copies of the
component's agents and instructions plus the candidate's knowledge files into one empty temp
directory, shared by all of its workers and removed when the process exits.
"""
from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path


def stage(component_root: Path, knowledge: Path | None = None,
          extra: dict[str, Path] | None = None) -> Path:
    """`extra` maps paths inside the run dir (e.g. "knowledge/candidate.json") to source files."""
    run_dir = Path(tempfile.mkdtemp(prefix=f"ljp_{component_root.name}_worker_"))
    atexit.register(shutil.rmtree, run_dir, ignore_errors=True)
    shutil.copytree(component_root / ".claude" / "agents", run_dir / ".claude" / "agents")
    shutil.copytree(component_root / "instructions", run_dir / "instructions")
    if knowledge is not None:
        if not knowledge.is_dir():
            raise FileNotFoundError(f"candidate knowledge directory not found: {knowledge}")
        shutil.copytree(knowledge, run_dir / "knowledge")
    for rel, src in (extra or {}).items():
        dest = run_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    return run_dir
