"""Restricted Python execution for Ask AI agent runs — Phase 3 v1.

Runs model-authored code in a separate OS process with hard resource limits:
CPU time, address space, output size, file count/size, wall clock, and a
scratch working directory that is deleted afterwards. Files the code writes
to ``out/`` are collected as artifacts.

KNOWN LIMITATION (documented in the feature spec): this v1 executes inside
the backend container (non-root, caps dropped) rather than a dedicated
network-isolated sandbox service. Do not enable for untrusted tenants until
the dedicated ``ask-sandbox`` service lands.
"""

from __future__ import annotations

import os
import resource
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

_WALL_TIMEOUT_S = 120          # generous: allows pip installs mid-run
_CPU_LIMIT_S = 60
_MEM_LIMIT_BYTES = 1024 * 1024 * 1024
_MAX_OUTPUT_CHARS = 20_000
_MAX_ARTIFACTS = 8
_MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
_FSIZE_LIMIT_BYTES = 256 * 1024 * 1024  # single-file cap (wheels can be large)
_MAX_CODE_CHARS = 20_000
_SKIP_DIRS = {"pkgs", "__pycache__", ".cache", ".pip"}


_PIP_ROOT: str | None = None


def _pip_site() -> str | None:
    """Bootstrap pip once into a writable root (the venv has no pip and is
    read-only for the runtime uid); expose it to sandbox children."""
    global _PIP_ROOT
    if _PIP_ROOT is not None:
        return _PIP_ROOT or None
    root = os.path.join(tempfile.gettempdir(), "askai-pip-root")
    try:
        import glob
        hits = glob.glob(os.path.join(root, "**", "site-packages"), recursive=True)
        if not hits:
            subprocess.run(
                [sys.executable, "-m", "ensurepip", "--root", root],
                capture_output=True, timeout=120, check=True,
            )
            hits = glob.glob(os.path.join(root, "**", "site-packages"), recursive=True)
        _PIP_ROOT = hits[0] if hits else ""
    except Exception:
        _PIP_ROOT = ""
    return _PIP_ROOT or None


@dataclass
class SandboxResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    artifacts: list[dict] = field(default_factory=list)  # {name, path, size}


def _limits() -> None:  # pragma: no cover - runs in the child process
    resource.setrlimit(resource.RLIMIT_CPU, (_CPU_LIMIT_S, _CPU_LIMIT_S))
    resource.setrlimit(resource.RLIMIT_AS, (_MEM_LIMIT_BYTES, _MEM_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (512, 512))
    resource.setrlimit(resource.RLIMIT_FSIZE, (_FSIZE_LIMIT_BYTES, _FSIZE_LIMIT_BYTES))
    os.nice(10)


def run_python(code: str) -> SandboxResult:
    """Execute ``code`` in a fresh scratch dir; collect files from ./out."""
    if len(code) > _MAX_CODE_CHARS:
        return SandboxResult(stderr="code too large", exit_code=1)
    workdir = tempfile.mkdtemp(prefix="askai-sbx-")
    out_dir = os.path.join(workdir, "out")
    os.makedirs(out_dir, exist_ok=True)
    try:
        try:
            pip_site = _pip_site()
            pythonpath = os.path.join(workdir, "pkgs")
            if pip_site:
                pythonpath = f"{pythonpath}:{pip_site}"
            proc = subprocess.run(
                # -s (no user site) rather than -I so PYTHONPATH can expose the
                # bootstrapped pip and ./pkgs installs to the child.
                [sys.executable, "-s", "-c", code],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=_WALL_TIMEOUT_S,
                preexec_fn=_limits,
                env={
                    # venv bin first so `python -m pip` and console scripts work
                    "PATH": f"{os.path.dirname(sys.executable)}:/usr/bin:/bin",
                    "HOME": workdir,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPATH": pythonpath,
                    "PIP_CACHE_DIR": os.path.join(workdir, ".pip"),
                    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                    "TMPDIR": workdir,
                },
            )
            stdout, stderr, exit_code, timed_out = (
                proc.stdout, proc.stderr, proc.returncode, False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = "execution timed out"
            exit_code, timed_out = 124, True

        artifacts: list[dict] = []
        # Collect from ./out first, then any stray files models wrote to the
        # workdir root (they often ignore path instructions).
        candidates: list[str] = []
        if os.path.isdir(out_dir):
            candidates += [os.path.join(out_dir, n) for n in sorted(os.listdir(out_dir))]
        candidates += [
            os.path.join(workdir, n)
            for n in sorted(os.listdir(workdir))
            if n not in _SKIP_DIRS and n != "out" and not n.startswith(".")
        ]
        for path in candidates[: _MAX_ARTIFACTS * 3]:
            if len(artifacts) >= _MAX_ARTIFACTS:
                break
            if True:
                name = os.path.basename(path)
                if not os.path.isfile(path):
                    continue
                size = os.path.getsize(path)
                if 0 < size <= _MAX_ARTIFACT_BYTES:
                    # Move out of the scratch dir so cleanup doesn't race the caller.
                    kept = tempfile.NamedTemporaryFile(delete=False, prefix="askai-art-")
                    kept.close()
                    shutil.move(path, kept.name)
                    artifacts.append({"name": os.path.basename(name)[:120], "path": kept.name, "size": size})
        return SandboxResult(
            stdout=stdout[:_MAX_OUTPUT_CHARS],
            stderr=stderr[:_MAX_OUTPUT_CHARS],
            exit_code=exit_code,
            timed_out=timed_out,
            artifacts=artifacts,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
