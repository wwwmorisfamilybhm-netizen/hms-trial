# Move the local SQLite database to PostgreSQL

The normal application requires `DATABASE_URL` to point to PostgreSQL. The
`hms.sqlite_export_settings` module exists only to read the old SQLite database for
this one-time transfer. Keep the app stopped throughout the transfer so records and
uploaded files remain in sync.

## One-time local transfer

Use a **new, empty** local PostgreSQL database. The `flush` command below removes rows
created by data migrations before importing their values from SQLite. Never run it
against a PostgreSQL database that already contains work you need to keep.

```bash
brew install postgresql@17            # skip if already installed
brew services start postgresql@17
"$(brew --prefix postgresql@17)/bin/createuser" --login --pwprompt hms
"$(brew --prefix postgresql@17)/bin/createdb" --owner=hms hms
cp .env.example .env                  # skip if already configured
# Edit .env: use the password entered for the hms role.
set -a; . ./.env; set +a
umask 077
MIGRATION_DIR="$(mktemp -d /private/tmp/hms-postgres.XXXXXX)"

# Take a consistent SQLite snapshot and back up file attachments.
SQLITE_BACKUP="$MIGRATION_DIR/db.sqlite3" .venv/bin/python - <<'PY'
import os
import sqlite3

source = sqlite3.connect('file:db.sqlite3?mode=ro', uri=True)
target = sqlite3.connect(os.environ['SQLITE_BACKUP'])
source.backup(target)
target.close()
source.close()
PY
cp -R media "$MIGRATION_DIR/"
cp -R private_reports "$MIGRATION_DIR/"

SQLITE_SOURCE_PATH="$MIGRATION_DIR/db.sqlite3" .venv/bin/python manage.py dumpdata \
  --settings=hms.sqlite_export_settings --natural-foreign --natural-primary \
  --exclude contenttypes --exclude auth.permission --exclude sessions \
  --output "$MIGRATION_DIR/hms.json"

.venv/bin/python manage.py migrate
.venv/bin/python manage.py flush --noinput
.venv/bin/python manage.py loaddata "$MIGRATION_DIR/hms.json"
.venv/bin/python manage.py check
```

Compare model counts before using the new database:

```bash
COUNT_COMMAND='from django.apps import apps; from django.contrib.auth import get_user_model; models = [m for m in apps.get_models() if m._meta.app_label in ("core", "patients", "appointments")]; models += [get_user_model()]; print(*sorted(f"{m._meta.label}: {m.objects.count()}" for m in models), sep="\n")'
SQLITE_SOURCE_PATH="$MIGRATION_DIR/db.sqlite3" .venv/bin/python manage.py shell --no-imports \
  --settings=hms.sqlite_export_settings -c "$COUNT_COMMAND" \
  > "$MIGRATION_DIR/sqlite-counts.txt"
.venv/bin/python manage.py shell --no-imports -c "$COUNT_COMMAND" \
  > "$MIGRATION_DIR/postgres-counts.txt"
diff -u "$MIGRATION_DIR/sqlite-counts.txt" "$MIGRATION_DIR/postgres-counts.txt"
```

An empty `diff` means the counts match. Also sign in with an existing staff account,
open the existing patient and appointment, check medicine stock and its photos, and
create a test record to confirm new IDs work.

The export includes staff accounts, patients, appointments, inventory, and other app
records. It excludes generated content types and permissions, which Django recreates,
and old login sessions. `loaddata` checks foreign keys and resets ID sequences.
Database fixtures store file paths, not file contents. Keep `media/` and
`private_reports/` in place locally; copy them with the database when moving to a
different host. Treat the temporary directory as sensitive because it contains patient
data and password hashes. Retain it until record counts, file links, and the main
workflows have been checked; then remove it from your machine securely.

If the import fails, keep the SQLite snapshot and file backups. Fix the cause and
repeat on a **fresh** PostgreSQL database. To inspect the SQLite snapshot without
changing the application database, use `--settings=hms.sqlite_export_settings` and
set `SQLITE_SOURCE_PATH` to the snapshot path.

## Future deployments

Provision a managed PostgreSQL database and supply its connection URL as the
`DATABASE_URL` secret. Do not copy the local `.env` or temporary fixture to the
deployment. Run Django migrations before serving traffic; `release.sh` already does
this. Back up the managed database and persistent uploaded-file directories together.
