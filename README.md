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

## Before you start

You need three things. Each takes a minute.

**1. Allow TR-064 on the FRITZ!Box.**
Open the box's web UI and go to **Home Network › Network › Network Settings**. Turn on
**Allow access for applications** and click **Apply**.

**2. Create a user for the watchdog on the FRITZ!Box.**
Go to **System › FRITZ!Box Users › Add User**. Pick a name (e.g. `watchdog`) and a long random password.
Under rights, tick only **FRITZ!Box Settings**: reconnect and reboot need this right, nothing else does.
Write the name and password down; you need them in step 4 of the how-to.

**3. Know the box's address.**
Usually `fritz.box` or `192.168.178.1`. If your server does not use the box as its DNS server, use the IP.
The server must reach the box on TCP port **49443** (TR-064 over HTTPS). Open that port in any firewall
between the two. In a normal home network with just the box, there is nothing to do.

### Where should the password go?

Pick one of the two ways. Both keep the password out of the logs.

| | **A. In the app settings** (`FRITZ_PASSWORD`) | **B. In a file `app/.env`** |
| --- | --- | --- |
| Good for | passwords without `$` and `'` | any password, and people who'd rather not think about escaping |
| How | type it into the YAML | one line `FRITZ_PASSWORD=…` in a text file |
| Watch out | Compose treats `$` specially, even inside quotes: write every `$` as `$$`. Inside `'…'` write `'` as `''`. A wrong character means HTTP 401 later. | Leave `FRITZ_PASSWORD` in the YAML empty (a set value wins over the file). Don't delete the file when you update the scripts. |

If unsure, use **B**. The how-tos below show both.

## How-to: TrueNAS SCALE (custom app)

Everything below happens in the TrueNAS web UI and its shell (**System › Shell**). Replace `POOL` with the name
of your pool (see **Storage**).

**1. Create the folders.**

```sh
sudo mkdir -p /mnt/POOL/apps/fritz-watchdog/app /mnt/POOL/apps/fritz-watchdog/logs
```

**2. Download the three script files.**

```sh
cd /mnt/POOL/apps/fritz-watchdog/app
for f in fritz_watchdog.py tr064.py fritz_common.py; do
  sudo curl -fsSLO "https://raw.githubusercontent.com/kejoat/fritz-watchdog/main/app/$f"
done
ls -l
```

You should see the three `.py` files.

**3. Hand the folder to the container user.** The container runs as the TrueNAS `apps` user (uid/gid 568).
It must read `app/` and write `logs/`.

```sh
sudo chown -R 568:568 /mnt/POOL/apps/fritz-watchdog
```

> If the pool uses NFSv4 ACLs (shares for Windows/SMB usually do), add the `apps` user instead in
> **Datasets › your dataset › Permissions › Edit**: read on `app`, modify on `logs`, and traverse on every
> parent folder. See [Troubleshooting](#troubleshooting) if the container cannot read or write.

**4. Only for password way B: create the password file.**

```sh
sudo nano /mnt/POOL/apps/fritz-watchdog/app/.env
```

Type one line, with your password after the `=` (no quotes, no spaces):

```text
FRITZ_PASSWORD=your-long-random-password
```

Save with **Ctrl+O**, **Enter**, then quit with **Ctrl+X**. Make it readable for the container only:

```sh
sudo chown 568:568 /mnt/POOL/apps/fritz-watchdog/app/.env
sudo chmod 600 /mnt/POOL/apps/fritz-watchdog/app/.env
```

**5. Install the app.**

1. Go to **Apps › Discover Apps**, click the **⋮** menu (top right) and choose **Install via YAML**.
2. **Name**: lowercase letters, digits and hyphens only, e.g. `fritz-watchdog`.
3. Open [`compose.yaml`](compose.yaml) from this repository (the raw file), copy everything and paste it.
   Copy from the file itself: text copied from a chat or a web page can contain invisible characters, and
   TrueNAS then says *"Invalid YAML provided"*.
4. Change these lines:

   ```yaml
   volumes:
     - /mnt/POOL/apps/fritz-watchdog/app:/app:ro     # only the part before the first ":"
     - /mnt/POOL/apps/fritz-watchdog/logs:/logs
   environment:
     FRITZ_HOST: "192.168.178.1"                     # your box
     FRITZ_USER: "watchdog"                          # the user from "Before you start"
     FRITZ_PASSWORD: ""                              # way B: leave empty; way A: the password ($ → $$)
   ```

   Leave `DRY_RUN: "1"` for now: the watchdog then only *logs* what it would do.
5. Click **Save**. After a few seconds the app shows **Running**.

**6. Check that it works.** Click the app, then **Logs** (or use the shell, see [Check and go live](#check-and-go-live)).
The first lines should be `start` and then **`box_ok`**. That means the container reached the box and the
login worked. Seeing `box_auth_failed` or `box_unreachable` instead? See [Troubleshooting](#troubleshooting).

Continue with [Check and go live](#check-and-go-live). The container name for shell commands is
`ix-<app name>-fritz-watchdog-1`, e.g. `ix-fritz-watchdog-fritz-watchdog-1` (list them with `sudo docker ps`).

## How-to: Docker Compose (any Linux host)

You need Docker with the Compose plugin (`docker compose version` prints a version).

**1. Create a folder and download the files.**

```sh
mkdir -p ~/fritz-watchdog/app ~/fritz-watchdog/logs
cd ~/fritz-watchdog
curl -fsSLO https://raw.githubusercontent.com/kejoat/fritz-watchdog/main/compose.yaml
for f in fritz_watchdog.py tr064.py fritz_common.py; do
  curl -fsSL -o "app/$f" "https://raw.githubusercontent.com/kejoat/fritz-watchdog/main/app/$f"
done
```

**2. Only for password way B: create the password file** (in `app/`, not next to `compose.yaml`):

```sh
nano app/.env            # one line: FRITZ_PASSWORD=your-long-random-password
chmod 600 app/.env
```

**3. Edit `compose.yaml`** (`nano compose.yaml`):

```yaml
user: "1000:1000"                  # your own ids, see the output of: id -u; id -g
volumes:
  - ./app:/app:ro
  - ./logs:/logs
environment:
  FRITZ_HOST: "192.168.178.1"
  FRITZ_USER: "watchdog"
  FRITZ_PASSWORD: ""               # way B: leave empty; way A: the password ($ → $$)
```

With your own uid/gid the container can read `app/` (and `.env`) and write `logs/` without any extra rights.

**4. Start it.**

```sh
docker compose up -d
docker compose logs -f             # Ctrl+C stops watching, not the container
```

Look for **`box_ok`** right after `start`. Continue with [Check and go live](#check-and-go-live); in the commands
there, use `docker compose exec fritz-watchdog` instead of `docker exec <container>`.

## Check and go live

**Login and box status** (read-only, changes nothing):

```sh
docker exec <container> python /app/fritz_watchdog.py --check-box
```

Expected: JSON with `"status": "Connected"`.

**Rehearse an outage safely.** This lets one check fail on purpose and shows what the watchdog *would* do.
Nothing is sent to the box:

```sh
docker exec <container> sh -c 'PROBES=tcp:192.0.2.1:443 python /app/fritz_watchdog.py --once --dry-run'
```

Expected: an `outage` line and a `dry_run` line with `"would_call": "WANIPConnection1.ForceTermination"`
(or `WANPPPConnection1` on DSL).

**Go live.** Watch the logs for a few days. When the decisions look right, set `DRY_RUN: "0"`:
on TrueNAS via **Apps › your app › Edit**, with Compose in `compose.yaml` followed by `docker compose up -d`.

Optional settings worth a look: `QUIET_WINDOWS` (skip the box's nightly forced reconnect) and `REBOOT_ENABLED`.
All settings are in the [table below](#settings-environment-variables).

### Updating

Download the three `.py` files again (step 2 of your how-to) and restart the app (TrueNAS: **Stop**/**Start**;
Compose: `docker compose restart`). Overwrite the files; don't empty the `app/` folder, or `.env` goes with it.

### Checking the password without showing it

If the box answers HTTP 401 although the password looks right, compare what the container got with what you
meant, by length and a short hash:

```sh
# inside the container (password way A only)
docker exec <container> python -c "import os,hashlib;p=os.environ['FRITZ_PASSWORD'];print(len(p),hashlib.sha256(p.encode()).hexdigest()[:8])"
# on your PC (asks for the password, shows nothing)
python -c "import getpass,hashlib;p=getpass.getpass();print(len(p),hashlib.sha256(p.encode()).hexdigest()[:8])"
```

Different results mean the password was changed on the way, usually by a `$`. Use `$$`, or switch to way B.

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

Events: `start`, `box_ok`, `online`, `probes_failed`, `outage`, `box_auth_failed`, `box_unreachable`, `waiting`,
`skipped`, `reconnect`, `connect`, `reboot`, `dry_run`, `action`, `action_failed`, `status_after_reconnect`,
`reconnect_blocked`, `reboot_blocked`, `no_action`, `recovered`, `config_error`, `log_file_disabled`.

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

Exit codes: `0` online/ok, `1` outage detected, `2` configuration error, `3` box not reachable or login refused, `4` unhealthy.

For local tests, copy [`.env.example`](.env.example) to `.env` and fill it in. Only the `FRITZ_*` keys are read from that file.
A safe way to simulate an outage: `PROBES=tcp:192.0.2.1:443 python app/fritz_watchdog.py --once --dry-run`.

## Development

```sh
python -m unittest discover -s tests -v
```

The tests use a fake box, fake probes and a fake clock; they need no network.
`app/tr064.py` and `app/fritz_common.py` are a small, dependency-free TR-064 client (SOAP with digest auth).

## Troubleshooting

- **`box_auth_failed`** (HTTP 401/403, UPnP 606): wrong user or password, or the user lacks the *FRITZ!Box Settings*
  right. A password set in the app configuration may have been changed by Compose (see [The password](#the-password)).
  HTTP 503 means the box blocks logins for a while after too many failures: fix the password, then wait.
- **`box_unreachable` with a timeout**: TR-064 is disabled, the host or port is wrong, or a firewall blocks the port.
- **`UPnP error 606`** on an action: the user lacks the right for it.
- **Quiet windows at the wrong time**: set `TZ` as a POSIX string (see above).
- **Container restarts at once, log says `can't open file`**: the `command` must point to `/app/fritz_watchdog.py`
  (the path inside the container), and uid 568 must be able to read the host folder.
- **`log_file_disabled`**: the container user cannot write to the host folder mounted at `/logs`. Docker applies
  only the uid and gid from `user:`, not the user's supplementary groups: if write access comes from a group,
  put that group's gid into `user: "UID:GID"`.
- **Checking read access inside the container**: the alpine `test -r` judges by the mode bits and ignores NFSv4 ACLs,
  so it can report a readable file as unreadable. Really open it instead:
  `docker exec <container> python -c "open('/app/.env').read()"`.

## Uninstall

Delete the app in TrueNAS (or `docker compose down`), then remove the app folder and the TR-064 user on the box.

## License

[GNU General Public License v3.0](LICENSE) or later. FRITZ!Box is a trademark of AVM / FRITZ! GmbH; this project is not affiliated with them.
