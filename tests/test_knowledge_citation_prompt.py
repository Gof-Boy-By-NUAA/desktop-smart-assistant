from agent.prompt.builder import _build_knowledge_section
from agent.prompt.workspace import _RULE_TEMPLATE_EN, _RULE_TEMPLATE_ZH


def test_runtime_prompt_requires_verbatim_knowledge_citations():
    """运行提示词必须禁止模型手工重建受治理引用。"""

    english = "\n".join(_build_knowledge_section("unused", "en"))
    chinese = "\n".join(_build_knowledge_section("unused", "zh"))

    assert "preserve every returned `knowledge://` citation verbatim" in english
    assert "Never shorten, reorder, or manually reconstruct" in english
    assert "逐字保留返回的完整 `knowledge://` 引用" in chinese
    assert "禁止截断、调整参数顺序或手工重建" in chinese


def test_workspace_rules_keep_the_same_citation_contract():
    """新建工作空间不能用旧规则覆盖运行提示词的引用约束。"""

    assert "preserve each complete `knowledge://` citation" in _RULE_TEMPLATE_EN
    assert "Never shorten, reorder, or reconstruct" in _RULE_TEMPLATE_EN
    assert "逐字保留返回的完整 `knowledge://` 引用" in _RULE_TEMPLATE_ZH
    assert "禁止截断、调整参数顺序或手工重建" in _RULE_TEMPLATE_ZH
