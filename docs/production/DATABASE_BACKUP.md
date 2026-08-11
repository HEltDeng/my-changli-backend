# Production Database Backup

## Executable baseline

The daily job must run with the repository-external Production environment loaded:

```bash
/opt/mychangli/backend/.venv/bin/python \
  /opt/mychangli/backend/scripts/backup_database.py --retention-days 14
```

The command uses SQLite's online backup API, writes a new timestamped file without
overwriting prior backups, runs `PRAGMA integrity_check`, emits a SHA-256 manifest,
and exits non-zero on failure. Its JSON output contains only status, filename, size,
digest, integrity, and retention results.

A verified backup is the database plus its adjacent manifest. Verification fails if
the manifest is missing or invalid, its format is unsupported, or its filename,
byte size, SHA-256 or integrity declaration differs from the actual database. A raw
SQLite file may be inspected with the separate `integrity_check()` helper, but it is
not a Production-restorable backup.

Verify a selected copy independently:

```bash
/opt/mychangli/backend/.venv/bin/python \
  /opt/mychangli/backend/scripts/verify_backup.py \
  /var/backups/mychangli/my-changli-<UTC_TIMESTAMP>.db
```

## Retention and scheduling

- Schedule one successful run per day with a systemd timer or cron.
- The default local retention is 14 days; the operator may increase it according
  to disk budget and recovery objectives.
- Alert on a non-zero exit, failed manifest/integrity validation, or no successful
  backup for 26 hours.
- Upload files are not inside SQLite and require a separate versioned backup job.

## Encryption and off-site boundary

The executable baseline creates a local `0600` SQLite file; it does not claim
application-level backup encryption. Before real user data is accepted, place the
backup directory on encrypted storage. During the later cloud module, add an
encrypted off-site copy with credentials and encryption keys held outside Git and
outside the backup storage domain. No real database is uploaded by this module.

Run the restore drill in `DATABASE_RESTORE.md` after the first scheduled backup and
at least quarterly. A backup without a tested restore is not sufficient evidence.
