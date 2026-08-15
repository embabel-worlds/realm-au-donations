#!/usr/bin/env python3
"""Run EVERY view in this realm against a live host, and fail if any returns no rows.

A declarative realm has no unit tests: its producers, joins and views are only exercised by a
running host against the real source. This IS that test. It drives each view the way the
platform's own ViewRunner does — the view body with its params substituted as escaped Cypher
literals, executed through /kg/execute (the same kgAskService.execute call) — so a view that
cannot run, or that silently returns nothing, fails here rather than in front of an audience.

EVERY view must have a case below. A view with no case is an untested view.

Usage:
    python3 scripts/test-views.py [port]        # default 8046
Env:
    EMBABEL_USER / EMBABEL_PASS   host credentials (default rod/test)
Requires the host running with this realm installed and the datasource provisioned
(docker compose up -d --wait && python3 scripts/load-aec.py), with AU_DONATIONS_PASSWORD
set in the host's environment.
"""
import base64
import json
import os
import pathlib
import re
import sys
import urllib.request

import yaml

PORT = sys.argv[1] if len(sys.argv) > 1 else "8046"
USER = os.environ.get("EMBABEL_USER", "rod")
PASS = os.environ.get("EMBABEL_PASS", "test")
URL = f"http://localhost:{PORT}/api/v1/admin/kg/execute?username={USER}"
AUTH = base64.b64encode(f"{USER}:{PASS}".encode()).decode()
VIEWS = str(pathlib.Path(__file__).resolve().parent.parent / "views" / "donations.yml")

# (view name, supplied args) — defaults fill the rest, exactly as a bare call would.
CASES = [
    ("DonorProfile", {"entityName": "MINERALOGY PTY LTD"}),
    ("DonationsMade", {"entityName": "MINERALOGY PTY LTD", "sinceFy": "2023-24", "limit": 5}),
    ("TopRecipients", {"entityName": "MINERALOGY PTY LTD", "sinceFy": "2023-24", "limit": 5}),
    ("DonationsReceived", {"entityName": "Australian Labor Party (ALP)", "sinceFy": "2024-25", "limit": 5}),
    ("MoneyIn", {"entityName": "Climate 200 Pty Limited", "sinceFy": "2024-25", "limit": 5}),
    ("YearSummary", {"entityName": "MINERALOGY PTY LTD", "limit": 5}),
]


def literal(v, t):
    if t in ("int", "integer", "long", "number"):
        return str(int(v))
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def substitute(view, args):
    body = view["cypher"].strip()
    merged = {}
    for name, spec in (view.get("params") or {}).items():
        if name in args:
            merged[name] = (args[name], spec.get("type", "string"))
        elif "default" in spec:
            merged[name] = (spec["default"], spec.get("type", "string"))
        else:
            raise SystemExit(f"{view['name']}: required param {name} not supplied")
    # longest first so $sinceFy is never clipped by a shorter $since
    for name in sorted(merged, key=len, reverse=True):
        value, typ = merged[name]
        body = re.sub(rf"\${re.escape(name)}\b", literal(value, typ), body)
    return body


def run(cypher):
    req = urllib.request.Request(
        URL,
        data=json.dumps({"cypher": cypher}).encode(),
        headers={"Authorization": f"Basic {AUTH}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def main():
    views = {v["name"]: v for v in yaml.safe_load(open(VIEWS))}
    failures = 0
    for name, args in CASES:
        view = views[name]
        cypher = substitute(view, args)
        try:
            res = run(cypher)
        except Exception as e:  # noqa: BLE001
            print(f"✗ {name}: request failed: {e}")
            failures += 1
            continue
        rows, warn, err = res.get("rows", []), res.get("warnings"), res.get("error")
        status = "✓" if rows and not err else "✗"
        if not rows or err:
            failures += 1
        print(f"{status} {name}  args={args}")
        print(f"    rows={res.get('rowCount')} ms={res.get('durationMs')} err={err}")
        if warn:
            print(f"    WARNINGS: {warn}")
        for row in rows[:3]:
            print("    ", {k: (str(v)[:44] if v is not None else None) for k, v in row.items()})
    print("\nFAILURES:" if failures else "\nALL VIEWS RETURNED ROWS", failures if failures else "")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
