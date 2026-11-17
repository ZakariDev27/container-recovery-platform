"""End-to-end test against a real Docker engine.

Opt in with:  CRP_DOCKER_TESTS=1 python -m unittest tests.test_docker_integration

CRP_TEST_IMAGE (default alpine:3.20) must provide sh, tar, find, sha256sum, sleep, mkdir, head.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from recovery import config as cfg
from recovery.runtime import DockerRuntime
from recovery.supervisor import Supervisor

IMAGE = os.environ.get("CRP_TEST_IMAGE", "alpine:3.20")
ENABLED = os.environ.get("CRP_DOCKER_TESTS") == "1"


def sh(volume: str, script: str) -> None:
    subprocess.run(
        ["docker", "run", "--rm", "-v", f"{volume}:/data", IMAGE, "sh", "-c", script],
        check=True, capture_output=True,
    )


@unittest.skipUnless(ENABLED, "set CRP_DOCKER_TESTS=1 to run against a real Docker engine")
class DockerRecoveryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        tag = uuid.uuid4().hex[:8]
        self.name = f"it{tag}"
        self.volume = f"crp-it-{tag}"
        config = cfg.parse(
            {
                "platform": {"state_dir": "state", "backup_dir": "backups", "helper_image": IMAGE},
                "services": {
                    self.name: {
                        "image": IMAGE,
                        "command": ["sleep", "3600"],
                        "volumes": [f"{self.volume}:/data"],
                        "health": {
                            "type": "exec",
                            "command": ["test", "-f", "/data/MANIFEST"],
                            "failure_threshold": 2,
                            "start_timeout": 3,
                        },
                        "recovery": {"restore_storage_after": 1, "stop_timeout": 1},
                        "backup": {"pause": True, "verify_command": ["test", "-f", "/data/MANIFEST"]},
                    }
                },
            },
            base=self.tmp,
        )
        self.runtime = DockerRuntime(IMAGE)
        self.sup = Supervisor(config, self.runtime)
        self.service = config.service(self.name)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for name in self.runtime.list_names(self.service.container_name):
            self.runtime.remove(name)
        subprocess.run(["docker", "volume", "rm", "-f", self.volume], capture_output=True)

    def test_health_replacement_and_storage_recovery(self):
        rt, sup, svc = self.runtime, self.sup, self.service
        rt.volume_create(self.volume)
        sh(self.volume, "cd /data && mkdir -p 'tables/my dir' && echo 'schema v7' > MANIFEST"
                        " && echo rows > 'tables/my dir/users 1.dat'"
                        " && head -c 300000 /bin/sh > tables/blob.bin")

        # 1. start and health check
        self.assertTrue(sup.up()[self.name])
        self.assertEqual(sup.tick()[self.name], "healthy")
        good = rt.volume_manifest(self.volume)
        self.assertEqual(set(good), {"MANIFEST", "tables/my dir/users 1.dat", "tables/blob.bin"})

        # 2. backup, restore-tested into a scratch volume (which is then removed)
        record = sup.storage.backup(svc)
        self.assertTrue(record.verified, record.verify_detail)
        self.assertTrue(rt.inspect(svc.container_name).running)  # unpaused again
        leftovers = subprocess.run(
            ["docker", "volume", "ls", "-q", "--filter", f"name=crp-verify-{self.name}"],
            capture_output=True, text=True).stdout.split()
        self.assertEqual(leftovers, [])

        # 3. a killed container is replaced; the volume carries over
        first_id = rt.inspect(svc.container_name).id
        subprocess.run(["docker", "kill", svc.container_name], check=True, capture_output=True)
        self.assertEqual(sup.tick()[self.name], "replaced")
        self.assertNotEqual(rt.inspect(svc.container_name).id, first_id)
        self.assertEqual(rt.list_names(f"{svc.container_name}-failed-"),
                         [f"{svc.container_name}-failed-0001"])
        self.assertEqual(rt.volume_manifest(self.volume), good)

        # 4. data loss: replacement alone fails, the tested backup is restored
        sh(self.volume, "rm /data/MANIFEST; echo junk > '/data/tables/my dir/users 1.dat'; echo x > /data/stray")
        outcomes = [sup.tick()[self.name] for _ in range(2)]
        self.assertEqual(outcomes, ["unhealthy", "replaced"])
        self.assertIn("storage_restored", sup.events.kinds(self.name))
        self.assertEqual(rt.volume_manifest(self.volume), good)
        self.assertEqual(sup.tick()[self.name], "healthy")


if __name__ == "__main__":
    unittest.main()
