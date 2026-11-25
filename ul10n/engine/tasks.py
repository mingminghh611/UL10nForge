"""Background task runner with live progress metrics and async SSE broadcasting."""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable


@dataclass
class TaskProgress:
    task_id: str
    kind: str            # translate | review | context
    project_id: str
    status: str          # running | completed | cancelled | failed
    seq: int = 0
    current: int = 0
    total: int = 0
    done: int = 0
    failed: int = 0
    speed_per_min: float = 0.0
    message: str = ""
    started_at: float = 0.0

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 100 if self.status == "completed" else 0
        return min(100, round(self.current * 100 / self.total))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["percent"] = self.percent
        return d


class EventBus:
    """Thread-safe async pub/sub bus for pushing SSE events to connected browsers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def add_listener(self, cb: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            if cb not in self._listeners:
                self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            if cb in self._listeners:
                self._listeners.remove(cb)

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._loop = loop

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        with self._lock:
            self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._queues.discard(q)

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        payload = {"type": event_type, "data": data, "ts": time.time()}
        with self._lock:
            loop = self._loop
            queues = list(self._queues)
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(payload)
            except Exception:
                pass
        if not queues:
            return
        if loop and loop.is_running():
            def _push() -> None:
                for q in queues:
                    if q.full():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        q.put_nowait(payload)
                    except asyncio.QueueFull:
                        pass
            loop.call_soon_threadsafe(_push)


class TaskManager:
    """Runs one cooperative background task at a time with live progress updates."""

    def __init__(self, event_bus: EventBus):
        self.bus = event_bus
        self._lock = threading.RLock()
        self._current: TaskProgress | None = None
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    def current_task(self) -> dict[str, Any] | None:
        with self._lock:
            return self._current.to_dict() if self._current else None

    def is_running(self) -> bool:
        with self._lock:
            return self._current is not None and self._current.status == "running"

    def cancel(self) -> bool:
        with self._lock:
            if not self._current or self._current.status != "running":
                return False
            self._cancel.set()
            self._current.message = "正在停止..."
            self.bus.publish("task_progress", self._current.to_dict())
            return True

    def start(
        self,
        kind: str,
        project_id: str,
        worker: Callable[[TaskProgress, threading.Event, Callable[[], None]], None],
        *,
        initial_message: str = "准备中...",
    ) -> dict[str, Any]:
        with self._lock:
            if self._current and self._current.status == "running":
                raise RuntimeError("当前已有后台任务正在运行，请先等待完成或暂停。")
            self._cancel = threading.Event()
            task = TaskProgress(
                task_id=uuid.uuid4().hex[:10],
                kind=kind,
                project_id=project_id,
                status="running",
                message=initial_message,
                started_at=time.monotonic(),
            )
            self._current = task

        def emit() -> None:
            with self._lock:
                task.seq += 1
                elapsed = max(0.01, time.monotonic() - task.started_at)
                task.speed_per_min = round(task.current * 60.0 / elapsed, 1)
                snap = task.to_dict()
            self.bus.publish("task_progress", snap)

        def runner() -> None:
            emit()
            try:
                worker(task, self._cancel, emit)
                with self._lock:
                    if self._cancel.is_set():
                        task.status = "cancelled"
                        if not task.message or task.message == "正在停止...":
                            task.message = "已暂停"
                    else:
                        task.status = "completed"
            except Exception as exc:
                with self._lock:
                    task.status = "failed"
                    task.message = str(exc) or type(exc).__name__
            finally:
                with self._lock:
                    task.seq += 1
                    snap = task.to_dict()
                self.bus.publish("task_done", snap)

        self._thread = threading.Thread(target=runner, daemon=True)
        self._thread.start()
        with self._lock:
            return task.to_dict()
