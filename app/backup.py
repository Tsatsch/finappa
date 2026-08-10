import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet


def create_snapshot(db_path: str, snapshot_dir: str, at: datetime | None = None) -> Path:
    Path(snapshot_dir).mkdir(parents=True, exist_ok=True)
    date_str = (at or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    snapshot_path = Path(snapshot_dir) / f"backup-{date_str}.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"VACUUM INTO '{snapshot_path}'")
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
