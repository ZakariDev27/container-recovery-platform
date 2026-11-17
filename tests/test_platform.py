"""Tests for health checks, replacement, and storage recovery (no Docker needed)."""

from __future__ import annotations

import gzip
import http.server
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from recovery import config as cfg
from recovery import health
from recovery.runtime import parse_sha256sum
from recovery.state import EventLog, service_lock
from recovery.storage import StorageError, archive_manifest, diff_manifests
from recovery.supervisor import Supervisor

from .fake_runtime import FakeRuntime


class Clock:
    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def make(tmp: Path, **service_overrides):
    """Build a one-service platform ('db', with a data volume) on a fake engine."""
    service = {
        "image": "example/db:1",
        "volumes": ["db-data:/var/lib/data"],
        "health": {"type": "docker", "failure_threshold": 3, "start_timeout": 5},
        "recovery": {"max_replacements": 3, "window": 600, "restore_storage_after": 2, "keep_failed": 1},
        "backup": {"keep": 3},
    }
    for key, value in service_overrides.items():
        if isinstance(value, dict):
            service[key] = {**service.get(key, {}), **value}
        else:
            service[key] = value
    config = cfg.parse(
        {"platform": {"state_dir": "state", "backup_dir": "backups"}, "services": {"db": service}},
        base=tmp,
    )
    clock = Clock()
    runtime = FakeRuntime(tmp / "volumes")
    sup = Supervisor(config, runtime, clock=clock, sleep=clock.sleep,
                     events=EventLog(tmp / "state" / "events.jsonl", clock))
    return sup, runtime, clock, config.service("db")


def seed(runtime: FakeRuntime, volume: str = "db-data") -> None:
    base = runtime.vol(volume)
    (base / "tables").mkdir(parents=True, exist_ok=True)
    (base / "MANIFEST").write_text("schema v7\n")
    (base / "tables" / "users.dat").write_bytes(b"alice\nbob\n" * 100)
    (base / "tables" / "orders.dat").write_bytes(bytes(range(256)) * 20)


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class ConfigTests(TmpCase):
    def test_example_config_loads(self):
        example = Path(__file__).resolve().parent.parent / "examples" / "recovery.toml"
        config = cfg.load(example)
        self.assertIn("web", config.services)
        self.assertEqual(config.service("web").container_name, "crp-web")

    def test_rejects_bad_input(self):
        bad = [
            {},  # no services
            {"services": {"a": {}}},  # no image
            {"services": {"a": {"image": "x", "health": {"type": "ping"}}}},
            {"services": {"a": {"image": "x", "health": {"type": "http"}}}},
            {"services": {"a": {"image": "x", "volumes": ["/host/path:/data"]}}},
            {"services": {"a": {"image": "x", "typo": 1}}},
            {"services": {"Bad Name": {"image": "x"}}},
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(cfg.ConfigError):
                cfg.parse(raw)


class HealthCheckTests(TmpCase):
    def _service(self, health_cfg: dict):
        sup, runtime, _, _ = make(self.tmp, health=health_cfg)
        sup.up()
        return sup.config.service("db"), runtime

    def test_http_check_against_real_server(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/ok" else 503)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]

        ok = health._http(f"http://127.0.0.1:{port}/ok", (200, 399), 2)
        bad = health._http(f"http://127.0.0.1:{port}/down", (200, 399), 2)
        self.assertTrue(ok.ok)
        self.assertFalse(bad.ok)
        self.assertIn("503", bad.detail)

    def test_http_check_ignores_proxy_settings(self):
        import os
        from unittest import mock

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        dead_proxy = {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
                      "no_proxy": "", "NO_PROXY": ""}
        with mock.patch.dict(os.environ, dead_proxy):
            result = health._http(f"http://127.0.0.1:{server.server_address[1]}/", (200, 399), 2)
        self.assertTrue(result.ok, result.detail)

    def test_tcp_check(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        self.assertTrue(health._tcp("127.0.0.1", port, 2).ok)
        listener.close()
        self.assertFalse(health._tcp("127.0.0.1", port, 1).ok)

    def test_docker_and_exec_checks_follow_container_state(self):
        for health_cfg in ({"type": "docker"}, {"type": "exec", "command": ["true"]}):
            with self.subTest(type=health_cfg["type"]):
                self.setUp()
                service, runtime = self._service(health_cfg)
                self.assertTrue(health.check(service, runtime).ok)
                runtime.set_health("crp-db", "unhealthy")
                self.assertFalse(health.check(service, runtime).ok)

    def test_stopped_or_missing_container_is_down(self):
        service, runtime = self._service({"type": "docker"})
        runtime.kill("crp-db")
        result = health.check(service, runtime)
        self.assertFalse(result.ok)
        self.assertTrue(result.down)
        runtime.remove("crp-db")
        self.assertTrue(health.check(service, runtime).down)


class ReplacementTests(TmpCase):
    def test_replaces_only_after_failure_threshold(self):
        sup, runtime, _, _ = make(self.tmp)
        sup.up()
        first_id = runtime.inspect("crp-db").id
        runtime.set_health("crp-db", "unhealthy")

        self.assertEqual(sup.tick()["db"], "unhealthy")
        self.assertEqual(sup.tick()["db"], "unhealthy")
        self.assertEqual(runtime.inspect("crp-db").id, first_id)
        self.assertEqual(sup.tick()["db"], "replaced")

        new = runtime.inspect("crp-db")
        self.assertNotEqual(new.id, first_id)
        self.assertEqual(new.labels["crp.generation"], "2")
        self.assertEqual(sup.tick()["db"], "healthy")

    def test_single_blip_does_not_replace(self):
        sup, runtime, _, _ = make(self.tmp)
        sup.up()
        first_id = runtime.inspect("crp-db").id
        runtime.set_health("crp-db", "unhealthy")
        sup.tick()
        runtime.set_health("crp-db", "healthy")
        sup.tick()
        runtime.set_health("crp-db", "unhealthy")
        sup.tick()
        sup.tick()
        self.assertEqual(runtime.inspect("crp-db").id, first_id)

    def test_dead_container_is_replaced_immediately(self):
        sup, runtime, _, _ = make(self.tmp)
        sup.up()
        runtime.kill("crp-db")
        self.assertEqual(sup.tick()["db"], "replaced")
        self.assertTrue(runtime.inspect("crp-db").running)

    def test_missing_container_is_recreated(self):
        sup, runtime, _, _ = make(self.tmp)
        sup.up()
        runtime.remove("crp-db")
        self.assertEqual(sup.tick()["db"], "replaced")
        self.assertTrue(runtime.inspect("crp-db").running)

    def test_failed_container_kept_for_forensics_and_pruned(self):
        sup, runtime, _, _ = make(self.tmp)
        sup.up()
        runtime.kill("crp-db")
        sup.tick()
        self.assertEqual(runtime.list_names("crp-db-failed-"), ["crp-db-failed-0001"])
        self.assertEqual(runtime.inspect("crp-db-failed-0001").status, "exited")
        self.assertTrue((self.tmp / "state" / "forensics" / "db-g0001.log").exists())

        runtime.kill("crp-db")
        sup.tick()
        self.assertEqual(runtime.list_names("crp-db-failed-"), ["crp-db-failed-0002"])

    def test_volume_data_survives_replacement(self):
        sup, runtime, _, _ = make(self.tmp)
        sup.up()
        seed(runtime)
        before = runtime.volume_manifest("db-data")
        runtime.kill("crp-db")
        sup.tick()
        self.assertEqual(runtime.volume_manifest("db-data"), before)

    def test_circuit_opens_after_max_replacements_and_reset_closes_it(self):
        sup, runtime, _, _ = make(self.tmp, volumes=[])
        sup.up()
        runtime.health_on_start = lambda rt, svc: "unhealthy"
        runtime.kill("crp-db")
        outcomes = [sup.tick()["db"] for _ in range(6)]
        self.assertEqual(outcomes.count("replace-failed"), 3)
        self.assertEqual(outcomes[-1], "circuit-open")
        runs = [c for c in runtime.calls if c[0] == "run"]
        sup.tick()
        self.assertEqual(len([c for c in runtime.calls if c[0] == "run"]), len(runs))
        self.assertIn("circuit_opened", sup.events.kinds("db"))

        runtime.health_on_start = lambda rt, svc: "healthy"
        sup.reset("db")
        self.assertEqual([sup.tick()["db"] for _ in range(3)], ["unhealthy", "unhealthy", "replaced"])

    def test_replacement_budget_refills_after_window(self):
        sup, runtime, clock, _ = make(self.tmp, volumes=[])
        sup.up()
        for _ in range(3):
            runtime.kill("crp-db")
            self.assertEqual(sup.tick()["db"], "replaced")
        clock.now += 601
        runtime.kill("crp-db")
        self.assertEqual(sup.tick()["db"], "replaced")

    def test_history_clears_once_service_stays_healthy(self):
        sup, runtime, clock, _ = make(self.tmp, volumes=[], recovery={"stable_after": 120})
        sup.up()
        for _ in range(5):  # five separate incidents, far more than max_replacements
            runtime.kill("crp-db")
            self.assertEqual(sup.tick()["db"], "replaced")
            sup.tick()
            clock.now += 121
            self.assertEqual(sup.tick()["db"], "healthy")
        self.assertEqual(sup.events.kinds("db").count("incident_closed"), 5)
        self.assertFalse(sup.state.get("db").circuit_open)

    def test_flapping_service_does_not_clear_history(self):
        sup, runtime, clock, _ = make(self.tmp, volumes=[], recovery={"stable_after": 120})
        sup.up()
        outcomes = []
        for _ in range(4):
            runtime.kill("crp-db")
            outcomes.append(sup.tick()["db"])
            sup.tick()
            clock.now += 30  # healthy, but never for long enough
            sup.tick()
        self.assertEqual(outcomes, ["replaced", "replaced", "replaced", "circuit-open"])

    def test_paused_container_is_not_replaced_immediately(self):
        sup, runtime, _, _ = make(self.tmp)
        sup.up()
        first_id = runtime.inspect("crp-db").id
        runtime.pause("crp-db")
        self.assertEqual(sup.tick()["db"], "unhealthy")
        self.assertEqual(runtime.inspect("crp-db").id, first_id)
        self.assertEqual([sup.tick()["db"] for _ in range(2)], ["unhealthy", "replaced"])

    def test_supervisor_leaves_a_service_alone_while_it_is_locked(self):
        sup, runtime, _, service = make(self.tmp)
        sup.up()
        runtime.kill("crp-db")
        with service_lock(sup.config.state_dir, "db") as held:
            self.assertTrue(held)
            self.assertEqual(sup.tick()["db"], "busy")
            self.assertEqual(runtime.inspect("crp-db").status, "exited")
        self.assertEqual(sup.tick()["db"], "replaced")

    def test_engine_failure_on_start_is_reported_not_raised(self):
        sup, runtime, _, _ = make(self.tmp)
        sup.up()
        runtime.kill("crp-db")
        runtime.fail_run = "port is already allocated"
        self.assertEqual(sup.tick()["db"], "replace-failed")
        self.assertIn("replace_failed", sup.events.kinds("db"))

    def test_state_survives_supervisor_restart(self):
        sup, runtime, clock, _ = make(self.tmp, volumes=[])
        sup.up()
        runtime.kill("crp-db")
        sup.tick()
        again = Supervisor(sup.config, runtime, clock=clock, sleep=clock.sleep)
        self.assertEqual(again.state.get("db").generation, 2)
        self.assertEqual(len(again.state.get("db").replacements), 1)


class StorageTests(TmpCase):
    def test_backup_is_restore_tested_and_matches(self):
        sup, runtime, _, service = make(self.tmp)
        sup.up()
        seed(runtime)
        record = sup.storage.backup(service)
        self.assertTrue(record.verified)
        self.assertEqual(record.volumes["db-data"]["files"], 3)
        archive = self.tmp / "backups" / "db" / record.id / "db-data.tar.gz"
        self.assertEqual(archive_manifest(archive), runtime.volume_manifest("db-data"))
        # scratch volume used for the restore test is cleaned up
        self.assertEqual([p.name for p in runtime.root.iterdir()], ["db-data"])

    def test_corrupt_archive_fails_verification(self):
        sup, runtime, _, service = make(self.tmp)
        sup.up()
        seed(runtime)
        record = sup.storage.backup(service, verify=False)
        self.assertFalse(record.verified)
        archive = self.tmp / "backups" / "db" / record.id / "db-data.tar.gz"
        data = bytearray(archive.read_bytes())
        data[len(data) // 2] ^= 0xFF
        archive.write_bytes(bytes(data))

        record = sup.storage.verify(service, record.id)
        self.assertFalse(record.verified)
        self.assertIn("checksum mismatch", record.verify_detail)
        self.assertIsNone(sup.storage.latest_verified(service))

    def test_restore_that_changes_data_fails_verification(self):
        sup, runtime, _, service = make(self.tmp)
        sup.up()
        seed(runtime)
        runtime.corrupt_import = True
        record = sup.storage.backup(service)
        self.assertFalse(record.verified)
        self.assertIn("content differs", record.verify_detail)

    def test_truncated_archive_fails_verification(self):
        sup, runtime, _, service = make(self.tmp)
        sup.up()
        seed(runtime)
        record = sup.storage.backup(service, verify=False)
        archive = self.tmp / "backups" / "db" / record.id / "db-data.tar.gz"
        truncated = archive.read_bytes()[:200]
        archive.write_bytes(truncated)
        # Make the outer checksum agree so the restore test itself has to catch it.
        import hashlib, json
        meta = archive.parent / "backup.json"
        body = json.loads(meta.read_text())
        body["volumes"]["db-data"]["sha256"] = hashlib.sha256(truncated).hexdigest()
        meta.write_text(json.dumps(body))
        self.assertFalse(sup.storage.verify(service, record.id).verified)

    def test_verify_command_gates_verification(self):
        sup, runtime, _, service = make(self.tmp, backup={"verify_command": ["db-check", "/var/lib/data"]})
        sup.up()
        seed(runtime)
        seen = {}

        def app_check(mounts, command):
            seen.update(mounts=mounts, command=command)
            return 3

        runtime.run_once_result = app_check
        record = sup.storage.backup(service)
        self.assertFalse(record.verified)
        self.assertIn("verify_command exited with code 3", record.verify_detail)
        self.assertEqual(list(seen["mounts"].values()), ["/var/lib/data"])

        runtime.run_once_result = lambda mounts, command: 0
        self.assertTrue(sup.storage.verify(service, record.id).verified)

    def test_restore_brings_back_exact_contents(self):
        sup, runtime, _, service = make(self.tmp)
        sup.up()
        seed(runtime)
        good = runtime.volume_manifest("db-data")
        sup.storage.backup(service)

        (runtime.vol("db-data") / "tables" / "users.dat").write_bytes(b"garbage")
        (runtime.vol("db-data") / "MANIFEST").unlink()
        (runtime.vol("db-data") / "stray.tmp").write_text("x")

        sup.storage.restore(service)
        self.assertEqual(runtime.volume_manifest("db-data"), good)

    def test_restore_keeps_a_copy_of_what_it_overwrote(self):
        sup, runtime, _, service = make(self.tmp)
        sup.up()
        seed(runtime)
        sup.storage.backup(service)
        (runtime.vol("db-data") / "after-backup.txt").write_text("written after the backup")
        sup.storage.restore(service)
        snapshots = list((self.tmp / "backups" / "db" / ".pre-restore").glob("*/db-data.tar.gz"))
        self.assertEqual(len(snapshots), 1)
        self.assertIn("after-backup.txt", archive_manifest(snapshots[0]))

    def test_restore_refuses_untested_backup(self):
        sup, runtime, _, service = make(self.tmp)
        sup.up()
        seed(runtime)
        record = sup.storage.backup(service, verify=False)
        with self.assertRaises(StorageError):
            sup.storage.restore(service)
        with self.assertRaises(StorageError):
            sup.storage.restore(service, record.id)
        sup.storage.restore(service, record.id, allow_unverified=True)

    def test_restore_detects_bad_write(self):
        sup, runtime, _, service = make(self.tmp)
        sup.up()
        seed(runtime)
        sup.storage.backup(service)
        runtime.corrupt_import = True
        with self.assertRaises(StorageError):
            sup.storage.restore(service)
        self.assertIn("restore_failed", sup.events.kinds("db"))

    def test_backup_pauses_container_when_configured(self):
        sup, runtime, _, service = make(self.tmp, backup={"pause": True})
        sup.up()
        seed(runtime)
        sup.storage.backup(service)
        kinds = [c[0] for c in runtime.calls]
        self.assertLess(kinds.index("pause"), kinds.index("unpause"))
        self.assertTrue(runtime.inspect("crp-db").running)

    def test_retention_keeps_newest_and_last_verified(self):
        sup, runtime, clock, service = make(self.tmp)
        sup.up()
        seed(runtime)
        verified = sup.storage.backup(service)
        for _ in range(4):
            clock.now += 60
            sup.storage.backup(service, verify=False)
        ids = [r.id for r in sup.storage.list(service)]
        self.assertEqual(len(ids), 4)  # keep=3 newest + the only verified one
        self.assertIn(verified.id, ids)

    def test_failed_backup_leaves_no_partial_entry(self):
        sup, runtime, _, service = make(self.tmp)
        with self.assertRaises(StorageError):
            sup.storage.backup(service)  # volume does not exist yet
        self.assertEqual(sup.storage.list(service), [])

    def test_scheduled_backup_only_while_healthy(self):
        sup, runtime, clock, service = make(self.tmp, backup={"interval": 3600})
        sup.up()
        seed(runtime)
        sup.tick()
        self.assertEqual(len(sup.storage.list(service)), 1)
        clock.now += 1800
        sup.tick()
        self.assertEqual(len(sup.storage.list(service)), 1)
        clock.now += 1801
        runtime.set_health("crp-db", "unhealthy")
        sup.tick()
        self.assertEqual(len(sup.storage.list(service)), 1)
        runtime.set_health("crp-db", "healthy")
        sup.tick()
        self.assertEqual(len(sup.storage.list(service)), 2)

    def test_manifest_helpers(self):
        parsed = parse_sha256sum("aa  ./a.txt\nbb  ./dir/b c.txt\n")
        self.assertEqual(parsed, {"a.txt": "aa", "dir/b c.txt": "bb"})
        diff = diff_manifests({"a": "1", "b": "2"}, {"b": "9", "c": "3"})
        self.assertEqual(diff, ["missing: a", "unexpected: c", "content differs: b"])


class EndToEndRecoveryTests(TmpCase):
    """Corrupted data: replacement alone cannot fix it, storage recovery does."""

    def test_escalates_from_replacement_to_storage_restore(self):
        sup, runtime, _, service = make(self.tmp)

        def health_from_data(rt, svc):
            marker = rt.vol("db-data") / "MANIFEST"
            return "healthy" if marker.exists() and marker.read_text() == "schema v7\n" else "unhealthy"

        runtime.vol("db-data").mkdir()
        seed(runtime)
        runtime.health_on_start = health_from_data
        self.assertTrue(sup.up()["db"])
        good = runtime.volume_manifest("db-data")
        self.assertTrue(sup.storage.backup(service).verified)

        # Disaster: the data is corrupted and the container goes down.
        (runtime.vol("db-data") / "MANIFEST").write_text("\x00\x00 torn write")
        runtime.kill("crp-db")

        outcomes = [sup.tick()["db"] for _ in range(3)]
        self.assertEqual(outcomes, ["replace-failed", "replace-failed", "replaced"])
        self.assertEqual(runtime.volume_manifest("db-data"), good)
        self.assertEqual(sup.tick()["db"], "healthy")
        kinds = sup.events.kinds("db")
        self.assertEqual(kinds.count("storage_restored"), 1)
        self.assertLess(kinds.index("storage_restored"), len(kinds) - 1 - kinds[::-1].index("replaced"))

    def test_without_tested_backup_no_restore_and_circuit_opens(self):
        sup, runtime, _, service = make(self.tmp)
        runtime.health_on_start = lambda rt, svc: (
            "healthy" if (rt.vol("db-data") / "MANIFEST").exists() else "unhealthy")
        runtime.vol("db-data").mkdir()
        seed(runtime)
        sup.up()
        sup.storage.backup(service, verify=False)  # exists, but never restore-tested

        (runtime.vol("db-data") / "MANIFEST").unlink()
        runtime.kill("crp-db")
        outcomes = [sup.tick()["db"] for _ in range(4)]
        self.assertEqual(outcomes[-1], "circuit-open")
        kinds = sup.events.kinds("db")
        self.assertIn("restore_unavailable", kinds)
        self.assertNotIn("storage_restored", kinds)


if __name__ == "__main__":
    unittest.main()
