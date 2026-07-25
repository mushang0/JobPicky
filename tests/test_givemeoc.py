import sqlite3

from jobpicky.givemeoc import (
    GiveMeOCCrawlResult,
    GiveMeOCRecord,
    apply_givemeoc_links,
    GiveMeOCCrawler,
    match_givemeoc_record,
    normalize_givemeoc_company,
    parse_givemeoc_page,
    extract_official_url_from_announcement,
)
from jobpicky.models import Job
from jobpicky.storage import JobRepository
from scripts.build_givemeoc_seed import build_givemeoc_seed


def test_parse_givemeoc_rows_extracts_links_and_skips_honeypot():
    html = """
    <table><tbody>
      <tr data-id="101">
        <td class="crt-col-company">影石 Insta360</td>
        <td class="crt-col-recruitment-type">秋招</td>
        <td class="crt-col-target">2027届</td>
        <td class="crt-col-location">深圳</td>
        <td class="crt-col-update-time">2026-07-23</td>
        <td class="crt-col-deadline">2026-10-31</td>
        <td class="crt-col-links"><a href="https://careers.example/jobs">投递</a></td>
        <td class="crt-col-notice"><a href="https://mp.weixin.qq.com/s/abc">公告</a></td>
      </tr>
      <tr class="crt-honeypot-row" aria-hidden="true" data-id="999">
        <td class="crt-col-company">诱饵公司</td>
      </tr>
    </tbody></table>
    """

    records = parse_givemeoc_page(html)

    assert len(records) == 1
    assert records[0].company == "影石 Insta360"
    assert records[0].official_url == "https://careers.example/jobs"
    assert records[0].announcement_url == "https://mp.weixin.qq.com/s/abc"


def test_givemeoc_rejects_internal_placeholder_links():
    html = """
    <tr data-id="1">
      <td class="crt-col-company">Example</td>
      <td class="crt-col-links"><a href="https://www.givemeoc.com/user/vip">投递</a></td>
      <td class="crt-col-notice"><a href="https://www.givemeoc.com/user/vip">公告</a></td>
    </tr>
    """

    record = parse_givemeoc_page(html)[0]

    assert record.official_url is None
    assert record.announcement_url is None


def test_extract_official_url_from_announcement_accepts_explicit_career_link():
    html = '<main><p>请通过官网报名</p><a href="https://careers.example.com/campus">报名入口</a></main>'

    assert extract_official_url_from_announcement(html, "https://mp.weixin.qq.com/s/notice") == (
        "https://careers.example.com/campus"
    )


def test_extract_official_url_prefers_high_confidence_link_when_notice_has_multiple_urls():
    html = (
        '<a href="https://example.com/company-profile">公司介绍</a>'
        '<a href="https://dexmal-inc.jobs.feishu.cn/285572/position/list">官方投递</a>'
    )

    assert extract_official_url_from_announcement(html, "https://news.example/notice") == (
        "https://dexmal-inc.jobs.feishu.cn/285572/position/list"
    )


def test_company_match_accepts_short_external_company_name():
    record = GiveMeOCRecord(
        "oryx", "原力灵机", "原力灵机",
        announcement_url="https://news.example/oryx",
        official_url="https://dexmal-inc.jobs.feishu.cn/285572/position/list",
    )

    assert match_givemeoc_record(Job(company="北京原力灵机智能科技有限公司"), (record,)).source_record_id == "oryx"


def test_company_match_prefers_batch_and_year():
    job = Job(company="影石Insta360", batch="秋招", target_graduate_year="2027届")
    records = (
        GiveMeOCRecord("old", "影石 Insta360", "影石insta360", "实习", "2026届", updated_at="2026-07-23"),
        GiveMeOCRecord("new", "影石 Insta360", "影石insta360", "秋招", "2027届", updated_at="2026-07-20"),
    )

    assert match_givemeoc_record(job, records).source_record_id == "new"


def test_company_normalization_handles_legal_entity_and_brand_names():
    assert normalize_givemeoc_company("北京字节跳动科技有限公司") == "字节跳动"
    assert normalize_givemeoc_company("影石创新科技股份有限公司") == "影石创新"
    assert match_givemeoc_record(
        Job(company="北京字节跳动科技有限公司"),
        (GiveMeOCRecord("byte", "字节跳动", "字节跳动", official_url="https://jobs.bytedance.com"),),
    ).source_record_id == "byte"
    assert match_givemeoc_record(
        Job(company="影石创新科技股份有限公司"),
        (GiveMeOCRecord("insta", "影石Insta360", "影石insta360", official_url="https://www.insta360.com"),),
        {"影石创新": ["影石Insta360", "insta360"]},
    ).source_record_id == "insta"


def test_initial_givemeoc_scan_reaches_late_pages_and_keeps_snapshot_rows():
    pages = {
        "https://www.givemeoc.com/": '<a href="?paged=3">3</a><tr data-id="noise"><td class="crt-col-company">其他公司</td></tr>',
        "https://www.givemeoc.com/?paged=2": "",
        "https://www.givemeoc.com/?paged=3": """
            <tr data-id="byte">
              <td class="crt-col-company">字节跳动</td>
              <td class="crt-col-links"><a href="https://jobs.bytedance.com">投递</a></td>
            </tr>
        """,
    }

    class Response:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

    def get(url, **_kwargs):
        return Response(pages[url])

    result = GiveMeOCCrawler(
        {"givemeoc": {"max_pages_daily": 0}, "system_taxonomy": {"company_aliases": {}}},
        get=get,
        sleep=lambda _seconds: None,
    ).crawl([Job(company="北京字节跳动科技有限公司")], mode="daily")

    assert result.complete is True
    assert result.pages_scanned == 3
    assert [record.source_record_id for record in result.records] == ["noise", "byte"]


def test_daily_givemeoc_scan_refreshes_only_recent_pages_and_matches_cached_rows():
    requested = []
    pages = {
        "https://www.givemeoc.com/": '<a href="?paged=70">70</a><tr data-id="new"><td class="crt-col-company">瀛楄妭璺冲姩</td></tr>',
        "https://www.givemeoc.com/?paged=2": '<tr data-id="newer"><td class="crt-col-company">Insta360</td><td class="crt-col-update-time">2026-07-24</td></tr>',
    }

    class Response:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

    def get(url, **_kwargs):
        requested.append(url)
        return Response(pages[url])

    crawler = GiveMeOCCrawler(
        {"givemeoc": {"max_pages_daily": 2}, "system_taxonomy": {"company_aliases": {}}},
        get=get,
        sleep=lambda _seconds: None,
    )
    crawler.set_cache(
        (GiveMeOCRecord("cached", "Insta360", "insta360", updated_at="2026-07-23", official_url="https://insta360.com/jobs"),),
        initialized=True,
    )
    result = crawler.crawl([Job(company="Insta360")], mode="daily")

    assert requested == ["https://www.givemeoc.com/", "https://www.givemeoc.com/?paged=2"]
    assert result.complete is False
    assert {record.source_record_id for record in result.records} == {"cached", "new", "newer"}
    assert match_givemeoc_record(Job(company="Insta360"), result.records).source_record_id == "newer"


def test_daily_givemeoc_scan_stops_at_the_first_fully_cached_page():
    requested = []

    class Response:
        text = '<a href="?paged=70">70</a><tr data-id="cached"><td class="crt-col-company">Example</td></tr>'

        def raise_for_status(self):
            return None

    def get(url, **_kwargs):
        requested.append(url)
        if len(requested) > 1:
            raise AssertionError("a fully cached page should end the incremental scan")
        return Response()

    crawler = GiveMeOCCrawler(
        {"givemeoc": {"max_pages_daily": 0}, "system_taxonomy": {"company_aliases": {}}},
        get=get,
        sleep=lambda _seconds: None,
    )
    crawler.set_cache((GiveMeOCRecord("cached", "Example", "example"),), initialized=True)

    result = crawler.crawl([Job(company="Example")], mode="daily")

    assert requested == ["https://www.givemeoc.com/"]
    assert result.complete is False
    assert result.pages_scanned == 1


def test_givemeoc_snapshot_is_persisted_and_survives_schema_reopen(tmp_path):
    repository = JobRepository(tmp_path / "jobs.sqlite")
    repository.init_schema()
    repository.save_givemeoc_records(
        [
            {
                "source_record_id": "batch-a",
                "company": "Insta360",
                "company_normalized": "insta360",
                "recruitment_type": "秋招",
                "official_url": "https://insta360.com/jobs",
            },
            {
                "source_record_id": "batch-b",
                "company": "Insta360",
                "company_normalized": "insta360",
                "recruitment_type": "实习",
            },
        ],
        complete=True,
        total_pages=70,
        pages_scanned=70,
    )

    repository.init_schema()
    assert repository.givemeoc_cache_initialized() is True
    records = repository.list_givemeoc_records()
    assert {record["source_record_id"] for record in records} == {"batch-a", "batch-b"}
    assert next(record for record in records if record["source_record_id"] == "batch-a")["official_url"] == (
        "https://insta360.com/jobs"
    )


def test_complete_givemeoc_snapshot_replaces_stale_records(tmp_path):
    repository = JobRepository(tmp_path / "jobs.sqlite")
    repository.init_schema()
    repository.save_givemeoc_records(
        [{"source_record_id": "stale", "company": "OldCo"}],
        complete=True,
        total_pages=40,
        pages_scanned=40,
    )
    repository.save_givemeoc_records(
        [{
            "source_record_id": "unmatched",
            "company": "NewCo",
            "company_normalized": "newco",
            "official_url": "https://newco.example/jobs",
        }],
        complete=True,
        total_pages=40,
        pages_scanned=40,
    )

    assert [row["source_record_id"] for row in repository.list_givemeoc_records()] == ["unmatched"]


def test_givemeoc_seed_builder_keeps_unmatched_crawler_records(tmp_path):
    repository = JobRepository(tmp_path / "jobs.sqlite")
    repository.init_schema()
    repository.save_givemeoc_records(
        [{
            "source_record_id": "unmatched",
            "company": "NewCo",
            "company_normalized": "newco",
            "announcement_url": "https://news.example/newco",
            "official_url": "https://newco.example/jobs",
        }],
        complete=True,
        total_pages=40,
        pages_scanned=40,
    )

    output = tmp_path / "givemeoc_seed.sqlite"
    assert build_givemeoc_seed(repository.db_path, output) == 1
    with sqlite3.connect(output) as connection:
        row = connection.execute(
            "SELECT company, announcement_url, official_url FROM givemeoc_records"
        ).fetchone()
    assert row == ("NewCo", "https://news.example/newco", "https://newco.example/jobs")


def test_unmatched_job_links_are_cleared_only_on_complete_snapshot():
    job = Job(
        company="Missing",
        announcement_url="https://mp.weixin.qq.com/s/old",
        announcement_url_source="givemeoc",
        official_url="https://careers.example/old",
        official_url_source="givemeoc",
        givemeoc_record_id="old",
    )
    partial = GiveMeOCCrawlResult((), 1, False)
    complete = GiveMeOCCrawlResult((), 2, True)

    apply_givemeoc_links([job], partial)
    assert job.official_url == "https://careers.example/old"
    apply_givemeoc_links([job], complete)
    assert job.official_url is None
    assert job.announcement_url is None
def test_givemeoc_scan_has_a_hard_page_limit():
    requested = []

    class Response:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

    def get(url, **_kwargs):
        requested.append(url)
        page = url.split("paged=")[-1] if "paged=" in url else "1"
        return Response(
            f'<a href="?paged=40">40</a><tr data-id="noise-{page}">'
            f'<td class="crt-col-company">Noise {page}</td></tr>'
        )

    result = GiveMeOCCrawler(
        {"givemeoc": {"max_pages_init": 999}, "system_taxonomy": {"company_aliases": {}}},
        get=get,
        sleep=lambda _seconds: None,
    ).crawl([Job(company="Missing Company")], mode="init")

    assert result.pages_scanned == 25
    assert len(requested) == 25
    assert result.complete is False
