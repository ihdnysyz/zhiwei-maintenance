"""Database snapshots and retention. Run from an authorized operating account."""
import argparse
import os
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.engine import make_url

from .db import DB, Job, Record, cfg, records, timestamp


def postgres_command(program, url, options):
    command = [program, "--host", url.host or "localhost", "--port", str(url.port or 5432),
               "--username", url.username or "", "--dbname", url.database or "", "--no-password", *options]
    environment = {**os.environ, "PGPASSWORD": url.password or ""}
    subprocess.run(command, env=environment, check=True)


def copy_sqlite(path, target):
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(target)) as destination:
        source.backup(destination)
        if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite 快照校验失败")


def backup(destination):
    url = make_url(cfg.database_url)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists():
        raise FileExistsError(partial)
    if url.get_backend_name() == "sqlite":
        database = Path(url.database)
        if not database.is_file():
            raise FileNotFoundError(database)
        copy_sqlite(database, partial)
    else:
        postgres_command("pg_dump", url, ["--format=custom", "--file", str(partial)])
        subprocess.run(["pg_restore", "--list", str(partial)], stdout=subprocess.DEVNULL, check=True)
    partial.replace(destination)


def restore(source, database_url):
    if not source.is_file():
        raise FileNotFoundError(source)
    target = make_url(database_url)
    if target.get_backend_name() == "sqlite":
        destination = Path(target.database)
        if destination.exists():
            raise FileExistsError("请指定未使用的目标数据库")
        destination.parent.mkdir(parents=True, exist_ok=True)
        copy_sqlite(source, destination)
        with closing(sqlite3.connect(destination)) as connection:
            connection.execute("DELETE FROM records WHERE kind = 'auth'")
            connection.commit()
    else:
        # No clean/overwrite flags. A non-empty restore target fails atomically.
        postgres_command("pg_restore", target, ["--exit-on-error", "--single-transaction", str(source)])
        postgres_command("psql", target, ["--set=ON_ERROR_STOP=1", "--command", "DELETE FROM records WHERE kind = 'auth'"])


def retention(org):
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, cfg.retention_days))
    with DB.begin() as db:
        sessions = db.scalars(records(org, "auth"))
        expired = [row.id for row in sessions if row.data["expiry"] <= timestamp()]
        if expired:
            db.execute(delete(Record).where(Record.org == org, Record.kind == "auth", Record.id.in_(expired)))
        count = db.execute(delete(Job).where(Job.org == org, Job.state.in_({"done", "failed"}),
                                            Job.available < cutoff.timestamp())).rowcount
    print(f"已清理 {len(expired)} 条过期登录记录和 {count} 条到期后台任务")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("backup").add_argument("file", type=Path)
    restore_command = commands.add_parser("restore")
    restore_command.add_argument("file", type=Path)
    commands.add_parser("retention").add_argument("org")
    args = parser.parse_args()
    if args.action == "backup":
        backup(args.file)
        print(args.file)
    elif args.action == "restore":
        target = os.environ.get("RESTORE_DATABASE_URL")
        if not target or target == cfg.database_url:
            parser.error("需通过 RESTORE_DATABASE_URL 指定独立的空恢复数据库")
        restore(args.file, target)
        print("恢复完成，旧登录会话已失效")
    else:
        retention(args.org)


if __name__ == "__main__":
    main()

