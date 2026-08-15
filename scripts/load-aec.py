#!/usr/bin/env python3
"""Provision the realm's backing store from the AEC Transparency Register bulk export.

Downloads AllAnnualData.zip (or uses --zip), normalizes the free-text disclosure names into
ONE entities table (the raw export links records only by (Financial Year, Name), with
leading-whitespace and spacing variants), and loads a fully FK'd relational schema — which
is exactly what the schema miner then turns into a graph.

Deliberately stdlib-only: SQL goes through `docker exec <container> psql`, so nothing needs
pip. For a non-Docker Postgres (brew, Neon, RDS) pass --psql "psql <connection-url>".

Usage:
    docker compose up -d --wait
    python3 scripts/load-aec.py [--zip path/to/AllAnnualData.zip]
"""

import argparse
import csv
import io
import re
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

DOWNLOAD_URL = "https://transparency.aec.gov.au/Download/AllAnnualData"
USER_AGENT = "realm-au-donations loader (https://github.com/johnsonr/realm-au-donations)"

# file name -> return_type for summary files without their own Return Type column
SUMMARY_FILES = {
    "Party Returns.csv": "Party Return",
    "Donor Returns.csv": "Donor Return",
    "Associated Entity Returns.csv": "Associated Entity Return",
    "MemberOfParliamentReturns.csv": None,          # has its own Return Type column
    "Significant Third Party Returns.csv": None,    # has its own Return Type column (+ ABN/ACN)
    "Third Party Returns.csv": "Third Party Return",  # carries ABN/ACN
}


def norm_name(raw: str) -> str:
    """The entity-resolution pass: trim and collapse whitespace. Kept deliberately
    conservative — variants like party branches stay distinct entities, because the
    register treats them as distinct disclosers."""
    return re.sub(r"\s+", " ", (raw or "").strip())


def num(raw: str):
    """Money as written. An integral amount stays integral — writing 55000.0 into a NUMERIC
    column makes the driver hand the graph a scale-1 decimal, and the value then reads as
    `55000.0` everywhere downstream for no reason."""
    s = (raw or "").strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        d = float(s)
    except ValueError:
        return None
    return int(d) if d.is_integer() else d


def iso_date(raw: str):
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", (raw or "").strip())
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else None


class Loader:
    def __init__(self):
        self.entities: dict[str, int] = {}
        self.abn: dict[int, str] = {}
        self.acn: dict[int, str] = {}
        self.returns, self.receipts, self.donations, self.debts = [], [], [], []
        self.skipped = 0

    def entity(self, raw: str):
        name = norm_name(raw)
        if not name:
            return None
        if name not in self.entities:
            self.entities[name] = len(self.entities) + 1
        return self.entities[name]

    def load_zip(self, z: zipfile.ZipFile):
        def rows(name):
            with z.open(name) as fh:
                yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))

        for fname, fixed_type in SUMMARY_FILES.items():
            for r in rows(fname):
                eid = self.entity(r.get("Name"))
                if eid is None:
                    self.skipped += 1
                    continue
                for key in ("ABN", "ACN"):
                    # DIGITS ONLY. The register writes ABNs both ways ('65010582680' and
                    # '98 632 816 383'), and every register we join to — AusTender's supplier ABN,
                    # the ABR bulk extract, Wikidata's P3548 — carries digits. Storing the spelling
                    # the return happened to use makes an exact join fail for half the rows, which
                    # reads as "this donor holds no contracts" rather than "the formats differ".
                    v = re.sub(r"\D", "", (r.get(key) or ""))
                    if v:
                        (self.abn if key == "ABN" else self.acn).setdefault(eid, v)
                self.returns.append((
                    eid,
                    r.get("Financial Year", "").strip(),
                    fixed_type or r.get("Return Type", "").strip(),
                    num(r.get("Total Receipts")),
                    num(r.get("Total Payments")) if r.get("Total Payments") is not None else num(r.get("Total Expenditure")),
                    num(r.get("Total Debts")),
                    num(r.get("Total Donations Made")),
                    num(r.get("Total Donations Received")) if r.get("Total Donations Received") is not None else num(r.get("Total Gifts Received")),
                    num(r.get("Electoral Expenditure")),
                ))

        for r in rows("Detailed Receipts.csv"):
            rec, payer = self.entity(r.get("Recipient Name")), self.entity(r.get("Received From"))
            if rec is None or payer is None:
                self.skipped += 1
                continue
            self.receipts.append((rec, payer, r.get("Financial Year", "").strip(),
                                  r.get("Return Type", "").strip(), r.get("Receipt Type", "").strip(),
                                  num(r.get("Value"))))
        # The two small "donations received" files are receipts in all but name.
        for fname in ("Donor Donations Received.csv", "Third Party Donations Received.csv"):
            for r in rows(fname):
                rec, payer = self.entity(r.get("Name")), self.entity(r.get("Donation Received From"))
                if rec is None or payer is None:
                    self.skipped += 1
                    continue
                self.receipts.append((rec, payer, r.get("Financial Year", "").strip(),
                                      fname.removesuffix(".csv"), "Donation Received", num(r.get("Value"))))

        for r in rows("Donations Made.csv"):
            donor, rec = self.entity(r.get("Donor Name")), self.entity(r.get("Donation Made To"))
            if donor is None or rec is None:
                self.skipped += 1
                continue
            self.donations.append((donor, rec, r.get("Financial Year", "").strip(),
                                   iso_date(r.get("Date")), num(r.get("Value"))))

        for r in rows("Detailed Debts.csv"):
            debtor, creditor = self.entity(r.get("Name")), self.entity(r.get("Creditor Name"))
            if debtor is None or creditor is None:
                self.skipped += 1
                continue
            self.debts.append((debtor, creditor, r.get("Financial Year", "").strip(),
                               num(r.get("Amount owed")), r.get("Financial or Non-financial institution", "").strip()))


def copy_block(table: str, cols: list[str], rows) -> str:
    def cell(v):
        if v is None:
            return "\\N"
        return str(v).replace("\\", "\\\\").replace("\t", " ").replace("\n", " ").replace("\r", " ")

    lines = [f"COPY {table} ({', '.join(cols)}) FROM STDIN;"]
    lines += ["\t".join(cell(c) for c in row) for row in rows]
    lines.append("\\.")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", help="use a local AllAnnualData.zip instead of downloading")
    ap.add_argument("--container", default="au-donations-pg", help="docker container running Postgres")
    ap.add_argument("--psql", help='run this psql command instead of docker exec (e.g. "psql <url>")')
    args = ap.parse_args()

    if args.zip:
        data = Path(args.zip).read_bytes()
    else:
        print(f"downloading {DOWNLOAD_URL} …", flush=True)
        req = urllib.request.Request(DOWNLOAD_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = resp.read()
        print(f"  {len(data):,} bytes")

    loader = Loader()
    loader.load_zip(zipfile.ZipFile(io.BytesIO(data)))

    psql = args.psql.split() if args.psql else [
        "docker", "exec", "-i", args.container, "psql", "-U", "admin", "-d", "au_donations",
    ]
    psql += ["-q", "-v", "ON_ERROR_STOP=1"]

    schema = (Path(__file__).resolve().parent.parent / "sql" / "schema.sql").read_text()
    script = io.StringIO()
    script.write(schema)
    script.write(copy_block("entities", ["id", "name", "abn", "acn"],
                            ((i, n, loader.abn.get(i), loader.acn.get(i)) for n, i in loader.entities.items())))
    script.write(copy_block("returns",
                            ["entity_id", "financial_year", "return_type", "total_receipts", "total_payments",
                             "total_debts", "total_donations_made", "total_donations_received", "electoral_expenditure"],
                            loader.returns))
    script.write(copy_block("receipts",
                            ["recipient_id", "payer_id", "financial_year", "return_type", "receipt_type", "value"],
                            loader.receipts))
    script.write(copy_block("donations_made",
                            ["donor_id", "recipient_id", "financial_year", "made_on", "value"],
                            loader.donations))
    script.write(copy_block("debts",
                            ["debtor_id", "creditor_id", "financial_year", "amount", "institution_type"],
                            loader.debts))
    script.write("SELECT setval('entities_id_seq', (SELECT max(id) FROM entities));\nANALYZE;\n")

    print(f"loading via: {' '.join(psql[:4])} …", flush=True)
    proc = subprocess.run(psql, input=script.getvalue().encode(), capture_output=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode()[-2000:])
        return 1

    print(f"entities:       {len(loader.entities):>8,}")
    print(f"returns:        {len(loader.returns):>8,}")
    print(f"receipts:       {len(loader.receipts):>8,}")
    print(f"donations_made: {len(loader.donations):>8,}")
    print(f"debts:          {len(loader.debts):>8,}")
    print(f"rows skipped (blank name): {loader.skipped:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
