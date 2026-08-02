"""客户验收证据共用的严格 JSON 工具。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, Tuple

from .contracts import CustomerPackageError


_SHA256_LENGTH = 64


def canonical_json_bytes(value: Any) -> bytes:
    """生成拒绝 NaN 的稳定 UTF-8 JSON。"""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CustomerPackageError("内容必须是标准 JSON 值") from error


def sha256_json(value: Any) -> str:
    """计算规范 JSON 的 SHA-256。"""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def strict_json_loads(payload: bytes, field_name: str) -> Any:
    """拒绝重复键、非 UTF-8 和非标准数值。"""

    def reject_duplicates(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CustomerPackageError(
                    "%s 包含重复字段: %s" % (field_name, key)
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise CustomerPackageError(
            "%s 包含非标准数值: %s" % (field_name, value)
        )

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except UnicodeDecodeError as error:
        raise CustomerPackageError("%s 必须是 UTF-8 JSON" % field_name) from error
    except json.JSONDecodeError as error:
        raise CustomerPackageError("%s 必须是有效 JSON" % field_name) from error


def clean_text(value: Any, field_name: str, maximum: int = 256) -> str:
    """校验短文本标识符。"""

    if not isinstance(value, str) or not value.strip():
        raise CustomerPackageError("%s 不能为空" % field_name)
    normalized = value.strip()
    if len(normalized) > maximum or any(ord(char) < 32 for char in normalized):
        raise CustomerPackageError("%s 格式无效" % field_name)
    return normalized


def clean_sha256(value: Any, field_name: str) -> str:
    """校验并规范化 SHA-256 十六进制文本。"""

    normalized = clean_text(value, field_name, _SHA256_LENGTH).lower()
    if len(normalized) != _SHA256_LENGTH or any(
        char not in "0123456789abcdef" for char in normalized
    ):
        raise CustomerPackageError(
            "%s 必须是 SHA-256 十六进制" % field_name
        )
    return normalized
