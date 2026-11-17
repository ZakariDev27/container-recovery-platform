"""In-memory container engine for tests.

Containers are records in a dict; volumes are real directories on disk, so
backup archives, checksums, and restores are exercised for real.
"""

from __future__ import annotations

import hashlib
import shutil
import tarfile
from pathlib import Path
from typing import BinaryIO, Callable

from recovery.config import Service
from recovery.runtime import ContainerState, EngineError


class FakeRuntime:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.containers: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self._next_id = 0
        # Decides the health of a container when it starts. Receives the runtime
        # and the service so tests can make health depend on volume contents.
        self.health_on_start: Callable[["FakeRuntime", Service], str] = lambda rt, svc: "healthy"
        self.run_once_result: Callable[[dict[str, str], list[str]], int] = lambda mounts, cmd: 0
        self.fail_run: str | None = None
        self.corrupt_import = False

    # -- helpers for tests ------------------------------------------------
    def vol(self, volume: str) -> Path:
        return self.root / volume

    def set_health(self, name: str, health: str) -> None:
        self.containers[name]["health"] = health

    def kill(self, name: str) -> None:
        self.containers[name]["status"] = "exited"

    # -- containers -------------------------------------------------------
    def inspect(self, name: str) -> ContainerState | None:
        c = self.containers.get(name)
        if c is None:
            return None
        return ContainerState(name, c["id"], c["status"], c["health"], dict(c["labels"]))

    def list_names(self, prefix: str) -> list[str]:
        return sorted(n for n in self.containers if n.startswith(prefix))

    def run(self, name: str, service: Service, labels: dict[str, str]) -> str:
        self.calls.append(("run", name))
        if self.fail_run:
            raise EngineError(self.fail_run)
        if name in self.containers:
            raise EngineError(f"container name {name} already in use")
        for volume in service.volume_names:
            self.vol(volume).mkdir(exist_ok=True)
        self._next_id += 1
        self.containers[name] = {
            "id": f"{self._next_id:012d}",
            "status": "running",
            "health": self.health_on_start(self, service),
            "labels": labels,
        }
        return self.containers[name]["id"]

    def stop(self, name: str, timeout: int) -> None:
        self.calls.append(("stop", name))
        if name in self.containers:
            self.containers[name]["status"] = "exited"

    def remove(self, name: str) -> None:
        self.calls.append(("remove", name))
        self.containers.pop(name, None)

    def rename(self, old: str, new: str) -> None:
        self.calls.append(("rename", old, new))
        if new in self.containers:
            raise EngineError(f"container name {new} already in use")
        self.containers[new] = self.containers.pop(old)

    def pause(self, name: str) -> None:
        self.calls.append(("pause", name))
        self.containers[name]["status"] = "paused"

    def unpause(self, name: str) -> None:
        self.calls.append(("unpause", name))
        self.containers[name]["status"] = "running"

    def exec(self, name: str, command: list[str], timeout: float) -> int:
        return 0 if self.containers[name]["health"] == "healthy" else 1

    def logs(self, name: str, tail: int) -> str:
        return f"logs of {name}\n"

    def run_once(self, image: str, mounts: dict[str, str], command: list[str]) -> int:
        self.calls.append(("run_once", image, tuple(command)))
        return self.run_once_result(mounts, command)

    # -- volumes ----------------------------------------------------------
    def volume_exists(self, volume: str) -> bool:
        return self.vol(volume).is_dir()

    def volume_create(self, volume: str) -> None:
        self.vol(volume).mkdir(exist_ok=True)

    def volume_remove(self, volume: str) -> None:
        self.calls.append(("volume_remove", volume))
        shutil.rmtree(self.vol(volume), ignore_errors=True)

    def volume_clear(self, volume: str) -> None:
        shutil.rmtree(self.vol(volume))
        self.vol(volume).mkdir()

    def volume_export(self, volume: str, out: BinaryIO) -> None:
        if not self.volume_exists(volume):
            raise EngineError(f"no such volume {volume}")
        with tarfile.open(fileobj=out, mode="w|") as tar:
            tar.add(self.vol(volume), arcname=".")

    def volume_import(self, volume: str, src: BinaryIO) -> None:
        with tarfile.open(fileobj=src, mode="r|") as tar:
            tar.extractall(self.vol(volume), filter="data")
        if self.corrupt_import:
            first = sorted(p for p in self.vol(volume).rglob("*") if p.is_file())[0]
            first.write_bytes(b"corrupted during restore")

    def volume_manifest(self, volume: str) -> dict[str, str]:
        base = self.vol(volume)
        return {
            str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(base.rglob("*")) if p.is_file()
        }
