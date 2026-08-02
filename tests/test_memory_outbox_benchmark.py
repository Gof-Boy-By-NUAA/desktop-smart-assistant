"""治理记忆真实数据门禁的公式测试。"""

from benchmarks.memory.outbox import _build_gates


def _metrics():
    return {
        "fact_write_latency_ms_p95": 10.0,
        "initial_pending_job_count": 848,
        "initial_recovery_documents_per_second": 100.0,
        "initial_active_count": 848,
        "initial_projection_match_count": 848,
        "initial_index_matches": True,
        "revoke_count": 50,
        "pending_before_revoke_recovery": 50,
        "revoke_recovery_documents_per_second": 100.0,
        "final_active_count": 798,
        "final_index_matches": True,
        "pending_after_recovery": 0,
        "revoked_index_count": 0,
        "revoked_projection_count": 0,
    }


def test_complete_real_dataset_evidence_passes_all_gate_formulas():
    gates = _build_gates(
        _metrics(),
        evaluated_count=848,
        available_count=848,
        max_documents=None,
    )

    assert all(gate["passed"] for gate in gates)


def test_diagnostic_subset_cannot_pass_official_data_gate():
    metrics = _metrics()
    metrics.update(
        {
            "initial_pending_job_count": 10,
            "initial_active_count": 10,
            "initial_projection_match_count": 10,
            "revoke_count": 2,
            "pending_before_revoke_recovery": 2,
            "final_active_count": 8,
        }
    )
    gates = _build_gates(
        metrics,
        evaluated_count=10,
        available_count=848,
        max_documents=10,
    )

    full_data = next(
        gate for gate in gates if gate["name"] == "data.full_document_set"
    )
    assert not full_data["passed"]


def test_any_revoked_pollution_fails_the_gate():
    metrics = _metrics()
    metrics["revoked_index_count"] = 1
    gates = _build_gates(
        metrics,
        evaluated_count=848,
        available_count=848,
        max_documents=None,
    )

    pollution = next(
        gate for gate in gates if gate["name"] == "safety.revoked_index_pollution"
    )
    assert not pollution["passed"]
