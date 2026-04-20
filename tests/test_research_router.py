from __future__ import annotations

import pytest

from acgs_lite.audit import AuditLog
from clinicalguard.skills import research_router as research_router_module


def test_build_plan_defaults_to_pubmed() -> None:
    entities = research_router_module._parse_deidentified_entities(
        'deidentified: {"genes": ["EGFR"], "diseases": ["NSCLC"]}'
    )
    assert entities is not None
    plan = research_router_module._build_base_plan(entities)
    assert plan["runtime"] == "pubmed"
    assert "literature_discovery" in plan["lanes"]


def test_build_plan_creates_multi_lane_queries() -> None:
    entities = research_router_module._parse_deidentified_entities(
        'deidentified: {"genes": ["EGFR"], "drugs": ["osimertinib"], "diseases": ["NSCLC"]}'
    )
    assert entities is not None
    plan = research_router_module._build_base_plan(entities)
    assert plan["runtime"] == "pubmed"
    assert "human_genetics_and_variant_interpretation" in plan["lanes"]
    assert "chemistry_ligands_and_pharmacology" in plan["lanes"]
    assert len(plan["lanes"]) == 3


def test_build_plan_routes_proteomics_queries() -> None:
    entities = research_router_module._parse_deidentified_entities(
        'deidentified: {"accessions": ["PXD024902"]}'
    )
    assert entities is not None
    plan = research_router_module._build_base_plan(entities)
    assert plan["runtime"] == "proteomexchange"
    assert plan["lanes"] == ["proteomics_dataset_context"]


def test_local_model_status_defaults_to_disabled_runtime() -> None:
    status = research_router_module._local_model_status()
    assert status["enabled"] is True
    assert status["available"] is False


def test_local_model_status_rejects_remote_endpoint(monkeypatch) -> None:
    monkeypatch.setenv(research_router_module.LOCAL_MODEL_API_BASE_ENV, "https://example.com/v1")
    status = research_router_module._local_model_status()
    assert status["available"] is False
    assert "not local/private" in status["summary"]


@pytest.mark.asyncio
async def test_research_router_requires_deidentified_prefix() -> None:
    result = await research_router_module.research_router(
        "what is known about warfarin plus aspirin?"
    )
    assert result["evidence_by_lane"] == []
    assert "did not provide explicitly de-identified structured entities" in result["direct_answer"]


@pytest.mark.asyncio
async def test_research_router_reports_missing_runtime(monkeypatch) -> None:
    monkeypatch.setattr(
        research_router_module,
        "_resolve_runtime_script",
        lambda relative_path: (_ for _ in ()).throw(FileNotFoundError("runtime missing")),
    )
    result = await research_router_module.research_router(
        'deidentified: {"drugs": ["warfarin", "aspirin"]}'
    )
    assert result["runtime"]["retrieval_runtime_available"] is False
    assert "runtime missing" in result["main_caveats"][0]


@pytest.mark.asyncio
async def test_research_router_blocks_sensitive_input() -> None:
    result = await research_router_module.research_router(
        'deidentified: {"drugs": ["warfarin"], "diseases": ["patient SSN 123-45-6789"]}'
    )
    assert result["evidence_by_lane"] == []
    assert "de-identified contract" in result["direct_answer"]
    assert "ssn" in result["sensitivity_findings"]


@pytest.mark.asyncio
async def test_research_router_blocks_non_normalized_entity_payload() -> None:
    result = await research_router_module.research_router(
        'deidentified: {"drugs": ["warfarin"], "diseases": ["John Doe"]}'
    )
    assert result["evidence_by_lane"] == []
    assert "did not satisfy the de-identified contract" in result["direct_answer"]
    assert any(item.startswith("diseases:") for item in result["sensitivity_findings"])


def test_validate_runtime_startup_requires_explicit_root(monkeypatch) -> None:
    monkeypatch.setenv(research_router_module.PLUGIN_ROOT_ENV, "/tmp/missing-runtime")
    monkeypatch.setattr(
        research_router_module,
        "_resolve_runtime_script",
        lambda relative_path: (_ for _ in ()).throw(FileNotFoundError("missing script")),
    )
    with pytest.raises(RuntimeError, match="retrieval runtime unavailable"):
        research_router_module.validate_runtime_startup()


def test_validate_runtime_startup_requires_local_model(monkeypatch) -> None:
    monkeypatch.setenv(research_router_module.REQUIRE_LOCAL_MODEL_ENV, "true")
    monkeypatch.delenv(research_router_module.LOCAL_MODEL_API_BASE_ENV, raising=False)
    monkeypatch.setattr(
        research_router_module,
        "_resolve_runtime_script",
        lambda relative_path: relative_path,
    )
    with pytest.raises(RuntimeError, match="Local model not configured"):
        research_router_module.validate_runtime_startup()


@pytest.mark.asyncio
async def test_research_router_pubmed_heuristic_path(monkeypatch) -> None:
    async def _fake_runtime(script_path, payload):
        if payload["endpoint"] == "esearch":
            return {"ok": True, "records": ["26467380", "39197978"]}
        if payload["endpoint"] == "esummary":
            return {
                "ok": True,
                "summary": {
                    "result": {
                        "uids": ["26467380", "39197978"],
                        "26467380": {
                            "title": "Inappropriate combination of warfarin and aspirin",
                            "source": "Anatol J Cardiol",
                            "pubdate": "2016 Mar",
                        },
                        "39197978": {
                            "title": "Anticoagulation Alone vs Anticoagulation Plus Aspirin or DAPT Following Left Atrial Appendage Occlusion.",
                            "source": "J Am Coll Cardiol",
                            "pubdate": "2024 Sep 3",
                        },
                    }
                },
            }
        return {
            "ok": True,
            "text_head": "OBJECTIVE: Combination therapy increases bleeding risk. CONCLUSIONS: Use should be carefully justified.",
        }

    monkeypatch.setattr(
        research_router_module, "_resolve_runtime_script", lambda relative_path: relative_path
    )
    monkeypatch.setattr(research_router_module, "_run_runtime_script", _fake_runtime)
    monkeypatch.delenv(research_router_module.LOCAL_MODEL_API_BASE_ENV, raising=False)
    result = await research_router_module.research_router(
        'deidentified: {"drugs": ["warfarin", "aspirin"]}'
    )
    assert result["runtime"]["local_model"]["available"] is False
    assert result["runtime"]["downstream_runtime"] == "pubmed"
    assert result["evidence_by_lane"][0]["records"][0]["pmid"] == "26467380"
    assert "OBJECTIVE:" in result["evidence_by_lane"][0]["records"][0]["abstract_summary"]


@pytest.mark.asyncio
async def test_research_router_local_model_refines_plan_and_summary(monkeypatch) -> None:
    async def _fake_runtime(script_path, payload):
        if payload["endpoint"] == "esearch":
            return {"ok": True, "records": ["26467380"]}
        if payload["endpoint"] == "esummary":
            return {
                "ok": True,
                "summary": {
                    "result": {
                        "uids": ["26467380"],
                        "26467380": {
                            "title": "Inappropriate combination of warfarin and aspirin",
                            "source": "Anatol J Cardiol",
                            "pubdate": "2016 Mar",
                        },
                    }
                },
            }
        return {
            "ok": True,
            "text_head": "OBJECTIVE: Combination therapy increases bleeding risk. CONCLUSIONS: Use should be carefully justified.",
        }

    async def _fake_local_completion(*, schema_name, parser, **kwargs):
        if schema_name == "lane_plan":
            return parser(
                {
                    "direct_answer": "Local model refined the plan.",
                    "lanes": [
                        {
                            "lane": "clinical_and_translational_evidence",
                            "query": "warfarin aspirin clinical trial outcome",
                        }
                    ],
                }
            )
        return parser(
            {
                "summary": "Combination therapy increases bleeding risk and should be justified carefully."
            }
        )

    monkeypatch.setattr(
        research_router_module, "_resolve_runtime_script", lambda relative_path: relative_path
    )
    monkeypatch.setattr(research_router_module, "_run_runtime_script", _fake_runtime)
    monkeypatch.setattr(
        research_router_module, "_complete_local_model_json", _fake_local_completion
    )
    monkeypatch.setenv(research_router_module.LOCAL_MODEL_API_BASE_ENV, "http://127.0.0.1:8000/v1")
    result = await research_router_module.research_router(
        'deidentified: {"drugs": ["warfarin", "aspirin"]}'
    )
    assert result["runtime"]["local_model"]["available"] is True
    assert result["runtime"]["lanes"] == ["clinical_and_translational_evidence"]
    assert result["evidence_by_lane"][0]["records"][0]["abstract_summary"].startswith(
        "Combination therapy"
    )


@pytest.mark.asyncio
async def test_research_router_local_model_transport_failure_falls_back(monkeypatch) -> None:
    async def _fake_runtime(script_path, payload):
        if payload["endpoint"] == "esearch":
            return {"ok": True, "records": ["26467380"]}
        if payload["endpoint"] == "esummary":
            return {
                "ok": True,
                "summary": {
                    "result": {
                        "uids": ["26467380"],
                        "26467380": {
                            "title": "Inappropriate combination of warfarin and aspirin",
                            "source": "Anatol J Cardiol",
                            "pubdate": "2016 Mar",
                        },
                    }
                },
            }
        return {
            "ok": True,
            "text_head": "OBJECTIVE: Combination therapy increases bleeding risk. CONCLUSIONS: Use should be carefully justified.",
        }

    async def _failing_local_completion(**kwargs):
        raise RuntimeError("lane_plan local-model transport failed: connect error")

    monkeypatch.setattr(
        research_router_module, "_resolve_runtime_script", lambda relative_path: relative_path
    )
    monkeypatch.setattr(research_router_module, "_run_runtime_script", _fake_runtime)
    monkeypatch.setattr(
        research_router_module, "_complete_local_model_json", _failing_local_completion
    )
    monkeypatch.setenv(research_router_module.LOCAL_MODEL_API_BASE_ENV, "http://127.0.0.1:8000/v1")
    result = await research_router_module.research_router(
        'deidentified: {"drugs": ["warfarin", "aspirin"]}'
    )
    assert result["runtime"]["local_model"]["available"] is True
    assert "Local planner fallback" in result["main_caveats"][0]
    assert result["evidence_by_lane"]


@pytest.mark.asyncio
async def test_research_router_proteomexchange_path(monkeypatch) -> None:
    async def _fake_runtime(script_path, payload):
        return {
            "ok": True,
            "summary": {
                "datasets": [
                    [
                        "PXD024902",
                        "Proteomic analysis of early salt stress response in root and shoot of rice seedlings",
                        "PRIDE",
                        "Oryza sativa",
                    ]
                ]
            },
        }

    monkeypatch.setattr(
        research_router_module, "_resolve_runtime_script", lambda relative_path: relative_path
    )
    monkeypatch.setattr(research_router_module, "_run_runtime_script", _fake_runtime)
    result = await research_router_module.research_router(
        'deidentified: {"accessions": ["PXD024902"]}'
    )
    assert result["runtime"]["downstream_runtime"] == "proteomexchange"
    assert result["evidence_by_lane"][0]["records"][0]["accession"] == "PXD024902"


@pytest.mark.asyncio
async def test_research_router_runtime_timeout_returns_structured_failure(monkeypatch) -> None:
    async def _timeout_runtime(script_path, payload):
        return {"ok": False, "error": {"message": "Runtime script timed out"}}

    monkeypatch.setattr(
        research_router_module, "_resolve_runtime_script", lambda relative_path: relative_path
    )
    monkeypatch.setattr(research_router_module, "_run_runtime_script", _timeout_runtime)
    result = await research_router_module.research_router(
        'deidentified: {"drugs": ["warfarin", "aspirin"]}'
    )
    assert result["evidence_by_lane"] == []
    assert (
        "No direct literature hits" in result["main_caveats"][0]
        or "timed out" in result["main_caveats"][0]
    )


@pytest.mark.asyncio
async def test_research_router_audit_logging(monkeypatch) -> None:
    async def _fake_runtime(script_path, payload):
        if payload["endpoint"] == "esearch":
            return {"ok": True, "records": ["26467380"]}
        if payload["endpoint"] == "esummary":
            return {
                "ok": True,
                "summary": {
                    "result": {
                        "uids": ["26467380"],
                        "26467380": {
                            "title": "Inappropriate combination of warfarin and aspirin",
                            "source": "Anatol J Cardiol",
                            "pubdate": "2016 Mar",
                        },
                    }
                },
            }
        return {
            "ok": True,
            "text_head": "OBJECTIVE: Combination therapy increases bleeding risk. CONCLUSIONS: Use should be carefully justified.",
        }

    persisted: list[int] = []
    monkeypatch.setattr(
        research_router_module, "_resolve_runtime_script", lambda relative_path: relative_path
    )
    monkeypatch.setattr(research_router_module, "_run_runtime_script", _fake_runtime)
    audit_log = AuditLog()
    result = await research_router_module.research_router(
        'deidentified: {"drugs": ["warfarin", "aspirin"]}',
        audit_log=audit_log,
        on_persist=lambda log: persisted.append(len(log)),
    )
    assert result["runtime"]["retrieval_runtime_available"] is True
    assert len(audit_log.entries) >= 2
    assert any(entry.type == "research_router_query" for entry in audit_log.entries)
    assert any(entry.type == "research_router_runtime_call" for entry in audit_log.entries)
    assert persisted
    runtime_entry = next(
        entry for entry in audit_log.entries if entry.type == "research_router_runtime_call"
    )
    assert "payload" not in runtime_entry.metadata
    assert "operation" in runtime_entry.metadata


@pytest.mark.asyncio
async def test_research_router_persistence_failure_raises(monkeypatch) -> None:
    async def _fake_runtime(script_path, payload):
        return {
            "ok": True,
            "summary": {
                "datasets": [
                    [
                        "PXD024902",
                        "Proteomic analysis of early salt stress response in root and shoot of rice seedlings",
                        "PRIDE",
                        "Oryza sativa",
                    ]
                ]
            },
        }

    monkeypatch.setattr(
        research_router_module, "_resolve_runtime_script", lambda relative_path: relative_path
    )
    monkeypatch.setattr(research_router_module, "_run_runtime_script", _fake_runtime)

    def failing_persist(log):
        raise OSError("disk full")

    audit_log = AuditLog()
    with pytest.raises(RuntimeError, match="Research-router audit persistence failed"):
        await research_router_module.research_router(
            'deidentified: {"accessions": ["PXD024902"]}',
            audit_log=audit_log,
            on_persist=failing_persist,
        )

    # Atomic audit guarantee: no partial batch state remains after persist failure.
    # A retry therefore would not duplicate audit history with different IDs.
    assert audit_log.entries == []
    assert audit_log.verify_chain()


@pytest.mark.asyncio
async def test_research_router_persistence_retry_does_not_duplicate(monkeypatch) -> None:
    """A retry after a transient persist failure must not leave orphan entries
    behind, so the second attempt's audit trail is not a duplicate of the first.
    """
    async def _fake_runtime(script_path, payload):
        return {
            "ok": True,
            "summary": {
                "datasets": [
                    ["PXD024902", "title", "PRIDE", "Oryza sativa"],
                ]
            },
        }

    monkeypatch.setattr(
        research_router_module, "_resolve_runtime_script", lambda relative_path: relative_path
    )
    monkeypatch.setattr(research_router_module, "_run_runtime_script", _fake_runtime)

    attempts = {"n": 0}

    def flaky_persist(_log):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("transient")

    audit_log = AuditLog()
    with pytest.raises(RuntimeError):
        await research_router_module.research_router(
            'deidentified: {"accessions": ["PXD024902"]}',
            audit_log=audit_log,
            on_persist=flaky_persist,
        )
    assert audit_log.entries == []

    # Second attempt should succeed and produce a single clean audit trail —
    # not a duplicate of the failed first attempt.
    await research_router_module.research_router(
        'deidentified: {"accessions": ["PXD024902"]}',
        audit_log=audit_log,
        on_persist=flaky_persist,
    )
    ids = [e.id for e in audit_log.entries]
    assert len(ids) == len(set(ids))
    assert audit_log.verify_chain()


def test_parse_deidentified_entities_requires_json_payload() -> None:
    assert research_router_module._parse_deidentified_entities("deidentified: not-json") is None
    entities = research_router_module._parse_deidentified_entities(
        'deidentified: {"genes": ["EGFR"], "drugs": ["osimertinib"], "diseases": ["NSCLC"]}'
    )
    assert entities == {
        "genes": ["EGFR"],
        "drugs": ["osimertinib"],
        "diseases": ["NSCLC"],
        "accessions": [],
    }
