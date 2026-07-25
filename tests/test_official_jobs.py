from dataclasses import replace
import base64
import html
import json

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from jobpicky.models import Job
from jobpicky.official_jobs import (
    OfficialJobCrawler,
    identify_official_platform,
    merge_official_jobs,
    parse_official_job,
)


def _detail(title="FPGA Engineer", company="Acme"):
    return f"""
    <script type="application/ld+json">
    {{
      "@context": "https://schema.org", "@type": "JobPosting",
      "title": "{title}", "description": "Build and verify FPGA designs with RTL, simulation, timing closure and board bring-up. Work with hardware and verification teams.",
      "datePosted": "2026-07-20", "validThrough": "2026-09-30",
      "identifier": {{"value": "fpga-42"}},
      "hiringOrganization": {{"name": "{company}"}},
      "jobLocation": {{"address": {{"addressLocality": "Shenzhen"}}}}
    }}
    </script>
    """


def test_identifies_common_recruiting_platforms():
    assert identify_official_platform("https://boards.greenhouse.io/acme/jobs/42") == "greenhouse"
    assert identify_official_platform("https://jobs.lever.co/acme/42") == "lever"
    assert identify_official_platform("https://acme.wd1.myworkdayjobs.com/en-US/jobs") == "workday"
    assert identify_official_platform("https://acme.zhiye.com/campus") == "zhiye"
    assert identify_official_platform("https://acme.jobs.feishu.cn/campus") == "feishu"
    assert identify_official_platform("https://app.mokahr.com/campus-recruitment/acme") == "mokahr"
    assert identify_official_platform("https://careers.acme.example/jobs") == "generic"


def test_parse_official_job_requires_concrete_detail_and_keeps_evidence():
    job = parse_official_job(
        _detail(),
        source_url="https://careers.acme.example/jobs",
        detail_url="https://careers.acme.example/jobs/fpga-42",
        company="Acme",
    )

    assert job is not None
    assert job.source == "official"
    assert job.source_job_id == "generic:fpga-42"
    assert job.detail_url.endswith("fpga-42")
    assert job.official_url == job.detail_url
    assert job.deadline == "2026-09-30"
    assert '"description"' in (job.field_evidence or "")
    assert job.positions[0].confidence == 0.95

    assert parse_official_job(
        _detail("Careers"),
        source_url="https://careers.acme.example/jobs",
        detail_url="https://careers.acme.example/jobs",
        company="Acme",
    ) is None


def test_crawler_discovers_detail_links_without_storing_listing_page():
    pages = {
        "https://jobs.lever.co/acme": '<a href="/acme/42">FPGA Engineer</a>',
        "https://jobs.lever.co/acme/42": _detail(company="Acme"),
    }

    class Response:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

    calls = []

    def get(url, **_kwargs):
        calls.append(url)
        return Response(pages[url])

    result = OfficialJobCrawler(
        {"official_jobs": {"max_details_per_source": 10}},
        get=get,
        sleep=lambda _: None,
    ).crawl([type("Record", (), {"company": "Acme", "official_url": "https://jobs.lever.co/acme"})()])

    assert len(result.jobs) == 1
    assert calls == ["https://jobs.lever.co/acme", "https://jobs.lever.co/acme/42"]


def test_crawler_parses_platform_api_job_payload_and_deduplicates_it():
    listing = "<html><body><div id='app'></div></body></html>"
    payload = {
        "data": {
            "job_post_list": [{
                "id": "feishu-42",
                "title": "FPGA Engineer",
                "description": "Build and verify FPGA designs with RTL, simulation, timing closure and board bring-up. " * 2,
                "location": "Shanghai",
            }]
        }
    }

    class Response:
        def __init__(self, text, data=None):
            self.text = text
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    def get(url, **_kwargs):
        if "/api/" in url:
            return Response("ignored", payload)
        return Response(listing)

    result = OfficialJobCrawler(
        {"official_jobs": {"max_details_per_source": 10}},
        get=get,
        sleep=lambda _: None,
    ).crawl([type("Record", (), {"company": "Acme", "official_url": "https://acme.jobs.feishu.cn/campus"})()])

    assert len(result.jobs) == 1
    assert result.jobs[0].source == "official"
    assert result.jobs[0].source_job_id == "feishu:feishu-42"
    assert result.jobs[0].detail_url == "https://acme.jobs.feishu.cn/position/feishu-42"
    assert result.details_checked == 1


def test_zhiye_crawler_posts_to_real_job_list_api_shape():
    listing = '<html><script>var x={"PortalId":"portal-42"}</script></html>'
    payload = {
        "Code": 200,
        "Data": [{
            "JobAdId": "230922194",
            "JobAdName": "招聘专员",
            "Duty": "负责招聘计划、简历筛选、面试安排和招聘流程优化。" * 4,
            "Require": "本科及以上学历，具备良好的沟通能力和执行力。" * 3,
            "LocNames": ["上海市"],
            "PostDate": "2026-07-22T09:37:42",
        }],
    }

    class Response:
        def __init__(self, text, data=None):
            self.text = text
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    def get(url, **_kwargs):
        return Response(listing)

    def post(url, **_kwargs):
        return Response("{}", payload)

    result = OfficialJobCrawler(
        {"official_jobs": {"max_details_per_source": 10}},
        get=get,
        post=post,
        sleep=lambda _: None,
    ).crawl([type("Record", (), {"company": "Acme", "official_url": "https://acme.zhiye.com/campus/jobs"})()])

    assert len(result.jobs) == 1
    assert result.jobs[0].source_job_id == "zhiye:230922194"
    assert result.jobs[0].city == "上海市"
    assert result.jobs[0].positions[0].extraction_version == "official-jsonld-v1"


def test_mokahr_crawler_decrypts_detail_api_and_uses_stable_job_id():
    key = "0123456789abcdef"
    iv = "abcdef0123456789"
    job_id = "job-42"
    init = {
        "org": {"id": "acme"},
        "siteId": "42953",
        "aesIv": iv,
        "jobs": [{"id": job_id, "title": "FPGA 实习生"}],
    }
    detail = {
        "code": 0,
        "data": {
            "id": job_id,
            "title": "FPGA 实习生",
            "jobDescription": "<p>负责 FPGA 逻辑设计、仿真验证、时序分析和板级联调。</p>" * 4,
            "locations": [{"cityName": "北京市"}],
            "publishedAt": "2026-07-20T00:00:00",
        },
    }
    raw = json.dumps(detail, ensure_ascii=False).encode()
    padder = padding.PKCS7(128).padder()
    padded = padder.update(raw) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).encryptor()
    encrypted = base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()
    api_payload = {"data": encrypted, "necromancer": key}
    listing = f'<input id="init-data" value="{html.escape(json.dumps(init, ensure_ascii=False))}">'

    class Response:
        def __init__(self, text, data=None):
            self.text = text
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    def get(url, **_kwargs):
        return Response(listing)

    def post(url, **_kwargs):
        return Response("ignored", api_payload)

    result = OfficialJobCrawler(
        {"official_jobs": {"max_details_per_source": 10}},
        get=get,
        post=post,
        sleep=lambda _: None,
    ).crawl([type("Record", (), {"company": "Acme", "official_url": "https://app.mokahr.com/campus-recruitment/acme/42953#/jobs"})()])

    assert len(result.jobs) == 1
    assert result.jobs[0].source_job_id == "mokahr:job-42"
    assert result.jobs[0].detail_url.endswith("#/job/job-42")
    assert "FPGA" in result.jobs[0].raw_text


def test_official_job_replaces_matching_wondercv_job_but_keeps_one_key():
    wondercv = Job(
        source="WonderCV",
        source_job_id="wonder-1",
        dedupe_key="WonderCV:id:wonder-1",
        company="Acme",
        title="FPGA Engineer",
        city="Shenzhen",
        summary="list card",
    )
    official = parse_official_job(
        _detail(),
        source_url="https://careers.acme.example/jobs",
        detail_url="https://careers.acme.example/jobs/fpga-42",
        company="Acme",
    )

    merged = merge_official_jobs([wondercv], [official])

    assert len(merged) == 1
    assert merged[0].source == "official"
    assert merged[0].dedupe_key == wondercv.dedupe_key
    assert merged[0].summary.startswith("Build and verify")


def test_repeated_refresh_updates_previously_merged_official_row_without_duplicate():
    previous = parse_official_job(
        _detail(),
        source_url="https://careers.acme.example/jobs",
        detail_url="https://careers.acme.example/jobs/fpga-42",
        company="Acme",
    )
    refreshed = parse_official_job(
        _detail("FPGA Engineer"),
        source_url="https://careers.acme.example/jobs",
        detail_url="https://careers.acme.example/jobs/fpga-42",
        company="Acme",
    )
    previous = replace(previous, dedupe_key="WonderCV:id:wonder-1")

    merged = merge_official_jobs([], [refreshed], [previous])

    assert len(merged) == 1
    assert merged[0].dedupe_key == "WonderCV:id:wonder-1"
