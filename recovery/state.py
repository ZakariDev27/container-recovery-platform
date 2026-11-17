"""Persistent supervisor state and the event log."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("recovery")


@dataclass
class ServiceState:
    generation: int = 0
    failures: int = 0                      # consecutive failed health checks
    replacements: list[float] = field(default_factory=list)
    restores: list[float] = field(default_factory=list)
    circuit_open: bool = False
    healthy_since: float | None = None


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


@contextlib.contextmanager
def service_lock(state_dir: Path, name: str, blocking: bool = True):
    """Cross-process lock for one service. Yields False if it is busy (non-blocking only).

    Stops a running supervisor and a manual command (backup, restore, replace)
    from acting on the same service at the same time.
    """
    try:
        import fcntl
    except ImportError:  # not available on this platform: no locking
        yield True
        return
    path = state_dir / "locks" / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class StateStore:
    """Per-service state, kept on disk so limits survive a supervisor restart."""

    def __init__(self, path: Path):
        self.path = path
        self._services: dict[str, ServiceState] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                self._services = {name: ServiceState(**body) for name, body in raw.items()}
            except (ValueError, TypeError) as exc:
                log.warning("ignoring unreadable state file %s: %s", path, exc)

    def get(self, name: str) -> ServiceState:
        return self._services.setdefault(name, ServiceState())

    def save(self) -> None:
        write_json_atomic(self.path, {n: asdict(s) for n, s in self._services.items()})


class EventLog:
    """Append-only JSON lines log of everything the platform does."""

    def __init__(self, path: Path | None, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self.events: list[dict] = []
        self.listeners: list[Callable[[dict], None]] = []

    def emit(self, kind: str, service: str, **fields) -> dict:
        event = {"time": round(self.clock(), 3), "event": kind, "service": service, **fields}
        self.events.append(event)
        extras = " ".join(f"{k}={v}" for k, v in fields.items())
        log.info("%s %s %s", service, kind, extras)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as fh:
                fh.write(json.dumps(event) + "\n")
        for listener in self.listeners:
            try:
                listener(event)
            except Exception as exc:  # a broken listener must not break recovery
                log.warning("event listener failed: %s", exc)
        return event

    def kinds(self, service: str | None = None) -> list[str]:
        return [e["event"] for e in self.events if service in (None, e["service"])]
