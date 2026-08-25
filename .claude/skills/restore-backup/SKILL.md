---
name: restore-backup
description: Restore finappa's database from an encrypted backup sent to the Telegram backup channel. Use when the host is lost, the local database is corrupted, or the user wants to roll back to a specific day.
---

# Restore from backup

Backups are daily, encrypted, and live in the private Telegram backup channel
(spec §8.1). This is the full path from "found a backup message" to "bot running
on it again."

1. In Telegram, open the backup channel and find the snapshot to restore — each
   message's caption has the date. Download the attached `backup-YYYY-MM-DD.db.enc`
   file into `./data/` on the host (that directory is bind-mounted into the
   container, so both sides can see it — if you downloaded it elsewhere, `scp`
   or `mv` it into `./data/` now).
2. Get `BACKUP_ENCRYPTION_KEY` from the password manager — it is **not** recoverable
   from anywhere else. Without it, this specific backup is permanently unreadable
   (spec §13.2); fall back to an older local snapshot in `./data/` if one exists.
3. Decrypt it. Run this inside the container, not on the host — the `cryptography`
   package lives in the container's venv, not necessarily on the host. Export the
   key into an environment variable first rather than typing it into the inline
   script, so it doesn't land in your shell history or show up in `ps` output
   (clear your shell history afterward if your shell logs exported values):
   ```bash
   export BACKUP_KEY='<BACKUP_ENCRYPTION_KEY>'
   docker compose run --rm -e BACKUP_KEY bot python -c "
   import os
   from pathlib import Path
   from app.backup import decrypt_file
   decrypt_file(Path('data/backup-2026-08-10.db.enc'), os.environ['BACKUP_KEY'], Path('data/restored.db'))
   "
   unset BACKUP_KEY
   ```
4. Verify it actually opens before trusting it:
   ```bash
   sqlite3 ./data/restored.db "PRAGMA integrity_check;"
   sqlite3 ./data/restored.db "SELECT COUNT(*) FROM transactions;"
   ```
   `PRAGMA integrity_check` must print `ok`. If it doesn't, try an older snapshot.
5. Stop the bot, swap the file in, restart. The live database is WAL-mode, so
   `./data/` may also contain `finappa.db-wal` and `finappa.db-shm` — if the
   container was ever killed abruptly (a crashed host is exactly the scenario
   this runbook is for), those files hold stale, un-checkpointed data and SQLite
   will replay them onto the restored database on next open, silently undoing
   the restore. Remove them before starting back up:
   ```bash
   docker compose down
   cp ./data/restored.db ./data/finappa.db
   rm -f ./data/finappa.db-wal ./data/finappa.db-shm
   docker compose up -d
   ```
6. Verify from Telegram: `/summary` should reflect the restored data's month.

## Quarterly verify (without restoring)

Once a quarter, prove a backup is still decryptable **without** swapping the live
DB. Pick a recent `.enc` from the backup channel (or a local `backup-*.db`):

1. Decrypt into a throwaway path (e.g. `data/verify.db`) using the same
   `decrypt_file` steps as above.
2. Run:
   ```bash
   sqlite3 ./data/verify.db "PRAGMA integrity_check;"
   sqlite3 ./data/verify.db "SELECT COUNT(*) FROM transactions;"
   ```
3. Confirm `integrity_check` prints `ok` and the row count looks sane.
4. Delete `data/verify.db` — do **not** copy it over `finappa.db` unless you are
   actually restoring.

Day-to-day health: `/backup_status` in Telegram shows the newest local snapshot
age and whether channel/key are configured.

