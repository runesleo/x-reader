# -*- coding: utf-8 -*-
"""Run external tools with a hard deadline, a streamed output budget and process-tree cleanup."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from typing import Mapping, Optional, Sequence

# After the tool exits or is killed, keep reading its pipes this long at most.
_DRAIN_GRACE_SECONDS = 1.0

# Ceilings the hosted reader process applies to itself. A hosted read runs in a
# single process (no external tools), so these are per-request ceilings. The
# largest pages the parse budgets accept peaked at 113-132 MiB resident in local
# runs, which is why the hosted server runs at most 3 reads at once.
HOSTED_CPU_SECONDS = 120
HOSTED_MAX_ADDRESS_SPACE_BYTES = 768 * 1024 * 1024
HOSTED_MAX_FILE_BYTES = 64 * 1024 * 1024
HOSTED_MAX_OPEN_FILES = 256


def run_bounded(
    cmd: Sequence[str],
    timeout: float,
    env: Optional[Mapping[str, str]] = None,
    max_output_bytes: int = 16 * 1024 * 1024,
    text: bool = True,
    new_session: Optional[bool] = None,
) -> subprocess.CompletedProcess:
    """subprocess.run equivalent that cannot outlive its deadline or output budget.

    new_session=True starts the tool in its own session. When it exits, times out
    or overflows, the whole process group is killed, so yt-dlp / ffmpeg children
    left behind cannot outlive the call. The default is True except inside a
    hosted request (X_READER_HOSTED=1): there tools stay in the request's process
    group, so the hosted server's group kill reaches them too.

    Output is read while the tool runs. Going over max_output_bytes kills it at
    once and raises ValueError; the deadline raises subprocess.TimeoutExpired.
    Both are raised only after the tool has been killed and reaped.
    """
    if new_session is None:
        from x_reader.routing import hosted_mode

        new_session = not hosted_mode()
    if os.name != "posix":
        return _run_without_selectors(cmd, timeout, env, max_output_bytes, text)

    proc = subprocess.Popen(
        list(cmd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env) if env is not None else None,
        start_new_session=new_session,
    )
    selector = selectors.DefaultSelector()
    buffers = {proc.stdout: bytearray(), proc.stderr: bytearray()}
    for stream in buffers:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)

    deadline = time.monotonic() + timeout
    outcome = ""
    try:
        while not outcome:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = "timeout"
                break
            if selector.get_map():
                if _read_ready(selector, buffers, min(remaining, 0.1), max_output_bytes):
                    outcome = "overflow"
                    break
            else:
                time.sleep(min(remaining, 0.05))
            if _has_exited(proc):
                outcome = "exited"
        # The tool is a zombie (exited) or still running: either way its process
        # group may hold leftovers. Kill before reaping so the PID cannot be reused.
        _kill_tree(proc, new_session)
        proc.wait()
        if _drain(selector, buffers, max_output_bytes):
            outcome = outcome if outcome == "timeout" else "overflow"
    except BaseException:
        _kill_tree(proc, new_session)
        proc.wait()
        raise
    finally:
        selector.close()
        proc.stdout.close()
        proc.stderr.close()

    stdout, stderr = bytes(buffers[proc.stdout]), bytes(buffers[proc.stderr])
    if text:
        stdout = stdout.decode("utf-8", errors="replace")
        stderr = stderr.decode("utf-8", errors="replace")
    if outcome == "timeout":
        raise subprocess.TimeoutExpired(proc.args, timeout, output=stdout, stderr=stderr)
    if outcome == "overflow":
        raise ValueError("External tool output exceeded the byte budget")
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def _read_ready(selector, buffers, wait: float, max_output_bytes: int) -> bool:
    """Read whatever is ready; True once the combined output exceeds the budget."""
    for key, _ in selector.select(timeout=max(0.0, wait)):
        try:
            chunk = os.read(key.fd, 64 * 1024)
        except BlockingIOError:
            continue
        if not chunk:
            selector.unregister(key.fileobj)
            continue
        buffers[key.fileobj] += chunk
        if sum(len(buf) for buf in buffers.values()) > max_output_bytes:
            return True
    return False


def _drain(selector, buffers, max_output_bytes: int) -> bool:
    """Collect output still buffered in the pipes, bounded by a short grace period."""
    end = time.monotonic() + _DRAIN_GRACE_SECONDS
    while selector.get_map():
        remaining = end - time.monotonic()
        if remaining <= 0:
            return False
        if _read_ready(selector, buffers, min(remaining, 0.05), max_output_bytes):
            return True
    return False


def _has_exited(proc: subprocess.Popen) -> bool:
    """True once the child has exited, without reaping it (its PID stays reserved)."""
    if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
        try:
            info = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return True
        return info is not None and info.si_pid != 0
    return proc.poll() is not None


def _kill_tree(proc: subprocess.Popen, own_group: bool) -> None:
    if own_group:
        kill_group(proc)
    elif proc.returncode is None:
        try:
            proc.kill()
        except OSError:
            pass


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


def _run_without_selectors(cmd, timeout, env, max_output_bytes, text) -> subprocess.CompletedProcess:
    """Windows fallback: pipes cannot be polled, so the budget is checked after exit."""
    proc = subprocess.Popen(
        list(cmd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(env) if env is not None else None,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except BaseException:
        proc.kill()
        proc.communicate()
        raise
    if len(stdout) + len(stderr) > max_output_bytes:
        raise ValueError("External tool output exceeded the byte budget")
    if text:
        stdout = stdout.decode("utf-8", errors="replace")
        stderr = stderr.decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def apply_hosted_limits() -> None:
    """Per-request ceilings for the hosted reader process.

    Called by the CLI itself when X_READER_HOSTED=1: memory (address space), CPU
    seconds, size of any file written, open files, no core dumps. The process
    also volunteers as the first OOM-kill candidate, so memory pressure inside
    the service ends a request, not the server. Best effort: unsupported limits
    are skipped.
    """
    try:
        import resource
    except ImportError:  # Windows
        return
    for name, value in (
        ("RLIMIT_AS", HOSTED_MAX_ADDRESS_SPACE_BYTES),
        ("RLIMIT_CPU", HOSTED_CPU_SECONDS),
        ("RLIMIT_FSIZE", HOSTED_MAX_FILE_BYTES),
        ("RLIMIT_NOFILE", HOSTED_MAX_OPEN_FILES),
        ("RLIMIT_CORE", 0),
    ):
        limit = getattr(resource, name, None)
        if limit is None:
            continue
        try:
            _soft, hard = resource.getrlimit(limit)
            target = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(limit, (target, target))
        except (ValueError, OSError):
            pass
    try:
        with open("/proc/self/oom_score_adj", "w", encoding="ascii") as handle:
            handle.write("1000")
    except OSError:
        pass
