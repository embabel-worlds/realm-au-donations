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

-- The role the DATASOURCE connects as: SELECT-only, enforced by the database itself.
-- Layer three of the read-only assurance (after the platform's readOnly-by-default
-- refusal and the JDBC session hint): even a compromised credential cannot write.
DO $$ BEGIN
    CREATE ROLE demo_reader LOGIN PASSWORD 'read-only-demo';
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
GRANT CONNECT ON DATABASE au_donations TO demo_reader;
GRANT USAGE ON SCHEMA public TO demo_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO demo_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO demo_reader;
