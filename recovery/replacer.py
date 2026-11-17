"""Replacement containers.

A failed container is never restarted in place. It is stopped, set aside
under a ``-failed-`` name for inspection, and a fresh container is created
from the service definition. Named volumes carry over to the replacement.
"""

from __future__ import annotations

import time
from typing import Callable

from .config import Config, Service
from .health import HealthResult, check as default_check
from .runtime import EngineError, Runtime
from .state import EventLog, StateStore
from .storage import StorageError

Checker = Callable[[Service, Runtime], HealthResult]


class Replacer:
    def __init__(
        self,
        config: Config,
        runtime: Runtime,
        events: EventLog,
        state: StateStore,
        checker: Checker = default_check,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.runtime = runtime
        self.events = events
        self.state = state
        self.checker = checker
        self.clock = clock
        self.sleep = sleep

    def wait_healthy(self, service: Service) -> HealthResult:
        """Poll the health check until it passes or start_timeout runs out."""
        deadline = self.clock() + service.health.start_timeout
        while True:
            result = self.checker(service, self.runtime)
            if result.ok:
                return result
            state = self.runtime.inspect(service.container_name)
            if state is None or state.status in ("exited", "dead"):
                return HealthResult(False, f"container stopped during startup ({result.detail})", down=True)
            if self.clock() >= deadline:
                return HealthResult(False, f"not healthy after {service.health.start_timeout:g}s: {result.detail}")
            self.sleep(1.0)

    def _save_forensics(self, service: Service, generation: int) -> None:
        try:
            text = self.runtime.logs(service.container_name, tail=200)
        except EngineError:
            return
        dest = self.config.state_dir / "forensics" / f"{service.name}-g{generation:04d}.log"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)

    def _set_aside(self, service: Service, generation: int) -> None:
        name = service.container_name
        self._save_forensics(service, generation)
        self.runtime.stop(name, service.recovery.stop_timeout)
        failed_name = f"{name}-failed-{generation:04d}"
        if self.runtime.inspect(failed_name) is not None:
            self.runtime.remove(failed_name)
        self.runtime.rename(name, failed_name)

    def _prune_failed(self, service: Service) -> None:
        failed = self.runtime.list_names(f"{service.container_name}-failed-")
        keep = service.recovery.keep_failed
        for name in failed[: len(failed) - keep] if keep else failed:
            self.runtime.remove(name)

    def _launch(self, service: Service) -> HealthResult:
        st = self.state.get(service.name)
        st.generation += 1
        self.state.save()
        labels = {"crp.service": service.name, "crp.generation": str(st.generation)}
        self.runtime.run(service.container_name, service, labels)
        return self.wait_healthy(service)

    def start(self, service: Service) -> bool:
        """Bring a service up if it is not already running."""
        state = self.runtime.inspect(service.container_name)
        if state is not None and state.running:
            return True
        return self.replace(service, "start requested")

    def replace(
        self,
        service: Service,
        reason: str,
        restore: Callable[[], object] | None = None,
    ) -> bool:
        """Swap the service's container for a new one. Returns True if it came up healthy.

        ``restore``, if given, is called after the old container is stopped and
        before the new one starts, so volumes are rewritten while nothing uses them.
        """
        st = self.state.get(service.name)
        try:
            if self.runtime.inspect(service.container_name) is not None:
                self._set_aside(service, st.generation)
            if restore is not None:
                try:
                    restore()
                except StorageError as exc:
                    self.events.emit("restore_skipped", service.name, error=str(exc))
            result = self._launch(service)
            self._prune_failed(service)
        except EngineError as exc:
            self.events.emit("replace_failed", service.name, reason=reason, error=str(exc))
            return False

        self.events.emit(
            "replaced" if result.ok else "replace_failed",
            service.name, reason=reason, generation=st.generation, detail=result.detail,
        )
        return result.ok
