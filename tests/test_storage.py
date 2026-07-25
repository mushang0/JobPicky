import sqlite3
import json
from pathlib import Path

import pytest

from jobpicky.models import Job, Position
from jobpicky.storage import JobRepository
from jobpicky.locations import location_options


def test_repository_upserts_new_job_and_does_not_duplicate(tmp_path: Path):
    repo = JobRepository(tmp_path / "jobs.sqlite")
    repo.init_schema()
    job = Job(
        source="WonderCV",
        source_job_id="abc",
        dedupe_key="WonderCV:id:abc",
        company="示例公司",
        company_normalized="示例公司",
        title="2027校园招聘",
    )

    first = repo.upsert_job(job)
    second = repo.upsert_job(job)

    assert first.created is True
    assert second.created is False
    assert repo.count_jobs() == 1


def test_city_filter_only_keeps_jobs_in_selected_city(tmp_path: Path):
    repo = JobRepository(tmp_path / "jobs.sqlite")
    repo.init_schema()
    repo.upsert_job(Job(dedupe_key="pending:1", company="待确认公司", title="嵌入式工程师", city=None))
    repo.upsert_job(Job(dedupe_key="known:1", company="上海公司", title="嵌入式工程师", city="上海市"))
    repo.upsert_job(Job(dedupe_key="known:2", company="深圳公司", title="嵌入式工程师", city="深圳市"))

    rows, total = repo.search_jobs(city="深圳市")

    assert total == 1
    assert [row["company"] for row in rows] == ["深圳公司"]


def test_repository_filters_semantic_batch_and_matched_direction(tmp_path: Path):
    repo = JobRepository(tmp_path / "jobs.sqlite")
    repo.init_schema()
    fall = repo.upsert_job(Job(
        dedupe_key="fall:1", company="秋招公司", title="2027届校园招聘", raw_title="2027届秋招正式启动", batch="校招",
    ))
    repo.upsert_job(Job(dedupe_key="intern:1", company="实习公司", title="算法工程师", batch="日常实习"))
    repo.save_match(fall.job_id, {"matched_role_group_id": "ai.algorithm"})

    batch_rows, batch_total = repo.search_jobs(batch="秋招")
    internship_rows, internship_total = repo.search_jobs(batch="实习")
    direction_rows, direction_total = repo.search_jobs(direction="ai.algorithm")

    assert batch_total == 1
    assert [row["company"] for row in batch_rows] == ["秋招公司"]
    assert internship_total == 1
    assert [row["company"] for row in internship_rows] == ["实习公司"]
    assert direction_total == 1
    assert [row["company"] for row in direction_rows] == ["秋招公司"]


def test_repository_does_not_replace_known_collected_date_with_null(tmp_path: Path):
    repo = JobRepository(tmp_path / "jobs.sqlite")
    repo.init_schema()
    repo.upsert_job(Job(dedupe_key="date:1", company="示例公司", title="校招", collected_date="2026-07-13"))

    repo.upsert_job(Job(dedupe_key="date:1", company="示例公司", title="校招", collected_date=None))

    with repo.connect() as connection:
        row = connection.execute("SELECT collected_date FROM jobs WHERE dedupe_key = 'date:1'").fetchone()
    assert row["collected_date"] == "2026-07-13"


def test_repository_stores_multiple_structured_positions_under_one_announcement(tmp_path: Path):
    repo = JobRepository(tmp_path / "jobs.sqlite")
    repo.init_schema()
    job = Job(
        dedupe_key="announcement:1",
        company="示例科技",
        title="2027届校园招聘",
        positions=[
            Position(
                title="嵌入式工程师",
                city="深圳市",
                degree="本科",
                skills=["C++", "RTOS"],
                requirements="熟悉实时操作系统",
                confidence=0.94,
                extraction_version="position-v1",
            ),
            Position(
                title="芯片验证工程师",
                city=None,
                skills=["UVM", "SystemVerilog"],
                confidence=0.91,
                extraction_version="position-v1",
            ),
        ],
    )

    result = repo.upsert_job(job)
    positions = repo.list_positions(result.job_id)

    assert [position["title"] for position in positions] == ["嵌入式工程师", "芯片验证工程师"]
    assert positions[0]["skills"] == "C++;RTOS"
    assert positions[1]["location_status"] == "pending"
    assert len({position["position_key"] for position in positions}) == 2
    assert repo.get_job_detail(result.job_id)["positions"] == positions


def test_empty_retry_does_not_delete_previously_extracted_positions(tmp_path: Path):
    repo = JobRepository(tmp_path / "jobs.sqlite")
    repo.init_schema()
    first = repo.upsert_job(
        Job(
            dedupe_key="announcement:retry",
            company="示例科技",
            title="校园招聘",
            parse_status="detail_ready",
            positions=[Position(title="FPGA工程师")],
        )
    )

    repo.upsert_job(Job(dedupe_key="announcement:retry", company="示例科技", title="校园招聘"))

    assert [position["title"] for position in repo.list_positions(first.job_id)] == ["FPGA工程师"]


def test_repository_saves_match_result(tmp_path: Path):
    repo = JobRepository(tmp_path / "jobs.sqlite")
    repo.init_schema()
    inserted = repo.upsert_job(
        Job(
            source="WonderCV",
            dedupe_key="WonderCV:id:abc",
            company="示例公司",
            title="FPGA工程师",
        )
    )

    repo.save_match(
        inserted.job_id,
        {
            "matched_keywords": ["FPGA"],
            "matched_company_rule": "",
            "matched_city_rule": "",
            "negative_keywords": [],
            "match_score": 80,
            "priority": "push",
            "is_relevant": True,
            "should_push": True,
            "match_reason": "命中岗位方向：硬件/嵌入式",
            "verify_status": "未核验",
            "suggested_search_terms": [],
            "matched_role_group_id": "hardware.fpga",
            "matched_position_title": "FPGA工程师",
            "matched_position_key": "fpga-1",
            "match_evidence": {"keywords": ["FPGA"]},
            "decision_trace": ["hard_filters:passed", "recall:role_taxonomy"],
        },
    )

    saved = repo.get_job_with_match(inserted.job_id)
    assert saved["priority"] == "push"
    assert saved["matched_keywords"] == "FPGA"
    assert saved["matched_role_group_id"] == "hardware.fpga"
    assert saved["matched_position_title"] == "FPGA工程师"
    assert json.loads(saved["match_evidence"])["keywords"] == ["FPGA"]


def test_repository_updates_user_state(tmp_path: Path):
    repo = JobRepository(tmp_path / "jobs.sqlite")
    repo.init_schema()
    inserted = repo.upsert_job(
        Job(
            source="WonderCV",
            dedupe_key="WonderCV:id:abc",
            company="测试公司",
            title="软件研发",
        )
    )

    repo.update_user_state(inserted.job_id, "已收藏", "测试备注")

    saved = repo.get_job_with_match(inserted.job_id)
    assert saved["user_status"] == "已收藏"
    assert saved["note"] == "测试备注"


def test_repository_does_not_implicitly_copy_seed_if_missing(tmp_path: Path):
    seed_file = tmp_path / "jobs_seed.sqlite"
    seed_file.write_text("dummy database content", encoding="utf-8")

    db_file = tmp_path / "jobs.sqlite"
    assert not db_file.exists()

    # Seed restoration is an explicit init/reset concern, not a repository side effect.
    repo = JobRepository(db_file)

    assert repo.db_path == db_file
    assert not db_file.exists()


def _make_old_schema_database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                dedupe_key TEXT UNIQUE NOT NULL,
                company TEXT,
                title TEXT
            );
            CREATE TABLE job_user_state (job_id INTEGER UNIQUE, status TEXT, note TEXT);
            CREATE TABLE scan_runs (
                id INTEGER PRIMARY KEY,
                run_type TEXT,
                started_at DATETIME,
                finished_at DATETIME,
                status TEXT,
                pages_scanned INTEGER,
                items_seen INTEGER,
                new_items INTEGER,
                updated_items INTEGER,
                error_message TEXT
            );
            INSERT INTO jobs(id, dedupe_key, company, title)
                VALUES (7, 'legacy:7', 'Legacy Co', 'Legacy Job');
            INSERT INTO scan_runs(id, run_type, status, error_message)
                VALUES (3, 'daily', 'success', 'legacy record');
            """
        )


def test_repository_migrates_old_schema_preserves_data_and_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "old.sqlite"
    _make_old_schema_database(db_path)
    repo = JobRepository(db_path)

    repo.init_schema()
    repo.init_schema()

    with sqlite3.connect(db_path) as conn:
        scan_columns = {row[1] for row in conn.execute("PRAGMA table_info(scan_runs)")}
        job = conn.execute("SELECT company, title FROM jobs WHERE id = 7").fetchone()
        old_run = conn.execute("SELECT status, error_message FROM scan_runs WHERE id = 3").fetchone()
    assert "notification_status" in scan_columns
    assert job == ("Legacy Co", "Legacy Job")
    assert old_run == ("success", "legacy record")

    backups = list((tmp_path / "backups").glob("*.sqlite"))
    assert backups
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("SELECT company FROM jobs WHERE id = 7").fetchone() == ("Legacy Co",)


def test_repository_failed_migration_keeps_original_schema_data_and_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db_path = tmp_path / "old.sqlite"
    _make_old_schema_database(db_path)
    original_ensure_columns = JobRepository._ensure_columns

    def fail_mid_migration(self, conn, table, columns):
        if table == "scan_runs":
            raise RuntimeError("injected migration failure")
        return original_ensure_columns(self, conn, table, columns)

    monkeypatch.setattr(JobRepository, "_ensure_columns", fail_mid_migration)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        JobRepository(db_path).init_schema()

    conn = sqlite3.connect(db_path)
    try:
        jobs_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        scan_columns = {row[1] for row in conn.execute("PRAGMA table_info(scan_runs)")}
        job = conn.execute("SELECT company, title FROM jobs WHERE id = 7").fetchone()
    finally:
        conn.close()
    assert "raw_title" not in jobs_columns
    assert "notification_status" not in scan_columns
    assert job == ("Legacy Co", "Legacy Job")

    backups = list((tmp_path / "backups").glob("*.sqlite"))
    assert backups
    backup = sqlite3.connect(backups[0])
    try:
        assert backup.execute("SELECT COUNT(*) FROM scan_runs").fetchone() == (1,)
    finally:
        backup.close()

    monkeypatch.setattr(JobRepository, "_ensure_columns", original_ensure_columns)
    JobRepository(db_path).init_schema()


def test_province_filter_includes_all_child_cities(tmp_path: Path):
    repo = JobRepository(tmp_path / "jobs.sqlite")
    repo.init_schema()
    guangdong = next(item for item in location_options() if item["value"] == "province:44")
    guangzhou, shenzhen = (city["label"] for city in guangdong["cities"][:3:2])
    repo.upsert_job(Job(dedupe_key="province:gz", company="Guangzhou Co", title="Engineer", city=guangzhou))
    repo.upsert_job(Job(dedupe_key="province:sz", company="Shenzhen Co", title="Engineer", city=shenzhen))

    rows, total = repo.search_jobs(province="province:44")

    assert total == 2
    assert {row["company"] for row in rows} == {"Guangzhou Co", "Shenzhen Co"}


def test_company_cards_group_announcements_without_losing_source_jobs(tmp_path: Path):
    repo = JobRepository(tmp_path / "jobs.sqlite")
    repo.init_schema()
    repo.upsert_job(Job(dedupe_key="company:1", company="Example Technology Co., Ltd.", title="Hardware Engineer"))
    repo.upsert_job(Job(dedupe_key="company:2", company="Example Technology Co., Ltd.", title="Algorithm Engineer"))

    cards, total = repo.search_company_cards()

    assert total == 1
    assert cards[0]["company"] == "Example Technology Co., Ltd."
    assert {row["title"] for row in cards[0]["jobs"]} == {"Hardware Engineer", "Algorithm Engineer"}
