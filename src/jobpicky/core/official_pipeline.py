from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Iterable

from ..givemeoc import (
    GiveMeOCCrawlResult,
    GiveMeOCRecord,
    apply_givemeoc_links,
    build_givemeoc_alias_index,
    build_givemeoc_record_index,
)
from ..models import Job
from ..official_jobs import OfficialCrawlResult, OfficialJobCrawler, identify_official_platform, merge_official_jobs


@dataclass(frozen=True, slots=True)
class OfficialJobBatch:
    jobs: tuple[Job, ...]
    matched_records: tuple[GiveMeOCRecord, ...]
    official_result: OfficialCrawlResult
    links_matched: int


def _official_source_key(value: str | None) -> str:
    return str(value or "").strip().rstrip("/").casefold()


def _official_cache_is_fresh(job: Job, now: datetime, cache_days: int) -> bool:
    if cache_days <= 0:
        return False
    for value in (job.last_seen, job.last_checked, job.first_seen):
        if not value:
            continue
        try:
            seen_at = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            continue
        return now - seen_at <= timedelta(days=cache_days)
    return False


def build_official_job_batch(
    jobs: Iterable[Job],
    records: Iterable[GiveMeOCRecord],
    config: dict[str, Any],
    *,
    existing_jobs: Iterable[Job] = (),
    crawler: OfficialJobCrawler | None = None,
    links_complete: bool = True,
    fetch_official: bool = True,
    record_index: dict[str, tuple[GiveMeOCRecord, ...]] | None = None,
) -> OfficialJobBatch:
    """Build one reusable WonderCV + official job batch.

    Daily scans, seed refreshes, and small integration tests differ only in
    their input scope and crawler implementation.  This function owns the
    shared link match, official fetch, evidence-preserving merge, and output
    batch construction.
    """
    source_jobs = list(jobs)
    all_records = tuple(records)
    existing_jobs = list(existing_jobs)
    aliases = config.get("system_taxonomy", {}).get("company_aliases", {})
    normalized_aliases = build_givemeoc_alias_index(aliases)
    record_index = record_index or build_givemeoc_record_index(all_records, aliases)
    links_matched = apply_givemeoc_links(
        source_jobs,
        GiveMeOCCrawlResult(records=all_records, pages_scanned=0, complete=links_complete),
        aliases,
        record_index,
        normalized_aliases,
    )

    records_by_id = {record.source_record_id: record for record in all_records}
    matched: list[GiveMeOCRecord] = []
    seen_ids: set[str] = set()
    for job in source_jobs:
        record = records_by_id.get(job.givemeoc_record_id or "")
        if record and record.official_url and record.source_record_id not in seen_ids:
            matched.append(record)
            seen_ids.add(record.source_record_id)

    official_config = config.get("official_jobs", {})
    allow_generic = bool(official_config.get("allow_generic", False))
    crawl_records: list[GiveMeOCRecord] = []
    seen_sources: set[str] = set()
    for record in matched:
        if identify_official_platform(record.official_url) == "generic" and not allow_generic:
            continue
        source_key = _official_source_key(record.official_url)
        if not source_key or source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        crawl_records.append(record)
    # ponytail: use existing official rows as a short-lived source cache;
    # add a source-state table when zero-result/error caching or HTTP validators matter.
    cached_by_source: dict[str, list[Job]] = {}
    for job in existing_jobs:
        if job.source == "official" and job.source_url:
            cached_by_source.setdefault(_official_source_key(job.source_url), []).append(job)

    cache_days = int(official_config.get("cache_days", 7) or 0)
    cached_jobs: list[Job] = []
    records_to_fetch: list[GiveMeOCRecord] = []
    now = datetime.now()
    official_enabled = fetch_official and official_config.get("enabled", True)
    if official_enabled:
        for record in crawl_records:
            cached = cached_by_source.get(_official_source_key(record.official_url), [])
            cached_jobs.extend(cached)
            if not cached or not all(_official_cache_is_fresh(job, now, cache_days) for job in cached):
                records_to_fetch.append(record)

    fetched_result = (
        (crawler or OfficialJobCrawler(config)).crawl(tuple(records_to_fetch))
        if official_enabled and records_to_fetch
        else OfficialCrawlResult((), 0, 0)
    )
    cached_by_key = {job.dedupe_key or job.detail_url: job for job in cached_jobs}
    cached_by_key.update({job.dedupe_key or job.detail_url: job for job in fetched_result.jobs})
    official_jobs_for_merge = tuple(cached_by_key.values())
    official_result = fetched_result

    record_by_url = {
        _official_source_key(record.official_url): record
        for record in matched
        if record.official_url
    }
    official_jobs = tuple(
        replace(
            job,
            announcement_url=record_by_url.get(_official_source_key(job.source_url)).announcement_url if record_by_url.get(_official_source_key(job.source_url)) else job.announcement_url,
            announcement_url_source="givemeoc" if record_by_url.get(_official_source_key(job.source_url)) and record_by_url[_official_source_key(job.source_url)].announcement_url else job.announcement_url_source,
            givemeoc_record_id=record_by_url.get(_official_source_key(job.source_url)).source_record_id if record_by_url.get(_official_source_key(job.source_url)) else job.givemeoc_record_id,
        )
        for job in official_jobs_for_merge
    )
    merged = merge_official_jobs(
        source_jobs,
        official_jobs,
        existing_jobs,
        aliases,
    )
    return OfficialJobBatch(tuple(merged), tuple(matched), official_result, links_matched)
