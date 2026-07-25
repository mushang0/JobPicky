import sqlite3
import json
from pathlib import Path

from jobpicky.seed import restore_seed_database
from scripts.build_seed import build_seed
from scripts.export_seed_source import export_seed_source


ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "src" / "jobpicky" / "resources" / "jobs_seed.sqlite"
SOURCE = ROOT / "src" / "jobpicky" / "resources" / "jobs_seed_source.json"


def _make_seed(path: Path, value: str) -> None:
    connection = sqlite3.connect(path)
    connection.execute("create table marker (value text)")
    connection.execute("insert into marker values (?)", (value,))
    connection.commit()
    connection.close()


def _marker(path: Path) -> str:
    connection = sqlite3.connect(path)
    value = connection.execute("select value from marker").fetchone()[0]
    connection.close()
    return value


def test_restore_seed_creates_missing_runtime_database(tmp_path: Path, monkeypatch):
    seed = tmp_path / "jobs_seed.sqlite"
    target = tmp_path / "data" / "jobs.sqlite"
    _make_seed(seed, "seed")
    monkeypatch.setattr("jobpicky.seed.find_seed_database", lambda: seed)

    assert restore_seed_database(target) is True
    assert _marker(target) == "seed"


def test_restore_seed_keeps_existing_runtime_database_unless_overwrite_requested(tmp_path: Path, monkeypatch):
    seed = tmp_path / "jobs_seed.sqlite"
    target = tmp_path / "jobs.sqlite"
    _make_seed(seed, "seed")
    _make_seed(target, "existing")
    monkeypatch.setattr("jobpicky.seed.find_seed_database", lambda: seed)

    assert restore_seed_database(target) is False
    assert _marker(target) == "existing"
    assert restore_seed_database(target, overwrite=True) is True
    assert _marker(target) == "seed"


def _table_snapshot(path: Path, table: str, columns: list[str] | None = None) -> tuple[list[str], list[tuple]]:
    connection = sqlite3.connect(path)
    try:
        actual_columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        selected_columns = columns or actual_columns
        projection = ", ".join(f'"{column}"' for column in selected_columns)
        rows = connection.execute(f"SELECT {projection} FROM {table} ORDER BY id").fetchall()
        return actual_columns, rows
    finally:
        connection.close()


def test_canonical_source_preserves_every_seed_job_value_and_null(tmp_path: Path):
    exported = tmp_path / "jobs.json"
    document = json.loads(SOURCE.read_text(encoding="utf-8"))
    expected_jobs = len(document["tables"]["jobs"]["rows"])

    assert export_seed_source(SEED, exported) == expected_jobs
    assert json.loads(exported.read_text(encoding="utf-8")) == document

    assert document["format_version"] == 2
    assert set(document["tables"]) == {"jobs", "job_positions"}
    for table, snapshot in document["tables"].items():
        columns, rows = _table_snapshot(SEED, table)
        assert snapshot["columns"] == columns
        assert [[row[column] for column in columns] for row in snapshot["rows"]] == [list(row) for row in rows]
    jobs = document["tables"]["jobs"]["rows"]
    assert all(len(job["summary"] or "") <= 96 for job in jobs)
    assert all(job["collected_date"] <= "2026-07-23" for job in jobs)
    assert all(job["last_checked"] is None for job in jobs)
    assert all(job["extraction_version"] == "detail-structure-v3" for job in jobs)


def test_built_seed_matches_source_and_initializes_new_runtime_database(tmp_path: Path, monkeypatch):
    generated = tmp_path / "generated.sqlite"
    runtime = tmp_path / "fresh" / "jobs.sqlite"
    document = json.loads(SOURCE.read_text(encoding="utf-8"))
    columns = document["tables"]["jobs"]["columns"]
    position_columns = document["tables"]["job_positions"]["columns"]

    assert build_seed(SOURCE, generated) == len(document["tables"]["jobs"]["rows"])
    generated_columns, generated_rows = _table_snapshot(generated, "jobs", columns)
    old_columns, old_rows = _table_snapshot(SEED, "jobs", columns)
    assert set(old_columns) == set(columns)
    assert set(generated_columns) == set(columns)
    assert generated_rows == old_rows
    assert _table_snapshot(generated, "job_positions", position_columns) == _table_snapshot(
        SEED, "job_positions", position_columns
    )
    monkeypatch.setattr("jobpicky.seed.find_seed_database", lambda: generated)
    assert restore_seed_database(runtime) is True
    assert _table_snapshot(runtime, "jobs") == _table_snapshot(generated, "jobs")
    assert _table_snapshot(runtime, "job_positions") == _table_snapshot(generated, "job_positions")

    connection = sqlite3.connect(runtime)
    try:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()
    assert {"jobs", "job_matches", "recommended_jobs", "job_user_state", "scan_runs", "feishu_sync"} <= tables


def test_build_seed_is_repeatable(tmp_path: Path):
    first = tmp_path / "first.sqlite"
    second = tmp_path / "second.sqlite"
    build_seed(SOURCE, first)
    build_seed(SOURCE, second)
    assert first.read_bytes() == second.read_bytes()
