"""严格解析并验证 GitHub issue 技能选择银标。"""

from __future__ import annotations

import hashlib
import json
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple


DEFAULT_DATASET_PATH = Path(__file__).with_name("github_issue_skill_selection.json")
EXPECTED_DATASET_SHA256 = (
    "ed5587e918854a32e8ee143550f2ef88e841bf311a3efbc3eb43c756edf889d8"
)
_MAX_DATASET_BYTES = 2 * 1024 * 1024
_MAX_RAW_RESPONSE_BYTES = 2 * 1024 * 1024
_EXPECTED_SNAPSHOT_FIELDS = (
    "number",
    "node_id",
    "title",
    "html_url",
    "state",
    "created_at",
    "updated_at",
    "pull_request",
)


class SkillSelectionDatasetError(ValueError):
    """数据集编码、结构、来源或冻结标签不可信。"""


@dataclass(frozen=True)
class AnnotationRule:
    """确定性银标规则。"""

    skill_name: str
    markers: Tuple[str, ...]


@dataclass(frozen=True)
class SkillSelectionCase:
    """一条只使用公开标题的路由样本。"""

    number: int
    node_id: str
    title: str
    html_url: str
    state: str
    created_at: str
    updated_at: str
    pull_request: bool
    expected_skill_names: Tuple[str, ...]
    fetch_provenance: "FetchProvenance"


@dataclass(frozen=True)
class FetchProvenance:
    """一条 GitHub REST 响应的本地字节和 HTTP 证明。"""

    http_status: int
    response_date: str
    etag: str
    fetched_at: str
    raw_response_path: str
    raw_response_sha256: str
    canonical_sha256: str


@dataclass(frozen=True)
class SkillSelectionDataset:
    """已完成哈希、结构、时序和标签验证的数据集。"""

    schema_version: int
    dataset_id: str
    snapshot_generated_at: str
    source: Dict[str, object]
    label_tier: str
    normalization: str
    rules: Tuple[AnnotationRule, ...]
    design_cases: Tuple[SkillSelectionCase, ...]
    evaluation_cases: Tuple[SkillSelectionCase, ...]
    sha256: str
    design_split_sha256: str
    evaluation_split_sha256: str
    provenance_complete: bool

    @property
    def skill_names(self) -> Tuple[str, ...]:
        return tuple(rule.skill_name for rule in self.rules)


def normalize_annotation_text(value: str) -> str:
    """按冻结规则执行 NFKC 和不区分大小写归一化。"""

    return unicodedata.normalize("NFKC", value).casefold()


def recompute_silver_labels(
    title: str, rules: Iterable[AnnotationRule]
) -> Tuple[str, ...]:
    """仅依据标题和冻结规则复算确定性银标。"""

    normalized_title = normalize_annotation_text(title)
    selected = []
    for rule in rules:
        if any(
            normalize_annotation_text(marker) in normalized_title
            for marker in rule.markers
        ):
            selected.append(rule.skill_name)
    return tuple(sorted(selected))


def load_skill_selection_dataset(
    path: Path = DEFAULT_DATASET_PATH,
    expected_sha256: Optional[str] = EXPECTED_DATASET_SHA256,
) -> SkillSelectionDataset:
    """从不可替换的 UTF-8 JSON 字节构建已验证数据集。"""

    dataset_path = Path(path)
    try:
        size = dataset_path.stat().st_size
    except OSError as exc:
        raise SkillSelectionDatasetError("无法读取技能选择数据集") from exc
    if size <= 0 or size > _MAX_DATASET_BYTES:
        raise SkillSelectionDatasetError("技能选择数据集大小无效")
    try:
        payload = dataset_path.read_bytes()
    except OSError as exc:
        raise SkillSelectionDatasetError("无法读取技能选择数据集") from exc
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None:
        if not _is_sha256(expected_sha256):
            raise SkillSelectionDatasetError("期望的数据集 SHA-256 格式无效")
        if actual_sha256 != expected_sha256.lower():
            raise SkillSelectionDatasetError(
                "数据集 SHA-256 与冻结值不一致: %s" % actual_sha256
            )
    document = _decode_json(payload)
    return _parse_dataset(document, actual_sha256, dataset_path.parent)


def _decode_json(payload: bytes) -> Dict[str, object]:
    try:
        text = payload.decode("utf-8")
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_standard_number,
        )
    except UnicodeDecodeError as exc:
        raise SkillSelectionDatasetError("数据集必须是严格 UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise SkillSelectionDatasetError("数据集必须是有效 JSON") from exc
    if not isinstance(document, dict):
        raise SkillSelectionDatasetError("数据集顶层必须是对象")
    return document


def _parse_dataset(
    document: Dict[str, object], actual_sha256: str, dataset_directory: Path
) -> SkillSelectionDataset:
    _require_keys(
        document,
        {
            "schema_version",
            "dataset_id",
            "snapshot_generated_at",
            "source",
            "annotation_policy",
            "design_cases",
            "evaluation_cases",
        },
        "数据集顶层",
    )
    schema_version = document["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise SkillSelectionDatasetError("schema_version 必须是整数")
    dataset_id = _require_string(document["dataset_id"], "dataset_id")
    snapshot_text, snapshot_time = _require_timestamp(
        document["snapshot_generated_at"], "snapshot_generated_at"
    )
    source = _parse_source(document["source"])
    if schema_version != 2:
        raise SkillSelectionDatasetError(
            "schema_version 必须为 2；旧数据缺少可验证抓取来源"
        )
    label_tier, normalization, rules = _parse_annotation_policy(
        document["annotation_policy"]
    )
    design_cases = _parse_cases(
        document["design_cases"],
        "design_cases",
        rules,
        source,
        dataset_directory,
    )
    evaluation_cases = _parse_cases(
        document["evaluation_cases"],
        "evaluation_cases",
        rules,
        source,
        dataset_directory,
    )
    if not design_cases or not evaluation_cases:
        raise SkillSelectionDatasetError("设计集和评测集都不能为空")

    all_cases = design_cases + evaluation_cases
    numbers = [case.number for case in all_cases]
    node_ids = [case.node_id for case in all_cases]
    if len(numbers) != len(set(numbers)):
        raise SkillSelectionDatasetError("设计集和评测集的 number 必须全局唯一")
    if len(node_ids) != len(set(node_ids)):
        raise SkillSelectionDatasetError("设计集和评测集的 node_id 必须全局唯一")

    newest_event = max(_parse_timestamp(case.created_at, "created_at") for case in all_cases)
    newest_event = max(
        newest_event,
        max(
            _parse_timestamp(case.updated_at, "updated_at")
            for case in all_cases
        ),
    )
    if snapshot_time < newest_event:
        raise SkillSelectionDatasetError(
            "snapshot_generated_at 早于样本的 created_at/updated_at，快照时序不可信"
        )
    newest_fetch = max(
        _parse_timestamp(case.fetch_provenance.fetched_at, "fetched_at")
        for case in all_cases
    )
    if snapshot_time < newest_fetch:
        raise SkillSelectionDatasetError(
            "snapshot_generated_at 早于 fetched_at，快照不可能包含该抓取结果"
        )

    return SkillSelectionDataset(
        schema_version=2,
        dataset_id=dataset_id,
        snapshot_generated_at=snapshot_text,
        source=source,
        label_tier=label_tier,
        normalization=normalization,
        rules=rules,
        design_cases=design_cases,
        evaluation_cases=evaluation_cases,
        sha256=actual_sha256,
        design_split_sha256=hashlib.sha256(
            _canonical_json(document["design_cases"])
        ).hexdigest(),
        evaluation_split_sha256=hashlib.sha256(
            _canonical_json(document["evaluation_cases"])
        ).hexdigest(),
        provenance_complete=True,
    )


def _parse_source(value: object) -> Dict[str, object]:
    if not isinstance(value, dict):
        raise SkillSelectionDatasetError("source 必须是对象")
    if "provenance_complete" not in value:
        raise SkillSelectionDatasetError(
            "source.provenance_complete 缺失，抓取来源证明不完整"
        )
    _require_keys(
        value,
        {
            "repository",
            "api",
            "license",
            "snapshot_fields",
            "body_or_comments_included",
            "provenance_complete",
        },
        "source",
    )
    repository = _require_string(value["repository"], "source.repository")
    api = _require_string(value["api"], "source.api")
    license_name = _require_string(value["license"], "source.license")
    snapshot_fields = _require_string_array(
        value["snapshot_fields"], "source.snapshot_fields"
    )
    if value["body_or_comments_included"] is not False:
        raise SkillSelectionDatasetError(
            "source.body_or_comments_included 必须为 false"
        )
    if value["provenance_complete"] is not True:
        raise SkillSelectionDatasetError(
            "source.provenance_complete 必须为 true，否则数据不具备生产评测资格"
        )
    if repository != "https://github.com/zhayujie/SmartAssistant":
        raise SkillSelectionDatasetError("source.repository 不是冻结仓库")
    if api != "https://api.github.com/repos/zhayujie/SmartAssistant/issues/{number}":
        raise SkillSelectionDatasetError("source.api 不是冻结 GitHub API")
    if license_name != "public GitHub issue metadata":
        raise SkillSelectionDatasetError("source.license 与冻结值不一致")
    if snapshot_fields != _EXPECTED_SNAPSHOT_FIELDS:
        raise SkillSelectionDatasetError("source.snapshot_fields 与严格结构不一致")
    return {
        "repository": repository,
        "api": api,
        "license": license_name,
        "snapshot_fields": snapshot_fields,
        "body_or_comments_included": False,
        "provenance_complete": True,
    }


def _parse_annotation_policy(
    value: object,
) -> Tuple[str, str, Tuple[AnnotationRule, ...]]:
    if not isinstance(value, dict):
        raise SkillSelectionDatasetError("annotation_policy 必须是对象")
    _require_keys(
        value,
        {"label_tier", "normalization", "multi_label_allowed", "rules"},
        "annotation_policy",
    )
    label_tier = _require_string(value["label_tier"], "annotation_policy.label_tier")
    normalization = _require_string(
        value["normalization"], "annotation_policy.normalization"
    )
    if label_tier != "deterministic_silver":
        raise SkillSelectionDatasetError("label_tier 必须为 deterministic_silver")
    if normalization != "NFKC casefold":
        raise SkillSelectionDatasetError("normalization 必须为 NFKC casefold")
    if value["multi_label_allowed"] is not True:
        raise SkillSelectionDatasetError("multi_label_allowed 必须为 true")
    raw_rules = value["rules"]
    if not isinstance(raw_rules, list) or not raw_rules:
        raise SkillSelectionDatasetError("annotation_policy.rules 必须是非空数组")
    rules = []
    marker_owners: Dict[str, str] = {}
    for index, raw_rule in enumerate(raw_rules, start=1):
        if not isinstance(raw_rule, dict):
            raise SkillSelectionDatasetError("第 %d 条标注规则必须是对象" % index)
        _require_keys(raw_rule, {"skill_name", "markers"}, "annotation_policy.rules")
        skill_name = _require_string(raw_rule["skill_name"], "rule.skill_name")
        markers = _require_string_array(raw_rule["markers"], "rule.markers")
        if not markers:
            raise SkillSelectionDatasetError("rule.markers 不能为空")
        normalized_markers = [normalize_annotation_text(marker) for marker in markers]
        if len(normalized_markers) != len(set(normalized_markers)):
            raise SkillSelectionDatasetError("同一规则包含重复 marker")
        for marker in normalized_markers:
            owner = marker_owners.get(marker)
            if owner is not None and owner != skill_name:
                raise SkillSelectionDatasetError("marker 被多个技能共享，银标有歧义")
            marker_owners[marker] = skill_name
        rules.append(AnnotationRule(skill_name=skill_name, markers=markers))
    names = [rule.skill_name for rule in rules]
    if len(names) != len(set(names)):
        raise SkillSelectionDatasetError("annotation_policy.rules 的 skill_name 必须唯一")
    return label_tier, normalization, tuple(rules)


def _parse_cases(
    value: object,
    field_name: str,
    rules: Tuple[AnnotationRule, ...],
    source: Dict[str, object],
    dataset_directory: Path,
) -> Tuple[SkillSelectionCase, ...]:
    if not isinstance(value, list):
        raise SkillSelectionDatasetError("%s 必须是数组" % field_name)
    cases = []
    known_skills = {rule.skill_name for rule in rules}
    for index, raw_case in enumerate(value, start=1):
        context = "%s[%d]" % (field_name, index)
        if not isinstance(raw_case, dict):
            raise SkillSelectionDatasetError("%s 必须是对象" % context)
        _require_keys(
            raw_case,
            set(_EXPECTED_SNAPSHOT_FIELDS)
            | {"expected_skill_names", "fetch_provenance"},
            context,
        )
        number = raw_case["number"]
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise SkillSelectionDatasetError("%s.number 必须是正整数" % context)
        node_id = _require_string(raw_case["node_id"], "%s.node_id" % context)
        title = _require_string(raw_case["title"], "%s.title" % context)
        html_url = _require_string(raw_case["html_url"], "%s.html_url" % context)
        state = _require_string(raw_case["state"], "%s.state" % context)
        if state not in {"open", "closed"}:
            raise SkillSelectionDatasetError("%s.state 只允许 open/closed" % context)
        created_at, created_time = _require_timestamp(
            raw_case["created_at"], "%s.created_at" % context
        )
        updated_at, updated_time = _require_timestamp(
            raw_case["updated_at"], "%s.updated_at" % context
        )
        if updated_time < created_time:
            raise SkillSelectionDatasetError("%s.updated_at 早于 created_at" % context)
        pull_request = raw_case["pull_request"]
        if not isinstance(pull_request, bool):
            raise SkillSelectionDatasetError("%s.pull_request 必须是布尔值" % context)
        expected_url = "%s/%s/%d" % (
            source["repository"],
            "pull" if pull_request else "issues",
            number,
        )
        if html_url != expected_url:
            raise SkillSelectionDatasetError("%s.html_url 与 number/type 不一致" % context)
        if pull_request != node_id.startswith("PR_"):
            raise SkillSelectionDatasetError("%s.node_id 与 pull_request 不一致" % context)
        labels = _require_string_array(
            raw_case["expected_skill_names"], "%s.expected_skill_names" % context
        )
        if labels != tuple(sorted(set(labels))):
            raise SkillSelectionDatasetError(
                "%s.expected_skill_names 必须排序且去重" % context
            )
        if not set(labels).issubset(known_skills):
            raise SkillSelectionDatasetError("%s 包含未知技能标签" % context)
        recomputed = recompute_silver_labels(title, rules)
        if labels != recomputed:
            raise SkillSelectionDatasetError(
                "%s 冻结标签与 annotation_policy 复算结果不一致" % context
            )
        canonical_record = {
            "number": number,
            "node_id": node_id,
            "title": title,
            "html_url": html_url,
            "state": state,
            "created_at": created_at,
            "updated_at": updated_at,
            "pull_request": pull_request,
        }
        fetch_provenance = _parse_fetch_provenance(
            raw_case["fetch_provenance"],
            context,
            dataset_directory,
            canonical_record,
            updated_time,
        )
        cases.append(
            SkillSelectionCase(
                number=number,
                node_id=node_id,
                title=title,
                html_url=html_url,
                state=state,
                created_at=created_at,
                updated_at=updated_at,
                pull_request=pull_request,
                expected_skill_names=labels,
                fetch_provenance=fetch_provenance,
            )
        )
    return tuple(cases)


def _parse_fetch_provenance(
    value: object,
    context: str,
    dataset_directory: Path,
    canonical_record: Dict[str, object],
    updated_time: datetime,
) -> FetchProvenance:
    field_name = "%s.fetch_provenance" % context
    if not isinstance(value, dict):
        raise SkillSelectionDatasetError("%s 必须是对象" % field_name)
    _require_keys(
        value,
        {
            "http_status",
            "response_date",
            "etag",
            "fetched_at",
            "raw_response_path",
            "raw_response_sha256",
            "canonical_sha256",
        },
        field_name,
    )
    http_status = value["http_status"]
    if http_status != 200 or isinstance(http_status, bool):
        raise SkillSelectionDatasetError("%s.http_status 必须为 200" % field_name)
    response_date = _require_string(
        value["response_date"], "%s.response_date" % field_name
    )
    response_time = _parse_http_date(response_date, "%s.response_date" % field_name)
    if response_time < updated_time:
        raise SkillSelectionDatasetError(
            "%s.response_date 早于 issue.updated_at" % field_name
        )
    etag_value = value["etag"]
    if etag_value is None:
        raise SkillSelectionDatasetError(
            "%s.etag 为 null，该条抓取证明不合格" % field_name
        )
    etag = _require_string(etag_value, "%s.etag" % field_name)
    if len(etag) > 512:
        raise SkillSelectionDatasetError("%s.etag 过长" % field_name)
    fetched_at, fetched_time = _require_timestamp(
        value["fetched_at"], "%s.fetched_at" % field_name
    )
    if fetched_time < response_time:
        raise SkillSelectionDatasetError("%s.fetched_at 早于 response_date" % field_name)
    if fetched_time < updated_time:
        raise SkillSelectionDatasetError("%s.fetched_at 早于 issue.updated_at" % field_name)
    raw_response_path = _require_string(
        value["raw_response_path"], "%s.raw_response_path" % field_name
    )
    raw_sha256 = _require_sha256(
        value["raw_response_sha256"], "%s.raw_response_sha256" % field_name
    )
    canonical_sha256 = _require_sha256(
        value["canonical_sha256"], "%s.canonical_sha256" % field_name
    )
    raw_path = _resolve_evidence_path(
        dataset_directory, raw_response_path, "%s.raw_response_path" % field_name
    )
    try:
        if raw_path.stat().st_size > _MAX_RAW_RESPONSE_BYTES:
            raise SkillSelectionDatasetError("归档的 GitHub REST 响应超过大小上限")
        raw_payload = raw_path.read_bytes()
    except OSError as exc:
        raise SkillSelectionDatasetError("无法读取归档的 GitHub REST 响应") from exc
    if hashlib.sha256(raw_payload).hexdigest() != raw_sha256:
        raise SkillSelectionDatasetError("%s 原始响应 SHA-256 不匹配" % field_name)
    raw_document = _decode_json(raw_payload)
    raw_canonical = _canonical_record_from_raw_response(raw_document, field_name)
    if _canonical_json(raw_canonical) != _canonical_json(canonical_record):
        raise SkillSelectionDatasetError("%s 与原始 REST 响应字段不一致" % field_name)
    actual_canonical_sha256 = hashlib.sha256(
        _canonical_json(canonical_record)
    ).hexdigest()
    if canonical_sha256 != actual_canonical_sha256:
        raise SkillSelectionDatasetError("%s 规范字段 SHA-256 不匹配" % field_name)
    return FetchProvenance(
        http_status=200,
        response_date=response_date,
        etag=etag,
        fetched_at=fetched_at,
        raw_response_path=raw_response_path,
        raw_response_sha256=raw_sha256,
        canonical_sha256=canonical_sha256,
    )


def _canonical_record_from_raw_response(
    document: Dict[str, object], field_name: str
) -> Dict[str, object]:
    required = set(_EXPECTED_SNAPSHOT_FIELDS) - {"pull_request"}
    missing = sorted(required - set(document))
    if missing:
        raise SkillSelectionDatasetError(
            "%s 原始 REST 响应缺少字段: %s" % (field_name, missing)
        )
    pull_request = "pull_request" in document
    if pull_request and not isinstance(document["pull_request"], dict):
        raise SkillSelectionDatasetError("%s 原始 pull_request 必须是对象" % field_name)
    return {
        "number": document["number"],
        "node_id": document["node_id"],
        "title": document["title"],
        "html_url": document["html_url"],
        "state": document["state"],
        "created_at": document["created_at"],
        "updated_at": document["updated_at"],
        "pull_request": pull_request,
    }


def _resolve_evidence_path(
    dataset_directory: Path, relative_path: str, field_name: str
) -> Path:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise SkillSelectionDatasetError("%s 必须是数据目录内的相对路径" % field_name)
    base = dataset_directory.resolve()
    candidate = base / path
    current = base
    for part in path.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag):
            raise SkillSelectionDatasetError("%s 不能经过符号链接或重解析点" % field_name)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(base)
    except (OSError, ValueError) as exc:
        raise SkillSelectionDatasetError("%s 指向的原始响应文件无效" % field_name) from exc
    if not resolved.is_file():
        raise SkillSelectionDatasetError("%s 必须指向普通文件" % field_name)
    return resolved


def _parse_http_date(value: str, field_name: str) -> datetime:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        raise SkillSelectionDatasetError("%s 不是有效 HTTP Date" % field_name) from exc
    if parsed.tzinfo is None:
        raise SkillSelectionDatasetError("%s 必须包含时区" % field_name)
    return parsed.astimezone(timezone.utc)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _require_keys(value: Dict[str, object], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise SkillSelectionDatasetError(
            "%s 字段不一致: missing=%s extra=%s" % (context, missing, extra)
        )


def _require_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SkillSelectionDatasetError("%s 必须是无首尾空白的非空字符串" % field_name)
    if any(ord(char) < 32 for char in value):
        raise SkillSelectionDatasetError("%s 包含控制字符" % field_name)
    return value


def _require_string_array(value: object, field_name: str) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise SkillSelectionDatasetError("%s 必须是字符串数组" % field_name)
    return tuple(
        _require_string(item, "%s[%d]" % (field_name, index))
        for index, item in enumerate(value)
    )


def _require_timestamp(value: object, field_name: str) -> Tuple[str, datetime]:
    text = _require_string(value, field_name)
    return text, _parse_timestamp(text, field_name)


def _parse_timestamp(value: str, field_name: str) -> datetime:
    if not value.endswith("Z"):
        raise SkillSelectionDatasetError("%s 必须是 UTC Z 时间" % field_name)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise SkillSelectionDatasetError("%s 时间格式无效" % field_name) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise SkillSelectionDatasetError("%s 必须是 UTC" % field_name)
    return parsed


def _reject_duplicate_keys(pairs: Iterable[Tuple[str, object]]) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SkillSelectionDatasetError("数据集包含重复 JSON 字段: %s" % key)
        result[key] = value
    return result


def _reject_non_standard_number(value: str) -> None:
    raise SkillSelectionDatasetError("数据集包含非标准数值: %s" % value)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _require_sha256(value: object, field_name: str) -> str:
    if not _is_sha256(value):
        raise SkillSelectionDatasetError("%s 必须是 64 位 SHA-256" % field_name)
    return str(value).lower()
