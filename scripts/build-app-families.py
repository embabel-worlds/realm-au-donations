#!/usr/bin/env python3
"""Write apps/families.json — the family labels the app's dropdown offers.

ONE SOURCE OF TRUTH. The labels come from sql/party_families.sql, the same committed mapping every
family view queries through, so the dropdown can never offer a family the data does not know or miss
one it does. Run this after regenerating that file (scripts/build-party-families.py) and commit both.

The alternative — enumerating families from the graph at runtime — is not available: a family is an
ANCHOR, pinned by name, and anchors cannot be listed. That is a deliberate property of the join
surface, not an oversight, so the catalogue belongs beside the mapping rather than in a query.
"""
import json
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
SQL = HERE.parent / "sql" / "party_families.sql"
OUT = HERE.parent / "apps" / "families.json"

rows = re.findall(r"\('(?:[^']|'')*',\s*'((?:[^']|'')*)',\s*'((?:[^']|'')*)'\)", SQL.read_text())
families = {}
for family, tier in rows:
    name = family.replace("''", "'")
    families.setdefault(name, set()).add(tier)

# Ordered by how many lodging entities the family has, which is the register's own measure of how
# spread a party's fundraising is — and the order a reader most likely wants the dropdown in.
catalogue = sorted(
    ({"family": f, "tiers": sorted(t), "lodgers": sum(1 for x, _ in rows if x.replace("''", "'") == f)}
     for f, t in families.items()),
    key=lambda e: (-e["lodgers"], e["family"]),
)
OUT.parent.mkdir(exist_ok=True)
OUT.write_text(json.dumps(catalogue, indent=1))
print(f"{len(catalogue)} families → {OUT}")
for e in catalogue[:8]:
    print(f"  {e['lodgers']:3}  {e['family']}")
