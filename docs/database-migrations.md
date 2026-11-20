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

For the current history this runs `0002`, adding `users.token_version` with
value `0` for every existing user. No other data is changed.

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

## Downgrading

```cmd
alembic downgrade -1
```

Back up the database before downgrading. Notes for `0002`:

- Downgrading removes `users.token_version` and the stored values. The
  application code at `head` requires this column, so do not run the API
  against a downgraded database.
- Upgrading again resets every user to version `0`. Tokens that had been
  invalidated earlier, but were issued at version `0` and have not yet
  expired, would become valid again. Wait for the access-token lifetime
  (`ACCESS_TOKEN_EXPIRE_MINUTES`) to pass, or rotate `SECRET_KEY`, if that
  matters.
