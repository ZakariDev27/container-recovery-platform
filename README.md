# Container Recovery Platform

[![CI](https://github.com/ZakariDev27/container-recovery-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/ZakariDev27/container-recovery-platform/actions/workflows/ci.yml)

Keeps Docker services alive and their data recoverable:

- **Health checks** — HTTP, TCP, a command inside the container, or the image's own `HEALTHCHECK`.
- **Replacement containers** — a failed container is never restarted in place. It is stopped, set aside for inspection, and a fresh one is created from the service definition.
- **Tested storage recovery** — volume backups are restored into a scratch volume and compared file by file before they are trusted. Only backups that pass are used for automatic recovery.

Python 3.11+ and the `docker` CLI. No other dependencies.

## Demo

Output of `crp events` from a real run: a web service with a data volume, supervised by `crp watch`, with failures injected by hand.

```
# first healthy pass: back up the volume and prove the backup restores
21:20:04  web  backup_created     backup=20261117T212004Z files=2
21:20:04  web  backup_verified    restored and matched 2 file(s)

# docker kill crp-web  -> replaced one second after detection
21:20:09  web  check_failed       container exited
21:20:10  web  replaced           generation=2  http status 200
21:20:20  web  incident_closed

# docker pause crp-web (a hang) -> replaced after three failed checks
21:20:22  web  check_failed       container paused  failures=1/3
21:20:24  web  check_failed       container paused  failures=2/3
21:20:27  web  check_failed       container paused  failures=3/3
21:20:30  web  replaced           generation=3  http status 200

# files deleted from the volume -> a new container alone cannot fix it,
# so the volume is restored from the tested backup
21:20:55  web  check_failed       http status 404  failures=3/3
21:21:08  web  replace_failed     generation=4  not healthy after 10s
21:21:13  web  storage_restored   backup=20261117T212041Z
21:21:15  web  replaced           generation=5  http status 200
21:21:25  web  incident_closed
```

## Quick start

```sh
pip install -e .                       # installs the `crp` command
cp examples/recovery.toml recovery.toml  # then edit it

crp up          # start every service
crp status      # container, health, circuit, newest tested backup
crp watch       # supervise until stopped (Ctrl-C)
```

Without installing: `python -m recovery <command>`.

## How recovery works

Each pass of `crp watch` runs every service's health check.

| Situation | Action |
|---|---|
| Check fails, fewer than `failure_threshold` times in a row | Count it, wait |
| Check fails `failure_threshold` times in a row | Replace the container |
| Container exited or missing | Replace immediately |
| `restore_storage_after` replacements in `window` did not fix it | Restore volumes from the newest restore-tested backup, then replace |
| `max_replacements` replacements in `window` | Open the circuit: stop trying, leave the evidence, wait for `crp reset` |
| Healthy for `stable_after` seconds after a replacement | The incident is over; the replacement count starts from zero next time |

A paused container counts as unhealthy but is not replaced on the spot, since a backup may have paused it. While a manual `crp backup`, `restore`, `replace`, or `drill` is working on a service, a running `crp watch` skips that service.

**Replacement.** The old container's last 200 log lines are saved to `state/forensics/`, it is stopped and renamed `crp-<service>-failed-<generation>`, and a new `crp-<service>` is started with the same named volumes. The newest `keep_failed` failed containers are kept; older ones are removed. A replacement counts as successful only once the new container passes its health check within `start_timeout`.

**Backup.** Each volume is streamed to a `.tar.gz` in `backups/<service>/<id>/`, together with a manifest of every file's SHA-256 and the archive's own checksum. With `pause = true` the container is frozen during the copy.

**Restore test.** `crp verify` (run automatically after each backup when `verify = true`):

1. re-checks the archive checksum,
2. restores the archive into a temporary scratch volume,
3. hashes every restored file and compares with the manifest,
4. optionally runs `verify_command` in the service's image against the restored copy,
5. removes the scratch volume and records PASS or FAIL.

**Restore.** Runs only with the container stopped. The current volume contents are first saved to `backups/<service>/.pre-restore/` (last three kept), the volume is emptied, the backup is written, and the result is hashed against the manifest again. Untested backups are refused unless you pass `--allow-unverified`.

Scheduled backups are taken only while the service is healthy, so a broken state is never captured as the newest backup. Retention keeps the newest `keep` backups and never deletes the newest one that passed a restore test.

## Alerts

Set `platform.alert_webhook` to a URL and the platform POSTs a JSON message there whenever something needs attention: a replacement that failed, a storage restore, a backup that failed its restore test, or a service it has given up on. The body has a `text` field, so a Slack incoming-webhook URL works as is. Routine events (a successful replacement, a new backup) are not sent. An unreachable webhook is logged and never interrupts recovery.

## Commands

| Command | What it does |
|---|---|
| `crp up [service...]` | Start services that are not running |
| `crp down [service...]` | Stop and remove service containers (volumes and failed containers are kept) |
| `crp status` | Show state; exit 1 if anything is unhealthy |
| `crp check` | Run health checks once, change nothing |
| `crp watch [--once]` | Supervise: check, replace, restore, back up |
| `crp replace <service>` | Replace a container now |
| `crp backup <service> [--no-verify]` | Back up volumes |
| `crp backups [service...]` | List backups and their restore-test results |
| `crp verify <service> [backup-id]` | Restore-test a backup |
| `crp restore <service> [backup-id]` | Restore volumes and start a replacement |
| `crp drill [service...]` | Recovery drill: fresh backup plus restore test for each service; exit 1 on any failure |
| `crp reset <service>` | Close the circuit after a manual repair |
| `crp events [-n N]` | Show recent events |

Global options: `-c FILE` (default `recovery.toml`), `-v`.

`crp drill` is suited to a scheduled job: a non-zero exit means a backup could not be restored.

## Configuration

See `examples/recovery.toml` for a commented example. Paths are relative to the config file.

| Key | Default | Meaning |
|---|---|---|
| `platform.state_dir` | `./state` | State, event log, forensics |
| `platform.backup_dir` | `./backups` | Backup archives |
| `platform.check_interval` | `5` | Seconds between passes |
| `platform.helper_image` | `alpine:3.20` | Image used to read and write volumes (needs `sh`, `tar`, `find`, `sha256sum`) |
| `platform.alert_webhook` | – | URL that receives serious events as JSON |
| `platform.alert_events` | see `recovery/alerts.py` | Which events are sent |
| `services.<name>.image` | required | Image to run |
| `services.<name>.command`, `ports`, `env`, `volumes`, `network`, `extra_args` | empty | Passed to `docker run`; volumes must be named volumes (`name:/path`) |
| `health.type` | `docker` | `http`, `tcp`, `exec`, or `docker` |
| `health.url`, `expect_status` | –, `[200, 399]` | For `http` |
| `health.host`, `port` | `127.0.0.1`, – | For `tcp` |
| `health.command` | – | For `exec` |
| `health.timeout` | `3` | Seconds per check |
| `health.failure_threshold` | `3` | Consecutive failures before replacement |
| `health.start_timeout` | `60` | Seconds a new container has to become healthy |
| `recovery.max_replacements` | `3` | Replacements per window before the circuit opens |
| `recovery.window` | `600` | Seconds |
| `recovery.restore_storage_after` | `2` | Replacements in the window before volumes are restored |
| `recovery.stable_after` | `120` | Seconds of continuous health after which the replacement history is cleared |
| `recovery.keep_failed` | `1` | Failed containers kept |
| `recovery.stop_timeout` | `10` | Seconds given to a container to stop |
| `backup.interval` | `0` | Seconds between scheduled backups; `0` is manual only |
| `backup.keep` | `5` | Backups retained |
| `backup.verify` | `true` | Restore-test each backup when taken |
| `backup.pause` | `false` | Pause the container during the copy |
| `backup.verify_command` | – | Extra check run against the restored copy |

HTTP and TCP checks run from the machine running `crp`, so point them at published ports. HTTP checks connect directly and ignore proxy environment variables.

## Tests

```sh
python -m unittest                      # unit and scenario tests, no Docker needed

# against a real Docker engine
CRP_DOCKER_TESTS=1 CRP_POSTGRES_TESTS=1 python -m unittest tests.test_docker_integration
```

- **Unit and scenario tests** run the recovery logic on an in-memory engine with real directories as volumes, so archives, checksums, restore tests, and restores are exercised for real.
- **Docker test** drives an actual engine through start, backup, restore test, kill, replacement, data loss, and storage recovery.
- **PostgreSQL test** loads 5,000 rows into a real database, backs it up live, deletes its control file, kills it, and checks that the platform restores the data directory and every row is back.

All three run on every push in GitHub Actions. `CRP_TEST_IMAGE` and `CRP_POSTGRES_IMAGE` override the images used.

## Limits

- Single host, Docker only. One supervisor per config file.
- Backups are file-level copies of volumes. With `backup.pause` the copy is crash-consistent, which PostgreSQL recovers from (this is tested); a database without crash recovery needs a dump-based backup instead.
- Replacement stops the old container before starting the new one (they share ports and volumes), so there is a short gap in service.
- File names containing newlines are not supported in volume manifests.

## Layout

```
recovery/
  config.py      TOML loading and validation
  runtime.py     container engine interface and the Docker implementation
  health.py      health checks
  replacer.py    replacement containers
  storage.py     backup, restore test, restore, retention
  supervisor.py  the recovery ladder and the watch loop
  state.py       persisted state, event log, per-service locks
  alerts.py      webhook alerts
  cli.py         the crp command
tests/
examples/recovery.toml
```

## License

MIT. See `LICENSE`.
