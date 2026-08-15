#!/usr/bin/env python3
"""Assemble a publishable donor report: the best paragraph per donor, each checked against its own sources.

WHY THIS IS A SCRIPT AND NOT A VIEW. `PartyFundingDossier` composes one paragraph per donor, which is
the unit that can be judged — and, when it fails, the unit that can be composed again without
discarding the paragraphs that passed. That retry loop is a PUBLISHING decision, not a query: how many
attempts are worth paying for, and what to do with a paragraph that never comes clean, are questions
about the report you are about to put someone's name in, not about the data.

WHAT IT CHECKS, per paragraph, against ONLY that donor's sources plus the register's own figures:
  · every word appears in a source, the register row, or the instruction the composer was given;
  · no inference language ("suggests", "reveals", "a strategy of").
A word appearing nowhere is how a fabrication reads: fluent, sourced-looking, and traceable to
nothing. Judging per donor is stricter than judging a whole report — a sentence about one company
cannot be "supported" by a page fetched for another.

WHAT IT DOES WITH WHAT IT CANNOT CLEAR. Nothing is edited and nothing is hidden: a paragraph still
flagged after the last attempt is returned WITH its flagged words, for the publisher to show beside
it or drop. Silently smoothing the word away is the failure this realm exists to prevent.

Usage:
    python3 scripts/publish-report.py 'Liberal Party of Australia' [sinceFy] [limit] [attempts]
Writes report.json next to it and prints a per-donor summary. Exit 1 if any paragraph is still
flagged, so a pipeline can refuse to publish without a human looking.
"""

import base64
import importlib.util
import json
import os
import pathlib
import sys
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
USER = os.environ.get("EMBABEL_USER", "rod")
PASS = os.environ.get("EMBABEL_PASS", "test")
PORT = os.environ.get("EMBABEL_PORT", "8046")
AUTH = base64.b64encode(f"{USER}:{PASS}".encode()).decode()

# The gates live in the test harness — one definition, so a report cannot be published under looser
# rules than the suite enforces.
spec = importlib.util.spec_from_file_location("tv", HERE / "test-views.py")
tv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tv)

FAMILY = sys.argv[1] if len(sys.argv) > 1 else "Liberal Party of Australia"
SINCE = sys.argv[2] if len(sys.argv) > 2 else "2024-25"
LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else 8
ATTEMPTS = int(sys.argv[4]) if len(sys.argv) > 4 else 3
ARGS = {"familyName": FAMILY, "sinceFy": SINCE, "limit": LIMIT}


def run(view):
    req = urllib.request.Request(
        f"http://localhost:{PORT}/api/v1/admin/kg/views/{view}/run?username={USER}",
        data=json.dumps({"args": ARGS}).encode(),
        headers={"Authorization": f"Basic {AUTH}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.load(r)


def register_facts():
    """The register's own figures per donor — what a paragraph may state without a web source."""
    facts = {}
    for s in run("PartyFundingSources").get("rows", []):
        facts.setdefault(s["donorAsLodged"], set()).update(
            str(s.get(f) or "") for f in (
                "donorAsLodged", "searchedAs", "disclosedTotal", "financialYear",
                "gifts", "branches", "branchesLodged",
            )
        )
    return facts


def judge(row, facts, instruction):
    """(inference-language problems, words in the paragraph that no source of THIS donor carries)."""
    corpus = " ".join(
        " ".join(str(s.get(f) or "") for f in ("kind", "title", "url", "text"))
        for s in (row["sources"] or [{}])
    )
    corpus += " " + " ".join(facts.get(row["donor"], ())) + " " + FAMILY + " " + instruction
    return (
        tv.prose_failures("PartyFundingDossier", [{"dossier": row["prose"]}]),
        tv.unsupported_words(row["prose"] or "", corpus),
    )


def main():
    facts = register_facts()
    instruction = tv.dossier_instruction()
    best = {}
    for attempt in range(1, ATTEMPTS + 1):
        for row in run("PartyFundingDossier").get("rows", []):
            problems, novel = judge(row, facts, instruction)
            score = (1 if problems else 0, len(novel))
            if row["donor"] not in best or score < best[row["donor"]]["score"]:
                best[row["donor"]] = {"score": score, "row": row, "flagged": novel,
                                      "problems": problems, "attempt": attempt}
        unclean = [d for d, v in best.items() if v["score"] != (0, 0)]
        print(f"attempt {attempt}: {len(best) - len(unclean)}/{len(best)} paragraphs clean"
              + (f" — still flagged: {[d[:24] for d in unclean]}" if unclean else ""))
        if not unclean:
            break

    rows = sorted((best[d]["row"] for d in best), key=lambda r: -r["disclosedTotal"])
    flags = {d: best[d]["flagged"] for d in best if best[d]["flagged"]}
    problems = {d: best[d]["problems"] for d in best if best[d]["problems"]}
    out = HERE.parent / "report.json"
    out.write_text(json.dumps({"family": FAMILY, "sinceFy": SINCE, "rows": rows,
                               "flagged": flags, "inferenceLanguage": problems}, indent=1))

    print(f"\n{len(rows)} donors, {sum(len(r['sources'] or []) for r in rows)} sources → {out}")
    for donor, words in flags.items():
        print(f"  ! {donor[:38]:40} words in no source: {words}")
    for donor, p in problems.items():
        print(f"  ✗ {donor[:38]:40} {p}")
    return 1 if flags or problems else 0


if __name__ == "__main__":
    sys.exit(main())
