"""Background job manager: long running tasks with progress, logs and results."""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from typing import Any, Callable, Optional


class Job:
    def __init__(self, kind: str, title: str = ""):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.title = title or kind
        self.status = "pending"          # pending | running | done | failed | cancelled
        self.percent = 0.0
        self.message = ""
        self.detail: dict[str, Any] = {}
        self.result: Any = None
        self.error: Optional[str] = None
        self.created = time.time()
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.logs: list[dict] = []
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # -- used by tasks ----------------------------------------------------
    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()
        self.log("已请求取消，等待当前步骤结束…", level="warn")

    def progress(self, percent: float, message: str = "", **detail) -> None:
        with self._lock:
            if percent is not None and percent >= 0:
                self.percent = max(0.0, min(100.0, float(percent)))
            if message:
                self.message = message
            if detail:
                self.detail.update(detail)

    def log(self, message: str, level: str = "info") -> None:
        with self._lock:
            self.logs.append({"t": time.time(), "level": level, "message": str(message)})
            if len(self.logs) > 800:
                del self.logs[:200]
            self.message = str(message)

    def as_dict(self, log_offset: int = 0) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "title": self.title,
                "status": self.status,
                "percent": round(self.percent, 2),
                "message": self.message,
                "detail": dict(self.detail),
                "result": self.result,
                "error": self.error,
                "created": self.created,
                "elapsed": round((self.finished or time.time()) - (self.started or self.created), 1),
                "logs": self.logs[log_offset:],
                "log_count": len(self.logs),
            }


class JobManager:
    def __init__(self, keep: int = 40):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._keep = keep
        self._lock = threading.Lock()

    def create(self, kind: str, title: str = "") -> Job:
        job = Job(kind, title)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > self._keep:
                old = self._order.pop(0)
                self._jobs.pop(old, None)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        return [self._jobs[i].as_dict(log_offset=10 ** 9) for i in self._order[::-1]]

    def run(self, kind: str, fn: Callable[[Job], Any], title: str = "") -> Job:
        job = self.create(kind, title)

        def runner():
            job.status = "running"
            job.started = time.time()
            job.log("任务开始")
            try:
                job.result = fn(job)
                if job.cancelled:
                    job.status = "cancelled"
                    job.error = "用户已取消"
                else:
                    job.status = "done"
                    job.percent = 100.0
            except Exception as exc:  # noqa: BLE001
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.log(traceback.format_exc().strip().splitlines()[-1], level="error")
            finally:
                job.finished = time.time()
                job.log(f"任务结束（{job.status}）", level="info" if job.status == "done" else "error")

        threading.Thread(target=runner, name=f"job-{job.id}", daemon=True).start()
        return job
