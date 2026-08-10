import sqlite3
from datetime import datetime, timezone

from cryptography.fernet import Fernet

from app.backup import create_snapshot, decrypt_file, encrypt_file, prune_snapshots
from app.db import init_db, make_engine


def test_create_snapshot_produces_readable_database(tmp_path):
    db_path = tmp_path / "finappa.db"
    engine = make_engine(str(db_path))
    init_db(engine)

    snapshot_dir = tmp_path / "snapshots"
    at = datetime(2026, 3, 15, tzinfo=timezone.utc)
    snapshot_path = create_snapshot(str(db_path), str(snapshot_dir), at=at)

    assert snapshot_path.name == "backup-2026-03-15.db"
    conn = sqlite3.connect(str(snapshot_path))
    count = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    conn.close()
    assert count == 14


def test_encrypt_decrypt_round_trip(tmp_path):
    db_path = tmp_path / "finappa.db"
    engine = make_engine(str(db_path))
    init_db(engine)
    snapshot_path = create_snapshot(str(db_path), str(tmp_path / "snapshots"))

    key = Fernet.generate_key().decode()
    encrypted_path = encrypt_file(snapshot_path, key)
    assert encrypted_path.exists()

    output_path = tmp_path / "restored.db"
    decrypt_file(encrypted_path, key, output_path)

    assert output_path.read_bytes() == snapshot_path.read_bytes()
    conn = sqlite3.connect(str(output_path))
    count = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    conn.close()
    assert count == 14


def test_prune_snapshots_keeps_newest_n(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    for day in range(1, 11):  # 10 fake snapshots
        (snapshot_dir / f"backup-2026-03-{day:02d}.db").write_text("fake")

    deleted = prune_snapshots(str(snapshot_dir), keep=7)

    remaining = sorted(p.name for p in snapshot_dir.glob("backup-*.db"))
    assert len(remaining) == 7
    assert len(deleted) == 3
    assert remaining == [f"backup-2026-03-{d:02d}.db" for d in range(4, 11)]
