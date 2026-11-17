"""Volume backup, restore testing, and restore.

A backup is not trusted until it has been *restore-tested*: the archive is
restored into a scratch volume, every file is hashed, and the result is
compared with the manifest recorded when the backup was taken. Only backups
that pass are used for automatic recovery.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import tarfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Config, Service
from .runtime import EngineError, Runtime, normalize_path
from .state import EventLog, write_json_atomic


class StorageError(Exception):
    """A backup, verification, or restore failed."""


@dataclass
class BackupRecord:
    id: str
    service: str
    created_at: float
    volumes: dict[str, dict] = field(default_factory=dict)  # name -> archive, sha256, bytes, files
    verified: bool = False
    verified_at: float | None = None
    verify_detail: str = "not yet restore-tested"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def archive_manifest(path: Path) -> dict[str, str]:
    """Per-file SHA-256 of every regular file inside a .tar.gz archive."""
    manifest: dict[str, str] = {}
    with tarfile.open(path, "r:gz") as tar:
        for member in tar:
            if not (member.isfile() or member.islnk()):
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            digest = hashlib.sha256()
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
            manifest[normalize_path(member.name)] = digest.hexdigest()
    return manifest


def diff_manifests(expected: dict[str, str], actual: dict[str, str]) -> list[str]:
    problems = []
    for path in sorted(expected.keys() - actual.keys()):
        problems.append(f"missing: {path}")
    for path in sorted(actual.keys() - expected.keys()):
        problems.append(f"unexpected: {path}")
    for path in sorted(expected.keys() & actual.keys()):
        if expected[path] != actual[path]:
            problems.append(f"content differs: {path}")
    return problems


class StorageManager:
    def __init__(
        self,
        config: Config,
        runtime: Runtime,
        events: EventLog,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.runtime = runtime
        self.events = events
        self.clock = clock

    # -- catalogue --------------------------------------------------------
    def _dir(self, service: Service) -> Path:
        return self.config.backup_dir / service.name

    def _load(self, path: Path) -> BackupRecord | None:
        try:
            return BackupRecord(**json.loads((path / "backup.json").read_text()))
        except (OSError, ValueError, TypeError):
            return None

    def _save(self, service: Service, record: BackupRecord) -> None:
        write_json_atomic(self._dir(service) / record.id / "backup.json", asdict(record))

    def list(self, service: Service) -> list[BackupRecord]:
        """All complete backups, newest first."""
        root = self._dir(service)
        if not root.is_dir():
            return []
        records = [self._load(p) for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
        return sorted((r for r in records if r), key=lambda r: (r.created_at, r.id), reverse=True)

    def get(self, service: Service, backup_id: str | None) -> BackupRecord:
        records = self.list(service)
        if not records:
            raise StorageError(f"{service.name}: no backups exist")
        if backup_id is None:
            return records[0]
        for record in records:
            if record.id == backup_id:
                return record
        raise StorageError(f"{service.name}: no backup with id {backup_id}")

    def latest_verified(self, service: Service) -> BackupRecord | None:
        return next((r for r in self.list(service) if r.verified), None)

    def _manifest(self, service: Service, record: BackupRecord, volume: str) -> dict[str, str]:
        path = self._dir(service) / record.id / f"{volume}.manifest.json"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise StorageError(f"{service.name}: manifest for {volume} unreadable: {exc}") from None

    # -- backup -----------------------------------------------------------
    def _new_id(self, service: Service) -> str:
        stamp = datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate, n = stamp, 1
        while (self._dir(service) / candidate).exists():
            n += 1
            candidate = f"{stamp}-{n}"
        return candidate

    def _export(self, volume: str, dest: Path) -> None:
        with gzip.open(dest, "wb", compresslevel=6) as gz:
            self.runtime.volume_export(volume, gz)

    def backup(self, service: Service, verify: bool | None = None) -> BackupRecord:
        if not service.volumes:
            raise StorageError(f"{service.name}: service has no volumes to back up")
        for volume in service.volume_names:
            if not self.runtime.volume_exists(volume):
                raise StorageError(f"{service.name}: volume {volume} does not exist")

        backup_id = self._new_id(service)
        final = self._dir(service) / backup_id
        partial = self._dir(service) / f".{backup_id}.partial"
        partial.mkdir(parents=True)
        record = BackupRecord(id=backup_id, service=service.name, created_at=self.clock())

        paused = False
        try:
            if service.backup.pause:
                state = self.runtime.inspect(service.container_name)
                if state and state.running:
                    self.runtime.pause(service.container_name)
                    paused = True
            try:
                for volume in service.volume_names:
                    self._export(volume, partial / f"{volume}.tar.gz")
            finally:
                if paused:
                    self.runtime.unpause(service.container_name)

            for volume in service.volume_names:
                archive = partial / f"{volume}.tar.gz"
                manifest = archive_manifest(archive)
                write_json_atomic(partial / f"{volume}.manifest.json", manifest)
                record.volumes[volume] = {
                    "archive": archive.name,
                    "sha256": file_sha256(archive),
                    "bytes": archive.stat().st_size,
                    "files": len(manifest),
                }
            write_json_atomic(partial / "backup.json", asdict(record))
            partial.rename(final)
        except (EngineError, OSError, tarfile.TarError) as exc:
            shutil.rmtree(partial, ignore_errors=True)
            self.events.emit("backup_failed", service.name, error=str(exc))
            raise StorageError(f"{service.name}: backup failed: {exc}") from exc

        self.events.emit(
            "backup_created", service.name, backup=backup_id,
            files=sum(v["files"] for v in record.volumes.values()),
        )
        if service.backup.verify if verify is None else verify:
            self.verify(service, backup_id)
            record = self.get(service, backup_id)
        self.prune(service)
        return record

    # -- restore test -----------------------------------------------------
    def _check_archives(self, service: Service, record: BackupRecord) -> list[str]:
        problems = []
        for volume, info in record.volumes.items():
            archive = self._dir(service) / record.id / info["archive"]
            if not archive.is_file():
                problems.append(f"{volume}: archive file is missing")
            elif file_sha256(archive) != info["sha256"]:
                problems.append(f"{volume}: archive checksum mismatch (file changed since backup)")
        return problems

    def _import(self, service: Service, record: BackupRecord, volume: str, target: str) -> None:
        archive = self._dir(service) / record.id / record.volumes[volume]["archive"]
        with gzip.open(archive, "rb") as gz:
            self.runtime.volume_import(target, gz)

    def verify(self, service: Service, backup_id: str | None = None) -> BackupRecord:
        """Restore-test a backup into scratch volumes and record the outcome."""
        record = self.get(service, backup_id)
        problems = self._check_archives(service, record)
        scratch: dict[str, str] = {}  # scratch volume -> mount path
        try:
            if not problems:
                mounts = service.volume_mounts()
                for volume in record.volumes:
                    target = f"crp-verify-{service.name}-{volume}-{record.id.lower()}"
                    self.runtime.volume_create(target)
                    scratch[target] = mounts.get(volume, "/data")
                    self._import(service, record, volume, target)
                    actual = self.runtime.volume_manifest(target)
                    diff = diff_manifests(self._manifest(service, record, volume), actual)
                    problems += [f"{volume}: {p}" for p in diff]
                if not problems and service.backup.verify_command:
                    rc = self.runtime.run_once(
                        service.image, scratch, list(service.backup.verify_command)
                    )
                    if rc != 0:
                        problems.append(f"verify_command exited with code {rc}")
        except (EngineError, OSError, EOFError, tarfile.TarError, StorageError) as exc:
            problems.append(f"restore test could not complete: {exc}")
        finally:
            for target in scratch:
                try:
                    self.runtime.volume_remove(target)
                except EngineError:
                    pass

        record.verified = not problems
        record.verified_at = self.clock()
        if problems:
            shown = "; ".join(problems[:5])
            more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
            record.verify_detail = shown + more
        else:
            files = sum(v["files"] for v in record.volumes.values())
            record.verify_detail = f"restored and matched {files} file(s)"
        self._save(service, record)
        self.events.emit(
            "backup_verified" if record.verified else "backup_verify_failed",
            service.name, backup=record.id, detail=record.verify_detail,
        )
        return record

    # -- restore ----------------------------------------------------------
    def _snapshot_current(self, service: Service) -> None:
        """Keep a copy of what is about to be overwritten (best effort)."""
        stamp = datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = self._dir(service) / ".pre-restore" / stamp
        dest.mkdir(parents=True, exist_ok=True)
        for volume in service.volume_names:
            if not self.runtime.volume_exists(volume):
                continue
            try:
                self._export(volume, dest / f"{volume}.tar.gz")
            except (EngineError, OSError) as exc:
                self.events.emit("pre_restore_snapshot_failed", service.name, volume=volume, error=str(exc))
        snapshots = sorted(p for p in dest.parent.iterdir() if p.is_dir())
        for old in snapshots[:-3]:
            shutil.rmtree(old, ignore_errors=True)

    def restore(
        self, service: Service, backup_id: str | None = None, allow_unverified: bool = False
    ) -> BackupRecord:
        """Replace the service's volume contents with a backup.

        The caller must make sure no container is writing to the volumes.
        With no ``backup_id`` the newest *verified* backup is used.
        """
        if backup_id is None and not allow_unverified:
            record = self.latest_verified(service)
            if record is None:
                raise StorageError(f"{service.name}: no restore-tested backup available")
        else:
            record = self.get(service, backup_id)
        if not record.verified and not allow_unverified:
            raise StorageError(
                f"{service.name}: backup {record.id} has not passed a restore test "
                f"({record.verify_detail})"
            )
        problems = self._check_archives(service, record)
        if problems:
            raise StorageError(f"{service.name}: backup {record.id} unusable: {'; '.join(problems)}")

        self._snapshot_current(service)
        try:
            for volume in record.volumes:
                if self.runtime.volume_exists(volume):
                    self.runtime.volume_clear(volume)
                else:
                    self.runtime.volume_create(volume)
                self._import(service, record, volume, volume)
                diff = diff_manifests(
                    self._manifest(service, record, volume), self.runtime.volume_manifest(volume)
                )
                if diff:
                    raise StorageError(f"volume {volume} does not match backup after restore: {diff[0]}")
        except (EngineError, OSError, EOFError, tarfile.TarError, StorageError) as exc:
            self.events.emit("restore_failed", service.name, backup=record.id, error=str(exc))
            raise StorageError(f"{service.name}: restore of {record.id} failed: {exc}") from exc
        self.events.emit("storage_restored", service.name, backup=record.id)
        return record

    # -- retention --------------------------------------------------------
    def prune(self, service: Service) -> list[str]:
        """Drop old backups, always keeping the newest restore-tested one."""
        records = self.list(service)
        keep = {r.id for r in records[: service.backup.keep]}
        newest_verified = self.latest_verified(service)
        if newest_verified:
            keep.add(newest_verified.id)
        removed = []
        for record in records:
            if record.id not in keep:
                shutil.rmtree(self._dir(service) / record.id, ignore_errors=True)
                removed.append(record.id)
        if removed:
            self.events.emit("backups_pruned", service.name, removed=len(removed))
        return removed
