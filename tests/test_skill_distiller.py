"""Tests for the screen-recording skill distiller (M1).

The distiller turns a validated step IR into a reviewable skill draft.
Everything here is deterministic and model-free; the analyzer is stubbed
by handing in IR dictionaries directly.
"""

import json

import pytest

from agent.skills.distiller import (
    REDACTED,
    DistillError,
    SkillDraft,
    distill,
    slugify,
    validate_ir,
    validate_slug,
    write_draft,
)
from agent.skills.frontmatter import parse_frontmatter, parse_metadata


def make_ir(**overrides):
    ir = {
        "schema_version": 1,
        "recording_id": "rec-001",
        "model_used": "qwen-vl-max",
        "summary": "在 Excel 中整理销售表并保存为 xlsx",
        "suggested_skill_name": "excel-sales-cleanup",
        "steps": [
            {"index": 1, "timestamp_s": 3.5, "action": "click",
             "target_description": "Excel 工具栏保存按钮", "region": [10, 20, 30, 40],
             "text": None, "confidence": 0.9},
            {"index": 2, "timestamp_s": 8.0, "action": "type",
             "target_description": "文件名输入框", "region": None,
             "text": "sales-2026.xlsx", "confidence": 0.86},
            {"index": 3, "timestamp_s": 12.0, "action": "hotkey",
             "target_description": "保存快捷键", "region": None,
             "text": "ctrl+s", "confidence": 0.4},
        ],
        "warnings": ["第 3 步画面模糊"],
    }
    ir.update(overrides)
    return ir


# ---------------------------------------------------------------- schema


def test_validate_ir_accepts_valid():
    assert validate_ir(make_ir())["recording_id"] == "rec-001"


@pytest.mark.parametrize("mutate, needle", [
    (lambda ir: ir.update(schema_version=2), "schema_version"),
    (lambda ir: ir.update(recording_id=""), "recording_id"),
    (lambda ir: ir.update(model_used=""), "model_used"),
    (lambda ir: ir.update(summary="  "), "summary"),
    (lambda ir: ir.update(suggested_skill_name=""), "suggested_skill_name"),
    (lambda ir: ir.update(steps=[]), "steps"),
    (lambda ir: ir.update(warnings="oops"), "warnings"),
    (lambda ir: ir["steps"][0].update(index=True), "index"),
    (lambda ir: ir["steps"][0].update(timestamp_s=-1), "timestamp_s"),
    (lambda ir: ir["steps"][0].update(action="teleport"), "action"),
    (lambda ir: ir["steps"][0].update(target_description=""), "target_description"),
    (lambda ir: ir["steps"][0].update(region=[1, 2, 3]), "region"),
    (lambda ir: ir["steps"][0].update(region=[1, 2, 3, "x"]), "region"),
    (lambda ir: ir["steps"][0].update(text=42), "text"),
    (lambda ir: ir["steps"][0].update(confidence=1.5), "confidence"),
    (lambda ir: ir["steps"][0].update(confidence=True), "confidence"),
])
def test_validate_ir_fail_closed(mutate, needle):
    ir = make_ir()
    mutate(ir)
    with pytest.raises(DistillError) as excinfo:
        validate_ir(ir)
    assert needle in str(excinfo.value)


def test_validate_ir_rejects_non_dict():
    with pytest.raises(DistillError):
        validate_ir([1, 2, 3])


# ---------------------------------------------------------------- naming


def test_validate_slug_rules():
    assert validate_slug("excel-sales-cleanup") == "excel-sales-cleanup"
    for bad in ["", "Excel", "-lead", "trail-", "a--b", "有中文", "with space", "x" * 0]:
        with pytest.raises(DistillError):
            validate_slug(bad)


def test_slugify_best_effort():
    assert slugify("Excel Sales Cleanup!") == "excel-sales-cleanup"
    assert slugify("---") == ""
    assert slugify("整理表格") == ""  # no ascii -> caller must fail closed


def test_distill_uses_suggested_name_and_description_override():
    draft = distill(make_ir(), description="自定义描述")
    assert draft.name == "excel-sales-cleanup"
    assert draft.summary == "自定义描述"


def test_distill_explicit_name_required_when_suggestion_unusable():
    ir = make_ir(suggested_skill_name="整理表格")
    with pytest.raises(DistillError) as excinfo:
        distill(ir)
    assert "显式指定" in str(excinfo.value)
    draft = distill(ir, name="table-cleanup")
    assert draft.name == "table-cleanup"


def test_distill_conflict_fail_closed():
    with pytest.raises(DistillError) as excinfo:
        distill(make_ir(), existing_names=["excel-sales-cleanup"])
    assert "冲突" in str(excinfo.value)


# ---------------------------------------------------------------- redaction


@pytest.mark.parametrize("secret", [
    "110101199003074321",              # 身份证
    "13812345678",                     # 手机号
    "6222021234567890123",             # 银行卡
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    "Bearer abcdef1234567890token",
])
def test_redaction_patterns(secret):
    ir = make_ir()
    ir["steps"][1]["text"] = secret
    draft = distill(ir)
    assert draft.redactions == 1
    assert any("脱敏" in w for w in draft.warnings)
    steps_json = json.loads(draft.files["scripts/steps.json"])
    assert steps_json["steps"][1]["text"] == REDACTED
    assert secret not in draft.files["SKILL.md"]


def test_redaction_password_context_wholesale():
    ir = make_ir()
    ir["steps"][1]["text"] = "hunter2"
    ir["steps"][1]["target_description"] = "密码输入框"
    ir["steps"][1]["text"] = "mypassword: hunter2"
    draft = distill(ir)
    assert draft.redactions == 1
    assert "hunter2" not in draft.files["SKILL.md"]


def test_normal_text_not_redacted_and_input_not_mutated():
    ir = make_ir()
    original_text = ir["steps"][1]["text"]
    draft = distill(ir)
    assert draft.redactions == 0
    assert ir["steps"][1]["text"] == original_text  # no mutation
    assert original_text in draft.files["SKILL.md"]


# ---------------------------------------------------------------- rendering


def test_skill_md_frontmatter_round_trip():
    draft = distill(make_ir())
    fm = parse_frontmatter(draft.files["SKILL.md"])
    assert fm["name"] == "excel-sales-cleanup"
    assert "Excel" in fm["description"]
    metadata = parse_metadata(fm)
    assert metadata is not None
    assert "windows" in metadata.os


def test_skill_md_marks_low_confidence_and_warnings():
    draft = distill(make_ir())
    body = draft.files["SKILL.md"]
    assert "⚠️ 置信度 0.40" in body
    assert "低置信度步骤：#3" in body
    assert "第 3 步画面模糊" in body


def test_frontmatter_survives_nasty_summary():
    nasty = '步骤: 含冒号 "引号" \n换行 [括号] #注释'
    draft = distill(make_ir(summary=nasty))
    fm = parse_frontmatter(draft.files["SKILL.md"])
    assert fm["description"] == "步骤: 含冒号 \"引号\" 换行 [括号] #注释"
    assert "\n" not in draft.summary


def test_steps_json_is_valid_and_sorted():
    draft = distill(make_ir())
    parsed = json.loads(draft.files["scripts/steps.json"])
    assert parsed["schema_version"] == 1
    assert len(parsed["steps"]) == 3
    assert draft.files["scripts/steps.json"].endswith("\n")


def test_scripts_readme_records_provenance():
    draft = distill(make_ir())
    readme = draft.files["scripts/README.md"]
    assert "rec-001" in readme and "qwen-vl-max" in readme
    assert "不含可执行脚本" in readme


# ---------------------------------------------------------------- writing


def test_write_draft_outputs_lf_bytes(tmp_path):
    draft = distill(make_ir())
    skill_dir = write_draft(draft, tmp_path)
    assert skill_dir.name == "excel-sales-cleanup"
    for relpath in draft.files:
        data = (skill_dir / relpath).read_bytes()
        assert b"\r\n" not in data
        assert b"\r" not in data


def test_write_draft_refuses_overwrite(tmp_path):
    draft = distill(make_ir())
    write_draft(draft, tmp_path)
    with pytest.raises(DistillError) as excinfo:
        write_draft(draft, tmp_path)
    assert "拒绝覆盖" in str(excinfo.value)


def test_write_draft_refuses_path_escape(tmp_path):
    draft = SkillDraft(name="ok-name", summary="s",
                       files={"../evil.md": "x"})
    with pytest.raises(DistillError):
        write_draft(draft, tmp_path)


# ---------------------------------------------------------------- property-ish


def test_frontmatter_parseable_for_many_summaries():
    samples = [
        "简单描述", "with: colon", "quote \"inside\"", "多  空格",
        "符号!@#$%^&*()", "末尾换行\n", "数字 12345", "- 以连字符开头",
    ]
    for summary in samples:
        draft = distill(make_ir(summary=summary))
        fm = parse_frontmatter(draft.files["SKILL.md"])
        assert fm["name"] == "excel-sales-cleanup"
        assert isinstance(fm["description"], str) and fm["description"]
