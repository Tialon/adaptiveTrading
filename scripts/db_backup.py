"""SQLite 备份 + 完整性校验(V12 §31)

主网部署的数据库安全工具: 备份前先 `PRAGMA integrity_check`, 再用标准库 sqlite3 的
在线备份 API(`Connection.backup`)生成带 UTC 时间戳的副本到 `data/backups/`, 并清理
超过保留份数的旧备份。WAL 模式下在线备份安全, 不打断运行中的 aiosqlite 连接。

用法:
    python scripts/db_backup.py --db data/adaptive.db --backup-dir data/backups --keep 30
    python scripts/db_backup.py --db data/adaptive.db --check-only   # 仅校验完整性

退出码: 0 = 成功(完整性 OK + 备份成功); 1 = DB 缺失 / 完整性校验失败 / 备份失败。

设计边界: 仅做「副本 + 完整性」, 不做压缩加密、不做跨机同步(如需异地备份用 rsync 或
对象存储自行叠加); 完整性校验为 `PRAGMA integrity_check`(快校验, 非 `foreign_key_check`
深校验)。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def integrity_ok(db_path: str) -> bool:
    """`PRAGMA integrity_check`, 返回是否 OK(空库/健康库均返回 True)。"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and str(row[0]).lower() == "ok"
    finally:
        conn.close()


def backup(db_path: str, backup_dir: str, keep: int = 30) -> str:
    """在线备份到 `<backup_dir>/adaptive-<UTC时间戳>.db`, 返回备份文件路径。

    `keep > 0` 时保留最新 `keep` 份, 删除更早的备份(按文件名时间戳排序)。
    """
    Path(backup_dir).mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(db_path)
    dst_path = (
        Path(backup_dir)
        / f"adaptive-{datetime.now(tz=timezone.utc):%Y%m%d-%H%M%S-%f}.db"
    )
    dst = sqlite3.connect(str(dst_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    if keep > 0:
        backups = sorted(Path(backup_dir).glob("adaptive-*.db"))
        for old in backups[:-keep]:
            old.unlink(missing_ok=True)
    return str(dst_path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SQLite 备份 + 完整性校验(V12 §31)")
    ap.add_argument("--db", default="data/adaptive.db")
    ap.add_argument("--backup-dir", default="data/backups")
    ap.add_argument("--keep", type=int, default=30, help="保留份数(0 = 不清理)")
    ap.add_argument("--check-only", action="store_true", help="仅校验完整性, 不备份")
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"DB 不存在: {args.db}", file=sys.stderr)
        return 1

    if not integrity_ok(str(db)):
        print(f"integrity_check FAILED: {args.db}", file=sys.stderr)
        return 1

    if args.check_only:
        print(f"integrity_check OK: {args.db}")
        return 0

    out = backup(str(db), args.backup_dir, args.keep)
    print(f"backup OK -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
