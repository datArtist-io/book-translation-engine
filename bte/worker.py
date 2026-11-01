"""Background worker: runs long jobs off the web request path and resumes after restarts.

All job state lives in the database, so the worker can be killed at any time; on start
it re-queues every project left in a running stage.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading

from sqlalchemy import select

from .service import Service, WorkflowError
from .store import Project

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, service: Service):
        self.service = service
        self._q: queue.Queue[tuple[int, str]] = queue.Queue()
        self._queued: set[tuple[int, str]] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="bte-worker", daemon=True)
            self._thread.start()
            self.resume()

    def resume(self) -> None:
        with self.service.store.session() as db:
            for p in db.scalars(select(Project)).all():
                if p.stage == "bible_draft":
                    self.submit(p.id, "bible")
                elif p.stage == "translating":
                    self.submit(p.id, "translate")

    def submit(self, pid: int, action: str) -> None:
        with self._lock:
            if (pid, action) in self._queued:
                return
            self._queued.add((pid, action))
        self._q.put((pid, action))

    def busy(self, pid: int) -> bool:
        with self._lock:
            return any(p == pid for p, _ in self._queued)

    def _loop(self) -> None:
        loop = asyncio.new_event_loop()
        while True:
            pid, action = self._q.get()
            try:
                if action == "bible":
                    loop.run_until_complete(self.service.build_bible(pid))
                elif action == "translate":
                    loop.run_until_complete(self.service.translate(pid))
            except WorkflowError as exc:
                self._fail(pid, str(exc))
            except Exception as exc:  # never let one job kill the worker
                log.exception("job %s/%s failed", pid, action)
                self._fail(pid, f"Unexpected error ({type(exc).__name__}). The job can be resumed.")
            finally:
                with self._lock:
                    self._queued.discard((pid, action))

    def _fail(self, pid: int, message: str) -> None:
        with self.service.store.session() as db:
            p = db.get(Project, pid)
            if p is not None:
                p.stage, p.stage_detail = "paused", message
        self.service.store.log(pid, "system", "job_failed", message=message)
