#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 kejoat
"""fritz-watchdog: reconnect (and optionally reboot) a FRITZ!Box when the internet is gone.

Every CHECK_INTERVAL seconds a few independent probes (TCP connects, one plain HTTP request) test the
internet. After FAIL_ROUNDS failed rounds in a row the box is asked via TR-064 whether it is still connected
to the provider, and the configured action runs: reconnect (like the "Reconnect" button), connect, or
nothing. If that does not help, an optional reboot follows. Cooldowns, daily limits, quiet windows and a
dry-run mode keep it from flapping. Standard library only; all settings come from environment variables
(see README.md). Credentials: FRITZ_HOST, FRITZ_USER, FRITZ_PASSWORD, FRITZ_TLS (see fritz_common.py).

Usage
  fritz_watchdog.py                 run the watchdog loop
  fritz_watchdog.py --once          one check; a detected outage is handled right away (no FAIL_ROUNDS)
  fritz_watchdog.py --check-box     read-only: print the box's WAN status as JSON
  fritz_watchdog.py --print-config  print the effective settings (password redacted)
  fritz_watchdog.py --health        container health check (status file fresh?)
  --dry-run                         force DRY_RUN=1 for this run

Logs go to stdout; with LOG_DIR set, the loop also writes a rotating LOG_DIR/fritz-watchdog.log.

Exit codes: 0 online / ok, 1 outage detected, 2 configuration error, 3 box not reachable,
4 health check failed.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import logging
import logging.handlers
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fritz_common as fc  # noqa: E402  (also fills FRITZ_* from a local credential file, if present)
from tr064 import Tr064  # noqa: E402

DEFAULT_PROBES = ("tcp:1.1.1.1:443,tcp:9.9.9.9:443,tcp:8.8.8.8:53,"
                  "http://connectivitycheck.gstatic.com/generate_204")
DAY = 86400
EXIT_OK, EXIT_OUTAGE, EXIT_CONFIG, EXIT_BOX, EXIT_HEALTH = 0, 1, 2, 3, 4


# ------------------------------------------------------------------------------ configuration

class ConfigError(Exception):
    pass


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value if value else default


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = _env(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not an integer") from None
    if value < minimum:
        raise ConfigError(f"{name}={value} must be >= {minimum}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name}={raw!r} must be 0 or 1")


def _choice(name: str, default: str, allowed: tuple) -> str:
    value = _env(name, default).lower()
    if value not in allowed:
        raise ConfigError(f"{name}={value!r} must be one of {', '.join(allowed)}")
    return value


def parse_windows(spec: str) -> list:
    """'03:20-03:35,12:00-12:05' -> [(200, 215), (720, 725)] in minutes since midnight."""
    out = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        try:
            start, end = part.split("-")
            sh, sm = (int(x) for x in start.split(":"))
            eh, em = (int(x) for x in end.split(":"))
        except ValueError:
            raise ConfigError(f"QUIET_WINDOWS entry {part!r} must look like HH:MM-HH:MM") from None
        if not (0 <= sh < 24 and 0 <= eh < 24 and 0 <= sm < 60 and 0 <= em < 60):
            raise ConfigError(f"QUIET_WINDOWS entry {part!r} is not a valid time range")
        out.append((sh * 60 + sm, eh * 60 + em))
    return out


def parse_probes(spec: str) -> list:
    probes = [p.strip() for p in spec.split(",") if p.strip()]
    if not probes:
        raise ConfigError("PROBES is empty")
    for p in probes:
        if p.startswith("tcp:"):
            host, _, port = p[4:].rpartition(":")
            if not host or not port.isdigit():
                raise ConfigError(f"probe {p!r} must look like tcp:HOST:PORT")
        elif not p.startswith(("http://", "https://")):
            raise ConfigError(f"probe {p!r} must start with tcp:, http:// or https://")
    return probes


@dataclass
class Config:
    check_interval: int = 60
    fail_rounds: int = 3
    probes: list = field(default_factory=lambda: parse_probes(DEFAULT_PROBES))
    probe_timeout: int = 5
    probe_min_ok: int = 1
    wan_service: str = "auto"
    on_connected: str = "reconnect"
    on_disconnected: str = "reconnect"
    reconnect_settle: int = 30
    recovery_wait: int = 180
    reconnect_cooldown: int = 600
    max_reconnects_per_day: int = 6
    reboot_enabled: bool = False
    reboot_after_reconnects: int = 2
    reboot_cooldown: int = 3600
    max_reboots_per_day: int = 2
    reboot_wait: int = 300
    quiet_windows: list = field(default_factory=list)
    dry_run: bool = True
    log_ok_every: int = 60
    status_file: str = "/tmp/fritz-watchdog.status"
    log_dir: str = ""
    log_max_bytes: int = 1_000_000
    log_backups: int = 5

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls(
            check_interval=_int("CHECK_INTERVAL", 60, 10),
            fail_rounds=_int("FAIL_ROUNDS", 3, 1),
            probes=parse_probes(_env("PROBES", DEFAULT_PROBES)),
            probe_timeout=_int("PROBE_TIMEOUT", 5, 1),
            probe_min_ok=_int("PROBE_MIN_OK", 1, 1),
            wan_service=_env("WAN_SERVICE", "auto"),
            on_connected=_choice("ON_CONNECTED", "reconnect", ("reconnect", "none")),
            on_disconnected=_choice("ON_DISCONNECTED", "reconnect", ("reconnect", "connect", "none")),
            reconnect_settle=_int("RECONNECT_SETTLE", 30),
            recovery_wait=_int("RECOVERY_WAIT", 180),
            reconnect_cooldown=_int("RECONNECT_COOLDOWN", 600),
            max_reconnects_per_day=_int("MAX_RECONNECTS_PER_DAY", 6),
            reboot_enabled=_bool("REBOOT_ENABLED", False),
            reboot_after_reconnects=_int("REBOOT_AFTER_RECONNECTS", 2, 1),
            reboot_cooldown=_int("REBOOT_COOLDOWN", 3600),
            max_reboots_per_day=_int("MAX_REBOOTS_PER_DAY", 2),
            reboot_wait=_int("REBOOT_WAIT", 300),
            quiet_windows=parse_windows(_env("QUIET_WINDOWS", "")),
            dry_run=_bool("DRY_RUN", True),
            log_ok_every=_int("LOG_OK_EVERY", 60),
            status_file=_env("STATUS_FILE", "/tmp/fritz-watchdog.status"),
            log_dir=_env("LOG_DIR", ""),
            log_max_bytes=_int("LOG_MAX_BYTES", 1_000_000, 10_000),
            log_backups=_int("LOG_BACKUPS", 5, 1),
        )
        if cfg.probe_min_ok > len(cfg.probes):
            raise ConfigError(f"PROBE_MIN_OK={cfg.probe_min_ok} is larger than the number of probes")
        return cfg


# ------------------------------------------------------------------------------ logging

LOG_FILE_NAME = "fritz-watchdog.log"
_file_log: logging.Logger | None = None


def log(event: str, level: str = "info", **fields) -> None:
    """One JSON object per line on stdout (container log) and, if LOG_DIR is set, in a rotating file."""
    rec = {"ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "level": level, "event": event}
    rec.update(fields)
    line = json.dumps(rec, ensure_ascii=False, default=str)
    print(line, flush=True)
    if _file_log is not None:
        _file_log.info(line)


def setup_file_log(cfg: "Config") -> str | None:
    """Enable the rotating log file. Returns an error text if LOG_DIR is not usable (stdout keeps working)."""
    global _file_log
    if not cfg.log_dir:
        return None
    path = os.path.join(cfg.log_dir, LOG_FILE_NAME)
    try:
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=cfg.log_max_bytes, backupCount=cfg.log_backups, encoding="utf-8")
    except OSError as e:
        return f"cannot write {path}: {e.strerror or e}"
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("fritz-watchdog")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logging.raiseExceptions = False  # a full or vanished disk must not stop the watchdog
    _file_log = logger
    return None


# ------------------------------------------------------------------------------ internet probes

def run_probe(probe: str, timeout: float) -> tuple:
    """Return (ok, detail). No ICMP, so the container needs no NET_RAW capability."""
    start = time.monotonic()
    try:
        if probe.startswith("tcp:"):
            host, _, port = probe[4:].rpartition(":")
            with socket.create_connection((host, int(port)), timeout=timeout):
                pass
        else:
            req = urllib.request.Request(probe, headers={"User-Agent": "fritz-watchdog"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    code = resp.status
            except urllib.error.HTTPError as e:  # any HTTP answer proves connectivity, 5xx aside
                code = e.code
            if code >= 500:
                return False, f"HTTP {code}"
        return True, f"{(time.monotonic() - start) * 1000:.0f} ms"
    except Exception as e:  # noqa: BLE001 - every failure just means "probe failed"
        return False, f"{type(e).__name__}: {e}"[:120]


def probe_all(cfg: Config) -> tuple:
    """Run all probes in parallel. Returns (internet_ok, {probe: detail})."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(cfg.probes)) as pool:
        results = dict(zip(cfg.probes, pool.map(lambda p: run_probe(p, cfg.probe_timeout), cfg.probes)))
    ok_count = sum(1 for ok, _ in results.values() if ok)
    return ok_count >= cfg.probe_min_ok, {p: ("ok " if ok else "FAIL ") + d for p, (ok, d) in results.items()}


# ------------------------------------------------------------------------------ the box (TR-064)

class BoxError(Exception):
    pass


class FritzBox:
    """Thin wrapper around Tr064: auto-detects the WAN service, turns every failure into BoxError."""

    def __init__(self, wan_service: str = "auto", client_factory=Tr064):
        self.wan_service = wan_service
        self.client_factory = client_factory
        self.client = None

    def _call(self, service: str, action: str) -> dict:
        if self.client is None:
            self.client = self.client_factory()
        try:
            res = self.client.call(service, action)
        except SystemExit:  # fc.die(): unknown service on this model/firmware
            raise BoxError(f"service {service} not available") from None
        except urllib.error.HTTPError as e:
            raise BoxError(f"{service}.{action}: HTTP {e.code} {e.reason}") from None
        except (urllib.error.URLError, OSError, ET.ParseError) as e:
            raise BoxError(f"{service}.{action}: {getattr(e, 'reason', e)}") from None
        if res.get("error"):
            raise BoxError(f"{service}.{action}: UPnP error {res.get('errorCode')} {res.get('errorDescription')}")
        return res

    def detect_wan_service(self) -> str:
        if self.wan_service != "auto":
            return self.wan_service
        res = self._call("Layer3Forwarding1", "GetDefaultConnectionService")
        value = res.get("NewDefaultConnectionService", "")  # e.g. "1.WANIPConnection.1"
        parts = value.split(".")
        if len(parts) >= 3 and parts[1] in ("WANIPConnection", "WANPPPConnection"):
            return f"{parts[1]}{parts[2]}"
        raise BoxError(f"cannot detect WAN service from {value!r}; set WAN_SERVICE")

    def status(self) -> dict:
        """Read-only snapshot. A new client per snapshot, so nothing stale survives a box reboot."""
        self.client = None
        service = self.detect_wan_service()
        st = self._call(service, "GetStatusInfo")
        out = {
            "service": service,
            "status": st.get("NewConnectionStatus", ""),
            "last_error": st.get("NewLastConnectionError", ""),
            "uptime": int(st.get("NewUptime") or 0),
        }
        try:  # informational only
            link = self._call("WANCommonInterfaceConfig1", "GetCommonLinkProperties")
            out["access_type"] = link.get("NewWANAccessType")
            out["physical_link"] = link.get("NewPhysicalLinkStatus")
        except BoxError:
            pass
        try:  # mobile boxes only (LTE/5G)
            mob = self._call("X_AVM-DE_WANMobileConnection1", "GetInfoEx")
            out["mobile_tech"] = mob.get("NewCurrentAccessTechnology")
            out["rsrp"] = mob.get("NewSignalRSRP0")
        except BoxError:
            pass
        return out

    def action(self, service: str, action: str) -> None:
        self._call(service, action)


# ------------------------------------------------------------------------------ watchdog logic

class Watchdog:
    def __init__(self, cfg: Config, box, probe=None, clock=time.time, sleep=time.sleep,
                 local_now=lambda: dt.datetime.now()):
        self.cfg = cfg
        self.box = box
        self.probe = probe or (lambda: probe_all(cfg))
        self.clock = clock
        self.sleep = sleep
        self.local_now = local_now
        self.fail_streak = 0
        self.attempts = 0          # reconnects since the internet was last OK (or since the last reboot)
        self.hold_until = 0.0      # no new action before this time (recovery/reboot wait)
        self.reconnects = []       # timestamps, rolling 24 h
        self.reboots = []
        self.rounds = 0
        self.last_ok = None
        self.last_action = None
        self.state = "starting"

    # -- helpers
    def _recent(self, stamps: list) -> list:
        now = self.clock()
        stamps[:] = [t for t in stamps if now - t < DAY]
        return stamps

    def in_quiet_window(self) -> bool:
        now = self.local_now()
        m = now.hour * 60 + now.minute
        for start, end in self.cfg.quiet_windows:
            if (start <= m < end) if start <= end else (m >= start or m < end):
                return True
        return False

    def _blocked(self, kind: str) -> str | None:
        """Why an action of this kind may not run now, or None."""
        cfg, now = self.cfg, self.clock()
        if kind == "reboot":
            stamps, cooldown, limit = self._recent(self.reboots), cfg.reboot_cooldown, cfg.max_reboots_per_day
        else:
            stamps, cooldown, limit = self._recent(self.reconnects), cfg.reconnect_cooldown, cfg.max_reconnects_per_day
        if len(stamps) >= limit:
            return f"daily limit reached ({len(stamps)}/{limit} in 24 h)"
        if stamps and now - stamps[-1] < cooldown:
            return f"cooldown ({int(cooldown - (now - stamps[-1]))} s left)"
        return None

    def write_status(self) -> None:
        data = {"ts": self.clock(), "state": self.state, "fail_streak": self.fail_streak, "attempts": self.attempts,
                "last_ok": self.last_ok, "last_action": self.last_action, "dry_run": self.cfg.dry_run,
                "check_interval": self.cfg.check_interval}
        try:
            tmp = self.cfg.status_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.cfg.status_file)
        except OSError:
            pass

    # -- one round
    def round(self, fail_rounds: int | None = None) -> str:
        """Run one check. Returns online | degraded | outage | box_unreachable."""
        self.rounds += 1
        ok, results = self.probe()
        if ok:
            if self.fail_streak or self.attempts:
                log("recovered", failed_rounds=self.fail_streak, attempts=self.attempts, probes=results)
            elif self.state == "starting" or (self.cfg.log_ok_every and self.rounds % self.cfg.log_ok_every == 0):
                log("online", probes=results)
            self.fail_streak = self.attempts = 0
            self.last_ok = self.clock()
            self.state = "online"
            return self.state

        self.fail_streak += 1
        needed = fail_rounds or self.cfg.fail_rounds
        if self.fail_streak < needed:
            log("probes_failed", "warning", round=self.fail_streak, of=needed, probes=results)
            self.state = "degraded"
            return self.state

        try:
            box = self.box.status()
        except BoxError as e:
            log("box_unreachable", "error", reason=str(e), probes=results,
                note="no internet and no answer from the box: LAN/box problem, no action")
            self.state = "box_unreachable"
            return self.state

        self.state = "outage"
        log("outage", "warning", round=self.fail_streak, box=box, probes=results)
        now = self.clock()
        if now < self.hold_until:
            log("waiting", wait_s=int(self.hold_until - now), reason="recovery/reboot wait after last action")
        elif self.in_quiet_window():
            log("skipped", reason="quiet window")
        else:
            self._act(box)
        return self.state

    def _act(self, box: dict) -> None:
        cfg = self.cfg
        if cfg.reboot_enabled and self.attempts >= cfg.reboot_after_reconnects:
            blocked = self._blocked("reboot")
            if blocked is None:
                self._reboot()
                return
            log("reboot_blocked", "warning", reason=blocked)

        connected = box.get("status") == "Connected"
        mode = cfg.on_connected if connected else cfg.on_disconnected
        if mode == "none":
            log("no_action", reason=f"{'ON_CONNECTED' if connected else 'ON_DISCONNECTED'}=none",
                box_status=box.get("status"))
            return
        blocked = self._blocked("reconnect")
        if blocked:
            log("reconnect_blocked", "warning", reason=blocked)
            return
        self._reconnect(box["service"], mode)

    def _do(self, service: str, action: str) -> bool:
        if self.cfg.dry_run:
            log("dry_run", would_call=f"{service}.{action}")
            return True
        try:
            self.box.action(service, action)
            log("action", call=f"{service}.{action}")
            return True
        except BoxError as e:
            log("action_failed", "error", call=f"{service}.{action}", reason=str(e))
            return False

    def _reconnect(self, service: str, mode: str) -> None:
        now = self.clock()
        self.reconnects.append(now)
        self.attempts += 1
        self.hold_until = now + self.cfg.recovery_wait
        self.last_action = {"ts": now, "action": mode, "dry_run": self.cfg.dry_run}
        log(mode, "warning", attempt=self.attempts, service=service, dry_run=self.cfg.dry_run)
        if mode == "connect":
            self._do(service, "RequestConnection")
            return
        self._do(service, "ForceTermination")
        if self.cfg.dry_run:
            return
        self.sleep(self.cfg.reconnect_settle)
        try:
            status = self.box.status().get("status")
        except BoxError as e:
            log("status_after_reconnect", "warning", reason=str(e))
            return
        log("status_after_reconnect", status=status)
        if status != "Connected":
            self._do(service, "RequestConnection")

    def _reboot(self) -> None:
        now = self.clock()
        self.reboots.append(now)
        self.attempts = 0
        self.hold_until = now + self.cfg.reboot_wait
        self.last_action = {"ts": now, "action": "reboot", "dry_run": self.cfg.dry_run}
        log("reboot", "warning", dry_run=self.cfg.dry_run)
        self._do("DeviceConfig1", "Reboot")

    # -- loop
    def run(self) -> None:
        log("start", config=redacted_config(self.cfg))
        while True:
            started = self.clock()
            try:
                self.round()
            except Exception as e:  # noqa: BLE001 - the loop must survive anything
                log("round_failed", "error", reason=f"{type(e).__name__}: {e}")
            self.write_status()
            self.sleep(max(1.0, self.cfg.check_interval - (self.clock() - started)))


# ------------------------------------------------------------------------------ CLI

def redacted_config(cfg: Config) -> dict:
    data = asdict(cfg)
    data["quiet_windows"] = [f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}" for s, e in cfg.quiet_windows]
    data["fritz_host"] = fc.host()
    data["fritz_user"] = fc.setting("FRITZ_USER", "")
    data["fritz_password"] = "set" if fc.setting("FRITZ_PASSWORD") else "MISSING"
    data["fritz_tls"] = fc.use_tls()
    return data


def health(cfg: Config) -> int:
    try:
        with open(cfg.status_file, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return EXIT_HEALTH
    max_age = 3 * cfg.check_interval + cfg.reconnect_settle + 120
    return EXIT_OK if time.time() - float(data.get("ts", 0)) < max_age else EXIT_HEALTH


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Reconnect/reboot a FRITZ!Box when the internet is down.")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="one check, act right away on an outage")
    group.add_argument("--check-box", action="store_true", help="print the box's WAN status (read-only)")
    group.add_argument("--print-config", action="store_true", help="print the effective settings")
    group.add_argument("--health", action="store_true", help="container health check")
    ap.add_argument("--dry-run", action="store_true", help="force DRY_RUN=1")
    args = ap.parse_args(argv)

    try:
        cfg = Config.from_env()
    except ConfigError as e:
        log("config_error", "error", reason=str(e))
        return EXIT_CONFIG
    if args.dry_run:
        cfg.dry_run = True
    if args.health:
        return health(cfg)
    if args.print_config:
        print(json.dumps(redacted_config(cfg), indent=2))
        return EXIT_OK
    if not fc.setting("FRITZ_PASSWORD"):
        log("config_error", "error", reason="FRITZ_PASSWORD is not set (FRITZ_USER is usually needed too)")
        return EXIT_CONFIG

    box = FritzBox(cfg.wan_service)
    if args.check_box:
        try:
            print(json.dumps(box.status(), indent=2, ensure_ascii=False))
            return EXIT_OK
        except BoxError as e:
            log("box_unreachable", "error", reason=str(e))
            return EXIT_BOX

    wd = Watchdog(cfg, box)
    if args.once:
        state = wd.round(fail_rounds=1)
        wd.write_status()
        return {"online": EXIT_OK, "box_unreachable": EXIT_BOX}.get(state, EXIT_OUTAGE)
    # Only the long-running loop writes the file, so ad-hoc CLI runs never rotate it underneath.
    problem = setup_file_log(cfg)
    if problem:
        log("log_file_disabled", "warning", reason=problem)
    try:
        wd.run()
    except KeyboardInterrupt:
        log("stop")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
