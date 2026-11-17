"""Configuration loading and validation (TOML)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HEALTH_TYPES = ("http", "tcp", "exec", "docker")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ConfigError(ValueError):
    """Raised when the configuration file is invalid."""


@dataclass(frozen=True)
class HealthConfig:
    type: str = "docker"
    url: str = ""                 # http
    expect_status: tuple[int, int] = (200, 399)  # http, inclusive range
    host: str = "127.0.0.1"       # tcp
    port: int = 0                 # tcp
    command: tuple[str, ...] = () # exec (runs inside the container)
    timeout: float = 3.0
    failure_threshold: int = 3
    start_timeout: float = 60.0   # a new container must pass a check within this


@dataclass(frozen=True)
class RecoveryConfig:
    max_replacements: int = 3     # per window, before the circuit opens
    window: float = 600.0         # seconds
    restore_storage_after: int = 2  # replacements in window before volumes are restored
    keep_failed: int = 1          # failed containers kept for forensics
    stop_timeout: int = 10


@dataclass(frozen=True)
class BackupConfig:
    interval: float = 0.0         # seconds; 0 disables scheduled backups
    keep: int = 5
    verify: bool = True           # restore-test every backup right after taking it
    pause: bool = False           # pause the container while volumes are exported
    verify_command: tuple[str, ...] = ()  # optional app-level check on restored data


@dataclass(frozen=True)
class Service:
    name: str
    image: str
    command: tuple[str, ...] = ()
    ports: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    volumes: tuple[str, ...] = ()  # "volume-name:/path/in/container[:ro]"
    network: str = ""
    extra_args: tuple[str, ...] = ()
    health: HealthConfig = HealthConfig()
    recovery: RecoveryConfig = RecoveryConfig()
    backup: BackupConfig = BackupConfig()

    @property
    def container_name(self) -> str:
        return f"crp-{self.name}"

    @property
    def volume_names(self) -> tuple[str, ...]:
        return tuple(v.split(":", 1)[0] for v in self.volumes)

    def volume_mounts(self) -> dict[str, str]:
        """Map of volume name -> mount path inside the container."""
        return {v.split(":")[0]: v.split(":")[1] for v in self.volumes}


@dataclass(frozen=True)
class Config:
    services: dict[str, Service]
    state_dir: Path = Path("./state")
    backup_dir: Path = Path("./backups")
    check_interval: float = 5.0
    helper_image: str = "alpine:3.20"

    def service(self, name: str) -> Service:
        try:
            return self.services[name]
        except KeyError:
            known = ", ".join(sorted(self.services)) or "(none)"
            raise ConfigError(f"unknown service '{name}'; configured: {known}") from None


def _section(cls, raw: dict, where: str):
    known = set(cls.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(sorted(unknown))}")
    values = {}
    for key, value in raw.items():
        if isinstance(value, list):
            value = tuple(value)
        values[key] = value
    return cls(**values)


def _parse_service(name: str, raw: dict) -> Service:
    where = f"services.{name}"
    if not _NAME_RE.match(name):
        raise ConfigError(f"{where}: name must match {_NAME_RE.pattern}")
    raw = dict(raw)
    if not raw.get("image"):
        raise ConfigError(f"{where}: 'image' is required")

    health = _section(HealthConfig, raw.pop("health", {}), f"{where}.health")
    recovery = _section(RecoveryConfig, raw.pop("recovery", {}), f"{where}.recovery")
    backup = _section(BackupConfig, raw.pop("backup", {}), f"{where}.backup")

    if health.type not in HEALTH_TYPES:
        raise ConfigError(f"{where}.health.type must be one of {HEALTH_TYPES}")
    if health.type == "http" and not health.url:
        raise ConfigError(f"{where}.health: http check needs 'url'")
    if health.type == "tcp" and not health.port:
        raise ConfigError(f"{where}.health: tcp check needs 'port'")
    if health.type == "exec" and not health.command:
        raise ConfigError(f"{where}.health: exec check needs 'command'")
    if len(health.expect_status) != 2:
        raise ConfigError(f"{where}.health.expect_status must be [low, high]")
    if health.failure_threshold < 1:
        raise ConfigError(f"{where}.health.failure_threshold must be >= 1")
    if recovery.max_replacements < 1:
        raise ConfigError(f"{where}.recovery.max_replacements must be >= 1")
    if backup.keep < 1:
        raise ConfigError(f"{where}.backup.keep must be >= 1")

    for vol in raw.get("volumes", []):
        parts = vol.split(":")
        if len(parts) not in (2, 3) or not parts[0] or not parts[1].startswith("/"):
            raise ConfigError(f"{where}.volumes: '{vol}' must be 'name:/path[:ro]'")
        if "/" in parts[0] or parts[0].startswith("."):
            raise ConfigError(
                f"{where}.volumes: '{vol}' must use a named volume, not a host path"
            )

    svc = _section(Service, {**raw, "name": name}, where)
    return Service(**{**svc.__dict__, "health": health, "recovery": recovery, "backup": backup})


def load(path: str | Path) -> Config:
    path = Path(path)
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    return parse(raw, base=path.parent)


def parse(raw: dict, base: Path = Path(".")) -> Config:
    platform = dict(raw.get("platform", {}))
    services_raw = raw.get("services", {})
    if not services_raw:
        raise ConfigError("no services defined")
    services = {name: _parse_service(name, body) for name, body in services_raw.items()}

    allowed = {"state_dir", "backup_dir", "check_interval", "helper_image"}
    unknown = set(platform) - allowed
    if unknown:
        raise ConfigError(f"platform: unknown key(s): {', '.join(sorted(unknown))}")
    for key in ("state_dir", "backup_dir"):
        if key in platform:
            platform[key] = (base / platform[key]).resolve()
        else:
            platform[key] = (base / Config.__dataclass_fields__[key].default).resolve()
    return Config(services=services, **platform)
