from __future__ import annotations

from datetime import datetime
import re

from .models import Job, MatchResult, Position
from .normalizer import normalize_company
from .locations import match_target_location


class Matcher:
    def __init__(self, config: dict):
        self.config = config
        self.profile = config.get("user_profile", {})
        self.taxonomy = config.get("system_taxonomy", {})
        self.company_aliases = self.taxonomy.get("company_aliases", {})
        self.role_labels = self.taxonomy.get("role_labels", {})
        self.role_weak_groups = self.taxonomy.get("role_weak_groups", {})
        self.organization_groups = self.taxonomy.get("organization_groups", {})
        self.role_groups = self._expand_role_groups()
        self.must_watch_companies = self._expand_must_watch_companies()
        self.custom_keywords = self._clean_terms(self.profile.get("custom_keywords", []))

    def match(self, job: Job) -> MatchResult:
        text = self._job_text(job)
        context_negative_hits = self._negative_hits(text)
        if not self._batch_matches(job):
            return self._result(False, "批次不匹配", decision_trace=["hard_filter:batch_mismatch"])

        candidates, rejected_negative, rejected_city = self._eligible_candidates(job)
        if not candidates:
            if rejected_negative:
                return self._result(
                    False,
                    "命中排除岗位",
                    negative_hits=rejected_negative,
                    decision_trace=["hard_filter:all_positions_excluded"],
                )
            if rejected_city:
                return self._result(False, "城市不匹配", decision_trace=["hard_filter:all_positions_city_mismatch"])
            return self._result(False, "", decision_trace=["no_eligible_position"])

        candidate = candidates[0]
        city_hit = candidate["city_hit"]

        company_hit = self._must_watch_company_hit(job)
        if company_hit:
            return self._result(
                True,
                "命中必看公司",
                score=100,
                matched_company=company_hit,
                negative_hits=context_negative_hits,
                city_hit=city_hit,
                position=candidate["position"],
                evidence={"company_rule": company_hit, "position": candidate["title"]},
                decision_trace=["hard_filters:passed", "recall:custom_company"],
            )

        organization_hit = self._organization_group_hit(job)
        if organization_hit:
            group_label, evidence = organization_hit
            return self._result(
                True,
                f"命中关注单位：{group_label}",
                score=95,
                matched_company=evidence,
                negative_hits=context_negative_hits,
                city_hit=city_hit,
                position=candidate["position"],
                evidence={"organization_group": group_label, "company_evidence": evidence},
                decision_trace=["hard_filters:passed", "recall:organization_group"],
            )

        role_group, strong_hits, weak_hits, candidate = self._best_role_candidate(candidates)
        if role_group:
            role_label = self.role_labels.get(role_group, role_group)
            position_title = candidate["title"]
            suffix = f"（{position_title}）" if position_title else ""
            role_trace = ["hard_filters:passed", "recall:role_taxonomy"]
            if candidate["position"]:
                role_trace.append("rank:best_position")
            return self._result(
                True,
                f"命中岗位方向：{role_label}{suffix}",
                score=candidate["score"],
                matched_keywords=[*strong_hits, *weak_hits],
                matched_strong_keywords=strong_hits,
                matched_weak_keywords=weak_hits,
                negative_hits=context_negative_hits,
                city_hit=candidate["city_hit"],
                role_group_id=role_group,
                position=candidate["position"],
                evidence={
                    "role_group_id": role_group,
                    "role_label": role_label,
                    "position": position_title,
                    "strong_keywords": strong_hits,
                    "weak_keywords": weak_hits,
                    "source_excerpt": candidate["text"][:240],
                },
                decision_trace=role_trace,
            )

        custom_candidate, custom_hits = self._best_keyword_candidate(candidates, self.custom_keywords)
        if custom_candidate:
            custom_suffix = f"（{custom_candidate['title']}）" if custom_candidate["title"] else ""
            return self._result(
                True,
                f"命中自定义关键词{custom_suffix}",
                score=85,
                matched_keywords=custom_hits,
                negative_hits=context_negative_hits,
                city_hit=custom_candidate["city_hit"],
                position=custom_candidate["position"],
                evidence={"keywords": custom_hits, "position": custom_candidate["title"]},
                decision_trace=["hard_filters:passed", "recall:custom_keywords"],
            )

        industry_hit = self._target_industry_hit(job, text)
        generic_candidate, generic_hits = self._best_keyword_candidate(
            candidates, self.taxonomy.get("generic_role_terms", [])
        )
        if industry_hit and generic_candidate:
            return self._result(
                True,
                "目标行业下的研发/技术类岗位",
                score=70,
                matched_keywords=generic_hits,
                negative_hits=context_negative_hits,
                city_hit=generic_candidate["city_hit"],
                position=generic_candidate["position"],
                evidence={"industry": industry_hit, "keywords": generic_hits},
                decision_trace=["hard_filters:passed", "recall:industry_generic_role"],
            )

        return self._result(
            False,
            "",
            negative_hits=context_negative_hits,
            city_hit=candidate["city_hit"],
            decision_trace=["hard_filters:passed", "recall:no_rule_hit"],
        )

    def _result(
        self,
        should_push: bool,
        reason: str,
        *,
        score: int = 0,
        matched_keywords: list[str] | None = None,
        matched_strong_keywords: list[str] | None = None,
        matched_weak_keywords: list[str] | None = None,
        matched_company: str = "",
        negative_hits: list[str] | None = None,
        city_hit: str | None = None,
        needs_verify: bool = False,
        verify_status: str = "未核验",
        role_group_id: str = "",
        position: Position | None = None,
        evidence: dict | None = None,
        decision_trace: list[str] | None = None,
    ) -> MatchResult:
        return MatchResult(
            matched_keywords=matched_keywords or [],
            matched_strong_keywords=matched_strong_keywords if matched_strong_keywords is not None else (matched_keywords or []),
            matched_weak_keywords=matched_weak_keywords or [],
            matched_industry_keywords=[],
            matched_company_rule=matched_company,
            matched_city_rule=city_hit or "",
            negative_keywords=negative_hits or [],
            match_score=score,
            priority="push" if should_push else "skip",
            is_relevant=should_push,
            should_push=should_push,
            needs_verify=needs_verify,
            match_reason=reason,
            verify_status=verify_status,
            suggested_search_terms=[],
            match_config_version=str(self.config.get("profile", {}).get("version", "")),
            matched_at=datetime.now().isoformat(timespec="seconds"),
            recommend_reason=reason if should_push else "",
            matched_role_group_id=role_group_id,
            matched_position_title=position.title if position else "",
            matched_position_key=position.position_key if position else "",
            match_evidence=evidence or {},
            decision_trace=decision_trace or [],
        )

    def _batch_matches(self, job: Job) -> bool:
        expected = self.profile.get("batches", [])
        if not expected:
            return True
        content_text = " ".join(filter(None, (
            job.title, job.clean_title, job.raw_title, job.summary,
            job.announcement_text, *job.tags, *job.job_tags, *job.raw_tags,
        )))
        batch_text = f"{job.batch or ''} {content_text}"
        if self._match_many(batch_text, ["社招", "社会招聘", "experienced hire"]):
            return False
        selected_kinds = self._selected_batch_kinds(expected)
        strong_internship = self._match_many(content_text, [
            "日常实习", "暑期实习", "寒假实习", "实习计划", "实习招聘", "实习生",
        ])
        campus_offer_text = re.sub(r"(?:直通|转入|进入|可转)校招(?:录用)?", "", content_text)
        has_campus_offer = "campus" in self._batch_kinds(campus_offer_text)
        if strong_internship and not has_campus_offer and "internship" not in selected_kinds:
            return False
        kinds = self._batch_kinds(batch_text)
        return not kinds or bool(kinds & selected_kinds)

    @staticmethod
    def _batch_kinds(text: str) -> set[str]:
        lowered = (text or "").casefold()
        kinds: set[str] = set()
        if any(word in lowered for word in ("实习", "intern")):
            kinds.add("internship")
        if any(word in lowered for word in ("校招", "校园招聘", "秋招", "春招", "提前批", "补录", "fall")):
            kinds.add("campus")
        return kinds

    @staticmethod
    def _selected_batch_kinds(values: list[str]) -> set[str]:
        selected: set[str] = set()
        for value in values:
            selected.update(Matcher._batch_kinds(str(value)))
        return selected

    def _eligible_candidates(self, job: Job) -> tuple[list[dict], list[str], bool]:
        target_cities = self.profile.get("target_cities", [])
        source_positions: list[Position | None] = list(job.positions) if job.positions else [None]
        eligible: list[dict] = []
        rejected_negative: list[str] = []
        rejected_city = False
        for position in source_positions:
            if position:
                position_kinds = self._batch_kinds(position.employment_type or "") or self._batch_kinds(position.title)
                if position_kinds and not position_kinds & self._selected_batch_kinds(self.profile.get("batches", [])):
                    continue
            text = self._position_text(position) if position else self._job_text(job)
            negative_text = text if position else self._job_role_text(job)
            title = position.title if position else ""
            location = (position.city if position else None) or job.city
            city_hit = match_target_location(location, target_cities)
            if target_cities and location and not city_hit:
                rejected_city = True
                continue
            negative_hits = self._negative_hits(negative_text)
            if negative_hits:
                rejected_negative.extend(negative_hits)
                continue
            eligible.append(
                {
                    "position": position,
                    "title": title,
                    "text": text,
                    "city_hit": city_hit,
                    "direction_id": position.direction_id if position else None,
                    "score": 0,
                }
            )
        return eligible, list(dict.fromkeys(rejected_negative)), rejected_city

    def _best_role_candidate(self, candidates: list[dict]) -> tuple[str, list[str], list[str], dict]:
        best: tuple[int, str, list[str], list[str], dict] | None = None
        for candidate in candidates:
            matched_groups: list[tuple[str, list[str], list[str], int]] = []
            for group, keywords in self.role_groups:
                title_hits = self._match_many(candidate["title"], keywords)
                hits = list(dict.fromkeys([*title_hits, *self._match_many(candidate["text"], keywords)]))
                direction_hit = candidate["direction_id"] == group
                if not hits and not direction_hit:
                    continue
                weak_hits = self._match_many(candidate["text"], self.role_weak_groups.get(group, []))
                score = 90 + min(8, len(title_hits) * 2 + len(hits) + len(weak_hits) + (3 if direction_hit else 0))
                if not hits and candidate["title"]:
                    hits = [candidate["title"]]
                matched_groups.append((group, hits, weak_hits, score))
            if self.profile.get("role_match_mode", "any") == "all":
                if len(matched_groups) < len(self.role_groups):
                    continue
                role_group = ",".join(group for group, *_ in matched_groups)
                strong_hits = list(dict.fromkeys(hit for _, hits, _, _ in matched_groups for hit in hits))
                weak_hits = list(dict.fromkeys(hit for _, _, hits, _ in matched_groups for hit in hits))
                score = min(100, sum(item[3] for item in matched_groups) // len(matched_groups) + 5)
                ranked = dict(candidate, score=score)
                item = (score, role_group, strong_hits, weak_hits, ranked)
                if best is None or item[0] > best[0]:
                    best = item
                continue
            for group, hits, weak_hits, score in matched_groups:
                ranked = dict(candidate, score=score)
                item = (score, group, hits, weak_hits, ranked)
                if best is None or item[0] > best[0]:
                    best = item
        return (best[1], best[2], best[3], best[4]) if best else ("", [], [], candidates[0])

    def _best_keyword_candidate(self, candidates: list[dict], keywords: list[str]) -> tuple[dict | None, list[str]]:
        best_candidate: dict | None = None
        best_hits: list[str] = []
        for candidate in candidates:
            hits = self._match_many(candidate["text"], keywords)
            if len(hits) > len(best_hits):
                best_candidate, best_hits = candidate, hits
        return best_candidate, best_hits

    @staticmethod
    def _position_text(position: Position) -> str:
        return " ".join(
            part
            for part in (
                position.title,
                position.department or "",
                position.responsibilities or "",
                position.requirements or "",
                " ".join(position.skills),
                " ".join(position.majors),
                position.source_text or "",
            )
            if part
        )

    def _city_is_clear_mismatch(self, job: Job, city_hit: str | None) -> bool:
        target_cities = self.profile.get("target_cities", [])
        return bool(target_cities and job.city and not city_hit)

    def _must_watch_company_hit(self, job: Job) -> str:
        company = normalize_company(job.company_normalized or job.company, self.company_aliases)
        return self._match_one(company, self.must_watch_companies) or ""

    def _organization_group_hit(self, job: Job) -> tuple[str, str] | None:
        company = normalize_company(job.company_normalized or job.company, self.company_aliases)
        company_type = job.company_type or ""
        for group_id in self.profile.get("selected_company_groups", []):
            group = self.organization_groups.get(str(group_id), {})
            if not group:
                continue
            member_names = group.get("member_names", [])
            member_hit = self._match_one(company, member_names)
            type_hit = self._match_one(company_type, group.get("company_types", []))
            pattern_hit = self._match_one(company, group.get("name_patterns", []))
            evidence = member_hit or type_hit or pattern_hit
            if evidence:
                return str(group.get("label", group_id)), evidence
        return None

    def _role_group_hit(self, text: str) -> tuple[str, list[str]]:
        for group, keywords in self.role_groups:
            hits = self._match_many(text, keywords)
            if hits:
                return group, hits
        return "", []

    def _expand_role_groups(self) -> list[tuple[str, list[str]]]:
        groups = self.taxonomy.get("role_groups", {})
        aliases = self.taxonomy.get("role_input_aliases", {})
        expanded: list[tuple[str, list[str]]] = []
        seen: set[str] = set()
        for value in self.profile.get("role_groups", []):
            requested = str(value).strip()
            if not requested:
                continue
            canonical = aliases.get(requested.lower(), requested)
            if canonical in seen:
                continue
            seen.add(canonical)
            # Unknown user input remains a literal keyword instead of silently
            # becoming an empty taxonomy group.
            keywords = list(groups.get(canonical, [])) or [requested]
            expanded.append((canonical, keywords))
        return expanded

    def _expand_must_watch_companies(self) -> list[str]:
        company_groups = self.taxonomy.get("company_groups", {})
        expanded: list[str] = []
        values = list(self.profile.get("must_watch_companies", [])) + list(self.profile.get("custom_companies", []))
        for value in values:
            name = str(value).strip()
            if not name:
                continue
            expanded.extend(company_groups.get(name, [name]))
        return list(dict.fromkeys(expanded))

    @staticmethod
    def _clean_terms(values) -> list[str]:
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple, set)):
            return []
        return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))

    def _target_industry_hit(self, job: Job, text: str) -> str:
        haystack = " ".join(part for part in [job.industry or "", text] if part)
        return self._match_one(haystack, self.profile.get("target_industries", [])) or ""

    def _negative_hits(self, text: str) -> list[str]:
        groups = self.taxonomy.get("exclude_role_groups", {})
        role_groups = self.taxonomy.get("role_groups", {})
        aliases = self.taxonomy.get("role_input_aliases", {})
        hits: list[str] = []
        for group in self.profile.get("exclude_role_groups", []):
            canonical = aliases.get(str(group).lower(), group)
            hits.extend(self._match_many(text, groups.get(group, []) or role_groups.get(canonical, [])))
        return list(dict.fromkeys(hits))

    @staticmethod
    def _match_many(text: str, words: list[str]) -> list[str]:
        return [word for word in words if _keyword_in_text(text, word)]

    @staticmethod
    def _match_one(text: str, words: list[str]) -> str | None:
        for word in words:
            if _keyword_in_text(text, word):
                return word
        return None

    @staticmethod
    def _contains_any(text: str, words: list[str]) -> bool:
        return any(_keyword_in_text(text, word) for word in words)

    @staticmethod
    def _job_text(job: Job) -> str:
        # Role relevance must come from a role section, never from generic
        # company/announcement prose.  Structured fields remain available for
        # hard filters above.
        role_text = job.role_text or (job.raw_text if not job.extraction_version else "")
        return " ".join(
            part
            for part in [
                job.clean_title or job.title,
                " ".join(job.job_tags),
                " ".join(job.role_signals),
                role_text or "",
            ]
            if part
        )

    @staticmethod
    def _job_role_text(job: Job) -> str:
        return " ".join(
            part
            for part in [
                job.clean_title or job.title,
                " ".join(job.job_tags),
                " ".join(job.role_signals),
                job.role_text or "",
            ]
            if part
        )


def _keyword_in_text(text: str, word: str) -> bool:
    if not text or not word:
        return False
    if _is_short_ascii_keyword(word):
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])"
        if re.search(pattern, text, flags=re.I):
            return True
        # Chinese job titles often attach short ASCII terms directly to CJK words,
        # e.g. IC验证工程师. Accept that while still rejecting public/service.
        cjk_attached = rf"(?<![A-Za-z0-9_]){re.escape(word)}(?=[\u4e00-\u9fff])"
        return bool(re.search(cjk_attached, text, flags=re.I))
    return word.lower() in text.lower()


def _is_short_ascii_keyword(word: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9+#./-]{1,3}", word))
