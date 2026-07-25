from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from typing import Callable
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .error_safety import safe_exception_detail
from .models import Job


GIVEMEOC_URL = "https://www.givemeoc.com/"

@dataclass(frozen=True, slots=True)
class GiveMeOCRecord:
    source_record_id: str
    company: str
    company_normalized: str
    recruitment_type: str = ""
    target_graduate_year: str = ""
    city: str = ""
    deadline: str = ""
    updated_at: str = ""
    announcement_url: str | None = None
    official_url: str | None = None


@dataclass(frozen=True, slots=True)
class GiveMeOCCrawlResult:
    records: tuple[GiveMeOCRecord, ...]
    pages_scanned: int
    complete: bool
    error: str | None = None
    total_pages: int = 0


def normalize_givemeoc_company(value: str | None) -> str:
    text = (value or "").strip().casefold()
    text = re.sub(r"[\s\u3000·•,，.。()（）【】\[\]（）\-—_]+", "", text)
    text = re.sub(
        r"^(?:北京|上海|深圳|广州|杭州|苏州|南京|成都|武汉|西安|重庆|天津|东莞|佛山|厦门|福州|合肥|济南|郑州|长沙|青岛|宁波|无锡|昆明|沈阳|大连|哈尔滨|长春|南昌|石家庄|贵阳|海口|珠海|惠州|中山|嘉兴|常州|绍兴|烟台|太原|南宁|兰州|南通|徐州)(?:市)?",
        "",
        text,
    )
    for suffix in ("股份有限公司", "有限责任公司", "有限公司", "集团公司", "集团"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    if text.endswith("科技") and len(text) > 2:
        text = text[:-2]
    text = re.sub(
        r"^(?:\u5317\u4eac|\u4e0a\u6d77|\u6df1\u5733|\u5e7f\u5dde|\u676d\u5dde|\u82cf\u5dde|\u5357\u4eac|\u6210\u90fd|\u6b66\u6c49|\u897f\u5b89|\u91cd\u5e86|\u5929\u6d25|\u4e1c\u839e|\u4f5b\u5c71|\u53a6\u95e8|\u798f\u5dde|\u5408\u80a5|\u6d4e\u5357|\u90d1\u5dde|\u957f\u6c99|\u9752\u5c9b|\u5b81\u6ce2|\u65e0\u9521|\u6606\u660e|\u6c88\u9633|\u5927\u8fde|\u54c8\u5c14\u6ee8|\u957f\u6625|\u5357\u660c|\u77f3\u5bb6\u5e84|\u8d35\u9633|\u6d77\u53e3|\u73e0\u6d77|\u60e0\u5dde|\u4e2d\u5c71|\u5609\u5174|\u5e38\u5dde|\u7ecd\u5174|\u70df\u53f0|\u592a\u539f|\u5357\u5b81|\u5170\u5dde|\u5357\u660c|\u5170\u5dde|\u5f90\u5dde)(?:\u5e02)?",
        "",
        text,
    )
    for suffix in ("\u80a1\u4efd\u6709\u9650\u516c\u53f8", "\u6709\u9650\u8d23\u4efb\u516c\u53f8", "\u6709\u9650\u516c\u53f8", "\u96c6\u56e2\u516c\u53f8", "\u96c6\u56e2"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    if text.endswith("\u79d1\u6280") and len(text) > 2:
        text = text[:-2]
    return text


def _company_key(value: str | None, aliases: dict[str, list[str]] | None) -> str:
    key = normalize_givemeoc_company(value)
    merged_aliases = {canonical: list(names) for canonical, names in (aliases or {}).items()}
    for canonical, names in merged_aliases.items():
        known = {normalize_givemeoc_company(canonical), *(normalize_givemeoc_company(item) for item in names)}
        if key in known:
            return normalize_givemeoc_company(canonical)
    return key


def givemeoc_company_key(value: str | None, aliases: dict[str, list[str]] | None = None) -> str:
    """Return the shared company key used by GiveMeOC link matching."""
    return _company_key(value, aliases)


def _company_match_score(
    left: str | None,
    right: str | None,
    aliases: dict[str, list[str]] | None = None,
) -> int:
    """Score a conservative company-name match for external enrichment.

    GiveMeOC often shortens legal names (for example “原力灵机” versus
    “北京原力灵机智能科技有限公司”). Exact normalized matches win; a
    containment fallback is only allowed for names of four or more characters
    so unrelated two/three-character brands are not merged accidentally.
    """
    left_key = _company_key(left, aliases)
    right_key = _company_key(right, aliases)
    if not left_key or not right_key:
        return 0
    if left_key == right_key:
        return 100
    shorter, longer = sorted((left_key, right_key), key=len)
    if len(shorter) >= 4 and shorter in longer:
        return 70
    return 0


def _external_url(value: str | None) -> str | None:
    if not value:
        return None
    url = value.strip()
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    host = (parts.hostname or "").casefold()
    path = parts.path.casefold()
    if host == "givemeoc.com" or host.endswith(".givemeoc.com"):
        return None
    if "/user/vip" in path or "/login" in path:
        return None
    return url


def _cell(row: BeautifulSoup, selector: str) -> str:
    node = row.select_one(selector)
    return node.get_text(" ", strip=True) if node else ""


def _link(row: BeautifulSoup, selector: str) -> str | None:
    node = row.select_one(selector)
    return _external_url(node.get("href")) if node else None


def parse_givemeoc_page(html: str, page_url: str = GIVEMEOC_URL) -> list[GiveMeOCRecord]:
    soup = BeautifulSoup(html, "html.parser")
    records: list[GiveMeOCRecord] = []
    for row in soup.select("tr[data-id]"):
        if row.select_one(".crt-honeypot-row") or row.get("aria-hidden") == "true":
            continue
        company = _cell(row, ".crt-col-company")
        source_record_id = str(row.get("data-id") or "").strip()
        if not company or not source_record_id:
            continue
        records.append(
            GiveMeOCRecord(
                source_record_id=source_record_id,
                company=company,
                company_normalized=normalize_givemeoc_company(company),
                recruitment_type=_cell(row, ".crt-col-recruitment-type"),
                target_graduate_year=_cell(row, ".crt-col-target"),
                city=_cell(row, ".crt-col-location"),
                deadline=_cell(row, ".crt-col-deadline"),
                updated_at=_cell(row, ".crt-col-update-time"),
                announcement_url=_link(row, ".crt-col-notice a"),
                official_url=_link(row, ".crt-col-links a"),
            )
        )
    return records


def extract_official_url_from_announcement(html: str, announcement_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    for node in soup.select("a[href]"):
        candidate = _external_url(urljoin(announcement_url, node.get("href") or ""))
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for text in soup.stripped_strings:
        for value in re.findall(r"https?://[^\s<>\"']+", text):
            candidate = _external_url(value.rstrip("，。；;）)"))
            if candidate and candidate not in candidates:
                candidates.append(candidate)
    if len(candidates) == 1:
        return candidates[0]
    def link_score(candidate: str) -> int:
        lowered = candidate.casefold()
        score = 0
        for keyword, points in (
            ("feishu", 8), ("campus", 6), ("career", 5),
            ("recruit", 5), ("apply", 5), ("job", 4),
            ("zhipin", 3), ("liepin", 3),
        ):
            if keyword in lowered:
                score += points
        return score

    preferred = [(link_score(candidate), index, candidate) for index, candidate in enumerate(candidates)]
    preferred = [item for item in preferred if item[0] > 0]
    if not preferred:
        return None
    return max(preferred, key=lambda item: (item[0], -item[1]))[2]


def _max_page(html: str, default: int = 1) -> int:
    pages = [
        int(value)
        for href in BeautifulSoup(html, "html.parser").select("a[href]")
        for value in parse_qs(urlsplit(urljoin(GIVEMEOC_URL, href.get("href") or "")).query).get("paged", [])
        if value.isdigit()
    ]
    return max(pages or [default])


class GiveMeOCCrawler:
    def __init__(
        self,
        config: dict,
        get: Callable[..., requests.Response] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        progress: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ):
        self.config = config
        self.get = get or requests.get
        self.sleep = sleep
        self.progress = progress or (lambda _message: None)
        self.cancel_check = cancel_check or (lambda: False)
        self.cached_records: tuple[GiveMeOCRecord, ...] = ()
        self.cache_initialized = False

    def set_cache(self, records: list[GiveMeOCRecord] | tuple[GiveMeOCRecord, ...], initialized: bool) -> None:
        self.cached_records = tuple(records)
        self.cache_initialized = initialized

    def crawl(self, jobs: list[Job], mode: str = "daily") -> GiveMeOCCrawlResult:
        target_keys = {
            _company_key(job.company, self.config.get("system_taxonomy", {}).get("company_aliases", {}))
            for job in jobs
            if job.company
        }
        if not target_keys:
            return GiveMeOCCrawlResult((), 0, True)

        records: dict[str, GiveMeOCRecord] = {
            record.source_record_id: record for record in self.cached_records
        }
        pages_scanned = 0
        total_pages = 1
        effective_mode = "init" if mode == "init" or not self.cache_initialized else mode
        stopped_on_cached_page = False
        try:
            page = 1
            while page <= min(total_pages, self._page_limit(effective_mode, total_pages)):
                if self.cancel_check():
                    return GiveMeOCCrawlResult(
                        tuple(records.values()), pages_scanned, False, "scan cancelled", total_pages
                    )
                page_url = GIVEMEOC_URL if page == 1 else f"{GIVEMEOC_URL}?paged={page}"
                self.progress("正在核对公司公告和官方投递入口")
                response = self.get(page_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
                response.raise_for_status()
                page_records = parse_givemeoc_page(response.text, page_url)
                pages_scanned += 1
                if page == 1:
                    total_pages = _max_page(response.text)
                cached_page = bool(page_records) and all(
                    record.source_record_id in records for record in page_records
                )
                for record in page_records:
                    record_key = _company_key(
                        record.company,
                        self.config.get("system_taxonomy", {}).get("company_aliases", {}),
                    )
                    target_record = any(
                        _company_match_score(job.company, record.company, self.config.get("system_taxonomy", {}).get("company_aliases", {}))
                        for job in jobs
                    )
                    if target_record and not record.official_url and record.announcement_url:
                        try:
                            notice = self.get(
                                record.announcement_url,
                                timeout=20,
                                headers={"User-Agent": "Mozilla/5.0"},
                            )
                            notice.raise_for_status()
                            official_url = extract_official_url_from_announcement(
                                notice.text, record.announcement_url,
                            )
                            if official_url:
                                record = replace(record, official_url=official_url)
                        except Exception:
                            pass
                    previous = records.get(record.source_record_id)
                    if previous:
                        record = replace(
                            record,
                            announcement_url=record.announcement_url or previous.announcement_url,
                            official_url=record.official_url or previous.official_url,
                        )
                    records[record.source_record_id] = record
                target_companies_covered = all(
                    any(
                        _company_match_score(
                            job.company,
                            record.company,
                            self.config.get("system_taxonomy", {}).get("company_aliases", {}),
                        )
                        for record in records.values()
                    )
                    for job in jobs
                )
                if (
                    effective_mode == "daily"
                    and self.config.get("givemeoc", {}).get("stop_when_page_cached", True)
                    and cached_page
                    and target_companies_covered
                ):
                    stopped_on_cached_page = True
                    break
                limit = self._page_limit(effective_mode, total_pages)
                if page >= min(total_pages, limit):
                    break
                self.sleep(float(self.config.get("givemeoc", {}).get("min_interval_seconds", 0)))
                page += 1
            complete = pages_scanned >= total_pages and not stopped_on_cached_page
            return GiveMeOCCrawlResult(tuple(records.values()), pages_scanned, complete, total_pages=total_pages)
        except Exception as exc:
            return GiveMeOCCrawlResult(
                tuple(records.values()), pages_scanned, False,
                f"page={page}: {safe_exception_detail(exc, self.config)}",
                total_pages,
            )

    def _page_limit(self, mode: str, discovered: int) -> int:
        configured = self.config.get("givemeoc", {}).get(
            "max_pages_init" if mode == "init" else "max_pages_daily"
        )
        return max(1, min(int(configured or discovered), discovered or 1))


def match_givemeoc_record(
    job: Job,
    records: tuple[GiveMeOCRecord, ...],
    aliases: dict[str, list[str]] | None = None,
    record_index: dict[str, tuple[GiveMeOCRecord, ...]] | None = None,
) -> GiveMeOCRecord | None:
    key = _company_key(job.company, aliases)
    candidates = list(record_index.get(key, ())) if record_index is not None else []
    if not candidates:
        candidates = [
            record for record in records
            if _company_match_score(job.company, record.company, aliases) > 0
        ]
    if not candidates:
        return None

    def score(record: GiveMeOCRecord) -> tuple[int, str, int]:
        value = _company_match_score(job.company, record.company, aliases)
        if job.batch and record.recruitment_type and job.batch in record.recruitment_type:
            value += 3
        if job.target_graduate_year and job.target_graduate_year in record.target_graduate_year:
            value += 2
        if job.city and record.city and any(city.strip() in record.city for city in job.city.split(";")):
            value += 1
        return value, record.updated_at, 1 if record.official_url else 0

    return max(candidates, key=score)


def build_givemeoc_record_index(
    records: tuple[GiveMeOCRecord, ...],
    aliases: dict[str, list[str]] | None = None,
) -> dict[str, tuple[GiveMeOCRecord, ...]]:
    index: dict[str, list[GiveMeOCRecord]] = {}
    for record in records:
        index.setdefault(_company_key(record.company, aliases), []).append(record)
    return {key: tuple(value) for key, value in index.items()}


def apply_givemeoc_links(
    jobs: list[Job],
    result: GiveMeOCCrawlResult,
    aliases: dict[str, list[str]] | None = None,
    record_index: dict[str, tuple[GiveMeOCRecord, ...]] | None = None,
) -> int:
    matched = 0
    for job in jobs:
        record = match_givemeoc_record(job, result.records, aliases, record_index)
        if record:
            matched += 1
            job.announcement_url = record.announcement_url
            job.official_url = record.official_url
            job.announcement_url_source = "givemeoc" if record.announcement_url else None
            job.official_url_source = "givemeoc" if record.official_url else None
            job.givemeoc_record_id = record.source_record_id
        elif result.complete:
            job.announcement_url = None
            job.official_url = None
            job.announcement_url_source = None
            job.official_url_source = None
            job.givemeoc_record_id = None
    return matched
