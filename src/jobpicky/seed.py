from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


class SeedDatabaseError(RuntimeError):
    pass


def find_givemeoc_seed_database() -> Path:
    """Locate the bundled GiveMeOC snapshot used to bootstrap the local cache."""
    frozen_root = Path(getattr(__import__("sys"), "_MEIPASS", ""))
    candidates = (
        Path(__file__).resolve().parent / "resources" / "givemeoc_seed.sqlite",
        frozen_root / "data" / "givemeoc_seed.sqlite",
        Path.cwd() / "data" / "givemeoc_seed.sqlite",
    )
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    raise SeedDatabaseError("找不到 givemeoc_seed.sqlite")


def _merge_givemeoc_seed(target: Path) -> None:
    try:
        source = find_givemeoc_seed_database()
    except SeedDatabaseError:
        return
    if source.resolve() == target.resolve():
        return
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("ATTACH DATABASE ? AS givemeoc_seed", (str(source),))
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS givemeoc_records (
                source_record_id TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                company_normalized TEXT,
                recruitment_type TEXT,
                target_graduate_year TEXT,
                city TEXT,
                deadline TEXT,
                updated_at TEXT,
                announcement_url TEXT,
                official_url TEXT,
                last_seen_at DATETIME NOT NULL,
                last_seen_page INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_givemeoc_records_company
                ON givemeoc_records(company_normalized);
            CREATE TABLE IF NOT EXISTS givemeoc_scan_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cache_initialized INTEGER NOT NULL DEFAULT 0,
                total_pages INTEGER NOT NULL DEFAULT 0,
                pages_scanned INTEGER NOT NULL DEFAULT 0,
                last_full_scan_at DATETIME,
                last_incremental_scan_at DATETIME,
                updated_at DATETIME NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO givemeoc_records
            SELECT source_record_id, company, company_normalized,
                   recruitment_type, target_graduate_year, city, deadline,
                   updated_at, announcement_url, official_url,
                   last_seen_at, last_seen_page
            FROM givemeoc_seed.givemeoc_records
            """
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO givemeoc_scan_state
            SELECT id, cache_initialized, total_pages, pages_scanned,
                   last_full_scan_at, last_incremental_scan_at, updated_at
            FROM givemeoc_seed.givemeoc_scan_state
            """
        )
        connection.commit()
        connection.execute("DETACH DATABASE givemeoc_seed")


def find_seed_database() -> Path:
    """Locate the bundled job baseline from an installed package or checkout.

    The packaged resource is the canonical source.  Working-directory and
    frozen-app fallbacks remain temporarily for legacy installs and migration.
    """
    frozen_root = Path(getattr(__import__("sys"), "_MEIPASS", ""))
    candidates = (
        Path(__file__).resolve().parent / "resources" / "jobs_seed.sqlite",
        frozen_root / "data" / "jobs_seed.sqlite",
        Path.cwd() / "data" / "jobs_seed.sqlite",
        Path(__file__).resolve().parents[2] / "data" / "jobs_seed.sqlite",
    )
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    raise SeedDatabaseError("找不到 data/jobs_seed.sqlite；请重新获取完整项目文件后重试")


def restore_seed_database(target: str | Path, *, overwrite: bool = False) -> bool:
    """Atomically create or replace a runtime database from the shipped seed.

    Returns True when a copy was made and False when an existing target was kept.
    """
    destination = Path(target)
    if destination.exists() and not overwrite:
        return False

    source = find_seed_database()
    if source.resolve() == destination.resolve():
        raise SeedDatabaseError("seed 数据库不能作为运行数据库直接使用")
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        if temporary.stat().st_size != source.stat().st_size:
            raise SeedDatabaseError("seed 数据库复制不完整")
        os.replace(temporary, destination)
        _merge_givemeoc_seed(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return True
