"""Health checks: http, tcp, exec (inside the container), docker (image HEALTHCHECK)."""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import Service
from .runtime import Runtime


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    detail: str
    down: bool = False  # container missing or not running: no point waiting for the threshold

    def __bool__(self) -> bool:
        return self.ok


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _http(url: str, expect: tuple[int, int], timeout: float) -> HealthResult:
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return HealthResult(False, f"http {url}: {getattr(exc, 'reason', exc)}")
    ok = expect[0] <= status <= expect[1]
    return HealthResult(ok, f"http {url}: status {status}")


def _tcp(host: str, port: int, timeout: float) -> HealthResult:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return HealthResult(True, f"tcp {host}:{port}: connected")
    except OSError as exc:
        return HealthResult(False, f"tcp {host}:{port}: {exc}")


def check(service: Service, runtime: Runtime) -> HealthResult:
    """Run the service's configured health check once.

    A container that is missing or not running is always unhealthy,
    whatever the check type.
    """
    state = runtime.inspect(service.container_name)
    if state is None:
        return HealthResult(False, "container missing", down=True)
    if state.status == "paused":
        return HealthResult(False, "container paused")
    if not state.running:
        return HealthResult(False, f"container {state.status}", down=True)

    h = service.health
    if h.type == "http":
        return _http(h.url, h.expect_status, h.timeout)
    if h.type == "tcp":
        return _tcp(h.host, h.port, h.timeout)
    if h.type == "exec":
        rc = runtime.exec(service.container_name, list(h.command), h.timeout)
        return HealthResult(rc == 0, f"exec exit code {rc}")
    # docker: rely on the image's HEALTHCHECK; with none defined, running is enough
    if state.health is None:
        return HealthResult(True, "running (image defines no HEALTHCHECK)")
    return HealthResult(state.health == "healthy", f"docker health: {state.health}")
