-- The normalized AEC Transparency Register schema. The raw export links records only by
-- (Financial Year, free-text Name); this schema resolves names into ONE entities table so
-- every money flow is a real foreign key — which is exactly what the schema miner turns
-- into graph edges.

DROP TABLE IF EXISTS donations_made, receipts, debts, returns, entities CASCADE;

CREATE TABLE entities (
    id      SERIAL PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,   -- whitespace-normalized disclosure name
    abn     TEXT,                   -- present only where a return supplied it
    acn     TEXT
);

-- One row per lodged summary return (party / donor / associated entity / third party / MP).
CREATE TABLE returns (
    id                        SERIAL PRIMARY KEY,
    entity_id                 INT NOT NULL REFERENCES entities(id),
    financial_year            TEXT NOT NULL,
    return_type               TEXT NOT NULL,
    total_receipts            NUMERIC,
    total_payments            NUMERIC,
    total_debts               NUMERIC,
    total_donations_made      NUMERIC,
    total_donations_received  NUMERIC,
    electoral_expenditure     NUMERIC
);

-- Detailed Receipts.csv: money a discloser reported RECEIVING, line by line.
CREATE TABLE receipts (
    id              SERIAL PRIMARY KEY,
    recipient_id    INT NOT NULL REFERENCES entities(id),
    payer_id        INT NOT NULL REFERENCES entities(id),
    financial_year  TEXT NOT NULL,
    return_type     TEXT,
    receipt_type    TEXT,
    value           NUMERIC
);

-- Donations Made.csv: money a donor reported GIVING, line by line.
CREATE TABLE donations_made (
    id              SERIAL PRIMARY KEY,
    donor_id        INT NOT NULL REFERENCES entities(id),
    recipient_id    INT NOT NULL REFERENCES entities(id),
    financial_year  TEXT NOT NULL,
    made_on         DATE,
    value           NUMERIC
);

-- Detailed Debts.csv: what a discloser reported OWING, and to whom.
CREATE TABLE debts (
    id               SERIAL PRIMARY KEY,
    debtor_id        INT NOT NULL REFERENCES entities(id),
    creditor_id      INT NOT NULL REFERENCES entities(id),
    financial_year   TEXT NOT NULL,
    amount           NUMERIC,
    institution_type TEXT
);

CREATE INDEX ON returns (entity_id);
CREATE INDEX ON receipts (recipient_id);
CREATE INDEX ON receipts (payer_id);
CREATE INDEX ON donations_made (donor_id);
CREATE INDEX ON donations_made (recipient_id);
CREATE INDEX ON debts (debtor_id);
CREATE INDEX ON entities (abn);

-- BOTH SIDES OF THE SAME GIFT, compared. A donation is disclosed TWICE under separate legal
-- obligations: by the donor (donations_made) and by the recipient (receipts). They need not agree,
-- and where they disagree materially that is a lead worth checking — the single most useful thing
-- this store can compute, and it needs no external source at all.
--
-- LIKE FOR LIKE, deliberately: only 'Donation Received' receipts are compared. A party's receipts
-- also carry subscriptions, public funding and other income, and including those manufactures
-- enormous fake "gaps" (Westpac appeared to under-report by $2m until this filter went in).
-- Even so a gap is NOT a finding: the two sides may split a gift across financial years, attribute
-- it to different branches, or treat in-kind support differently.
CREATE OR REPLACE VIEW disclosure_discrepancies AS
WITH gave AS (
    SELECT donor_id, recipient_id, financial_year, sum(value) AS donor_says, count(*) AS donor_lines
    FROM donations_made GROUP BY donor_id, recipient_id, financial_year
), got AS (
    SELECT payer_id AS donor_id, recipient_id, financial_year,
           sum(value) AS recipient_says, count(*) AS recipient_lines
    FROM receipts WHERE receipt_type = 'Donation Received'
    GROUP BY payer_id, recipient_id, financial_year
)
SELECT
       -- A UNIQUE row key. The identity property is what the materializer MERGEs on, and every
       -- discrepancy for one party shares its recipient_id — so keying on that collapsed a party's
       -- entire set into a single node and the view returned one row. The key must identify the
       -- ROW, not the anchor it is fetched by.
       gave.donor_id || ':' || gave.recipient_id || ':' || gave.financial_year AS discrepancy_key,
       gave.recipient_id,
       gave.donor_id,
       dn.name                                   AS donor_name,
       rc.name                                   AS recipient_name,
       gave.financial_year,
       gave.donor_says,
       got.recipient_says,
       (gave.donor_says - got.recipient_says)    AS gap,
       abs(gave.donor_says - got.recipient_says) AS gap_size,
       gave.donor_lines,
       got.recipient_lines
FROM gave
JOIN got ON gave.donor_id = got.donor_id
        AND gave.recipient_id = got.recipient_id
        AND gave.financial_year = got.financial_year
JOIN entities dn ON dn.id = gave.donor_id
JOIN entities rc ON rc.id = gave.recipient_id
WHERE gave.donor_says <> got.recipient_says;

-- ── Political families ────────────────────────────────────────────────────────────────────────
--
-- The register names LEGAL LODGERS, not organisations. There are 2,073 distinct recipient names
-- for a few dozen political interests, and the variants are not cosmetic: 'Climate 200 Pty Ltd'
-- and 'Climate 200 Pty Limited' hold $19.75m and $10.35m; 'Liberal Party (W.A. Division) Inc' and
-- '... Inc.' differ by a full stop and $6.25m; 'ALP-FED' holds $3.05m under a bare code. Any
-- question of the form "who funds party X" is unanswerable without putting these back together.
--
-- ROWS COME FROM sql/party_families.sql, generated by scripts/build-party-families.py and
-- committed so the mapping is reviewable line by line. The table is deliberately dumb: an exact
-- lodged name to a family label. No patterns, no inference at query time.
CREATE TABLE IF NOT EXISTS party_families (
    lodged_name text PRIMARY KEY,
    family      text NOT NULL,
    -- 'federal', a state/territory code, or 'unspecified' — read off the lodger's own name.
    -- Present so a reader can see that a family total spans separate legal entities; never used
    -- to decide whether a row counts.
    tier        text NOT NULL
);
CREATE INDEX IF NOT EXISTS party_families_family_idx ON party_families (family);

-- Who backs a political FAMILY, with the branches the money actually went to.
--
-- LEFT JOIN, not JOIN: a recipient absent from the mapping keeps its money in the answer with a
-- NULL family. An inner join would shrink every total silently — the same failure as dropping
-- unidentified donors from an ownership view, and just as invisible in the output.
--
-- `branches` and `branches_lodged` are the honesty columns. A family total of $23,969 that turns
-- out to be four same-day payments to four separate legal entities is a different fact from one
-- cheque, and the reader must be able to see which without running a second query.
CREATE OR REPLACE VIEW family_backers AS
SELECT
    coalesce(pf.family, r.name) || '|' || d.financial_year || '|' || dn.name AS backer_key,
    coalesce(pf.family, r.name)          AS family,
    pf.family IS NOT NULL                AS family_mapped,
    d.financial_year                     AS financial_year,
    dn.name                              AS donor_name,
    sum(d.value)::bigint                 AS total,
    count(*)                             AS gifts,
    count(DISTINCT r.name)               AS branches,
    string_agg(DISTINCT r.name, ' | ')   AS branches_lodged,
    string_agg(DISTINCT coalesce(pf.tier, 'unmapped'), ', ') AS tiers,
    min(d.made_on)                       AS first_gift,
    max(d.made_on)                       AS last_gift
FROM donations_made d
JOIN entities r  ON r.id  = d.recipient_id
JOIN entities dn ON dn.id = d.donor_id
LEFT JOIN party_families pf ON pf.lodged_name = r.name
GROUP BY coalesce(pf.family, r.name), pf.family IS NOT NULL, d.financial_year, dn.name;

-- Same-day giving to MORE THAN ONE political family: the pattern a per-return reading cannot see.
-- A lobby group paying several sides on one day is not itself wrongdoing — it is ordinary access
-- buying, and often disclosed exactly as required. It is, however, invisible in any single return,
-- which is the whole reason to compute it here.
CREATE OR REPLACE VIEW same_day_multi_party AS
SELECT
    dn.name || '|' || d.made_on::text                       AS same_day_key,
    dn.name                                                 AS donor_name,
    d.made_on                                               AS made_on,
    d.financial_year                                        AS financial_year,
    count(DISTINCT coalesce(pf.family, r.name))             AS families_paid,
    sum(d.value)::bigint                                    AS paid_that_day,
    string_agg(DISTINCT coalesce(pf.family, r.name), ' | ') AS which_families
FROM donations_made d
JOIN entities r  ON r.id  = d.recipient_id
JOIN entities dn ON dn.id = d.donor_id
LEFT JOIN party_families pf ON pf.lodged_name = r.name
WHERE d.made_on IS NOT NULL
GROUP BY dn.name, d.made_on, d.financial_year
HAVING count(DISTINCT coalesce(pf.family, r.name)) > 1;

-- ── Donor identity ────────────────────────────────────────────────────────────────────────────
--
-- The donor side has the same fragmentation as the recipient side and CANNOT be fixed the same
-- way. Recipients are a closed set: 174 names cover 95% of the money, so a curated table works.
-- Donors are not: 1,956 names cover 95%, there is no register of them, and new ones appear every
-- year.
--
-- So this does only what can be done WITHOUT ASSERTING ANYTHING. Two rules, both identity:
--
--   1. SAME ABN. Definitionally the same legal entity — the register simply spelled it two ways
--      ('CFMEU Mining & Energy Division' / 'Mining and Energy Union').
--   2. IDENTICAL AFTER NORMALIZING case, punctuation and legal suffix. 'National Australia Bank
--      Limited' and '... Ltd' ($2.5m); 'Progressive Business Association Inc' and '... Inc.',
--      differing by a full stop ($1.59m).
--
-- WHAT IT DELIBERATELY DOES NOT DO is group a CORPORATE FAMILY — the related companies behind one
-- owner. That is not an identity question, it is an assertion of control over named companies and,
-- by implication, named people. Name similarity is not evidence for it: two donors sharing a
-- surname may be one group or may be strangers, and a table row asserting the former is more
-- dangerous than a sentence doing so, because it looks like a fact and propagates silently into
-- every total downstream. A corporate-group mapping needs a filing per row and is a separate
-- piece of work — see docs/CORPORATE_GROUPS.md.
CREATE OR REPLACE VIEW donor_identity AS
WITH normalized AS (
    SELECT e.id,
           e.name,
           nullif(e.abn, '') AS abn,
           trim(regexp_replace(
               regexp_replace(lower(e.name), '[^a-z0-9 ]', '', 'g'),
               '\s+(pty|ltd|limited|inc|incorporated|the)\s*', ' ', 'g')) AS name_key
    FROM entities e
),
-- ABN wins where present: it is a registered identifier, the normalized name is a heuristic.
identified AS (
    SELECT id, name, abn, coalesce(abn, name_key) AS identity_key FROM normalized
),
totals AS (
    SELECT i.identity_key, i.name, sum(d.value)::bigint AS name_total, count(d.*) AS name_gifts
    FROM identified i JOIN donations_made d ON d.donor_id = i.id
    GROUP BY i.identity_key, i.name
),
-- The canonical label is the spelling that gave the most money — a presentation choice, not a
-- judgement about which spelling is "right". Every variant is listed beside it either way.
canonical AS (
    SELECT DISTINCT ON (identity_key) identity_key, name AS canonical_name
    FROM totals ORDER BY identity_key, name_total DESC, name
)
SELECT
    t.name                                  AS lookup_name,
    t.identity_key                          AS identity_key,
    c.canonical_name                        AS canonical_name,
    count(*) OVER (PARTITION BY t.identity_key)          AS variants,
    string_agg(t.name, ' | ') OVER (PARTITION BY t.identity_key) AS variant_names,
    sum(t.name_total) OVER (PARTITION BY t.identity_key) AS combined_total,
    sum(t.name_gifts) OVER (PARTITION BY t.identity_key) AS combined_gifts,
    t.name_total                            AS this_spelling_total
FROM totals t JOIN canonical c ON c.identity_key = t.identity_key;

-- The role the DATASOURCE connects as: SELECT-only, enforced by the database itself.
-- Layer three of the read-only assurance (after the platform's readOnly-by-default
-- refusal and the JDBC session hint): even a compromised credential cannot write.
DO $$ BEGIN
    CREATE ROLE demo_reader LOGIN PASSWORD 'read-only-demo';
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
GRANT CONNECT ON DATABASE au_donations TO demo_reader;
GRANT USAGE ON SCHEMA public TO demo_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO demo_reader;  -- includes views
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO demo_reader;
