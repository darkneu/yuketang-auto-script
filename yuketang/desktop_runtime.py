"""Bounded, cancellable desktop jobs. Credentials stay in private local IPC."""
from __future__ import annotations

import logging
import multiprocessing
import os
import queue
import signal
import subprocess
import threading
import time


class _WindowsJob:
    """A private kernel job owns only this run's worker and inherited descendants."""
    def __init__(self, pid):
        import ctypes
        from ctypes import wintypes as wt
        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wt.DWORD), ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t),
                        ("active", wt.DWORD), ("affinity", ctypes.c_size_t), ("priority", wt.DWORD), ("scheduling", wt.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")]
        class Limits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
        self.api.CreateJobObjectW.restype = wt.HANDLE
        self.api.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        self.api.OpenProcess.restype = wt.HANDLE
        self.api.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        self.api.CloseHandle.argtypes = [wt.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        process_handle = None
        try:
            limits = Limits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            process_handle = self.api.OpenProcess(0x0100 | 0x0001, False, pid)
            if not process_handle or not self.api.AssignProcessToJobObject(self.handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            if process_handle:
                self.api.CloseHandle(process_handle)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _bootstrap(worker, kind, config, sender, cancel, payload, gate):
    # Do not allow descendants or network calls until the parent owns our entire job.
    if gate.wait(10):
        worker(kind, config, sender, cancel, payload)


class _EventRelay:
    """Serialize UI events on a separate thread; Pipe backpressure cannot stall tasks."""
    def __init__(self, sender):
        self.sender = sender
        self.events = queue.SimpleQueue()
        self.thread = threading.Thread(target=self._send, name="yuketang-events", daemon=True)
        self.thread.start()

    def emit(self, event, value):
        self.events.put((event, value))

    def _send(self):
        try:
            while (event := self.events.get()) is not None:
                self.sender.send(event)
        except (BrokenPipeError, EOFError, OSError):
            pass

    def close(self):
        # Preserve event order and the final result. Parent hard-stop still bounds cancellation.
        self.events.put(None)
        self.thread.join()
        self.sender.close()


def _worker(kind, config, sender, cancel, payload):
    if os.name != "nt":
        os.setsid()
    logger = logging.getLogger("yuketang")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False

    relay = _EventRelay(sender)
    emit = relay.emit
    reply_log = None

    try:
        if kind in {"course", "ai_check"}:
            from .ai_log import AIReplyLog
            reply_log = AIReplyLog(config.project_root / "logs" / "ai-replies.jsonl")
        if kind == "offline_probe":
            # Packaged smoke test: exercise frozen spawn and hard stop without any imports/network.
            emit("probe_ready", None)
            time.sleep(60)
        elif kind == "course":
            from .desktop_backend import DesktopService
            from .session import YuketangSession
            service = DesktopService(config, emit)
            service.ai_reply_observer = reply_log.record
            service.cancel = cancel
            service.session = YuketangSession(**payload["session_data"])
            service.session.stop_event = cancel
            service.session.observer = lambda leaf_id, values: emit("task", (leaf_id, values))
            service.course, service.leaves = payload["course"], payload["leaves"]
            try:
                service.run_course()
            finally:
                service.session.session.close()
        elif kind == "ai_check":
            from .ai import create_ai_client
            client = create_ai_client(config)
            client.stop_event = cancel
            client.reply_observer = reply_log.record
            client.chat("只输出数字。", "1 + 1 等于多少？", max_retries=1)
            if not cancel.is_set():
                emit("ai_check", (True, "连接正常"))
    except Exception:
        if not cancel.is_set():
            if kind == "ai_check":
                emit("ai_check", (False, "连接失败，请检查所选模式的配置与登录状态"))
            else:
                emit("error", "任务执行失败，请检查连接与设置")
    finally:
        if reply_log is not None:
            reply_log.close()
            if reply_log.failed:
                emit("log_error", None)
        relay.close()


def _stop_tree(process, job=None):
    """Only kill the worker we launched and its descendants, never other CLI jobs."""
    if job is not None:
        job.close()
        process.join(timeout=1)
    if not process.is_alive():
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=3,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    if process.is_alive():
        process.kill()
    process.join(timeout=1)


def run_isolated(kind, config, stop_event, emit, *, worker=_worker, grace=1.0, **payload):
    """Run off the UI thread, with a hard cancellation boundary for blocked HTTP/SDK calls."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    cancel = context.Event()
    gate = context.Event()
    process = context.Process(target=_bootstrap, args=(worker, kind, config, sender, cancel, payload, gate),
                              name="yuketang-" + kind)
    started = False
    cancelling_at = None
    got_result = False
    job = None
    try:
        if stop_event.is_set():
            return
        process.start()
        started = True
        if os.name == "nt":
            job = _WindowsJob(process.pid)
        gate.set()
        sender.close()
        while True:
            if stop_event.is_set():
                cancel.set()
                if cancelling_at is None:
                    cancelling_at = time.monotonic()
                if time.monotonic() - cancelling_at >= grace and process.is_alive():
                    _stop_tree(process, job)
            try:
                available = receiver.poll(0.05)
                if available:
                    event, value = receiver.recv()
            except (EOFError, OSError):
                break
            if available:
                if event in {"run_done", "ai_check"}:
                    got_result = True
                if not (stop_event.is_set() and event in {"run_done", "ai_check"}):
                    emit(event, value)
            elif not process.is_alive():
                break
        process.join(timeout=0.2)
        if stop_event.is_set():
            emit("cancelled", None)
        elif process.exitcode not in (0, None) or (kind == "ai_check" and not got_result):
            emit("error", "工作任务意外结束，请重试")
    finally:
        if started and process.is_alive():
            _stop_tree(process, job)
        if job is not None:
            job.close()
        receiver.close()
        sender.close()
        if started and not process.is_alive():
            process.close()
