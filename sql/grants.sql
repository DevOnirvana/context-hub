-- Least-privilege roles (SPEC 8 and 17.5). Safe to rerun: roles live at cluster level and
-- survive scripts/reset_db.py, while the grants below are reapplied to the fresh schemas.
--
-- Local Homebrew PostgreSQL uses trust authentication, so the roles have no password.

DO $$
BEGIN
    -- hub_app: the only role the app and agents' hub calls use.
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hub_app') THEN
        CREATE ROLE hub_app LOGIN;
    END IF;
    -- hub_archiver: writes raw transcripts and can do nothing else.
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hub_archiver') THEN
        CREATE ROLE hub_archiver LOGIN;
    END IF;
END
$$;

-- hub_app ----------------------------------------------------------------------
-- No USAGE on the archive schema: agents never read transcripts, so the hub's read path
-- cannot reach them even by mistake.
GRANT USAGE ON SCHEMA case_memory, events, customer_context, policy TO hub_app;

-- Read and add, everywhere it has access.
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA case_memory, events, customer_context, policy TO hub_app;

-- Versioned tables: the only permitted change is closing a version by setting valid_to.
-- Any other edit (status, due_at, payload) must be a new version row, so history cannot be rewritten.
GRANT UPDATE (valid_to) ON case_memory.units, customer_context.items TO hub_app;

-- events.log gets no UPDATE and no DELETE: the database itself refuses edits to history.
-- Nothing gets DELETE: erasure is out of scope for the prototype (LATER.md).

-- bigserial columns (events.log, policy.access_log) need the sequence to insert.
GRANT USAGE ON ALL SEQUENCES IN SCHEMA events, policy TO hub_app;

-- hub_archiver ---------------------------------------------------------------------
-- INSERT only: it can store a transcript but cannot read one back.
GRANT USAGE ON SCHEMA archive TO hub_archiver;
GRANT INSERT ON archive.conversations TO hub_archiver;
