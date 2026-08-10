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
   file to the host (or to your machine, then `scp` it over).
2. Get `BACKUP_ENCRYPTION_KEY` from the password manager — it is **not** recoverable
   from anywhere else. Without it, this specific backup is permanently unreadable
   (spec §13.2); fall back to an older local snapshot in `./data/` if one exists.
3. Decrypt it:
   ```bash
   python -c "
   from pathlib import Path
   from app.backup import decrypt_file
   decrypt_file(Path('backup-2026-08-10.db.enc'), '<BACKUP_ENCRYPTION_KEY>', Path('restored.db'))
   "
   ```
4. Verify it actually opens before trusting it:
   ```bash
   sqlite3 restored.db "PRAGMA integrity_check;"
   sqlite3 restored.db "SELECT COUNT(*) FROM transactions;"
   ```
   `PRAGMA integrity_check` must print `ok`. If it doesn't, try an older snapshot.
5. Stop the bot, swap the file in, restart:
   ```bash
   docker compose down
   cp restored.db ./data/finappa.db
   docker compose up -d
   ```
6. Verify from Telegram: `/summary` should reflect the restored data's month.
