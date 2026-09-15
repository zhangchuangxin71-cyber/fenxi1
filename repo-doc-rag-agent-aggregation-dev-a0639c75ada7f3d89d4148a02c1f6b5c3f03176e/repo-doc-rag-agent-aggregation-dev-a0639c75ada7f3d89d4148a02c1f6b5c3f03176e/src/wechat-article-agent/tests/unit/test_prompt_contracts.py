from app.prompts import system


def test_all_system_prompts_are_partitioned_and_auditable() -> None:
    prompts = {
        name: value
        for name, value in vars(system).items()
        if name.endswith("_SYSTEM_PROMPT") and isinstance(value, str)
    }
    assert prompts
    for name, prompt in prompts.items():
        assert "# 角色与任务" in prompt, name
        assert "# 输出契约" in prompt, name
        assert "# 禁止事项" in prompt, name


def test_user_facing_output_prompts_require_professional_tone() -> None:
    for name in (
        "ORCHESTRATOR_SYSTEM_PROMPT",
        "PENDING_PREFLIGHT_SYSTEM_PROMPT",
        "RESEARCH_DIRECTION_SYSTEM_PROMPT",
        "TASK_SPEC_SYSTEM_PROMPT",
        "OUTLINE_SYSTEM_PROMPT",
        "ARTICLE_SYSTEM_PROMPT",
        "IMAGE_PLAN_SYSTEM_PROMPT",
    ):
        assert "简洁、专业" in getattr(system, name), name


def test_control_prompts_include_concrete_examples() -> None:
    for name in (
        "INTENT_ROUTER_SYSTEM_PROMPT",
        "ORCHESTRATOR_SYSTEM_PROMPT",
        "PENDING_PREFLIGHT_SYSTEM_PROMPT",
        "RESEARCH_DIRECTION_VALIDATION_SYSTEM_PROMPT",
    ):
        assert "# 示例" in getattr(system, name), name


def test_intent_router_preserves_unknown_entities_and_only_classifies_product_intent() -> None:
    prompt = system.INTENT_ROUTER_SYSTEM_PROMPT
    assert "判断对象是用户要不要生成微信公众号文章" in prompt
    assert "生成一篇关于牛来的微信公众号文章" in prompt
    assert "牛市来了" in prompt
    assert "不要改写" in prompt


def test_all_new_material_research_prompts_include_examples() -> None:
    for name in (
        "MATERIAL_SUFFICIENCY_SYSTEM_PROMPT",
        "WEB_INFO_OVERVIEW_SYSTEM_PROMPT",
        "MATERIAL_FILTER_SYSTEM_PROMPT",
        "WEB_MATERIAL_SYSTEM_PROMPT",
        "MATERIAL_COMPACTION_SYSTEM_PROMPT",
        "CONFLICT_CHECK_SYSTEM_PROMPT",
        "CONFLICT_HANDLER_SYSTEM_PROMPT",
    ):
        assert "# 示例" in getattr(system, name), name


def test_generation_prompts_put_generation_basis_at_high_priority() -> None:
    for name in (
        "TASK_SPEC_SYSTEM_PROMPT",
        "OUTLINE_SYSTEM_PROMPT",
        "ARTICLE_SYSTEM_PROMPT",
    ):
        prompt = getattr(system, name)
        assert "# 最高优先级：生成依据" in prompt, name
        assert "generation_basis=reference_materials" in prompt, name
        assert "不得用模型知识" in prompt, name
        assert "generation_basis=general_knowledge" in prompt, name


def test_writing_prompts_reject_draft_markers_and_emoji_by_default() -> None:
    assert "写作要点" in system.OUTLINE_SYSTEM_PROMPT
    assert "默认不使用 emoji" in system.OUTLINE_SYSTEM_PROMPT
    assert "正式发布稿" in system.ARTICLE_SYSTEM_PROMPT
    assert "默认不使用 emoji" in system.ARTICLE_SYSTEM_PROMPT


def test_image_prompt_requires_context_facts_and_avoids_fake_diagrams() -> None:
    prompt = system.IMAGE_PLAN_SYSTEM_PROMPT
    assert "context_text" in prompt
    assert "数字、比例、金额、日期、条款或流程顺序" in prompt
    assert "画面不出现文字、数字、图表、流程节点或界面标签" in prompt
