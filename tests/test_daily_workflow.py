from __future__ import annotations

import os
import time
import json
import io
import logging
import threading
import pytest
from fastapi.testclient import TestClient
from pathlib import Path
from types import SimpleNamespace

from jobpicky import cli
from jobpicky.feishu import FeishuResult
from jobpicky.models import Job
from jobpicky.paths import AppPaths
from jobpicky.error_safety import redact_text, safe_exception_detail
from jobpicky.logging_utils import SensitiveDataFilter
from jobpicky.services import scanning
from jobpicky.services.scanning import DailyStageError, DailyWorkflowResult, run_daily_workflow
from jobpicky.services.synchronization import SyncSummary
from jobpicky.storage import JobRepository
from jobpicky.web import app as web_app
from jobpicky.wondercv import WonderCVCrawler


def _config(*, feishu: bool = True, webhook: bool = True) -> dict:
    config = {
        "user_profile": {
            "graduate_years": ["2027届"],
            "batches": ["秋招"],
            "role_groups": ["硬件/嵌入式"],
            "target_cities": [],
            "must_watch_companies": [],
            "exclude_role_groups": [],
        },
        "system_taxonomy": {
            "role_groups": {"硬件/嵌入式": ["FPGA"]},
            "exclude_role_groups": {},
            "generic_role_terms": [],
            "important_company_types": [],
            "important_company_marks": [],
            "company_aliases": {},
        },
    }
    if feishu:
        config["feishu"] = {
            "bitable_app_token": "base",
            "table_id": "table",
            "tenant_access_token": "token",
        }
        if webhook:
            config["feishu"]["webhook_url"] = "https://example.com/hook"
    return config


class _Crawler:
    def __init__(self, config, cancel_check=None):
        self.cancel_check = cancel_check

    def crawl(self, mode, should_stop):
        return type(
            "Crawl",
            (),
            {
                "jobs": [
                    Job(
                        dedupe_key="daily:1",
                        company="Example",
                        title="2027届 FPGA 工程师",
                        batch="秋招",
                        target_graduate_year="2027届",
                    )
                ],
                "pages_scanned": 1,
                "error": None,
                "interrupted": False,
            },
        )()


def test_daily_workflow_updates_local_before_pull_and_sync(monkeypatch, tmp_path: Path):
    events: list[str] = []

    class Client:
        def __init__(self, config):
            pass

    class Crawler(_Crawler):
        def crawl(self, mode, should_stop):
            events.append("fetch")
            return super().crawl(mode, should_stop)

    class Finder:
        def find_best(self, job):
            events.append("enrich")
            return "https://careers.example.com/1"

    class Bot:
        def __init__(self, webhook_url):
            pass

        def send_text(self, message):
            events.append("notify")
            return FeishuResult(sent=True)

    def pull(repo, client):
        events.append("pull")
        return type("Recovery", (), {"updated_count": 0, "skipped_record_ids": [], "unknown_statuses": {}})()

    def sync(repo, config, rows, *, client_factory):
        events.append("sync")
        return SyncSummary(created=1)

    monkeypatch.setattr(scanning, "WonderCVCrawler", Crawler)
    monkeypatch.setattr(scanning, "OfficialUrlFinder", Finder)
    monkeypatch.setattr(scanning, "FeishuBitableClient", Client)
    monkeypatch.setattr(scanning, "FeishuBot", Bot)
    monkeypatch.setattr(scanning, "pull_user_states_from_feishu", pull)
    monkeypatch.setattr(scanning, "sync_feishu", sync)

    result = run_daily_workflow(_config(), tmp_path / "jobs.sqlite")

    assert events == ["fetch", "pull", "sync", "notify"]
    assert result.status == "success"
    assert result.fetched_count == result.created_count == result.matched_count == result.recommended_count == 1
    assert result.unchanged_count == 0
    assert result.link_enriched_count == 0
    assert result.feishu_pull_attempted is result.feishu_pull_succeeded is True
    assert result.feishu_created_count == 1
    assert result.notification_status == "sent"
    assert result.notification_attempted is result.notification_sent is True
    assert _latest_run(tmp_path / "jobs.sqlite")["status"] == "success"
    assert _latest_run(tmp_path / "jobs.sqlite")["notification_status"] == "sent"


def test_pull_failure_blocks_sync_but_returns_stage_error(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(scanning, "WonderCVCrawler", _Crawler)
    monkeypatch.setattr(scanning, "FeishuBitableClient", lambda config: object())
    monkeypatch.setattr(
        scanning,
        "pull_user_states_from_feishu",
        lambda repo, client: (_ for _ in ()).throw(RuntimeError("pull unavailable")),
    )
    monkeypatch.setattr(
        scanning,
        "sync_feishu",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("sync must be blocked")),
    )
    monkeypatch.setattr(scanning, "OfficialUrlFinder", lambda: type("Finder", (), {"find_best": lambda self, job: None})())
    monkeypatch.setattr(scanning, "FeishuBot", lambda url: type("Bot", (), {"send_text": lambda self, text: FeishuResult(sent=True)})())

    result = run_daily_workflow(_config(), tmp_path / "jobs.sqlite")

    assert result.status == "partial_success"
    assert result.exit_code == 1
    assert result.feishu_pull_attempted is True
    assert result.feishu_pull_succeeded is False
    assert result.feishu_created_count == result.feishu_updated_count == 0
    assert result.errors == (DailyStageError("feishu_pull", "feishu_pull_failed", "飞书状态回拉失败"),)


def test_no_feishu_skips_pull_sync_and_notification(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(scanning, "WonderCVCrawler", _Crawler)
    monkeypatch.setattr(
        scanning,
        "pull_user_states_from_feishu",
        lambda *args: (_ for _ in ()).throw(AssertionError("pull must not run")),
    )
    monkeypatch.setattr(
        scanning,
        "sync_feishu",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("sync must not run")),
    )

    result = run_daily_workflow(_config(feishu=False), tmp_path / "jobs.sqlite")

    assert result.status == "success"
    assert result.feishu_pull_attempted is False
    assert result.notification_status == "skipped"
    assert result.notification_attempted is False
    assert result.notification_sent is False
    assert result.link_enriched_count == 0


def test_daily_workflow_uses_givemeoc_links_and_does_not_search(monkeypatch, tmp_path: Path):
    class GiveMeOCCrawler:
        def __init__(self, config, cancel_check=None, progress=None):
            pass

        def crawl(self, jobs, mode="daily"):
            from jobpicky.givemeoc import GiveMeOCCrawlResult, GiveMeOCRecord

            return GiveMeOCCrawlResult(
                records=(GiveMeOCRecord(
                    source_record_id="oc-1",
                    company="Example",
                    company_normalized="example",
                    announcement_url="https://mp.weixin.qq.com/s/example",
                    official_url="https://careers.example.com/apply",
                ),),
                pages_scanned=1,
                complete=True,
            )

    monkeypatch.setattr(scanning, "WonderCVCrawler", _Crawler)
    monkeypatch.setattr(scanning, "GiveMeOCCrawler", GiveMeOCCrawler)
    monkeypatch.setattr(scanning, "OfficialUrlFinder", lambda: (_ for _ in ()).throw(AssertionError("search must not run")))
    config = _config(feishu=False)
    config["givemeoc"] = {"enabled": True}

    result = run_daily_workflow(config, tmp_path / "jobs.sqlite")
    row = JobRepository(tmp_path / "jobs.sqlite").list_all_jobs()[0]

    assert result.link_enriched_count == 1
    assert row["announcement_url"] == "https://mp.weixin.qq.com/s/example"
    assert row["official_url"] == "https://careers.example.com/apply"
    assert row["official_url_source"] == "givemeoc"


def test_process_failure_identifies_the_failed_stage(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(scanning, "WonderCVCrawler", _Crawler)
    monkeypatch.setattr(
        scanning,
        "run_daily_with_jobs",
        lambda *args: (_ for _ in ()).throw(ValueError("bad normalized job")),
    )

    result = run_daily_workflow(_config(feishu=False), tmp_path / "jobs.sqlite")

    assert result.status == "failed"
    assert result.errors == (DailyStageError("process", "processing_failed", "岗位处理失败"),)
    assert result.exit_code == 1


def test_cli_and_web_use_the_same_daily_service_and_expose_same_stats(tmp_path: Path, monkeypatch, capsys):
    assert cli.run_daily_workflow is web_app.run_daily_workflow is scanning.run_daily_workflow
    shared_result = DailyWorkflowResult(
        status="partial_success",
        task_id="task-1",
        fetched_count=9,
        created_count=3,
        updated_count=2,
        unchanged_count=4,
        recommended_count=2,
        feishu_created_count=1,
        matched_count=2,
        notification_status="failed",
        errors=(DailyStageError("fetch", "fetch_partial", "部分岗位抓取失败，已保留成功结果"),),
    )

    monkeypatch.setattr(cli, "run_daily_workflow", lambda *args, **kwargs: shared_result)
    assert cli._run_daily({}, str(tmp_path / "cli.sqlite")) == shared_result.exit_code
    cli_output = capsys.readouterr().out
    assert "seen=9" in cli_output and "new=3" in cli_output and "matched=2" in cli_output
    assert "feishu_created=1" in cli_output and "notification_status=failed" in cli_output

    monkeypatch.setattr(web_app, "run_daily_workflow", lambda *args, **kwargs: shared_result)
    manager = web_app.TaskManager(AppPaths(tmp_path / "profile"))
    task_id = manager.start(
        "daily",
        lambda task_id, cancelled: web_app.run_daily_workflow(
            {}, manager.paths.database, task_id=task_id, cancel_check=cancelled
        ).to_dict(),
    )
    for _ in range(100):
        task = manager.get(task_id)
        if task and task["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    assert task["fetched_count"] == 9
    assert task["created_count"] == 3
    assert task["updated_count"] == 2
    assert task["feishu_created_count"] == 1
    assert task["errors"] == [{"stage": "fetch", "code": "fetch_partial", "message": "部分岗位抓取失败，已保留成功结果"}]


def test_web_module_does_not_depend_on_cli():
    assert "cli" not in web_app.__dict__


def _latest_run(db_path: Path) -> dict:
    conn = JobRepository(db_path).connect()
    try:
        row = conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row)
    finally:
        conn.close()


def test_notification_without_webhook_is_skipped_and_keeps_success(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(scanning, "WonderCVCrawler", _Crawler)
    monkeypatch.setattr(scanning, "FeishuBitableClient", lambda config: object())
    monkeypatch.setattr(
        scanning,
        "pull_user_states_from_feishu",
        lambda repo, client: SimpleNamespace(updated_count=0, skipped_record_ids=[], unknown_statuses={}),
    )
    monkeypatch.setattr(scanning, "OfficialUrlFinder", lambda: type("Finder", (), {"find_best": lambda self, job: None})())
    monkeypatch.setattr(scanning, "sync_feishu", lambda *args, **kwargs: SyncSummary())
    monkeypatch.setattr(scanning, "FeishuBot", lambda *_args: (_ for _ in ()).throw(AssertionError("skipped notification must not construct bot")))

    db_path = tmp_path / "jobs.sqlite"
    result = run_daily_workflow(_config(webhook=False), db_path)

    assert result.status == "success"
    assert result.exit_code == 0
    assert result.notification_status == "skipped"
    assert result.errors == ()
    assert _latest_run(db_path)["status"] == "success"
    assert _latest_run(db_path)["notification_status"] == "skipped"


def test_notification_sent_false_is_failed_partial_and_nonzero(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(scanning, "WonderCVCrawler", _Crawler)
    monkeypatch.setattr(scanning, "FeishuBitableClient", lambda config: object())
    monkeypatch.setattr(
        scanning,
        "pull_user_states_from_feishu",
        lambda repo, client: SimpleNamespace(updated_count=0, skipped_record_ids=[], unknown_statuses={}),
    )
    monkeypatch.setattr(scanning, "OfficialUrlFinder", lambda: type("Finder", (), {"find_best": lambda self, job: None})())
    monkeypatch.setattr(scanning, "sync_feishu", lambda *args, **kwargs: SyncSummary())
    monkeypatch.setattr(scanning, "FeishuBot", lambda *_args: type("Bot", (), {"send_text": lambda self, text: FeishuResult(sent=False, error="remote rejected")})())

    db_path = tmp_path / "jobs.sqlite"
    result = run_daily_workflow(_config(), db_path)

    assert result.status == "partial_success"
    assert result.exit_code == 1
    assert result.notification_status == "failed"
    assert result.notification_attempted is True
    assert result.notification_sent is False
    assert result.errors == (DailyStageError("notification", "notification_send_failed", "飞书通知发送失败"),)
    assert _latest_run(db_path)["status"] == "partial"
    assert _latest_run(db_path)["notification_status"] == "failed"

    monkeypatch.setattr(cli, "run_daily_workflow", lambda *args, **kwargs: result)
    assert cli._run_daily({}, str(tmp_path / "cli.sqlite")) == 1
    assert "notification_status=failed" in capsys.readouterr().out


def test_feishu_sync_failure_is_partial_and_nonzero(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(scanning, "WonderCVCrawler", _Crawler)
    monkeypatch.setattr(scanning, "FeishuBitableClient", lambda config: object())
    monkeypatch.setattr(
        scanning,
        "pull_user_states_from_feishu",
        lambda repo, client: SimpleNamespace(updated_count=0, skipped_record_ids=[], unknown_statuses={}),
    )
    monkeypatch.setattr(scanning, "OfficialUrlFinder", lambda: SimpleNamespace(find_best=lambda job: None))
    monkeypatch.setattr(scanning, "sync_feishu", lambda *args, **kwargs: SyncSummary(failed=1))
    monkeypatch.setattr(scanning, "FeishuBot", lambda *_args: SimpleNamespace(send_text=lambda _text: FeishuResult(sent=True)))

    result = run_daily_workflow(_config(), tmp_path / "jobs.sqlite")

    assert result.status == "partial_success"
    assert result.exit_code == 1
    assert result.errors[0].stage == "feishu_sync"
    assert result.errors[0].code == "feishu_sync_failed"


def test_sensitive_exception_text_is_redacted_from_result_database_and_logs(monkeypatch, tmp_path: Path, caplog):
    monkeypatch.setattr(scanning, "WonderCVCrawler", _Crawler)
    monkeypatch.setattr(scanning, "FeishuBitableClient", lambda config: object())
    raw = (
        "Authorization: Bearer SECRET-TOKEN; "
        "webhook=https://hooks.example.com/robot?token=WEBHOOK-TOKEN&signature=SIG; "
        "app_secret=APP-SECRET; https://example.com/callback?secret=URL-SECRET"
    )
    monkeypatch.setattr(scanning, "pull_user_states_from_feishu", lambda repo, client: (_ for _ in ()).throw(RuntimeError(raw)))
    monkeypatch.setattr(scanning, "OfficialUrlFinder", lambda: type("Finder", (), {"find_best": lambda self, job: None})())
    monkeypatch.setattr(scanning, "sync_feishu", lambda *args, **kwargs: SyncSummary())
    monkeypatch.setattr(scanning, "FeishuBot", lambda *_args: type("Bot", (), {"send_text": lambda self, text: FeishuResult(sent=True)})())

    config = _config()
    config["feishu"]["app_secret"] = "APP-SECRET"
    config["feishu"]["webhook_url"] = "https://hooks.example.com/robot?token=WEBHOOK-TOKEN&signature=SIG"
    db_path = tmp_path / "jobs.sqlite"
    with caplog.at_level(logging.WARNING):
        result = run_daily_workflow(config, db_path)

    payload_text = json.dumps(result.to_dict(), ensure_ascii=False)
    db_error = _latest_run(db_path)["error_message"] or ""
    assert result.errors[0].code == "feishu_pull_failed"
    for secret in ("SECRET-TOKEN", "WEBHOOK-TOKEN", "APP-SECRET", "URL-SECRET"):
        assert secret not in payload_text
        assert secret not in db_error
        assert secret not in caplog.text


def test_web_task_fallback_is_structured_and_does_not_expose_exception(monkeypatch, tmp_path: Path):
    raw = "Authorization: Bearer SECRET-TOKEN app_secret=APP-SECRET https://x.test/hook?token=WEBHOOK-TOKEN"
    monkeypatch.setattr(web_app, "run_daily_workflow", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(raw)))

    manager = web_app.TaskManager(AppPaths(tmp_path / "profile"))
    task_id = manager.start(
        "daily",
        lambda task_id, cancelled: web_app.run_daily_workflow(
            {"feishu": {"app_secret": "APP-SECRET"}},
            manager.paths.database,
            task_id=task_id,
            cancel_check=cancelled,
        ).to_dict(),
    )
    for _ in range(100):
        task = manager.get(task_id)
        if task and task["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)

    assert task["status"] == "failed"
    assert task["exit_code"] == 1
    assert task["errors"] == [{"stage": "workflow", "code": "workflow_failed", "message": "每日工作流失败"}]
    assert all(secret not in json.dumps(task, ensure_ascii=False) for secret in ("SECRET-TOKEN", "APP-SECRET", "WEBHOOK-TOKEN"))


def _wait_for_task_status(client: TestClient, task_id: str, expected: set[str], timeout: float = 2) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        task = response.json()
        if task["status"] in expected:
            return task
        time.sleep(0.01)
    pytest.fail(f"task {task_id} did not reach {sorted(expected)} within {timeout} seconds")


def test_task_cancellation_api_preserves_cancelled_state_and_exclusivity(monkeypatch, tmp_path: Path):
    paths = AppPaths(tmp_path / "profile")
    client = TestClient(web_app.create_app(paths))
    saved = client.put(
        "/api/preferences",
        json={"user_profile": {"batches": ["秋招"], "role_groups": ["硬件/嵌入式"]}},
    )
    assert saved.status_code == 200
    cancellation_observed = threading.Event()
    allow_return = threading.Event()

    def cancelled_daily(_config, _database, *, task_id, cancel_check, **_kwargs):
        while not cancel_check():
            time.sleep(0.01)
        cancellation_observed.set()
        assert allow_return.wait(timeout=2), "test did not release the cancelled workflow"
        return DailyWorkflowResult(
            status="failed",
            task_id=task_id,
            errors=(DailyStageError("fetch", "fetch_failed", "岗位抓取已中断"),),
        )

    monkeypatch.setattr(web_app, "run_daily_workflow", cancelled_daily)
    started = client.post("/api/tasks/daily")
    assert started.status_code == 202
    task_id = started.json()["task_id"]
    _wait_for_task_status(client, task_id, {"running"})

    cancelled = client.delete(f"/api/tasks/{task_id}")
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "cancelling"
    assert cancellation_observed.wait(timeout=2), "workflow did not observe cancellation"

    blocked = client.post("/api/tasks/daily")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "already_running"

    allow_return.set()
    task = _wait_for_task_status(client, task_id, {"cancelled"})
    assert task["status"] == "cancelled"
    assert task["errors"] == [
        {"stage": "fetch", "code": "fetch_failed", "message": "岗位抓取已中断"}
    ]


def test_task_manager_does_not_turn_normal_failure_into_cancellation(tmp_path: Path):
    manager = web_app.TaskManager(AppPaths(tmp_path / "profile"))
    task_id = manager.start(
        "test",
        lambda operation_task_id, _cancelled: DailyWorkflowResult(
            status="failed",
            task_id=operation_task_id,
            errors=(DailyStageError("process", "processing_failed", "岗位处理失败"),),
        ).to_dict(),
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        task = manager.get(task_id)
        if task and task["status"] == "failed":
            break
        time.sleep(0.01)
    else:
        pytest.fail("ordinary failed task did not finish within 2 seconds")
    assert task["status"] == "failed"


def test_task_manager_continues_when_snapshot_replace_fails(monkeypatch, tmp_path: Path):
    real_replace = os.replace

    def fail_snapshot_replace(source, destination):
        if Path(destination).name == "scan-task.json":
            raise PermissionError("snapshot is temporarily locked")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_snapshot_replace)
    manager = web_app.TaskManager(AppPaths(tmp_path / "profile"))
    task_id = manager.start(
        "test",
        lambda operation_task_id, _cancelled: DailyWorkflowResult(
            status="success",
            task_id=operation_task_id,
        ).to_dict(),
    )

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        task = manager.get(task_id)
        if task and task["status"] == "success" and manager.active() is None:
            break
        time.sleep(0.01)
    else:
        pytest.fail("snapshot failure left the task running")

    assert task["status"] == "success"


def test_fetch_failure_without_jobs_is_failed_and_reported_consistently(monkeypatch, tmp_path: Path):
    reports = []
    class FailedCrawler:
        def __init__(self, config, cancel_check=None): pass
        def crawl(self, mode, should_stop):
            return SimpleNamespace(jobs=[], pages_scanned=1, error="network down", interrupted=False)

    monkeypatch.setattr(scanning, "WonderCVCrawler", FailedCrawler)
    db_path = tmp_path / "jobs.sqlite"
    result = run_daily_workflow(_config(feishu=False), db_path, reporter=scanning.RunReporter(report_sink=reports.append))

    assert result.status == "failed"
    assert result.exit_code == 1
    assert reports[-1].status == "failed"
    assert _latest_run(db_path)["status"] == "failed"


def test_partial_fetch_with_jobs_preserves_results_and_is_not_failed(monkeypatch, tmp_path: Path):
    class PartialCrawler(_Crawler):
        def crawl(self, mode, should_stop):
            result = super().crawl(mode, should_stop)
            result.error = "one page failed"
            return result

    monkeypatch.setattr(scanning, "WonderCVCrawler", PartialCrawler)
    db_path = tmp_path / "jobs.sqlite"
    result = run_daily_workflow(_config(feishu=False), db_path)

    assert result.status == "partial_success"
    assert result.exit_code == 0
    assert result.fetched_count == 1
    assert _latest_run(db_path)["status"] == "partial"


def test_successful_empty_source_is_success_and_reported_consistently(monkeypatch, tmp_path: Path):
    class EmptyCrawler:
        def __init__(self, config, cancel_check=None):
            pass

        def crawl(self, mode, should_stop):
            return SimpleNamespace(
                jobs=[], pages_scanned=1, error=None, interrupted=False,
                sources_attempted=1, sources_succeeded=1, sources_failed=0,
            )

    reports = []
    monkeypatch.setattr(scanning, "WonderCVCrawler", EmptyCrawler)
    result = run_daily_workflow(
        _config(feishu=False),
        tmp_path / "jobs.sqlite",
        reporter=scanning.RunReporter(report_sink=reports.append),
    )

    assert result.status == "success"
    assert result.exit_code == 0
    assert result.fetched_count == 0
    assert result.sources_succeeded == 1
    assert reports[-1].status == "success"
    assert _latest_run(tmp_path / "jobs.sqlite")["status"] == "success"


def test_all_sources_failed_with_no_jobs_is_failed_and_reported(monkeypatch, tmp_path: Path):
    class FailedCrawler:
        def __init__(self, config, cancel_check=None):
            pass

        def crawl(self, mode, should_stop):
            return SimpleNamespace(
                jobs=[], pages_scanned=1, error="network down", interrupted=False,
                sources_attempted=1, sources_succeeded=0, sources_failed=1,
            )

    reports = []
    monkeypatch.setattr(scanning, "WonderCVCrawler", FailedCrawler)
    db_path = tmp_path / "jobs.sqlite"
    result = run_daily_workflow(
        _config(feishu=False),
        db_path,
        reporter=scanning.RunReporter(report_sink=reports.append),
    )

    assert result.status == "failed"
    assert result.exit_code != 0
    assert reports[-1].status == "failed"
    assert _latest_run(db_path)["status"] == "failed"


def test_notification_requires_boolean_true_and_failed_stage_is_not_done(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(scanning, "WonderCVCrawler", _Crawler)
    monkeypatch.setattr(scanning, "FeishuBitableClient", lambda config: object())
    monkeypatch.setattr(
        scanning,
        "pull_user_states_from_feishu",
        lambda repo, client: SimpleNamespace(updated_count=0, skipped_record_ids=[], unknown_statuses={}),
    )
    monkeypatch.setattr(scanning, "OfficialUrlFinder", lambda: SimpleNamespace(find_best=lambda job: None))
    monkeypatch.setattr(scanning, "sync_feishu", lambda *args, **kwargs: SyncSummary())

    class RaisesOnSent:
        @property
        def sent(self):
            raise RuntimeError("bad result object")

    values = [False, "false", 1, None, object(), RaisesOnSent()]
    for index, value in enumerate(values):
        monkeypatch.setattr(
            scanning,
            "FeishuBot",
            lambda _url, value=value: SimpleNamespace(send_text=lambda _text: value),
        )
        events = []
        db_path = tmp_path / f"notify-{index}.sqlite"
        result = run_daily_workflow(
            _config(), db_path, reporter=scanning.RunReporter(event_sink=events.append)
        )
        assert result.status == "partial_success"
        assert result.exit_code != 0
        assert result.notification_status == "failed"
        assert _latest_run(db_path)["notification_status"] == "failed"
        notification_events = [event for event in events if event.step == 6]
        assert notification_events[-1].status == "failed"
        assert all(event.status != "done" for event in notification_events[-1:])


def test_safe_exception_detail_never_raises_for_hostile_values():
    class BadStr(Exception):
        def __str__(self):
            raise RuntimeError("cannot stringify")

    detail = safe_exception_detail(BadStr(), {"feishu": {"app_secret": "SECRET"}})
    assert isinstance(detail, str)
    assert "SECRET" not in detail
    assert isinstance(redact_text(BadStr()), str)
    assert isinstance(redact_text(b"access_token: SECRET"), str)
    links = redact_text("https://example.com/jobs/123 https://example.com/hook/SECRET?token=QUERY")
    assert "https://example.com/jobs/123" in links
    assert "SECRET" not in links and "QUERY" not in links


def test_logging_filter_redacts_exception_traceback_without_dropping_event():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SensitiveDataFilter(("SECRET-TOKEN",)))
    logger = logging.getLogger("daily-audit-filter")
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    try:
        try:
            raise RuntimeError("Authorization: Bearer SECRET-TOKEN")
        except RuntimeError:
            logger.exception("crawler failed")
    finally:
        logger.removeHandler(handler)
        handler.close()
    output = stream.getvalue()
    assert "crawler failed" in output
    assert "SECRET-TOKEN" not in output


def test_crawler_error_is_safe_in_log_result_database_and_notification(monkeypatch, tmp_path: Path, caplog):
    raw = "Authorization: Bearer SECRET-TOKEN app_secret=APP-SECRET https://x.test/hook/SECRET-PATH"

    class Crawler:
        def __init__(self, config, cancel_check=None):
            pass

        def crawl(self, mode, should_stop):
            return SimpleNamespace(
                jobs=[Job(dedupe_key="safe:1", company="C", title="T")],
                pages_scanned=1, error=raw, interrupted=False,
                sources_attempted=1, sources_succeeded=1, sources_failed=1,
            )

    captured = {}
    monkeypatch.setattr(scanning, "WonderCVCrawler", Crawler)
    monkeypatch.setattr(scanning, "FeishuBitableClient", lambda config: object())
    monkeypatch.setattr(
        scanning,
        "pull_user_states_from_feishu",
        lambda repo, client: SimpleNamespace(updated_count=0, skipped_record_ids=[], unknown_statuses={}),
    )
    monkeypatch.setattr(scanning, "OfficialUrlFinder", lambda: SimpleNamespace(find_best=lambda job: None))
    monkeypatch.setattr(scanning, "sync_feishu", lambda *args, **kwargs: SyncSummary())

    class Bot:
        def __init__(self, url):
            pass

        def send_text(self, message):
            captured["message"] = message
            return FeishuResult(sent=True)

    monkeypatch.setattr(scanning, "FeishuBot", Bot)
    with caplog.at_level(logging.WARNING):
        result = run_daily_workflow(_config(), tmp_path / "jobs.sqlite")

    db_error = _latest_run(tmp_path / "jobs.sqlite")["error_message"] or ""
    for secret in ("SECRET-TOKEN", "APP-SECRET", "SECRET-PATH"):
        assert secret not in caplog.text
        assert secret not in captured["message"]
        assert secret not in db_error


def test_real_crawler_logging_and_sync_error_are_redacted(tmp_path: Path, caplog):
    raw = "Authorization: Bearer SECRET-TOKEN app_secret=APP-SECRET https://x.test/hook/SECRET-PATH"

    def bad_get(*args, **kwargs):
        raise RuntimeError(raw)

    with caplog.at_level(logging.ERROR):
        crawl = WonderCVCrawler(
            {"crawler": {"max_pages_daily": 1}},
            get=bad_get,
            sleep=lambda _: None,
            progress=lambda _: None,
        ).crawl("daily")
    assert all(secret not in caplog.text for secret in ("SECRET-TOKEN", "APP-SECRET", "SECRET-PATH"))
    assert all(secret not in (crawl.error or "") for secret in ("SECRET-TOKEN", "APP-SECRET", "SECRET-PATH"))

    repo = JobRepository(tmp_path / "sync.sqlite")
    repo.init_schema()
    inserted = repo.upsert_job(Job(dedupe_key="sync-safe:1", company="C", title="T"))
    repo.append_recommendations("2026-07-13", [{"job_id": inserted.job_id, "recommend_reason": "test"}])
    config = _config()

    class Client:
        def __init__(self, config):
            pass

        def batch_create_records(self, records):
            return SimpleNamespace(sent=False, error=raw)

    with caplog.at_level(logging.INFO):
        summary = scanning.sync_feishu(
            repo, config, repo.list_feishu_sync_candidates(), client_factory=Client
        )
    assert summary.failed == 1
    with repo.connect() as conn:
        sync_error = conn.execute("SELECT sync_error FROM feishu_sync").fetchone()[0] or ""
    assert all(secret not in sync_error for secret in ("SECRET-TOKEN", "APP-SECRET", "SECRET-PATH"))
    assert all(secret not in caplog.text for secret in ("SECRET-TOKEN", "APP-SECRET", "SECRET-PATH"))


def test_initialization_failure_reports_once_without_secondary_exception(monkeypatch, tmp_path: Path):
    reports = []

    def fail_init(self):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(JobRepository, "init_schema", fail_init)
    result = run_daily_workflow(
        _config(feishu=False),
        tmp_path / "unavailable.sqlite",
        reporter=scanning.RunReporter(report_sink=reports.append),
    )
    assert result.status == "failed"
    assert result.exit_code != 0
    assert reports[-1].status == "failed"


def test_database_recording_failure_is_nonzero(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(scanning, "WonderCVCrawler", _Crawler)

    def fail_record(self, values):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(JobRepository, "record_scan_run", fail_record)
    result = run_daily_workflow(_config(feishu=False), tmp_path / "record-failure.sqlite")

    assert result.status == "partial_success"
    assert result.exit_code != 0
    assert result.errors[-1] == DailyStageError(
        "database", "database_failed", "每日运行结果保存失败"
    )


def test_database_maintenance_failure_is_nonzero(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(scanning, "WonderCVCrawler", _Crawler)

    def fail_vacuum(self):
        raise RuntimeError("database maintenance failed")

    monkeypatch.setattr(JobRepository, "vacuum", fail_vacuum)
    db_path = tmp_path / "maintenance-failure.sqlite"
    result = run_daily_workflow(_config(feishu=False), db_path)

    assert result.status == "partial_success"
    assert result.exit_code != 0
    assert result.errors[-1] == DailyStageError(
        "database", "database_failed", "每日数据库维护失败"
    )
    assert _latest_run(db_path)["status"] == "partial"


def test_already_running_reports_failed_without_creating_duplicate_run(monkeypatch, tmp_path: Path):
    reports = []

    def already_running(self):
        raise scanning.DailyRunInProgress("busy")

    monkeypatch.setattr(scanning.DailyRunGuard, "__enter__", already_running)
    db_path = tmp_path / "busy.sqlite"
    result = run_daily_workflow(
        _config(feishu=False), db_path,
        reporter=scanning.RunReporter(report_sink=reports.append),
    )
    assert result.status == "failed"
    assert result.exit_code != 0
    assert result.errors[0].code == "daily_already_running"
    assert reports[-1].status == "failed"
    with JobRepository(db_path).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 0
