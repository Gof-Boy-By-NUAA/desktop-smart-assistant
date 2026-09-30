"""Skill distiller: turn a screen-recording step IR into a reviewable skill draft.

Design contract (docs/录屏沉淀Skill技术方案-2026-08-20):
- Input is a validated step IR produced by the screen analyzer.
- Output is a *draft* (SKILL.md + scripts/), never an installed skill.
  Installation happens only after human review, through the single
  ``service.add()`` channel.
- Fail-closed on any schema, naming, or conflict problem.
- Sensitive values in step text are redacted here (defense in depth;
  the analyzer redacts as well) and every redaction is recorded in the
  draft warnings.
- All files are written as deterministic LF bytes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

IR_SCHEMA_VERSION = 1

IR_ACTIONS = frozenset({"click", "type", "drag", "hotkey", "wait"})

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Sensitive-value patterns applied to step text before it lands in a draft.
# Ordered; first match wins. Each tuple: (label, compiled pattern, full_text_match).
_ID_CARD_RE = re.compile(r"\b\d{17}[\dXx]\b")
_PHONE_RE = re.compile(r"\b1[3-9]\d{9}\b")
_BANK_CARD_RE = re.compile(r"\b\d{16,19}\b")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{16,}")
_PASSWORD_CONTEXT_RE = re.compile(r"(?i)(password|passwd|pwd|密码|口令)")

REDACTED = "<redacted>"


class DistillError(Exception):
    """Fail-closed rejection of an IR or draft request. The message is
    shown to the user, so it must name the concrete problem."""


@dataclass
class SkillDraft:
    """An uninstalled skill draft ready for human review."""

    name: str
    summary: str
    files: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    redactions: int = 0


def validate_slug(name: str) -> str:
    """Return ``name`` if it is a legal skill slug, else raise DistillError."""
    if not isinstance(name, str) or not name:
        raise DistillError("技能名不能为空；请提供小写字母/数字/连字符组成的名称")
    if not SLUG_RE.match(name):
        raise DistillError(
            f"技能名 {name!r} 不合法：只允许小写字母、数字和连字符，"
            "且不能以连字符开头或结尾"
        )
    return name


def slugify(value: str) -> str:
    """Best-effort slug from a model-suggested name. May return an empty
    string when nothing usable remains; callers must treat that as a
    fail-closed signal and ask the user for an explicit name."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug


def _redact_text(text: str) -> Tuple[str, bool]:
    """Redact sensitive values from a single step text.

    Returns (possibly-redacted text, was_redacted). Password-context text
    is redacted wholesale; other patterns redact only the matched value.
    """
    if _PASSWORD_CONTEXT_RE.search(text):
        return REDACTED, True
    redacted = text
    hit = False
    for pattern in (_JWT_RE, _BEARER_RE, _ID_CARD_RE, _PHONE_RE, _BANK_CARD_RE):
        new = pattern.sub(REDACTED, redacted)
        if new != redacted:
            hit = True
            redacted = new
    return redacted, hit


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise DistillError(message)


def validate_ir(ir: Any) -> Dict[str, Any]:
    """Validate a step-IR mapping, returning it unchanged on success.

    Fail-closed: any structural problem raises DistillError naming the
    offending field. Unknown extra fields are tolerated (forward compat).
    """
    _require(isinstance(ir, dict), "步骤 IR 必须是 JSON 对象")
    _require(
        ir.get("schema_version") == IR_SCHEMA_VERSION,
        f"步骤 IR schema_version 必须为 {IR_SCHEMA_VERSION}，实际为 {ir.get('schema_version')!r}",
    )
    _require(isinstance(ir.get("recording_id"), str) and ir["recording_id"].strip(),
             "步骤 IR 缺少 recording_id")
    _require(isinstance(ir.get("model_used"), str) and ir["model_used"].strip(),
             "步骤 IR 缺少 model_used（必须记录分析所用模型）")
    _require(isinstance(ir.get("summary"), str) and ir["summary"].strip(),
             "步骤 IR 缺少 summary")
    _require(isinstance(ir.get("suggested_skill_name"), str)
             and ir["suggested_skill_name"].strip(),
             "步骤 IR 缺少 suggested_skill_name")
    steps = ir.get("steps")
    _require(isinstance(steps, list) and len(steps) > 0, "步骤 IR 的 steps 必须是非空数组")
    warnings = ir.get("warnings", [])
    _require(isinstance(warnings, list) and all(isinstance(w, str) for w in warnings),
             "步骤 IR 的 warnings 必须是字符串数组")

    for i, step in enumerate(steps):
        where = f"steps[{i}]"
        _require(isinstance(step, dict), f"{where} 必须是对象")
        _require(isinstance(step.get("index"), int) and not isinstance(step.get("index"), bool),
                 f"{where}.index 必须是整数")
        _require(isinstance(step.get("timestamp_s"), (int, float))
                 and not isinstance(step.get("timestamp_s"), bool)
                 and step["timestamp_s"] >= 0,
                 f"{where}.timestamp_s 必须是非负数字")
        _require(step.get("action") in IR_ACTIONS,
                 f"{where}.action 必须是 {sorted(IR_ACTIONS)} 之一，实际为 {step.get('action')!r}")
        _require(isinstance(step.get("target_description"), str)
                 and step["target_description"].strip(),
                 f"{where}.target_description 不能为空")
        region = step.get("region")
        if region is not None:
            _require(isinstance(region, list) and len(region) == 4
                     and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in region),
                     f"{where}.region 必须是 [x, y, w, h] 四元数组")
        text = step.get("text")
        _require(text is None or isinstance(text, str), f"{where}.text 必须是字符串或 null")
        confidence = step.get("confidence")
        _require(isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                 and 0.0 <= confidence <= 1.0,
                 f"{where}.confidence 必须在 [0, 1] 区间")
    return ir


def _format_timestamp(seconds: float) -> str:
    minutes = int(seconds // 60)
    secs = seconds - minutes * 60
    return f"{minutes:02d}:{secs:04.1f}"


def _redact_steps(steps: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str], int]:
    """Return (redacted step copies, redaction warnings, redaction count).
    Input steps are never mutated."""
    out: List[Dict[str, Any]] = []
    warnings: List[str] = []
    count = 0
    for step in steps:
        copied = dict(step)
        text = copied.get("text")
        if isinstance(text, str) and text:
            new_text, hit = _redact_text(text)
            if hit:
                copied["text"] = new_text
                count += 1
                warnings.append(f"步骤 {copied['index']} 的文本已脱敏（命中敏感信息模式）")
        out.append(copied)
    return out, warnings, count


def _render_skill_md(name: str, summary: str, ir: Dict[str, Any],
                     steps: List[Dict[str, Any]], warnings: List[str],
                     generated_at: str) -> str:
    low_confidence = [s for s in steps if s["confidence"] < 0.5]
    # JSON double-quoting is valid YAML for scalars and keeps the
    # frontmatter parseable for arbitrary summary text.
    yaml_description = json.dumps(summary, ensure_ascii=False)
    lines: List[str] = [
        "---",
        f"name: {name}",
        f"description: {yaml_description}",
        "metadata:",
        "  smart_assistant:",
        "    os:",
        "      - windows",
        "---",
        "",
        f"# {summary}",
        "",
        f"> 本技能由屏幕录制蒸馏生成（录制 `{ir['recording_id']}`，分析模型 "
        f"`{ir['model_used']}`，生成于 {generated_at}）。步骤来自视觉模型识别，"
        "执行前请人工复核。",
        "",
        "## 何时使用",
        "",
        summary,
        "",
        "## 操作步骤",
        "",
    ]
    for step in steps:
        stamp = _format_timestamp(step["timestamp_s"])
        entry = f"{step['index']}. [{stamp}] {step['action']} — {step['target_description']}"
        if step.get("text"):
            entry += f"（输入：{step['text']}）"
        if step["confidence"] < 0.5:
            entry += f" ⚠️ 置信度 {step['confidence']:.2f}，需人工复核"
        lines.append(entry)
    lines += ["", "## 注意事项", ""]
    if low_confidence:
        idx = ", ".join(f"#{s['index']}" for s in low_confidence)
        lines.append(f"- 低置信度步骤：{idx}，执行前务必人工确认")
    for warning in warnings:
        lines.append(f"- {warning}")
    if not low_confidence and not warnings:
        lines.append("- 无")
    lines.append("")
    return "\n".join(lines)


def _render_scripts_readme(name: str, ir: Dict[str, Any], generated_at: str) -> str:
    return "\n".join([
        f"# {name} — 蒸馏来源说明",
        "",
        "本技能由屏幕录制蒸馏自动生成：",
        f"- 录制 ID：`{ir['recording_id']}`",
        f"- 分析模型：`{ir['model_used']}`",
        f"- 生成时间：{generated_at}",
        "",
        "`steps.json` 是分析得到的步骤中间表示（IR）归档，供人工复核与后续",
        "回放验证使用。当前版本不含可执行脚本；如需将其改造为可执行自动化，",
        "请人工逐步核对步骤后再行扩展。",
        "",
    ])


def distill(ir: Dict[str, Any], *, name: Optional[str] = None,
            description: Optional[str] = None,
            existing_names: Iterable[str] = (),
            generated_at: Optional[str] = None) -> SkillDraft:
    """Distill a validated step IR into a reviewable skill draft.

    Fail-closed on: invalid IR, unusable/illegal skill name, or name
    conflict with ``existing_names``. Never mutates ``ir``.
    """
    validate_ir(ir)

    raw_name = name if name is not None else slugify(ir["suggested_skill_name"])
    if not raw_name:
        raise DistillError(
            f"模型建议的名称 {ir['suggested_skill_name']!r} 无法转为合法技能名，"
            "请显式指定 name（小写字母/数字/连字符）"
        )
    final_name = validate_slug(raw_name)
    if final_name in set(existing_names):
        raise DistillError(f"技能名 {final_name!r} 与现有技能冲突，请改名后重试")

    summary = (description or ir["summary"]).strip()
    _require(bool(summary), "技能描述不能为空")
    # Frontmatter and markdown are single-line contexts for the summary.
    summary = re.sub(r"\s*\n\s*", " ", summary)

    steps, redaction_warnings, redaction_count = _redact_steps(ir["steps"])
    stamp = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    warnings = list(ir.get("warnings", [])) + redaction_warnings
    redacted_ir = dict(ir)
    redacted_ir["steps"] = steps

    files = {
        "SKILL.md": _render_skill_md(final_name, summary, ir, steps, warnings, stamp),
        "scripts/steps.json": json.dumps(redacted_ir, ensure_ascii=False, indent=2,
                                         sort_keys=True) + "\n",
        "scripts/README.md": _render_scripts_readme(final_name, ir, stamp),
    }
    return SkillDraft(name=final_name, summary=summary, files=files,
                      warnings=warnings, redactions=redaction_count)


def write_draft(draft: SkillDraft, target_dir: Path) -> Path:
    """Write a draft into ``target_dir`` as deterministic LF bytes.

    Fail-closed: refuses to overwrite an existing non-empty directory and
    refuses any path escape. Returns the draft directory path.
    """
    target_dir = Path(target_dir)
    skill_dir = target_dir / draft.name
    if skill_dir.exists() and any(skill_dir.iterdir()):
        raise DistillError(f"目标目录已存在且非空：{skill_dir}；拒绝覆盖")
    for relpath, content in draft.files.items():
        parts = Path(relpath).parts
        if any(part in ("..", ".", "") for part in parts) or Path(relpath).is_absolute():
            raise DistillError(f"非法的草稿文件路径：{relpath!r}")
        dest = skill_dir.joinpath(*parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content.encode("utf-8"))
    return skill_dir
