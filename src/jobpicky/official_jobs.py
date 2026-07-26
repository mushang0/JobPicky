from __future__ import annotations

import hashlib
import base64
from concurrent.futures import ThreadPoolExecutor
import html as html_lib
import json
import re
import time
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .models import Job, Position
from .normalizer import build_dedupe_key, infer_batch, infer_city, infer_graduate_year, normalize_company, normalize_date


_DETAIL_WORDS = re.compile(
    r"/(?:job|jobs|position|positions|opening|openings|vacancy|vacancies|requisition|opportunity)(?:/|[-_])"
    r"|[?&](?:job|jobid|jobId|position|positionId|reqid|requisition)(?:=|%3d)",
    re.I,
)
_GENERIC_TITLES = {"careers", "career", "jobs", "job openings", "招聘", "校园招聘", "加入我们"}


@dataclass(frozen=True, slots=True)
class OfficialCrawlResult:
    jobs: tuple[Job, ...]
    sources_checked: int
    details_checked: int
    rejected: int = 0
    errors: tuple[str, ...] = ()


def identify_official_platform(url: str | None) -> str | None:
    host = (urlsplit(url or "").hostname or "").casefold()
    if not host:
        return None
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if "myworkdayjobs.com" in host or host.endswith("workday.com"):
        return "workday"
    if "recruitee.com" in host:
        return "recruitee"
    if "zhiye.com" in host:
        return "zhiye"
    if host.endswith("feishu.cn") or ".feishu.cn" in host:
        return "feishu"
    if "mokahr.com" in host:
        return "mokahr"
    return "generic"


def _json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    postings: list[dict[str, Any]] = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.string or node.get_text())
        except (TypeError, ValueError):
            continue
        values = payload if isinstance(payload, list) else [payload]
        for value in values:
            if not isinstance(value, dict):
                continue
            graph = value.get("@graph") if isinstance(value.get("@graph"), list) else [value]
            postings.extend(item for item in graph if isinstance(item, dict) and item.get("@type") in ("JobPosting", ["JobPosting"]))
    return postings


def _text(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        node = soup.select_one(selector)
        value = node.get_text(" ", strip=True) if node else ""
        if value:
            return re.sub(r"\s+", " ", value)
    return ""


def _value(value: Any) -> str:
    if isinstance(value, dict):
        return str(
            value.get("name")
            or value.get("value")
            or value.get("cityName")
            or value.get("provinceName")
            or value.get("label")
            or value.get("addressLocality")
            or value.get("addressRegion")
            or value.get("streetAddress")
            or ""
        ).strip()
    if isinstance(value, list):
        return ";".join(part for part in (_value(item) for item in value) if part)
    return str(value or "").strip()


def _posting_data(soup: BeautifulSoup) -> dict[str, Any]:
    posting = _json_ld(soup)
    if posting:
        return posting[0]
    return {
        "title": _text(soup, ("[data-job-title]", "[data-automation-id=jobPostingHeader]", ".posting-headline h2", ".job__title", ".job-title", ".job-name", ".position-name", "h1", "title")),
        "description": _text(soup, ("[data-job-description]", "[data-automation-id=jobPostingDescription]", ".posting-description", ".job__description", ".job-description", ".job-detail", ".job-content", "#job-description", "main", "article")),
        "datePosted": _text(soup, ("[data-date-posted]", ".date-posted")),
        "validThrough": _text(soup, ("[data-valid-through]", ".deadline", ".closing-date")),
        "jobLocation": _text(soup, ("[data-job-location]", "[data-automation-id=locations]", ".job__location", ".job-location", ".job-address", ".location", "[class*=location]")),
    }


def _embedded_payloads(soup: BeautifulSoup) -> list[Any]:
    payloads: list[Any] = []
    for node in soup.select('script[type="application/json"], script[type="text/json"], script#__NEXT_DATA__, script#__NUXT_DATA__'):
        try:
            payloads.append(json.loads(node.string or node.get_text()))
        except (TypeError, ValueError):
            continue
    init_data = soup.select_one("#init-data")
    if init_data:
        raw = html_lib.unescape(str(init_data.get("value") or init_data.get_text() or "")).strip()
        if raw:
            try:
                payloads.append(json.loads(raw))
            except ValueError:
                pass
    return payloads


def _job_payloads(value: Any) -> list[dict[str, Any]]:
    """Find job-shaped records in common SSR/API response envelopes."""
    found: list[dict[str, Any]] = []
    title_keys = ("title", "job_title", "jobtitle", "position_name", "positionname", "job_name", "jobname", "jobadname", "name")
    description_keys = ("description", "job_description", "jobdescription", "job_detail", "jobdetail", "jobaddescription", "jobcontent", "duty", "require", "content", "responsibilities", "requirements", "body")

    def text_for(mapping: dict[str, Any], keys: tuple[str, ...]) -> str:
        normalized = {str(key).casefold().replace("-", "_"): item for key, item in mapping.items()}
        return next((_value(normalized[key]) for key in keys if normalized.get(key)), "")

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            title = text_for(item, title_keys)
            description = text_for(item, description_keys)
            if not description:
                parts = [text_for(item, (key,)) for key in ("duty", "require", "job_duty", "job_require")]
                description = "\n\n".join(part for part in parts if part)
            if title and description and len(description) >= 40:
                normalized = {str(key).casefold().replace("-", "_"): value for key, value in item.items()}
                found.append({
                    "title": title,
                    "description": description,
                    "identifier": normalized.get("id") or normalized.get("job_id") or normalized.get("jobadid") or normalized.get("job_ad_id") or normalized.get("position_id") or normalized.get("code") or normalized.get("requisition_id"),
                    "url": normalized.get("detail_url") or normalized.get("job_url") or normalized.get("position_url") or normalized.get("url") or normalized.get("link") or normalized.get("href"),
                    "jobLocation": normalized.get("location") or normalized.get("locations") or normalized.get("locnames") or normalized.get("detailaddress") or normalized.get("city") or normalized.get("job_location"),
                    "datePosted": normalized.get("date_posted") or normalized.get("dateposted") or normalized.get("postdate") or normalized.get("created_at"),
                    "validThrough": normalized.get("valid_through") or normalized.get("endtime") or normalized.get("deadline") or normalized.get("closing_date"),
                    "hiringOrganization": normalized.get("company") or normalized.get("company_name") or normalized.get("organization"),
                })
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item[:200]:
                walk(child)

    walk(value)
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in found:
        key = (item["title"], _value(item.get("identifier")) or item["description"][:80])
        unique[key] = item
    return list(unique.values())


def _api_paths(
    platform: str | None, source_url: str, limit: int, page_size: int = 50,
) -> tuple[str, ...]:
    base = f"{urlsplit(source_url).scheme}://{urlsplit(source_url).netloc}"
    page_size = max(1, min(page_size, limit))
    feishu_pages = tuple(
        f"{base}/api/v1/search/job/posts?keyword=&limit={page_size}&offset={offset}&portal_type=3"
        for offset in range(0, limit, page_size)
    )
    paths = {
        "feishu": feishu_pages,
        "zhiye": (f"{base}/api/position/list", f"{base}/api/job/list", f"{base}/api/jobs"),
        "mokahr": (f"{base}/api/position/list", f"{base}/api/job/list", f"{base}/api/jobs"),
    }
    return paths.get(platform or "", ())


def _api_job_url(data: dict[str, Any], source_url: str, platform: str, identifier: str) -> str:
    candidate = _value(data.get("url"))
    if candidate:
        return urljoin(source_url, candidate)
    if platform == "mokahr":
        return f"{source_url.split('#', 1)[0]}#/job/{identifier}"
    path = {
        "feishu": f"/position/{identifier}",
        "zhiye": f"/campus/job/{identifier}",
    }.get(platform, f"/jobs/{identifier}")
    return urljoin(source_url, path)


def _page_context(html: str, platform: str | None) -> dict[str, Any]:
    if platform != "mokahr":
        return {}
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("#init-data")
    if not node:
        return {}
    raw = html_lib.unescape(str(node.get("value") or "")).strip()
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _decrypt_mokahr_payload(payload: Any, context: dict[str, Any]) -> Any:
    if not isinstance(payload, dict) or not payload.get("data") or not payload.get("necromancer"):
        return payload
    iv = str(context.get("aesIv") or "")
    key = str(payload.get("necromancer") or "")
    if len(iv) != 16 or len(key) not in {16, 24, 32}:
        return payload
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding

        decryptor = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).decryptor()
        raw = decryptor.update(base64.b64decode(payload["data"])) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return json.loads((unpadder.update(raw) + unpadder.finalize()).decode("utf-8"))
    except (ImportError, ValueError, TypeError, json.JSONDecodeError):
        return payload


def _job_from_payload(
    data: dict[str, Any],
    *,
    source_url: str,
    company: str,
    platform: str,
    aliases: dict[str, list[str]] | None,
) -> Job | None:
    identifier = _value(data.get("identifier"))
    if not identifier:
        return None
    detail_url = _api_job_url(data, source_url, platform, identifier)
    data = dict(data)
    if "<" in _value(data.get("description")):
        data["description"] = BeautifulSoup(_value(data.get("description")), "html.parser").get_text(" ", strip=True)
    data["@type"] = "JobPosting"
    data.setdefault("hiringOrganization", {"name": company})
    data["identifier"] = {"value": identifier}
    data["url"] = detail_url
    html = f'<script type="application/ld+json">{json.dumps(data, ensure_ascii=False)}</script>'
    return parse_official_job(
        html,
        source_url=source_url,
        detail_url=detail_url,
        company=company,
        aliases=aliases,
        platform=platform,
    )


def _company_name(data: dict[str, Any], fallback: str) -> str:
    hiring = data.get("hiringOrganization")
    return _value(hiring) or fallback.strip()


def _location_value(value: Any) -> str:
    if isinstance(value, dict):
        return _value(value) or _location_value(value.get("address"))
    if isinstance(value, list):
        return ";".join(part for part in (_location_value(item) for item in value) if part)
    return _value(value)


def _identifier(data: dict[str, Any], detail_url: str, platform: str) -> str:
    identifier = data.get("identifier")
    value = _value(identifier)
    if value:
        return f"{platform}:{value}"
    return f"{platform}:url:{hashlib.sha1(detail_url.encode()).hexdigest()[:20]}"


def _company_matches(actual: str, expected: str, aliases: dict[str, list[str]] | None) -> bool:
    left = normalize_company(actual, aliases).casefold()
    right = normalize_company(expected, aliases).casefold()
    compact = lambda value: re.sub(r"[^\w\u4e00-\u9fff]+", "", value)
    left, right = compact(left), compact(right)
    return bool(left and right and (left == right or left in right or right in left))


def _valid_detail(title: str, description: str, detail_url: str, source_url: str) -> bool:
    return bool(
        title
        and title.casefold() not in _GENERIC_TITLES
        and detail_url != source_url
        and len(description.strip()) >= 80
    )


def parse_official_job(
    html: str,
    *,
    source_url: str,
    detail_url: str,
    company: str,
    aliases: dict[str, list[str]] | None = None,
    platform: str | None = None,
) -> Job | None:
    soup = BeautifulSoup(html, "html.parser")
    data = _posting_data(soup)
    title = _value(data.get("title"))
    description = _value(data.get("description"))
    actual_company = _company_name(data, company)
    if not _valid_detail(title, description, detail_url, source_url) or not _company_matches(actual_company, company, aliases):
        return None
    location = _location_value(data.get("jobLocation"))
    city = infer_city(location) or location or None
    text = re.sub(r"\s+", " ", description).strip()
    detail_id = _identifier(data, detail_url, platform or identify_official_platform(detail_url) or "generic")
    company_normalized = normalize_company(actual_company, aliases)
    collected_date = normalize_date(_value(data.get("datePosted"))) or date.today().isoformat()
    deadline = normalize_date(_value(data.get("validThrough")))
    field_evidence = {
        "title": {"source": "jsonld" if _json_ld(soup) else "html", "evidence": title[:240]},
        "company": {"source": "jsonld" if data.get("hiringOrganization") else "givemeoc", "evidence": actual_company[:240]},
        "city": {"source": "jsonld" if data.get("jobLocation") else "html", "evidence": location[:240]},
        "deadline": {"source": "jsonld" if data.get("validThrough") else "html", "evidence": _value(data.get("validThrough"))[:240]},
        "description": {"source": "jsonld" if data.get("description") else "html", "evidence": text[:240]},
    }
    position = Position(
        title=title,
        position_key=detail_id,
        city=city,
        location_status="confirmed" if city else "pending",
        responsibilities=text,
        requirements=text,
        source_text=text[:2000],
        field_evidence=field_evidence,
        confidence=0.95 if _json_ld(soup) else 0.72,
        extraction_version="official-jsonld-v1" if _json_ld(soup) else "official-html-v1",
    )
    role_signals = [term for term in ("FPGA", "Verilog", "嵌入式", "硬件", "芯片", "算法", "软件", "测试") if term.casefold() in f"{title} {text}".casefold()]
    return Job(
        source="official",
        source_job_id=detail_id,
        source_url=source_url,
        detail_url=detail_url,
        dedupe_key=build_dedupe_key(
            source="official",
            source_job_id=detail_id,
            detail_url=detail_url,
            company_normalized=company_normalized,
            title=title,
            batch=infer_batch(f"{title} {text}"),
            collected_date=collected_date,
        ),
        company=actual_company,
        raw_company=actual_company,
        company_normalized=company_normalized,
        title=title,
        raw_title=title,
        clean_title=title,
        summary=text[:500],
        batch=infer_batch(f"{title} {text}"),
        target_graduate_year=infer_graduate_year(f"{title} {text}"),
        city=city,
        location_text=location or city,
        location_status="confirmed" if city else "pending",
        collected_date=collected_date,
        deadline=deadline,
        raw_text=text,
        role_text=text,
        role_signals=role_signals,
        field_evidence=json.dumps(field_evidence, ensure_ascii=False, sort_keys=True),
        extraction_version=position.extraction_version,
        apply_url=detail_url,
        official_url=detail_url,
        official_url_source="official",
        parse_status="detail_ready",
        parse_note="",
        positions=[position],
    )


class OfficialJobCrawler:
    def __init__(
        self,
        config: dict | None = None,
        get: Callable[..., requests.Response] | None = None,
        post: Callable[..., requests.Response] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        progress: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ):
        self.config = config or {}
        self.get = get or requests.get
        self.post = post or requests.post
        self.sleep = sleep
        self.progress = progress or (lambda _message: None)
        self.cancel_check = cancel_check or (lambda: False)

    def crawl(self, records: Iterable[Any]) -> OfficialCrawlResult:
        records = self._unique_records(records)
        aliases = self.config.get("system_taxonomy", {}).get("company_aliases", {})
        official_config = self.config.get("official_jobs", {})
        limit = max(1, int(official_config.get(
            "max_jobs_per_source", official_config.get("max_details_per_source", 50),
        )))
        configured_workers = int(self.config.get("official_jobs", {}).get("max_workers", 8) or 8)
        max_workers = max(1, min(configured_workers, 10))
        if max_workers > 1 and len(records) > 1:
            return self._crawl_parallel(records, aliases, limit, max_workers)
        jobs: list[Job] = []
        errors: list[str] = []
        sources_checked = details_checked = rejected = 0
        total_records = len(records)
        for index, record in enumerate(records, start=1):
            if self.cancel_check():
                break
            source_url = str(getattr(record, "official_url", "") or "").strip()
            company = str(getattr(record, "company", "") or "").strip()
            if not source_url or not company:
                continue
            sources_checked += 1
            self.progress(f"官方岗位补充 {index}/{total_records}: {company} ({identify_official_platform(source_url) or 'generic'})")
            try:
                response = self.get(source_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")
                platform = identify_official_platform(source_url)
                dynamic_jobs = self._dynamic_jobs(response.text, source_url, company, platform, aliases, limit)
                jobs.extend(dynamic_jobs)
                details_checked += len(dynamic_jobs)
                links = [] if dynamic_jobs else ([source_url] if _json_ld(soup) else self._detail_links(soup, source_url, platform, limit))
                detail_links = self._unique_urls(links)[:limit]
                for detail_url in detail_links:
                    if self.cancel_check():
                        break
                    details_checked += 1
                    html = response.text if detail_url == source_url else self._fetch(detail_url)
                    if html is None:
                        rejected += 1
                        continue
                    job = parse_official_job(
                        html,
                        source_url=source_url,
                        detail_url=detail_url,
                        company=company,
                        aliases=aliases,
                        platform=platform,
                    )
                    if job:
                        jobs.append(job)
                    else:
                        rejected += 1
                    if detail_url != detail_links[-1]:
                        self.sleep(float(self.config.get("official_jobs", {}).get("min_interval_seconds", 0)))
            except Exception as exc:
                errors.append(f"{company}: {type(exc).__name__}")
            self.progress(
                f"官方岗位补充 {index}/{total_records}: {company} 完成，累计岗位 {len(jobs)}"
            )
        unique = {job.dedupe_key or job.detail_url: job for job in jobs}
        return OfficialCrawlResult(tuple(unique.values()), sources_checked, details_checked, rejected, tuple(errors))

    @staticmethod
    def _unique_records(records: Iterable[Any]) -> tuple[Any, ...]:
        unique: list[Any] = []
        seen_sources: set[str] = set()
        for record in records:
            source_key = str(getattr(record, "official_url", "") or "").strip().rstrip("/").casefold()
            if not source_key or source_key in seen_sources:
                continue
            seen_sources.add(source_key)
            unique.append(record)
        return tuple(unique)

    @staticmethod
    def _unique_urls(urls: Iterable[str]) -> tuple[str, ...]:
        unique: list[str] = []
        seen: set[str] = set()
        for url in urls:
            normalized = str(url or "").strip()
            key = normalized.rstrip("/").casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(normalized)
        return tuple(unique)

    def _crawl_parallel(
        self,
        records: tuple[Any, ...],
        aliases: dict[str, list[str]] | None,
        limit: int,
        max_workers: int,
    ) -> OfficialCrawlResult:
        """Fetch different official sources concurrently, one source at a time.

        The worker boundary is the official source URL.  Detail links and
        platform API calls inside one source remain serial, preserving the
        existing request interval and avoiding duplicate source requests.
        """
        total_records = len(records)
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="official-job") as executor:
            results = list(
                executor.map(
                    lambda item: self._crawl_one_source(item[1], item[0], total_records, aliases, limit),
                    enumerate(records, start=1),
                )
            )
        jobs = [job for result in results for job in result[0]]
        unique = {job.dedupe_key or job.detail_url: job for job in jobs}
        return OfficialCrawlResult(
            tuple(unique.values()),
            sum(result[1] for result in results),
            sum(result[2] for result in results),
            sum(result[3] for result in results),
            tuple(error for result in results for error in result[4]),
        )

    def _crawl_one_source(
        self,
        record: Any,
        index: int,
        total_records: int,
        aliases: dict[str, list[str]] | None,
        limit: int,
    ) -> tuple[list[Job], int, int, int, list[str]]:
        if self.cancel_check():
            return [], 0, 0, 0, []
        source_url = str(getattr(record, "official_url", "") or "").strip()
        company = str(getattr(record, "company", "") or "").strip()
        if not source_url or not company:
            return [], 0, 0, 0, []
        self.progress(f"官方岗位补充 {index}/{total_records}: {company} ({identify_official_platform(source_url) or 'generic'})")
        jobs: list[Job] = []
        errors: list[str] = []
        details_checked = rejected = 0
        try:
            response = self.get(source_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            platform = identify_official_platform(source_url)
            dynamic_jobs = self._dynamic_jobs(response.text, source_url, company, platform, aliases, limit)
            jobs.extend(dynamic_jobs)
            details_checked += len(dynamic_jobs)
            links = [] if dynamic_jobs else ([source_url] if _json_ld(soup) else self._detail_links(soup, source_url, platform, limit))
            detail_links = self._unique_urls(links)[:limit]
            for detail_index, detail_url in enumerate(detail_links):
                if self.cancel_check():
                    break
                details_checked += 1
                html = response.text if detail_url == source_url else self._fetch(detail_url)
                if html is None:
                    rejected += 1
                    continue
                job = parse_official_job(
                    html,
                    source_url=source_url,
                    detail_url=detail_url,
                    company=company,
                    aliases=aliases,
                    platform=platform,
                )
                if job:
                    jobs.append(job)
                else:
                    rejected += 1
                if detail_index < len(detail_links) - 1:
                    self.sleep(float(self.config.get("official_jobs", {}).get("min_interval_seconds", 0)))
        except Exception as exc:
            errors.append(f"{company}: {type(exc).__name__}")
        self.progress(
            f"官方岗位补充 {index}/{total_records}: {company} 完成，累计岗位 {len(jobs)}"
        )
        return jobs, 1, details_checked, rejected, errors

    def _dynamic_jobs(
        self,
        html: str,
        source_url: str,
        company: str,
        platform: str | None,
        aliases: dict[str, list[str]] | None,
        limit: int,
    ) -> list[Job]:
        payloads = _embedded_payloads(BeautifulSoup(html, "html.parser"))
        context = _page_context(html, platform)
        official_config = self.config.get("official_jobs", {})
        page_size = max(1, min(
            int(official_config.get("page_size", 50) or 50), limit,
        ))
        if platform == "zhiye":
            endpoint = f"{urlsplit(source_url).scheme}://{urlsplit(source_url).netloc}/api/Jobad/GetJobAdPageList"
            for page_index in range((limit + page_size - 1) // page_size):
                payload = self._fetch_json(
                    endpoint,
                    source_url,
                    method="POST",
                    json_body={
                        "PageIndex": page_index,
                        "PageSize": page_size,
                        "KeyWords": "",
                        "SpecialType": 0,
                        "PortalId": self._zhiye_portal_id(html),
                        "DisplayFields": ["Category"],
                    },
                )
                if payload is None:
                    break
                payloads.append(payload)
                if len(_job_payloads(payload)) < page_size:
                    break
        if platform == "mokahr":
            init_jobs = next((item.get("jobs") for item in payloads if isinstance(item, dict) and isinstance(item.get("jobs"), list)), [])
            for item in init_jobs[:limit]:
                identifier = _value(item.get("id")) if isinstance(item, dict) else ""
                if not identifier:
                    continue
                payload = self._fetch_json(
                    f"{urlsplit(source_url).scheme}://{urlsplit(source_url).netloc}/api/outer/ats-apply/website/job",
                    source_url,
                    method="POST",
                    json_body={
                        "siteId": context.get("siteId"),
                        "orgId": (context.get("org") or {}).get("id") if isinstance(context.get("org"), dict) else "",
                        "jobId": identifier,
                        "isInviteResume": False,
                        "locale": "zh-CN",
                    },
                )
                if payload is not None:
                    payloads.append(_decrypt_mokahr_payload(payload, context))
        if platform not in {"zhiye", "mokahr"}:
            for path in _api_paths(platform, source_url, limit, page_size):
                payload = self._fetch_json(path, source_url)
                if payload is not None:
                    payloads.append(payload)
                    if platform == "feishu" and len(_job_payloads(payload)) < page_size:
                        break
        jobs: list[Job] = []
        for payload in payloads:
            for data in _job_payloads(payload):
                if len(jobs) >= limit:
                    return jobs
                job = _job_from_payload(
                    data,
                    source_url=source_url,
                    company=company,
                    platform=platform or "generic",
                    aliases=aliases,
                )
                if job:
                    jobs.append(job)
        return jobs

    @staticmethod
    def _zhiye_portal_id(html: str) -> str:
        match = re.search(r'PortalId\\?":\\?"([^"\\]+)', html, re.I)
        return match.group(1) if match else ""

    def _fetch_json(
        self,
        url: str,
        referer: str,
        *,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
    ) -> Any | None:
        try:
            request = self.post if method.upper() == "POST" else self.get
            kwargs: dict[str, Any] = {
                "timeout": 20,
                "headers": {
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json",
                    "Referer": referer,
                    "X-Requested-With": "XMLHttpRequest",
                },
            }
            if json_body is not None:
                kwargs["json"] = json_body
            response = request(url, **kwargs)
            response.raise_for_status()
            text = str(getattr(response, "text", "") or "").strip()
            if not text or text.startswith("<"):
                return None
            return response.json() if hasattr(response, "json") else json.loads(text)
        except Exception:
            return None

    def _fetch(self, url: str) -> str | None:
        try:
            response = self.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            return response.text
        except Exception:
            return None

    @staticmethod
    def _detail_links(soup: BeautifulSoup, source_url: str, platform: str | None, limit: int) -> list[str]:
        source = urlsplit(source_url)
        links: list[str] = []
        for node in soup.select("a[href]"):
            href = urljoin(source_url, node.get("href") or "")
            parts = urlsplit(href)
            if parts.scheme not in {"http", "https"} or parts.netloc != source.netloc:
                continue
            if href.rstrip("/") == source_url.rstrip("/") or href in links:
                continue
            text = node.get_text(" ", strip=True)
            if _DETAIL_WORDS.search(parts.path) or re.search(
                r"apply|投递|职位|岗位|详情|申请|应聘|立即申请|查看职位|engineer|developer|job",
                text,
                re.I,
            ):
                links.append(href)
            if len(links) >= limit:
                break
        return links


def _title_key(value: str | None) -> str:
    text = (value or "").casefold()
    text = re.sub(r"校招|校园招聘|社会招聘|招聘|应届|实习|全职|兼职", "", text)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text)


def _locations_compatible(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return True
    lhs = {item for item in re.split(r"[;,，、/ ]+", left) if item}
    rhs = {item for item in re.split(r"[;,，、/ ]+", right) if item}
    return bool(lhs & rhs)


def merge_official_jobs(
    wondercv_jobs: Iterable[Job],
    official_jobs: Iterable[Job],
    existing_jobs: Iterable[Job] = (),
    aliases: dict[str, list[str]] | None = None,
) -> list[Job]:
    """Attach official positions to WonderCV announcements without creating jobs.

    WonderCV is the discovery baseline.  Official sources may improve the
    positions beneath an existing announcement, but an unmatched official
    company must never become a new card in the library.
    """
    result = list(wondercv_jobs)
    candidates = [
        job for job in [*result, *existing_jobs]
        if str(job.source or "").casefold() != "official"
    ]
    by_key = {job.dedupe_key: index for index, job in enumerate(result) if job.dedupe_key}
    by_givemeoc_record: dict[str, list[Job]] = {}
    for candidate in candidates:
        if candidate.givemeoc_record_id:
            by_givemeoc_record.setdefault(candidate.givemeoc_record_id, []).append(candidate)

    def target_for(official: Job) -> Job | None:
        if official.givemeoc_record_id:
            matched = by_givemeoc_record.get(official.givemeoc_record_id, ())
            if matched:
                return matched[0]
        return next((
            candidate for candidate in candidates
            if _company_matches(official.company, candidate.company, aliases)
            and _locations_compatible(official.city, candidate.city)
        ), None)

    def merged_positions(current: list[Position], incoming: list[Position]) -> list[Position]:
        positions: dict[str, Position] = {}
        for position in [*current, *incoming]:
            key = (position.position_key or _title_key(position.title)).casefold()
            positions.setdefault(key or str(len(positions)), position)
        return list(positions.values())

    merged_targets: dict[str, Job] = {}

    for official in official_jobs:
        match = target_for(official)
        if match is None or not match.dedupe_key:
            continue
        current = merged_targets.get(match.dedupe_key, match)
        merged = replace(
            current,
            positions=merged_positions(current.positions or [], official.positions or []),
        )
        merged_targets[match.dedupe_key] = merged
        if match.dedupe_key in by_key:
            result[by_key[match.dedupe_key]] = merged
        else:
            result.append(merged)
            by_key[match.dedupe_key] = len(result) - 1
    return result
