"""Incrementally refresh the distributable seed in an isolated workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from jobpicky.config import DEFAULT_CONFIG  # noqa: E402
from jobpicky.core.ingestion import JobIngestionService  # noqa: E402
from jobpicky.givemeoc import (  # noqa: E402
    GiveMeOCCrawlResult,
    GiveMeOCRecord,
    apply_givemeoc_links,
)
from jobpicky.storage import JobRepository  # noqa: E402
from jobpicky.wondercv import EXTRACTION_VERSION, WonderCVCrawler  # noqa: E402
from scripts.build_seed import build_seed  # noqa: E402
from scripts.export_seed_source import export_seed_source  # noqa: E402


DEFAULT_DATABASE = ROOT / "src" / "jobpicky" / "resources" / "jobs_seed.sqlite"
DEFAULT_JSON = ROOT / "src" / "jobpicky" / "resources" / "jobs_seed_source.json"
DEFAULT_GIVEMEOC_SEED = ROOT / "src" / "jobpicky" / "resources" / "givemeoc_seed.sqlite"
DEFAULT_WORK_ROOT = ROOT / ".test-results" / "seed-refresh"
PRIVATE_TABLES = ("job_matches", "recommended_jobs", "job_user_state", "feishu_sync", "scan_runs")
PUBLIC_TABLES = ("jobs", "job_positions")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _load_givemeoc_seed(path: Path) -> tuple[GiveMeOCRecord, ...]:
    if not path.is_file():
        return ()
    with closing(sqlite3.connect(path)) as connection:
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
        except sqlite3.OperationalError:
            return ()
    return tuple(GiveMeOCRecord(**dict(row)) for row in rows)


def _snapshot(database: Path, table: str) -> tuple[list[str], list[tuple]]:
    connection = sqlite3.connect(database)
    try:
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        projection = ", ".join(f'"{column}"' for column in columns)
        return columns, connection.execute(f"SELECT {projection} FROM {table} ORDER BY id").fetchall()
    finally:
        connection.close()


def _validate_database(database: Path, through_date: date, old_keys: set[str]) -> dict[str, int | str]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        jobs = connection.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        positions = connection.execute("SELECT COUNT(*) FROM job_positions").fetchone()[0]
        private_counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in PRIVATE_TABLES
        }
    finally:
        connection.close()

    errors: list[str] = []
    if integrity != "ok":
        errors.append(f"integrity_check={integrity}")
    if foreign_keys:
        errors.append(f"foreign_key_check={len(foreign_keys)}")
    if any(private_counts.values()):
        errors.append(f"private tables are not empty: {private_counts}")
    keys = {row["dedupe_key"] for row in jobs}
    missing = old_keys - keys
    if missing:
        errors.append(f"lost {len(missing)} existing jobs")
    parsed_dates = {row["id"]: _date(row["collected_date"]) for row in jobs}
    invalid_dates = [job_id for job_id, collected in parsed_dates.items() if collected is None]
    too_new = [job_id for job_id, collected in parsed_dates.items() if collected and collected > through_date]
    stale = [
        row["id"]
        for row in jobs
        if row["parse_status"] != "detail_ready" or row["extraction_version"] != EXTRACTION_VERSION
    ]
    if invalid_dates:
        errors.append(f"jobs with invalid collected_date: {invalid_dates[:10]}")
    if too_new:
        errors.append(f"jobs newer than {through_date.isoformat()}: {too_new[:10]}")
    if stale:
        errors.append(f"jobs not parsed with {EXTRACTION_VERSION}: {stale[:10]}")
    if errors:
        raise RuntimeError("seed validation failed: " + "; ".join(errors))
    return {"jobs": len(jobs), "positions": positions, "integrity": integrity}


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(f"another seed refresh may be running; remove stale lock if necessary: {path}") from None
    try:
        os.write(descriptor, f"pid={os.getpid()} started={datetime.now().isoformat()}\n".encode())
        os.close(descriptor)
        yield
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        path.unlink(missing_ok=True)


def _publish_pair(staged_json: Path, staged_database: Path, target_json: Path, target_database: Path, backup: Path) -> None:
    backup.mkdir(parents=True, exist_ok=True)
    targets = ((staged_json, target_json), (staged_database, target_database))
    for _, target in targets:
        if not target.exists():
            raise FileNotFoundError(f"publish target does not exist: {target}")
        shutil.copy2(target, backup / target.name)

    prepared: list[tuple[Path, Path]] = []
    try:
        for staged, target in targets:
            handle, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".publish", dir=target.parent)
            os.close(handle)
            temporary = Path(name)
            shutil.copy2(staged, temporary)
            prepared.append((temporary, target))
        for temporary, target in prepared:
            os.replace(temporary, target)
        if any(_sha256(staged) != _sha256(target) for staged, target in targets):
            raise RuntimeError("published seed hashes do not match staged artifacts")
    except Exception:
        for _, target in targets:
            saved = backup / target.name
            if saved.exists():
                shutil.copy2(saved, target)
        raise
    finally:
        for temporary, _ in prepared:
            temporary.unlink(missing_ok=True)


def refresh_seed(
    source_database: Path = DEFAULT_DATABASE,
    target_json: Path = DEFAULT_JSON,
    target_database: Path = DEFAULT_DATABASE,
    work_root: Path = DEFAULT_WORK_ROOT,
    *,
    through_date: date,
    publish: bool = False,
    max_pages: int = 50,
    overlap_days: int = 7,
    max_new_items: int = 200,
    givemeoc_seed: Path = DEFAULT_GIVEMEOC_SEED,
    crawler_factory=WonderCVCrawler,
) -> dict:
    source_database = source_database.resolve()
    target_json = target_json.resolve()
    target_database = target_database.resolve()
    work_root = work_root.resolve()
    run_dir = work_root / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
    staging = run_dir / "staging"
    reports = run_dir / "reports"
    staging.mkdir(parents=True)
    reports.mkdir(parents=True)

    with _exclusive_lock(work_root / ".refresh.lock"):
        staged_raw = staging / "jobs_seed.crawled.sqlite"
        staged_json = staging / "jobs_seed_source.json"
        staged_database = staging / "jobs_seed.sqlite"
        shutil.copy2(source_database, staged_raw)
        repository = JobRepository(staged_raw)
        repository.init_schema()

        with repository.connect() as connection:
            old_rows = connection.execute("SELECT dedupe_key, collected_date FROM jobs").fetchall()
        old_keys = {row[0] for row in old_rows if _date(row[1]) and _date(row[1]) <= through_date}
        old_dates = [_date(row[1]) for row in old_rows if row[1]]
        latest_existing = max(old_dates) if old_dates else through_date
        overlap_start = latest_existing - timedelta(days=max(0, overlap_days))

        config = deepcopy(DEFAULT_CONFIG)
        config["crawler"].update({"max_pages_daily": max_pages, "enrich_details": True})
        crawler = crawler_factory(config)
        consecutive_known_old_pages = 0

        def should_stop(page_jobs) -> bool:
            nonlocal consecutive_known_old_pages
            dates = [_date(job.collected_date) for job in page_jobs if job.collected_date]
            known = all(repository.job_exists(job.dedupe_key) for job in page_jobs)
            old_page = bool(dates) and max(dates) < overlap_start
            consecutive_known_old_pages = consecutive_known_old_pages + 1 if known and old_page else 0
            return consecutive_known_old_pages >= 2

        crawl = crawler.crawl(mode="daily", should_stop=should_stop)
        if crawl.error or crawl.partial or crawl.interrupted:
            raise RuntimeError(f"crawl did not complete safely: {crawl.error or 'partial/interrupted result'}")

        eligible = []
        rejected = []
        for job in crawl.jobs:
            try:
                collected = _date(job.collected_date)
            except ValueError:
                collected = None
            if collected is None:
                rejected.append({"detail_url": job.detail_url, "collected_date": job.collected_date, "reason": "invalid date"})
            elif collected <= through_date:
                eligible.append(job)
        (reports / "rejected.json").write_text(json.dumps(rejected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if rejected:
            raise RuntimeError(f"crawl returned {len(rejected)} jobs with missing or invalid collected_date")

        ingestion = JobIngestionService(repository, config).ingest(eligible)
        if ingestion.new_items > max_new_items:
            raise RuntimeError(
                f"refusing unusually large seed growth: {ingestion.new_items} new jobs exceeds {max_new_items}"
            )
        givemeoc_records = _load_givemeoc_seed(givemeoc_seed.resolve())
        givemeoc_links_updated = 0
        if givemeoc_records:
            with repository.connect() as connection:
                stored_rows = [dict(row) for row in connection.execute("SELECT * FROM jobs")]
            stored_jobs = [repository.job_from_row(row) for row in stored_rows]
            givemeoc_links_updated = apply_givemeoc_links(
                stored_jobs,
                GiveMeOCCrawlResult(
                    records=givemeoc_records,
                    pages_scanned=0,
                    complete=True,
                ),
                config.get("system_taxonomy", {}).get("company_aliases", {}),
            )
            repository.reconcile_givemeoc_links(stored_jobs, complete=True)
        with repository.connect() as connection:
            connection.execute("DELETE FROM job_positions WHERE job_id IN (SELECT id FROM jobs WHERE date(collected_date) > date(?))", (through_date.isoformat(),))
            connection.execute("DELETE FROM jobs WHERE date(collected_date) > date(?)", (through_date.isoformat(),))
            for table in PRIVATE_TABLES:
                connection.execute(f"DELETE FROM {table}")
            connection.execute("UPDATE jobs SET last_checked = NULL")
            connection.execute("UPDATE job_positions SET created_at = NULL, updated_at = NULL")

        crawled_validation = _validate_database(staged_raw, through_date, old_keys)
        export_seed_source(staged_raw, staged_json)
        build_seed(staged_json, staged_database)
        rebuilt_validation = _validate_database(staged_database, through_date, old_keys)
        for table in PUBLIC_TABLES:
            if _snapshot(staged_raw, table) != _snapshot(staged_database, table):
                raise RuntimeError(f"JSON round-trip changed table {table}")

        manifest = {
            "through_date": through_date.isoformat(),
            "source_sha256": _sha256(source_database),
            "json_sha256": _sha256(staged_json),
            "database_sha256": _sha256(staged_database),
            "pages_scanned": crawl.pages_scanned,
            "items_seen": ingestion.items_seen,
            "new_items": ingestion.new_items,
            "updated_items": ingestion.updated_items,
            "official_links_checked": 0,
            "official_links_updated": 0,
            "givemeoc_records_loaded": len(givemeoc_records),
            "givemeoc_links_updated": givemeoc_links_updated,
            "validation": rebuilt_validation,
            "run_directory": str(run_dir),
            "published": False,
        }
        (reports / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if crawled_validation != rebuilt_validation:
            raise RuntimeError("rebuilt seed validation summary differs from crawled seed")
        if publish:
            _publish_pair(staged_json, staged_database, target_json, target_database, run_dir / "backup")
            manifest["published"] = True
            (reports / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--target-json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--target-database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument("--through-date", type=date.fromisoformat, default=date.today() - timedelta(days=2))
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--overlap-days", type=int, default=7)
    parser.add_argument("--max-new-items", type=int, default=200)
    parser.add_argument("--givemeoc-seed", type=Path, default=DEFAULT_GIVEMEOC_SEED)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    result = refresh_seed(
        args.source,
        args.target_json,
        args.target_database,
        args.work_root,
        through_date=args.through_date,
        publish=args.publish,
        max_pages=args.max_pages,
        overlap_days=args.overlap_days,
        max_new_items=args.max_new_items,
        givemeoc_seed=args.givemeoc_seed,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
