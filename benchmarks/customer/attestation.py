"""客户执行器和独立判定器的 Ed25519 证据证明。"""

from __future__ import annotations

from typing import Any, Dict

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .contracts import CustomerPackageError
from .json_utils import canonical_json_bytes


def clean_ed25519_public_key(value: Any, field_name: str) -> str:
    """校验 32 字节 Ed25519 公钥的十六进制表示。"""

    return _clean_hex(value, field_name, 64)


def clean_ed25519_signature(value: Any, field_name: str) -> str:
    """校验 64 字节 Ed25519 签名的十六进制表示。"""

    return _clean_hex(value, field_name, 128)


def execution_attestation_payload(
    *,
    run_id: str,
    case_id: str,
    arm: str,
    request_sha256: str,
    execution_snapshot_sha256: str,
    output_sha256: str,
    latency_ms: float,
    input_tokens: int,
    output_tokens: int,
    executor_artifact_sha256: str,
) -> Dict[str, Any]:
    """生成由可信执行器签名的稳定载荷。"""

    return {
        "schema_version": 1,
        "kind": "customer-execution",
        "run_id": run_id,
        "case_id": case_id,
        "arm": arm,
        "request_sha256": request_sha256,
        "execution_snapshot_sha256": execution_snapshot_sha256,
        "output_sha256": output_sha256,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "executor_artifact_sha256": executor_artifact_sha256,
    }


def judgment_attestation_payload(
    *,
    run_id: str,
    case_id: str,
    arm_label: str,
    oracle_id: str,
    output_sha256: str,
    success: bool,
    evidence_sha256: str,
    judge_artifact_sha256: str,
) -> Dict[str, Any]:
    """生成由客户独立判定器签名的稳定载荷。"""

    return {
        "schema_version": 1,
        "kind": "customer-judgment",
        "run_id": run_id,
        "case_id": case_id,
        "arm_label": arm_label,
        "oracle_id": oracle_id,
        "output_sha256": output_sha256,
        "success": success,
        "evidence_sha256": evidence_sha256,
        "judge_artifact_sha256": judge_artifact_sha256,
    }


def verify_ed25519_signature(
    public_key_hex: str,
    signature_hex: str,
    payload: Dict[str, Any],
) -> bool:
    """验证外部签名；任何格式或签名错误都返回失败。"""

    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(public_key_hex)
        )
        public_key.verify(
            bytes.fromhex(signature_hex), canonical_json_bytes(payload)
        )
    except (InvalidSignature, TypeError, ValueError):
        return False
    return True


def _clean_hex(value: Any, field_name: str, length: int) -> str:
    if not isinstance(value, str):
        raise CustomerPackageError("%s 必须是十六进制文本" % field_name)
    normalized = value.strip().lower()
    if len(normalized) != length or any(
        char not in "0123456789abcdef" for char in normalized
    ):
        raise CustomerPackageError("%s 十六进制格式无效" % field_name)
    return normalized
