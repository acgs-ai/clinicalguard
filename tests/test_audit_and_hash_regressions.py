"""Regression tests for audit query parsing, hash alignment, and fail-closed restore.

Constitutional Hash: 608508a9bd224290
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from acgs_lite.audit import AuditEntry, AuditLog
from acgs_lite.constitution import Constitution
from starlette.testclient import TestClient

from clinicalguard.agent import CONSTITUTIONAL_HASH, ClinicalGuardApp
from clinicalguard.skills.audit_query import query_audit_trail
from clinicalguard.skills.hipaa_checker import check_hipaa_compliance
from clinicalguard.skills.validate_clinical import (
    CONDITIONAL,
    RISK_HIGH,
    LLMClinicalAssessment,
)


def _make_a2a_body(text: str, task_id: str = "task-reg-001") -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "tasks/send",
        "id": "req-reg-1",
        "params": {
            "id": task_id,
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": text}],
            },
        },
    }


def _mock_assessment() -> LLMClinicalAssessment:
    return LLMClinicalAssessment(
        recommended_decision=CONDITIONAL,
        risk_tier=RISK_HIGH,
        reasoning="Test reasoning.",
        conditions=["Monitor"],
        llm_available=True,
    )


class TestQueryAuditTrailParsing:
    """Verify query_audit_trail handles recent/last N and ID lookups."""

    @pytest.fixture
    def populated_client(self, tmp_path):
        guard = ClinicalGuardApp.create(audit_log_path=tmp_path / "audit.json")
        with patch(
            "clinicalguard.skills.validate_clinical.get_llm_assessment",
            return_value=_mock_assessment(),
        ):
            client = TestClient(guard.build_starlette_app())
            ids = []
            for idx in range(4):
                resp = client.post(
                    "/",
                    json=_make_a2a_body(
                        f"validate_clinical_action: Patient SYNTH-00{idx} Lisinopril 10mg."
                    ),
                )
                ids.append(resp.json()["result"]["result"]["audit_id"])
        return client, guard, ids

    def test_recent_without_number_returns_entries(self, populated_client):
        client, _, ids = populated_client
        resp = client.post("/", json=_make_a2a_body("query_audit_trail: recent"))
        result = resp.json()["result"]["result"]
        assert result["found"] is True
        assert result["total_entries"] == 4
        assert [e["id"] for e in result["entries"]] == ids

    def test_recent_with_count_limits_entries(self, populated_client):
        client, _, ids = populated_client
        resp = client.post("/", json=_make_a2a_body("query_audit_trail: recent 2"))
        result = resp.json()["result"]["result"]
        assert result["found"] is True
        assert result["total_entries"] == 4
        assert [e["id"] for e in result["entries"]] == ids[-2:]

    def test_last_with_count_limits_entries(self, populated_client):
        client, _, ids = populated_client
        resp = client.post("/", json=_make_a2a_body("query_audit_trail: last 2"))
        result = resp.json()["result"]["result"]
        assert result["found"] is True
        assert result["total_entries"] == 4
        assert [e["id"] for e in result["entries"]] == ids[-2:]

    def test_recent_zero_does_not_dump_entire_log(self, populated_client):
        client, _, ids = populated_client
        resp = client.post("/", json=_make_a2a_body("query_audit_trail: recent 0"))
        result = resp.json()["result"]["result"]
        assert result["found"] is True
        assert [e["id"] for e in result["entries"]] == ids[-1:]

    def test_empty_query_lists_recent(self, populated_client):
        client, _, ids = populated_client
        resp = client.post("/", json=_make_a2a_body("query_audit_trail: "))
        result = resp.json()["result"]["result"]
        assert result["found"] is True
        assert [e["id"] for e in result["entries"]] == ids

    def test_hc_audit_id_lookup_still_works(self, populated_client):
        client, _, ids = populated_client
        target_id = ids[1]
        resp = client.post("/", json=_make_a2a_body(f"query_audit_trail: {target_id}"))
        result = resp.json()["result"]["result"]
        assert result["found"] is True
        assert [e["id"] for e in result["entries"]] == [target_id]

    def test_non_hc_audit_id_lookup_still_works(self, populated_client):
        client, guard, _ = populated_client
        guard.audit_log.record(
            AuditEntry(
                id="CUSTOM-ID-42",
                type="clinical_validation",
                agent_id="test",
                action="test action",
                valid=True,
                constitutional_hash=CONSTITUTIONAL_HASH,
            )
        )
        resp = client.post("/", json=_make_a2a_body("query_audit_trail: CUSTOM-ID-42"))
        result = resp.json()["result"]["result"]
        assert result["found"] is True
        assert [e["id"] for e in result["entries"]] == ["CUSTOM-ID-42"]

    def test_direct_query_limit_zero_returns_empty(self):
        log = AuditLog()
        log.record(
            AuditEntry(
                id="ev-1",
                type="clinical_validation",
                agent_id="a",
                action="x",
                valid=True,
            )
        )
        result = query_audit_trail(log, limit=0)
        assert result["found"] is True
        assert result["entries"] == []


class TestConstitutionalHashAlignment:
    """Verify /health, HIPAA checker, and clinical validation report the same hash."""

    def test_all_endpoints_report_healthcare_constitution_hash(self, tmp_path):
        constitution = Constitution.from_yaml("constitution/healthcare_v1.yaml")
        expected_hash = constitution.hash
        assert expected_hash == CONSTITUTIONAL_HASH

        guard = ClinicalGuardApp.create(audit_log_path=tmp_path / "audit.json")
        client = TestClient(guard.build_starlette_app())

        health = client.get("/health").json()
        assert health["constitutional_hash"] == expected_hash

        direct_hipaa = check_hipaa_compliance("Agent with audit log and MACI.")
        assert direct_hipaa["constitutional_hash"] == expected_hash

        a2a_hipaa = client.post(
            "/",
            json=_make_a2a_body("check_hipaa_compliance: Agent with audit log and MACI."),
        ).json()["result"]["result"]
        assert a2a_hipaa["constitutional_hash"] == expected_hash

        with patch(
            "clinicalguard.skills.validate_clinical.get_llm_assessment",
            return_value=_mock_assessment(),
        ):
            val = client.post(
                "/",
                json=_make_a2a_body("validate_clinical_action: Patient SYNTH-001 Lisinopril."),
            ).json()["result"]["result"]
        assert val["constitutional_hash"] == expected_hash


class TestFailClosedAuditRestore:
    """Verify non-empty corrupt audit logs fail closed without overwriting history."""

    @pytest.mark.parametrize(
        "corrupt_bytes",
        [
            b"{corrupt json",
            b"[]",
            b'{"entries": "not-a-list"}',
            b'{"entries": [123]}',
            b'{"entries": [{"agent_id": "missing-id"}]}',
        ],
    )
    def test_corrupt_non_empty_audit_log_raises_and_preserves_file(
        self, tmp_path, corrupt_bytes
    ):
        audit_path = tmp_path / "audit.json"
        audit_path.write_bytes(corrupt_bytes)

        with pytest.raises(RuntimeError, match="audit log"):
            ClinicalGuardApp.create(audit_log_path=audit_path)

        assert audit_path.read_bytes() == corrupt_bytes

    def test_zero_byte_empty_audit_log_starts_cleanly(self, tmp_path):
        audit_path = tmp_path / "audit.json"
        audit_path.write_bytes(b"")

        guard = ClinicalGuardApp.create(audit_log_path=audit_path)
        assert len(guard.audit_log) == 0

    def test_valid_audit_log_restores_and_appends_without_loss(self, tmp_path):
        audit_path = tmp_path / "audit.json"
        guard1 = ClinicalGuardApp.create(audit_log_path=audit_path)
        client1 = TestClient(guard1.build_starlette_app())

        with patch(
            "clinicalguard.skills.validate_clinical.get_llm_assessment",
            return_value=_mock_assessment(),
        ):
            resp1 = client1.post(
                "/",
                json=_make_a2a_body("validate_clinical_action: Patient SYNTH-001 Lisinopril."),
            )
            id1 = resp1.json()["result"]["result"]["audit_id"]

            guard2 = ClinicalGuardApp.create(audit_log_path=audit_path)
            assert len(guard2.audit_log) == 1
            assert guard2.audit_log.entries[0].id == id1

            client2 = TestClient(guard2.build_starlette_app())
            resp2 = client2.post(
                "/",
                json=_make_a2a_body("validate_clinical_action: Patient SYNTH-002 Lisinopril."),
            )
            id2 = resp2.json()["result"]["result"]["audit_id"]

        data = json.loads(audit_path.read_text())
        assert [e["id"] for e in data["entries"]] == [id1, id2]
