"""Private per-process run directories for model workers (staged prompts, empty cwds).

Every directory lives under <workspace>/.runtime/worker_runs/<pid>-<random>/ with mode 0700,
never in a shared temp location. Cleanup happens three ways:

1. normal exit: an atexit handler removes the process's directories;
2. SIGTERM: converted to SystemExit (only if no other handler is installed) so 1. runs;
3. SIGKILL / power loss: the next process sweeps directories whose owning pid is gone.
"""
from __future__ import annotations

import atexit
import os
import secrets
import shutil
import signal
import stat
from pathlib import Path

from common import paths

_OWNED: list[Path] = []
_SWEPT = False


def root() -> Path:
    return paths.data_dir(".runtime", "worker_runs")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sweep_stale() -> list[Path]:
    """Remove run directories left behind by processes that no longer exist."""
    removed = []
    base = root()
    if not base.is_dir():
        return removed
    for d in base.iterdir():
        pid_part = d.name.split("-", 1)[0]
        if d.is_dir() and pid_part.isdigit() and int(pid_part) != os.getpid() and not _pid_alive(int(pid_part)):
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
    return removed


def _cleanup() -> None:
    while _OWNED:
        shutil.rmtree(_OWNED.pop(), ignore_errors=True)


def _on_sigterm(signum, frame):  # pragma: no cover - exercised via subprocess in tests
    raise SystemExit(128 + signum)


def new_run_dir(label: str) -> Path:
    """Create a fresh 0700 directory owned by this process."""
    global _SWEPT
    if not _SWEPT:
        _SWEPT = True
        sweep_stale()
        atexit.register(_cleanup)
        if signal.getsignal(signal.SIGTERM) == signal.SIG_DFL:
            try:
                signal.signal(signal.SIGTERM, _on_sigterm)
            except ValueError:  # not the main thread
                pass
    base = root()
    base.mkdir(parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    d = base / f"{os.getpid()}-{label}-{secrets.token_hex(4)}"
    d.mkdir(mode=0o700)
    _OWNED.append(d)
    return d


def remove(d: Path) -> None:
    shutil.rmtree(d, ignore_errors=True)
    if d in _OWNED:
        _OWNED.remove(d)


def write_private(path: Path, text: str) -> None:
    """Write a file readable only by the owner (0600)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
