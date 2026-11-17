"""The supervisor: watches health, replaces containers, escalates to storage recovery.

Recovery ladder for a service that keeps failing, within ``recovery.window``:

1. Replace the container (volumes carried over).
2. After ``restore_storage_after`` replacements, restore volumes from the
   newest restore-tested backup, then replace again.
3. After ``max_replacements`` replacements, open the circuit: stop trying and
   leave the last failed container in place for a human. ``reset`` closes it.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .config import Config, Service
from .health import check as default_check
from .replacer import Checker, Replacer
from .runtime import EngineError, Runtime
from .state import EventLog, StateStore
from .storage import StorageError, StorageManager


class Supervisor:
    def __init__(
        self,
        config: Config,
        runtime: Runtime,
        *,
        checker: Checker = default_check,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        events: EventLog | None = None,
    ):
        self.config = config
        self.runtime = runtime
        self.checker = checker
        self.clock = clock
        self.events = events or EventLog(config.state_dir / "events.jsonl", clock)
        self.state = StateStore(config.state_dir / "state.json")
        self.storage = StorageManager(config, runtime, self.events, clock)
        self.replacer = Replacer(config, runtime, self.events, self.state, checker, clock, sleep)

    # -- lifecycle --------------------------------------------------------
    def up(self, names: list[str] | None = None) -> dict[str, bool]:
        results = {}
        for service in self._select(names):
            results[service.name] = self.replacer.start(service)
        self.state.save()
        return results

    def down(self, names: list[str] | None = None) -> None:
        for service in self._select(names):
            self.runtime.stop(service.container_name, service.recovery.stop_timeout)
            self.runtime.remove(service.container_name)
            self.events.emit("stopped", service.name)

    def reset(self, name: str) -> None:
        """Close the circuit and forget the failure history for a service."""
        st = self.state.get(self.config.service(name).name)
        st.circuit_open = False
        st.failures = 0
        st.replacements.clear()
        st.restores.clear()
        self.state.save()
        self.events.emit("circuit_reset", name)

    def _select(self, names: list[str] | None) -> list[Service]:
        if not names:
            return list(self.config.services.values())
        return [self.config.service(n) for n in names]

    # -- one pass ---------------------------------------------------------
    def tick(self) -> dict[str, str]:
        """Check every service once and act on the result."""
        outcome = {}
        for service in self.config.services.values():
            try:
                outcome[service.name] = self._tick_service(service)
            except EngineError as exc:
                self.events.emit("engine_error", service.name, error=str(exc))
                outcome[service.name] = "engine-error"
        self.state.save()
        return outcome

    def _tick_service(self, service: Service) -> str:
        st = self.state.get(service.name)
        if st.circuit_open:
            return "circuit-open"

        result = self.checker(service, self.runtime)
        if result.ok:
            st.failures = 0
            self._backup_if_due(service)
            return "healthy"

        st.failures += 1
        self.events.emit(
            "check_failed", service.name, detail=result.detail,
            failures=st.failures, threshold=service.health.failure_threshold,
        )
        if result.down or st.failures >= service.health.failure_threshold:
            return self.recover(service, result.detail)
        return "unhealthy"

    def recover(self, service: Service, reason: str) -> str:
        st = self.state.get(service.name)
        rc = service.recovery
        now = self.clock()
        st.replacements = [t for t in st.replacements if now - t < rc.window]
        st.restores = [t for t in st.restores if now - t < rc.window]

        if len(st.replacements) >= rc.max_replacements:
            st.circuit_open = True
            self.events.emit(
                "circuit_opened", service.name, reason=reason,
                replacements=len(st.replacements), window=rc.window,
            )
            return "circuit-open"

        restore = None
        if service.volumes and len(st.replacements) >= rc.restore_storage_after and not st.restores:
            record = self.storage.latest_verified(service)
            if record is None:
                self.events.emit("restore_unavailable", service.name, detail="no restore-tested backup")
            else:
                def restore(record=record):
                    self.storage.restore(service, record.id)
                    st.restores.append(now)

        st.replacements.append(now)
        ok = self.replacer.replace(service, reason, restore)
        # A replacement that never became healthy should escalate on the next pass.
        st.failures = 0 if ok else max(service.health.failure_threshold - 1, 0)
        return "replaced" if ok else "replace-failed"

    def _backup_if_due(self, service: Service) -> None:
        """Scheduled backups run only while the service is healthy."""
        if not service.volumes or service.backup.interval <= 0:
            return
        records = self.storage.list(service)
        if records and self.clock() - records[0].created_at < service.backup.interval:
            return
        try:
            self.storage.backup(service)
        except StorageError:
            pass  # already recorded in the event log

    # -- loop -------------------------------------------------------------
    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        self.events.emit("supervisor_started", "*", services=len(self.config.services))
        while not stop.is_set():
            self.tick()
            stop.wait(self.config.check_interval)
        self.events.emit("supervisor_stopped", "*")

    # -- reporting --------------------------------------------------------
    def status(self) -> list[dict]:
        rows = []
        for service in self.config.services.values():
            st = self.state.get(service.name)
            state = self.runtime.inspect(service.container_name)
            result = self.checker(service, self.runtime)
            verified = self.storage.latest_verified(service) if service.volumes else None
            rows.append({
                "service": service.name,
                "container": state.status if state else "missing",
                "health": "healthy" if result.ok else "unhealthy",
                "detail": result.detail,
                "generation": st.generation,
                "recent_replacements": len(st.replacements),
                "circuit": "open" if st.circuit_open else "closed",
                "verified_backup": verified.id if verified else ("-" if service.volumes else "n/a"),
            })
        return rows
