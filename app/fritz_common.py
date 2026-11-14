# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 kejoat
"""Shared helpers for the fritzbox skill scripts (Python 3.8+, standard library only).

Configuration comes from environment variables. If the credential-file loader below is present, it fills in
missing FRITZ_* values from the shared credential file in the repo root. Credentials therefore never
appear in command lines, shell history or agent transcripts:

  FRITZ_HOST      host or IP of the box (default: fritz.box)
  FRITZ_USER      FRITZ!Box user name (FRITZ!OS >= 7.24 always needs one)
  FRITZ_PASSWORD  password of that user
  FRITZ_TLS       "1" to use HTTPS (self-signed box certificate is accepted)
  FRITZ_VERIFY    "1" to verify the TLS certificate (only if you installed a trusted cert)
  FRITZ_TIMEOUT   request timeout in seconds (default 15)
"""

from __future__ import annotations

import hashlib
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

INVALID_SID = "0000000000000000"

def _load_credential_file() -> None:
    """Fill missing FRITZ_* settings from the shared credential file in the repo root.

    Searches upwards from this script for ".env" (KEY=VALUE lines, '#' comments, optional
    'export ' prefix and quotes). Only FRITZ_* keys are used; real environment variables win.
    Independent of other skills' loaders. Values are never printed.
    """
    name = ".env"
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        path = os.path.join(here, name)
        if os.path.isfile(path):
            with open(path, encoding="utf-8-sig") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    if key.startswith("export "):
                        key = key[7:].strip()
                    value = value.strip().strip('"').strip("'")
                    if key.startswith("FRITZ_") and value and not os.environ.get(key):
                        os.environ[key] = value
            return
        parent = os.path.dirname(here)
        if parent == here:
            return
        here = parent


_load_credential_file()

try:  # Windows consoles default to a legacy code page; FRITZ!OS returns UTF-8 (e.g. NBSP, umlauts)
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

def setting(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def host() -> str:
    return setting("FRITZ_HOST", "fritz.box")


def use_tls() -> bool:
    return setting("FRITZ_TLS", "0") == "1"


def timeout() -> float:
    return float(setting("FRITZ_TIMEOUT", "15"))


def ssl_context() -> ssl.SSLContext:
    """FRITZ!Boxes use a self-signed certificate by default -> no verification unless asked."""
    if setting("FRITZ_VERIFY", "0") == "1":
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def base_url() -> str:
    return f"{'https' if use_tls() else 'http'}://{host()}"


def credentials(required: bool = True) -> tuple[str, str]:
    user = setting("FRITZ_USER", "")
    password = setting("FRITZ_PASSWORD")
    if required and password is None:
        die("FRITZ_PASSWORD is not set (FRITZ_USER is usually needed too). Add the FRITZ_* keys to the "
            "credential file in the repo root (see its example template) or set them in the environment; "
            "never pass secrets as arguments.")
    return user or "", password or ""


def die(msg: str, code: int = 2) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def http(url: str, data: bytes | None = None, headers: dict | None = None,
         method: str | None = None, opener: urllib.request.OpenerDirector | None = None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    if opener is not None:
        return opener.open(req, timeout=timeout())
    return urllib.request.urlopen(req, timeout=timeout(), context=ssl_context())


# --------------------------------------------------------------------------------------
# Web/SID login (login_sid.lua?version=2) - used by AHA HTTP, Smart Home REST API, web UI
# --------------------------------------------------------------------------------------

def pbkdf2_response(challenge: str, password: str) -> str:
    """Challenge '2$<iter1>$<salt1>$<iter2>$<salt2>' -> '<salt2>$<hex(hash2)>'."""
    _, iter1, salt1, iter2, salt2 = challenge.split("$")
    hash1 = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt1), int(iter1))
    hash2 = hashlib.pbkdf2_hmac("sha256", hash1, bytes.fromhex(salt2), int(iter2))
    return f"{salt2}${hash2.hex()}"


def md5_response(challenge: str, password: str) -> str:
    """Legacy (< FRITZ!OS 7.24): md5(utf16le('<challenge>-<password>')), chars > 255 -> '.'."""
    pw = "".join(c if ord(c) <= 255 else "." for c in password)
    digest = hashlib.md5(f"{challenge}-{pw}".encode("utf-16-le")).hexdigest()
    return f"{challenge}-{digest}"


def _session_info(data: dict | None = None) -> ET.Element:
    url = base_url() + "/login_sid.lua?version=2"
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    hdrs = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
    with http(url, body, hdrs) as resp:
        return ET.fromstring(resp.read())


def session_info(sid: str | None = None) -> dict:
    """Return parsed SessionInfo (optionally validating a given SID)."""
    root = _session_info({"sid": sid} if sid else None)
    rights = {}
    names = root.findall("Rights/Name")
    accesses = root.findall("Rights/Access")
    for n, a in zip(names, accesses):
        rights[n.text] = int(a.text)
    return {
        "sid": root.findtext("SID"),
        "challenge": root.findtext("Challenge"),
        "blocktime": int(root.findtext("BlockTime") or 0),
        "users": [u.text for u in root.findall("Users/User")],
        "last_user": next((u.text for u in root.findall("Users/User") if u.get("last") == "1"), None),
        "rights": rights,  # 1 = read, 2 = read/write
    }


def login(user: str | None = None, password: str | None = None) -> str:
    """Perform challenge-response login and return a SID. Raises on failure."""
    if password is None:
        user, password = credentials()
    state = session_info()
    if state["blocktime"] > 0:
        # Every failed login doubles the block time on the box - waiting is mandatory.
        time.sleep(state["blocktime"])
        state = session_info()
    challenge = state["challenge"]
    response = pbkdf2_response(challenge, password) if challenge.startswith("2$") \
        else md5_response(challenge, password)
    root = _session_info({"username": user or "", "response": response})
    sid = root.findtext("SID")
    if not sid or sid == INVALID_SID:
        bt = root.findtext("BlockTime")
        raise PermissionError(f"login failed (wrong user/password or missing rights); BlockTime={bt}")
    return sid


def logout(sid: str) -> None:
    _session_info({"logout": "1", "sid": sid})


def cached_sid() -> str:
    """Reuse FRITZ_SID if set and still valid, otherwise log in.

    The box allows only a limited number of parallel sessions, so long-running tools should
    keep one SID (valid 20 min, extended on every use) instead of logging in per request.
    """
    sid = setting("FRITZ_SID")
    if sid and session_info(sid)["sid"] == sid:
        return sid
    return login()
