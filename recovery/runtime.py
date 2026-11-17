"""Container runtime adapter.

Everything the platform needs from a container engine goes through the
``Runtime`` interface, so the recovery logic can be exercised without Docker.
``DockerRuntime`` implements it with the ``docker`` command line client.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import BinaryIO, Protocol

from .config import Service


class EngineError(Exception):
    """A container engine operation failed."""


@dataclass(frozen=True)
class ContainerState:
    name: str
    id: str
    status: str            # running | exited | created | paused | dead | ...
    health: str | None     # healthy | unhealthy | starting | None (no HEALTHCHECK)
    labels: dict[str, str]

    @property
    def running(self) -> bool:
        return self.status == "running"


class Runtime(Protocol):
    def inspect(self, name: str) -> ContainerState | None: ...
    def list_names(self, prefix: str) -> list[str]: ...
    def run(self, name: str, service: Service, labels: dict[str, str]) -> str: ...
    def stop(self, name: str, timeout: int) -> None: ...
    def remove(self, name: str) -> None: ...
    def rename(self, old: str, new: str) -> None: ...
    def pause(self, name: str) -> None: ...
    def unpause(self, name: str) -> None: ...
    def exec(self, name: str, command: list[str], timeout: float) -> int: ...
    def logs(self, name: str, tail: int) -> str: ...
    def run_once(self, image: str, mounts: dict[str, str], command: list[str]) -> int: ...
    def volume_exists(self, volume: str) -> bool: ...
    def volume_create(self, volume: str) -> None: ...
    def volume_remove(self, volume: str) -> None: ...
    def volume_clear(self, volume: str) -> None: ...
    def volume_export(self, volume: str, out: BinaryIO) -> None: ...
    def volume_import(self, volume: str, src: BinaryIO) -> None: ...
    def volume_manifest(self, volume: str) -> dict[str, str]: ...


def parse_sha256sum(output: str) -> dict[str, str]:
    """Parse ``sha256sum`` output into {relative path: hex digest}."""
    manifest: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        digest, _, path = line.partition("  ")
        if not path:  # busybox may use a single space + '*' in binary mode
            digest, _, path = line.partition(" ")
            path = path.lstrip("*")
        manifest[normalize_path(path)] = digest.lstrip("\\")
    return manifest


def normalize_path(path: str) -> str:
    while path.startswith("./"):
        path = path[2:]
    return path


class DockerRuntime:
    def __init__(self, helper_image: str = "alpine:3.20", binary: str = "docker"):
        self.helper_image = helper_image
        self.binary = binary

    # -- plumbing ---------------------------------------------------------
    def _docker(self, *args: str, check: bool = True, timeout: float | None = None):
        try:
            proc = subprocess.run(
                [self.binary, *args], capture_output=True, text=True, timeout=timeout
            )
        except FileNotFoundError:
            raise EngineError(f"'{self.binary}' not found on PATH") from None
        except subprocess.TimeoutExpired:
            raise EngineError(f"docker {args[0]} timed out after {timeout}s") from None
        if check and proc.returncode != 0:
            raise EngineError(
                f"docker {' '.join(args[:2])} failed: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc

    # -- containers -------------------------------------------------------
    def inspect(self, name: str) -> ContainerState | None:
        proc = self._docker("container", "inspect", "--format", "{{json .}}", name, check=False)
        if proc.returncode != 0:
            if "no such" in proc.stderr.lower():
                return None
            raise EngineError(f"docker inspect {name} failed: {proc.stderr.strip()}")
        data = json.loads(proc.stdout)
        state = data.get("State", {})
        health = (state.get("Health") or {}).get("Status")
        return ContainerState(
            name=name,
            id=data.get("Id", "")[:12],
            status=state.get("Status", "unknown"),
            health=health,
            labels=(data.get("Config", {}).get("Labels") or {}),
        )

    def list_names(self, prefix: str) -> list[str]:
        proc = self._docker("ps", "-a", "--filter", f"name=^{prefix}", "--format", "{{.Names}}")
        return sorted(n for n in proc.stdout.split() if n.startswith(prefix))

    def run(self, name: str, service: Service, labels: dict[str, str]) -> str:
        args = ["run", "-d", "--name", name]
        for key, value in labels.items():
            args += ["--label", f"{key}={value}"]
        for port in service.ports:
            args += ["-p", port]
        for key, value in service.env.items():
            args += ["-e", f"{key}={value}"]
        for volume in service.volumes:
            args += ["-v", volume]
        if service.network:
            args += ["--network", service.network]
        args += list(service.extra_args)
        args.append(service.image)
        args += list(service.command)
        return self._docker(*args).stdout.strip()[:12]

    def stop(self, name: str, timeout: int) -> None:
        self._docker("stop", "-t", str(timeout), name, check=False)

    def remove(self, name: str) -> None:
        self._docker("rm", "-f", name, check=False)

    def rename(self, old: str, new: str) -> None:
        self._docker("rename", old, new)

    def pause(self, name: str) -> None:
        self._docker("pause", name)

    def unpause(self, name: str) -> None:
        self._docker("unpause", name)

    def exec(self, name: str, command: list[str], timeout: float) -> int:
        try:
            return self._docker("exec", name, *command, check=False, timeout=timeout).returncode
        except EngineError:
            return 124

    def logs(self, name: str, tail: int) -> str:
        proc = self._docker("logs", "--tail", str(tail), name, check=False)
        return proc.stdout + proc.stderr

    def run_once(self, image: str, mounts: dict[str, str], command: list[str]) -> int:
        args = ["run", "--rm"]
        for volume, path in mounts.items():
            args += ["-v", f"{volume}:{path}"]
        return self._docker(*args, image, *command, check=False).returncode

    # -- volumes ----------------------------------------------------------
    def volume_exists(self, volume: str) -> bool:
        return self._docker("volume", "inspect", volume, check=False).returncode == 0

    def volume_create(self, volume: str) -> None:
        self._docker("volume", "create", volume)

    def volume_remove(self, volume: str) -> None:
        self._docker("volume", "rm", "-f", volume)

    def _helper(self, volume: str, script: str, readonly: bool = False) -> list[str]:
        mount = f"{volume}:/data" + (":ro" if readonly else "")
        return [self.binary, "run", "--rm", "-i", "-v", mount, self.helper_image, "sh", "-c", script]

    def volume_clear(self, volume: str) -> None:
        proc = subprocess.run(
            self._helper(volume, "find /data -mindepth 1 -delete"), capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise EngineError(f"clearing volume {volume} failed: {proc.stderr.strip()}")

    def volume_export(self, volume: str, out: BinaryIO) -> None:
        """Write an uncompressed tar of the volume's contents to ``out``."""
        cmd = self._helper(volume, "tar -C /data -cf - .", readonly=True)
        with tempfile.TemporaryFile() as err:
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err) as proc:
                assert proc.stdout is not None
                shutil.copyfileobj(proc.stdout, out, 1 << 20)
            if proc.returncode != 0:
                err.seek(0)
                detail = err.read().decode(errors="replace").strip()
                raise EngineError(f"exporting volume {volume} failed: {detail}")

    def volume_import(self, volume: str, src: BinaryIO) -> None:
        """Extract an uncompressed tar stream from ``src`` into the volume."""
        cmd = self._helper(volume, "tar -C /data -xf -")
        with tempfile.TemporaryFile() as err:
            with subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=err) as proc:
                assert proc.stdin is not None
                try:
                    shutil.copyfileobj(src, proc.stdin, 1 << 20)
                except BrokenPipeError:
                    pass  # helper exited early; its exit code and stderr say why
            if proc.returncode != 0:
                err.seek(0)
                detail = err.read().decode(errors="replace").strip()
                raise EngineError(f"importing into volume {volume} failed: {detail}")

    def volume_manifest(self, volume: str) -> dict[str, str]:
        script = "cd /data && find . -type f -exec sha256sum {} +"
        proc = subprocess.run(self._helper(volume, script, readonly=True), capture_output=True, text=True)
        if proc.returncode != 0:
            raise EngineError(f"hashing volume {volume} failed: {proc.stderr.strip()}")
        return parse_sha256sum(proc.stdout)
