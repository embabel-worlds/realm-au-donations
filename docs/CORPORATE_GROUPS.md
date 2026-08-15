# Corporate groups: a design note, not an implementation

Two donors can be the same **entity** — that is identity, it is mechanical, and `donor_identity`
does it (same ABN, or identical after normalizing case, punctuation and legal suffix).

Two donors can also belong to the same **corporate group** — related companies under one owner.
That is a different kind of statement and this realm does not make it yet. This note says how it
would have to work.

## Why it is not just more of the same

The party-family mapping works because parties are a closed, small, stable set and the claim is
bookkeeping: `Queensland Greens` belongs with `Australian Greens`. Nobody is accused of anything.

A corporate-group mapping asserts **control over named companies and, by implication, named
people**. Three consequences follow, and all three are load-bearing:

1. **Name similarity is not evidence.** Two donors sharing a surname or a word may be one group or
   may be strangers. This is the single most tempting shortcut and the one that would put a false
   claim about a real family into a published total.
2. **A wrong row is worse than a wrong sentence.** A row looks like a fact, and it propagates
   silently into every rollup downstream, where nobody re-reads its provenance.
3. **A scored match is not evidence either.** A commercial knowledge graph resolves company names
   with a threshold (0.45, deliberately permissive) and identified roughly one donor in six here.
   It is useful for proposing candidates to check. It must never populate the table.

## The standard: a filing per row

The worked example is the one that prompted this note. `ORYXIUM PTY LIMITED` gave $1.8m to the
Liberal family in 2024-25 and resolves to nothing in a corporate knowledge graph. An open web
search finds its ownership — and also finds partisan commentary describing the payments as
"secretive" and "routed through an obscure company", which is characterisation, not fact, and has
no place in a mapping table or in generated prose.

**The ownership was resolvable without any of that.** It is in the primary record:

| source | what it gives |
|---|---|
| LEI registry | ACN 625887612 |
| ABN Lookup | ABN 78 626 951 915 |
| **ASX Form 603** (Corporations Act s671B substantial holder notice) | `Oryxium Pty Limited ATF The Oryxium 2025 FRB Trust ("Oryxium") (a trust for the benefit of members of the …)` |

A Form 603 is filed under legal obligation, names the substantial holder and its associates, and
is a public document with a stable URL. That is the evidence standard: **if a filing does not say
it, the row does not exist.** Commentary may tell you where to look; it never supplies the row.

## Shape

```sql
CREATE TABLE donor_groups (
    lodged_name    text PRIMARY KEY,   -- as the register spells it
    group_name     text NOT NULL,      -- the group label, ours
    evidence_kind  text NOT NULL,      -- 'asx-603' | 'asx-604' | 'asx-605' | 'abr' | 'asic'
    evidence_url   text NOT NULL,      -- the document itself, stable and public
    evidence_quote text NOT NULL,      -- the words in it that establish the link
    asserted_on    date NOT NULL       -- ownership changes; a row is true as at a date
);
```

`evidence_quote` is the column that makes the rest honest. A URL can rot or point at a document
that says something else; the quote is what a reader checks the link against, and writing it forces
whoever adds the row to have actually read the filing.

Every view over this must surface `evidence_url` beside any group figure, the way `branchesLodged`
travels with a party-family total. A group number without its filings is not publishable.

## The capability gap

Search snippets are not enough. The Form 603 snippet truncates at exactly the point the
beneficiaries are named:

> `… ATF The Oryxium 2025 FRB Trust ("Oryxium") (a trust for the benefit of members of the ...`

So the pipeline has to **fetch and read the filing**, not summarise search results. That is a
document-extraction step, not a search step — and it is also what keeps the commentary out, since
nothing but the filing is ever read. Until that exists, `donor_groups` should be populated by hand,
a few dozen rows for the groups that matter, each with its quote.

## What to do in the meantime

- `DonorIdentity` for same-entity spellings — mechanical, already shipped.
- `DonorTrail` for the trail, returned verbatim with URLs, for a human to read and weigh.
- `GroupGiving` for knowledge-graph candidates, clearly labelled as scored name matches.

None of those asserts a group. That is the correct state until the filings can be read.
