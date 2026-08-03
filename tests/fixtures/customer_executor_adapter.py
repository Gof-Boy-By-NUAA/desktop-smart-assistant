"""测试客户验收子进程执行协议。"""

from __future__ import annotations

import json
import hashlib
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


_EXECUTOR_ARTIFACT_SHA256 = "d" * 64


def _canonical(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def main() -> int:
    request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    expected = request["input"].get("candidate_output", {"answer": "expected"})
    output = expected if request["skill"] is not None else {"answer": "wrong"}
    latency_ms = 0.8 if request["skill"] is not None else 1.0
    cpu_time_ms = 0.6 if request["skill"] is not None else 1.0
    peak_rss_bytes = 800 if request["skill"] is not None else 1000
    snapshot = {
        "tenant_id": request["tenant_id"],
        "model_id": request["model"]["id"],
        "model_parameters": request["model"]["parameters"],
        "endpoint_sha256": request["model"]["endpoint_sha256"],
        "prompt_sha256": request["model"]["prompt_sha256"],
        "tools_sha256": request["model"]["tools_sha256"],
        "comparison_environment_sha256": (
            request["comparison_environment_sha256"]
        ),
    }
    snapshot_hash = hashlib.sha256(_canonical(snapshot)).hexdigest()
    request_hash = hashlib.sha256(_canonical(request)).hexdigest()
    output_hash = hashlib.sha256(_canonical(output)).hexdigest()
    attestation = {
        "schema_version": 1,
        "kind": "customer-execution",
        "run_id": request["run_id"],
        "case_id": request["case_id"],
        "arm": request["arm"],
        "request_sha256": request_hash,
        "execution_snapshot_sha256": snapshot_hash,
        "output_sha256": output_hash,
        "latency_ms": latency_ms,
        "cpu_time_ms": cpu_time_ms,
        "peak_rss_bytes": peak_rss_bytes,
        "input_tokens": 10,
        "output_tokens": 2,
        "comparison_environment_sha256": (
            request["comparison_environment_sha256"]
        ),
        "requested_release_identity_sha256": (
            request["requested_release_identity_sha256"]
        ),
        "observed_release_identity_sha256": (
            request["requested_release_identity_sha256"]
        ),
        "executor_artifact_sha256": _EXECUTOR_ARTIFACT_SHA256,
    }
    signature = Ed25519PrivateKey.from_private_bytes(b"\x11" * 32).sign(
        _canonical(attestation)
    ).hex()
    response = {
        "schema_version": 1,
        "run_id": request["run_id"],
        "case_id": request["case_id"],
        "arm": request["arm"],
        "model_id": request["model"]["id"],
        "execution_snapshot_sha256": snapshot_hash,
        "request_sha256": request_hash,
        "executor_artifact_sha256": _EXECUTOR_ARTIFACT_SHA256,
        "observed_release_identity_sha256": (
            request["requested_release_identity_sha256"]
        ),
        "attestation_signature": signature,
        "latency_ms": latency_ms,
        "resource_usage": {
            "cpu_time_ms": cpu_time_ms,
            "peak_rss_bytes": peak_rss_bytes,
        },
        "output": output,
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }
    sys.stdout.write(
        json.dumps(response, ensure_ascii=False, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
