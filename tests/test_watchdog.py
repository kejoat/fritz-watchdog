# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 kejoat
"""Unit tests for the watchdog logic: fake box, fake probes, fake clock. No network access.

Run: python -m unittest discover -s tests -v
"""

import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import fritz_watchdog as fw  # noqa: E402


class FakeBox:
    def __init__(self, status="Connected", reachable=True, status_after=None, auth_ok=True):
        self.status_value = status
        self.status_after = status_after  # status reported after ForceTermination
        self.reachable = reachable
        self.auth_ok = auth_ok
        self.calls = []

    def status(self):
        if not self.reachable:
            raise fw.BoxError("unreachable")
        if not self.auth_ok:
            raise fw.BoxAuthError("HTTP 401 Unauthorized")
        return {"service": "WANIPConnection1", "status": self.status_value}

    def action(self, service, action):
        self.calls.append(f"{service}.{action}")
        if action == "ForceTermination" and self.status_after:
            self.status_value = self.status_after


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def make(box=None, online=False, hour=12, **overrides):
    cfg = fw.Config(dry_run=False, fail_rounds=3, reconnect_settle=0, **overrides)
    clock = Clock()
    state = {"online": online}
    wd = fw.Watchdog(cfg, box or FakeBox(), probe=lambda: (state["online"], {}), clock=clock,
                     sleep=clock.sleep, local_now=lambda: dt.datetime(2026, 1, 1, hour, 0))
    return wd, clock, state


def quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


class WatchdogTests(unittest.TestCase):
    def rounds(self, wd, n):
        return [quiet(wd.round) for _ in range(n)]

    def test_online_does_nothing(self):
        wd, _, _ = make(online=True)
        self.assertEqual(self.rounds(wd, 5), ["online"] * 5)
        self.assertEqual(wd.box.calls, [])

    def test_needs_consecutive_failed_rounds(self):
        wd, _, state = make()
        self.assertEqual(self.rounds(wd, 2), ["degraded", "degraded"])
        state["online"] = True
        quiet(wd.round)
        state["online"] = False
        self.assertEqual(self.rounds(wd, 2), ["degraded", "degraded"])
        self.assertEqual(wd.box.calls, [])

    def test_reconnect_when_box_says_connected(self):
        wd, _, _ = make()
        self.assertEqual(self.rounds(wd, 3)[-1], "outage")
        self.assertEqual(wd.box.calls, ["WANIPConnection1.ForceTermination"])

    def test_request_connection_if_not_back_after_reconnect(self):
        wd, _, _ = make(box=FakeBox(status_after="Disconnected"))
        self.rounds(wd, 3)
        self.assertEqual(wd.box.calls, ["WANIPConnection1.ForceTermination", "WANIPConnection1.RequestConnection"])

    def test_on_connected_none(self):
        wd, _, _ = make(on_connected="none")
        self.rounds(wd, 5)
        self.assertEqual(wd.box.calls, [])

    def test_disconnected_modes(self):
        wd, _, _ = make(box=FakeBox(status="Disconnected"), on_disconnected="connect")
        self.rounds(wd, 3)
        self.assertEqual(wd.box.calls, ["WANIPConnection1.RequestConnection"])
        wd, _, _ = make(box=FakeBox(status="Disconnected"), on_disconnected="none")
        self.rounds(wd, 5)
        self.assertEqual(wd.box.calls, [])

    def test_box_unreachable_means_no_action(self):
        wd, _, _ = make(box=FakeBox(reachable=False))
        self.assertEqual(self.rounds(wd, 4)[-1], "box_unreachable")
        self.assertEqual(wd.box.calls, [])

    def test_refused_login_is_reported_as_such(self):
        wd, _, _ = make(box=FakeBox(auth_ok=False))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            states = [wd.round() for _ in range(4)]
        self.assertEqual(states[-1], "box_auth_failed")
        self.assertEqual(wd.box.calls, [])
        events = [json.loads(line)["event"] for line in out.getvalue().splitlines()]
        self.assertIn("box_auth_failed", events)
        self.assertNotIn("box_unreachable", events)

    def test_startup_check_logs_box_state(self):
        for box, event in ((FakeBox(), "box_ok"), (FakeBox(auth_ok=False), "box_auth_failed"),
                           (FakeBox(reachable=False), "box_unreachable")):
            wd, _, _ = make(box=box)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                wd.check_box()
            self.assertEqual(json.loads(out.getvalue())["event"], event)
            self.assertEqual(box.calls, [])

    def test_dry_run_never_calls_the_box(self):
        wd, clock, _ = make(reboot_enabled=True)
        wd.cfg.dry_run = True
        for _ in range(200):
            quiet(wd.round)
            clock.t += 60
        self.assertEqual(wd.box.calls, [])
        self.assertGreater(len(wd.reconnects), 0)
        self.assertGreater(len(wd.reboots), 0)

    def test_quiet_window_skips_actions(self):
        wd, _, _ = make(hour=3, quiet_windows=fw.parse_windows("02:50-03:10"))
        self.rounds(wd, 5)
        self.assertEqual(wd.box.calls, [])
        wd, _, _ = make(hour=0, quiet_windows=fw.parse_windows("23:30-00:30"))  # wraps midnight
        self.rounds(wd, 5)
        self.assertEqual(wd.box.calls, [])

    def test_recovery_wait_and_cooldown(self):
        wd, clock, _ = make(recovery_wait=180, reconnect_cooldown=600)
        self.rounds(wd, 3)
        for _ in range(9):  # 9 minutes later: still within the cooldown
            clock.t += 60
            quiet(wd.round)
        self.assertEqual(len(wd.box.calls), 1)
        clock.t += 60
        quiet(wd.round)
        self.assertEqual(len(wd.box.calls), 2)

    def test_daily_limit(self):
        wd, clock, _ = make(recovery_wait=0, reconnect_cooldown=0, max_reconnects_per_day=3)
        for _ in range(20):
            quiet(wd.round)
            clock.t += 60
        self.assertEqual(len(wd.box.calls), 3)
        clock.t += fw.DAY
        quiet(wd.round)
        self.assertEqual(len(wd.box.calls), 4)

    def test_reboot_only_when_enabled(self):
        wd, clock, _ = make(recovery_wait=0, reconnect_cooldown=0)
        for _ in range(10):
            quiet(wd.round)
            clock.t += 60
        self.assertNotIn("DeviceConfig1.Reboot", wd.box.calls)

    def test_reboot_after_failed_reconnects(self):
        wd, clock, _ = make(recovery_wait=0, reconnect_cooldown=0, reboot_enabled=True,
                            reboot_after_reconnects=2, reboot_wait=300)
        for _ in range(5):
            quiet(wd.round)
            clock.t += 60
        self.assertEqual(wd.box.calls, ["WANIPConnection1.ForceTermination"] * 2 + ["DeviceConfig1.Reboot"])
        self.assertEqual(wd.attempts, 0)

    def test_recovery_resets_escalation(self):
        wd, clock, state = make(recovery_wait=0, reconnect_cooldown=0, reboot_enabled=True,
                                reboot_after_reconnects=2)
        self.rounds(wd, 3)
        state["online"] = True
        quiet(wd.round)
        self.assertEqual(wd.attempts, 0)


class FritzBoxTests(unittest.TestCase):
    class Client:
        def __init__(self, default="1.WANPPPConnection.1", fail=None):
            self.default, self.fail, self.calls = default, fail, []

        def call(self, service, action):
            self.calls.append((service, action))
            if self.fail:
                raise self.fail
            if action == "GetDefaultConnectionService":
                return {"NewDefaultConnectionService": self.default}
            if action == "GetStatusInfo":
                return {"NewConnectionStatus": "Connected", "NewUptime": "42"}
            return {"error": True, "errorCode": 401, "errorDescription": "Invalid Action"}

    def test_detects_wan_service(self):
        box = fw.FritzBox(client_factory=lambda: self.Client())
        st = box.status()
        self.assertEqual(st["service"], "WANPPPConnection1")
        self.assertEqual(st["status"], "Connected")
        self.assertEqual(st["uptime"], 42)

    def test_explicit_service(self):
        client = self.Client()
        fw.FritzBox("WANIPConnection1", client_factory=lambda: client).status()
        self.assertEqual(client.calls[0], ("WANIPConnection1", "GetStatusInfo"))

    def test_errors_become_box_error(self):
        for exc in (urllib.error.URLError("down"), TimeoutError(), SystemExit(2)):
            box = fw.FritzBox(client_factory=lambda e=exc: self.Client(fail=e))
            with self.assertRaises(fw.BoxError):
                box.status()

    def test_refused_login_becomes_box_auth_error(self):
        for code in (401, 403, 503):
            exc = urllib.error.HTTPError("https://box", code, "denied", None, None)
            box = fw.FritzBox(client_factory=lambda e=exc: self.Client(fail=e))
            with self.assertRaises(fw.BoxAuthError):
                box.status()
        exc = urllib.error.HTTPError("https://box", 404, "not found", None, None)
        box = fw.FritzBox(client_factory=lambda: self.Client(fail=exc))
        with self.assertRaises(fw.BoxError) as ctx:
            box.status()
        self.assertNotIsInstance(ctx.exception, fw.BoxAuthError)

    def test_upnp_606_becomes_box_auth_error(self):
        client = self.Client()
        client.call = lambda service, action: {"error": True, "errorCode": 606, "errorDescription": "Action Not Authorized"}
        with self.assertRaises(fw.BoxAuthError):
            fw.FritzBox("WANIPConnection1", client_factory=lambda: client).status()


class FileLogTests(unittest.TestCase):
    def tearDown(self):
        if fw._file_log is not None:
            for h in fw._file_log.handlers:
                h.close()
        fw._file_log = None

    def test_writes_and_rotates(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = fw.Config(log_dir=tmp, log_max_bytes=2000, log_backups=2)
            self.assertIsNone(fw.setup_file_log(cfg))
            for i in range(100):
                quiet(fw.log, "online", round=i)
            self.tearDown()
            files = sorted(os.listdir(tmp))
            self.assertEqual(files, ["fritz-watchdog.log", "fritz-watchdog.log.1", "fritz-watchdog.log.2"])
            with open(os.path.join(tmp, "fritz-watchdog.log"), encoding="utf-8") as fh:
                last = json.loads(fh.read().splitlines()[-1])
            self.assertEqual(last["round"], 99)

    def test_unusable_dir_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = fw.Config(log_dir=os.path.join(tmp, "missing"))
            self.assertIn("cannot write", fw.setup_file_log(cfg))
            self.assertIsNone(fw._file_log)

    def test_disabled_by_default(self):
        self.assertIsNone(fw.setup_file_log(fw.Config()))
        self.assertIsNone(fw._file_log)


class ConfigTests(unittest.TestCase):
    def test_parse_windows(self):
        self.assertEqual(fw.parse_windows("03:20-03:35, 23:50-00:10"), [(200, 215), (1430, 10)])
        for bad in ("3-4", "25:00-01:00", "03:20"):
            with self.assertRaises(fw.ConfigError):
                fw.parse_windows(bad)

    def test_parse_probes(self):
        self.assertEqual(fw.parse_probes("tcp:example.org:443, http://example.org/"),
                         ["tcp:example.org:443", "http://example.org/"])
        for bad in ("", "tcp:example.org", "example.org"):
            with self.assertRaises(fw.ConfigError):
                fw.parse_probes(bad)

    def test_defaults_are_safe(self):
        keys = ("DRY_RUN", "REBOOT_ENABLED", "ON_CONNECTED", "ON_DISCONNECTED", "QUIET_WINDOWS")
        saved = {k: os.environ.pop(k, None) for k in keys}
        try:
            cfg = fw.Config.from_env()
        finally:
            os.environ.update({k: v for k, v in saved.items() if v is not None})
        self.assertTrue(cfg.dry_run)
        self.assertFalse(cfg.reboot_enabled)
        self.assertEqual((cfg.on_connected, cfg.on_disconnected), ("reconnect", "reconnect"))


if __name__ == "__main__":
    unittest.main()
