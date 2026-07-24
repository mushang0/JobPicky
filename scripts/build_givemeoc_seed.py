"""Build the bundled GiveMeOC cache seed from the packaged job seed."""

from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "src" / "jobpicky" / "resources" / "jobs_seed.sqlite"
DEFAULT_OUTPUT = ROOT / "src" / "jobpicky" / "resources" / "givemeoc_seed.sqlite"


def build_givemeoc_seed(source: Path, output: Path) -> int:
    with sqlite3.connect(source) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT givemeoc_record_id, company, company_normalized, batch,
                   target_graduate_year, city, deadline, last_seen,
                   announcement_url, announcement_url_source,
                   official_url, official_url_source
            FROM jobs
            WHERE givemeoc_record_id IS NOT NULL AND givemeoc_record_id <> ''
            ORDER BY id
            """
        ).fetchall()

    records: dict[str, dict] = {}
    now = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        record_id = str(row["givemeoc_record_id"])
        record = records.setdefault(
            record_id,
            {
                "source_record_id": record_id,
                "company": row["company"] or "",
                "company_normalized": row["company_normalized"] or "",
                "recruitment_type": row["batch"] or "",
                "target_graduate_year": row["target_graduate_year"] or "",
                "city": row["city"] or "",
                "deadline": row["deadline"] or "",
                "updated_at": row["last_seen"] or "",
                "announcement_url": None,
                "official_url": None,
                "last_seen_at": now,
                "last_seen_page": 0,
            },
        )
        if row["announcement_url_source"] == "givemeoc" and row["announcement_url"]:
            record["announcement_url"] = row["announcement_url"]
        if row["official_url_source"] == "givemeoc" and row["official_url"]:
            record["official_url"] = row["official_url"]

    output.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        with sqlite3.connect(temporary) as connection:
            connection.executescript(
                """
                CREATE TABLE givemeoc_records (
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
                CREATE INDEX idx_givemeoc_records_company
                    ON givemeoc_records(company_normalized);
                CREATE TABLE givemeoc_scan_state (
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
            connection.executemany(
                """
                INSERT INTO givemeoc_records (
                    source_record_id, company, company_normalized,
                    recruitment_type, target_graduate_year, city, deadline,
                    updated_at, announcement_url, official_url,
                    last_seen_at, last_seen_page
                ) VALUES (:source_record_id, :company, :company_normalized,
                          :recruitment_type, :target_graduate_year, :city,
                          :deadline, :updated_at, :announcement_url,
                          :official_url, :last_seen_at, :last_seen_page)
                """,
                records.values(),
            )
            connection.execute(
                """
                INSERT INTO givemeoc_scan_state (
                    id, cache_initialized, total_pages, pages_scanned,
                    last_full_scan_at, last_incremental_scan_at, updated_at
                ) VALUES (1, ?, 0, 0, ?, NULL, ?)
                """,
                (int(bool(records)), now if records else None, now),
            )
        connection.close()
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return len(records)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(f"built {args.output} with {build_givemeoc_seed(args.source, args.output)} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
