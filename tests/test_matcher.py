from copy import deepcopy

from jobpicky.config import DEFAULT_CONFIG
from jobpicky.matcher import Matcher
from jobpicky.models import Job, Position


def _config() -> dict:
    return {
        "profile": {"version": 3},
        "user_profile": {
            "graduate_years": ["2027届"],
            "batches": ["秋招", "提前批", "实习"],
            "role_groups": ["硬件/嵌入式", "半导体/芯片", "开发/部署"],
            "target_industries": ["新能源", "半导体"],
            "target_cities": ["深圳", "上海"],
            "must_watch_companies": ["必看科技"],
            "exclude_role_groups": ["销售", "市场", "运营"],
            "recall_mode": "balanced",
            "daily_push_limit": 20,
        },
        "system_taxonomy": {
            "role_groups": {
                "硬件/嵌入式": ["硬件", "嵌入式", "单片机", "驱动开发", "FPGA"],
                "半导体/芯片": ["半导体", "芯片", "芯片验证"],
                "开发/部署": ["AI工程师", "AI算法", "AI Infra"],
            },
            "exclude_role_groups": {
                "销售": ["销售", "客户经理"],
                "市场": ["市场", "营销"],
                "运营": ["运营"],
            },
            "generic_role_terms": ["研发类", "技术类", "工程师类", "校招生", "管培生"],
            "important_company_types": ["上市公司", "央企", "国企", "外企"],
            "important_company_marks": ["知名大厂", "研究院"],
            "company_aliases": {"必看科技": ["必看集团"]},
        },
    }


def test_must_watch_company_pushes_generic_notice():
    matcher = Matcher(_config())
    job = Job(company="必看集团", title="必看集团2027届校园招聘正式启动", batch="秋招", target_graduate_year="2027届")

    result = matcher.match(job)

    assert result.should_push is True
    assert result.is_relevant is True
    assert result.recommend_reason == "命中必看公司"
    assert result.priority == "push"


def test_role_input_alias_expands_to_bundled_ai_taxonomy():
    config = _config()
    config["user_profile"]["role_groups"] = ["推理部署"]
    config["system_taxonomy"]["role_groups"]["AI/大模型/推理部署"] = ["大模型", "推理优化", "TensorRT"]
    config["system_taxonomy"]["role_input_aliases"] = {"推理部署": "AI/大模型/推理部署"}

    result = Matcher(config).match(
        Job(company="普通公司", title="2027届大模型推理优化工程师", batch="秋招", target_graduate_year="2027届")
    )

    assert result.should_push is True
    assert result.matched_keywords == ["大模型", "推理优化"]


def test_company_group_expands_to_bundled_known_companies():
    config = _config()
    config["user_profile"]["must_watch_companies"] = ["互联网大厂"]
    config["system_taxonomy"]["company_groups"] = {"互联网大厂": ["字节跳动"]}
    config["system_taxonomy"]["company_aliases"] = {"字节跳动": ["字节"]}

    result = Matcher(config).match(
        Job(company="北京字节跳动科技有限公司", title="2027届校园招聘", batch="秋招", target_graduate_year="2027届")
    )

    assert result.should_push is True
    assert result.matched_company_rule == "字节跳动"


def test_research_institute_is_not_recalled_when_group_is_not_selected():
    config = deepcopy(DEFAULT_CONFIG)
    config["user_profile"].update(
        batches=["校招"], role_groups=["hardware.embedded"], selected_company_groups=[]
    )

    result = Matcher(config).match(
        Job(company="华福证券研究所", title="2027届宏观研究员", batch="校招")
    )

    assert result.should_push is False


def test_selected_research_group_recalls_institute_with_explicit_reason():
    config = deepcopy(DEFAULT_CONFIG)
    config["user_profile"].update(
        batches=["校招"], role_groups=["hardware.embedded"], selected_company_groups=["org.research_institute"]
    )

    result = Matcher(config).match(
        Job(company="某某研究所", title="2027届综合岗位", batch="校招")
    )

    assert result.should_push is True
    assert result.recommend_reason == "命中关注单位：研究院/研究所"
    assert result.matched_company_rule == "研究所"


def test_missing_location_is_not_rejected_by_any_selected_city():
    config = deepcopy(DEFAULT_CONFIG)
    config["user_profile"].update(
        batches=["校招"], role_groups=["hardware.embedded"], target_cities=["city:4403"]
    )

    result = Matcher(config).match(
        Job(company="示例科技", title="嵌入式工程师", batch="校招", city=None)
    )

    assert result.should_push is True
    assert result.match_reason.startswith("命中岗位方向")


def test_province_selection_matches_a_city_inside_that_province():
    config = deepcopy(DEFAULT_CONFIG)
    config["user_profile"].update(
        batches=["校招"], role_groups=["hardware.embedded"], target_cities=["province:44"]
    )

    result = Matcher(config).match(
        Job(company="示例科技", title="嵌入式工程师", batch="校招", city="深圳市")
    )

    assert result.should_push is True
    assert result.matched_city_rule == "广东省"


def test_structured_positions_are_filtered_and_ranked_independently():
    config = deepcopy(DEFAULT_CONFIG)
    config["user_profile"].update(
        batches=["校招"],
        role_groups=["hardware.embedded"],
        exclude_role_groups=["sales"],
        target_cities=["city:4403"],
    )
    job = Job(
        company="示例科技",
        title="2027届校园招聘",
        batch="校招",
        role_text="销售经理 嵌入式工程师",
        positions=[
            Position(title="销售经理", city="深圳市", requirements="负责客户拓展"),
            Position(
                title="嵌入式工程师",
                direction_id="hardware.embedded",
                city="深圳市",
                skills=["BSP", "RTOS"],
                source_text="负责 BSP 驱动和 RTOS 开发",
                position_key="embedded-1",
            ),
        ],
    )

    result = Matcher(config).match(job)

    assert result.should_push is True
    assert result.matched_role_group_id == "hardware.embedded"
    assert result.matched_position_title == "嵌入式工程师"
    assert result.matched_position_key == "embedded-1"
    assert {"BSP", "RTOS"} <= set(result.matched_keywords)
    assert result.matched_city_rule == "深圳市"
    assert result.match_evidence["position"] == "嵌入式工程师"
    assert result.decision_trace == ["hard_filters:passed", "recall:role_taxonomy", "rank:best_position"]


def test_selected_company_cannot_bypass_when_every_structured_position_is_excluded():
    config = deepcopy(DEFAULT_CONFIG)
    config["user_profile"].update(
        batches=["校招"],
        role_groups=["hardware.embedded"],
        exclude_role_groups=["sales"],
        custom_companies=["必看科技"],
    )
    job = Job(
        company="必看科技",
        title="2027届校园招聘",
        batch="校招",
        positions=[Position(title="销售经理", requirements="负责商务拓展")],
    )

    result = Matcher(config).match(job)

    assert result.should_push is False
    assert result.match_reason == "命中排除岗位"


def test_unknown_position_location_survives_city_filter_with_pending_evidence():
    config = deepcopy(DEFAULT_CONFIG)
    config["user_profile"].update(
        batches=["校招"], role_groups=["hardware.embedded"], target_cities=["city:4403"]
    )
    job = Job(
        company="示例科技",
        title="2027届校园招聘",
        batch="校招",
        city=None,
        positions=[Position(title="嵌入式工程师", direction_id="hardware.embedded", city=None)],
    )

    result = Matcher(config).match(job)

    assert result.should_push is True
    assert result.matched_city_rule == ""
    assert result.matched_position_title == "嵌入式工程师"


def test_weak_association_term_cannot_trigger_a_direction_by_itself():
    config = deepcopy(DEFAULT_CONFIG)
    config["user_profile"].update(batches=["校招"], role_groups=["hardware.embedded"])

    weak_only = Matcher(config).match(
        Job(company="示例软件", title="C++后端开发工程师", batch="校招", role_text="负责服务端 C++ 开发")
    )
    strong_with_weak = Matcher(config).match(
        Job(company="示例硬件", title="嵌入式工程师", batch="校招", role_text="负责嵌入式 C++ 开发")
    )

    assert weak_only.should_push is False
    assert strong_with_weak.should_push is True
    assert "嵌入式" in strong_with_weak.matched_strong_keywords
    assert "C++" in strong_with_weak.matched_weak_keywords


def test_must_watch_company_cannot_bypass_an_explicit_excluded_role():
    matcher = Matcher(_config())
    job = Job(
        company="必看集团",
        title="2027届销售客户经理",
        batch="秋招",
        target_graduate_year="2027届",
    )

    result = matcher.match(job)

    assert result.should_push is False
    assert result.recommend_reason == ""


def test_role_group_hit_pushes_and_names_group():
    matcher = Matcher(_config())
    job = Job(company="普通公司", title="2027届FPGA工程师", batch="秋招", target_graduate_year="2027届", city="深圳")

    result = matcher.match(job)

    assert result.should_push is True
    assert result.recommend_reason == "命中岗位方向：硬件/嵌入式"
    assert result.matched_keywords == ["FPGA"]


def test_target_industry_plus_generic_role_pushes():
    matcher = Matcher(_config())
    job = Job(
        company="新能源公司",
        title="2027届研发类校招生",
        batch="秋招",
        target_graduate_year="2027届",
        industry="新能源",
    )

    result = matcher.match(job)

    assert result.should_push is True
    assert result.recommend_reason == "目标行业下的研发/技术类岗位"


def test_important_company_fallback_does_not_create_a_pending_verification_recommendation():
    matcher = Matcher(_config())
    job = Job(
        company="重点公司",
        title="2027届校园招聘",
        batch="秋招",
        target_graduate_year="2027届",
        company_type="上市公司",
    )

    result = matcher.match(job)

    assert result.should_push is False
    assert result.needs_verify is False
    assert result.recommend_reason == ""


def test_city_only_does_not_push():
    matcher = Matcher(_config())
    job = Job(company="普通公司", title="2027届校园招聘", batch="秋招", target_graduate_year="2027届", city="深圳")

    result = matcher.match(job)

    assert result.should_push is False
    assert result.matched_city_rule == "深圳市"


def test_industry_only_does_not_push():
    matcher = Matcher(_config())
    job = Job(company="普通公司", title="2027届校园招聘", batch="秋招", target_graduate_year="2027届", industry="新能源")

    result = matcher.match(job)

    assert result.should_push is False


def test_graduate_year_mismatch_is_ignored_for_recommendations():
    matcher = Matcher(_config())
    job = Job(company="必看科技", title="2026届校园招聘", batch="秋招", target_graduate_year="2026届")

    result = matcher.match(job)

    assert result.should_push is True
    assert result.match_reason == "命中必看公司"


def test_campus_profile_excludes_spring_and_experienced_hires():
    config = _config()
    config["user_profile"]["batches"] = ["校招", "实习"]
    matcher = Matcher(config)

    assert matcher.match(Job(company="必看科技", title="春季校园招聘", batch="春招")).should_push is True
    assert matcher.match(Job(company="必看科技", title="资深工程师招聘", batch="社招")).should_push is False


def test_ambiguous_algorithm_words_do_not_recommend_non_algorithm_jobs():
    config = deepcopy(DEFAULT_CONFIG)
    config["user_profile"].update(
        batches=["校招"],
        role_groups=["算法"],
        target_cities=[],
        custom_keywords=[],
        must_watch_companies=[],
        exclude_role_groups=[],
    )
    matcher = Matcher(config)

    jobs = [
        Job(title="研究助理", batch="校招", role_text="协助整理研究所观点及推荐标的。", extraction_version="v1"),
        Job(title="集团工作人员", batch="校招", role_text="面试名单按笔试成绩排序。", extraction_version="v1"),
        Job(title="招聘实习生", batch="校招", role_text="负责候选人搜索和背景调查。", extraction_version="v1"),
        Job(title="电气培训生", batch="校招", role_text="培养为复合型研发工程师。", extraction_version="v1"),
    ]

    assert all(matcher.match(job).should_push is False for job in jobs)


def test_qualified_algorithm_phrases_still_recommend():
    config = deepcopy(DEFAULT_CONFIG)
    config["user_profile"].update(
        batches=["校招"],
        role_groups=["算法"],
        target_cities=[],
        custom_keywords=[],
        must_watch_companies=[],
        exclude_role_groups=[],
    )

    result = Matcher(config).match(Job(title="推荐系统算法工程师", batch="校招"))

    assert result.should_push is True
    assert {"算法", "算法工程师", "推荐系统"}.issubset(result.matched_keywords)


def test_robot_industry_mentions_do_not_recommend_non_robot_roles():
    config = deepcopy(DEFAULT_CONFIG)
    config["user_profile"].update(
        batches=["校招"],
        role_groups=["具身智能/机器人"],
        target_cities=[],
        custom_keywords=[],
        must_watch_companies=[],
        exclude_role_groups=[],
    )
    matcher = Matcher(config)

    industry_report = Job(
        title="证券研究助理",
        batch="校招",
        role_text="研究范围包括汽车、机器人、军工和新能源行业。",
        extraction_version="v1",
    )
    robot_role = Job(title="机器人算法工程师", batch="校招")

    assert matcher.match(industry_report).should_push is False
    assert matcher.match(robot_role).should_push is True


def test_batch_mismatch_does_not_push():
    matcher = Matcher(_config())
    job = Job(company="必看科技", title="2027届社招", batch="社招", target_graduate_year="2027届")

    result = matcher.match(job)

    assert result.should_push is False
    assert result.match_reason == "批次不匹配"


def test_campus_and_internship_preferences_filter_announcements_and_positions():
    config = deepcopy(DEFAULT_CONFIG)
    config["user_profile"].update(
        batches=["校招"], role_groups=["算法"], target_cities=[],
        custom_keywords=[], must_watch_companies=[], selected_company_groups=[],
        exclude_role_groups=[],
    )
    campus = Matcher(config)
    internship = Job(title="算法工程师实习生招聘", batch="秋招", role_text="校园招聘项目", positions=[
        Position(title="算法工程师实习生", employment_type="实习", skills=["算法"]),
    ])

    assert campus.match(internship).should_push is False
    assert campus.match(Job(title="校园招聘算法实习生", batch="校招")).match_reason != "批次不匹配"
    assert campus.match(Job(company="上海秋晟资产管理有限公司", title="日常实习招聘", clean_title="投资研究岗", batch="校招")).should_push is False
    assert campus.match(Job(title="暑期实习项目", clean_title="算法工程师", batch="校招")).should_push is False
    assert campus.match(Job(company="上海秋晟资产管理有限公司", title="投资研究岗", batch="校招", summary="暑期实习、日常实习岗位招聘")).should_push is False
    leaked = Job(company="上海秋晟资产管理有限公司", title="中科国晟2027届暑期实习计划启动", batch="校招", summary="面向海内外2027届硕士及以上应届生，实习周期4-5周，优秀者可直通校招录用。", positions=[
        Position(title="燃气轮机算法研发课题", skills=["算法"]),
    ])
    assert campus.match(leaked).match_reason == "批次不匹配"

    ambiguous = Job(title="2027届校园招聘及暑期实习计划", batch="校招", summary="同时开放正式校招岗位和实习岗位。")
    assert campus._batch_matches(ambiguous) is True
    assert campus._batch_matches(Job(title="校园招聘算法实习生", batch="校招")) is True

    mixed = Job(title="2027届校园招聘", batch="秋招", positions=[
        Position(title="算法实习生", employment_type="实习", skills=["算法"]),
        Position(title="算法工程师", employment_type="校园招聘", skills=["算法"]),
    ])
    campus_result = campus.match(mixed)
    assert campus_result.should_push is True
    assert campus_result.matched_position_title == "算法工程师"

    config["user_profile"]["batches"] = ["校招", "实习"]
    both_result = Matcher(config).match(internship)
    assert both_result.should_push is True
    assert both_result.matched_position_title == "算法工程师实习生"
    assert Matcher(config)._batch_matches(leaked) is True


def test_negative_only_does_not_push():
    matcher = Matcher(_config())
    job = Job(company="普通公司", title="2027届销售管培生", batch="秋招", target_graduate_year="2027届")

    result = matcher.match(job)

    assert result.should_push is False
    assert result.negative_keywords == ["销售"]
    assert result.match_reason == "命中排除岗位"


def test_negative_words_block_positive_role_group_signal():
    matcher = Matcher(_config())
    job = Job(company="普通公司", title="2027届研发、销售、FPGA岗位", batch="秋招", target_graduate_year="2027届")

    result = matcher.match(job)

    assert result.should_push is False
    assert result.negative_keywords == ["销售"]
    assert result.match_reason == "命中排除岗位"


def test_negative_words_in_raw_text_do_not_block_positive_role_group_signal():
    matcher = Matcher(_config())
    job = Job(
        company="普通公司",
        title="2027届FPGA工程师",
        batch="秋招",
        target_graduate_year="2027届",
        raw_text="本公告还开放销售管培生、市场营销实习生。",
    )

    result = matcher.match(job)

    assert result.should_push is True
    assert result.negative_keywords == ["销售", "市场", "营销"]
    assert result.recommend_reason == "命中岗位方向：硬件/嵌入式"


def test_must_watch_mixed_notice_with_an_explicit_excluded_role_is_not_recommended():
    matcher = Matcher(_config())
    job = Job(
        company="必看集团",
        title="必看集团2027届AI算法和营销岗位同步开放",
        batch="秋招",
        target_graduate_year="2027届",
        raw_text="岗位包括AI算法工程师、市场营销、运营。",
    )

    result = matcher.match(job)

    assert result.should_push is False
    assert result.needs_verify is False
    assert result.negative_keywords == ["营销"]
    assert result.recommend_reason == ""


def test_marketing_keyword_is_excluded_by_market_group():
    matcher = Matcher(_config())
    job = Job(company="普通公司", title="2027届在线营销FPGA实习生", batch="实习", target_graduate_year="2027届")

    result = matcher.match(job)

    assert result.should_push is False
    assert result.negative_keywords == ["营销"]
    assert result.match_reason == "命中排除岗位"


def test_marketing_job_tag_blocks_positive_raw_text():
    matcher = Matcher(_config())
    job = Job(
        company="普通公司",
        title="2027届实习生",
        batch="实习",
        target_graduate_year="2027届",
        job_tags=["在线营销"],
        raw_text="技术团队还招聘FPGA工程师。",
    )

    result = matcher.match(job)

    assert result.should_push is False
    assert result.negative_keywords == ["营销"]
    assert result.match_reason == "命中排除岗位"


def test_ai_requires_qualified_role_term():
    matcher = Matcher(_config())
    phone_interview = Job(company="普通公司", title="2027届实习生", batch="实习", target_graduate_year="2027届", raw_text="投递后会有AI电话面试。")
    ai_role = Job(company="普通公司", title="2027届AI算法工程师", batch="实习", target_graduate_year="2027届")

    assert matcher.match(phone_interview).should_push is False
    assert matcher.match(ai_role).matched_keywords == ["AI算法"]


def test_negative_words_block_generic_industry_fallback_without_role_group_signal():
    matcher = Matcher(
        {
            "user_profile": {
                "graduate_years": ["2026"],
                "batches": ["fall"],
                "role_groups": ["algorithm"],
                "target_industries": ["internet"],
                "target_cities": [],
                "must_watch_companies": [],
                "exclude_role_groups": ["sales"],
            },
            "system_taxonomy": {
                "role_groups": {"algorithm": ["algorithm"]},
                "exclude_role_groups": {"sales": ["sales"]},
                "generic_role_terms": ["trainee"],
                "important_company_types": [],
                "important_company_marks": [],
                "company_aliases": {},
            },
        }
    )
    job = Job(company="OtherCo", title="2026 fall sales trainee", batch="fall", target_graduate_year="2026", industry="internet")

    result = matcher.match(job)

    assert result.should_push is False
    assert result.match_reason == "命中排除岗位"


def test_configured_city_filters_clear_mismatch_but_not_missing_city():
    matcher = Matcher(_config())
    mismatch = Job(company="必看科技", title="2027届校园招聘", batch="秋招", target_graduate_year="2027届", city="广州")
    missing = Job(company="必看科技", title="2027届校园招聘", batch="秋招", target_graduate_year="2027届")

    mismatch_result = matcher.match(mismatch)
    missing_result = matcher.match(missing)

    assert mismatch_result.should_push is False
    assert mismatch_result.match_reason == "城市不匹配"
    assert missing_result.should_push is True


def test_detail_keywords_in_raw_text_trigger_role_group_match():
    matcher = Matcher(
        {
            "profile": {"version": 4},
            "user_profile": {
                "graduate_years": ["2027届"],
                "batches": ["秋招"],
                "role_groups": ["硬件/嵌入式"],
                "target_industries": [],
                "target_cities": [],
                "must_watch_companies": [],
                "exclude_role_groups": ["销售"],
            },
            "system_taxonomy": {
                "role_groups": {"硬件/嵌入式": ["嵌入式", "GNSS", "测试开发"]},
                "exclude_role_groups": {"销售": ["销售"]},
                "generic_role_terms": [],
                "important_company_types": [],
                "important_company_marks": [],
                "company_aliases": {},
            },
        }
    )
    job = Job(
        company="深圳市大疆创新科技有限公司",
        title="大疆27秋招",
        batch="秋招",
        target_graduate_year="2027届",
        raw_text="招聘岗位：嵌入式工程师、GNSS定位算法工程师、测试开发工程师。",
    )

    result = matcher.match(job)

    assert result.should_push is True
    assert result.matched_keywords == ["嵌入式", "GNSS", "测试开发"]


def test_short_ascii_keyword_requires_token_boundary():
    matcher = Matcher(
        {
            "user_profile": {
                "graduate_years": ["2027届"],
                "batches": ["秋招"],
                "role_groups": ["半导体/芯片"],
                "target_industries": [],
                "target_cities": [],
                "must_watch_companies": [],
                "exclude_role_groups": [],
            },
            "system_taxonomy": {
                "role_groups": {"半导体/芯片": ["IC"]},
                "exclude_role_groups": {},
                "generic_role_terms": [],
                "important_company_types": [],
                "important_company_marks": [],
                "company_aliases": {},
            },
        }
    )
    false_positive = Job(
        company="普通公司",
        title="2027届秋招",
        batch="秋招",
        target_graduate_year="2027届",
        raw_text="public service policy practice logistics",
    )
    real_hit = Job(
        company="芯片公司",
        title="2027届秋招",
        batch="秋招",
        target_graduate_year="2027届",
        raw_text="招聘岗位：IC验证工程师。",
    )

    assert matcher.match(false_positive).should_push is False
    assert matcher.match(real_hit).matched_keywords == ["IC"]


def test_role_match_mode_all_requires_every_selected_direction():
    config = _config()
    config["user_profile"].update(batches=[], target_cities=[], role_groups=["hardware", "chip"], role_match_mode="all")
    config["system_taxonomy"]["role_groups"] = {"hardware": ["hardware"], "chip": ["chip"]}

    result = Matcher(config).match(Job(company="Example", title="hardware chip engineer"))

    assert result.should_push is True
    assert result.matched_role_group_id == "hardware,chip"
