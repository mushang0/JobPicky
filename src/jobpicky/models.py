from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class Position:
    title: str
    position_key: str = ""
    direction_id: str | None = None
    department: str | None = None
    employment_type: str | None = None
    city: str | None = None
    location_status: str = "confirmed"
    degree: str | None = None
    majors: list[str] = field(default_factory=list)
    responsibilities: str | None = None
    requirements: str | None = None
    skills: list[str] = field(default_factory=list)
    headcount: int | None = None
    source_text: str | None = None
    field_evidence: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    extraction_version: str | None = None
    ordinal: int = 0


@dataclass(slots=True)
class Job:
    source: str = "WonderCV"
    source_job_id: str | None = None
    source_url: str | None = None
    detail_url: str | None = None
    dedupe_key: str | None = None
    company: str = ""
    raw_company: str | None = None
    company_normalized: str | None = None
    title: str = ""
    raw_title: str | None = None
    clean_title: str | None = None
    summary: str | None = None
    batch: str | None = None
    target_graduate_year: str | None = None
    degree: str | None = None
    city: str | None = None
    location_text: str | None = None
    location_status: str = "confirmed"
    collected_date: str | None = None
    deadline: str | None = None
    company_type: str | None = None
    industry: str | None = None
    tags: list[str] = field(default_factory=list)
    job_tags: list[str] = field(default_factory=list)
    special_marks: list[str] = field(default_factory=list)
    raw_tags: list[str] = field(default_factory=list)
    raw_text: str | None = None
    role_text: str | None = None
    announcement_text: str | None = None
    role_signals: list[str] = field(default_factory=list)
    field_evidence: str | None = None
    extraction_version: str | None = None
    apply_url: str | None = None
    official_url: str | None = None
    announcement_url: str | None = None
    announcement_url_source: str | None = None
    official_url_source: str | None = None
    givemeoc_record_id: str | None = None
    # Directly constructed jobs (tests/imports) are treated as complete; the
    # WonderCV list parser explicitly marks discovery records as list_only.
    parse_status: str = "detail_ready"
    parse_note: str | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    last_checked: str | None = None
    content_hash: str | None = None
    is_active: int = 1
    positions: list[Position] = field(default_factory=list)

    def as_db_values(self) -> dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        return {
            "source": self.source,
            "source_job_id": self.source_job_id,
            "source_url": self.source_url,
            "detail_url": self.detail_url,
            "dedupe_key": self.dedupe_key,
            "company": self.company,
            "raw_company": self.raw_company or self.company,
            "company_normalized": self.company_normalized or self.company,
            "title": self.title,
            "raw_title": self.raw_title,
            "clean_title": self.clean_title or self.title,
            "summary": self.summary,
            "batch": self.batch,
            "target_graduate_year": self.target_graduate_year,
            "degree": self.degree,
            "city": self.city,
            "location_text": self.location_text,
            "location_status": self.location_status if self.city else "pending",
            "collected_date": self.collected_date,
            "deadline": self.deadline,
            "company_type": self.company_type,
            "industry": self.industry,
            "tags": ";".join(self.tags),
            "job_tags": ";".join(self.job_tags),
            "special_marks": ";".join(self.special_marks),
            "raw_tags": ";".join(self.raw_tags),
            "raw_text": self.raw_text,
            "role_text": self.role_text,
            "announcement_text": self.announcement_text,
            "role_signals": ";".join(self.role_signals),
            "field_evidence": self.field_evidence,
            "extraction_version": self.extraction_version,
            "apply_url": self.apply_url,
            "official_url": self.official_url,
            "announcement_url": self.announcement_url,
            "announcement_url_source": self.announcement_url_source,
            "official_url_source": self.official_url_source,
            "givemeoc_record_id": self.givemeoc_record_id,
            "parse_status": self.parse_status,
            "parse_note": self.parse_note,
            "first_seen": self.first_seen or now,
            "last_seen": self.last_seen or now,
            "last_checked": self.last_checked,
            "content_hash": self.content_hash,
            "is_active": self.is_active,
        }


@dataclass(slots=True)
class MatchResult:
    matched_keywords: list[str]
    matched_strong_keywords: list[str]
    matched_weak_keywords: list[str]
    matched_industry_keywords: list[str]
    matched_company_rule: str
    matched_city_rule: str
    negative_keywords: list[str]
    match_score: int
    priority: str
    is_relevant: bool
    should_push: bool
    needs_verify: bool
    match_reason: str
    verify_status: str
    suggested_search_terms: list[str]
    match_config_version: str
    matched_at: str
    recommend_reason: str = ""
    matched_role_group_id: str = ""
    matched_position_title: str = ""
    matched_position_key: str = ""
    match_evidence: dict[str, Any] = field(default_factory=dict)
    decision_trace: list[str] = field(default_factory=list)
