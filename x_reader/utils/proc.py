# -*- coding: utf-8 -*-
"""Run external tools so a timeout ends the whole process tree, not just the direct child."""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Mapping, Optional, Sequence


def run_bounded(
    cmd: Sequence[str],
    timeout: float,
    env: Optional[Mapping[str, str]] = None,
    max_output_bytes: int = 16 * 1024 * 1024,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """subprocess.run equivalent that starts a new session and kills the group on timeout.

    yt-dlp, ffmpeg and headless browsers spawn children; killing only the direct
    child leaves them running. Raises subprocess.TimeoutExpired after the group is
    killed and reaped, and ValueError when captured output exceeds the byte budget.
    """
    proc = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env) if env is not None else None,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc)
        proc.communicate()
        raise
    except BaseException:
        kill_group(proc)
        proc.communicate()
        raise
    if len(stdout) + len(stderr) > max_output_bytes:
        raise ValueError("External tool output exceeded the byte budget")
    if text:
        return subprocess.CompletedProcess(
            proc.args, proc.returncode,
            stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace"),
        )
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the process group started for proc (no-op if it already exited)."""
    try:
        if not hasattr(os, "killpg"):  # Windows: no process groups via setsid
            raise OSError("process groups unavailable")
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
