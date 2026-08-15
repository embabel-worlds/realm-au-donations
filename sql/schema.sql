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
