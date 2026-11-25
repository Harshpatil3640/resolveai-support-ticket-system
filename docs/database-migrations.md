# Database Setup and Migrations

ResolveAI manages its database schema with [Alembic](https://alembic.sqlalchemy.org/).
Every schema change is a versioned migration under `alembic/versions/`, and
each database records the revision it is at in an `alembic_version` table.

`Base.metadata.create_all` is **no longer used** to create the application
database: it creates tables without recording a revision, which leaves the
database unmanaged.

All commands below are run from the project root with the virtual environment
activated. Alembic targets the same database as the application
(`resolveai.db` in the project root, configured in `app/db/database.py`);
no connection string is stored in `alembic.ini`.

**Migrations are always run explicitly** by an operator, with
`alembic upgrade head` (or `python -m app.db.init_db`, which does the same).
Nothing else migrates the database as a side effect: the API does not
migrate on startup, and scripts such as `app.scripts.create_agent` only
*check* the migration state (see section 7).

## Revisions

| Revision | Purpose |
|----------|---------|
| `0001` | **Baseline.** The schema exactly as it existed before Alembic (`users`, `tickets`, `revoked_tokens` with their indexes and constraints). |
| `0002` | Adds `users.token_version` (`INTEGER NOT NULL DEFAULT 0`). Existing users receive `0`. |
| `0003` | **Ticket assignment and history.** Adds `tickets.assigned_to_user_id` (nullable, foreign key to `users.id`, indexed), `tickets.assigned_at` (nullable) and `tickets.version` (`INTEGER NOT NULL DEFAULT 1`, the optimistic-concurrency counter), and creates the `ticket_events` history table. Existing tickets keep every value, start unassigned at version `1` and receive no history. See [section 8](#8-upgrading-an-existing-database-to-0003). |
| `0004` | **Ticket conversations.** Creates the `ticket_messages` table (`id`, `ticket_id`, `sender_user_id`, `sender_role`, `content`, `created_at`). `ticket_id` references `tickets.id` with `ON DELETE CASCADE`; `sender_user_id` references `users.id`. Named CHECK constraints allow only the roles `admin`, `agent` and `customer` and reject blank content or content over 10,000 characters. Index `ix_ticket_messages_ticket_id_id` on `(ticket_id, id)`. No existing table is altered: every user, ticket and history row is left as it is. Downgrading to `0003` drops the index and the table (and every message). Rehearse on a copy first, as in [section 8](#8-upgrading-an-existing-database-to-0003). |

`alembic heads` reports `0004`. A database is only at `0004` once
`alembic upgrade head` has been run against it; check with `alembic current`.

## 1. Install dependencies

```cmd
python -m pip install -r requirements.txt
```

This installs Alembic (pinned in `requirements.txt`).

## 2. Brand-new database

If `resolveai.db` does not exist yet:

```cmd
alembic upgrade head
```

This runs every migration from the baseline to the latest revision.
`python -m app.db.init_db` does the same thing.

The baseline migration (`0001`) only initialises an **empty** database. If
the target database already contains any table (other than SQLite's internal
`sqlite_*` tables and an empty `alembic_version` table), it refuses to run and
nothing is changed, even if those tables are unrelated to ResolveAI. Use a new
file, or follow section 3 for an existing ResolveAI database.

## 3. Existing database created before Alembic

A database created by the earlier `create_all` setup already has the baseline
tables but no `alembic_version` table. **Do not run `alembic upgrade head`
on it first**: the baseline migration detects the existing tables and refuses
to run (nothing is changed), because it must never try to recreate them.

Instead, verify the schema, record ("stamp") the baseline revision, then apply
the later migrations.

### Step 1: Back up the database

```cmd
copy resolveai.db resolveai.db.bak
```

### Step 2: Verify that the schema matches the baseline

```cmd
python -m app.db.check_baseline
```

The check opens the database **read-only** and compares its tables, columns,
types, nullability, defaults, primary keys, foreign keys, indexes, unique and
check constraints with a temporary database built from revision `0001`.

| Exit code | Meaning | What to do |
|-----------|---------|------------|
| `0` | Schema matches the baseline. | Continue to step 3. |
| `1` | Schema differs; every difference is summarised (see below). | **Stop.** Do not stamp. Investigate each difference (for example, a manual edit or an older/newer schema) and resolve it first. |
| `2` | Not applicable: the file is missing, has no tables, or is already managed by Alembic (recognised revisions are shown). | For a missing or empty database use section 2; for a managed one use section 4. |
| `3` | The database could not be read, or the stamp failed (a short diagnostic such as "the database is locked" is shown). | Fix the underlying problem (lock, permissions, a damaged file) and re-run. Nothing was stamped. |

The output never contains text read from the database itself, which could
include sensitive data or terminal control characters. Differences name only
this project's own tables and columns ("missing column", "column definition
differs", "check constraints differ"); objects that exist only in the
inspected database are counted ("1 unexpected table (names not shown)");
recorded revisions are shown only when they are one of this project's
revision IDs. Inspect the database directly (for example with the `sqlite3`
shell) if you need the full details.

### Step 3: Stamp the baseline revision

Stamping only writes the revision number; it does not alter any table.

```cmd
python -m app.db.check_baseline --stamp
```

`--stamp` re-runs the verification and stamps **only** if the schema matches
exactly. (Equivalent manual command after a successful check:
`alembic stamp 0001`.)

### Step 4: Apply the remaining migrations

```cmd
alembic upgrade head
```

For the current history this runs `0002` (adding `users.token_version` with
value `0` for every existing user) and then `0003` (ticket assignment,
versioning and history; see [section 8](#8-upgrading-an-existing-database-to-0003)).
No existing value is changed.

### Rehearsing on a copy (optional)

Any Alembic command can target another SQLite file with `-x db_url=...`, and
the checker accepts `--database`:

```cmd
copy resolveai.db rehearsal.db
python -m app.db.check_baseline --database rehearsal.db --stamp
alembic -x db_url=sqlite:///rehearsal.db upgrade head
```

## 4. Applying future migrations

After pulling changes that add migrations:

```cmd
alembic upgrade head
```

Migrations run inside a transaction; if one fails, its changes are rolled back.

## 5. Inspecting the migration state

```cmd
alembic current            # revision the database is at
alembic history --verbose  # all revisions
alembic heads              # latest revision(s) in the code
alembic check              # fails if the models have changes with no migration
```

To preview the SQL of a migration without running it:

```cmd
alembic upgrade 0001:0002 --sql
```

> **Warning: offline SQL skips the safety checks.** `--sql` mode never
> connects to a database, so the baseline migration cannot check whether
> tables already exist. SQL generated for `0001` (for example by
> `alembic upgrade head --sql`) is for **brand-new, empty databases only**.
> Never apply it to an existing pre-Alembic database: verify and stamp that
> database instead (section 3).

## 6. Creating a new migration

1. Change the SQLAlchemy models in `app/models/`. A new model module must be
   imported in `app/models/__init__.py` so Alembic can see it.
2. Generate a migration against a database that is at `head`:

   ```cmd
   alembic upgrade head
   alembic revision --autogenerate -m "describe the change"
   ```

3. **Review the generated file** in `alembic/versions/`. Autogenerate does not
   detect everything (for example, renames are detected as drop + add). On
   SQLite, column changes require batch mode
   (`with op.batch_alter_table(...)`), which the environment enables.
4. Give new columns on existing tables a `server_default` or make them
   nullable so existing rows remain valid.
5. Apply it and run the tests:

   ```cmd
   alembic upgrade head
   python -m pytest -v tests/test_migrations.py
   ```

`tests/test_migrations.py` fails if the models and migrations drift apart.

## 7. Provisioning users requires a migrated database

The provisioning scripts, `python -m app.scripts.create_admin` and
`python -m app.scripts.create_agent`, never run migrations. Before asking for
any input (including the password) each checks, read-only, that:

1. the `alembic_version` table records exactly the head revision, once; and
2. the schema (tables, columns, types, nullability, defaults, primary and
   foreign keys, indexes, unique and check constraints) matches the schema
   the project's migrations produce at head. The reference is built in a
   temporary database from `alembic/versions`, never from `resolveai.db`.

Otherwise it stops with exit code `1` and explains what to do, without
changing the database:

| Database state | What to do |
|----------------|------------|
| File missing or unreadable | Create it: `alembic upgrade head` (section 2). |
| No `alembic_version` (pre-Alembic) | Back up, verify and stamp, then upgrade (section 3). |
| One earlier, known revision (pending migrations) | Back up, then `alembic upgrade head` (section 4). |
| Unknown revision (e.g. migrated by newer code) | Use code that matches the database. |
| Corrupt or inconsistent records: empty `alembic_version`, a duplicated revision, or several revisions in this single-head history | **Do not run `alembic upgrade head`.** See below. |
| Correct revision, but the schema differs (missing or extra tables, columns, indexes or constraints, or not a ResolveAI database) | **Do not run `alembic upgrade head`.** See below. |

Run the migration step yourself, then re-run the script.

### Inconsistent records or schema mismatch

These states mean the database was changed outside the migrations (or the
file is not a ResolveAI database at all), so its real schema is unknown and
running migrations could damage it:

1. Stop, and keep a backup copy of the file.
2. Restore the database from a known-good backup if you have one.
3. Otherwise, compare its schema with the reported differences and repair it
   manually. Only when the schema has been confirmed to match a revision
   should the records be reset (for example
   `alembic stamp --purge <revision>`); the check then re-verifies the
   schema before any user can be provisioned.

### "Alembic could not resolve a recorded database revision"

Alembic commands (`upgrade`, `downgrade`, `current`, `stamp`, `check`, ...)
stop with this message when `alembic_version` records a revision this code
does not know. The stored value is deliberately not shown: it is database
content and could contain secrets or terminal control characters. Nothing is
changed. Treat it like the "Unknown revision" row above and use code that
matches the database. Reset the records with
`alembic stamp --purge <revision>` only after confirming the schema matches
that revision (see "Inconsistent records or schema mismatch").

## 8. Upgrading an existing database to 0003

Revision `0003` adds ticket assignment, ticket versioning and the
`ticket_events` history table. Application code from Phase 7 onward
requires it: the API, the provisioning scripts and the frontend's
assignment, claim and history features all expect the `0003` schema, and
the scripts refuse to run against a database that is not at head
(section 7).

> **Status of the local database.** The repository's local `resolveai.db`
> has **not** been upgraded: it is still at `0002`. Upgrading it is a
> separate, deliberate operation that follows the procedure below.

### What the upgrade changes

| Object | Change |
|---|---|
| `tickets.assigned_to_user_id` | New, nullable. Foreign key `fk_tickets_assigned_to_user_id_users` to `users.id`; index `ix_tickets_assigned_to_user_id`. |
| `tickets.assigned_at` | New, nullable timestamp of the current assignment. |
| `tickets.version` | New, `NOT NULL`, server default `1`. |
| `ticket_events` | New table: `id`, `ticket_id`, `event_type`, `actor_user_id`, `from_assignee_id`, `to_assignee_id`, `from_status`, `to_status`, `reason`, `created_at`. CHECK constraints allow only the known event types, statuses and unassignment reasons. `ticket_id` references `tickets.id` with `ON DELETE CASCADE`; the three user columns reference `users.id`. Index `ix_ticket_events_ticket_id_created_at_id`. |
| `users`, `revoked_tokens` | Not changed. |

### Existing ticket preservation

- Every existing column value of every ticket is kept unchanged, including
  `id`, `created_by_user_id`, both timestamps, the AI prediction and the
  final category.
- Every existing ticket starts **unassigned** (`assigned_to_user_id` and
  `assigned_at` are `NULL`) at `version` `1`.
- **No history is generated** for existing tickets: their history starts
  with the first change made after the upgrade. The history endpoint
  returns an empty list for them, and the frontend shows "No activity
  history available yet."
- Statuses are kept exactly. A ticket that was "In Progress" before the
  upgrade stays "In Progress" with no assignee (a *legacy* state the
  workflow cannot create). An agent can claim it, or an administrator can
  assign it; both are permitted for unassigned Open and In Progress
  tickets.

### SQLite table rebuild strategy

SQLite cannot add a foreign key with `ALTER TABLE`, so `tickets` is rebuilt
in Alembic batch mode: a new table is created, every row is copied, the old
table is dropped and the new one renamed. To make the rebuild exact:

- The new table is built from an **explicit definition** of `tickets`
  (`copy_from`), not from reflection, so every column, both CHECK
  constraints (`ck_tickets_priority`, `ck_tickets_status`), the
  `created_by_user_id` foreign key and its index are recreated exactly.
- **Before** rebuilding, the live `tickets` columns are compared with that
  definition. If they differ (a missing column, or an extra column that the
  copy would silently drop), the migration stops with
  `Migration 0003 stopped: the tickets table does not have the expected
  columns ...` and **nothing is changed**. Investigate the schema
  (section 7, "Inconsistent records or schema mismatch") before retrying.
- The whole migration runs in **one transaction** (the migration engine
  makes SQLite's DDL transactional). If any step fails, everything is
  rolled back and the database remains at `0002`.
- Offline SQL (`--sql`) cannot perform the column check. Never apply
  generated `0003` SQL to a database; run the migration online.

### Foreign key requirements

- The application enables SQLite foreign-key enforcement on every
  connection (`PRAGMA foreign_keys=ON`). Migrations deliberately run with
  SQLite's default (enforcement **off**), so the table rebuild cannot
  trigger foreign-key actions. The migration is also ordered to be valid
  with enforcement on: `tickets` is rebuilt **before** `ticket_events`,
  which references it, exists; on downgrade `ticket_events` is dropped
  **before** `tickets` is rebuilt. The test suite runs `0003` both ways.
- Existing data must already satisfy the foreign keys: every
  `tickets.created_by_user_id` must reference an existing user. Check
  before upgrading (step 2 below); `PRAGMA foreign_key_check` must return
  no rows.
- `ticket_events.ticket_id` cascades: deleting a ticket deletes its
  history. The event user columns do not cascade; users are deactivated,
  never deleted, so history keeps valid references.

### Procedure

Run from the project root with the virtual environment activated.

**1. Stop the application.** No API process or script may use the database
while it is copied or migrated.

**2. Check the current state (read-only).**

```cmd
alembic current
python -c "import sqlite3; c = sqlite3.connect('file:resolveai.db?mode=ro', uri=True); print('integrity:', c.execute('PRAGMA integrity_check').fetchone()[0]); print('foreign key problems:', c.execute('PRAGMA foreign_key_check').fetchall()); print('users:', c.execute('SELECT COUNT(*) FROM users').fetchone()[0], 'tickets:', c.execute('SELECT COUNT(*) FROM tickets').fetchone()[0])"
```

Expected: `0002`, `integrity: ok`, `foreign key problems: []`. Note the user
and ticket counts. Do not continue on any other result.

**3. Back up the database.** Keep the copy until the upgraded application
has been verified in use. Use a new name, and never overwrite an earlier
backup:

```cmd
copy resolveai.db resolveai.db.pre-0003-YYYYMMDD.bak
certutil -hashfile resolveai.db SHA256
certutil -hashfile resolveai.db.pre-0003-YYYYMMDD.bak SHA256
```

The two hashes must be identical.

**4. Rehearse on a copy** (recommended). Every Alembic command can target
another file with `-x db_url=...`:

```cmd
copy resolveai.db rehearsal-0003.db
alembic -x db_url=sqlite:///rehearsal-0003.db upgrade head
alembic -x db_url=sqlite:///rehearsal-0003.db current
alembic -x db_url=sqlite:///rehearsal-0003.db check
alembic -x db_url=sqlite:///rehearsal-0003.db downgrade -1
alembic -x db_url=sqlite:///rehearsal-0003.db upgrade head
del rehearsal-0003.db
```

Each command must succeed; `current` reports `0003 (head)` and `check`
reports `No new upgrade operations detected.`

**5. Upgrade.**

```cmd
alembic upgrade head
```

The output ends with `Running upgrade 0002 -> 0003, Add ticket assignment
columns and the ticket_events history table.`

**6. Verify.**

```cmd
alembic current
alembic check
python -c "import sqlite3; c = sqlite3.connect('file:resolveai.db?mode=ro', uri=True); print('integrity:', c.execute('PRAGMA integrity_check').fetchone()[0]); print('foreign key problems:', c.execute('PRAGMA foreign_key_check').fetchall()); print('users:', c.execute('SELECT COUNT(*) FROM users').fetchone()[0], 'tickets:', c.execute('SELECT COUNT(*) FROM tickets').fetchone()[0], 'events:', c.execute('SELECT COUNT(*) FROM ticket_events').fetchone()[0])"
```

Expected: `0003 (head)`; `No new upgrade operations detected.`;
`integrity: ok`; `foreign key problems: []`; the same user and ticket counts
as in step 2; `events: 0`. Then start the application, sign in, and open the
tickets page: existing tickets appear as unassigned.

**7. If there is no administrator yet**, create one
(`python -m app.scripts.create_admin`, see the README). Without an active
administrator, tickets can be claimed by agents but not assigned.

### Recovery precautions

- **A failed upgrade changes nothing.** The migration is transactional: the
  database remains at `0002` and the error is reported with fixed text
  (no SQL or data). Fix the cause and re-run.
- **To abandon an upgrade that succeeded**, prefer restoring the backup
  over downgrading: stop the application, then
  `copy resolveai.db.pre-0003-YYYYMMDD.bak resolveai.db` and check the hash
  and `alembic current` (`0002`). Restoring loses every change made after
  the backup, so decide before the system is in use.
- **Downgrading** (`alembic downgrade -1`, see below) keeps tickets but
  permanently discards assignments and history. Back up first.
- Never edit `alembic_version` by hand to "skip" `0003`; the provisioning
  check compares the real schema with the recorded revision and refuses a
  mismatch.

## Downgrading

```cmd
alembic downgrade -1
```

Back up the database before downgrading.

Notes for `0003`:

- Every ticket keeps its rows and its `0002` columns (including any status,
  priority, category or text changes made after the upgrade), and the
  `tickets` table returns to exactly the `0002` schema.
- **Permanently discarded:** every assignment (`assigned_to_user_id`,
  `assigned_at`), every `version` value, and the whole `ticket_events`
  history. Upgrading again starts every ticket unassigned at version `1`
  with no history.
- The migration refuses to downgrade, changing nothing, if `tickets` does
  not have exactly the `0003` columns.
- Application code from Phase 7 onward requires `0003`; do not run it
  against a downgraded database.

Notes for `0002`:

- Downgrading removes `users.token_version` and the stored values. The
  application code at `head` requires this column, so do not run the API
  against a downgraded database.
- Upgrading again resets every user to version `0`. Tokens that had been
  invalidated earlier, but were issued at version `0` and have not yet
  expired, would become valid again. Wait for the access-token lifetime
  (`ACCESS_TOKEN_EXPIRE_MINUTES`) to pass, or rotate `SECRET_KEY`, if that
  matters.
