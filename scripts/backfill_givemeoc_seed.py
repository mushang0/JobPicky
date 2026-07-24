"""Backfill the seed database with verified GiveMeOC links."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jobpicky.config import load_config
from jobpicky.givemeoc import GiveMeOCCrawler, apply_givemeoc_links
from jobpicky.storage import JobRepository

from build_seed import build_seed
from build_givemeoc_seed import build_givemeoc_seed
from export_seed_source import export_seed_source


def _stats(database: Path) -> tuple[int, int, int, int, int, int]:
    with sqlite3.connect(database) as conn:
        return conn.execute(
            """
            SELECT
                COUNT(*),
                SUM(CASE WHEN official_url_source = 'givemeoc' THEN 1 ELSE 0 END),
                SUM(CASE WHEN announcement_url_source = 'givemeoc' THEN 1 ELSE 0 END),
                SUM(CASE WHEN official_url_source = 'givemeoc'
                          OR announcement_url_source = 'givemeoc' THEN 1 ELSE 0 END),
                SUM(CASE WHEN official_url IS NOT NULL AND official_url <> '' THEN 1 ELSE 0 END),
                SUM(CASE WHEN announcement_url IS NOT NULL AND announcement_url <> '' THEN 1 ELSE 0 END)
            FROM jobs
            """
        ).fetchone()


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill the seed database from GiveMeOC")
    parser.add_argument(
        "--database",
        type=Path,
        default=ROOT / "src" / "jobpicky" / "resources" / "jobs_seed.sqlite",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=ROOT / "src" / "jobpicky" / "resources" / "jobs_seed_source.json",
    )
    parser.add_argument("--max-pages", type=int, default=0, help="0 means all discovered pages")
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument(
        "--givemeoc-seed",
        type=Path,
        default=ROOT / "src" / "jobpicky" / "resources" / "givemeoc_seed.sqlite",
    )
    args = parser.parse_args()

    database = args.database.resolve()
    source = args.source.resolve()
    with tempfile.TemporaryDirectory(prefix="jobpicky-givemeoc-") as temporary:
        temporary_path = Path(temporary)
        database_backup = temporary_path / database.name
        source_backup = temporary_path / source.name
        givemeoc_seed = args.givemeoc_seed.resolve()
        givemeoc_seed_backup = temporary_path / givemeoc_seed.name
        if givemeoc_seed.is_file():
            shutil.copy2(givemeoc_seed, givemeoc_seed_backup)
        shutil.copy2(database, database_backup)
        shutil.copy2(source, source_backup)
        try:
            config = load_config("__jobpicky_missing_config__.yaml")
            config.setdefault("givemeoc", {}).update(
                enabled=True,
                max_pages_init=max(0, args.max_pages),
                min_interval_seconds=max(0.0, args.interval),
            )

            repo = JobRepository(database)
            repo.init_schema()
            # list_all_jobs() is a public projection and intentionally omits
            # the internal dedupe key; seed reconciliation needs the raw row.
            with repo.connect() as connection:
                rows = [dict(row) for row in connection.execute("SELECT * FROM jobs")]
            jobs = [repo.job_from_row(row) for row in rows]
            progress_events = 0

            def progress(_message: str) -> None:
                nonlocal progress_events
                progress_events += 1
                if progress_events % 25 == 0:
                    print(f"progress_events={progress_events}", flush=True)

            result = GiveMeOCCrawler(config, progress=progress).crawl(jobs, mode="init")
            matched = apply_givemeoc_links(
                jobs,
                result,
                config.get("system_taxonomy", {}).get("company_aliases", {}),
            )
            print(f"crawl_pages={result.pages_scanned}", flush=True)
            print(f"crawl_complete={int(result.complete)}", flush=True)
            print(f"crawl_records={len(result.records)}", flush=True)
            print(f"matched_jobs={matched}", flush=True)
            if result.error:
                raise RuntimeError("GivemeOC crawl failed; seed update rolled back")

            # The configured page limit is an intentional seed snapshot boundary.
            # Treat it as complete for reconciliation so legacy unverified links
            # outside the selected recent pages are removed from the seed.
            snapshot = replace(result, complete=True)
            apply_givemeoc_links(
                jobs,
                snapshot,
                config.get("system_taxonomy", {}).get("company_aliases", {}),
            )
            repo.reconcile_givemeoc_links(jobs, complete=True)
            repo.save_givemeoc_records(
                (asdict(record) for record in result.records),
                complete=True,
                total_pages=result.total_pages,
                pages_scanned=result.pages_scanned,
            )
            staged_source = temporary_path / source.name
            staged_database = temporary_path / database.name
            staged_givemeoc_seed = temporary_path / givemeoc_seed.name
            export_seed_source(database, staged_source)
            build_seed(staged_source, staged_database)
            build_givemeoc_seed(database, staged_givemeoc_seed)
            shutil.copy2(staged_source, source)
            shutil.copy2(staged_database, database)
            shutil.copy2(staged_givemeoc_seed, givemeoc_seed)

            values = _stats(database)
            labels = (
                "seed_total",
                "seed_givemeoc_official",
                "seed_givemeoc_announcement",
                "seed_givemeoc_any",
                "seed_any_official_value",
                "seed_any_announcement_value",
            )
            for label, value in zip(labels, values):
                print(f"{label}={value}")
            print("snapshot_committed=1")
            return 0
        except BaseException:
            shutil.copy2(database_backup, database)
            shutil.copy2(source_backup, source)
            if givemeoc_seed_backup.is_file():
                shutil.copy2(givemeoc_seed_backup, givemeoc_seed)
            raise


if __name__ == "__main__":
    raise SystemExit(main())
