from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Literal
from uuid import uuid4

from ..alerts import build_daily_message
from ..error_safety import known_secrets, redact_text, safe_exception_detail
from ..feishu import FeishuBitableClient, FeishuBot, FeishuConfig
from ..official_search import OfficialUrlFinder
from ..givemeoc import (
    GiveMeOCCrawlResult,
    GiveMeOCCrawler,
    GiveMeOCRecord,
    apply_givemeoc_links,
    build_givemeoc_alias_index,
    build_givemeoc_record_index,
    givemeoc_company_key,
)
from ..official_jobs import OfficialJobCrawler
from ..core import DailyUpdateService, JobIngestionService, MatchingService, RecommendationService, build_official_job_batch
from ..pipeline import enrich_official_urls, pull_user_states_from_feishu
from ..run_guard import DailyRunGuard, DailyRunInProgress
from ..runtime import RunReport, RunReporter
from ..storage import JobRepository
from ..wondercv import EXTRACTION_VERSION, WonderCVCrawler
from ..integrations.feishu import FeishuIntegrationService
from .synchronization import SyncSummary, sync_feishu


DailyStatus = Literal["success", "partial_success", "failed"]


def _public_scan_detail(value: str | None) -> str:
    """Keep source-specific crawler names out of user-facing progress text."""
    return (value or "").replace("WonderCV", "招聘数据源").replace("wondercv", "招聘数据源")


def run_daily_with_jobs(
    repo: JobRepository,
    jobs,
    config: dict,
    run_date: str | None = None,
):
    """Compatibility adapter backed by the shared daily core service."""
    class CompletedCrawl:
        def crawl(self, *, mode="daily"):
            return type("Crawl", (), {"jobs": jobs, "pages_scanned": 0})()

    return DailyUpdateService(
        CompletedCrawl(),
        JobIngestionService(repo),
        MatchingService(repo, config),
        RecommendationService(repo),
    ).run(run_date)


@dataclass(frozen=True, slots=True)
class DailyStageError:
    stage: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class _FinalizeOutcome:
    database_errors: tuple[DailyStageError, ...] = ()
    current_workspace_items: int = 0


def _daily_exit_code(
    status: DailyStatus,
    errors: tuple[DailyStageError, ...],
    fetched_count: int,
) -> int:
    """Return the single exit-code policy shared by CLI and Web results."""
    if status == "success":
        return 0
    if (
        status == "partial_success"
        and fetched_count > 0
        and errors
        and all(error.code == "fetch_partial" for error in errors)
    ):
        return 0
    return 1


@dataclass(frozen=True, slots=True)
class DailyWorkflowResult:
    status: DailyStatus
    task_id: str
    fetched_count: int = 0
    sources_attempted: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0
    created_count: int = 0
    updated_count: int = 0
    unchanged_count: int = 0
    matched_count: int = 0
    recommended_count: int = 0
    link_enriched_count: int = 0
    feishu_pull_attempted: bool = False
    feishu_pull_succeeded: bool = False
    feishu_pull_updated_count: int = 0
    feishu_pull_skipped_count: int = 0
    feishu_pull_unknown_count: int = 0
    feishu_created_count: int = 0
    feishu_updated_count: int = 0
    feishu_skipped_count: int = 0
    feishu_failed_count: int = 0
    notification_status: Literal["skipped", "sent", "failed"] = "skipped"
    notification_attempted: bool = False
    notification_sent: bool = False
    errors: tuple[DailyStageError, ...] = ()
    exit_code: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "exit_code", _daily_exit_code(self.status, self.errors, self.fetched_count))
        if self.notification_status == "skipped":
            object.__setattr__(self, "notification_attempted", False)
            object.__setattr__(self, "notification_sent", False)
        elif self.notification_status == "sent":
            object.__setattr__(self, "notification_attempted", True)
            object.__setattr__(self, "notification_sent", True)
        else:
            object.__setattr__(self, "notification_attempted", True)
            object.__setattr__(self, "notification_sent", False)

    @property
    def error_summary(self) -> str:
        return "; ".join(
            f"{error.stage} [{error.code}]: {error.message}" for error in self.errors
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the shared result, including compatibility aliases for Web clients."""
        payload = asdict(self)
        payload["errors"] = [asdict(error) for error in self.errors]
        payload.update(
            {
                "items_seen": self.fetched_count,
                "new_items": self.created_count,
                "recommended_items": self.recommended_count,
                "feishu_created": self.feishu_created_count,
                "feishu_updated": self.feishu_updated_count,
                "feishu_failed": self.feishu_failed_count,
                # Legacy aliases remain for existing Web consumers. They are
                # derived from the canonical fields above and are redacted.
                "error": self.error_summary,
            }
        )
        return payload


def run_daily_workflow(
    config: dict,
    db_path: str | Path,
    *,
    reporter: RunReporter | None = None,
    cancel_check: Callable[[], bool] | None = None,
    task_id: str | None = None,
) -> DailyWorkflowResult:
    """Run the complete daily business workflow for every application entry point."""
    reporter = reporter or RunReporter()
    cancel_check = cancel_check or (lambda: False)
    task_id = task_id or uuid4().hex
    started_at = datetime.now().isoformat(timespec="seconds")
    repo = JobRepository(db_path)
    errors: list[DailyStageError] = []
    try:
        repo.init_schema()
    except Exception as exc:
        logging.error("Daily workflow initialization failed: %s", safe_exception_detail(exc, config))
        result = DailyWorkflowResult(
            status="failed",
            task_id=task_id,
            errors=(_stage_error("initialization", "initialization_failed", "每日工作流初始化失败"),),
        )
        _finalize_result(repo, reporter, config, started_at, result, write_scan_run=True)
        return result
    try:
        feishu_enabled = _feishu_is_configured(config)
    except Exception as exc:
        logging.error("Daily workflow configuration failed: %s", safe_exception_detail(exc, config))
        errors.append(_stage_error("configuration", "workflow_failed", "daily configuration failed"))
        return _finish(repo, reporter, config, started_at, task_id, errors=errors)
    pull_attempted = pull_succeeded = False
    pull_updated = pull_skipped = pull_unknown = 0
    notification_status: Literal["skipped", "sent", "failed"] = "skipped"
    notification_attempted = notification_sent = False
    crawl = None
    summary = None
    enrich_summary = None
    official_result = None
    jobs_for_ingest = None
    sync_summary = SyncSummary()
    try:
        with DailyRunGuard(db_path) as guard:
            def is_cancelled() -> bool:
                return cancel_check() or guard.cancelled.is_set()

            crawler = WonderCVCrawler(
                config,
                cancel_check=is_cancelled,
            )
            if hasattr(crawler, "progress"):
                crawler.progress = lambda detail: reporter.stage(
                    "daily", 2, 6, "获取最新招聘公告", detail=_public_scan_detail(detail),
                )
            last_run_date = repo.get_last_successful_run_date("daily")

            def should_stop(page_jobs) -> bool:
                if not page_jobs:
                    return True
                if all(
                    repo.job_exists(job.dedupe_key)
                    and not repo.job_requires_detail_enrichment(job.dedupe_key, EXTRACTION_VERSION)
                    for job in page_jobs
                ):
                    logging.info("Dynamic stop triggered: all jobs on the current page already exist.")
                    return True
                return bool(
                    last_run_date
                    and all(not job.collected_date or job.collected_date < last_run_date for job in page_jobs)
                )

            reporter.stage("daily", 2, 6, "获取最新招聘公告")
            try:
                crawl = crawler.crawl(mode="daily", should_stop=should_stop)
            except Exception as exc:
                logging.error("Daily fetch failed: %s", safe_exception_detail(exc, config))
                errors.append(_stage_error("fetch", "fetch_failed", "岗位抓取失败"))
                return _finish(
                    repo, reporter, config, started_at, task_id, errors=errors,
                    pull_attempted=pull_attempted, pull_succeeded=pull_succeeded,
                    pull_updated=pull_updated, pull_skipped=pull_skipped, pull_unknown=pull_unknown,
                )
            reporter.stage("daily", 2, 6, "获取最新招聘公告", "done", f"抓取 {len(crawl.jobs)} 条")
            source_attempted, source_succeeded, source_failed = _crawl_source_counts(crawl)
            if crawl.error or source_failed:
                code = "fetch_partial" if crawl.jobs else "fetch_failed"
                message = "部分岗位抓取失败，已保留成功结果" if crawl.jobs else "岗位抓取失败"
                logging.warning(
                    "Daily fetch reported an error: %s",
                    redact_text(crawl.error, secrets=known_secrets(config)),
                )
                errors.append(_stage_error("fetch", code, message))
            elif source_attempted and not source_succeeded:
                errors.append(_stage_error("fetch", "fetch_failed", "岗位抓取失败"))
            if getattr(crawl, "interrupted", False) or is_cancelled():
                errors.append(_stage_error("fetch", "fetch_failed", "岗位抓取已中断"))
                return _finish(
                    repo, reporter, config, started_at, task_id, crawl=crawl, errors=errors,
                    pull_attempted=pull_attempted, pull_succeeded=pull_succeeded,
                    pull_updated=pull_updated, pull_skipped=pull_skipped, pull_unknown=pull_unknown,
                )

            if config.get("givemeoc", {}).get("enabled", False):
                try:
                    givemeoc = GiveMeOCCrawler(
                        config,
                        cancel_check=is_cancelled,
                        progress=lambda detail: reporter.stage(
                            "daily", 2, 6, "核对公告与官方投递入口", detail=detail,
                        ),
                    )
                    cached_records = tuple(
                        GiveMeOCRecord(
                            source_record_id=str(row.get("source_record_id") or ""),
                            company=str(row.get("company") or ""),
                            company_normalized=str(row.get("company_normalized") or ""),
                            recruitment_type=str(row.get("recruitment_type") or ""),
                            target_graduate_year=str(row.get("target_graduate_year") or ""),
                            city=str(row.get("city") or ""),
                            deadline=str(row.get("deadline") or ""),
                            updated_at=str(row.get("updated_at") or ""),
                            announcement_url=row.get("announcement_url"),
                            official_url=row.get("official_url"),
                        )
                        for row in repo.list_givemeoc_records()
                    )
                    cached_records_by_id = {
                        record.source_record_id: record for record in cached_records
                    }
                    if config.get("givemeoc", {}).get("cache_only", False):
                        link_result = GiveMeOCCrawlResult(
                            records=cached_records,
                            pages_scanned=0,
                            complete=repo.givemeoc_cache_initialized(),
                            total_pages=0,
                        )
                    else:
                        if hasattr(givemeoc, "set_cache"):
                            givemeoc.set_cache(cached_records, repo.givemeoc_cache_initialized())
                        link_result = givemeoc.crawl(crawl.jobs, mode="daily")
                        repo.save_givemeoc_records(
                            (asdict(record) for record in link_result.records),
                            complete=link_result.complete,
                            total_pages=getattr(link_result, "total_pages", 0),
                            pages_scanned=link_result.pages_scanned,
                        )
                    # Match only affected companies for an incomplete incremental
                    # crawl; a complete snapshot still reconciles every stored job.
                    aliases = config.get("system_taxonomy", {}).get("company_aliases", {})
                    normalized_aliases = build_givemeoc_alias_index(aliases)
                    record_index = build_givemeoc_record_index(link_result.records, aliases)
                    changed_company_keys = {
                        givemeoc_company_key(job.company, aliases, normalized_aliases)
                        for job in crawl.jobs
                        if job.company
                    }
                    changed_company_keys.update(
                        givemeoc_company_key(record.company, aliases, normalized_aliases)
                        for record in link_result.records
                        if cached_records_by_id.get(record.source_record_id) != record
                    )
                    jobs_by_key = {
                        job.dedupe_key: job
                        for job in (repo.job_from_row(row) for row in repo.list_stored_jobs())
                        if job.dedupe_key
                    }
                    jobs_by_key.update({job.dedupe_key: job for job in crawl.jobs if job.dedupe_key})
                    link_jobs = (
                        list(jobs_by_key.values())
                        if link_result.complete
                        else [
                            job for job in jobs_by_key.values()
                            if givemeoc_company_key(job.company, aliases, normalized_aliases) in changed_company_keys
                        ]
                    ) + [job for job in crawl.jobs if not job.dedupe_key]
                    matched_links = apply_givemeoc_links(
                        link_jobs,
                        link_result,
                        aliases,
                        record_index,
                        normalized_aliases,
                    )
                    repo.reconcile_givemeoc_links(link_jobs, complete=link_result.complete)
                    if False and config.get("official_jobs", {}).get("enabled", False):
                        official_result = OfficialJobCrawler(
                            config,
                            cancel_check=is_cancelled,
                            progress=lambda detail: reporter.stage(
                                "daily", 2, 6, "抓取官方岗位", detail=detail,
                            ),
                        ).crawl(
                            record
                            for record in link_result.records
                            if record.official_url
                            and (
                                identify_official_platform(record.official_url) != "generic"
                                or config.get("official_jobs", {}).get("allow_generic", False)
                            )
                        )
                        if official_result.jobs:
                            stored_jobs = [
                                repo.job_from_row(row)
                                for row in repo.list_stored_jobs()
                            ]
                            batch = build_official_job_batch(
                                crawl.jobs,
                                link_result.records,
                                config,
                                existing_jobs=(repo.job_from_row(row) for row in repo.list_stored_jobs()),
                                crawler=OfficialJobCrawler(
                                    config,
                                    cancel_check=is_cancelled,
                                    progress=lambda detail: reporter.stage(
                                        "daily", 2, 6, "抓取官方岗位", detail=detail,
                                    ),
                                ),
                                links_complete=link_result.complete,
                                record_index=record_index,
                            )
                            official_result = batch.official_result
                            jobs_for_ingest = list(batch.jobs)
                        if official_result.errors:
                            errors.append(_stage_error("official_jobs", "official_jobs_partial", "官方岗位补充部分失败"))
                    if config.get("official_jobs", {}).get("enabled", False):
                        batch = build_official_job_batch(
                            crawl.jobs,
                            link_result.records,
                            config,
                            existing_jobs=(repo.job_from_row(row) for row in repo.list_stored_jobs()),
                            crawler=OfficialJobCrawler(
                                config,
                                cancel_check=is_cancelled,
                                progress=lambda detail: reporter.stage(
                                    "daily", 2, 6, "抓取官方岗位", detail=detail,
                                ),
                            ),
                            links_complete=link_result.complete,
                            record_index=record_index,
                        )
                        official_result = batch.official_result
                        jobs_for_ingest = list(batch.jobs)
                        if official_result.errors:
                            errors.append(_stage_error("official_jobs", "official_jobs_partial", "官方岗位补充部分失败"))

                    enrich_summary = SimpleNamespace(
                        items_seen=len({job.company_normalized or job.company for job in crawl.jobs}),
                        updated_items=matched_links,
                    )
                    if link_result.error:
                        errors.append(_stage_error("link_enrichment", "link_enrichment_failed", "公告与投递入口核对失败"))
                    reporter.stage("daily", 2, 6, "核对公告与官方投递入口", "done", f"已核对 {matched_links} 个公司")
                except Exception as exc:
                    logging.warning("GiveMeOC link reconciliation failed: %s", safe_exception_detail(exc, config))
                    errors.append(_stage_error("link_enrichment", "link_enrichment_failed", "公告与投递入口核对失败"))

            reporter.stage("daily", 3, 6, "标准化、增量写入并匹配岗位")
            try:
                summary = run_daily_with_jobs(repo, jobs_for_ingest or crawl.jobs, config)
            except Exception as exc:
                logging.error("Daily processing failed: %s", safe_exception_detail(exc, config))
                errors.append(_stage_error("process", "processing_failed", "岗位处理失败"))
                return _finish(
                    repo, reporter, config, started_at, task_id, crawl=crawl, errors=errors,
                    pull_attempted=pull_attempted, pull_succeeded=pull_succeeded,
                    pull_updated=pull_updated, pull_skipped=pull_skipped, pull_unknown=pull_unknown,
                )
            reporter.stage("daily", 3, 6, "标准化、增量写入并匹配岗位", "done", f"新推荐 {summary.recommended_items} 条")
            notification_rows = _notification_rows(repo.list_all_jobs(), config)

            reporter.stage("daily", 4, 6, "整理可用的公告与投递信息", "done")

            if feishu_enabled and not is_cancelled():
                reporter.stage("daily", 5, 6, "回拉并同步飞书")
                integration = FeishuIntegrationService(
                    repo, config, client_factory=FeishuBitableClient, bot_factory=FeishuBot,
                    pull_states=pull_user_states_from_feishu, push_jobs=sync_feishu,
                ).run_after_local_update(
                    new_items=summary.new_items,
                    notification_rows=notification_rows,
                    fetch_error=next((error.message for error in errors if error.stage == "fetch"), None),
                    cancelled=is_cancelled,
                )
                pull_attempted = integration.pull_attempted
                pull_succeeded = integration.pull_succeeded
                pull_updated = integration.pull_updated
                pull_skipped = integration.pull_skipped
                pull_unknown = integration.pull_unknown
                sync_summary = integration.sync
                notification_status = integration.notification_status
                notification_attempted = notification_status != "skipped"
                notification_sent = notification_status == "sent"
                errors.extend(_stage_error(issue.stage, issue.code, issue.message) for issue in integration.issues)
                reporter.stage("daily", 5, 6, "回拉并同步飞书", "done")
                reporter.stage(
                    "daily", 6, 6, "发送每日通知",
                    "failed" if notification_status == "failed" else "done",
                )

            return _finish(
                repo, reporter, config, started_at, task_id,
                crawl=crawl, summary=summary, enrich_summary=enrich_summary,
                sync_summary=sync_summary, errors=errors,
                pull_attempted=pull_attempted, pull_succeeded=pull_succeeded,
                pull_updated=pull_updated, pull_skipped=pull_skipped, pull_unknown=pull_unknown,
                notification_attempted=notification_attempted,
                notification_status=notification_status,
                notification_sent=notification_sent,
            )
    except DailyRunInProgress as exc:
        result = DailyWorkflowResult(
            status="failed", task_id=task_id,
            errors=(_stage_error("run_guard", "daily_already_running", "已有日常扫描正在运行"),),
        )
        _finalize_result(repo, reporter, config, started_at, result, write_scan_run=False)
        return result
    except Exception as exc:
        logging.error("Daily workflow failed: %s", safe_exception_detail(exc, config))
        errors.append(_stage_error("workflow", "workflow_failed", "每日工作流失败"))
        return _finish(
            repo, reporter, config, started_at, task_id, crawl=crawl,
            summary=summary, enrich_summary=enrich_summary, sync_summary=sync_summary,
            errors=errors, pull_attempted=pull_attempted, pull_succeeded=pull_succeeded,
            pull_updated=pull_updated, pull_skipped=pull_skipped, pull_unknown=pull_unknown,
            notification_attempted=notification_attempted,
            notification_status=notification_status, notification_sent=notification_sent,
        )

def _crawl_source_counts(crawl) -> tuple[int, int, int]:
    jobs = list(getattr(crawl, "jobs", ()) or ())
    error = getattr(crawl, "error", None)
    attempted = getattr(crawl, "sources_attempted", None)
    succeeded = getattr(crawl, "sources_succeeded", None)
    failed = getattr(crawl, "sources_failed", None)
    if attempted is None or succeeded is None or failed is None:
        attempted = 1
        succeeded = int(not error or bool(jobs))
        failed = int(bool(error))
    elif not attempted:
        attempted = 1
        if error:
            failed = max(failed, 1)
        else:
            succeeded = max(succeeded, 1)
    return int(attempted), int(succeeded), int(failed)


def _notification_result_sent(result: Any) -> bool:
    try:
        return result.sent is True
    except BaseException:
        return False


def _notification_result_detail(result: Any, config: dict) -> str:
    try:
        value = getattr(result, "error", None) or "unknown notification error"
    except BaseException:
        value = "unknown notification error"
    return redact_text(value, secrets=known_secrets(config))


def _finalize_result(
    repo: JobRepository,
    reporter: RunReporter,
    config: dict,
    started_at: str,
    result: DailyWorkflowResult,
    *,
    write_scan_run: bool,
    pages_scanned: int = 0,
    emit_report: bool = True,
) -> _FinalizeOutcome:
    database_errors: list[DailyStageError] = []
    if write_scan_run:
        values = {
            "run_type": "daily",
            "task_id": result.task_id,
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "status": "partial" if result.status == "partial_success" else result.status,
            "pages_scanned": pages_scanned,
            "items_seen": result.fetched_count,
            "new_items": result.created_count,
            "updated_items": result.updated_count,
            "recommended_items": result.recommended_count,
            "expiring_items": 0,
            "failure_stage": result.errors[0].stage if result.errors else None,
            "error_message": result.error_summary or None,
            "notification_status": result.notification_status,
        }
        try:
            conn = repo.connect()
            try:
                object_row = conn.execute(
                    "SELECT type FROM sqlite_master WHERE name = 'scan_runs'"
                ).fetchone()
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(scan_runs)")}
            finally:
                conn.close()
            if object_row and object_row["type"] == "table" and columns:
                values["expiring_items"] = repo.count_expiring_jobs(recommended=True)
                repo.record_scan_run({key: value for key, value in values.items() if key in columns})
            else:
                raise RuntimeError("scan_runs table unavailable")
        except Exception as exc:
            logging.error("Daily early-result recording failed: %s", safe_exception_detail(exc, config))
            database_errors.append(_stage_error("database", "database_failed", "每日运行结果保存失败"))

    current_workspace_items = 0
    try:
        current_workspace_items = len(repo.list_feishu_sync_candidates())
    except Exception as exc:
        logging.error("Daily early-result reporting failed: %s", safe_exception_detail(exc, config))
        database_errors.append(_stage_error("database", "database_failed", "每日运行结果读取失败"))
    if not emit_report:
        return _FinalizeOutcome(tuple(database_errors), current_workspace_items)
    try:
        reporter.finish(
            RunReport(
                "daily",
                "partial" if result.status == "partial_success" else result.status,
                items_seen=result.fetched_count,
                new_items=result.created_count,
                recommended_items=result.recommended_count,
                current_workspace_items=current_workspace_items,
                feishu_created=result.feishu_created_count,
                feishu_updated=result.feishu_updated_count,
                feishu_skipped=result.feishu_skipped_count,
                feishu_failed=result.feishu_failed_count,
                notification_status=result.notification_status,
                workspace_url=str(config.get("feishu", {}).get("base_url") or ""),
                advice=_advice(result),
            )
        )
    except Exception as exc:
        logging.error("Daily early-result reporter failed: %s", safe_exception_detail(exc, config))
    return _FinalizeOutcome(tuple(database_errors), current_workspace_items)


def _emit_daily_report(
    reporter: RunReporter,
    config: dict,
    result: DailyWorkflowResult,
    current_workspace_items: int,
) -> None:
    try:
        reporter.finish(
            RunReport(
                "daily",
                "partial" if result.status == "partial_success" else result.status,
                items_seen=result.fetched_count,
                new_items=result.created_count,
                recommended_items=result.recommended_count,
                current_workspace_items=current_workspace_items,
                feishu_created=result.feishu_created_count,
                feishu_updated=result.feishu_updated_count,
                feishu_skipped=result.feishu_skipped_count,
                feishu_failed=result.feishu_failed_count,
                notification_status=result.notification_status,
                workspace_url=str(config.get("feishu", {}).get("base_url") or ""),
                advice=_advice(result),
            )
        )
    except Exception as exc:
        logging.error("Daily early-result reporter failed: %s", safe_exception_detail(exc, config))


def _finish(
    repo: JobRepository,
    reporter: RunReporter,
    config: dict,
    started_at: str,
    task_id: str,
    *,
    crawl=None,
    summary=None,
    enrich_summary=None,
    sync_summary: SyncSummary | None = None,
    errors: list[DailyStageError] | None = None,
    pull_attempted: bool = False,
    pull_succeeded: bool = False,
    pull_updated: int = 0,
    pull_skipped: int = 0,
    pull_unknown: int = 0,
    notification_attempted: bool = False,
    notification_status: Literal["skipped", "sent", "failed"] = "skipped",
    notification_sent: bool = False,
    source_attempted: int | None = None,
    source_succeeded: int | None = None,
    source_failed: int | None = None,
) -> DailyWorkflowResult:
    sync_summary = sync_summary or SyncSummary()
    if crawl is not None and source_attempted is None:
        source_attempted, source_succeeded, source_failed = _crawl_source_counts(crawl)
    source_attempted = source_attempted or 0
    source_succeeded = source_succeeded or 0
    source_failed = source_failed or 0
    fetched = summary.items_seen if summary else 0
    created = summary.new_items if summary else 0
    updated = summary.updated_items if summary else 0
    finish_errors = list(errors or ())
    try:
        repo.vacuum()
    except Exception as exc:
        logging.error("Daily database maintenance failed: %s", safe_exception_detail(exc, config))
        finish_errors.append(_stage_error("database", "database_failed", "每日数据库维护失败"))
    status = _calculate_status(finish_errors)
    result = DailyWorkflowResult(
        status=status,
        task_id=task_id,
        fetched_count=fetched,
        sources_attempted=source_attempted,
        sources_succeeded=source_succeeded,
        sources_failed=source_failed,
        created_count=created,
        updated_count=updated,
        unchanged_count=max(fetched - created - updated, 0),
        matched_count=summary.matched_items if summary else 0,
        recommended_count=summary.recommended_items if summary else 0,
        link_enriched_count=enrich_summary.updated_items if enrich_summary else 0,
        feishu_pull_attempted=pull_attempted,
        feishu_pull_succeeded=pull_succeeded,
        feishu_pull_updated_count=pull_updated,
        feishu_pull_skipped_count=pull_skipped,
        feishu_pull_unknown_count=pull_unknown,
        feishu_created_count=sync_summary.created,
        feishu_updated_count=sync_summary.updated,
        feishu_skipped_count=sync_summary.skipped,
        feishu_failed_count=sync_summary.failed,
        notification_status=notification_status,
        notification_attempted=notification_attempted,
        notification_sent=notification_sent,
        errors=tuple(finish_errors),
    )
    finalize_outcome = _finalize_result(
        repo,
        reporter,
        config,
        started_at,
        result,
        write_scan_run=True,
        pages_scanned=getattr(crawl, "pages_scanned", 0),
        emit_report=False,
    )
    if finalize_outcome.database_errors:
        final_errors = result.errors + finalize_outcome.database_errors
        result = replace(
            result,
            status=_calculate_status(final_errors),
            errors=final_errors,
        )
    _emit_daily_report(
        reporter,
        config,
        result,
        finalize_outcome.current_workspace_items,
    )
    return result


_CORE_FAILURE_CODES = frozenset({
    "initialization_failed",
    "fetch_failed",
    "processing_failed",
    "workflow_failed",
})


def _stage_error(stage: str, code: str, message: str) -> DailyStageError:
    return DailyStageError(stage=stage, code=code, message=message)


def _calculate_status(errors: tuple[DailyStageError, ...] | list[DailyStageError]) -> DailyStatus:
    if any(error.code in _CORE_FAILURE_CODES for error in errors):
        return "failed"
    return "partial_success" if errors else "success"


def _advice(result: DailyWorkflowResult) -> str:
    stages = {error.stage for error in result.errors}
    if "feishu_pull" in stages:
        return "飞书状态回收异常，已阻止后续同步；请检查后安全重试。"
    if "feishu_sync" in stages:
        return "飞书同步有失败项，请检查错误后安全重试。"
    if "notification" in stages:
        return "岗位结果已保存，但飞书通知发送失败，请检查机器人配置后重试。"
    if result.recommended_count == 0:
        return "本次没有新的匹配岗位，飞书无需更新。"
    return "已同步本次匹配到的岗位。"


def _notification_rows(rows: list[dict], config: dict) -> list[dict]:
    recommended_rows = [
        row for row in rows
        if row.get("recommendation_status") == "推荐"
        and (not row.get("feishu_record_id") or row.get("sync_status") in ("pending", "failed"))
    ]
    value = config.get("user_profile", {}).get("daily_push_limit")
    if value in (None, "", "不限制", "unlimited"):
        return recommended_rows
    return recommended_rows[: max(int(value), 0)]


def _feishu_is_configured(config: dict) -> bool:
    if config.get("feishu", {}).get("enabled", True) is False:
        return False
    feishu = FeishuConfig.from_config(config)
    has_auth = bool(feishu.tenant_access_token or (feishu.app_id and feishu.app_secret))
    return bool(feishu.app_token and feishu.table_id and has_auth)
