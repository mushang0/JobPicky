import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from jobpicky.models import Job
from jobpicky.official_jobs import OfficialCrawlResult
from jobpicky.storage import JobRepository
from jobpicky.wondercv import CrawlResult, EXTRACTION_VERSION
from scripts import refresh_seed as refresh_seed_module
from scripts.refresh_seed import refresh_seed


def _job(key: str, collected_date: str, *, status: str = "detail_ready") -> Job:
    return Job(
        dedupe_key=key,
        source_job_id=key,
        company=f"{key} company",
        title=f"{key} recruitment",
        detail_url=f"https://www.wondercv.com/jobs/{key}",
        collected_date=collected_date,
        parse_status=status,
        extraction_version=EXTRACTION_VERSION,
        raw_text="招聘岗位：嵌入式工程师",
    )


def _seed(path: Path) -> None:
    repository = JobRepository(path)
    repository.init_schema()
    repository.upsert_job(_job("existing", "2026-07-13"))


class FakeCrawler:
    jobs = [_job("new", "2026-07-17"), _job("future", "2026-07-18")]

    def __init__(self, config):
        self.config = config

    def crawl(self, mode="daily", should_stop=None):
        assert mode == "daily"
        assert should_stop is not None
        return CrawlResult(jobs=list(self.jobs), pages_scanned=2)


def test_refresh_builds_isolated_seed_through_inclusive_date(tmp_path: Path):
    source = tmp_path / "source.sqlite"
    target_json = tmp_path / "official.json"
    target_database = tmp_path / "official.sqlite"
    _seed(source)
    target_json.write_text("old json", encoding="utf-8")
    target_database.write_bytes(b"old database")

    result = refresh_seed(
        source,
        target_json,
        target_database,
        tmp_path / "runs",
        through_date=date(2026, 7, 17),
        givemeoc_seed=tmp_path / "missing-givemeoc.sqlite",
        crawler_factory=FakeCrawler,
    )

    assert result["new_items"] == 1
    assert result["official_links_checked"] == 0
    assert result["official_links_updated"] == 0
    assert result["published"] is False
    assert target_json.read_text(encoding="utf-8") == "old json"
    assert target_database.read_bytes() == b"old database"
    staged = Path(result["run_directory"]) / "staging" / "jobs_seed.sqlite"
    connection = sqlite3.connect(staged)
    try:
        assert connection.execute("SELECT dedupe_key, official_url FROM jobs ORDER BY dedupe_key").fetchall() == [
            ("existing", None),
            ("new", None),
        ]
    finally:
        connection.close()


def test_refresh_applies_givemeoc_seed_links_to_staged_job_seed(tmp_path: Path):
    source = tmp_path / "source.sqlite"
    target_json = tmp_path / "official.json"
    target_database = tmp_path / "official.sqlite"
    givemeoc_seed = tmp_path / "givemeoc_seed.sqlite"
    _seed(source)
    link_repository = JobRepository(givemeoc_seed)
    link_repository.init_schema()
    link_repository.save_givemeoc_records(
        [{
            "source_record_id": "givemeoc-new",
            "company": "new company",
            "company_normalized": "newcompany",
            "official_url": "https://newco.example/jobs",
        }],
        complete=True,
        total_pages=40,
        pages_scanned=40,
    )

    result = refresh_seed(
        source,
        target_json,
        target_database,
        tmp_path / "runs",
        through_date=date(2026, 7, 17),
        givemeoc_seed=givemeoc_seed,
        crawler_factory=FakeCrawler,
    )

    assert result["givemeoc_records_loaded"] == 1
    assert result["givemeoc_links_updated"] == 1
    staged = Path(result["run_directory"]) / "staging" / "jobs_seed.sqlite"
    with sqlite3.connect(staged) as connection:
        assert connection.execute(
            "SELECT official_url, official_url_source, givemeoc_record_id FROM jobs WHERE dedupe_key = 'new'"
        ).fetchone() == ("https://newco.example/jobs", "givemeoc", "givemeoc-new")


def test_refresh_publishes_both_artifacts_after_validation(tmp_path: Path):
    source = tmp_path / "source.sqlite"
    target_json = tmp_path / "official.json"
    target_database = tmp_path / "official.sqlite"
    _seed(source)
    target_json.write_text("old json", encoding="utf-8")
    target_database.write_bytes(b"old database")

    result = refresh_seed(
        source,
        target_json,
        target_database,
        tmp_path / "runs",
        through_date=date(2026, 7, 17),
        publish=True,
        givemeoc_seed=tmp_path / "missing-givemeoc.sqlite",
        crawler_factory=FakeCrawler,
    )

    assert json.loads(target_json.read_text(encoding="utf-8"))["format_version"] == 2
    assert target_database.read_bytes()[:16] == b"SQLite format 3\x00"
    assert result["published"] is True
    backup = Path(result["run_directory"]) / "backup"
    assert (backup / target_json.name).read_text(encoding="utf-8") == "old json"
    assert (backup / target_database.name).read_bytes() == b"old database"


def test_refresh_keeps_link_checkpoint_when_official_stage_fails(tmp_path: Path):
    source = tmp_path / "source.sqlite"
    target_json = tmp_path / "official.json"
    target_database = tmp_path / "official.sqlite"
    givemeoc_seed = tmp_path / "givemeoc_seed.sqlite"
    _seed(source)
    target_json.write_text("old json", encoding="utf-8")
    target_database.write_bytes(b"old database")
    link_repository = JobRepository(givemeoc_seed)
    link_repository.init_schema()
    link_repository.save_givemeoc_records(
        [{
            "source_record_id": "givemeoc-new",
            "company": "new company",
            "company_normalized": "newcompany",
            "official_url": "https://jobs.lever.co/newco",
        }],
        complete=True,
        total_pages=1,
        pages_scanned=1,
    )

    class FailingOfficialCrawler:
        def __init__(self, config):
            pass

        def crawl(self, records):
            raise RuntimeError("official source unavailable")

    messages = []
    with pytest.raises(RuntimeError, match="official source unavailable"):
        refresh_seed(
            source,
            target_json,
            target_database,
            tmp_path / "runs",
            through_date=date(2026, 7, 17),
            givemeoc_seed=givemeoc_seed,
            crawler_factory=FakeCrawler,
            official_crawler_factory=FailingOfficialCrawler,
            max_new_items=10,
            publish=True,
            progress=messages.append,
        )

    with JobRepository(target_database).connect() as connection:
        row = connection.execute(
            "SELECT official_url, official_url_source, givemeoc_record_id FROM jobs WHERE dedupe_key = 'new'"
        ).fetchone()
    assert tuple(row) == ("https://jobs.lever.co/newco", "givemeoc", "givemeoc-new")
    assert any(message.startswith("[scan]") for message in messages)
    assert any(message.startswith("[givemeoc-links]") for message in messages)

    class UnexpectedWonderCrawler:
        def __init__(self, config):
            raise AssertionError("resume should not create the WonderCV crawler")

    class SuccessfulOfficialCrawler:
        def __init__(self, config):
            pass

        def crawl(self, records):
            return OfficialCrawlResult((), len(tuple(records)), 0)

    resumed_messages = []
    resumed = refresh_seed(
        source,
        target_json,
        target_database,
        tmp_path / "runs",
        through_date=date(2026, 7, 17),
        givemeoc_seed=givemeoc_seed,
        crawler_factory=UnexpectedWonderCrawler,
        official_crawler_factory=SuccessfulOfficialCrawler,
        max_new_items=10,
        publish=True,
        resume=True,
        progress=resumed_messages.append,
    )
    assert resumed["published"] is True
    assert any(message.startswith("[恢复]") for message in resumed_messages)


def test_refresh_rejects_invalid_dates_without_publishing(tmp_path: Path):
    source = tmp_path / "source.sqlite"
    target_json = tmp_path / "official.json"
    target_database = tmp_path / "official.sqlite"
    _seed(source)
    target_json.write_text("old json", encoding="utf-8")
    target_database.write_bytes(b"old database")

    class InvalidDateCrawler(FakeCrawler):
        jobs = [_job("bad", "not-a-date")]

    with pytest.raises(RuntimeError, match="missing or invalid collected_date"):
        refresh_seed(
            source,
            target_json,
            target_database,
            tmp_path / "runs",
            through_date=date(2026, 7, 17),
            givemeoc_seed=tmp_path / "missing-givemeoc.sqlite",
            publish=True,
            crawler_factory=InvalidDateCrawler,
        )

    assert target_json.read_text(encoding="utf-8") == "old json"
    assert target_database.read_bytes() == b"old database"


def test_publish_pair_restores_both_targets_when_replace_fails(tmp_path: Path, monkeypatch):
    staged_json = tmp_path / "staged.json"
    staged_database = tmp_path / "staged.sqlite"
    target_json = tmp_path / "official.json"
    target_database = tmp_path / "official.sqlite"
    staged_json.write_text("new json", encoding="utf-8")
    staged_database.write_bytes(b"new database")
    target_json.write_text("old json", encoding="utf-8")
    target_database.write_bytes(b"old database")
    real_replace = refresh_seed_module.os.replace
    replacements = 0

    def fail_second_replace(source, target):
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            raise PermissionError("database is busy")
        real_replace(source, target)

    monkeypatch.setattr(refresh_seed_module.os, "replace", fail_second_replace)
    with pytest.raises(PermissionError, match="database is busy"):
        refresh_seed_module._publish_pair(
            staged_json, staged_database, target_json, target_database, tmp_path / "backup"
        )

    assert target_json.read_text(encoding="utf-8") == "old json"
    assert target_database.read_bytes() == b"old database"
