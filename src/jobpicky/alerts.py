from __future__ import annotations

from typing import Any

from .error_safety import redact_text


def build_daily_message(total_new: int, relevant_rows: list[dict[str, Any]], error: str | None = None) -> str:
    lines = [f"今日新增秋招信息：{total_new} 条", f"推荐岗位：{len(relevant_rows)} 条"]
    if error:
        error = redact_text(error)
        lines.append(f"抓取异常：{error}")
    if not relevant_rows:
        lines.append("暂无需要推送的推荐岗位。")
        return "\n".join(lines)

    for index, row in enumerate(relevant_rows[:20], start=1):
        company = row.get("company") or "未知公司"
        title = row.get("title") or row.get("clean_title") or "未命名公告"
        city = row.get("city") or "城市待确认"
        official_url = row.get("official_url") if row.get("official_url_source") == "givemeoc" else ""
        link = row.get("original_url") or official_url or ""
        lines.extend(
            [
                "",
                f"{index}. {company} - {title} - {city}",
            ]
        )
        if link:
            lines.append(str(link))
    if len(relevant_rows) > 20:
        lines.append(f"\n其余 {len(relevant_rows) - 20} 条请在飞书视图查看。")
    return "\n".join(lines)
