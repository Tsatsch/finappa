import sqlite3
from datetime import date, datetime, timezone

from cryptography.fernet import Fernet

from app.backup import (
    backup_status_text,
    create_snapshot,
    decrypt_file,
    encrypt_file,
    is_backup_stale,
    prune_snapshots,
    stale_backup_message,
)
from app.categories import CategoryName
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
    assert count == len(CategoryName)


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
    assert count == len(CategoryName)


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


def test_is_backup_stale_false_when_age_is_two_days(tmp_path):
    (tmp_path / "backup-2026-08-23.db").write_text("x")
    assert is_backup_stale(str(tmp_path), today=date(2026, 8, 25)) is False


def test_is_backup_stale_true_when_age_is_three_days(tmp_path):
    (tmp_path / "backup-2026-08-22.db").write_text("x")
    assert is_backup_stale(str(tmp_path), today=date(2026, 8, 25)) is True


def test_is_backup_stale_true_when_no_snapshots(tmp_path):
    assert is_backup_stale(str(tmp_path), today=date(2026, 8, 25)) is True


def test_stale_backup_message_none_when_fresh(tmp_path):
    (tmp_path / "backup-2026-08-24.db").write_text("x")
    assert stale_backup_message(str(tmp_path), today=date(2026, 8, 25)) is None


def test_stale_backup_message_when_missing(tmp_path):
    msg = stale_backup_message(str(tmp_path), today=date(2026, 8, 25))
    assert msg == "Backup overdue: no local snapshots found."


def test_backup_status_text_reports_ok_and_flags(tmp_path):
    (tmp_path / "backup-2026-08-24.db").write_text("x")
    text = backup_status_text(
        str(tmp_path),
        channel_configured=True,
        key_configured=True,
        today=date(2026, 8, 25),
    )
    assert "Backup status: OK" in text
    assert "2026-08-24" in text
    assert "1 day old" in text
    assert "Channel configured: yes" in text
    assert "Encryption key configured: yes" in text
