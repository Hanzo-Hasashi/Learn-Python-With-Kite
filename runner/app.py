from __future__ import annotations

import base64
import hmac
import logging
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Kite isolated Python runner")
log = logging.getLogger("kite.runner")
MAX_OUTPUT = 64 * 1024
MAX_SECONDS = 3
FIRST_SANDBOX_UID = 20_000
LAST_SANDBOX_UID = 60_000
MAX_CONCURRENT_RUNS = 2
ACTIVE_UIDS: set[int] = set()
UID_LOCK = threading.Lock()
RUN_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_RUNS)
ACTIVE_EXECUTIONS: dict[str, subprocess.Popen[bytes] | None] = {}
CANCELLED_EXECUTIONS: dict[str, float] = {}
FINISHED_EXECUTIONS: dict[str, float] = {}
EXECUTION_LOCK = threading.Lock()
EXECUTION_MARKER_TTL = 60
BOOTSTRAP = os.path.join(os.path.dirname(__file__), "sandbox_bootstrap.py")
LAUNCHER = os.path.join(os.path.dirname(__file__), "sandbox_launch.py")


class CodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=20_000)
    execution_id: str | None = Field(default=None, min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")


def require_key(key: str | None) -> None:
    expected = os.getenv("RUNNER_SHARED_SECRET", "")
    if not expected or not key or not hmac.compare_digest(key, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def allocate_sandbox_uid() -> int:
    with UID_LOCK:
        for uid in range(FIRST_SANDBOX_UID, LAST_SANDBOX_UID):
            if uid not in ACTIVE_UIDS:
                ACTIVE_UIDS.add(uid)
                return uid
    raise HTTPException(status_code=503, detail="No isolated execution slot is available.")


def release_sandbox_uid(uid: int) -> None:
    with UID_LOCK:
        ACTIVE_UIDS.discard(uid)


def purge_execution_markers(now: float) -> None:
    for markers in (CANCELLED_EXECUTIONS, FINISHED_EXECUTIONS):
        for execution_id, expires_at in list(markers.items()):
            if expires_at <= now:
                markers.pop(execution_id, None)


def reserve_execution(execution_id: str | None) -> bool:
    if execution_id is None:
        return True
    with EXECUTION_LOCK:
        now = time.monotonic()
        purge_execution_markers(now)
        if execution_id in FINISHED_EXECUTIONS or execution_id in ACTIVE_EXECUTIONS:
            raise HTTPException(status_code=409, detail="Execution ID is already in use.")
        if execution_id in CANCELLED_EXECUTIONS:
            CANCELLED_EXECUTIONS.pop(execution_id, None)
            FINISHED_EXECUTIONS[execution_id] = now + EXECUTION_MARKER_TTL
            return False
        ACTIVE_EXECUTIONS[execution_id] = None
        return True


def set_execution_process(execution_id: str | None, process: subprocess.Popen[bytes]) -> bool:
    if execution_id is None:
        return False
    with EXECUTION_LOCK:
        ACTIVE_EXECUTIONS[execution_id] = process
        return execution_id in CANCELLED_EXECUTIONS


def execution_cancelled(execution_id: str | None) -> bool:
    if execution_id is None:
        return False
    with EXECUTION_LOCK:
        return execution_id in CANCELLED_EXECUTIONS


def finish_execution(execution_id: str | None) -> None:
    if execution_id is None:
        return
    with EXECUTION_LOCK:
        now = time.monotonic()
        ACTIVE_EXECUTIONS.pop(execution_id, None)
        CANCELLED_EXECUTIONS.pop(execution_id, None)
        FINISHED_EXECUTIONS[execution_id] = now + EXECUTION_MARKER_TTL


def remove_work_directory(path: str) -> None:
    os.chown(path, 0, 0)
    os.chmod(path, 0o700)
    for current, directories, _ in os.walk(path, topdown=True):
        for name in directories:
            child = os.path.join(current, name)
            if os.path.islink(child):
                directories.remove(name)
                os.unlink(child)
            else:
                os.chown(child, 0, 0, follow_symlinks=False)
                os.chmod(child, 0o700, follow_symlinks=False)
    for current, directories, files in os.walk(path, topdown=False):
        for name in files:
            os.unlink(os.path.join(current, name))
        for name in directories:
            child = os.path.join(current, name)
            if os.path.islink(child):
                os.unlink(child)
            else:
                os.rmdir(child)
    os.rmdir(path)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/cancel/{execution_id}")
def cancel_execution(execution_id: str, x_kite_runner_key: Annotated[str | None, Header()] = None):
    require_key(x_kite_runner_key)
    if not re.fullmatch(r"[a-f0-9]{32}", execution_id):
        raise HTTPException(status_code=422, detail="Invalid execution ID.")
    process: subprocess.Popen[bytes] | None = None
    with EXECUTION_LOCK:
        now = time.monotonic()
        purge_execution_markers(now)
        if execution_id in FINISHED_EXECUTIONS:
            return {"cancelled": False}
        if execution_id not in ACTIVE_EXECUTIONS:
            CANCELLED_EXECUTIONS[execution_id] = now + EXECUTION_MARKER_TTL
            return {"cancelled": True}
        process = ACTIVE_EXECUTIONS[execution_id]
        if process is not None and process.poll() is not None:
            return {"cancelled": False}
        CANCELLED_EXECUTIONS[execution_id] = now + EXECUTION_MARKER_TTL
    if process is not None:
        kill_group(process)
    return {"cancelled": True}


@app.post("/run")
def run_code(payload: CodeIn, x_kite_runner_key: Annotated[str | None, Header()] = None):
    require_key(x_kite_runner_key)
    if sys.platform != "linux":
        raise HTTPException(status_code=503, detail="The isolated runner requires its Linux container.")
    if not RUN_SLOTS.acquire(blocking=False):
        raise HTTPException(status_code=503, detail="All isolated execution slots are busy.")

    try:
        uid = allocate_sandbox_uid()
    except HTTPException:
        RUN_SLOTS.release()
        raise
    try:
        should_run = reserve_execution(payload.execution_id)
    except HTTPException:
        release_sandbox_uid(uid)
        RUN_SLOTS.release()
        raise
    if not should_run:
        release_sandbox_uid(uid)
        RUN_SLOTS.release()
        return {"stdout": "", "stderr": "", "error": "Execution cancelled."}
    work_directory: str | None = None
    process: subprocess.Popen[bytes]
    try:
        work_directory = tempfile.mkdtemp(prefix="kite-run-", dir="/tmp")
        os.chown(work_directory, uid, uid)
        os.chmod(work_directory, 0o700)
        encoded_code = base64.b64encode(payload.code.encode("utf-8")).decode("ascii")
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", LAUNCHER, str(uid), str(uid), BOOTSTRAP, encoded_code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=work_directory,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "TMPDIR": work_directory},
            start_new_session=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        log.exception("Could not start isolated Python execution")
        if work_directory:
            try:
                remove_work_directory(work_directory)
            except OSError:
                log.exception("Could not clean failed Python execution directory")
        finish_execution(payload.execution_id)
        release_sandbox_uid(uid)
        RUN_SLOTS.release()
        raise HTTPException(status_code=503, detail="Python runtime could not be started.")

    was_cancelled = set_execution_process(payload.execution_id, process)
    if was_cancelled:
        kill_group(process)
    selector: selectors.BaseSelector | None = None
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    started = time.monotonic()
    exceeded = False
    timed_out = False
    try:
        selector = selectors.DefaultSelector()
        assert process.stdout is not None and process.stderr is not None
        for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map() or process.poll() is None:
            if execution_cancelled(payload.execution_id):
                was_cancelled = True
                kill_group(process)
                break
            if time.monotonic() - started > MAX_SECONDS:
                timed_out = True
                kill_group(process)
                break
            for key, _ in selector.select(timeout=0.05):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                output = captured[key.data]
                remaining = MAX_OUTPUT - sum(len(part) for part in captured.values())
                output.extend(chunk[:max(remaining, 0)])
                if len(chunk) > remaining:
                    exceeded = True
                    kill_group(process)
                    break
            if exceeded:
                break
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            kill_group(process)
            process.wait(timeout=0.25)
    finally:
        try:
            if selector is not None:
                selector.close()
        finally:
            try:
                kill_group(process)
                if process.poll() is None:
                    process.kill()
                    process.wait()
            finally:
                try:
                    if work_directory is not None:
                        remove_work_directory(work_directory)
                except OSError as exc:
                    log.exception("Could not clean Python execution directory")
                    raise HTTPException(status_code=503, detail="Python execution cleanup failed.") from exc
                finally:
                    finish_execution(payload.execution_id)
                    release_sandbox_uid(uid)
                    RUN_SLOTS.release()

    decode = lambda value: value.decode("utf-8", errors="replace")
    if was_cancelled or execution_cancelled(payload.execution_id):
        return {"stdout": decode(captured["stdout"]), "stderr": decode(captured["stderr"]),
                "error": "Execution cancelled."}
    if timed_out:
        return {"stdout": decode(captured["stdout"]), "stderr": decode(captured["stderr"]), "error": "Execution timed out."}
    if exceeded:
        return {"stdout": decode(captured["stdout"]), "stderr": decode(captured["stderr"]), "error": "Output limit exceeded."}
    return {"stdout": decode(captured["stdout"]), "stderr": decode(captured["stderr"]), "error": None if process.returncode == 0 else "Code exited with an error."}
