"""V12 §31: SQLite 备份脚本(integrity_check + 在线备份 + 保留清理)。

用临时 SQLite 库验证 `scripts/db_backup.py` 的核心函数, 无网络。
"""

import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import db_backup  # noqa: E402


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    conn.close()


class TestIntegrity:
    def test_ok_on_healthy_db(self, tmp_path):
        db = tmp_path / "a.db"
        _make_db(db)
        assert db_backup.integrity_ok(str(db)) is True

    def test_ok_on_empty_db(self, tmp_path):
        db = tmp_path / "empty.db"
        sqlite3.connect(str(db)).close()  # 仅创建空文件
        assert db_backup.integrity_ok(str(db)) is True


class TestBackup:
    def test_backup_copies_content(self, tmp_path):
        db = tmp_path / "a.db"
        _make_db(db)
        out = db_backup.backup(str(db), str(tmp_path / "bak"))
        assert Path(out).exists()
        conn = sqlite3.connect(out)
        assert conn.execute("SELECT v FROM t").fetchone() == ("hello",)
        conn.close()

    def test_backup_keeps_limit(self, tmp_path):
        db = tmp_path / "a.db"
        _make_db(db)
        bak_dir = tmp_path / "bak"
        for _ in range(3):
            db_backup.backup(str(db), str(bak_dir), keep=2)
        backups = sorted(bak_dir.glob("adaptive-*.db"))
        assert len(backups) == 2


class TestMain:
    def test_check_only_ok(self, tmp_path, capsys):
        db = tmp_path / "a.db"
        _make_db(db)
        assert db_backup.main(["--db", str(db), "--check-only"]) == 0
        assert "integrity_check OK" in capsys.readouterr().out

    def test_missing_db(self, tmp_path):
        assert db_backup.main(["--db", str(tmp_path / "nope.db")]) == 1

    def test_backup_mode_ok(self, tmp_path, capsys):
        db = tmp_path / "a.db"
        _make_db(db)
        bak = tmp_path / "bak"
        rc = db_backup.main(["--db", str(db), "--backup-dir", str(bak), "--keep", "1"])
        assert rc == 0
        assert "backup OK" in capsys.readouterr().out
        assert len(list(bak.glob("adaptive-*.db"))) == 1
