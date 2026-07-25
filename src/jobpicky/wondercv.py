from __future__ import annotations

import hashlib
import json
import logging
from html.parser import HTMLParser
import random
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .models import Job, Position
from .normalizer import build_dedupe_key, infer_batch, infer_city, infer_graduate_year, normalize_company, normalize_date
from .error_safety import safe_exception_detail
from .taxonomy import infer_role_direction, role_signal_terms


WONDERCV_URL = "https://www.wondercv.com/xiaozhao/"
EXTRACTION_VERSION = "detail-structure-v3"


def _print_progress(message: str) -> None:
    print(message, flush=True)


@dataclass(frozen=True, slots=True)
class CrawlResult:
    jobs: list[Job]
    pages_scanned: int
    partial: bool = False
    error: str | None = None
    interrupted: bool = False
    sources_attempted: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0


class ScanInterrupted(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DetailParseResult:
    raw_text: str
    apply_url: str | None = None
    keywords: list[str] | None = None
    summary: str = ""
    city: str | None = None
    degree: str | None = None
    deadline: str | None = None
    batch: str | None = None
    graduate_year: str | None = None
    role_text: str = ""
    announcement_text: str = ""
    role_signals: list[str] | None = None
    field_evidence: dict[str, dict[str, object]] | None = None
    positions: list[Position] | None = None


def parse_wondercv_list(html: str, page_url: str, aliases: dict[str, list[str]] | None = None) -> list[Job]:
    text = _strip_tags(html)
    if ("验证码" in text or "请登录" in text) and len(text) < 1000:
        raise RuntimeError("WonderCV 公开页面受限，出现登录或验证码提示")

    cards = _CardParser.parse(html)
    jobs: list[Job] = []
    seen_urls: set[str] = set()
    for card in cards:
        detail_url = urljoin(page_url, card.href)
        source_job_id = _extract_job_id(detail_url)
        if not source_job_id:
            continue
        if detail_url in seen_urls:
            continue
        seen_urls.add(detail_url)

        raw_title = _clean(card.title or card.text)
        if not raw_title or len(raw_title) < 4:
            continue

        parsed = _parse_card_text(raw_title)
        title = parsed["clean_title"]
        company = card.company or parsed["company"] or _infer_company(title)
        city = card.city or infer_city(raw_title)
        # WonderCV currently uses ``card-date`` but the visible card text is a
        # stable fallback when the site's presentation class changes again.
        date_text = card.date or raw_title
        tags = card.tags or parsed["raw_tags"]
        company_normalized = normalize_company(company, aliases)
        collected_date = normalize_date(date_text)
        batch = infer_batch(raw_title)
        dedupe_key = build_dedupe_key(
            source="WonderCV",
            source_job_id=source_job_id,
            detail_url=detail_url,
            company_normalized=company_normalized,
            title=title,
            batch=batch,
            collected_date=collected_date,
        )
        jobs.append(
            Job(
                source="WonderCV",
                source_job_id=source_job_id,
                source_url=page_url,
                detail_url=detail_url,
                dedupe_key=dedupe_key,
                company=company,
                raw_company=company,
                company_normalized=company_normalized,
                title=title,
                raw_title=raw_title,
                clean_title=title,
                summary=_concise_card_summary(str(parsed["summary"] or "")),
                batch=batch,
                target_graduate_year=infer_graduate_year(raw_title),
                city=city,
                location_text=city,
                collected_date=collected_date,
                company_type=parsed["company_type"],
                industry=parsed["industry"],
                tags=tags,
                job_tags=parsed["job_tags"],
                special_marks=parsed["special_marks"],
                raw_tags=parsed["raw_tags"],
                raw_text=raw_title,
                parse_status="list_only",
                parse_note="" if title else "clean title missing",
            )
        )
    return jobs


def parse_wondercv_detail(html: str) -> DetailParseResult:
    html_without_noise = _remove_noise_blocks(html)
    text = _trim_detail_tail(_focus_detail_body(_strip_tags(html_without_noise)))
    important_text = _extract_detail_signal_text(text)
    role_text = important_text or ""
    keywords = _extract_detail_keywords(role_text)
    apply_url = _extract_apply_url(html)
    summary_source = _clean(f"{text[:220]} {important_text}") if important_text else text
    summary = _clean(summary_source[:500])
    city = infer_city(role_text or text)
    degree = _extract_degree(role_text or text)
    deadline = _extract_deadline(text)
    batch = infer_batch(text)
    graduate_year = infer_graduate_year(text)
    evidence = _field_evidence(
        city=city,
        degree=degree,
        deadline=deadline,
        batch=batch,
        graduate_year=graduate_year,
        role_text=role_text,
    )
    positions = _extract_detail_positions(html_without_noise, role_text)
    return DetailParseResult(
        raw_text=text,
        apply_url=apply_url,
        keywords=keywords,
        summary=summary,
        city=city,
        degree=degree,
        deadline=deadline,
        batch=batch,
        graduate_year=graduate_year,
        role_text=role_text,
        announcement_text=text,
        role_signals=keywords,
        field_evidence=evidence,
        positions=positions,
    )


def extract_wondercv_card_summary(raw_title: str) -> str:
    """Recover the concise list-card copy kept in WonderCV discovery text."""
    if not _clean(raw_title):
        return ""
    return _concise_card_summary(str(_parse_card_text(_clean(raw_title))["summary"] or ""))


def _concise_card_summary(value: str, limit: int = 96) -> str:
    text = _clean(value)
    if len(text) <= limit:
        return text
    sentences = [part for part in re.findall(r".+?(?:[。！？]|$)", text) if part]
    selected: list[str] = []
    for sentence in sentences[:2]:
        if len("".join(selected)) + len(sentence) <= limit:
            selected.append(sentence)
        else:
            break
    if selected:
        return "".join(selected)
    clauses = [part for part in re.findall(r".+?(?:[，；、]|$)", text) if part]
    for clause in clauses:
        if len("".join(selected)) + len(clause) > limit:
            break
        selected.append(clause)
    concise = "".join(selected).rstrip("，；、 ")
    return f"{concise}。" if concise else text[: limit - 1].rstrip("，；、 ") + "。"


class WonderCVCrawler:
    def __init__(
        self,
        config: dict,
        get: Callable[..., requests.Response] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        progress: Callable[[str], None] = _print_progress,
        cancel_check: Callable[[], bool] | None = None,
    ):
        self.config = config
        self.get = get or requests.get
        self.sleep = sleep
        self.progress = progress
        self.cancel_check = cancel_check or (lambda: False)
        self.aliases = config.get("system_taxonomy", {}).get("company_aliases", {})

    def crawl(self, mode: str = "daily", should_stop: Callable[[list[Job]], bool] | None = None) -> CrawlResult:
        jobs: list[Job] = []
        pages_scanned = 0
        try:
            for page_jobs in self.crawl_pages(mode, should_stop=should_stop):
                pages_scanned += 1
                jobs.extend(page_jobs)
            return CrawlResult(
                jobs=jobs,
                pages_scanned=pages_scanned,
                sources_attempted=1,
                sources_succeeded=1,
            )
        except Exception as exc:
            if isinstance(exc, ScanInterrupted):
                return CrawlResult(jobs=jobs, pages_scanned=pages_scanned, error="日常扫描已中断", interrupted=True)
            return CrawlResult(
                jobs=jobs,
                pages_scanned=pages_scanned,
                partial=bool(jobs),
                error=safe_exception_detail(exc, self.config),
                sources_attempted=1,
                sources_succeeded=int(bool(pages_scanned or jobs)),
                sources_failed=1,
            )

    def crawl_pages(self, mode: str = "daily", should_stop: Callable[[list[Job]], bool] | None = None):
        crawler_config = self.config.get("crawler", {})
        max_pages = int(crawler_config.get("max_pages_init" if mode == "init" else "max_pages_daily", 20))
        for page in range(1, max_pages + 1):
            self._ensure_not_cancelled()
            self.progress(f"正在查找第 {page}/{max_pages} 页的新岗位")
            page_url = self._page_url(page)
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                }
                response = self.get(page_url, timeout=20, headers=headers)
                response.raise_for_status()
            except Exception as exc:
                logging.error("Failed to fetch page %s: %s", page, safe_exception_detail(exc, self.config))
                safe_error = safe_exception_detail(exc, self.config)
                raise RuntimeError(f"WonderCV 第 {page} 页抓取失败：{safe_error}") from None
            page_jobs = parse_wondercv_list(response.text, page_url, self.aliases)
            self._ensure_not_cancelled()
            if not page_jobs:
                self.progress(f"第 {page} 页没有发现新岗位，正在结束扫描")
                break
            # The stop rule only depends on list-page fields.  Evaluate it before
            # detail requests so a fully known page does not spend minutes
            # re-fetching detail pages which will be discarded anyway.
            if should_stop and should_stop(page_jobs):
                self.progress(f"第 {page} 页岗位均已整理，已获取到最新位置")
                break
            if self._enrich_details_enabled():
                self.progress(f"第 {page} 页发现 {len(page_jobs)} 条招聘公告，正在读取详情")
                enriched_jobs: list[Job] = []
                for index, job in enumerate(page_jobs, start=1):
                    self._ensure_not_cancelled()
                    company = _clean(job.company or job.title or "新公司")[:28]
                    self.progress(f"发现新公司「{company}」，正在获取招聘公告（{index}/{len(page_jobs)}）")
                    enriched = self.enrich_detail(job)
                    self._ensure_not_cancelled()
                    if enriched.parse_status == "detail_ready":
                        count = len(enriched.positions)
                        detail = f"发现 {count} 个明确岗位" if count else "公告未明确列出岗位名称"
                        self.progress(f"已读取「{company}」招聘详情，{detail}")
                    else:
                        self.progress(f"「{company}」详情暂未完整，已保留招聘公告")
                    enriched_jobs.append(enriched)
                page_jobs = enriched_jobs
            else:
                self.progress(f"第 {page} 页发现 {len(page_jobs)} 条招聘公告")
            yield page_jobs

    def _ensure_not_cancelled(self) -> None:
        if self.cancel_check():
            raise ScanInterrupted()
            self._pause()

    def enrich_detail(self, job: Job) -> Job:
        if not job.detail_url:
            return job
        try:
            timeout = float(self.config.get("crawler", {}).get("detail_timeout_seconds", 20))
            response = self.get(job.detail_url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            detail = parse_wondercv_detail(response.text)
        except Exception as exc:
            job.parse_status = "detail_failed"
            note = f"detail fetch failed: {exc}"
            job.parse_note = "; ".join(part for part in [job.parse_note, note] if part)
            return job
        if not detail.raw_text:
            job.parse_status = "detail_failed"
            job.parse_note = "; ".join(part for part in [job.parse_note, "detail fetch empty"] if part)
            return job
        merge_detail_into_job(job, detail)
        self._detail_pause()
        return job
    def _page_url(self, page: int) -> str:
        if page <= 1:
            return WONDERCV_URL
        return f"{WONDERCV_URL}page/pn{page}/"

    def _pause(self) -> None:
        cfg = self.config.get("crawler", {})
        low = float(cfg.get("min_interval_seconds", 2))
        high = float(cfg.get("max_interval_seconds", 5))
        self.sleep(random.uniform(low, high))

    def _detail_pause(self) -> None:
        cfg = self.config.get("crawler", {})
        low = float(cfg.get("detail_min_interval_seconds", cfg.get("min_interval_seconds", 2)))
        high = float(cfg.get("detail_max_interval_seconds", cfg.get("max_interval_seconds", 5)))
        self.sleep(random.uniform(low, high))

    def _enrich_details_enabled(self) -> bool:
        value = self.config.get("crawler", {}).get("enrich_details", True)
        return value not in (False, "false", "False", 0, "0")


def merge_detail_into_job(job: Job, detail: DetailParseResult) -> Job:
    """Apply one successful detail parse using the authoritative field policy."""
    job.raw_text = detail.raw_text
    job.apply_url = urljoin(job.detail_url, detail.apply_url) if detail.apply_url else job.apply_url
    job.content_hash = hashlib.sha256(detail.raw_text.encode("utf-8")).hexdigest()
    job.role_text = detail.role_text
    job.announcement_text = detail.announcement_text
    job.role_signals = detail.role_signals or []
    job.field_evidence = json.dumps(detail.field_evidence or {}, ensure_ascii=False, sort_keys=True)
    job.extraction_version = EXTRACTION_VERSION
    job.positions = detail.positions or []
    job.job_tags = _non_detail_tags(job.job_tags)
    # The discovery card is intentionally concise.  Detail text remains in
    # announcement_text and must not replace the copy used by list cards.
    if detail.summary and not job.summary:
        job.summary = detail.summary
    if detail.graduate_year:
        job.target_graduate_year = detail.graduate_year
    if detail.city:
        job.city = detail.city
        job.location_text = detail.city
    if detail.degree:
        job.degree = detail.degree
    if detail.deadline:
        job.deadline = detail.deadline
    title_batch = infer_batch(job.title)
    if detail.batch and (not job.batch or title_batch != job.batch):
        job.batch = detail.batch
    job.parse_status = "detail_ready"
    return job


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _strip_tags(html: str) -> str:
    return _clean(re.sub(r"<[^>]+>", " ", html))


DETAIL_SIGNAL_MARKERS = [
    "招聘岗位",
    "关联岗位",
    "岗位要求",
    "职位摘要",
    "招聘方向",
    "岗位方向",
    "岗位信息",
]

DETAIL_SECTION_END_MARKERS = [
    "招聘流程",
    "投递建议",
    "福利待遇",
    "常见问题",
    "FAQ",
    "公司介绍",
    "申请方式",
]

DETAIL_KEYWORDS = [
    "嵌入式",
    "GNSS",
    "图像算法",
    "测试开发",
    "流体力学",
    "FPGA",
    "IC",
    "数字电路",
    "模拟电路",
    "芯片",
    "半导体",
    "Verilog",
    "PCB",
    "C/C++",
    "RTOS",
    "Linux",
    "单片机",
    "驱动开发",
    "算法",
    "硬件",
    "软件",
    "技术",
    "产品",
    "运营",
    "数据",
    "研发",
]

POSITION_TITLE_TERMS = (
    "工程师", "研究员", "研究助理", "科研人员", "助理", "经理", "专员", "顾问", "设计师", "架构师",
    "管培生", "培训生", "实习生", "实习岗", "教师", "辅导员", "代表", "HRBP",
)
POSITION_ENGLISH_TERMS = re.compile(
    r"\b(?:engineer|researcher|intern|manager|developer|designer|consultant|specialist|sales|hrbp)\b",
    re.I,
)
POSITION_REJECT_PREFIXES = (
    "负责", "参与", "协助", "完成", "支持", "开展", "承担", "进行", "编写", "制定", "跟进",
    "配合", "根据", "提供", "协同", "推动", "维护", "实现", "具备", "熟悉", "掌握", "要求",
    "独立完成", "主导", "搭建", "收集", "深入了解", "定期", "候选人",
)
POSITION_LIST_LABELS = ("类", "岗位", "方向", "地点", "城市", "类别")
POSITION_SUMMARY_TERMS = ("运营", "财务", "审计", "采购", "销售", "教练", "编辑", "律师")


def _extract_detail_signal_text(text: str) -> str:
    snippets: list[str] = []
    for marker in DETAIL_SIGNAL_MARKERS:
        start = text.find(marker)
        if start < 0:
            continue
        end = len(text)
        for next_marker in DETAIL_SIGNAL_MARKERS:
            if next_marker == marker:
                continue
            next_index = text.find(next_marker, start + len(marker))
            if next_index > start:
                end = min(end, next_index)
        for end_marker in DETAIL_SECTION_END_MARKERS:
            end_index = text.find(end_marker, start + len(marker))
            if end_index > start:
                end = min(end, end_index)
        snippets.append(text[start : min(end, start + 1600)])
    return _clean(" ".join(snippets))


def _trim_detail_tail(text: str) -> str:
    candidates = []
    for marker in ("校招推荐", "文章推荐", "©202", "京ICP备"):
        index = text.find(marker)
        if index >= 0:
            candidates.append(index)
    if not candidates:
        return text
    first_tail = min(candidates)
    if first_tail <= 80:
        later = [index for index in candidates if index > 80]
        if later:
            first_tail = min(later)
    return _clean(text[:first_tail])


def _focus_detail_body(text: str) -> str:
    starts = [text.find(marker) for marker in ("首页 / 校招信息", "招聘公告与岗位信息") if text.find(marker) >= 0]
    if not starts:
        return text
    focused = _clean(text[min(starts):])
    import re
    cleaned = re.sub(r'^首页\s*/\s*校招信息(?:\s*/\s*[^/]+){1,2}\s*/\s*', '', focused)
    return _clean(cleaned)


def _extract_detail_keywords(text: str) -> list[str]:
    hits: list[str] = []
    for keyword in dict.fromkeys([*DETAIL_KEYWORDS, *role_signal_terms()]):
        if _detail_keyword_in_text(text, keyword):
            hits.append(keyword)
    return hits


def _extract_detail_positions(html: str, role_text: str) -> list[Position]:
    soup = BeautifulSoup(html, "html.parser")
    has_role_cards = bool(soup.select("section#jobs .role-item"))
    authoritative = [*_extract_role_card_positions(html), *_extract_position_tables(html)]
    if authoritative or has_role_cards:
        return _finalize_positions(authoritative)

    blocks = _role_section_blocks(_extract_block_texts(html))
    if not blocks and role_text:
        blocks = [role_text]
    elif role_text:
        blocks = [*_related_position_title_blocks(role_text), *blocks]
    positions: list[Position] = []
    positions_by_title: dict[str, Position] = {}
    current: Position | None = None
    for block in blocks:
        candidates = _position_candidates(block)
        if candidates:
            for title, tail, location_hint in candidates:
                city = infer_city(location_hint or block)
                candidate = Position(
                    title=title,
                    direction_id=infer_role_direction(f"{title} {tail}"),
                    employment_type=_extract_employment_type(block),
                    city=city,
                    location_status="confirmed" if city else "pending",
                    degree=_extract_degree(block),
                    majors=_extract_majors(block),
                    responsibilities=tail or None,
                    skills=_extract_detail_keywords(block),
                    headcount=_extract_headcount(block),
                    source_text=block,
                    field_evidence={"title": {"source": "detail_role_block", "evidence": block[:240]}},
                    confidence=0.94 if tail else 0.88,
                    extraction_version="position-v1",
                    ordinal=len(positions),
                )
                key = _normalized_position_title(title)
                current = positions_by_title.get(key)
                if current is None:
                    current = candidate
                    positions_by_title[key] = current
                    positions.append(current)
                else:
                    _merge_position(current, candidate)
            continue
        if current is None:
            continue
        current.source_text = _clean(f"{current.source_text} {block}")
        current.skills = _merge_unique(current.skills, _extract_detail_keywords(block))
        current.degree = current.degree or _extract_degree(block)
        current.majors = _merge_unique(current.majors, _extract_majors(block))
        current.headcount = current.headcount or _extract_headcount(block)
        if not current.city:
            current.city = infer_city(block)
            current.location_status = "confirmed" if current.city else "pending"
        if re.match(r"^(?:岗位职责|工作职责|职位职责|职责)[：:]?", block):
            current.responsibilities = _clean(re.sub(r"^(?:岗位职责|工作职责|职位职责|职责)[：:]?", "", block))
        elif re.match(r"^(?:岗位要求|任职要求|职位要求|任职资格|要求)[：:]?", block):
            current.requirements = _clean(re.sub(r"^(?:岗位要求|任职要求|职位要求|任职资格|要求)[：:]?", "", block))
        elif current.requirements:
            current.requirements = _clean(f"{current.requirements} {block}")
    return _finalize_positions(positions)


GENERIC_ROLE_CARD_TITLES = {"研发", "制造", "运营", "技术", "产品", "市场", "销售", "职能", "管培生", "其他"}
POSITION_TABLE_TITLE_HEADERS = {
    "岗位", "职位", "岗位名称", "职位名称", "招聘岗位", "需求岗位", "岗位类型", "岗位类别",
    "招聘职位", "职位类型", "职位类别", "招聘类别", "招聘方向", "岗位方向",
}
NON_POSITION_HEADER_TITLES = {
    "面向人群", "招聘对象", "学历要求", "工作地点", "专业要求", "语言/区域方向", "招聘人数",
}
POSITION_HEADER_TITLES = POSITION_TABLE_TITLE_HEADERS | NON_POSITION_HEADER_TITLES
VAGUE_POSITION_TITLES = {
    "所属企业校招岗位", "所属企业校招岗", "具体岗位", "各类岗位", "多个岗位", "招聘岗位详见官网",
}


def _extract_role_card_positions(html: str) -> list[Position]:
    soup = BeautifulSoup(html, "html.parser")
    section = soup.select_one("section#jobs")
    if not section:
        return []
    items = section.select(".role-item")
    if _role_cards_are_cross_announcement_mix(soup, items):
        return []
    campaign_marker = any(
        re.search(r"招聘(?:入口)?\s*$", _clean(item.find("strong").get_text(" ", strip=True)))
        for item in items if item.find("strong")
    )
    announcement_text = _clean(" ".join(
        heading.get_text(" ", strip=True)
        for heading in reversed(section.find_all_previous(["h2", "h3"]))
    ))
    positions: list[Position] = []
    for item in items:
        heading = item.find("strong")
        if not heading:
            continue
        description_node = item.find("p")
        description = _clean(description_node.get_text(" ", strip=True) if description_node else "")
        title = _clean_authoritative_position_title(heading.get_text(" ", strip=True))
        expanded_titles = _expanded_role_card_titles(title, description)
        if expanded_titles:
            for expanded_title in expanded_titles:
                if campaign_marker and not _title_has_announcement_evidence(expanded_title, announcement_text):
                    continue
                source_text = _clean(f"{expanded_title} {description}")
                positions.append(_position_from_authoritative_text(
                    expanded_title, source_text, source="detail_role_card", confidence=0.99,
                    responsibilities=description or None,
                ))
            continue
        if title in GENERIC_ROLE_CARD_TITLES:
            match = re.match(r"^(.{2,32}?)(?:方向|岗位)(?:[，,。；;]|$)", description)
            title = _clean_authoritative_position_title(match.group(1)) if match else ""
        if not title:
            continue
        if campaign_marker and not _title_has_announcement_evidence(title, announcement_text):
            continue
        source_text = _clean(f"{title} {description}")
        positions.append(_position_from_authoritative_text(
            title, source_text, source="detail_role_card", confidence=0.99,
            responsibilities=description or None,
        ))
    return positions


def _title_has_announcement_evidence(title: str, announcement_text: str) -> bool:
    evidence = re.sub(r"[^\w\u4e00-\u9fff]", "", announcement_text).casefold()
    candidates = [title, re.sub(r"[（(].*?[）)]", "", title)]
    return any(
        len(normalized) >= 4 and normalized in evidence
        for candidate in candidates
        if (normalized := re.sub(r"[^\w\u4e00-\u9fff]", "", candidate).casefold())
    )


def _expanded_role_card_titles(title: str, description: str) -> list[str]:
    if not title or not re.search(r"(?:中心|分院|部门).*(?:实习|见习)", title):
        return []
    match = re.search(r"招聘(.{2,100}?)(?:等)?(?:方向)?(?:实习生|见习生)", description)
    if not match:
        return []
    titles: list[str] = []
    for value in re.split(r"[、；;]", match.group(1)):
        cleaned = _clean_authoritative_position_title(value)
        if cleaned and cleaned not in titles:
            titles.append(cleaned)
    return titles


def _role_cards_are_cross_announcement_mix(soup: BeautifulSoup, items: list) -> bool:
    heading = _clean(soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "")
    page_hint = re.split(r"(?:20\d{2}|\d{2}届|春招|秋招|校招|招聘|实习|提前批)", heading, maxsplit=1)[0]
    page_hint = re.sub(r"[^\w\u4e00-\u9fff]", "", page_hint).casefold()
    organizations: list[str] = []
    pattern = re.compile(
        r"^(.{2,36}?(?:有限责任公司|股份有限公司|有限公司|集团|大学|学院|公安局|研究院|分局))"
        r".{0,12}?(?:公开)?(?:招聘|招收|招募)"
    )
    for item in items:
        text = _clean(item.get_text(" ", strip=True))
        match = pattern.search(text)
        if match:
            organization = re.sub(r"[^\w\u4e00-\u9fff]", "", match.group(1)).casefold()
            if organization not in organizations:
                organizations.append(organization)
    if len(organizations) < 2 or not page_hint:
        return False
    return not any(
        page_hint in organization or organization in page_hint
        or (len(page_hint) >= 4 and page_hint[:4] in organization)
        for organization in organizations
    )


def _extract_position_tables(html: str) -> list[Position]:
    soup = BeautifulSoup(html, "html.parser")
    positions: list[Position] = []
    for table in soup.find_all("table"):
        rows: list[list[str]] = []
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"], recursive=False) or row.find_all(["th", "td"])
            values = [_clean(cell.get_text(" ", strip=True)) for cell in cells]
            if any(values):
                rows.append(values)
        if len(rows) < 2:
            continue
        header_index = title_index = None
        title_priority = 99
        header_rows = rows[:2] if len(rows[0]) == 1 else rows[:1]
        for row_index, row in enumerate(header_rows):
            for cell_index, value in enumerate(row):
                if value in POSITION_TABLE_TITLE_HEADERS or re.fullmatch(r"(?:招聘|需求)?(?:岗位|职位)(?:名称|类型|类别)?", value):
                    priority = 0 if value in {"岗位名称", "职位名称", "招聘岗位", "需求岗位", "招聘职位", "岗位", "职位"} else 1
                    current = (priority, cell_index)
                    if title_index is None or current < (title_priority, title_index):
                        header_index, title_index, title_priority = row_index, cell_index, priority
            if title_index is not None:
                break
        if header_index is None or title_index is None:
            continue
        for row in rows[header_index + 1:]:
            if title_index >= len(row):
                continue
            row_text = _clean(" ".join(row))
            raw_titles = re.split(r"[、；;\n]+", row[title_index])
            for raw_title in raw_titles:
                title = _clean_authoritative_position_title(raw_title)
                if not title:
                    continue
                positions.append(_position_from_authoritative_text(
                    title, row_text, source="detail_position_table", confidence=0.97,
                ))
    return positions


def _position_from_authoritative_text(
    title: str,
    source_text: str,
    *,
    source: str,
    confidence: float,
    responsibilities: str | None = None,
) -> Position:
    city = infer_city(source_text)
    return Position(
        title=title,
        direction_id=infer_role_direction(f"{title} {source_text}"),
        employment_type=_extract_employment_type(source_text),
        city=city,
        location_status="confirmed" if city else "pending",
        degree=_extract_degree(source_text),
        majors=_extract_majors(source_text),
        responsibilities=responsibilities,
        skills=_extract_detail_keywords(source_text),
        headcount=_extract_headcount(source_text),
        source_text=source_text,
        field_evidence={"title": {"source": source, "evidence": source_text[:240]}},
        confidence=confidence,
        extraction_version="position-v3",
    )


def _clean_authoritative_position_title(value: str) -> str:
    title = _clean(value).strip("-—–·• ")
    title = re.sub(r"^[^\w\u4e00-\u9fff（(【]+", "", title)
    title = re.sub(r"^【(?:(?:20)?\d{2}届)?(?:校招|春招|秋招|实习|校招实习生)】\s*", "", title)
    title = re.sub(r"^[（(](?:20)?\d{2}届[）)]\s*", "", title)
    title = re.sub(r"^(?:20)?\d{2}(?:秋季|春季)?校园招聘[：:]\s*", "", title)
    title = re.sub(r"^(?:提前批|春招|秋招|校招)\s*[-—–：:]\s*", "", title)
    title = re.sub(r"^(?:20)?\d{2}届\s*[-—–]?\s*", "", title)
    title = re.sub(r"^[A-Za-z]{1,10}\d{2,6}\s*[-—–]\s*", "", title)
    title = re.sub(r"^[A-Za-z]{1,8}\d{1,5}\s+", "", title)
    title = re.sub(r"\s*[（(]J\d{3,}[）)]\s*$", "", title, flags=re.I)
    title = re.sub(r"\s*[（(]\d{4,}[）)]\s*$", "", title)
    title = re.sub(r"[（(](?:春招|秋招|校招|可转正)[）)]", "", title)
    title = re.sub(r"[（(](?:20)?\d{2}届(?:春季|秋季)?校园招聘[）)]\s*$", "", title)
    title = re.sub(r"[【[](?:20)?\d{2}届[^】\]]*(?:计划|校园招聘)[】\]]\s*$", "", title)
    title = re.sub(r"【(?:20)?\d{2}届】\s*$", "", title)
    title = re.sub(r"[（(]实习生计划[）)]\s*$", "", title)
    title = re.sub(r"[（(]仅限(?:20)?\d{2}届[）)]\s*$", "", title)
    title = re.sub(r"^[^—–-]{2,16}计划\s*[-—–]\s*(?=.{2,})", "", title)
    title = re.sub(r"\s*[-—–]?\s*(?:20)?\d{2}届(?:春招|秋招|校招|提前批)?\s*$", "", title)
    title = re.sub(r"\s*[-—–]?\s*(?:20)?\d{2}校招\s*$", "", title)
    title = re.sub(r"\s*[-—–]\s*(?:春招|秋招|校招|提前批)\s*$", "", title)
    title = re.sub(r"^(?:年薪|月薪)?\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?(?:万|[Kk])(?:/年|/月)?\s*", "", title)
    trailing = re.search(r"\s*[-—–]\s*([^—–-]{2,14})$", title)
    if trailing and infer_city(trailing.group(1)):
        title = title[: trailing.start()]
    leading = re.match(r"^([^—–-]{2,14})\s*[-—–]\s*(.+)$", title)
    if leading and infer_city(leading.group(1)):
        title = leading.group(2)
    location = re.search(r"[（(]([^）)]{2,12})[）)]$", title)
    if location and infer_city(location.group(1)):
        title = title[: location.start()]
    long_qualifier = re.search(r"[（(]([^）)]{25,})[）)]$", title)
    if long_qualifier:
        base = _clean(title[: long_qualifier.start()])
        if _has_position_signal(base):
            title = base
    department_recruiting = re.search(
        r"(?P<department>(?:传播|财务|人力资源|市场|运营|研发|法务|行政|投资|技术)部)"
        r"现招聘(?P<role>实习生|工程师|专员|经理|助理|顾问|教师|研究员|博士后)$",
        title,
    )
    if department_recruiting:
        title = f"{department_recruiting.group('department')}{department_recruiting.group('role')}"
    title = _clean(title).strip("-—–·• ")
    if (
        not title
        or title in POSITION_HEADER_TITLES
        or title in VAGUE_POSITION_TITLES
        or len(title) > 60
        or re.search(
            r"(?:应届毕业生.*实习生|招聘对象|面向人群|学历要求|招聘公告|招聘简章|"
            r"(?:有限公司|集团|大学|学院|公安局|研究院).*(?:招聘|招收|招募)|"
            r"(?:本科|硕士|博士|大专).*学历.*届|未就业毕业生|计划持续招募中|"
            r"^(?:20\d{2}年)?(?:本硕博|应届(?:本科|大专|硕士|博士)?)(?:高校)?毕业生$|"
            r"^具有工作经验的专业人才$|(?:公开)?招聘(?:入口)?$|招募$|^校园招聘\s*[-—–]|"
            r"(?:人才|合伙人)计划(?:$|[｜|])|(?:菁英|精英|摘星)计划(?:暑期)?(?:实习生|实习招募|补招岗)?$|"
            r"推免生|预选拔)",
            title,
        )
    ):
        return ""
    return title


def _finalize_positions(positions: list[Position]) -> list[Position]:
    merged: list[Position] = []
    by_title: dict[str, Position] = {}
    for position in positions:
        position.title = _clean_authoritative_position_title(position.title)
        if not position.title:
            continue
        key = _position_identity(position.title)
        existing = by_title.get(key)
        if existing is None:
            by_title[key] = position
            merged.append(position)
        else:
            _merge_position(existing, position)

    normalized = [(position, _position_identity(position.title)) for position in merged]
    pruned = [
        position for position, key in normalized
        if sum(other_key != key and len(other_key) >= 4 and other_key in key for _, other_key in normalized) < 2
    ]
    for ordinal, position in enumerate(pruned):
        position.ordinal = ordinal
        evidence_source = position.field_evidence.get("title", {}).get("source", "detail_role_block")
        for field_name in ("city", "degree", "majors", "responsibilities", "requirements", "skills", "headcount"):
            value = getattr(position, field_name)
            if value not in (None, "", []):
                position.field_evidence[field_name] = {
                    "source": evidence_source,
                    "evidence": (position.source_text or "")[:240],
                }
    return pruned


def _position_candidates(block: str) -> list[tuple[str, str, str]]:
    text = _clean(re.sub(r"^(?:[-•·🎯📍]\s*|\d{1,3}[.、)）]\s*)", "", block))
    if not text:
        return []
    colon = re.match(r"^(?P<label>[^：:]{1,40})\s*[：:]\s*(?P<body>.+)$", text)
    if colon:
        label = _clean(colon.group("label"))
        body = _clean(colon.group("body"))
        title = _clean_position_title(label)
        if title:
            return [(title, body, label)]
        if label.endswith(("地点", "城市")):
            return []
        if infer_city(label) or label.endswith(POSITION_LIST_LABELS):
            return _position_list_candidates(body, location_hint=label if infer_city(label) else "", allow_plain=True)
        return []

    sentence = re.match(r"^(?P<title>[^，。]{2,32})，(?P<tail>(?:负责|参与|承担|涵盖|工作地点).+)$", text)
    if sentence:
        title = _clean_position_title(sentence.group("title"), allow_plain=True)
        if title and (_has_position_signal(title) or title.endswith(POSITION_SUMMARY_TERMS)):
            return [(title, _clean(sentence.group("tail")), title)]
    parts = re.split(r"[、；;]", text)
    if len(parts) > 1:
        candidates = _position_list_candidates(text)
        if len(candidates) > 1:
            return candidates
    title = _clean_position_title(text)
    return [(title, "", text)] if title else []


def _related_position_title_blocks(role_text: str) -> list[str]:
    terms = "|".join(re.escape(term) for term in sorted(POSITION_TITLE_TERMS, key=len, reverse=True))
    titles: list[str] = []
    for marker in ("关联岗位", "招聘岗位"):
        match = re.search(rf"{marker}\s+(?P<title>.{{0,40}}?(?:{terms}))(?=\s)", role_text)
        if match:
            title = _clean_position_title(match.group("title"))
            if title and title not in titles:
                titles.append(title)
    for match in re.finditer(r"(?:本次)?招聘岗位为\s*([^。，]{2,60})", role_text):
        for value in re.split(r"[、和]", match.group(1)):
            title = _clean_position_title(value, allow_plain=True)
            if title and len(title) <= 20 and title not in titles:
                titles.append(title)
    return titles


def _position_list_candidates(
    text: str, *, location_hint: str = "", allow_plain: bool = False
) -> list[tuple[str, str, str]]:
    candidates: list[tuple[str, str, str]] = []
    parts = re.split(r"[、；;]", text)
    if allow_plain and not any(_clean_position_title(part) for part in parts):
        return []
    for part in parts:
        title = _clean_position_title(part, allow_plain=allow_plain)
        if title:
            candidates.append((title, "", location_hint))
    return candidates


def _clean_position_title(value: str, *, allow_plain: bool = False) -> str:
    title = _clean_authoritative_position_title(value)
    if not title or title.startswith(POSITION_REJECT_PREFIXES):
        return ""
    if re.match(r"^[（(]?[一二三四五六七八九十0-9]+[）).、]", title):
        return ""
    for inner in reversed(re.findall(r"[（(]([^）)]{2,40})[）)]", title)):
        if POSITION_ENGLISH_TERMS.search(title) and _has_position_signal(inner):
            title = _clean(inner)
            break
    trailing = re.search(r"\s*[-—–]\s*([^—–-]{2,12})$", title)
    if trailing and infer_city(trailing.group(1)):
        title = _clean(title[: trailing.start()])
    location = re.search(r"[（(]([^）)]{2,20})[）)]$", title)
    if location and (infer_city(location.group(1)) or "岗位详情" in location.group(1)):
        title = _clean(title[: location.start()])
    qualification = re.search(r"[（(]([^）)]{8,60})[）)]$", title)
    if qualification and ("相关专业" in qualification.group(1) or "专业" in qualification.group(1) and "、" in qualification.group(1)):
        title = _clean(title[: qualification.start()])
    if not title or len(title) > 36 or title.startswith(POSITION_REJECT_PREFIXES):
        return ""
    if any(mark in title for mark in "，。；;：:！？→"):
        return ""
    if title in {"岗位", "职位", "招聘岗位", "关联岗位", "岗位信息", "专业要求", "技能要求"}:
        return ""
    if not allow_plain and not _has_position_signal(title):
        return ""
    if allow_plain and len(title) > 24:
        return ""
    return title


def _has_position_signal(title: str) -> bool:
    base = re.sub(r"[（(][^）)]{1,40}[）)]\s*$", "", title).strip()
    parts = re.split(r"\s*[-—–]\s*", base, maxsplit=1)
    if len(parts) > 1 and (parts[0].endswith(POSITION_TITLE_TERMS) or POSITION_ENGLISH_TERMS.search(parts[0])):
        base = parts[0]
    if base in {"开发", "测试", "算法", "助理", "经理", "专员", "顾问", "教师", "代表"}:
        return False
    return base.endswith(POSITION_TITLE_TERMS) or bool(POSITION_ENGLISH_TERMS.search(base))


def _normalized_position_title(title: str) -> str:
    return re.sub(r"[\s·•]+", "", title).casefold()


def _position_identity(title: str) -> str:
    return re.sub(r"(?:岗位|岗)$", "", _normalized_position_title(title))


def _merge_position(target: Position, incoming: Position) -> None:
    target.city = _merge_delimited(target.city, incoming.city)
    target.location_status = "confirmed" if target.city else "pending"
    target.degree = target.degree or incoming.degree
    target.majors = _merge_unique(target.majors, incoming.majors)
    target.skills = _merge_unique(target.skills, incoming.skills)
    target.headcount = target.headcount or incoming.headcount
    target.responsibilities = target.responsibilities or incoming.responsibilities
    target.requirements = target.requirements or incoming.requirements
    if incoming.source_text and incoming.source_text not in (target.source_text or ""):
        target.source_text = _clean(f"{target.source_text} {incoming.source_text}")


def _merge_delimited(left: str | None, right: str | None) -> str | None:
    values: list[str] = []
    for item in [*(left or "").split(";"), *(right or "").split(";")]:
        if item and item not in values:
            values.append(item)
    return ";".join(values) or None


def _extract_block_texts(html: str) -> list[str]:
    blocks: list[str] = []
    pattern = r"<(?:h[1-6]|p|li|dt|dd|td|th)\b[^>]*>([\s\S]*?)</(?:h[1-6]|p|li|dt|dd|td|th)>"
    for match in re.finditer(pattern, html, re.I):
        text = _strip_tags(match.group(1))
        if text and text not in blocks:
            blocks.append(text)
    if blocks:
        return blocks
    line_html = re.sub(r"<(?:br|/p|/li|/div|/section|/h[1-6])\b[^>]*>", "\n", html, flags=re.I)
    return [_clean(_strip_tags(line)) for line in line_html.splitlines() if _clean(_strip_tags(line))]


def _role_section_blocks(blocks: list[str]) -> list[str]:
    selected: list[str] = []
    active = False
    for block in blocks:
        heading = _clean(re.sub(r"^[^\w\u4e00-\u9fff]+", "", block))
        if active and any(heading == marker or heading.startswith(f"{marker}：") for marker in DETAIL_SECTION_END_MARKERS):
            break
        wrapper = heading == "招聘公告与岗位信息"
        marker = next(
            (item for item in DETAIL_SIGNAL_MARKERS if heading == item or heading.startswith(f"{item}：")),
            None,
        )
        if wrapper or marker:
            active = True
            remainder = "" if wrapper else _clean(heading[len(marker or "") :].lstrip("：: "))
            if remainder:
                selected.append(remainder)
            continue
        if active:
            selected.append(block)
    return selected


def _extract_employment_type(text: str) -> str | None:
    if "实习" in text:
        return "实习"
    if any(term in text for term in ("校招", "应届", "校园招聘")):
        return "校招"
    if any(term in text for term in ("社招", "社会招聘")):
        return "社招"
    return None


def _extract_majors(text: str) -> list[str]:
    match = re.search(r"(?:专业要求|专业)[：:]?\s*([^。；;]{2,100})", text or "")
    if not match:
        return []
    return [part.strip() for part in re.split(r"[、,/，及或]+", match.group(1)) if part.strip()]


def _extract_headcount(text: str) -> int | None:
    match = re.search(r"(?:招聘|招募|人数)[^0-9]{0,6}(\d{1,4})\s*人", text or "")
    return int(match.group(1)) if match else None


def _field_evidence(
    *,
    city: str | None,
    degree: str | None,
    deadline: str | None,
    batch: str | None,
    graduate_year: str | None,
    role_text: str,
) -> dict[str, dict[str, object]]:
    """Record enough provenance to explain why detail values win over card values."""
    values = {
        "city": (city, "detail_role_text" if city and city in role_text else "detail_body"),
        "degree": (degree, "detail_role_text" if degree and degree in role_text else "detail_body"),
        "deadline": (deadline, "detail_body"),
        "batch": (batch, "detail_body"),
        "target_graduate_year": (graduate_year, "detail_body"),
    }
    evidence: dict[str, dict[str, object]] = {}
    for name, (value, source) in values.items():
        if value:
            text = role_text if source == "detail_role_text" else "详情页正文"
            evidence[name] = {
                "value": value,
                "source": source,
                "evidence": text[:240],
                "confidence": 0.95 if source == "detail_role_text" else 0.9,
            }
    return evidence


def _detail_keyword_in_text(text: str, keyword: str) -> bool:
    if not text or not keyword:
        return False
    if keyword == "IC":
        return bool(re.search(r"(?<![A-Za-z0-9_])IC(?![A-Za-z0-9_])", text, flags=re.I)) or bool(
            re.search(r"(?<![A-Za-z0-9_])IC(?=[\u4e00-\u9fff])", text, flags=re.I)
        )
    return keyword.lower() in text.lower()


def _remove_noise_blocks(html: str) -> str:
    cleaned = html
    for tag in ("script", "style", "noscript", "nav", "header", "footer", "aside"):
        cleaned = re.sub(rf"<{tag}\b[\s\S]*?</{tag}>", " ", cleaned, flags=re.I)
    return cleaned


def _extract_apply_url(html: str) -> str | None:
    for match in re.finditer(r"<a\b([^>]*)>([\s\S]*?)</a>", html, re.I):
        attrs, body = match.groups()
        href_match = re.search(r"href=['\"]([^'\"]+)['\"]", attrs, re.I)
        if not href_match:
            continue
        href = href_match.group(1)
        label = _strip_tags(body)
        if "投递" in label or re.search(r"(jobs|apply|campus|zhaopin)", href, re.I):
            return href
    match = re.search(r"<a[^>]+href=['\"]([^'\"]*(?:jobs|apply|campus|zhaopin)[^'\"]*)['\"]", html, re.I)
    return match.group(1) if match else None


def _extract_degree(text: str) -> str | None:
    for degree in ("大专及以上", "大专", "本科及以上", "本科", "硕士及以上", "硕士", "博士", "MBA"):
        if degree in (text or ""):
            return degree
    return None


def _extract_deadline(text: str) -> str | None:
    for pattern in (
        r"截止时间\s*(20\d{2})[-./](\d{1,2})[-./](\d{1,2})",
        r"截止[^0-9]*(20\d{2})[-./](\d{1,2})[-./](\d{1,2})",
    ):
        match = re.search(pattern, text or "")
        if match:
            year, month, day = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return None


def _merge_unique(left: list[str], right: list[str]) -> list[str]:
    values: list[str] = []
    for item in [*left, *right]:
        if item and item not in values:
            values.append(item)
    return values


def _non_detail_tags(tags: list[str]) -> list[str]:
    detail_keywords = set(DETAIL_KEYWORDS)
    return [tag for tag in tags if tag not in detail_keywords]


def _infer_company(title: str) -> str:
    for sep in (" 202", "202", "校园招聘", "秋招", "提前批"):
        if sep in title:
            return title.split(sep)[0].strip()
    return title[:30]


COMPANY_TYPES = {"上市公司", "央企国企", "民企", "外企", "合资", "事业单位", "政府机关", "社会组织", "其他"}
SPECIAL_MARKS = {"有内推"}
DEGREES = {"大专", "本科", "硕士", "博士", "MBA"}
BATCH_WORDS = {"秋招提前批", "秋招", "秋招补录", "春招提前批", "春招", "春招补录", "暑期实习", "寒假实习"}


def _parse_card_text(text: str) -> dict[str, object]:
    tokens = text.split()
    company_type = tokens[0] if tokens and tokens[0] in COMPANY_TYPES else ""
    industry = ""
    special_marks: list[str] = []
    raw_tags: list[str] = []
    prefix_end = 0

    for index, token in enumerate(tokens):
        if token == "收录":
            prefix_end = index + 2 if index + 1 < len(tokens) else index + 1
            break
        if index == 1 and company_type:
            industry = token
        if token in SPECIAL_MARKS:
            special_marks.append(token)
        if index <= 3:
            raw_tags.append(token)

    rest = tokens[prefix_end:] if prefix_end else tokens
    company = rest[0] if rest else ""
    after_company = rest[1:] if len(rest) > 1 else []
    clean_tokens: list[str] = []
    job_tags: list[str] = []
    metadata_started = False
    for token in after_company:
        if token in DEGREES or token in BATCH_WORDS:
            raw_tags.append(token)
            metadata_started = True
            continue
        if infer_city(token):
            raw_tags.append(token)
            metadata_started = True
            continue
        if metadata_started:
            job_tags.append(token)
            continue
        if len(clean_tokens) < 12:
            clean_tokens.append(token)
        else:
            job_tags.append(token)

    clean_title = _clean(" ".join(clean_tokens)) or company or text
    summary = _clean(" ".join(after_company))[:300] if after_company else clean_title
    job_tags = [tag for tag in job_tags if tag not in raw_tags]
    return {
        "company_type": company_type,
        "industry": industry,
        "company": company,
        "clean_title": clean_title[:160],
        "summary": summary,
        "special_marks": special_marks,
        "job_tags": job_tags[:10],
        "raw_tags": [tag for tag in raw_tags if tag],
    }


def _extract_job_id(url: str) -> str | None:
    match = re.search(r"/xiaozhao/([^/?#]+)", url)
    if not match:
        return None
    value = match.group(1).strip("/")
    return value or None


class _Card:
    def __init__(self, href: str):
        self.href = href
        self.text_parts: list[str] = []
        self.title = ""
        self.company = ""
        self.city = ""
        self.date = ""
        self.tags: list[str] = []

    @property
    def text(self) -> str:
        return _clean(" ".join(self.text_parts))


class _CardParser(HTMLParser):
    CARD_CLASSES = {"job-card", "position-card", "school-recruit-card"}

    def __init__(self):
        super().__init__()
        self.cards: list[_Card] = []
        self.current: _Card | None = None
        self.current_field = ""
        self.depth = 0

    @classmethod
    def parse(cls, html: str) -> list[_Card]:
        parser = cls()
        parser.feed(html)
        return parser.cards

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        classes = set(attr.get("class", "").split())
        href = attr.get("href", "")
        starts_card = href and "xiaozhao" in href and (tag == "a" or classes & self.CARD_CLASSES)
        if starts_card and self.current is None:
            self.current = _Card(href)
            self.depth = 1
        elif self.current is not None:
            self.depth += 1

        if self.current is not None:
            if tag in {"h1", "h2", "h3"} or classes & {"title", "job-title", "name"}:
                self.current_field = "title"
            elif classes & {"company", "company-name"} or "data-company" in attr:
                self.current_field = "company"
                if attr.get("data-company"):
                    self.current.company = attr["data-company"]
            elif classes & {"city", "work-city"} or "data-city" in attr:
                self.current_field = "city"
                if attr.get("data-city"):
                    self.current.city = attr["data-city"]
            elif classes & {"date", "time", "card-date", "collect-date", "created-at"}:
                self.current_field = "date"
            elif classes & {"tag", "label", "badge"}:
                self.current_field = "tag"

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        self.depth -= 1
        self.current_field = ""
        if self.depth <= 0:
            self.cards.append(self.current)
            self.current = None

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return
        text = _clean(data)
        if not text:
            return
        self.current.text_parts.append(text)
        if self.current_field == "title":
            self.current.title = _clean(f"{self.current.title} {text}")
        elif self.current_field == "company":
            self.current.company = _clean(f"{self.current.company} {text}")
        elif self.current_field == "city":
            self.current.city = _clean(f"{self.current.city} {text}")
        elif self.current_field == "date":
            self.current.date = _clean(f"{self.current.date} {text}")
        elif self.current_field == "tag":
            self.current.tags.append(text)
