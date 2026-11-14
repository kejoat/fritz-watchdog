#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 kejoat
"""Minimal, dependency-free TR-064 (SOAP) client for FRITZ! devices.

Settings (environment): FRITZ_HOST (default fritz.box), FRITZ_USER, FRITZ_PASSWORD,
FRITZ_TLS=1 (HTTPS on the security port, normally 49443), FRITZ_VERIFY=1 (verify cert),
FRITZ_TIMEOUT, FRITZ_TR064_PORT (override port).

Commands
  services                         list all services: short id, serviceType, controlURL, SCPD
  actions  <service>               list actions of a service with IN/OUT arguments (from SCPD)
  call     <service> <action> [Arg=Value ...] [--token UUID] [--raw]
                                   execute an action, print OUT arguments as JSON
  fetch    <url-or-path>           HTTP GET a URL returned by an action (host list, call list,
                                   phonebook, mesh JSON...) and print the body
  info                             shortcut: DeviceInfo GetInfo + security port

<service> may be given as serviceId short name ("Hosts1", "WANIPConnection1",
"X_AVM-DE_HostFilter1"), full serviceType ("urn:dslforum-org:service:Hosts:1"), or the
type name without prefix ("Hosts:1", "Hosts"). Argument names may omit the "New" prefix
(MACAddress=... becomes NewMACAddress=... if the SCPD says so).

Exit codes: 0 ok, 1 SOAP/UPnP error (JSON with errorCode on stdout), 2 usage/config error,
3 HTTP auth error (401 = bad credentials, 503 = too many failed auths -> wait).

Examples
  python tr064.py services
  python tr064.py actions Hosts1
  python tr064.py call DeviceInfo1 GetInfo
  python tr064.py call Hosts1 GetSpecificHostEntry NewMACAddress=AA:BB:CC:DD:EE:FF
  python tr064.py call Hosts1 X_AVM-DE_GetHostListPath   # -> then: tr064.py fetch <path>
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fritz_common as fc  # noqa: E402

DESC_PATH = "/tr64desc.xml"
NS_DEV = "{urn:dslforum-org:device-1-0}"
NS_SVC = "{urn:dslforum-org:service-1-0}"
NS_SOAP = "{http://schemas.xmlsoap.org/soap/envelope/}"

UPNP_ERRORS = {
    401: "Invalid Action (unknown or obsolete action)",
    402: "Invalid Args (wrong number/name of arguments)",
    501: "Action Failed",
    600: "Argument Value Invalid",
    601: "Argument Value Out of Range",
    606: "Action not authorized (user lacks required right)",
    713: "SpecifiedArrayIndexInvalid (index beyond list end)",
    714: "NoSuchEntryInArray (MAC/IP/entry not found)",
    820: "Internal Error",
    866: "Second factor authentication required (see references/tr064-system-management.md)",
    867: "Second factor authentication blocked (wait up to 60 min)",
    868: "Second factor authentication busy (retry in ~2 min)",
}


class Tr064:
    def __init__(self):
        self.user, self.password = fc.credentials(required=False)
        self.tls = fc.use_tls()
        self.host = fc.host()
        self.port = int(fc.setting("FRITZ_TR064_PORT", "49443" if self.tls else "49000"))
        self.base = f"{'https' if self.tls else 'http'}://{self.host}:{self.port}"
        handlers = [urllib.request.HTTPSHandler(context=fc.ssl_context())]
        if self.password:
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, self.base, self.user, self.password)
            handlers.append(urllib.request.HTTPDigestAuthHandler(mgr))
        self.opener = urllib.request.build_opener(*handlers)
        self._services = None
        self._scpd = {}

    # ---------------------------------------------------------------- discovery
    def services(self) -> list:
        if self._services is None:
            with fc.http(self.base + DESC_PATH, opener=self.opener) as r:
                root = ET.fromstring(r.read())
            out = []
            for s in root.iter(NS_DEV + "service"):
                sid = s.findtext(NS_DEV + "serviceId") or ""
                out.append({
                    "id": sid.split(":")[-1],
                    "serviceType": s.findtext(NS_DEV + "serviceType"),
                    "serviceId": sid,
                    "controlURL": s.findtext(NS_DEV + "controlURL"),
                    "SCPDURL": s.findtext(NS_DEV + "SCPDURL"),
                })
            self._services = out
        return self._services

    def resolve(self, name: str) -> dict:
        svcs = self.services()
        n = name.lower()
        for key in ("id", "serviceType", "serviceId"):
            for s in svcs:
                if (s[key] or "").lower() == n:
                    return s
        for s in svcs:  # "Hosts:1" or "Hosts"
            st = (s["serviceType"] or "").split(":service:")[-1].lower()
            if st == n or st.split(":")[0] == n:
                return s
        fc.die(f"unknown service '{name}'. Run 'tr064.py services' to list them.")

    def scpd(self, svc: dict) -> dict:
        url = svc["SCPDURL"]
        if url not in self._scpd:
            with fc.http(self.base + url, opener=self.opener) as r:
                root = ET.fromstring(r.read())
            actions = {}
            for a in root.iter(NS_SVC + "action"):
                args = [{"name": x.findtext(NS_SVC + "name"),
                         "direction": x.findtext(NS_SVC + "direction"),
                         "var": x.findtext(NS_SVC + "relatedStateVariable")}
                        for x in a.iter(NS_SVC + "argument")]
                actions[a.findtext(NS_SVC + "name")] = args
            types = {v.findtext(NS_SVC + "name"): v.findtext(NS_SVC + "dataType")
                     for v in root.iter(NS_SVC + "stateVariable")}
            self._scpd[url] = {"actions": actions, "types": types}
        return self._scpd[url]

    # ---------------------------------------------------------------- calls
    def call(self, service: str, action: str, args: dict = None, token: str = None) -> dict:
        svc = self.resolve(service)
        args = dict(args or {})
        try:
            spec = self.scpd(svc)["actions"].get(action)
        except Exception:
            spec = None
        if spec is not None:
            in_names = [a["name"] for a in spec if a["direction"] == "in"]
            fixed = {}
            for k, v in args.items():
                if k not in in_names and ("New" + k) in in_names:
                    k = "New" + k
                fixed[k] = v
            args = fixed
            missing = [a for a in in_names if a not in args]
            if missing:
                fc.die(f"{action} needs IN args: {', '.join(in_names)} (missing: {', '.join(missing)})")
        body_args = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in args.items())
        header = ""
        if token:  # 2FA token from X_AVM-DE_Auth SetConfig(NewAction=start)
            header = ('<s:Header><avm:token xmlns:avm="avm.de" s:mustUnderstand="1">'
                      f"{escape(token)}</avm:token></s:Header>")
        st = svc["serviceType"]
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'{header}<s:Body><u:{action} xmlns:u="{st}">{body_args}</u:{action}>'
            "</s:Body></s:Envelope>").encode("utf-8")
        hdrs = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{st}#{action}"'}
        try:
            with fc.http(self.base + svc["controlURL"], envelope, hdrs, "POST", self.opener) as r:
                return self._parse_ok(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code == 500 and body:
                return self._parse_fault(body)
            raise

    @staticmethod
    def _parse_ok(raw: bytes) -> dict:
        body = ET.fromstring(raw).find(NS_SOAP + "Body")
        if body is None or not len(body):
            return {}
        return {child.tag.split("}")[-1]: (child.text or "") for child in list(body)[0]}

    @staticmethod
    def _parse_fault(raw: bytes) -> dict:
        code = desc = None
        for el in ET.fromstring(raw).iter():
            tag = el.tag.split("}")[-1]
            if tag == "errorCode":
                code = el.text
            elif tag == "errorDescription":
                desc = el.text
        code = int(code) if code and code.isdigit() else code
        return {"error": True, "errorCode": code, "errorDescription": desc,
                "hint": UPNP_ERRORS.get(code, "")}

    def fetch(self, url: str) -> bytes:
        # Git Bash (MSYS) may rewrite "/x.lua" to "C:/Program Files/Git/x.lua": undo that.
        m = re.match(r"^[A-Za-z]:[/\\].*?[/\\]Git([/\\].*)$", url)
        if m:
            url = m.group(1).replace("\\", "/")
        if url.startswith("/"):
            url = self.base + url
        with fc.http(url, opener=self.opener) as r:
            return r.read()


def parse_kv(items):
    args, token, raw = {}, None, False
    it = iter(items)
    for a in it:
        if a == "--token":
            token = next(it)
        elif a == "--raw":
            raw = True
        elif "=" in a:
            k, v = a.split("=", 1)
            args[k] = v
        else:
            fc.die(f"argument '{a}' must be Name=Value")
    return args, token, raw


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 2
    c = Tr064()
    cmd = argv[1]
    try:
        if cmd == "services":
            for s in c.services():
                print(f"{s['id']:<34} {s['serviceType']:<55} {s['controlURL']:<38} {s['SCPDURL']}")
        elif cmd == "actions" and len(argv) > 2:
            svc = c.resolve(argv[2])
            spec = c.scpd(svc)
            print(f"# {svc['serviceType']}  control={svc['controlURL']}")
            for name, args in sorted(spec["actions"].items()):
                ins = ", ".join(f"{a['name']}:{spec['types'].get(a['var'], '?')}"
                                for a in args if a["direction"] == "in")
                outs = ", ".join(f"{a['name']}:{spec['types'].get(a['var'], '?')}"
                                 for a in args if a["direction"] == "out")
                print(f"{name}({ins}) -> {outs}")
        elif cmd == "call" and len(argv) > 3:
            args, token, raw = parse_kv(argv[4:])
            res = c.call(argv[2], argv[3], args, token)
            print(json.dumps(res, indent=None if raw else 2, ensure_ascii=False))
            return 1 if res.get("error") else 0
        elif cmd == "fetch" and len(argv) > 2:
            sys.stdout.write(c.fetch(argv[2]).decode("utf-8", "replace"))
        elif cmd == "info":
            res = c.call("DeviceInfo1", "GetInfo")
            log = res.pop("NewDeviceLog", "")
            if log:  # full log is huge; show the newest lines only (use 'call DeviceInfo1 GetInfo' for all)
                res["NewDeviceLog(last 5)"] = log.splitlines()[:5]
            res.update(c.call("DeviceInfo1", "GetSecurityPort"))
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(__doc__)
            return 2
    except urllib.error.HTTPError as e:
        hint = {401: "unauthorized: check FRITZ_USER/FRITZ_PASSWORD and the user's rights",
                403: "forbidden",
                503: "too many unauthenticated requests - wait before retrying",
                404: "service/URL not found on this model/firmware"}.get(e.code, "")
        fc.die(f"HTTP {e.code} {e.reason} {hint}", 3)
    except urllib.error.URLError as e:
        fc.die(f"cannot reach {c.base}: {e.reason}. TR-064 is LAN-only and must be enabled "
               "(Home Network > Network > Network Settings > 'Allow access for applications').")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
