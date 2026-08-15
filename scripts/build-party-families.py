#!/usr/bin/env python3
"""Generate sql/party_families.sql — the lodged-name → political-family mapping.

WHY THIS IS A SCRIPT THAT EMITS DATA, NOT A RULE THAT RUNS AT QUERY TIME.

The register's recipient names are LEGAL LODGERS, not organisations: 2,073 distinct names for a
few dozen political interests. 'Climate 200 Pty Ltd' and 'Climate 200 Pty Limited' are the same
organisation and hold $19.75m and $10.35m respectively; 'Liberal Party (W.A. Division) Inc' and
'... Inc.' differ by a full stop and $6.25m. Reading either name alone understates the truth.

So a rollup is needed. But a rollup is an EDITORIAL ACT — it produces a number that goes in print —
and three things follow:

  1. It must be DETERMINISTIC. A string rule evaluated inside every query, or an LLM asked to
     classify names on the fly, gives different totals on different days and cannot answer "why
     did this figure change".
  2. It must be AUDITABLE. The committed artifact is explicit `lodged_name → family` rows. A
     journalist can read the diff and challenge any single line. That is impossible with a regex
     buried in a view.
  3. It must NOT GUESS. A name this script cannot confidently place is LEFT OUT of the table.
     Unmapped names still appear in every query with a NULL family — never silently dropped,
     because dropping them shrinks totals invisibly.

Run it when the register gains new lodgers; review the diff before committing.

    python3 scripts/build-party-families.py            # writes sql/party_families.sql
    python3 scripts/build-party-families.py --report   # also print what it could NOT place
"""

import os
import re
import subprocess
import sys

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sql", "party_families.sql")
CONTAINER = os.environ.get("AU_DONATIONS_CONTAINER", "au-donations-pg")
DB_USER = os.environ.get("AU_DONATIONS_ADMIN", "admin")
DB_NAME = os.environ.get("AU_DONATIONS_DB", "au_donations")

# Each family is (family_label, list of exact-lowercase name markers that IDENTIFY it).
#
# Matching is on a normalized name (lowercase, punctuation stripped, whitespace collapsed) and a
# marker must appear as a WHOLE-WORD SEQUENCE. Deliberately conservative: these are the phrases
# that name the party itself, not words that merely co-occur with it. 'labour' is NOT a marker for
# Labor — the Democratic Labour Party is a different party and must not be folded in.
FAMILIES = [
    # 'Secretariate' is the register's own misspelling, and it holds $1,000,000. Exactly the kind
    # of thing a rule would miss forever and an explicit row makes visible.
    ("Australian Labor Party", [
        "australian labor party", "labor party of australia",
        "alp national secretariat", "alp national secretariate",
    ]),
    ("Liberal Party of Australia", [
        "liberal party of australia", "liberal party wa division",
        "liberal party w a division", "liberal party of western australia",
    ]),
    ("Liberal National Party of Queensland", ["liberal national party of queensland"]),
    ("National Party of Australia", ["national party of australia", "the nationals"]),
    # Bare 'greens' catches the state branches ('Queensland Greens', $2.23m) which name no
    # national body. Safe because no other registered family uses the word.
    ("Australian Greens", ["australian greens", "the australian greens", "the greens", "greens"]),
    ("United Australia Party", ["united australia party", "clive palmers united australia party"]),
    ("Palmer United Party", ["palmer united party"]),
    ("Trumpet of Patriots", ["trumpet of patriots"]),
    ("Climate 200", ["climate 200"]),
    ("Advance Australia", ["advance australia"]),
    ("Pauline Hanson's One Nation", ["pauline hansons one nation", "one nation"]),
    ("Australian Democrats", ["australian democrats"]),
    ("Katter's Australian Party", ["katters australian party"]),
    ("Centre Alliance", ["centre alliance", "nick xenophon team"]),
    ("Shooters, Fishers and Farmers", ["shooters fishers and farmers", "shooters and fishers"]),
    ("Animal Justice Party", ["animal justice party"]),
    ("Legalise Cannabis", ["legalise cannabis"]),
    ("Jacqui Lambie Network", ["jacqui lambie network"]),
    ("Democratic Labour Party", ["democratic labour party", "democratic labor party"]),
    ("Family First", ["family first"]),
    ("Christian Democratic Party", ["christian democratic party"]),
    ("Australian Christians", ["australian christians"]),
    ("Liberal Democratic Party", ["liberal democratic party", "liberal democrats"]),
    ("Sustainable Australia", ["sustainable australia"]),
    ("Socialist Alliance", ["socialist alliance"]),
    ("Victorian Socialists", ["victorian socialists"]),
    ("Reason Party", ["reason party", "australian sex party"]),
    ("Health Australia Party", ["health australia party"]),
    # A distinct NT party, NOT a Liberal or National branch — kept as its own family so a
    # Coalition total cannot quietly absorb it.
    ("Country Liberal Party (NT)", ["country liberals", "country liberal party"]),
    ("Citizens Electoral Council", ["citizens electoral council", "cec", "cec fed"]),
    ("Western Australia Party", ["western australia party"]),
    ("Advance Australia", ["advance aus limited"]),
]

# Order matters: the most specific label must win. 'Liberal National Party of Queensland' contains
# neither 'liberal party of australia' nor 'national party of australia' after normalization, but
# 'Democratic Labour Party' would otherwise be caught by nothing and 'one nation' must not steal
# 'National Party of Australia' — hence whole-word-sequence matching plus this explicit ordering.
FAMILIES.sort(key=lambda f: -max(len(m) for m in f[1]))

# The AEC's own shorthand, which appears as a recipient name in its own right — 'ALP-FED' holds
# $3.05m and 'LIB-NSW' $1.97m. Matched only against the WHOLE normalized name, never as a
# substring: 'LIB' as a fragment would swallow half the register.
#
# The register uses SEVERAL abbreviations for the same family (LIB-, LP-, LPA-, LIB-FEC for the
# Liberals; NAT-, NP-, NATS- for the Nationals) and puts the jurisdiction on either side
# ('WA-ALP' as readily as 'ALP-NSW'). Both orders are generated, so a new spelling shows up in the
# unmapped report rather than being silently absorbed by a pattern.
EXACT_CODES = {}
_JURISDICTIONS = ["fed", "nat", "fec", "nsw", "vic", "qld", "wa", "sa", "tas", "nt", "act"]
for _codes, _family in [
    (["alp"], "Australian Labor Party"),
    (["lib", "lp", "lpa"], "Liberal Party of Australia"),
    (["nat", "nats", "np"], "National Party of Australia"),
    (["grn", "grns"], "Australian Greens"),
    (["lnp"], "Liberal National Party of Queensland"),
    (["clp"], "Country Liberal Party (NT)"),
    (["kap"], "Katter's Australian Party"),
    (["jln"], "Jacqui Lambie Network"),
    (["dlp"], "Democratic Labour Party"),
    (["on"], "Pauline Hanson's One Nation"),
    (["cec"], "Citizens Electoral Council"),
]:
    for _code in _codes:
        EXACT_CODES[_code] = _family
        for _j in _JURISDICTIONS:
            EXACT_CODES[f"{_code} {_j}"] = _family
            EXACT_CODES[f"{_j} {_code}"] = _family

STATE_MARKERS = {
    "nsw": "NSW", "n s w": "NSW", "new south wales": "NSW",
    "vic": "VIC", "victorian": "VIC", "victoria": "VIC",
    "qld": "QLD", "queensland": "QLD",
    "wa": "WA", "w a": "WA", "western australian": "WA", "western australia": "WA",
    "sa": "SA", "s a": "SA", "south australian": "SA", "south australia": "SA",
    "tas": "TAS", "tasmanian": "TAS", "tasmania": "TAS",
    "nt": "NT", "northern territory": "NT",
    "act": "ACT", "australian capital territory": "ACT",
}
FEDERAL_MARKERS = ["national", "federal", "federal secretariat", "national secretariat"]


def normalize(name: str) -> str:
    # Apostrophes are DELETED, not spaced: "Katter's Australian Party" must normalize to
    # "katters australian party" and not "katter s australian party", which matches nothing.
    # Same for "Pauline Hanson's One Nation". This cost both families a mapping on the first run.
    n = name.lower().replace("'", "").replace("’", "")
    n = re.sub(r"[^a-z0-9]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def family_of(name: str):
    n = normalize(name)
    if n in EXACT_CODES:
        return EXACT_CODES[n]
    for family, markers in FAMILIES:
        for m in markers:
            if re.search(rf"(?:^| ){re.escape(m)}(?:$| )", n):
                return family
    return None


def tier_of(name: str):
    """federal / state-or-territory / unspecified — from the lodger's own name only.

    A branch that names no jurisdiction gets 'unspecified' rather than a guess. The tier exists so
    a reader can see that a family total spans separate legal entities; it is never used to decide
    whether a row counts.
    """
    n = normalize(name)
    for m in FEDERAL_MARKERS:
        if re.search(rf"(?:^| ){re.escape(m)}(?:$| )", n):
            return "federal"
    for marker, code in STATE_MARKERS.items():
        if re.search(rf"(?:^| ){re.escape(marker)}(?:$| )", n):
            return code
    return "unspecified"


def recipient_names():
    sql = (
        "SELECT r.name, coalesce(sum(d.value),0)::bigint "
        "FROM donations_made d JOIN entities r ON r.id = d.recipient_id "
        "GROUP BY r.name ORDER BY 2 DESC;"
    )
    out = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME, "-At", "-F", "\t", "-c", sql],
        capture_output=True, text=True, check=True,
    ).stdout
    rows = []
    for line in out.splitlines():
        if "\t" in line:
            name, total = line.rsplit("\t", 1)
            rows.append((name, int(total)))
    return rows


def sql_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def main() -> int:
    rows = recipient_names()
    mapped, unmapped = [], []
    for name, total in rows:
        fam = family_of(name)
        (mapped if fam else unmapped).append((name, fam, tier_of(name), total))

    total_all = sum(t for _, t in rows) or 1
    covered = sum(r[3] for r in mapped)

    with open(OUT, "w") as f:
        f.write(
            "-- GENERATED by scripts/build-party-families.py — review the diff, then commit.\n"
            "--\n"
            "-- Maps a LODGED RECIPIENT NAME to the political family behind it. The register's names are\n"
            "-- legal lodgers, not organisations: this file is what lets a question about a party be asked\n"
            "-- without reading 2,000 name variants by hand.\n"
            "--\n"
            "-- IT NEVER REPLACES THE LODGED NAME. Every view built on it returns recipient_as_lodged\n"
            "-- beside the family, because the family is OUR interpretation and the lodged name is what\n"
            "-- the entity actually disclosed. A name absent from this table is UNMAPPED, not excluded —\n"
            "-- queries left-join, so its money still appears with a NULL family.\n"
            "--\n"
            "-- Federal and state divisions are SEPARATE LEGAL ENTITIES with separate obligations. Summing\n"
            "-- them is a defensible analytical choice, not a fact — which is why `tier` is carried through\n"
            "-- and every rollup can show its breakdown.\n"
            f"--\n-- Coverage at generation: {len(mapped)} of {len(rows)} distinct recipient names, "
            f"{100.0 * covered / total_all:.1f}% of all disclosed money.\n\n"
            "-- The TABLE and the family_backers VIEW live in schema.sql; this file is DATA ONLY,\n"
            "-- so regenerating it never changes the shape of anything.\n\n"
            "TRUNCATE party_families;\n\n"
            "INSERT INTO party_families (lodged_name, family, tier) VALUES\n"
        )
        f.write(",\n".join(
            f"    ({sql_literal(n)}, {sql_literal(fam)}, {sql_literal(tier)})"
            for n, fam, tier, _ in mapped
        ))
        f.write(";\n")

    print(f"wrote {OUT}")
    print(f"mapped   {len(mapped):5d} names  ({100.0 * covered / total_all:.1f}% of money)")
    print(f"unmapped {len(unmapped):5d} names  ({100.0 * (total_all - covered) / total_all:.1f}% of money)")

    if "--report" in sys.argv:
        print("\nLargest UNMAPPED recipients — each is either a genuine minor party/independent")
        print("(correctly absent) or a family this script should learn. Judge them by hand:")
        for name, _, tier, total in unmapped[:30]:
            print(f"  ${total:>12,}  [{tier:11}] {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
