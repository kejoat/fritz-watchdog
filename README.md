# fritz-watchdog

A tiny watchdog for AVM FRITZ!Box routers. It checks regularly whether the internet is reachable.
When it is not, it asks the FRITZ!Box via TR-064 whether it is still connected to the provider, and
reconnects the line, just like the "Reconnect" button in the web UI. As a last resort it can also reboot the box.

Why: some providers, often mobile (LTE/5G) ones, silently drop the data session while the box stays
attached to the cell and keeps reporting "Connected". The box never recovers on its own; a reconnect fixes it.

- Provider-neutral: the active WAN service (IP or PPP) is detected automatically.
- Python standard library only, a single process, about 15–25 MB RAM.
- Runs on the official `python:3.13-alpine` image: no build, no extra packages, read-only, non-root, no capabilities, no open ports.
- No ICMP: the internet checks are TCP connects and one plain HTTP request, so the container needs no `NET_RAW`.
- Guard rails: several failed rounds before acting, cooldowns, daily limits, quiet windows and a dry-run mode.

## How it works

```text
every CHECK_INTERVAL:
  probes (TCP 1.1.1.1:443, 9.9.9.9:443, 8.8.8.8:53, HTTP generate_204)
  ├─ at least PROBE_MIN_OK ok            → online, reset everything
  └─ all failed for FAIL_ROUNDS rounds   → ask the box (TR-064)
       ├─ box does not answer            → LAN/box problem: log only, never act
       ├─ quiet window / recovery wait   → log only
       ├─ reboot enabled and REBOOT_AFTER_RECONNECTS reconnects did not help → DeviceConfig Reboot
       ├─ status "Connected"             → ON_CONNECTED    (reconnect | none)
       └─ any other status               → ON_DISCONNECTED (reconnect | connect | none)
```

- `reconnect` calls `ForceTermination`. The box then re-establishes the connection by itself. If it is not
  `Connected` after `RECONNECT_SETTLE` seconds, `RequestConnection` follows.
- `connect` only calls `RequestConnection`.
- After an action, the watchdog waits `RECOVERY_WAIT` seconds before it acts again, and `REBOOT_WAIT` seconds after a reboot.
- Counters (cooldowns, daily limits) live in memory. Restarting the container resets them.

## Requirements on the FRITZ!Box

1. **Home Network > Network > Network Settings**: enable *Allow access for applications* (TR-064).
2. **System > FRITZ!Box Users**: create a dedicated user for the watchdog with only the right
   *FRITZ!Box Settings*, which reconnect and reboot need. Use a long random password.
3. The host running the container must reach the box on TCP **49443** (TR-064 over HTTPS, `FRITZ_TLS=1`)
   or 49000 (plain HTTP). Open it in any firewall in between.

## Deployment on TrueNAS SCALE

1. Create a folder, for example `/mnt/POOL/apps/fritz-watchdog`, with the subfolders `app` and `logs`, and copy the
   three files from [`app/`](app/) into `app`. The container runs as the TrueNAS `apps` user (uid 568), which needs
   **read** access to `app`, **write** access to `logs` and **traverse** on every parent folder. On datasets with
   NFSv4 ACLs the `ls -l` mode bits don't tell; add explicit ACL entries for `apps` where needed.
2. **Apps > Discover Apps > ⋮ > Install via YAML**: the app name may only contain lowercase letters, digits and
   hyphens (e.g. `fritz-watchdog`). Paste [`compose.yaml`](compose.yaml), change only the **left (host) side** of the
   volume lines (inside the container the paths stay `/app` and `/logs`) and fill in `FRITZ_HOST`, `FRITZ_USER` and
   `FRITZ_PASSWORD`.
3. Keep `DRY_RUN: "1"` for the first days and watch the logs. When the decisions look right, set it to `"0"`.

Check that the container can reach the box (read-only):
`docker exec <container> python /app/fritz_watchdog.py --check-box`.

Plain Docker works the same way: `docker compose up -d` with the adjusted `compose.yaml`.

The password is stored in the app configuration, where only NAS admins can see it. It never appears in the logs.

## Settings (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `FRITZ_HOST` | `fritz.box` | Host name or IP of the box |
| `FRITZ_USER` / `FRITZ_PASSWORD` | – | TR-064 user (required) |
| `FRITZ_TLS` | `0` | `1` = HTTPS on port 49443 (recommended) |
| `FRITZ_VERIFY` | `0` | `1` = verify the box's certificate (only with a trusted certificate) |
| `FRITZ_TIMEOUT` | `15` | TR-064 request timeout in seconds |
| `DRY_RUN` | `1` | `1` = only log what would be done |
| `CHECK_INTERVAL` | `60` | Seconds between checks (min. 10) |
| `FAIL_ROUNDS` | `3` | Failed rounds in a row before the box is asked |
| `PROBES` | see above | Comma-separated `tcp:HOST:PORT` and `http(s)://URL` probes |
| `PROBE_TIMEOUT` | `5` | Seconds per probe |
| `PROBE_MIN_OK` | `1` | Probes that must succeed for "online" |
| `WAN_SERVICE` | `auto` | `auto`, `WANIPConnection1` or `WANPPPConnection1` |
| `ON_CONNECTED` | `reconnect` | Box says Connected, no internet: `reconnect` or `none` |
| `ON_DISCONNECTED` | `reconnect` | Box is not connected: `reconnect`, `connect` or `none` |
| `RECONNECT_SETTLE` | `30` | Seconds after `ForceTermination` before the status is checked |
| `RECOVERY_WAIT` | `180` | Seconds without new actions after a reconnect |
| `RECONNECT_COOLDOWN` | `600` | Minimum seconds between two reconnects |
| `MAX_RECONNECTS_PER_DAY` | `6` | Reconnects per rolling 24 h |
| `REBOOT_ENABLED` | `0` | `1` = allow rebooting the box |
| `REBOOT_AFTER_RECONNECTS` | `2` | Unsuccessful reconnects before a reboot |
| `REBOOT_COOLDOWN` | `3600` | Minimum seconds between two reboots |
| `MAX_REBOOTS_PER_DAY` | `2` | Reboots per rolling 24 h |
| `REBOOT_WAIT` | `300` | Seconds without new actions after a reboot |
| `QUIET_WINDOWS` | – | `HH:MM-HH:MM[,…]` local time without actions, e.g. around the box's nightly forced reconnect (see the box's event log for the time); may span midnight |
| `TZ` | UTC | Time zone for `QUIET_WINDOWS`. The alpine image has no tz database, so use a POSIX string, e.g. `CET-1CEST,M3.5.0,M10.5.0/3` |
| `LOG_OK_EVERY` | `60` | Log an "online" line every N rounds (0 = only on changes) |
| `LOG_DIR` | – | Folder for a rotating `fritz-watchdog.log` (in addition to stdout); empty = stdout only |
| `LOG_MAX_BYTES` | `1000000` | Size at which the log file rotates |
| `LOG_BACKUPS` | `5` | Rotated files to keep (`fritz-watchdog.log.1` … `.5`) |
| `STATUS_FILE` | `/tmp/fritz-watchdog.status` | Status file for the health check |

## Logs and status

One JSON object per line on stdout. With `LOG_DIR` set, the watchdog loop writes the same lines to
`LOG_DIR/fritz-watchdog.log`, rotated at `LOG_MAX_BYTES` with `LOG_BACKUPS` old files. If the folder is not
writable, it logs `log_file_disabled` and keeps running with stdout only.

```json
{"ts": "2026-01-01T12:00:00+01:00", "level": "warning", "event": "outage", "round": 3, "box": {"service": "WANIPConnection1", "status": "Connected", "physical_link": "Up", "mobile_tech": "5G-SA"}, "probes": {"tcp:1.1.1.1:443": "FAIL TimeoutError: timed out"}}
{"ts": "2026-01-01T12:00:00+01:00", "level": "warning", "event": "reconnect", "attempt": 1, "service": "WANIPConnection1", "dry_run": false}
```

Events: `start`, `online`, `probes_failed`, `outage`, `box_unreachable`, `waiting`, `skipped`, `reconnect`, `connect`,
`reboot`, `dry_run`, `action`, `action_failed`, `status_after_reconnect`, `reconnect_blocked`, `reboot_blocked`,
`no_action`, `recovered`, `config_error`, `log_file_disabled`.

The container health check (`--health`) fails when the status file has not been updated for about three intervals.

## Command line

```text
python app/fritz_watchdog.py                 run the loop
python app/fritz_watchdog.py --check-box     read-only: print the box's WAN status
python app/fritz_watchdog.py --once          one check; acts right away on an outage (respects DRY_RUN)
python app/fritz_watchdog.py --print-config  effective settings, password redacted
python app/fritz_watchdog.py --health        health check
                             --dry-run       force DRY_RUN=1
```

Exit codes: `0` online/ok, `1` outage detected, `2` configuration error, `3` box not reachable, `4` unhealthy.

For local tests, copy [`.env.example`](.env.example) to `.env` and fill it in. Only the `FRITZ_*` keys are read from that file.
A safe way to simulate an outage: `PROBES=tcp:192.0.2.1:443 python app/fritz_watchdog.py --once --dry-run`.

## Development

```sh
python -m unittest discover -s tests -v
```

The tests use a fake box, fake probes and a fake clock; they need no network.
`app/tr064.py` and `app/fritz_common.py` are a small, dependency-free TR-064 client (SOAP with digest auth).

## Troubleshooting

- **`box_unreachable` with HTTP 401**: wrong user or password, or the user lacks the *FRITZ!Box Settings* right.
- **`box_unreachable` with a timeout**: TR-064 is disabled, the host or port is wrong, or a firewall blocks the port.
- **`UPnP error 606`** on an action: the user lacks the right for it.
- **Quiet windows at the wrong time**: set `TZ` as a POSIX string (see above).
- **Container restarts at once, log says `can't open file`**: the `command` must point to `/app/fritz_watchdog.py`
  (the path inside the container), and uid 568 must be able to read the host folder.
- **`log_file_disabled`**: the container user cannot write to the host folder mounted at `/logs`. Docker applies
  only the uid and gid from `user:`, not the user's supplementary groups: if write access comes from a group,
  put that group's gid into `user: "UID:GID"`.

## Uninstall

Delete the app in TrueNAS (or `docker compose down`), then remove the app folder and the TR-064 user on the box.

## License

[GNU General Public License v3.0](LICENSE) or later. FRITZ!Box is a trademark of AVM / FRITZ! GmbH; this project is not affiliated with them.
