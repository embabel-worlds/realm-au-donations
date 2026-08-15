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

BUT that reloads the YAML, NOT the producer RECORD CACHE. If you change the underlying data (or a
SQL view definition) and re-run the loader, a producer with a `ttl` cache keeps serving the old
records for that key until the TTL expires — which reads as "my fix did nothing" or, worse, as a
view that silently returns fewer rows than the database holds. While iterating, either drop the
`cache:` TTL on the producer you are changing or test with a key you have not queried before.
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
VIEW_FILES = ["donations.yml", "ownership.yml", "investigation.yml"]
VIEWS_DIR = pathlib.Path(__file__).resolve().parent.parent / "views"

# (view, args) — declared defaults fill the rest, exactly as a bare call would.
CASES = [
    ("DonorProfile", {"entityName": "MINERALOGY PTY LTD"}),
    ("DonationsMade", {"entityName": "MINERALOGY PTY LTD", "sinceFy": "2023-24", "limit": 5}),
    ("TopRecipients", {"entityName": "MINERALOGY PTY LTD", "sinceFy": "2023-24", "limit": 5}),
    ("DonationsReceived", {"entityName": "Australian Labor Party (ALP)", "sinceFy": "2024-25", "limit": 5}),
    ("MoneyIn", {"entityName": "Climate 200 Pty Limited", "sinceFy": "2024-25", "limit": 5}),
    ("YearSummary", {"entityName": "MINERALOGY PTY LTD", "limit": 5}),
    # CROSS-REALM (needs realm-diffbot + DIFFBOT_TOKEN). Limits are deliberately tiny: every
    # resolved donor is a knowledge-graph entity export, and the free tier is ~400 a MONTH.
    ("DonorOwnership", {"entityName": "Fox Group Holdings Pty Ltd"}),
    ("PartyBackers", {"partyName": "Australian Labor Party (ALP)", "sinceFy": "2023-24", "limit": 3}),
    # Rows are gifts, not donors, so this asserts the ROWS come back — most of them will have empty
    # company columns, and that is the correct answer rather than a failure (see below).
    ("LargestGiftsWithOwners", {"entityName": "Liberal Party of Australia", "sinceFy": "2023-24", "limit": 3}),
    # Needs realm-gov-au + BRAVE_API_KEY. Three searches per call; zero rows means the SEARCH did
    # not run (realm or key missing), not that the donor has no trail.
    ("DonorTrail", {"entityName": "ORYXIUM PTY LIMITED", "limit": 9}),
    # FAMILIES — store only, no credentials. If these return 0 rows the party_families mapping
    # was not loaded (regenerate with scripts/build-party-families.py, then re-run the loader).
    ("FamilyBackers", {"familyName": "Liberal Party of Australia", "sinceFy": "2024-25", "limit": 5}),
    ("BranchSpreading", {"familyName": "Australian Labor Party", "sinceFy": "2023-24", "minBranches": 5, "limit": 5}),
    ("SameDayGiving", {"entityName": "Minerals Council of Australia", "limit": 5}),
    # LLM-composed. Gated by prose_failures() below, not just by returning a row: the risk here is
    # a fluent paragraph that interprets, and only an assertion about the WORDS can catch that.
    ("FamilyBriefing", {"familyName": "Liberal Party of Australia", "sinceFy": "2024-25", "limit": 8}),
    # INVESTIGATIVE
    ("DisclosureGaps", {"entityName": "Liberal Party of Australia", "minGap": 50000, "limit": 5}),
    ("GroupGiving", {"partyName": "Liberal Party of Australia", "sinceFy": "2023-24", "limit": 3}),
]

# Declared but NOT proven to return rows — listed so the "every view needs a case" check stays
# meaningful without the suite going permanently red over drafts. Each entry says why.
UNVERIFIED = {
    "ShellCompanyCheck": "needs realm-gov-au; its ABR extract is ~1GB on first touch",
    "LobbyingFirmRoll": "needs a firm GUID; the register cannot be searched by client",
    "DonorBriefing": "aggregation returns no rows and no error — under investigation",
}


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


# ── Prose gate ────────────────────────────────────────────────────────────────────────────────
#
# Views that return LLM-composed prose need an assertion no row count can make: that the model
# described the records instead of interpreting them.
#
# This is not hypothetical. Asked to explain "the significance" of the same-day payment rows, the
# summarize aggregation wrote "a strategy of targeted influence" and "suggests coordination" from
# records containing nothing but dates, amounts and party names. The engine's own grounding
# verifier passed it — correctly, on its own terms: it checks whether FACTS are supported, and
# "suggests coordination" asserts no fact. A verifier catches an invented fact. Nothing catches
# invented significance, so it has to be caught here.
#
# The words below are the tells. Each one introduces a claim the register cannot support, and any
# of them in a briefing means the instruction stopped holding — which is a real regression, since
# the instruction is the only thing enforcing the description/interpretation line.
INFERENCE_TELLS = [
    "suggest", "indicat", "implies", "imply", "appears to", "seems to",
    "strategy", "strategic", "influence", "coordinat", "deliberate",
    "intent", "motiv", "in order to", "aims to", "seeks to",
    "reveals", "demonstrat", "proves", "highlight", "underscores",
    "raises questions", "critics", "controvers",
]

PROSE_VIEWS = {"FamilyBriefing"}


def prose_failures(name, rows):
    """Assert an LLM-composed row describes rather than interprets. Returns a list of problems."""
    if name not in PROSE_VIEWS:
        return []
    problems = []
    for row in rows:
        for field, value in row.items():
            if not isinstance(value, str) or len(value) < 40:
                continue
            lowered = value.lower()
            hits = sorted({t for t in INFERENCE_TELLS if t in lowered})
            if hits:
                problems.append(f"{field}: inference language {hits}")
            # A briefing with no figures in it has stopped reporting and started narrating.
            if not any(ch.isdigit() for ch in value):
                problems.append(f"{field}: no figures — a briefing without numbers is not grounded")
    return problems


# Fixtures that prove the gate FIRES. A gate only ever seen to pass is decoration, and this one
# guards the difference between reporting and inventing — so it is checked before it is trusted.
# The fabricated text is verbatim output from this same aggregation over these same rows, asked
# for "significance" instead of for the records.
GATE_FIXTURES = [
    ("fabricated (asked for significance)", True,
     "The payments from the Minerals Council of Australia to political families consistently occur "
     "on or near the same dates, indicating a pattern of regular, scheduled contributions. These "
     "payments often involve multiple political parties or families, highlighting a strategy of "
     "targeted influence across the political spectrum. The timing of these payments is "
     "significant, as it suggests coordination and may impact political decision-making."),
    ("grounded (asked for the records only)", False,
     "The records cover payments from the Minerals Council of Australia between October 10, 2024, "
     "and April 30, 2025, totaling 15 occasions. The parties that recur are the Liberal Party of "
     "Australia and Jacqui Lambie Network. The number of political families paid each occasion "
     "ranges from 2 to 4, with amounts ranging from $5,500 to $62,500."),
    ("narration carrying no figures", True,
     "The register shows a number of donors contributing to the party over the period, with "
     "contributions spread across various party entities and financial years."),
]


def check_the_gate():
    """Verify the prose gate on known-good and known-bad text. Returns the number of failures."""
    bad = 0
    for label, should_fire, text in GATE_FIXTURES:
        problems = prose_failures(next(iter(PROSE_VIEWS)), [{"briefing": text}])
        fired = bool(problems)
        if fired != should_fire:
            bad += 1
            print(f"✗ PROSE GATE IS WRONG — {label}: gate {'fired' if fired else 'stayed silent'}")
        else:
            print(f"✓ prose gate {'fires on' if fired else 'accepts'} {label}")
    return bad


def main():
    declared = {v["name"] for f in VIEW_FILES for v in yaml.safe_load((VIEWS_DIR / f).read_text())}
    covered = {name for name, _ in CASES} | UNVERIFIED.keys()
    missing = declared - covered
    for name, why in sorted(UNVERIFIED.items()):
        print(f"! {name}: UNVERIFIED — {why}")
    if missing:
        print(f"✗ views with no test case (an untested view is an unshipped view): {sorted(missing)}")

    failures = len(missing) + check_the_gate()
    for name, args in CASES:
        res = post(f"/views/{name}/run", {"args": args})
        rows, warn, err = res.get("rows", []), res.get("warnings"), res.get("error")
        prose = prose_failures(name, rows)
        ok = rows and not err and not prose
        failures += 0 if ok else 1
        print(f"{'✓' if ok else '✗'} {name}  args={args}")
        print(f"    rows={res.get('rowCount')} ms={res.get('durationMs')} err={err}")
        for problem in prose:
            print(f"    PROSE GATE FAILED — {problem}")
        # A 0-row result WITH a warning is a broken realm, not an empty source — always read these.
        if warn:
            print(f"    WARNINGS: {warn}")
        for row in rows[:3]:
            print("    ", {k: (str(v)[:44] if v is not None else None) for k, v in row.items()})

    print(f"\n{'FAILURES: ' + str(failures) if failures else 'ALL VIEWS RETURNED ROWS'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
