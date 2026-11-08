-- Context Hub schema (SPEC sections 8 and 17).
-- Four logical stores (case_memory, events, customer_context, archive) plus policy,
-- as PostgreSQL schemas in one database. They are logical boundaries, not separate databases.
--
-- Rules that apply to every table below:
-- * No DEFAULT now() anywhere: every timestamp comes from the simulated clock (hub/clock.py),
--   so the demo can move time forward and replay deterministically (SPEC 17.12).
-- * All timestamps are timestamptz; code always passes Asia/Kolkata aware datetimes.
-- * Tables are created by the admin role, so hub_app never owns them (owners bypass grants).

CREATE SCHEMA case_memory;
CREATE SCHEMA events;
CREATE SCHEMA customer_context;
CREATE SCHEMA archive;
CREATE SCHEMA policy;

-- Close-once rule for versioned tables (SPEC 17.13). hub_app may UPDATE valid_to, but the column
-- grant alone would let it reopen a closed version (set back to NULL) or move a closure time,
-- which rewrites history. This trigger allows exactly one change per row: NULL -> a timestamp
-- >= valid_from. It fires for every role, including the owner. Used by case_memory.units and
-- customer_context.items; it lives in case_memory only because a function needs some schema.
CREATE FUNCTION case_memory.enforce_close_once() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.valid_to IS NOT DISTINCT FROM OLD.valid_to THEN
        RETURN NEW;  -- valid_to untouched (no other column is updatable by hub_app anyway)
    END IF;
    IF OLD.valid_to IS NOT NULL THEN
        RAISE EXCEPTION '%.%: version % is already closed at %; a closed version is final (tried to set valid_to to %)',
            TG_TABLE_SCHEMA, TG_TABLE_NAME, OLD.version, OLD.valid_to, coalesce(NEW.valid_to::text, 'NULL')
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.valid_to < OLD.valid_from THEN
        RAISE EXCEPTION '%.%: version % cannot close at % because it only became valid at %',
            TG_TABLE_SCHEMA, TG_TABLE_NAME, OLD.version, NEW.valid_to, OLD.valid_from
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

-- ---------------------------------------------------------------------------
-- customer_context: customer-scope data (different scope, expiry and permission model)
-- ---------------------------------------------------------------------------

-- The customer record. IDs are used everywhere else; display_name is for the UI only (SPEC 17.1).
CREATE TABLE customer_context.customers (
    customer_id  text PRIMARY KEY,
    display_name text NOT NULL
);

-- Explicit customer preferences across cases, versioned like memory units.
-- A row is never edited except to set valid_to when a newer version replaces it.
CREATE TABLE customer_context.items (
    item_id       text        NOT NULL,
    version       integer     NOT NULL CHECK (version >= 1),
    customer_id   text        NOT NULL REFERENCES customer_context.customers,
    key           text        NOT NULL,
    value         text        NOT NULL,
    source        text        NOT NULL CHECK (source IN ('customer', 'agent', 'system')),
    -- Why this version exists (SPEC 17.2, 17.21): correction = the earlier value was wrong;
    -- reschedule = a commitment's time changed; replacement = a preference changed.
    -- A closed version is displayed according to its successor's change_reason.
    change_reason text        NOT NULL CHECK (change_reason IN ('created', 'reschedule', 'correction', 'replacement', 'status_change')),
    valid_from    timestamptz NOT NULL,
    valid_to      timestamptz,
    expires_at    timestamptz,
    PRIMARY KEY (item_id, version),
    -- Version 1 is always the creation, and every later version is a change.
    CHECK ((version = 1) = (change_reason = 'created')),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
-- At most one current version per item.
CREATE UNIQUE INDEX items_one_current ON customer_context.items (item_id) WHERE valid_to IS NULL;
-- At most one current value per preference key per customer: a new value replaces, never coexists.
CREATE UNIQUE INDEX items_one_current_key ON customer_context.items (customer_id, key) WHERE valid_to IS NULL;
CREATE TRIGGER items_close_once BEFORE UPDATE ON customer_context.items
    FOR EACH ROW EXECUTE FUNCTION case_memory.enforce_close_once();

-- ---------------------------------------------------------------------------
-- case_memory: claims and commitments, versioned, grouped by case
-- ---------------------------------------------------------------------------

-- A case is a container, not a unit (SPEC 4). Created by scenario steps, not seeded (SPEC 17.4).
CREATE TABLE case_memory.cases (
    case_id       text        PRIMARY KEY,
    customer_id   text        NOT NULL REFERENCES customer_context.customers,
    business_line text        NOT NULL CHECK (business_line IN ('payments', 'credit')),
    case_type     text        NOT NULL CHECK (case_type IN ('card_dispute', 'upi_failed_debit')),
    opened_at     timestamptz NOT NULL,
    closed_at     timestamptz,
    CHECK (closed_at IS NULL OR closed_at >= opened_at)
);

-- One row per version of a claim or commitment. unit_id is stable across versions;
-- the current version is the row with valid_to IS NULL (SPEC 4).
CREATE TABLE case_memory.units (
    unit_id       text        NOT NULL,
    version       integer     NOT NULL CHECK (version >= 1),
    case_id       text        NOT NULL REFERENCES case_memory.cases,
    customer_id   text        NOT NULL REFERENCES customer_context.customers,
    unit_type     text        NOT NULL CHECK (unit_type IN ('claim', 'commitment')),
    owner         text        CHECK (owner IN ('bank', 'customer')),
    action        text,
    due_at        timestamptz,
    -- Only stored statuses. "Superseded" and "Corrected" are never stored: they are derived
    -- from the successor's change_reason, because rows are immutable except valid_to (SPEC 17.2).
    status        text        NOT NULL,
    payload       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    source        text        NOT NULL CHECK (source IN ('customer', 'agent', 'system')),
    -- Restricted units are denied to every agent unless a policy rule explicitly allows them.
    sensitivity   text        NOT NULL DEFAULT 'normal' CHECK (sensitivity IN ('normal', 'restricted')),
    change_reason text        NOT NULL CHECK (change_reason IN ('created', 'reschedule', 'correction', 'replacement', 'status_change')),
    valid_from    timestamptz NOT NULL,
    valid_to      timestamptz,
    created_by    text        NOT NULL,
    PRIMARY KEY (unit_id, version),
    CHECK ((version = 1) = (change_reason = 'created')),
    CHECK (valid_to IS NULL OR valid_to >= valid_from),
    -- Claims are only ever 'active'; commitments use the lifecycle states of SPEC 7 and 17.8.
    CHECK (
        (unit_type = 'claim' AND status = 'active')
        OR (unit_type = 'commitment' AND status IN ('active', 'attempted', 'fulfilled', 'missed', 'cancelled'))
    ),
    -- A commitment is only actionable with an owner, an action and an exact due time (SPEC 6).
    CHECK (unit_type <> 'commitment' OR (owner IS NOT NULL AND action IS NOT NULL AND due_at IS NOT NULL))
);
-- Exactly one current version per unit. Together with the expected-version check in
-- hub/store.py this is the optimistic-concurrency guard: two writers cannot both open version n+1.
CREATE UNIQUE INDEX units_one_current ON case_memory.units (unit_id) WHERE valid_to IS NULL;
CREATE TRIGGER units_close_once BEFORE UPDATE ON case_memory.units
    FOR EACH ROW EXECUTE FUNCTION case_memory.enforce_close_once();
CREATE INDEX units_by_case ON case_memory.units (case_id);
CREATE INDEX units_by_customer ON case_memory.units (customer_id);

-- ---------------------------------------------------------------------------
-- events: append-only chronology. hub_app may INSERT and SELECT only (sql/grants.sql).
-- ---------------------------------------------------------------------------
CREATE TABLE events.log (
    event_id    bigserial   PRIMARY KEY,
    customer_id text        NOT NULL REFERENCES customer_context.customers,
    -- Nullable: customer-scope events (a preference change) belong to no case.
    case_id     text        REFERENCES case_memory.cases,
    -- Nullable, and no foreign key: units have a composite key and an event refers to the unit, not a version.
    unit_id     text,
    event_type  text        NOT NULL,
    actor_type  text        NOT NULL CHECK (actor_type IN ('customer', 'agent', 'system')),
    actor_id    text        NOT NULL,
    -- Two timestamps on purpose: when it happened vs when the bank learned it
    -- (a late dialler record: occurred_at 09:04, recorded_at 09:40).
    occurred_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL,
    payload     jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX log_by_customer_time ON events.log (customer_id, occurred_at);
CREATE INDEX log_by_case ON events.log (case_id);

-- ---------------------------------------------------------------------------
-- archive: raw transcripts, audit only. Agents never read it; hub_app has no access at all.
-- ---------------------------------------------------------------------------
CREATE TABLE archive.conversations (
    conversation_id text        PRIMARY KEY,
    customer_id     text        NOT NULL REFERENCES customer_context.customers,
    case_id         text        REFERENCES case_memory.cases,
    agent_role      text        NOT NULL,
    channel         text        NOT NULL,
    started_at      timestamptz NOT NULL,
    transcript      jsonb       NOT NULL
);

-- ---------------------------------------------------------------------------
-- policy: access rules and the access log (enforced in hub/policy.py, never by the model)
-- ---------------------------------------------------------------------------

-- NULL in agent_role, purpose, case_type or sensitivity means wildcard. The most specific
-- matching rule (most non-null columns) wins; on a tie the more restrictive decision wins
-- (SPEC 17.3). Customer-context allow and default deny live in code, not here.
CREATE TABLE policy.rules (
    agent_role  text,
    purpose     text,
    case_type   text CHECK (case_type IN ('card_dispute', 'upi_failed_debit')),
    sensitivity text CHECK (sensitivity IN ('normal', 'restricted')),
    decision    text NOT NULL CHECK (decision IN ('allow', 'existence_only', 'deny'))
);
-- Two rules with the same match columns would make the outcome depend on tie-breaking;
-- forbid it. NULLS NOT DISTINCT makes two wildcards count as equal.
CREATE UNIQUE INDEX rules_unique_match ON policy.rules
    (agent_role, purpose, case_type, sensitivity) NULLS NOT DISTINCT;

-- Every access decision, with the reason, for audit and for the demo's access-log tab.
CREATE TABLE policy.access_log (
    log_id      bigserial   PRIMARY KEY,
    at          timestamptz NOT NULL,
    agent_id    text        NOT NULL,
    agent_role  text        NOT NULL,
    purpose     text        NOT NULL,
    customer_id text        NOT NULL,
    case_id     text,
    unit_id     text,
    decision    text        NOT NULL CHECK (decision IN ('allow', 'existence_only', 'deny')),
    reason      text        NOT NULL
);
