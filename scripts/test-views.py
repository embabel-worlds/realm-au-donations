#!/usr/bin/env python3
"""Run EVERY view in this realm against a live host, and fail if any returns no rows.

A declarative realm has no unit tests: its producers, joins and views are only exercised by a
running host against the real source. This IS that test.

It calls the host's own view-run endpoint (`POST /api/v1/admin/kg/views/{name}/run`), so argument
merging, defaults, type coercion and literal substitution are all done by the PLATFORM — the same
path a real caller takes. An earlier version of this script re-implemented that substitution in
Python, which is a copy of platform logic that can pass while the platform's own is broken.

EVERY view must have a case below. A view with no case is an untested view.

Usage:
    python3 scripts/test-views.py [port]        # default 8046
Env:
    EMBABEL_USER / EMBABEL_PASS   host credentials (default rod/test)

Requires the host running with this realm installed and the datasource provisioned:
    docker compose up -d --wait && python3 scripts/load-aec.py
with AU_DONATIONS_PASSWORD set in the host's environment.

After editing realm YAML you do NOT need to restart the host:
    curl -X POST .../api/v1/realms/au-donations/update -H "Authorization: Basic <b64 user:pass>"
"""

import base64
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

import yaml

PORT = sys.argv[1] if len(sys.argv) > 1 else "8046"
USER = os.environ.get("EMBABEL_USER", "rod")
PASS = os.environ.get("EMBABEL_PASS", "test")
AUTH = base64.b64encode(f"{USER}:{PASS}".encode()).decode()
BASE = f"http://localhost:{PORT}/api/v1/admin/kg"
VIEWS = pathlib.Path(__file__).resolve().parent.parent / "views" / "donations.yml"

# (view, args) — declared defaults fill the rest, exactly as a bare call would.
CASES = [
    ("DonorProfile", {"entityName": "MINERALOGY PTY LTD"}),
    ("DonationsMade", {"entityName": "MINERALOGY PTY LTD", "sinceFy": "2023-24", "limit": 5}),
    ("TopRecipients", {"entityName": "MINERALOGY PTY LTD", "sinceFy": "2023-24", "limit": 5}),
    ("DonationsReceived", {"entityName": "Australian Labor Party (ALP)", "sinceFy": "2024-25", "limit": 5}),
    ("MoneyIn", {"entityName": "Climate 200 Pty Limited", "sinceFy": "2024-25", "limit": 5}),
    ("YearSummary", {"entityName": "MINERALOGY PTY LTD", "limit": 5}),
]


def post(path, body):
    req = urllib.request.Request(
        f"{BASE}{path}?username={USER}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Basic {AUTH}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}", "rows": [], "rowCount": 0}


def main():
    declared = {v["name"] for v in yaml.safe_load(VIEWS.read_text())}
    covered = {name for name, _ in CASES}
    missing = declared - covered
    if missing:
        print(f"✗ views with no test case (an untested view is an unshipped view): {sorted(missing)}")

    failures = len(missing)
    for name, args in CASES:
        res = post(f"/views/{name}/run", {"args": args})
        rows, warn, err = res.get("rows", []), res.get("warnings"), res.get("error")
        ok = rows and not err
        failures += 0 if ok else 1
        print(f"{'✓' if ok else '✗'} {name}  args={args}")
        print(f"    rows={res.get('rowCount')} ms={res.get('durationMs')} err={err}")
        # A 0-row result WITH a warning is a broken realm, not an empty source — always read these.
        if warn:
            print(f"    WARNINGS: {warn}")
        for row in rows[:3]:
            print("    ", {k: (str(v)[:44] if v is not None else None) for k, v in row.items()})

    print(f"\n{'FAILURES: ' + str(failures) if failures else 'ALL VIEWS RETURNED ROWS'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
