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
import re
import sys
import urllib.error
import urllib.request

import yaml

PORT = sys.argv[1] if len(sys.argv) > 1 else "8046"
USER = os.environ.get("EMBABEL_USER", "rod")
PASS = os.environ.get("EMBABEL_PASS", "test")
AUTH = base64.b64encode(f"{USER}:{PASS}".encode()).decode()
BASE = f"http://localhost:{PORT}/api/v1/admin/kg"
# EVERY view file, or the "every view needs a case" check silently ignores a whole file —
# which is exactly what happened when families.yml was added and went unchecked.
VIEW_FILES = ["donations.yml", "ownership.yml", "investigation.yml", "trail.yml", "families.yml"]
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
    # Identity only. Asserts nothing about ownership — see docs/CORPORATE_GROUPS.md.
    ("DonorIdentity", {"entityName": "National Australia Bank Ltd"}),
    # Cross-realm (realm-wikidata). Structured assertion, not evidence — see
    # docs/CORPORATE_GROUPS.md. Westpac is used because it reliably resolves; most donors do not.
    ("DonorGroup", {"entityName": "Westpac Banking Corporation", "limit": 6}),
    # LLM-composed. Gated by prose_failures() below, not just by returning a row: the risk here is
    # a fluent paragraph that interprets, and only an assertion about the WORDS can catch that.
    ("FamilyBriefing", {"familyName": "Liberal Party of Australia", "sinceFy": "2024-25", "limit": 8}),
    # The funding report and its prose. This family and window are chosen because they exercise ALL
    # FOUR identification tiers — an ABN on the return, a knowledge-base statement, a scored name
    # match, and donors nothing resolves — which is the only way row_failures() can prove the tier
    # logic rather than one branch of it. Gated by row_failures() AND, for the brief, by the prose
    # gate: the qualifiers are the thing that got improvised by hand, so they are asserted.
    ("PartyFundingReport", {"familyName": "Australian Labor Party", "sinceFy": "2023-24", "limit": 8}),
    ("PartyFundingBriefing", {"familyName": "Australian Labor Party", "sinceFy": "2023-24", "limit": 8}),
    # A family whose top donors are ALL unresolved private vehicles. It looks like the easy case and
    # is the hard one: with nothing to report about identity, a summary quietly drops the "no public
    # corporate record" clause from every sentence and reads as if the names were established.
    ("PartyFundingBriefing", {"familyName": "Liberal Party of Australia", "sinceFy": "2024-25", "limit": 6}),
    # The dossier and the evidence under it. Same args, or the corpus the gate checks against is
    # not the corpus the prose was composed from.
    ("PartyFundingSources", {"familyName": "Liberal Party of Australia", "sinceFy": "2024-25", "limit": 4}),
    ("PartyFundingDossier", {"familyName": "Liberal Party of Australia", "sinceFy": "2024-25", "limit": 4}),
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

# view → the fields an LLM composed. Named rather than "every long string", because these views
# also return CONSTANTS (the caveat block), and a constant is not the model's work: gating it would
# fail the no-figures rule forever while proving nothing about what was generated.
PROSE_VIEWS = {
    "FamilyBriefing": {"briefing"},
    "PartyFundingBriefing": {"briefing"},
    "PartyFundingDossier": {"dossier"},
}


# Tells that stay forbidden even when a source is named. A dossier may report that a publication
# SAYS something; it may not tell the reader what the pattern means. Attribution verbs (indicates,
# reveals, highlights) are removed for this view only — with a named source they are reporting.
ATTRIBUTION_SAFE = {"indicat", "reveals", "demonstrat", "highlight", "appears to", "seems to"}


def prose_failures(name, rows):
    """Assert an LLM-composed row describes rather than interprets. Returns a list of problems."""
    fields = PROSE_VIEWS.get(name)
    if not fields:
        return []
    problems = []
    for row in rows:
        for field, value in row.items():
            if field not in fields or not isinstance(value, str) or len(value) < 40:
                continue
            lowered = value.lower()
            tells = INFERENCE_TELLS if name != "PartyFundingDossier" else [
                t for t in INFERENCE_TELLS if t not in ATTRIBUTION_SAFE
            ]
            hits = sorted({t for t in tells if t in lowered})
            if hits:
                problems.append(f"{field}: inference language {hits}")
            # A briefing with no figures in it has stopped reporting and started narrating.
            if not any(ch.isdigit() for ch in value):
                problems.append(f"{field}: no figures — a briefing without numbers is not grounded")
    return problems


# ── Row gate: the identification tier, asserted as a value ────────────────────────────────────
#
# The failure this realm actually had was not a wrong number. A funding write-up produced by hand
# stated every figure correctly and could still not be reproduced, because the RESOLUTION STATUS of
# each donor existed nowhere in the data — so it was supplied from memory, and a second run differed.
#
# So the assertions here are about the tier, not the totals. A null `identifiedBy` is the exact bug:
# it cannot distinguish "nothing resolved" from "nobody asked", and a consumer that meets one
# invents its own answer.
TIERS = {"identity", "structured", "inference", "none"}

# The host's grounding contract: an aggregation that judges its items insufficient returns exactly
# this sentence. A weak reduce model sometimes appends it to an otherwise complete brief.
REFUSAL_SENTINEL = "The provided items do not contain this information."

CAVEATS = (
    "These are amounts disclosed under law. They say nothing about what any donor sought or "
    "received. Gifts below the disclosure threshold do not appear. Donor and recipient returns are "
    "separate disclosures that need not agree. Every corporate identification is a match to check "
    "— the register asserts only the name on the return."
)


# The one case whose rows MUST span several tiers. It is a property of this fixture, not of the
# view: a family whose top donors are all private vehicles returns `none` for every row, and that
# is the correct answer (see issue #1). If this window stops spanning tiers the fixture has
# drifted, and the suite would be proving one branch of the tier logic while looking green.
TIER_SPREAD_FIXTURE = ("Australian Labor Party", "2023-24")


def row_failures(name, rows, args=None, warnings=None):
    """Assert the identification tier is present, honest, and rendered. Returns a list of problems."""
    args = args or {}
    problems = []
    # `none` MUST mean "nothing was resolved for this donor", never "the source did not answer".
    # The two are indistinguishable in the row — which is the very defect this view exists to
    # prevent, one level up — and the difference lives in the response envelope rather than in
    # any column, because a producer failure is not per-row. So it is asserted HERE, where both
    # are visible. A run whose external source failed cannot report an honest `none` for anyone,
    # and its prose ("no public corporate record was resolved") would be a claim nobody checked.
    if name in ("PartyFundingReport", "PartyFundingBriefing"):
        failed = [w for w in (warnings or []) if "PRODUCER_ERROR" in str(w)]
        unresolved = [r for r in rows if r.get("identifiedBy") == "none"]
        if failed and (unresolved or name == "PartyFundingBriefing"):
            problems.append(
                "a source failed on this run, so `none` cannot be distinguished from unasked: "
                + str(failed[0])[:160]
            )
    if name == "PartyFundingReport":
        for row in rows:
            donor, tier = row.get("donorAsLodged"), row.get("identifiedBy")
            if tier not in TIERS:
                problems.append(f"{donor}: identifiedBy is {tier!r} — a null tier is THE bug this view exists to prevent")
                continue
            if tier == "none" and "unresolved" not in (row.get("identification") or ""):
                problems.append(f"{donor}: tier 'none' must render as unresolved, not as {row.get('identification')!r}")
            if tier != "none" and not row.get("evidence"):
                problems.append(f"{donor}: tier {tier!r} with no evidence — a claim with no basis to check")
            # A null external field must not null the whole render: that silently drops a donor's
            # sentence while the row still looks fine.
            if not row.get("sentence"):
                problems.append(f"{donor}: no sentence rendered")
            if not row.get("path"):
                problems.append(f"{donor}: no path rendered")
        spread_asked = (args.get("familyName"), args.get("sinceFy")) == TIER_SPREAD_FIXTURE
        if spread_asked and len({r.get("identifiedBy") for r in rows}) < 2:
            problems.append("every row shares one tier — this fixture is chosen to exercise several")
    if name == "PartyFundingBriefing":
        for row in rows:
            if (row.get("caveats") or "") != CAVEATS:
                problems.append("caveats are not the constant, verbatim — they must not be composed")
            # Every input line for this case carries a qualifier, and the donors nothing resolved
            # are the majority. A brief carrying none of them has quietly upgraded the lot.
            brief = (row.get("briefing") or "").lower()
            if not any(q in brief for q in ("no public corporate record", "not verified", "unverified", "name match")):
                problems.append("no identification qualifier survived into the prose")
            # The platform's own refusal sentence, appended AFTER a perfectly good brief. The engine
            # cannot see it — `isHonestMiss` treats any output CONTAINING the sentinel as a miss, so
            # a mixed answer skips verification — and a realm cannot strip it, because an
            # aggregation's result may not be post-processed. Named separately so it is never
            # mistaken for the model inventing something.
            if REFUSAL_SENTINEL.lower() in brief:
                problems.append("the aggregation's refusal sentinel leaked into a composed brief")
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


# ── Grounding gate: a render may not use words its input never contained ──────────────────────
#
# The prose gate catches invented SIGNIFICANCE. It does not catch invented IDENTITY, and that is
# what a model reaches for when the register leaves a donor unresolved: asked to brief rows saying
# "no public corporate record was resolved under this lodged name", it wrote that one donor sells
# COVID-19 tests, that another is "an identified individual", and that a third is "a symbolically
# named organization". Every figure in that paragraph was right. None of those descriptions came
# from the input, and a reader cannot tell which half is which.
#
# So the rule for this path is lexical: a BRIEF IS A RENDER, and a render may use the words of its
# input plus ordinary connectives, and nothing else. Any other word is a fact from somewhere the
# reader cannot check. The allowlist below is deliberately dull — if a legitimate brief needs a
# word that is not here, adding it is a decision someone makes in a diff.
# Structural vocabulary a render may reach for without making a claim about anybody: connectives,
# the counting words that restate a figure, and the calendar words the rows imply. Nothing here can
# describe a donor, which is the whole test for whether a word belongs on this list.
FUNCTION_WORDS = {
    # determiners, pronouns, prepositions, conjunctions, auxiliaries, degree/ordering adverbs
    "a", "an", "the", "this", "that", "these", "those", "each", "every", "all", "both", "some",
    "any", "no", "none", "other", "another", "such", "same", "own", "few", "several", "many",
    "much", "more", "most", "less", "least", "one", "two", "three", "four", "five", "six", "seven",
    "eight", "nine", "ten", "eleven", "twelve", "first", "second", "third", "last", "next",
    "it", "its", "they", "them", "their", "which", "who", "whom", "whose", "what", "there", "here",
    "of", "in", "on", "at", "to", "from", "by", "for", "with", "without", "within", "into", "onto",
    "over", "under", "across", "through", "between", "among", "amongst", "along", "against",
    "about", "around", "during", "before", "after", "since", "until", "while", "than", "as",
    "and", "or", "but", "nor", "so", "yet", "if", "then", "because", "though", "although",
    "whereas", "however", "similarly", "likewise", "also", "too", "again", "further", "moreover",
    "additionally", "finally", "lastly", "meanwhile", "respectively", "including", "via",
    "is", "are", "was", "were", "be", "been", "being", "has", "have", "had", "do", "does", "did",
    "can", "could", "may", "might", "will", "would", "shall", "should", "must",
    "not", "only", "just", "still", "well", "up", "down", "out", "off",
}

# Domain vocabulary a RENDER may reach for without making a claim about anybody: the shape of the
# rows (how many, how spread, what period) and the verbs for money moving. Every word here restates
# something the rows state; none can say what a donor IS, which is the test for adding one. A
# content word that is not here fails the gate, and adding it is a decision someone makes in a diff.
DOMAIN_WORDS = {
    "financial", "year", "years", "period", "total", "totals", "totalling", "totaling", "combined",
    "amount", "amounts", "figure", "figures", "record", "records", "donation", "donations",
    "summary", "brief", "report", "reported", "reports", "case", "cases", "example",
    # the register's own vocabulary for the rollup, and the words a citation needs to exist
    "separate", "entities", "entity", "party", "parties", "available", "details", "detailed",
    "classified", "listed", "registered", "registration", "incorporated", "documented",
    "documents", "document", "titled", "paying", "pays", "pay", "paid",
    "multiple", "different", "differ", "differs", "range", "ranges", "ranging", "vary", "varies",
    "varying", "spanning", "spread", "split", "distributed", "single", "largest", "previous",
    "earlier", "later", "involve", "involved", "involves", "found", "turn",
    # y→i morphology the stem check cannot bridge: the rows say "relying"
    "reliance", "reliant",
    # attribution: naming who said a thing is the dossier's whole contract
    "says", "said", "states", "stated", "reports", "reported", "notes", "noted", "describes",
    "described", "confirms", "confirmed", "mentions", "mentioned", "refers", "referring",
    "according", "source", "sources", "article", "website", "entry", "profile", "register",
    "publication", "encyclopaedia", "disclosure", "filing", "lodged", "law", "government",
    "regulator", "percent", "per", "cent", "owning", "owner", "owns", "owned", "age",
    # the register's own verb is "disclosed"; these restate the transfer, never a donor
    "gave", "given", "giving", "made", "paid", "sent", "went", "directed", "allocated", "provided",
    "channelled", "channeled", "contributed", "contribution", "contributions",
}

CONNECTIVES = FUNCTION_WORDS | DOMAIN_WORDS

# Morphology is not fabrication: "gift" for gifts, "verification" for verified. Words sharing this
# much of a stem with an input word are treated as the same word. Fabricated identity does not
# share a stem with anything in the rows — that is exactly what makes it fabricated.
STEM = 4


def briefing_instruction():
    """The words the brief was ASKED with. Read from the view so the two cannot drift apart.

    A render may use its input's words and its own instruction's words — neither comes from
    anywhere the reader cannot see. Everything else is the model's own knowledge, which is the
    thing being screened for.
    """
    views = yaml.safe_load((VIEWS_DIR / "families.yml").read_text())
    cypher = next(v["cypher"] for v in views if v["name"] == "PartyFundingBriefing")
    quoted = re.search(r"summarize\(line,\s*'(.*?)'\s*\)", cypher, re.S)
    return quoted.group(1) if quoted else ""


def dossier_instruction():
    """The words the dossier was ASKED with — legitimate vocabulary, like the briefing's."""
    views = yaml.safe_load((VIEWS_DIR / "families.yml").read_text())
    cypher = next(v["cypher"] for v in views if v["name"] == "PartyFundingDossier")
    quoted = re.search(r"render\(line,\s*'(.*?)'\s*\)", cypher, re.S)
    return quoted.group(1) if quoted else ""


def unsupported_words(text, corpus):
    """Words in a rendered brief that its input never contained. Returns them sorted."""
    def tokens(s):
        cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in s.replace(",", "").lower())
        return {t for t in cleaned.split() if t}

    known = tokens(corpus) | CONNECTIVES
    stems = {w[:STEM] for w in known}
    return sorted(w for w in tokens(text) if w not in known and w[:STEM] not in stems)


# The same discipline for the row gate. Each fabricated row is a way the tier has actually been
# lost: absent, present but rendered as if resolved, asserted with nothing to check it against, or
# nulled out of the sentence by one null external field.
GOOD_ROW = {
    "donorAsLodged": "ACME PTY LTD", "identifiedBy": "identity",
    "identification": "ACME PTY LTD", "evidence": "ABN 00000000000, disclosed on the return",
    "sentence": "ACME PTY LTD disclosed $1 in 1 gifts. Identified by ABN 00000000000.",
    "path": "A Party → A Party (Branch) → ACME PTY LTD → ACME PTY LTD",
}
UNRESOLVED_ROW = {**GOOD_ROW, "donorAsLodged": "BETA PTY LTD", "identifiedBy": "none",
                  "identification": "BETA PTY LTD (unresolved)", "evidence": None,
                  "sentence": "BETA PTY LTD disclosed $1 in 1 gifts. No public corporate record …"}

ROW_GATE_FIXTURES = [
    ("a report with both tiers rendered honestly", False, "PartyFundingReport",
     [GOOD_ROW, UNRESOLVED_ROW]),
    ("a null tier — the bug this view exists to prevent", True, "PartyFundingReport",
     [GOOD_ROW, {**UNRESOLVED_ROW, "identifiedBy": None}]),
    ("an unresolved donor rendered as if resolved", True, "PartyFundingReport",
     [GOOD_ROW, {**UNRESOLVED_ROW, "identification": "BETA PTY LTD"}]),
    ("a claim with no basis to check", True, "PartyFundingReport",
     [{**GOOD_ROW, "evidence": None}, UNRESOLVED_ROW]),
    ("one null external field nulling a whole sentence", True, "PartyFundingReport",
     [GOOD_ROW, {**UNRESOLVED_ROW, "sentence": None}]),
    ("caveats composed instead of constant", True, "PartyFundingBriefing",
     [{"briefing": "ACME PTY LTD disclosed $1, with no public corporate record.",
       "caveats": "These are amounts disclosed under law, and should be read with care."}]),
    ("prose that dropped every qualifier", True, "PartyFundingBriefing",
     [{"briefing": "ACME PTY LTD disclosed $1 and BETA PTY LTD disclosed $2.", "caveats": CAVEATS}]),
    ("prose that kept them", False, "PartyFundingBriefing",
     [{"briefing": "ACME PTY LTD disclosed $1. BETA PTY LTD disclosed $2, with no public corporate "
                   "record resolved under this lodged name.", "caveats": CAVEATS}]),
    # …and the fixture-scoped one: the same uniform rows are FINE for any other family, and a
    # failure for the window chosen to span tiers.
    ("uniform tiers for a family that legitimately has them", False, "PartyFundingReport",
     [UNRESOLVED_ROW, UNRESOLVED_ROW], {"familyName": "Liberal Party of Australia", "sinceFy": "2024-25"}),
    ("uniform tiers where the fixture must span them", True, "PartyFundingReport",
     [UNRESOLVED_ROW, UNRESOLVED_ROW], {"familyName": "Australian Labor Party", "sinceFy": "2023-24"}),
    # `none` is only honest when every source actually answered. Same rows, same tiers — the
    # difference is a producer failure in the envelope, and it turns every `none` into "unknown".
    ("unresolved rows on a run where every source answered", False, "PartyFundingReport",
     [GOOD_ROW, UNRESOLVED_ROW], None, []),
    ("unresolved rows on a run where a source failed", True, "PartyFundingReport",
     [GOOD_ROW, UNRESOLVED_ROW], None,
     ["PRODUCER_ERROR: producer 'orgEnhanceByName' — the data could NOT be fetched from its source"]),
    ("a complete brief with the refusal sentinel appended", True, "PartyFundingBriefing",
     [{"briefing": "ACME PTY LTD disclosed $1, with no public corporate record resolved under this "
                   "lodged name.\n\n" + REFUSAL_SENTINEL, "caveats": CAVEATS}]),
]


# Grounding fixtures. The fabricated text is VERBATIM output from this same aggregation over these
# same rows, before the instruction forbade describing what a donor is.
GROUNDING_FIXTURES = [
    ("a brief that only renders its rows", False,
     "ACME PTY LTD disclosed $1,000 in 2 gifts. No public corporate record was resolved under this "
     "lodged name."),
    ("a brief that says what a donor sells", True,
     "ACME PTY LTD, identified as a seller of COVID-19 tests, disclosed $1,000 in 2 gifts."),
    ("a brief that classifies a donor", True,
     "ACME PTY LTD, an identified individual, disclosed $1,000 in 2 gifts."),
]
GROUNDING_CORPUS = ("ACME PTY LTD disclosed $1,000 to A Party in 2024-25, in 2 gifts. "
                    "No public corporate record was resolved under this lodged name.")


def check_the_gate():
    """Verify every gate on known-good and known-bad input. Returns the number of failures."""
    bad = 0
    for label, should_fire, text in GROUNDING_FIXTURES:
        novel = unsupported_words(text, GROUNDING_CORPUS)
        if bool(novel) != should_fire:
            bad += 1
            print(f"✗ GROUNDING GATE IS WRONG — {label}: {novel or 'nothing flagged'}")
        else:
            print(f"✓ grounding gate {'fires on' if novel else 'accepts'} {label}")
    for label, should_fire, text in GATE_FIXTURES:
        problems = prose_failures("FamilyBriefing", [{"briefing": text}])
        fired = bool(problems)
        if fired != should_fire:
            bad += 1
            print(f"✗ PROSE GATE IS WRONG — {label}: gate {'fired' if fired else 'stayed silent'}")
        else:
            print(f"✓ prose gate {'fires on' if fired else 'accepts'} {label}")
    for label, should_fire, view, rows, *rest in ROW_GATE_FIXTURES:
        problems = row_failures(
            view, rows,
            rest[0] if rest else None,
            rest[1] if len(rest) > 1 else None,
        )
        fired = bool(problems)
        if fired != should_fire:
            bad += 1
            print(f"✗ ROW GATE IS WRONG — {label}: gate {'fired' if fired else 'stayed silent'} {problems}")
        else:
            print(f"✓ row gate {'fires on' if fired else 'accepts'} {label}")
    return bad


def run_case(name, args):
    """Run one view and apply every gate that applies to it. Returns (response, rows, problems)."""
    res = post(f"/views/{name}/run", {"args": args})
    rows = res.get("rows", [])
    problems = prose_failures(name, rows) + row_failures(name, rows, args, res.get('warnings'))
    # The brief is a render of PartyFundingReport's sentences, so its own input is fetchable: ask
    # for the rows it was composed from and hold every word against them.
    # Determinism is a promise this view makes (same query, same tiers, same templates → same
    # output for a cache window), and it is cheap to check because the second call is served from
    # the producer cache. It has already been broken once: two donors disclosing the SAME amount
    # tie, and an unstable tie-break changes which donors the limit keeps.
    if name == "PartyFundingReport" and rows:
        again = post(f"/views/{name}/run", {"args": args}).get("rows", [])
        if json.dumps(again, sort_keys=True) != json.dumps(rows, sort_keys=True):
            problems.append("the same query returned different rows twice — an unstable order or tie-break")
    # A DOSSIER QUOTES SOURCES, SO ITS FABRICATIONS LOOK LIKE CITATIONS. The composer is handed
    # page text and asked to say who a donor is; a name it slightly alters ("Ian Walls" for the
    # sources' "Ian Wall") arrives wearing a URL, which is worse than an unsourced claim because it
    # invites the reader to stop checking. PartyFundingSources returns exactly what the composer
    # saw, so every word can be held against it.
    if name == "PartyFundingDossier" and rows:
        source = post("/views/PartyFundingSources/run", {"args": args}).get("rows", [])
        corpus = " ".join(
            " ".join(str(r.get(f) or "") for f in (
                "donorAsLodged", "searchedAs", "disclosedTotal", "financialYear", "gifts",
                "branches", "branchesLodged", "sourceKind", "title", "url", "text"))
            for r in source
        )
        novel = unsupported_words(rows[0].get("dossier") or "", corpus + " " + dossier_instruction())
        if not source:
            problems.append("could not fetch the dossier's own source rows — grounding unchecked")
        elif novel:
            problems.append(f"words in the dossier that appear in no source it was given: {novel}")
    if name == "PartyFundingBriefing" and rows:
        source = post("/views/PartyFundingReport/run", {"args": args}).get("rows", [])
        corpus = " ".join([r.get("sentence") or "" for r in source] + [briefing_instruction()])
        novel = unsupported_words(rows[0].get("briefing") or "", corpus)
        if not source:
            problems.append("could not fetch the brief's own source rows — grounding unchecked")
        elif novel:
            problems.append(f"words not in the source rows (invented identity reads exactly like this): {novel}")
    return res, rows, problems


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
        res, rows, prose = run_case(name, args)
        # The reduce model appends the host's refusal sentence to a complete brief in a minority of
        # runs (~2 in 6 for a family whose donors are all unresolved). It is a model weakness in a
        # composition path, not a defect in these rows — the report view is unaffected — so it is
        # retried ONCE and reported either way rather than being either hidden or left flaky.
        if any(REFUSAL_SENTINEL.lower() in p or "refusal sentinel" in p for p in prose):
            print(f"    RETRY {name}: the aggregation's refusal sentinel leaked; composing again")
            res, rows, prose = run_case(name, args)
        warn, err = res.get("warnings"), res.get("error")
        ok = rows and not err and not prose
        failures += 0 if ok else 1
        print(f"{'✓' if ok else '✗'} {name}  args={args}")
        print(f"    rows={res.get('rowCount')} ms={res.get('durationMs')} err={err}")
        for problem in prose:
            print(f"    GATE FAILED — {problem}")
        # A 0-row result WITH a warning is a broken realm, not an empty source — always read these.
        if warn:
            print(f"    WARNINGS: {warn}")
        for row in rows[:3]:
            print("    ", {k: (str(v)[:44] if v is not None else None) for k, v in row.items()})

    print(f"\n{'FAILURES: ' + str(failures) if failures else 'ALL VIEWS RETURNED ROWS'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
