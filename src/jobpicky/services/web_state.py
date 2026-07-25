from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..config import (
    DEFAULT_CONFIG,
    LEGACY_DEFAULT_EXCLUDED_ROLES,
    ONBOARDING_BATCH_OPTIONS,
    ONBOARDING_ROLE_SECTIONS,
    load_config,
    save_config,
    validate_config,
)
from ..onboarding import parse_base_url
from ..locations import location_options
from ..organizations import organization_options
from ..paths import AppPaths
from ..core import DatabaseBootstrapService, JobQueryService
from ..wondercv import extract_wondercv_card_summary
from ..workspace_schema import desired_workspace


class WebStateService:
    def __init__(self, paths: AppPaths):
        self.paths = paths

    def preferences(self) -> dict[str, Any]:
        config = load_config(self.paths.config)
        profile = deepcopy(config.get("user_profile", {}))
        if set(profile.get("exclude_role_groups", [])) == set(LEGACY_DEFAULT_EXCLUDED_ROLES):
            profile["exclude_role_groups"] = []
        if not self.paths.config.is_file():
            for key in (
                "batches",
                "role_groups",
                "target_cities",
                "custom_keywords",
                "selected_company_groups",
                "custom_companies",
                "exclude_role_groups",
            ):
                profile[key] = []
        feishu = config.get("feishu", {})
        feishu_enabled = feishu.get("enabled", True) is not False
        configuration_errors = validate_config(
            config,
            require_graduate_years=False,
            require_batches=True,
        )
        return {
            "user_profile": profile,
            "feishu": {
                "base_url": feishu.get("base_url", ""),
                "app_id": feishu.get("app_id", ""),
                "secret_saved": bool(feishu.get("app_secret")),
                "configured": feishu_enabled and bool(feishu.get("base_url") and feishu.get("app_id") and feishu.get("app_secret")),
                "workspace_configured": feishu_enabled and bool(feishu.get("workspace_table_id")),
            },
            "onboarding_complete": self.paths.config.is_file() and not configuration_errors,
        }

    def onboarding_options(self) -> dict[str, Any]:
        return {
            "batches": [{"value": value, "label": value} for value in ONBOARDING_BATCH_OPTIONS],
            "role_sections": deepcopy(list(ONBOARDING_ROLE_SECTIONS)),
            "cities": [{"value": "", "label": "不限"}],
            "location_sections": location_options(),
            "exclude_role_sections": deepcopy(list(ONBOARDING_ROLE_SECTIONS)),
            "organization_groups": organization_options(),
            "matching_policy": deepcopy(DEFAULT_CONFIG["system_taxonomy"]["matching_policy"]),
        }

    def save_preferences(self, payload: dict[str, Any]) -> list[str]:
        config = load_config(self.paths.config)
        profile = config.setdefault("user_profile", {})
        incoming_profile = payload.get("user_profile") or {}
        for key in (
            "batches",
            "role_groups",
            "exclude_role_groups",
            "target_cities",
            "custom_keywords",
            "must_watch_companies",
            "selected_company_groups",
            "custom_companies",
        ):
            if key in incoming_profile:
                profile[key] = _list_values(incoming_profile[key])
        if "role_match_mode" in incoming_profile:
            mode = str(incoming_profile["role_match_mode"] or "any").strip().lower()
            profile["role_match_mode"] = mode if mode in {"any", "all"} else "any"
        if set(profile.get("exclude_role_groups", [])) == set(LEGACY_DEFAULT_EXCLUDED_ROLES):
            profile["exclude_role_groups"] = []

        incoming_feishu = payload.get("feishu") or {}
        feishu = config.setdefault("feishu", {})
        try:
            if "base_url" in incoming_feishu:
                previous_base_url = str(feishu.get("base_url") or "").strip()
                feishu["base_url"] = str(incoming_feishu["base_url"]).strip()
                if feishu["base_url"]:
                    parse_base_url(feishu["base_url"])
                if previous_base_url and previous_base_url != feishu["base_url"]:
                    for key in (
                        "workspace_table_id",
                        "workspace_schema_version",
                        "workspace_url",
                        "last_sync_at",
                        "last_successful_sync_at",
                        "last_sync_summary",
                        "baseline_items",
                        "recommended_items",
                    ):
                        feishu.pop(key, None)
            for key in ("app_id", "app_secret"):
                if key in incoming_feishu and str(incoming_feishu[key]).strip():
                    feishu[key] = str(incoming_feishu[key]).strip()
        except ValueError as exc:
            return [str(exc)]

        errors = validate_config(
            config,
            require_feishu=False,
            require_graduate_years=False,
            require_batches=True,
        )
        if errors:
            return errors
        save_config(config, self.paths.config)
        return []

    def save_feishu_credentials(self, *, base_url: str, app_id: str, app_secret: str) -> None:
        config = load_config(self.paths.config)
        parsed_base_url = str(base_url).strip()
        parse_base_url(parsed_base_url)
        feishu = config.setdefault("feishu", {})
        feishu.update({"base_url": parsed_base_url, "app_id": str(app_id).strip(), "enabled": True})
        if str(app_secret).strip():
            feishu["app_secret"] = str(app_secret).strip()
        save_config(config, self.paths.config)

    def feishu_status(self) -> dict[str, Any]:
        config = load_config(self.paths.config)
        feishu = config.get("feishu", {})
        enabled = feishu.get("enabled", True) is not False
        credentials_saved = bool(feishu.get("base_url") and feishu.get("app_id") and feishu.get("app_secret"))
        workspace_configured = bool(feishu.get("workspace_table_id"))
        return {
            "configured": enabled and credentials_saved,
            "secret_saved": credentials_saved,
            "credentials_saved": credentials_saved,
            "workspace_configured": enabled and workspace_configured,
            "workspace_url": feishu.get("workspace_url", ""),
            "workspace_name": desired_workspace().table_name,
            "last_successful_sync_at": feishu.get("last_successful_sync_at", ""),
            "last_sync_at": feishu.get("last_sync_at", ""),
            "last_sync_summary": deepcopy(feishu.get("last_sync_summary") or {}),
            "baseline_items": int(feishu.get("baseline_items") or 0),
            "recommended_items": int(feishu.get("recommended_items") or 0),
            "last_error": deepcopy(feishu.get("last_error") or None),
        }

    def disconnect_feishu(self, *, clear_credentials: bool) -> dict[str, Any]:
        config = load_config(self.paths.config)
        feishu = config.setdefault("feishu", {})
        feishu["enabled"] = False
        if clear_credentials:
            for key in ("base_url", "app_id", "app_secret", "tenant_access_token"):
                feishu[key] = ""
        save_config(config, self.paths.config)
        return self.feishu_status()

    @staticmethod
    def _public_job(item: dict[str, Any]) -> dict[str, Any]:
        item = dict(item)
        raw_title = item.pop("raw_title", "")
        item["card_summary"] = extract_wondercv_card_summary(raw_title) or item.get("summary")
        item["announcement_url"] = item.pop("verified_announcement_url", None) or item.get("announcement_url")
        item["official_url"] = item.pop("verified_official_url", None) or item.get("official_url")
        item["detail_url"] = item.pop("legacy_detail_url", None)
        item["apply_url"] = None
        return item

    @staticmethod
    def _announcement_key(row: dict[str, Any]) -> str:
        record_id = str(row.get("givemeoc_record_id") or "").strip()
        if record_id:
            return f"record:{record_id}"
        url = str(row.get("announcement_url") or "").strip().rstrip("/").casefold()
        if url:
            return f"url:{url}"
        if str(row.get("source") or "").casefold() == "wondercv":
            position_titles = sorted(
                "".join(char for char in str(position.get("title") or "").casefold() if char.isalnum())
                for position in (row.get("positions") or [])
                if str(position.get("title") or "").strip()
            )
            if position_titles:
                signature = (
                    str(row.get("company") or "").casefold(),
                    str(row.get("title") or "").casefold(),
                    str(row.get("deadline") or "").casefold(),
                    str(row.get("city") or "").casefold(),
                    ",".join(position_titles),
                )
                return "signature:" + "|".join(signature)
        return f"job:{row.get('job_id') or row.get('id') or id(row)}"

    @classmethod
    def _collapse_company_jobs(
        cls, rows: list[dict[str, Any]], limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Collapse source rows that came from the same recruitment announcement."""
        groups: dict[str, dict[str, Any]] = {}
        position_keys: dict[str, set[str]] = {}
        for row in rows:
            key = cls._announcement_key(row)
            if key not in groups:
                groups[key] = dict(row)
                groups[key]["positions"] = [dict(position) for position in (row.get("positions") or [])]
                position_keys[key] = {
                    str(position.get("title") or "").strip().casefold()
                    for position in groups[key]["positions"]
                    if str(position.get("title") or "").strip()
                }
                continue
            target = groups[key]
            known = position_keys[key]
            for position in (row.get("positions") or []):
                title = str(position.get("title") or "").strip()
                position_key = title.casefold()
                if title and position_key not in known:
                    target["positions"].append(dict(position))
                    known.add(position_key)
            for field in ("announcement_url", "official_url", "deadline", "collected_date"):
                if not target.get(field) and row.get(field):
                    target[field] = row[field]
        result = list(groups.values())
        if limit is not None:
            result = result[:limit]
        return result

    def _company_card(self, group: dict[str, Any], repo) -> dict[str, Any]:
        rows = [self._public_job(row) for row in group.get("jobs", [])]
        primary_row = next(
            (row for row in rows if str(row.get("source") or "").casefold() == "wondercv" and row.get("card_summary")),
            rows[0] if rows else {"company": group.get("company", "")},
        )
        primary = dict(primary_row)
        titles = [row.get("matched_position_title") or row.get("title") for row in rows]
        titles = list(dict.fromkeys(title for title in titles if title))
        primary.update({
            "company": group.get("company") or primary.get("company", ""),
            "company_key": group["company_key"],
            "jobs": rows,
            "job_count": min(3, len(self._collapse_company_jobs(rows))),
            "position_count": len(titles),
            "position_titles": titles,
            "matched_position_title": titles[0] if titles else "",
        })
        return primary

    def jobs(self, *, page: int = 1, page_size: int = 25, scope: str = "all", query: str = "",
             city: str = "", province: str = "", batch: str = "", direction: str = "", deadline_status: str = "",
             company_type: str = "", sort: str = "deadline", recommended: bool = False) -> dict[str, Any]:
        if not self.paths.database.is_file():
            return {"items": [], "page": 1, "page_size": page_size, "total": 0, "pages": 0,
                    "summary": {"all": 0, "recommended": 0, "today_recommended": 0,
                                "new_recommended": 0, "expiring": 0},
                    "facets": {"cities": [], "provinces": [], "batches": [], "company_types": [], "directions": []}}
        # Reads must never bootstrap/restore the packaged seed database.
        from ..storage import JobRepository
        repo = JobRepository(self.paths.database)
        queries = JobQueryService(repo)
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        recommended = recommended or scope in {"recommended", "today", "new", "expiring"}
        today_since = (date.today() - timedelta(days=1)).isoformat()
        new_since = today_since if scope in {"today", "new"} else ""
        if scope == "expiring":
            deadline_status = "expiring"
        groups, total = repo.search_company_cards(
            recommended=recommended, query=query.strip(), city=city, batch=batch,
            province=province, direction=direction, deadline_status=deadline_status,
            company_type=company_type, new_since=new_since, sort=sort,
            limit=page_size, offset=(page - 1) * page_size,
        )
        stats = queries.stats()
        items = [self._company_card(group, repo) for group in groups]
        _, today_recommended_total = repo.search_jobs(
            recommended=True, new_since=today_since, limit=1,
        )
        facets = repo.job_facets()
        facets["directions"] = _list_values(load_config(self.paths.config).get("user_profile", {}).get("role_groups", []))
        return {
            "items": items,
            "total": total,
            "pages": (total + page_size - 1) // page_size,
            "recommended_total": stats["recommendations"],
            "page": page,
            "page_size": page_size,
            "summary": {"all": stats["jobs"], "recommended": stats["recommendations"],
                        "today_recommended": today_recommended_total,
                        "new_recommended": today_recommended_total,
                        "expiring": repo.count_expiring_jobs(recommended=True)},
            "facets": facets,
        }

    def job_detail(self, job_id: int) -> dict[str, Any]:
        if not self.paths.database.is_file():
            return {}
        from ..storage import JobRepository
        item = JobRepository(self.paths.database).get_job_detail(job_id)
        if item:
            item = self._public_job(item)
        return item

    def company_detail(self, company_key: str) -> dict[str, Any]:
        if not self.paths.database.is_file():
            return {}
        from ..storage import JobRepository
        repo = JobRepository(self.paths.database)
        group = repo.get_company_detail(company_key)
        if not group:
            return {}
        jobs = []
        for row in group["jobs"]:
            detail = repo.get_job_detail(int(row["job_id"]))
            jobs.append(self._public_job(detail or row))
        jobs.sort(key=lambda row: 0 if str(row.get("source") or "").casefold() == "wondercv" else 1)
        collapsed = self._collapse_company_jobs(jobs)
        visible_jobs = collapsed
        announcement_links: list[dict[str, str]] = []
        seen_announcements: set[str] = set()
        official_url = ""
        for job in collapsed:
            announcement_url = str(job.get("announcement_url") or "").strip()
            if announcement_url and announcement_url not in seen_announcements and len(announcement_links) < 3:
                seen_announcements.add(announcement_url)
                announcement_links.append({"url": announcement_url})
            if not official_url and job.get("official_url"):
                official_url = str(job["official_url"]).strip()
        card = self._company_card({**group, "jobs": visible_jobs}, repo)
        card["jobs"] = visible_jobs
        card["card_summary"] = next(
            (str(job.get("card_summary") or "").strip() for job in collapsed if str(job.get("card_summary") or "").strip()),
            card.get("card_summary") or "",
        )
        position_titles = list(dict.fromkeys(
            str(position.get("title") or "").strip()
            for job in collapsed
            for position in (job.get("positions") or [])
            if str(position.get("title") or "").strip()
        ))
        card["position_count"] = len(position_titles) or len({
            str(job.get("title") or "").strip() for job in collapsed if str(job.get("title") or "").strip()
        })
        card["announcement_links"] = announcement_links
        card["official_url"] = official_url or None
        return card

    def scan_status(self, active_task: dict[str, Any] | None = None,
                    recent_task: dict[str, Any] | None = None) -> dict[str, Any]:
        latest: dict[str, Any] = {}
        last_success: dict[str, Any] = {}
        if self.paths.database.is_file():
            from ..storage import JobRepository
            repo = JobRepository(self.paths.database)
            latest = repo.latest_scan_run()
            last_success = repo.latest_scan_run(successful_only=True)
        if active_task:
            return {
                "state": "running",
                "active_task": active_task,
                "last_run": _scan_summary(latest),
                "last_success_at": last_success.get("finished_at"),
            }
        if recent_task and str(recent_task.get("finished_at") or "") > str(latest.get("finished_at") or ""):
            latest = recent_task
            if recent_task.get("status") == "success":
                last_success = recent_task
        if not latest:
            return {"state": "never", "active_task": None, "last_run": None, "last_success_at": None}
        status = str(latest.get("status") or "failed")
        return {
            "state": "success" if status == "success" else "failed",
            "active_task": None,
            "last_run": _scan_summary(latest),
            "last_success_at": last_success.get("finished_at"),
        }

    def health(self) -> dict[str, Any]:
        repo = DatabaseBootstrapService(self.paths.database).initialize()
        queries = JobQueryService(repo)
        config = load_config(self.paths.config)
        return {
            "config_exists": self.paths.config.is_file(),
            "database_exists": self.paths.database.is_file(),
            "job_count": queries.stats()["jobs"],
            "configuration_errors": validate_config(
                config,
                require_graduate_years=False,
                require_batches=True,
            ),
        }


def _list_values(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _valid_apply_url(value: str) -> bool:
    if not value.lower().startswith(("http://", "https://")):
        return False
    normalized = value.rstrip("/").lower()
    return normalized not in {"https://www.wondercv.com", "http://www.wondercv.com", "https://www.wondercv.com/jobs", "http://www.wondercv.com/jobs"}


def _scan_summary(run: dict[str, Any]) -> dict[str, Any] | None:
    if not run:
        return None
    errors = run.get("errors") or []
    failure_stage = run.get("failure_stage")
    if not failure_stage and errors:
        failure_stage = errors[0].get("stage")
    recommended_items = run.get("recommended_items")
    if recommended_items is None:
        recommended_items = run.get("new_recommended_count", run.get("recommended_count"))
    return {
        "task_id": run.get("task_id"),
        "status": run.get("status"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "items_seen": run.get("items_seen", run.get("fetched_count", 0)) or 0,
        "new_items": run.get("new_items", run.get("created_count", 0)) or 0,
        "recommended_items": recommended_items,
        "expiring_items": run.get("expiring_items", 0) or 0,
        "failure_stage": failure_stage,
        "error_message": run.get("error_message") or run.get("error") or "",
    }
