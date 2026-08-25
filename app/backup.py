import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet


def create_snapshot(db_path: str, snapshot_dir: str, at: datetime | None = None) -> Path:
    Path(snapshot_dir).mkdir(parents=True, exist_ok=True)
    date_str = (at or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    snapshot_path = Path(snapshot_dir) / f"backup-{date_str}.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM INTO ?", (str(snapshot_path),))
    finally:
        conn.close()
    return snapshot_path


def encrypt_file(path: Path, key: str) -> Path:
    fernet = Fernet(key.encode())
    encrypted_path = path.with_suffix(path.suffix + ".enc")
    encrypted_path.write_bytes(fernet.encrypt(path.read_bytes()))
    return encrypted_path


def decrypt_file(encrypted_path: Path, key: str, output_path: Path) -> Path:
    fernet = Fernet(key.encode())
    output_path.write_bytes(fernet.decrypt(encrypted_path.read_bytes()))
    return output_path


def prune_snapshots(snapshot_dir: str, keep: int = 7) -> list[Path]:
    snapshots = sorted(Path(snapshot_dir).glob("backup-*.db"), key=lambda p: p.name, reverse=True)
    to_delete = snapshots[keep:]
    for path in to_delete:
        path.unlink()
    return to_delete


def newest_snapshot(snapshot_dir: str) -> tuple[Path, date] | None:
    best: tuple[Path, date] | None = None
    for path in Path(snapshot_dir).glob("backup-*.db"):
        try:
            d = date.fromisoformat(path.stem.removeprefix("backup-"))
        except ValueError:
            continue
        if best is None or d > best[1]:
            best = (path, d)
    return best


def backup_age_days(snapshot_date: date, today: date) -> int:
    return (today - snapshot_date).days


def is_backup_stale(
    snapshot_dir: str,
    today: date | None = None,
    tz_name: str = "Europe/Berlin",
) -> bool:
    if today is None:
        today = datetime.now(ZoneInfo(tz_name)).date()
    newest = newest_snapshot(snapshot_dir)
    if newest is None:
        return True
    return backup_age_days(newest[1], today) > 2


def stale_backup_message(
    snapshot_dir: str,
    today: date | None = None,
    tz_name: str = "Europe/Berlin",
) -> str | None:
    if today is None:
        today = datetime.now(ZoneInfo(tz_name)).date()
    newest = newest_snapshot(snapshot_dir)
    if newest is None:
        return "Backup overdue: no local snapshots found."
    _, snapshot_date = newest
    age = backup_age_days(snapshot_date, today)
    if age <= 2:
        return None
    return (
        f"Backup overdue: newest local snapshot is {snapshot_date.isoformat()} "
        f"({age} days old)."
    )


def backup_status_text(
    snapshot_dir: str,
    *,
    channel_configured: bool,
    key_configured: bool,
    today: date | None = None,
    tz_name: str = "Europe/Berlin",
) -> str:
    if today is None:
        today = datetime.now(ZoneInfo(tz_name)).date()
    newest = newest_snapshot(snapshot_dir)
    channel = "yes" if channel_configured else "no"
    key = "yes" if key_configured else "no"
    if newest is None:
        return (
            "Backup status: overdue\n"
            "Newest snapshot: none\n"
            f"Channel configured: {channel}\n"
            f"Encryption key configured: {key}"
        )
    _, snapshot_date = newest
    age = backup_age_days(snapshot_date, today)
    state = "OK" if age <= 2 else "overdue"
    day_label = "day" if age == 1 else "days"
    return (
        f"Backup status: {state}\n"
        f"Newest snapshot: {snapshot_date.isoformat()} ({age} {day_label} old)\n"
        f"Channel configured: {channel}\n"
        f"Encryption key configured: {key}"
    )
