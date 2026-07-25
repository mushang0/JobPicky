from datetime import date
from datetime import datetime
from pathlib import Path

import jobpicky.core.official_pipeline as official_pipeline

from jobpicky.core import JobIngestionService, MatchingService, build_official_job_batch
from jobpicky.givemeoc import GiveMeOCRecord, build_givemeoc_record_index
from jobpicky.models import Job, Position
from jobpicky.official_jobs import OfficialCrawlResult
from jobpicky.storage import JobRepository
from jobpicky.wondercv import CrawlResult, EXTRACTION_VERSION
from scripts.refresh_seed import refresh_seed


def test_official_batch_reuses_prebuilt_givemeoc_index(monkeypatch, mock_config):
    config = mock_config()
    job = Job(company="TargetCo", title="FPGA Engineer")
    record = GiveMeOCRecord("oc-1", "TargetCo", "targetco", official_url="https://jobs.example/target")
    record_index = build_givemeoc_record_index((record,), {})

    def unexpected_index_build(*_args, **_kwargs):
        raise AssertionError("the caller-provided GiveMeOC index should be reused")

    monkeypatch.setattr(official_pipeline, "build_givemeoc_record_index", unexpected_index_build)
    batch = build_official_job_batch(
        [job],
        [record],
        config,
        fetch_official=False,
        record_index=record_index,
    )

    assert batch.links_matched == 1
    assert batch.jobs[0].official_url == record.official_url


def test_official_batch_reuses_fresh_official_jobs_without_refetching(mock_config):
    config = mock_config()
    config["official_jobs"] = {"enabled": True, "cache_days": 7, "allow_generic": True}
    wondercv = Job(
        source="WonderCV",
        source_job_id="wonder-1",
        dedupe_key="WonderCV:id:wonder-1",
        company="TargetCo",
        title="FPGA Engineer",
        city="Shanghai",
    )
    record = GiveMeOCRecord("oc-1", "TargetCo", "targetco", official_url="https://careers.target.example/jobs")
    cached = Job(
        source="official",
        source_job_id="official:target-1",
        dedupe_key="official:id:official:target-1",
        source_url=record.official_url,
        detail_url="https://careers.target.example/jobs/target-1",
        company="TargetCo",
        title="FPGA Engineer",
        city="Shanghai",
        parse_status="detail_ready",
        extraction_version="official-html-v1",
        raw_text="official detail",
        last_seen=datetime.now().isoformat(),
    )

    class UnexpectedCrawler:
        def crawl(self, records):
            raise AssertionError(f"fresh official source should not be fetched: {list(records)}")

    batch = build_official_job_batch(
        [wondercv],
        [record],
        config,
        existing_jobs=[cached],
        crawler=UnexpectedCrawler(),
    )

    assert batch.official_result.sources_checked == 0
    assert batch.jobs[0].source == "official"
    assert batch.jobs[0].dedupe_key == wondercv.dedupe_key


def test_official_batch_fetches_one_time_per_shared_source_url(mock_config):
    config = mock_config()
    config["official_jobs"] = {"enabled": True, "allow_generic": True}
    shared_url = "https://careers.shared.example/jobs"
    jobs = [
        Job(company="Alpha", title="FPGA Engineer"),
        Job(company="Beta", title="RTL Engineer"),
    ]
    records = [
        GiveMeOCRecord("oc-alpha", "Alpha", "alpha", official_url=shared_url),
        GiveMeOCRecord("oc-beta", "Beta", "beta", official_url=shared_url),
    ]

    class FakeOfficialCrawler:
        def __init__(self):
            self.records = []

        def crawl(self, records_to_crawl):
            self.records = list(records_to_crawl)
            return OfficialCrawlResult((), len(self.records), 0)

    crawler = FakeOfficialCrawler()
    build_official_job_batch(jobs, records, config, crawler=crawler)

    assert [record.official_url for record in crawler.records] == [shared_url]


def test_official_batch_runs_match_merge_and_database_write(tmp_path, mock_config):
    config = mock_config()
    config["official_jobs"] = {"enabled": True, "allow_generic": True}
    wondercv = Job(
        source="WonderCV",
        source_job_id="wonder-1",
        dedupe_key="WonderCV:id:wonder-1",
        company="TargetCo",
        title="FPGA Engineer",
        city="Shanghai",
        batch="绉嬫嫑",
        target_graduate_year="2027灞?",
        parse_status="detail_ready",
    )
    record = GiveMeOCRecord(
        "oc-1", "TargetCo", "TargetCo", official_url="https://careers.target.example/jobs"
    )
    official = Job(
        source="official",
        source_job_id="official:target-1",
        dedupe_key="official:id:official:target-1",
        source_url=record.official_url,
        detail_url="https://careers.target.example/jobs/target-1",
        company="TargetCo",
        title="FPGA Engineer",
        city="Shanghai",
        raw_text="Build and verify FPGA designs with RTL, simulation, timing closure and board bring-up. " * 2,
        role_text="FPGA RTL verification",
        parse_status="detail_ready",
        extraction_version="official-html-v1",
        positions=[Position(title="FPGA Engineer", confidence=0.8)],
        official_url="https://careers.target.example/jobs/target-1",
        official_url_source="official",
    )

    class FakeOfficialCrawler:
        def __init__(self):
            self.records = []

        def crawl(self, records):
            self.records = list(records)
            return OfficialCrawlResult((official,), 1, 1)

    crawler = FakeOfficialCrawler()
    batch = build_official_job_batch([wondercv], [record], config, crawler=crawler)

    repo = JobRepository(tmp_path / "jobs.sqlite")
    repo.init_schema()
    ingestion = JobIngestionService(repo, config).ingest(batch.jobs)
    matching = MatchingService(repo, config).match_ingested(ingestion.changed_items)
    row = repo.list_all_jobs()[0]

    assert [item.source_record_id for item in crawler.records] == ["oc-1"]
    assert batch.links_matched == 1
    assert len(batch.jobs) == 1
    assert batch.jobs[0].source == "official"
    assert batch.jobs[0].dedupe_key == wondercv.dedupe_key
    assert ingestion.new_items == 1
    assert matching.matched_items == 1
    assert row["source"] == "official"
    assert row["official_url"] == official.detail_url
    assert row["official_url_source"] == "official"
    assert repo.list_positions(ingestion.items[0].job_id)[0]["title"] == "FPGA Engineer"


def test_seed_refresh_reuses_the_same_official_batch_pipeline(tmp_path):
    source = tmp_path / "source.sqlite"
    target_json = tmp_path / "seed.json"
    target_database = tmp_path / "seed.sqlite"
    givemeoc_seed = tmp_path / "givemeoc.sqlite"

    source_repo = JobRepository(source)
    source_repo.init_schema()
    source_repo.upsert_job(Job(
        dedupe_key="existing",
        source_job_id="existing",
        company="ExistingCo",
        title="Existing recruitment",
        collected_date="2026-07-13",
        parse_status="detail_ready",
        extraction_version=EXTRACTION_VERSION,
        raw_text="existing detail",
    ))
    link_repo = JobRepository(givemeoc_seed)
    link_repo.init_schema()
    link_repo.save_givemeoc_records([{
        "source_record_id": "oc-new",
        "company": "NewCo",
        "company_normalized": "NewCo",
        "official_url": "https://jobs.lever.co/newco",
    }], complete=True, total_pages=1, pages_scanned=1)

    wonder_job = Job(
        source_job_id="new",
        dedupe_key="WonderCV:id:new",
        company="NewCo",
        title="New recruitment",
        detail_url="https://www.wondercv.com/jobs/new",
        collected_date="2026-07-17",
        parse_status="detail_ready",
        extraction_version=EXTRACTION_VERSION,
        raw_text="wondercv detail",
    )
    official_job = Job(
        source="official",
        source_job_id="official:1",
        dedupe_key="official:id:official:1",
        source_url="https://jobs.lever.co/newco",
        detail_url="https://jobs.lever.co/newco/1",
        company="NewCo",
        title="New recruitment",
        collected_date="2026-07-17",
        raw_text="Official detail with requirements and responsibilities. " * 3,
        parse_status="detail_ready",
        extraction_version="official-html-v1",
        official_url="https://jobs.lever.co/newco/1",
        official_url_source="official",
        positions=[Position(title="New recruitment", confidence=0.8)],
    )

    class WonderCrawler:
        def __init__(self, config):
            pass

        def crawl(self, mode="daily", should_stop=None):
            return CrawlResult([wonder_job], 1)

    class OfficialCrawler:
        def __init__(self, config):
            pass

        def crawl(self, records):
            assert [record.source_record_id for record in records] == ["oc-new"]
            return OfficialCrawlResult((official_job,), 1, 1)

    result = refresh_seed(
        source,
        target_json,
        target_database,
        tmp_path / "runs",
        through_date=date(2026, 7, 17),
        givemeoc_seed=givemeoc_seed,
        crawler_factory=WonderCrawler,
        official_crawler_factory=OfficialCrawler,
    )

    assert result["official_links_checked"] == 1
    assert result["official_links_updated"] == 1
    staged = Path(result["run_directory"]) / "staging" / "jobs_seed.sqlite"
    with JobRepository(staged).connect() as connection:
        row = connection.execute(
            "SELECT source, dedupe_key, official_url, official_url_source, givemeoc_record_id FROM jobs WHERE dedupe_key = ?",
            ("WonderCV:id:new",),
        ).fetchone()
    assert tuple(row) == ("official", "WonderCV:id:new", "https://jobs.lever.co/newco/1", "official", "oc-new")
