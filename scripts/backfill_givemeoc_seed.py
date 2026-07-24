"""Backfill the seed database with verified GiveMeOC links."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from dataclasses import asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jobpicky.config import load_config
from jobpicky.givemeoc import GiveMeOCRecord, GiveMeOCCrawler, apply_givemeoc_links
from jobpicky.storage import JobRepository

from build_seed import build_seed
from build_givemeoc_seed import build_givemeoc_seed
from export_seed_source import export_seed_source


def _load_seed_cache(seed: Path) -> tuple[tuple[GiveMeOCRecord, ...], bool]:
    if not seed.is_file():
        return (), False
    with closing(sqlite3.connect(seed)) as connection:
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT source_record_id, company, company_normalized,
                       recruitment_type, target_graduate_year, city, deadline,
                       updated_at, announcement_url, official_url
                FROM givemeoc_records
                ORDER BY source_record_id
                """
            ).fetchall()
            state = connection.execute(
                "SELECT cache_initialized FROM givemeoc_scan_state WHERE id = 1"
            ).fetchone()
        except sqlite3.OperationalError:
            return (), False
    return tuple(GiveMeOCRecord(**dict(row)) for row in rows), bool(state and state[0])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build or incrementally update the GiveMeOC seed without modifying the job seed"
    )
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
    parser.add_argument("--mode", choices=("init", "daily"), default="init")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=40,
        help="init mode page limit; 0 means all discovered pages",
    )
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument(
        "--givemeoc-seed",
        type=Path,
        default=ROOT / "src" / "jobpicky" / "resources" / "givemeoc_seed.sqlite",
    )
    parser.add_argument(
        "--sync-job-seed",
        action="store_true",
        help="also update jobs_seed.sqlite and jobs_seed_source.json with matched links",
    )
    args = parser.parse_args()

    database = args.database.resolve()
    source = args.source.resolve()
    with tempfile.TemporaryDirectory(prefix="jobpicky-givemeoc-") as temporary:
        temporary_path = Path(temporary)
        working_database = temporary_path / database.name
        givemeoc_seed = args.givemeoc_seed.resolve()
        givemeoc_seed_backup = temporary_path / givemeoc_seed.name
        database_backup = temporary_path / f"{database.name}.bak"
        source_backup = temporary_path / f"{source.name}.bak"
        if givemeoc_seed.is_file():
            shutil.copy2(givemeoc_seed, givemeoc_seed_backup)
        if args.sync_job_seed:
            shutil.copy2(database, database_backup)
            if source.is_file():
                shutil.copy2(source, source_backup)
        try:
            config = load_config("__jobpicky_missing_config__.yaml")
            config.setdefault("givemeoc", {}).update(
                enabled=True,
                max_pages_init=max(0, args.max_pages),
                max_pages_daily=0,
                min_interval_seconds=max(0.0, args.interval),
            )

            shutil.copy2(database, working_database)
            repo = JobRepository(working_database)
            repo.init_schema()
            # Read jobs from the temporary copy; the packaged job seed stays read-only.
            with repo.connect() as connection:
                rows = [dict(row) for row in connection.execute("SELECT * FROM jobs")]
            jobs = [repo.job_from_row(row) for row in rows]
            if args.mode == "daily":
                cached_records, cache_initialized = _load_seed_cache(givemeoc_seed)
                if not cache_initialized:
                    raise RuntimeError("GiveMeOC seed is not initialized; run with --mode init first")
            else:
                cached_records, cache_initialized = (), False
            progress_events = 0

            def progress(_message: str) -> None:
                nonlocal progress_events
                progress_events += 1
                if progress_events % 25 == 0:
                    print(f"progress_events={progress_events}", flush=True)

            crawler = GiveMeOCCrawler(config, progress=progress)
            crawler.set_cache(cached_records, initialized=cache_initialized)
            result = crawler.crawl(jobs, mode=args.mode)
            print(f"crawl_mode={args.mode}", flush=True)
            print(f"crawl_pages={result.pages_scanned}", flush=True)
            print(f"crawl_complete={int(result.complete)}", flush=True)
            print(f"crawl_records={len(result.records)}", flush=True)
            if result.error:
                print(f"crawl_error={result.error}", file=sys.stderr, flush=True)
                raise RuntimeError("GivemeOC crawl failed; seed update rolled back")

            if args.sync_job_seed:
                matched = apply_givemeoc_links(
                    jobs,
                    result,
                    config.get("system_taxonomy", {}).get("company_aliases", {}),
                )
                repo.reconcile_givemeoc_links(jobs, complete=True)
                print(f"matched_jobs={matched}", flush=True)

            repo.save_givemeoc_records(
                (asdict(record) for record in result.records),
                complete=True,
                total_pages=result.total_pages,
                pages_scanned=result.pages_scanned,
            )
            staged_givemeoc_seed = temporary_path / givemeoc_seed.name
            build_givemeoc_seed(working_database, staged_givemeoc_seed)
            shutil.copy2(staged_givemeoc_seed, givemeoc_seed)
            if args.sync_job_seed:
                staged_source = temporary_path / source.name
                staged_database = temporary_path / database.name
                export_seed_source(working_database, staged_source)
                build_seed(staged_source, staged_database)
                shutil.copy2(staged_source, source)
                shutil.copy2(staged_database, database)
                print("job_seed_synced=1", flush=True)
            print(f"seed_records={len(result.records)}", flush=True)
            print("givemeoc_seed_committed=1")
            return 0
        except BaseException:
            if givemeoc_seed_backup.is_file():
                shutil.copy2(givemeoc_seed_backup, givemeoc_seed)
            if args.sync_job_seed:
                if database_backup.is_file():
                    shutil.copy2(database_backup, database)
                if source_backup.is_file():
                    shutil.copy2(source_backup, source)
            raise


if __name__ == "__main__":
    raise SystemExit(main())
