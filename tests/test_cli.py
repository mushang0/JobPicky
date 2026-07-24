from pathlib import Path
import gc

from jobpicky.cli import main, _notification_rows, _run_enrich_official_urls, _run_reset
from jobpicky.models import Job
from jobpicky.feishu import FeishuResult
from jobpicky.storage import JobRepository


def test_cli_export_writes_excel_from_existing_database(tmp_path: Path):
    db_path = tmp_path / "jobs.sqlite"
    output = tmp_path / "all_jobs.xlsx"
    repo = JobRepository(db_path)
    repo.init_schema()
    repo.upsert_job(
        Job(
            source="WonderCV",
            dedupe_key="WonderCV:id:1",
            company="示例公司",
            title="FPGA工程师",
        )
    )

    exit_code = main(["export", "--db", str(db_path), "--output", str(output)])

    assert exit_code == 0
    assert output.exists()


def test_reset_requires_explicit_confirmation(tmp_path: Path, capsys):
    config = {"feishu": {"workspace_table_id": "tbl-test"}}

    assert _run_reset(config, str(tmp_path / "jobs.sqlite"), str(tmp_path / "config.yaml"), "out.xlsx", confirmed=False) == 2
    assert "--yes" in capsys.readouterr().out


def test_reset_keeps_local_state_when_delete_fails(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "jobs.sqlite"
    repo = JobRepository(db_path)
    repo.init_schema()
    job = repo.upsert_job(Job(dedupe_key="reset:1", company="Keep", title="Engineer"))
    repo.mark_sync(job.job_id, "synced", record_id="rec-keep")
    config = {
        "user_profile": {"graduate_years": ["2027"], "role_groups": ["hardware"]},
        "feishu": {"base_url": "https://example.feishu.cn/base/app", "app_id": "id", "app_secret": "secret", "workspace_table_id": "tbl-test"},
    }

    class Client:
        def __init__(self, _config):
            pass

        def list_tables(self):
            return [{"table_id": "tbl-test", "name": "求职工作台"}]

        def delete_table(self, _table_id):
            raise RuntimeError("delete failed")

    monkeypatch.setattr("jobpicky.cli.FeishuBitableClient", Client)
    assert _run_reset(config, str(db_path), str(tmp_path / "config.yaml"), "out.xlsx", confirmed=True) == 1
    assert config["feishu"]["workspace_table_id"] == "tbl-test"
    assert repo.sync_job_ids_by_record_id() == {"rec-keep": job.job_id}


def test_reset_clears_sync_state_before_reinitializing(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "jobs.sqlite"
    repo = JobRepository(db_path)
    repo.init_schema()
    job = repo.upsert_job(Job(dedupe_key="reset:2", company="Replace", title="Engineer"))
    repo.mark_sync(job.job_id, "synced", record_id="rec-replace")
    del repo
    gc.collect()
    config = {
        "user_profile": {"graduate_years": ["2027"], "role_groups": ["hardware"]},
        "feishu": {"base_url": "https://example.feishu.cn/base/app", "app_id": "id", "app_secret": "secret", "workspace_table_id": "tbl-test", "workspace_schema_version": "2"},
    }

    class Client:
        def __init__(self, _config):
            pass

        def list_tables(self):
            return [{"table_id": "tbl-test", "name": "求职工作台"}]

        def delete_table(self, table_id):
            assert table_id == "tbl-test"

    captured = {}

    def reinitialize(updated_config, *_args, **kwargs):
        captured["config"] = updated_config
        assert kwargs["assume_yes"] is True
        assert kwargs["seeded_from_reset"] is True
        return 0

    monkeypatch.setattr("jobpicky.cli.FeishuBitableClient", Client)
    monkeypatch.setattr("jobpicky.cli._run_init", reinitialize)
    assert _run_reset(config, str(db_path), str(tmp_path / "config.yaml"), "out.xlsx", confirmed=True) == 0
    assert JobRepository(db_path).sync_job_ids_by_record_id() == {}
    assert captured["config"]["feishu"]["workspace_table_id"] == ""


def test_reset_resolves_missing_base_url_and_table_id_from_configured_token(tmp_path: Path, monkeypatch):
    config = {
        "user_profile": {"graduate_years": ["2027"], "role_groups": ["hardware"]},
        "feishu": {"bitable_app_token": "app-token", "app_id": "id", "app_secret": "secret"},
    }
    events = []

    class Client:
        def __init__(self, _config):
            pass

        def list_tables(self):
            events.append("list")
            return [{"name": "求职工作台", "table_id": "tbl-discovered"}]

        def delete_table(self, table_id):
            events.append(f"delete:{table_id}")

    def reinitialize(updated_config, *_args, **_kwargs):
        events.append("init")
        assert updated_config["feishu"]["base_url"] == "https://feishu.cn/base/app-token"
        return 0

    monkeypatch.setattr("jobpicky.cli.FeishuBitableClient", Client)
    monkeypatch.setattr("jobpicky.cli._run_init", reinitialize)

    assert _run_reset(config, str(tmp_path / "jobs.sqlite"), str(tmp_path / "config.yaml"), "out.xlsx", confirmed=True) == 0
    assert events == ["list", "delete:tbl-discovered", "init"]


def test_reset_recovers_when_saved_workspace_was_already_deleted(tmp_path: Path, monkeypatch):
    config = {
        "user_profile": {"graduate_years": ["2027"], "role_groups": ["hardware"]},
        "feishu": {"base_url": "https://example.feishu.cn/base/app", "app_id": "id", "app_secret": "secret", "workspace_table_id": "tbl-missing"},
    }
    events = []

    class Client:
        def __init__(self, _config):
            pass

        def list_tables(self):
            events.append("list")
            return []

    monkeypatch.setattr("jobpicky.cli.FeishuBitableClient", Client)
    monkeypatch.setattr("jobpicky.cli.restore_seed_database", lambda *_args, **_kwargs: events.append("restore") or True)
    monkeypatch.setattr("jobpicky.cli._run_init", lambda updated, *_args, **kwargs: events.append("init") or int(kwargs["seeded_from_reset"] is not True))

    assert _run_reset(config, str(tmp_path / "jobs.sqlite"), str(tmp_path / "config.yaml"), "out.xlsx", confirmed=True) == 0
    assert events == ["list", "restore", "init"]
    assert config["feishu"]["workspace_table_id"] == ""


def test_cli_rematch_backfills_recommendations(tmp_path: Path):
    db_path = tmp_path / "jobs.sqlite"
    config_path = tmp_path / "config.yaml"
    repo = JobRepository(db_path)
    repo.init_schema()
    repo.upsert_job(
        Job(
            source="WonderCV",
            dedupe_key="WonderCV:id:1",
            company="示例公司",
            title="2027届FPGA工程师",
            batch="秋招",
            target_graduate_year="2027届",
        )
    )
    config_path.write_text(
        """
user_profile:
  graduate_years: ["2027届"]
  batches: ["秋招"]
  role_groups: ["硬件/嵌入式"]
system_taxonomy:
  role_groups:
    硬件/嵌入式: ["FPGA"]
""",
        encoding="utf-8",
    )

    exit_code = main(["--config", str(config_path), "--db", str(db_path), "rematch", "--date", "2026-07-03"])

    assert exit_code == 0
    assert repo.list_recommended_jobs("2026-07-03")[0]["recommend_reason"] == "命中岗位方向：嵌入式开发"
def test_internal_enrich_official_urls_runs_on_recommended_jobs(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "jobs.sqlite"
    config_path = tmp_path / "config.yaml"
    repo = JobRepository(db_path)
    repo.init_schema()
    result = repo.upsert_job(
        Job(
            source="WonderCV",
            dedupe_key="WonderCV:id:1",
            company="TargetCo",
            title="FPGA工程师",
        )
    )
    repo.append_recommendations("2026-07-09", [{"job_id": result.job_id, "recommend_reason": "推荐"}])
    config_path.write_text("crawler: {}\n", encoding="utf-8")

    class Finder:
        def __init__(self, *args, **kwargs):
            pass

        def find_best(self, job: Job) -> str:
            return "https://careers.example.com/target"

    monkeypatch.setattr("jobpicky.cli.OfficialUrlFinder", Finder)

    exit_code = _run_enrich_official_urls({"crawler": {}}, str(db_path), limit=1)

    assert exit_code == 0
    assert repo.list_all_jobs()[0]["official_url"] == "https://careers.example.com/target"


def test_cli_rematch_enriches_official_urls_and_syncs_feishu(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "jobs.sqlite"
    config_path = tmp_path / "config.yaml"
    repo = JobRepository(db_path)
    repo.init_schema()
    result = repo.upsert_job(
        Job(
            source="WonderCV",
            dedupe_key="WonderCV:id:1",
            company="TargetCo",
            title="2027 FPGA engineer",
            batch="fall",
            target_graduate_year="2027",
        )
    )
    repo.mark_sync(result.job_id, "synced", record_id="rec-target")
    config_path.write_text(
        """
user_profile:
  graduate_years: ["2027"]
  batches: ["fall"]
  role_groups: ["hardware"]
  target_cities: []
  must_watch_companies: []
  exclude_role_groups: []
system_taxonomy:
  role_groups:
    hardware: ["FPGA"]
  exclude_role_groups: {}
  generic_role_terms: []
  important_company_types: []
  important_company_marks: []
  company_aliases: {}
givemeoc:
  enabled: true
feishu:
  bitable_app_token: base
  table_id: tbl
  tenant_access_token: token
""",
        encoding="utf-8",
    )
    captured = {}

    class Finder:
        def __init__(self, *args, **kwargs):
            pass

        def find_best(self, job: Job) -> str:
            return "https://careers.example.com/target"

    class Client:
        def __init__(self, config):
            pass

        def list_all_records(self):
            return [{"record_id": "rec-target", "fields": {"岗位ID": str(result.job_id)}}]

        def batch_create_records(self, records):
            raise AssertionError("create should not be called")

        def batch_update_records(self, records):
            captured["records"] = records
            return FeishuResult(sent=True)

    monkeypatch.setattr("jobpicky.cli.OfficialUrlFinder", Finder)
    monkeypatch.setattr("jobpicky.cli.FeishuBitableClient", Client)

    exit_code = main(["--config", str(config_path), "--db", str(db_path), "rematch", "--date", "2026-07-09"])

    assert exit_code == 0
    row = repo.list_all_jobs()[0]
    assert row["official_url"] == "https://careers.example.com/target"
    assert row["sync_status"] == "synced"
    assert captured["records"][0]["record_id"] == "rec-target"
    assert captured["records"][0]["fields"]["投递入口"]["link"] == "https://careers.example.com/target"


def test_cli_rematch_no_feishu_skips_enrichment_and_sync(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "jobs.sqlite"
    config_path = tmp_path / "config.yaml"
    repo = JobRepository(db_path)
    repo.init_schema()
    repo.upsert_job(
        Job(
            source="WonderCV",
            dedupe_key="WonderCV:id:1",
            company="TargetCo",
            title="2027 FPGA engineer",
            batch="fall",
            target_graduate_year="2027",
        )
    )
    config_path.write_text(
        """
user_profile:
  graduate_years: ["2027"]
  batches: ["fall"]
  role_groups: ["hardware"]
  target_cities: []
  must_watch_companies: []
  exclude_role_groups: []
system_taxonomy:
  role_groups:
    hardware: ["FPGA"]
  exclude_role_groups: {}
  generic_role_terms: []
  important_company_types: []
  important_company_marks: []
  company_aliases: {}
""",
        encoding="utf-8",
    )

    class Finder:
        def __init__(self, *args, **kwargs):
            raise AssertionError("official URL enrichment should not run")

    class Client:
        def __init__(self, config):
            raise AssertionError("feishu sync should not run")

    monkeypatch.setattr("jobpicky.cli.OfficialUrlFinder", Finder)
    monkeypatch.setattr("jobpicky.cli.FeishuBitableClient", Client)

    exit_code = main([
        "--config",
        str(config_path),
        "--db",
        str(db_path),
        "rematch",
        "--date",
        "2026-07-09",
        "--no-feishu",
    ])

    assert exit_code == 0
    assert len(repo.list_recommended_jobs("2026-07-09")) == 1
    assert repo.list_all_jobs()[0]["official_url"] is None


def test_cli_daily_enriches_official_urls_before_feishu_sync(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "jobs.sqlite"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
user_profile:
  graduate_years: ["2027"]
  batches: ["fall"]
  role_groups: ["hardware"]
  target_cities: []
  must_watch_companies: []
  exclude_role_groups: []
system_taxonomy:
  role_groups:
    hardware: ["FPGA"]
  exclude_role_groups: {}
  generic_role_terms: []
  important_company_types: []
  important_company_marks: []
  company_aliases: {}
givemeoc:
  enabled: true
feishu:
  bitable_app_token: base
  table_id: tbl
  tenant_access_token: token
  webhook_url: https://example.com/hook
""",
        encoding="utf-8",
    )
    captured = {}

    class Crawler:
        def __init__(self, config, cancel_check=None):
            pass

        def crawl(self, mode, should_stop):
            return type(
                "Crawl",
                (),
                {
                    "jobs": [
                        Job(
                            source="WonderCV",
                            dedupe_key="WonderCV:id:daily",
                            company="DailyCo",
                            title="2027 FPGA engineer",
                            batch="fall",
                            target_graduate_year="2027",
                        )
                    ],
                    "pages_scanned": 1,
                    "error": None,
                },
            )()

    class Finder:
        def __init__(self, *args, **kwargs):
            pass

        def find_best(self, job: Job) -> str:
            return "https://careers.example.com/daily"

    class GiveMeOCCrawler:
        def __init__(self, config, cancel_check=None, progress=None):
            pass

        def crawl(self, jobs, mode="daily"):
            from jobpicky.givemeoc import GiveMeOCCrawlResult, GiveMeOCRecord

            return GiveMeOCCrawlResult(
                records=(GiveMeOCRecord(
                    source_record_id="oc-daily",
                    company="DailyCo",
                    company_normalized="dailyco",
                    official_url="https://careers.example.com/daily",
                ),),
                pages_scanned=1,
                complete=True,
            )

    class Client:
        def __init__(self, config):
            pass

        def list_all_records(self):
            return []

        def batch_create_records(self, records):
            captured["create_records"] = records
            return FeishuResult(sent=True, record_ids=["rec-daily"])

        def batch_update_records(self, records):
            raise AssertionError("update should not be called for a new Feishu row")

    class Bot:
        def __init__(self, webhook_url):
            pass

        def send_text(self, text):
            captured["message"] = text
            return FeishuResult(sent=True)

    monkeypatch.setattr("jobpicky.services.scanning.WonderCVCrawler", Crawler)
    monkeypatch.setattr("jobpicky.services.scanning.GiveMeOCCrawler", GiveMeOCCrawler)
    monkeypatch.setattr("jobpicky.services.scanning.OfficialUrlFinder", Finder)
    monkeypatch.setattr("jobpicky.services.scanning.FeishuBitableClient", Client)
    monkeypatch.setattr("jobpicky.services.scanning.FeishuBot", Bot)

    exit_code = main(["--config", str(config_path), "--db", str(db_path), "daily"])

    assert exit_code == 0
    repo = JobRepository(db_path)
    row = repo.list_all_jobs()[0]
    assert row["official_url"] == "https://careers.example.com/daily"
    assert row["sync_status"] == "synced"
    assert captured["create_records"][0]["fields"]["投递入口"]["link"] == "https://careers.example.com/daily"


def test_notification_rows_obeys_daily_push_limit_without_limiting_recommendations():
    rows = [
        {"recommendation_status": "推荐", "sync_status": "pending", "job_id": 1},
        {"recommendation_status": "推荐", "sync_status": "pending", "job_id": 2},
        {"recommendation_status": "推荐", "sync_status": "pending", "job_id": 3},
    ]

    limited = _notification_rows(rows, {"user_profile": {"daily_push_limit": 1}})

    assert [row["job_id"] for row in limited] == [1]


def test_cli_rematch_keeps_previously_synced_job_that_is_no_longer_recommended(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "jobs.sqlite"
    config_path = tmp_path / "config.yaml"
    repo = JobRepository(db_path)
    repo.init_schema()
    inserted = repo.upsert_job(Job(source="WonderCV", dedupe_key="stale:1", company="OldCo", title="销售经理"))
    repo.append_recommendations("2026-07-10", [{"job_id": inserted.job_id, "recommend_reason": "old"}])
    repo.mark_sync(inserted.job_id, "synced", record_id="rec-stale")
    config_path.write_text(
        """
user_profile:
  graduate_years: ["2027届"]
  batches: ["秋招"]
  role_groups: ["硬件"]
  exclude_role_groups: ["销售"]
system_taxonomy:
  role_groups:
    硬件: ["FPGA"]
  exclude_role_groups:
    销售: ["销售"]
  generic_role_terms: []
  important_company_types: []
  important_company_marks: []
  company_aliases: {}
feishu:
  bitable_app_token: base
  table_id: tbl
  tenant_access_token: token
""",
        encoding="utf-8",
    )
    captured = {}

    class Client:
        def __init__(self, config):
            pass

        def list_all_records(self):
            return [{"record_id": "rec-stale", "fields": {"求职状态": "待处理"}}]

        def batch_create_records(self, records):
            raise AssertionError("inactive record already exists")

        def batch_update_records(self, records):
            captured["records"] = records
            return FeishuResult(sent=True)

    monkeypatch.setattr("jobpicky.cli.FeishuBitableClient", Client)

    code = main(["--config", str(config_path), "--db", str(db_path), "rematch", "--no-enrich-official"])

    assert code == 0
    assert captured["records"][0]["record_id"] == "rec-stale"
    assert "推荐有效" not in captured["records"][0]["fields"]


def test_cli_pull_command(tmp_path, monkeypatch):
    from unittest.mock import patch
    from jobpicky.cli import main
    db_file = tmp_path / "jobs.sqlite"

    with patch("jobpicky.cli.FeishuBitableClient") as mock_client_cls, \
         patch("jobpicky.cli.pull_user_states_from_feishu") as mock_pull:
        mock_pull.return_value = type(
            "Recovery",
            (),
            {"updated_count": 5, "skipped_record_ids": [], "unknown_statuses": {}},
        )()
        code = main(["--db", str(db_file), "pull"])
        assert code == 0
        mock_pull.assert_called_once()

