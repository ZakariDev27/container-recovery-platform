"""Command line interface: ``crp``."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from datetime import datetime, timezone

from . import __version__, health
from .config import Config, ConfigError, load
from .runtime import DockerRuntime, EngineError
from .storage import BackupRecord, StorageError
from .supervisor import Supervisor


def _table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "(nothing to show)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    lines = ["  ".join(c.upper().ljust(widths[c]) for c in columns)]
    for row in rows:
        lines.append("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns))
    return "\n".join(line.rstrip() for line in lines)


def _when(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _backup_row(record: BackupRecord) -> dict:
    return {
        "service": record.service,
        "backup": record.id,
        "files": sum(v["files"] for v in record.volumes.values()),
        "bytes": sum(v["bytes"] for v in record.volumes.values()),
        "restore-tested": "PASS" if record.verified else ("FAIL" if record.verified_at else "no"),
        "detail": record.verify_detail,
    }


BACKUP_COLUMNS = ["service", "backup", "files", "bytes", "restore-tested", "detail"]


def cmd_up(sup: Supervisor, args) -> int:
    results = sup.up(args.services)
    for name, ok in results.items():
        print(f"{name}: {'healthy' if ok else 'FAILED to become healthy'}")
    return 0 if all(results.values()) else 1


def cmd_down(sup: Supervisor, args) -> int:
    sup.down(args.services)
    return 0


def cmd_status(sup: Supervisor, args) -> int:
    rows = sup.status()
    print(_table(rows, ["service", "container", "health", "generation",
                        "recent_replacements", "circuit", "verified_backup", "detail"]))
    return 0 if all(r["health"] == "healthy" for r in rows) else 1


def cmd_check(sup: Supervisor, args) -> int:
    """Run health checks only; never changes anything."""
    failed = 0
    for service in sup.config.services.values():
        result = health.check(service, sup.runtime)
        failed += not result.ok
        print(f"{service.name}: {'ok' if result.ok else 'FAIL'} - {result.detail}")
    return 1 if failed else 0


def cmd_watch(sup: Supervisor, args) -> int:
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    if args.once:
        for name, outcome in sup.tick().items():
            print(f"{name}: {outcome}")
        return 0
    sup.run_forever(stop)
    return 0


def cmd_replace(sup: Supervisor, args) -> int:
    service = sup.config.service(args.service)
    ok = sup.replacer.replace(service, "manual replace")
    sup.state.save()
    print(f"{service.name}: {'replaced, healthy' if ok else 'replacement FAILED'}")
    return 0 if ok else 1


def cmd_backup(sup: Supervisor, args) -> int:
    service = sup.config.service(args.service)
    record = sup.storage.backup(service, verify=False if args.no_verify else None)
    print(_table([_backup_row(record)], BACKUP_COLUMNS))
    return 0 if (record.verified or args.no_verify or not service.backup.verify) else 1


def cmd_backups(sup: Supervisor, args) -> int:
    rows = []
    for service in sup._select(args.services):
        rows += [_backup_row(r) for r in sup.storage.list(service)]
    print(_table(rows, BACKUP_COLUMNS))
    return 0


def cmd_verify(sup: Supervisor, args) -> int:
    record = sup.storage.verify(sup.config.service(args.service), args.backup)
    print(_table([_backup_row(record)], BACKUP_COLUMNS))
    return 0 if record.verified else 1


def cmd_restore(sup: Supervisor, args) -> int:
    """Stop the service, restore its volumes, start a replacement container."""
    service = sup.config.service(args.service)
    record = (sup.storage.get(service, args.backup) if args.backup
              else sup.storage.latest_verified(service))
    if record is None:
        raise StorageError(f"{service.name}: no restore-tested backup available")
    if not record.verified and not args.allow_unverified:
        raise StorageError(
            f"{service.name}: backup {record.id} has not passed a restore test; "
            "run 'crp verify' or pass --allow-unverified"
        )
    restored: list[bool] = []

    def restore():
        sup.storage.restore(service, record.id, allow_unverified=args.allow_unverified)
        restored.append(True)

    ok = sup.replacer.replace(service, f"manual restore of {record.id}", restore)
    sup.state.save()
    if not restored:
        print(f"{service.name}: restore of {record.id} FAILED (see events)")
        return 1
    print(f"{service.name}: restored {record.id}; container {'healthy' if ok else 'NOT healthy'}")
    return 0 if ok else 1


def cmd_drill(sup: Supervisor, args) -> int:
    """Recovery drill: take a fresh backup of each service and restore-test it."""
    rows, failed = [], 0
    for service in sup._select(args.services):
        if not service.volumes:
            continue
        try:
            record = sup.storage.backup(service, verify=True)
        except StorageError as exc:
            failed += 1
            rows.append({"service": service.name, "backup": "-", "restore-tested": "FAIL", "detail": str(exc)})
            continue
        failed += not record.verified
        rows.append(_backup_row(record))
    print(_table(rows, BACKUP_COLUMNS))
    return 1 if failed else 0


def cmd_reset(sup: Supervisor, args) -> int:
    sup.reset(args.service)
    print(f"{args.service}: circuit closed, failure history cleared")
    return 0


def cmd_events(sup: Supervisor, args) -> int:
    path = sup.config.state_dir / "events.jsonl"
    if not path.exists():
        print("(no events yet)")
        return 0
    for line in path.read_text().splitlines()[-args.n:]:
        event = json.loads(line)
        ts, kind, service = event.pop("time"), event.pop("event"), event.pop("service")
        extras = " ".join(f"{k}={v}" for k, v in event.items())
        print(f"{_when(ts)}  {service:<12} {kind:<24} {extras}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crp", description="Container recovery platform")
    parser.add_argument("-c", "--config", default="recovery.toml", help="config file (default: recovery.toml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every event to stderr")
    parser.add_argument("--version", action="version", version=f"crp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(func=func)
        return p

    add("up", cmd_up, "start services that are not running").add_argument("services", nargs="*")
    add("down", cmd_down, "stop and remove service containers (volumes are kept)").add_argument("services", nargs="*")
    add("status", cmd_status, "show container, health, and backup state")
    add("check", cmd_check, "run health checks once, change nothing")
    add("watch", cmd_watch, "supervise: check, replace, back up").add_argument(
        "--once", action="store_true", help="do a single pass and exit")
    add("replace", cmd_replace, "replace a service's container now").add_argument("service")
    p = add("backup", cmd_backup, "back up a service's volumes")
    p.add_argument("service")
    p.add_argument("--no-verify", action="store_true", help="skip the restore test")
    add("backups", cmd_backups, "list backups").add_argument("services", nargs="*")
    p = add("verify", cmd_verify, "restore-test a backup into scratch volumes")
    p.add_argument("service")
    p.add_argument("backup", nargs="?", help="backup id (default: newest)")
    p = add("restore", cmd_restore, "restore volumes from a backup and start a replacement")
    p.add_argument("service")
    p.add_argument("backup", nargs="?", help="backup id (default: newest restore-tested)")
    p.add_argument("--allow-unverified", action="store_true")
    add("drill", cmd_drill, "recovery drill: back up and restore-test").add_argument("services", nargs="*")
    add("reset", cmd_reset, "close a service's circuit after manual repair").add_argument("service")
    add("events", cmd_events, "show recent events").add_argument("-n", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if (args.verbose or args.command == "watch") else logging.WARNING,
        format="%(asctime)s %(message)s", stream=sys.stderr,
    )
    try:
        config: Config = load(args.config)
        sup = Supervisor(config, DockerRuntime(config.helper_image))
        return args.func(sup, args)
    except (ConfigError, StorageError, EngineError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
