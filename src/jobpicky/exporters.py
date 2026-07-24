from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import Workbook


ALL_JOBS_FIELD_MAP = {
    "job_id": "岗位ID",
    "company": "公司",
    "summary": "公告摘要",
    "batch": "批次",
    "target_graduate_year": "届别",
    "city": "城市",
    "company_type": "公司类型",
    "job_tags": "岗位标签",
    "original_url": "原始链接",
    "verify_status": "核验状态",
    "recommendation_status": "推荐状态",
    "feishu_collected_date": "收录日期",
    "user_status": "用户状态",
    "note": "备注",
    "official_url": "官方链接",
}

RECOMMENDED_JOBS_FIELD_MAP = {
    "feishu_collected_date": "收录日期",
    "job_id": "岗位ID",
    "company": "公司",
    "city": "城市",
    "batch": "批次",
    "target_graduate_year": "届别",
    "summary": "摘要",
    "original_url": "原始链接",
    "verify_status": "核验状态",
    "user_status": "用户状态",
    "note": "备注",
    "official_url": "官方链接",
}

FIELD_MAP = ALL_JOBS_FIELD_MAP
HEADERS = list(FIELD_MAP.values())


DATE_FIELDS = {"collected_date", "deadline", "first_seen", "last_seen", "recommendation_date", "feishu_collected_date"}


def _parse_date_to_timestamp_ms(val: Any) -> int | None:
    if not val:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    from datetime import datetime, timedelta, timezone

    feishu_timezone = timezone(timedelta(hours=8))
    val_str = str(val).strip()
    try:
        # Support ISO 8601 formats including Z and offset timezone info
        dt = datetime.fromisoformat(val_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=feishu_timezone)
        return int(dt.timestamp() * 1000)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(val_str, fmt)
            dt = dt.replace(tzinfo=feishu_timezone)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def build_feishu_record(row: dict[str, Any], table: str = "all") -> dict[str, Any]:
    field_map = _field_map_for(table)
    fields: dict[str, Any] = {}
    normalized = _normalize_all_job_row(row) if table == "all" else dict(row)
    for source_field, feishu_field in field_map.items():
        value = normalized.get(source_field)
        if value is None:
            continue
        if source_field in {"original_url", "apply_url", "official_url"} and value:
            fields[feishu_field] = {"link": str(value), "text": str(value)}
        elif source_field in DATE_FIELDS:
            ts = _parse_date_to_timestamp_ms(value)
            if ts is not None:
                fields[feishu_field] = ts
            else:
                fields[feishu_field] = _excel_value(value)
        elif source_field == "job_id":
            fields[feishu_field] = str(value)
        else:
            fields[feishu_field] = _excel_value(value)
    fields.setdefault("用户状态", "未看")
    fields.setdefault("备注", "")
    return {"fields": fields}


def export_jobs_to_excel(rows: list[dict[str, Any]], output_path: str | Path, table: str = "all") -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    field_map = _field_map_for(table)
    normalized_rows = [_normalize_all_job_row(row) for row in rows] if table == "all" else rows

    wb = Workbook()
    ws = wb.active
    ws.title = "RecommendedJobs" if table == "recommended" else "AllJobs"
    ws.append(list(field_map.values()))
    for row in normalized_rows:
        ws.append([_excel_value(row.get(source_field)) for source_field in field_map])
    for column in ws.columns:
        max_length = max(len(str(cell.value or "")) for cell in column)
        ws.column_dimensions[column[0].column_letter].width = min(max(max_length + 2, 10), 60)
    wb.save(output)
    return output


def _field_map_for(table: str) -> dict[str, str]:
    if table == "recommended":
        return RECOMMENDED_JOBS_FIELD_MAP
    return ALL_JOBS_FIELD_MAP


def _normalize_all_job_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    normalized.setdefault("job_id", row.get("id"))
    normalized.setdefault("title", row.get("clean_title") or row.get("title"))
    if "announcement_url" in row:
        normalized.setdefault("original_url", row.get("announcement_url"))
    else:
        normalized.setdefault("original_url", row.get("detail_url"))
    if row.get("official_url_source") not in (None, "givemeoc"):
        normalized["official_url"] = None
    normalized["apply_url"] = None
    normalized.setdefault("recommendation_status", "推荐" if row.get("recommendation_date") else "不推荐")
    normalized.setdefault("recommendation_date", row.get("recommendation_date"))
    normalized.setdefault("recommend_reason", row.get("recommend_reason") or "")
    normalized["user_status"] = row.get("user_status") or row.get("status") or "未看"
    normalized["note"] = row.get("note") or ""
    return normalized


def _excel_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, list):
        return ";".join(str(item) for item in value)
    return value
