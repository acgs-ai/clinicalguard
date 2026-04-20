"""ClinicalGuard: research_router skill.

Routes broad life-science questions through installed retrieval runtimes and a
local model-first summarization/planning layer.

Constitutional Hash: derived from bundled healthcare_v1.yaml
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, TypeVar
from urllib.parse import urlparse

import httpx

from acgs_lite.audit import AuditEntry, AuditLog
from clinicalguard import CONSTITUTIONAL_HASH

logger = logging.getLogger(__name__)

PLUGIN_ROOT_ENV = "CLINICALGUARD_LIFE_SCIENCE_RUNTIME_ROOT"
REQUIRE_RUNTIME_ENV = "CLINICALGUARD_REQUIRE_LIFE_SCIENCE_RUNTIME"
LOCAL_MODEL_ENABLED_ENV = "CLINICALGUARD_LOCAL_MODEL_ENABLED"
LOCAL_MODEL_API_BASE_ENV = "CLINICALGUARD_LOCAL_MODEL_API_BASE"
LOCAL_MODEL_API_KEY_ENV = "CLINICALGUARD_LOCAL_MODEL_API_KEY"
LOCAL_MODEL_NAME_ENV = "CLINICALGUARD_LOCAL_MODEL_NAME"
LOCAL_MODEL_TIMEOUT_ENV = "CLINICALGUARD_LOCAL_MODEL_TIMEOUT_SEC"
REQUIRE_LOCAL_MODEL_ENV = "CLINICALGUARD_REQUIRE_LOCAL_MODEL"
LOCAL_MODEL_PROVIDER = "local-openai-compatible"
DEIDENTIFIED_PREFIXES = ("deidentified:", "de-identified:")

PLUGIN_CACHE_ROOT = (
    Path.home() / ".codex" / "plugins" / "cache" / "openai-curated" / "life-science-research"
)
NCBI_SCRIPT_RELATIVE = Path("skills/ncbi-entrez-skill/scripts/ncbi_entrez.py")
PROXI_SCRIPT_RELATIVE = Path("skills/proteomexchange-skill/scripts/rest_request.py")
PROXI_BASE_URL = "https://proteomecentral.proteomexchange.org/api/proxi/v0.1"
DEFAULT_LOCAL_MODEL_NAME = "clinicalguard-qwen35-4b"
MAX_PMIDS_PER_LANE = 3
MAX_LANES = 3

DRUG_HINTS = {
    "adalimumab",
    "aspirin",
    "clopidogrel",
    "erlotinib",
    "insulin",
    "methotrexate",
    "metformin",
    "osimertinib",
    "warfarin",
}
DRUG_SUFFIXES = ("mab", "nib", "parib", "ciclib", "statin", "xaban", "pril", "sartan")
DISEASE_PATTERNS = (
    r"\bnsclc\b",
    r"\bcancer\b",
    r"\bdisease\b",
    r"\bsyndrome\b",
    r"\bdiabetes\b",
    r"\bstroke\b",
    r"\barthritis\b",
    r"\binfection\b",
    r"\btumou?r\b",
    r"\baf\b",
)
GENE_STOPWORDS = {
    "A2A",
    "DNA",
    "RNA",
    "JSON",
    "HTTP",
    "HIPAA",
    "PMID",
    "PROXI",
    "PXD",
    "TLS",
    "USA",
}
ABSTRACT_LABELS = (
    "ABSTRACT",
    "BACKGROUND:",
    "OBJECTIVE:",
    "INTRODUCTION:",
    "PURPOSE:",
    "METHODS:",
    "RESULTS:",
    "CONCLUSIONS:",
)
PHI_EGRESS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("medical_record_number", re.compile(r"\b(?:MRN|Medical Record)\s*[#:]?\s*\d{5,}\b", re.I)),
    (
        "date_of_birth",
        re.compile(
            r"\b(?:DOB|date of birth|born)\s*[:\-]?\s*\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b", re.I
        ),
    ),
    ("email", re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b")),
    ("phone", re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")),
    (
        "patient_identifier",
        re.compile(
            r"\b(?:patient|member|subscriber)\s*(?:id|number|#)?\s*[:\-]?\s*[A-Z0-9]{4,}\b", re.I
        ),
    ),
)
GENE_ENTITY_PATTERN = re.compile(r"^(?=.{2,20}$)(?=.*[A-Z])[A-Z0-9-]+$")
DRUG_ENTITY_PATTERN = re.compile(r"^(?=.{2,40}$)[A-Za-z0-9][A-Za-z0-9+\-]{1,39}$")
DISEASE_ENTITY_PATTERN = re.compile(
    r"^(?=.{2,30}$)(?:[A-Z0-9]+(?:-[A-Z0-9]+)*|(?:MONDO|EFO|DOID|SNOMED|MeSH):[A-Z0-9._-]+)$"
)
ACCESSION_ENTITY_PATTERN = re.compile(r"^PXD\d{6}$")


@dataclass(frozen=True)
class RuntimeCall:
    runtime: str
    operation: str
    request: dict[str, Any]
    audit_payload: dict[str, Any]


@dataclass(frozen=True)
class LaneQuery:
    lane: str
    runtime: str
    query: str


@dataclass(frozen=True)
class LaneCandidate:
    lane: str
    query: str


@dataclass(frozen=True)
class LanePlanResponse:
    direct_answer: str = ""
    lanes: list[LaneCandidate] = field(default_factory=list)


@dataclass(frozen=True)
class AbstractSummaryResponse:
    summary: str


T = TypeVar("T")


def validate_runtime_startup() -> dict[str, Any]:
    """Validate configured runtime requirements at startup."""
    status = runtime_status()
    explicit_root = os.environ.get(PLUGIN_ROOT_ENV)
    require_runtime = os.environ.get(REQUIRE_RUNTIME_ENV, "").lower() in {"1", "true", "yes"}
    require_local_model = os.environ.get(REQUIRE_LOCAL_MODEL_ENV, "").lower() in {
        "1",
        "true",
        "yes",
    }

    if explicit_root and not status["retrieval_runtime"]["available"]:
        raise RuntimeError(status["retrieval_runtime"]["summary"])
    if require_runtime and not status["retrieval_runtime"]["available"]:
        raise RuntimeError(status["retrieval_runtime"]["summary"])
    if require_local_model and not status["local_model"]["available"]:
        raise RuntimeError(status["local_model"]["summary"])
    return status


def runtime_status() -> dict[str, Any]:
    scripts: dict[str, str] = {}
    missing: list[str] = []
    for label, relative in {
        "ncbi_entrez": NCBI_SCRIPT_RELATIVE,
        "proteomexchange": PROXI_SCRIPT_RELATIVE,
    }.items():
        try:
            scripts[label] = str(_resolve_runtime_script(relative))
        except FileNotFoundError as exc:
            missing.append(str(exc))

    retrieval_available = not missing
    retrieval_summary = (
        "Life-science retrieval runtime available."
        if retrieval_available
        else f"Life-science retrieval runtime unavailable; set {PLUGIN_ROOT_ENV} or install the plugin cache."
    )

    local_model = _local_model_status()
    return {
        "retrieval_runtime": {
            "available": retrieval_available,
            "scripts": scripts,
            "missing": missing,
            "summary": retrieval_summary,
        },
        "local_model": local_model,
        "summary": " | ".join([retrieval_summary, local_model["summary"]]),
    }


def _local_model_status() -> dict[str, Any]:
    enabled = os.environ.get(LOCAL_MODEL_ENABLED_ENV, "true").lower() not in {"0", "false", "no"}
    api_base = os.environ.get(LOCAL_MODEL_API_BASE_ENV, "").strip()
    model_name = os.environ.get(LOCAL_MODEL_NAME_ENV, DEFAULT_LOCAL_MODEL_NAME).strip()
    local_endpoint_safe = _is_local_model_endpoint_safe(api_base)
    available = enabled and bool(api_base and model_name and local_endpoint_safe)
    if not enabled:
        summary = "Local model disabled; heuristic planning/summarization will be used."
    elif available:
        summary = f"Local model configured via {LOCAL_MODEL_PROVIDER} at {api_base}."
    elif api_base and not local_endpoint_safe:
        summary = (
            "Local model endpoint rejected because it is not local/private. "
            "ClinicalGuard only permits local/private model endpoints on the active runtime path."
        )
    else:
        summary = (
            f"Local model not configured; set {LOCAL_MODEL_API_BASE_ENV} to enable model-first "
            "research routing."
        )
    return {
        "enabled": enabled,
        "available": available,
        "provider": LOCAL_MODEL_PROVIDER,
        "api_base": api_base,
        "model_name": model_name,
        "timeout_sec": int(os.environ.get(LOCAL_MODEL_TIMEOUT_ENV, "30")),
        "summary": summary,
    }


async def research_router(
    question: str,
    *,
    audit_log: AuditLog | None = None,
    on_persist: Any = None,
) -> dict[str, Any]:
    """Route a life-science research question through retrieval + local-model planning."""
    question = question.strip()
    if not question:
        return {
            "error": "Research question is required",
            "hint": 'Use: research_router: deidentified: {"genes": [...], "drugs": [...], "diseases": [...], "accessions": [...]}',
        }
    entities = _parse_deidentified_entities(question)
    if entities is None:
        result = {
            "direct_answer": "ClinicalGuard blocked this research routing request because it did not provide explicitly de-identified structured entities.",
            "evidence_by_lane": [],
            "main_caveats": [
                "Research routing is fail-closed and only accepts JSON entity payloads prefixed with `deidentified:` or `de-identified:`."
            ],
            "recommended_next_steps": [
                "Retry with explicit de-identified biomedical entities only",
                'Example: `research_router: deidentified: {"genes": ["EGFR"], "drugs": ["osimertinib"], "diseases": ["NSCLC"]}`',
            ],
            "runtime": {
                "retrieval_runtime_available": False,
                "retrieval_runtime_summary": "Request blocked before retrieval.",
                "local_model": _local_model_status(),
                "router": "life-science-research:research-router-skill",
                "lanes": [],
                "downstream_runtime": None,
            },
            "sensitivity_findings": [],
        }
        await _record_research_audit_entries(
            question="",
            plan={"lanes": [], "entities": {}, "runtime": "blocked"},
            result=result,
            runtime_calls=[],
            audit_log=audit_log,
            on_persist=on_persist,
        )
        return result

    entity_issues = _validate_deidentified_entities(entities)
    if entity_issues:
        result = {
            "direct_answer": "ClinicalGuard blocked this research routing request because the entity payload did not satisfy the de-identified contract.",
            "evidence_by_lane": [],
            "main_caveats": [
                "Research routing accepts only compact biomedical entity codes or identifiers, not free-text narrative.",
            ],
            "recommended_next_steps": [
                "Rewrite the payload with biomedical symbols, accession identifiers, or ontology-style codes only",
                "Do not include patient names, locations, dates, or narrative context",
            ],
            "runtime": {
                "retrieval_runtime_available": False,
                "retrieval_runtime_summary": "Request blocked before retrieval.",
                "local_model": _local_model_status(),
                "router": "life-science-research:research-router-skill",
                "lanes": [],
                "downstream_runtime": None,
            },
            "sensitivity_findings": entity_issues,
        }
        await _record_research_audit_entries(
            question="",
            plan={"lanes": [], "entities": {}, "runtime": "blocked"},
            result=result,
            runtime_calls=[],
            audit_log=audit_log,
            on_persist=on_persist,
        )
        return result

    combined_entities = " ".join(
        entities["genes"] + entities["drugs"] + entities["diseases"] + entities["accessions"]
    )
    sensitivity_findings = _detect_sensitive_research_input(combined_entities)
    if sensitivity_findings:
        result = {
            "direct_answer": "ClinicalGuard blocked this research routing request because it appears to contain sensitive identifiers.",
            "evidence_by_lane": [],
            "main_caveats": [
                "Research routing requires de-identified prompts before any local-model planning or external literature retrieval."
            ],
            "recommended_next_steps": [
                "Remove patient identifiers, direct contact information, and other PHI from the prompt",
                "Retry with a de-identified biomedical question",
            ],
            "runtime": {
                "retrieval_runtime_available": False,
                "retrieval_runtime_summary": "Request blocked before retrieval.",
                "local_model": _local_model_status(),
                "router": "life-science-research:research-router-skill",
                "lanes": [],
                "downstream_runtime": None,
            },
            "sensitivity_findings": sensitivity_findings,
        }
        await _record_research_audit_entries(
            question="",
            plan={"lanes": [], "entities": {}, "runtime": "blocked"},
            result=result,
            runtime_calls=[],
            audit_log=audit_log,
            on_persist=on_persist,
        )
        return result

    status = runtime_status()
    plan = _build_base_plan(entities)
    runtime_calls: list[dict[str, Any]] = []
    caveats: list[str] = []

    if plan["runtime"] == "pubmed" and not plan["lane_queries"]:
        result = {
            "direct_answer": "ClinicalGuard could not derive a de-identified biomedical query from this request.",
            "evidence_by_lane": [],
            "main_caveats": [
                "Research routing only proceeds on de-identified gene, drug, disease, or accession-style prompts."
            ],
            "recommended_next_steps": [
                "Retry with explicit biomedical entities such as a gene, drug, disease, or dataset accession",
                "Remove any patient-specific narrative and rephrase the question as a literature query",
            ],
        }
        result["runtime"] = {
            "retrieval_runtime_available": status["retrieval_runtime"]["available"],
            "retrieval_runtime_summary": "Request blocked before retrieval because no safe entity-derived query was available.",
            "local_model": status["local_model"],
            "router": "life-science-research:research-router-skill",
            "lanes": [],
            "downstream_runtime": None,
        }
        await _record_research_audit_entries(
            question="",
            plan={"lanes": [], "entities": {}, "runtime": "blocked"},
            result=result,
            runtime_calls=[],
            audit_log=audit_log,
            on_persist=on_persist,
        )
        return result

    if plan["runtime"] == "pubmed" and status["local_model"]["available"]:
        try:
            plan = await _refine_plan_with_local_model(
                plan,
                status["local_model"],
                runtime_calls,
            )
        except RuntimeError as exc:
            caveats.append(f"Local planner fallback: {exc}")

    try:
        if plan["runtime"] == "proteomexchange":
            result = await _run_proteomexchange_plan(plan, runtime_calls)
        else:
            result = await _run_pubmed_plan(plan, runtime_calls, status["local_model"])
    except FileNotFoundError as exc:
        result = {
            "direct_answer": "Life-science retrieval runtime is not available in this deployment.",
            "evidence_by_lane": [],
            "main_caveats": [str(exc)],
            "recommended_next_steps": [
                f"Set {PLUGIN_ROOT_ENV} to the installed Life Science plugin runtime root",
                "Retry the same question once the runtime scripts are present",
            ],
        }
    else:
        existing_caveats = result.get("main_caveats", [])
        if not isinstance(existing_caveats, list):
            existing_caveats = [str(existing_caveats)]
        result["main_caveats"] = [*caveats, *(str(item) for item in existing_caveats)]

    result["runtime"] = {
        "retrieval_runtime_available": status["retrieval_runtime"]["available"],
        "retrieval_runtime_summary": status["retrieval_runtime"]["summary"],
        "local_model": status["local_model"],
        "router": "life-science-research:research-router-skill",
        "lanes": plan["lanes"],
        "downstream_runtime": plan["runtime"],
    }

    await _record_research_audit_entries(
        question="",
        plan=plan,
        result=result,
        runtime_calls=runtime_calls,
        audit_log=audit_log,
        on_persist=on_persist,
    )
    return result


def _build_base_plan(entities: dict[str, list[str]]) -> dict[str, Any]:
    if entities["accessions"]:
        return {
            "runtime": "proteomexchange",
            "lanes": ["proteomics_dataset_context"],
            "entities": entities,
            "lane_queries": [],
        }

    lane_queries = _build_pubmed_lane_queries(entities)
    return {
        "runtime": "pubmed",
        "lanes": [lane.lane for lane in lane_queries],
        "entities": entities,
        "lane_queries": lane_queries,
    }


def _parse_deidentified_entities(question: str) -> dict[str, list[str]] | None:
    payload_text = _extract_deidentified_question(question)
    if payload_text is None:
        return None
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    entities: dict[str, list[str]] = {"genes": [], "drugs": [], "diseases": [], "accessions": []}
    for key in entities:
        value = payload.get(key, [])
        if value is None:
            value = []
        if not isinstance(value, list):
            return None
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                return None
            cleaned = item.strip()
            if cleaned:
                normalized.append(cleaned)
        entities[key] = normalized
    if not any(entities.values()):
        return None
    return entities


def _validate_deidentified_entities(entities: dict[str, list[str]]) -> list[str]:
    issues: list[str] = []
    for entity_type, values in entities.items():
        for raw_value in values:
            value = raw_value.strip()
            if not value:
                issues.append(f"{entity_type}:empty")
                continue
            if len(value) > 64:
                issues.append(f"{entity_type}:too_long")
                continue
            if _detect_sensitive_research_input(value):
                issues.extend(_detect_sensitive_research_input(value))
                continue
            if entity_type == "genes":
                if not GENE_ENTITY_PATTERN.match(value) or value in GENE_STOPWORDS:
                    issues.append(f"{entity_type}:invalid_symbol")
            elif entity_type == "drugs":
                if not DRUG_ENTITY_PATTERN.match(value) or " " in value:
                    issues.append(f"{entity_type}:invalid_token")
            elif entity_type == "diseases":
                if not DISEASE_ENTITY_PATTERN.match(value) or " " in value:
                    issues.append(f"{entity_type}:invalid_code")
            elif entity_type == "accessions":
                if not ACCESSION_ENTITY_PATTERN.match(value):
                    issues.append(f"{entity_type}:invalid_accession")
            else:
                issues.append(f"{entity_type}:unsupported_field")
    return issues


def _build_pubmed_lane_queries(entities: dict[str, list[str]]) -> list[LaneQuery]:
    gene_focus = " ".join(entities["genes"][:2])
    disease_focus = " ".join(entities["diseases"][:2])
    drug_focus = " ".join(entities["drugs"][:2])
    discovery_focus = " ".join(part for part in [gene_focus, disease_focus, drug_focus] if part)
    lanes: list[LaneQuery] = []

    if discovery_focus:
        lanes.append(
            LaneQuery(
                lane="literature_discovery",
                runtime="pubmed",
                query=" ".join(part for part in [discovery_focus, "clinical evidence"] if part),
            )
        )

    if entities["genes"] and (entities["diseases"] or entities["drugs"]):
        lanes.append(
            LaneQuery(
                lane="human_genetics_and_variant_interpretation",
                runtime="pubmed",
                query=" ".join(
                    part
                    for part in [
                        gene_focus,
                        disease_focus or drug_focus,
                        "mutation biomarker mechanism",
                    ]
                    if part
                ),
            )
        )
    if entities["drugs"]:
        lanes.append(
            LaneQuery(
                lane="chemistry_ligands_and_pharmacology",
                runtime="pubmed",
                query=" ".join(
                    part
                    for part in [drug_focus, gene_focus or disease_focus, "pharmacology mechanism"]
                    if part
                ),
            )
        )
    if entities["diseases"] or entities["drugs"]:
        lanes.append(
            LaneQuery(
                lane="clinical_and_translational_evidence",
                runtime="pubmed",
                query=" ".join(
                    part
                    for part in [
                        disease_focus or discovery_focus,
                        drug_focus,
                        "clinical trial outcome",
                    ]
                    if part
                ),
            )
        )

    deduped: list[LaneQuery] = []
    seen: set[tuple[str, str]] = set()
    for lane in lanes:
        key = (lane.lane, lane.query.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(lane)
    return deduped[:MAX_LANES]


def _is_local_model_endpoint_safe(api_base: str) -> bool:
    if not api_base:
        return False
    parsed = urlparse(api_base)
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback


def _extract_deidentified_question(question: str) -> str | None:
    stripped = question.strip()
    lowered = stripped.lower()
    for prefix in DEIDENTIFIED_PREFIXES:
        if lowered.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return None


def _detect_sensitive_research_input(question: str) -> list[str]:
    findings: list[str] = []
    for label, pattern in PHI_EGRESS_PATTERNS:
        if pattern.search(question):
            findings.append(label)
    return findings


async def _refine_plan_with_local_model(
    plan: dict[str, Any],
    local_model_status: dict[str, Any],
    runtime_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    lane_summary = [{"lane": lane.lane, "query": lane.query} for lane in plan["lane_queries"]]
    prompt = (
        "You are planning a narrow biomedical literature routing step for a governed healthcare service. "
        f"Entities: {json.dumps(plan['entities'])}\n"
        f"Initial lanes: {json.dumps(lane_summary)}\n"
        f"Return JSON with keys direct_answer and lanes. Keep at most {MAX_LANES} lanes. "
        "Each lane item must include lane and query. Prefer these lane names when relevant: "
        "literature_discovery, human_genetics_and_variant_interpretation, "
        "chemistry_ligands_and_pharmacology, clinical_and_translational_evidence, "
        "proteomics_dataset_context."
    )
    response = await _complete_local_model_json(
        prompt=prompt,
        schema_name="lane_plan",
        parser=_parse_lane_plan_response,
        local_model_status=local_model_status,
        runtime_calls=runtime_calls,
        operation="local_model:lane_plan",
    )
    lanes = [
        LaneQuery(lane=item.lane, runtime="pubmed", query=item.query)
        for item in response.lanes
        if item.lane and item.query
    ][:MAX_LANES]
    if not lanes:
        raise RuntimeError("local planner returned no usable lane plan")
    return {
        **plan,
        "lanes": [lane.lane for lane in lanes],
        "lane_queries": lanes,
        "direct_answer": response.direct_answer,
    }


def _resolve_runtime_script(relative_path: Path) -> Path:
    candidates: list[Path] = []
    explicit_root = os.environ.get(PLUGIN_ROOT_ENV)
    if explicit_root:
        candidates.append(Path(explicit_root) / relative_path)

    if PLUGIN_CACHE_ROOT.exists():
        plugin_roots = sorted(
            [p for p in PLUGIN_CACHE_ROOT.iterdir() if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        candidates.extend(root / relative_path for root in plugin_roots)

    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        f"Could not resolve runtime script {relative_path} via {PLUGIN_ROOT_ENV} or {PLUGIN_CACHE_ROOT}"
    )


async def _run_runtime_script(script_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return {"ok": False, "error": {"message": f"Could not start runtime script: {exc}"}}
    stdin = json.dumps(payload).encode("utf-8")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(stdin), timeout=45.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": {"message": "Runtime script timed out"}}
    if not stdout:
        message = (
            stderr.decode("utf-8", errors="replace").strip() or "No output from runtime script"
        )
        return {"ok": False, "error": {"message": message}}
    try:
        result = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": {"message": f"Could not parse runtime output: {exc}"}}
    if not isinstance(result, dict):
        return {"ok": False, "error": {"message": "Runtime output was not a JSON object"}}
    if proc.returncode != 0 and result.get("ok", True):
        message = stderr.decode("utf-8", errors="replace").strip() or "Runtime script failed"
        return {"ok": False, "error": {"message": message}}
    return result


async def _run_and_trace(
    *,
    script_path: Path,
    call: RuntimeCall,
    runtime_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    started = perf_counter()
    result = await _run_runtime_script(script_path, call.request)
    runtime_calls.append(
        {
            "runtime": call.runtime,
            "operation": call.operation,
            "latency_ms": round((perf_counter() - started) * 1000, 3),
            "payload": call.audit_payload,
            "ok": bool(result.get("ok", False)),
            "error": result.get("error", {}).get("message")
            if not result.get("ok", False)
            else None,
        }
    )
    return result


async def _complete_local_model_json(
    *,
    prompt: str,
    schema_name: str,
    parser: Callable[[dict[str, Any]], T],
    local_model_status: dict[str, Any],
    runtime_calls: list[dict[str, Any]],
    operation: str,
) -> T:
    started = perf_counter()
    api_base = local_model_status["api_base"].rstrip("/")
    model_name = local_model_status["model_name"]
    timeout_sec = local_model_status["timeout_sec"]
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get(LOCAL_MODEL_API_KEY_ENV, "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model_name,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are the local ClinicalGuard research model. Return only a JSON object "
                    "that matches the requested schema. Do not include markdown fences."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }

    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        try:
            response = await client.post(
                f"{api_base}/chat/completions", headers=headers, json=payload
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"{schema_name} local-model transport failed: {exc}") from exc
    data = response.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    parsed = _extract_json_object(content)
    model_response = parser(parsed)

    runtime_calls.append(
        {
            "runtime": "local_model",
            "operation": operation,
            "latency_ms": round((perf_counter() - started) * 1000, 3),
            "payload": {
                "model": model_name,
                "schema": schema_name,
            },
            "ok": True,
            "error": None,
        }
    )
    return model_response


def _extract_json_object(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        parts = content.split("```")
        content = next((part for part in parts if "{" in part), content)
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError("local model did not return a JSON object")
    data = json.loads(content[start : end + 1])
    if not isinstance(data, dict):
        raise RuntimeError("local model returned non-object JSON")
    return data


def _parse_lane_plan_response(data: dict[str, Any]) -> LanePlanResponse:
    direct_answer = data.get("direct_answer", "")
    if not isinstance(direct_answer, str):
        raise RuntimeError("lane_plan direct_answer must be a string")
    raw_lanes = data.get("lanes", [])
    if raw_lanes is None:
        raw_lanes = []
    if not isinstance(raw_lanes, list):
        raise RuntimeError("lane_plan lanes must be a list")
    lanes: list[LaneCandidate] = []
    for item in raw_lanes[:MAX_LANES]:
        if not isinstance(item, dict):
            continue
        lane = item.get("lane", "")
        query = item.get("query", "")
        if isinstance(lane, str) and isinstance(query, str) and lane.strip() and query.strip():
            lanes.append(LaneCandidate(lane=lane.strip(), query=query.strip()))
    return LanePlanResponse(direct_answer=direct_answer, lanes=lanes)


def _parse_abstract_summary_response(data: dict[str, Any]) -> AbstractSummaryResponse:
    summary = data.get("summary", "")
    if not isinstance(summary, str) or not summary.strip():
        raise RuntimeError("abstract_summary summary must be a non-empty string")
    return AbstractSummaryResponse(summary=summary.strip())


async def _run_pubmed_plan(
    plan: dict[str, Any],
    runtime_calls: list[dict[str, Any]],
    local_model_status: dict[str, Any],
) -> dict[str, Any]:
    script = _resolve_runtime_script(NCBI_SCRIPT_RELATIVE)
    lane_outputs = await asyncio.gather(
        *[
            _run_pubmed_lane(script, lane_query, runtime_calls, local_model_status)
            for lane_query in plan["lane_queries"]
        ]
    )
    evidence_by_lane = [output for output in lane_outputs if output["records"]]
    total_records = sum(len(output["records"]) for output in evidence_by_lane)
    if not total_records:
        return {
            "direct_answer": "No PubMed records matched the query closely enough for a concise abstract-backed summary.",
            "evidence_by_lane": [],
            "main_caveats": [
                "No direct literature hits were returned from PubMed for the selected lanes."
            ],
            "recommended_next_steps": [
                "Retry with a narrower disease, drug, gene, or phenotype term",
                "Add a mechanism, mutation, or assay keyword to sharpen the search",
            ],
        }

    lane_names = ", ".join(output["lane"] for output in evidence_by_lane)
    base_answer = plan.get("direct_answer") or (
        f"ClinicalGuard found {total_records} PubMed-backed records across {len(evidence_by_lane)} lane(s): {lane_names}."
    )
    return {
        "direct_answer": (
            f"{base_answer} The summaries below come from deterministic retrieval plus "
            "local-model or heuristic synthesis, and should be treated as evidence triage, not medical advice."
        ),
        "evidence_by_lane": evidence_by_lane,
        "main_caveats": [
            "Summaries are for evidence triage, not final clinical guidance.",
            "Association-level literature should not be treated as causal without deeper review.",
        ],
        "recommended_next_steps": [
            "Review the cited PMIDs directly for methods, cohort, and statistical details",
            "Narrow the question to one gene, drug, or disease if you want higher-specificity routing",
        ],
    }


async def _run_pubmed_lane(
    script: Path,
    lane_query: LaneQuery,
    runtime_calls: list[dict[str, Any]],
    local_model_status: dict[str, Any],
) -> dict[str, Any]:
    search = await _run_and_trace(
        script_path=script,
        call=RuntimeCall(
            runtime="pubmed",
            operation=f"{lane_query.lane}:esearch",
            request={
                "endpoint": "esearch",
                "params": {
                    "db": "pubmed",
                    "term": lane_query.query,
                    "retmode": "json",
                    "retmax": MAX_PMIDS_PER_LANE,
                    "sort": "relevance",
                },
                "max_items": MAX_PMIDS_PER_LANE,
            },
            audit_payload={
                "endpoint": "esearch",
                "lane": lane_query.lane,
                "retmax": MAX_PMIDS_PER_LANE,
            },
        ),
        runtime_calls=runtime_calls,
    )
    if not search.get("ok"):
        return {
            "lane": lane_query.lane,
            "source": "ncbi-entrez",
            "records": [],
            "caveat": search.get("error", {}).get("message", "PubMed search failed"),
        }

    pmids = [str(pmid) for pmid in search.get("records", [])[:MAX_PMIDS_PER_LANE]]
    if not pmids:
        return {"lane": lane_query.lane, "source": "ncbi-entrez", "records": []}

    summary = await _run_and_trace(
        script_path=script,
        call=RuntimeCall(
            runtime="pubmed",
            operation=f"{lane_query.lane}:esummary",
            request={
                "endpoint": "esummary",
                "params": {"db": "pubmed", "id": ",".join(pmids), "retmode": "json"},
                "max_items": 10,
            },
            audit_payload={
                "endpoint": "esummary",
                "pmid_count": len(pmids),
            },
        ),
        runtime_calls=runtime_calls,
    )
    summary_result = summary.get("summary", {}).get("result", {})

    records: list[dict[str, str]] = []
    for pmid in pmids:
        abstract = await _run_and_trace(
            script_path=script,
            call=RuntimeCall(
                runtime="pubmed",
                operation=f"{lane_query.lane}:efetch_abstract",
                request={
                    "endpoint": "efetch",
                    "params": {
                        "db": "pubmed",
                        "id": pmid,
                        "retmode": "text",
                        "rettype": "abstract",
                    },
                    "response_format": "text",
                },
                audit_payload={
                    "endpoint": "efetch",
                    "pmid": pmid,
                },
            ),
            runtime_calls=runtime_calls,
        )
        record = summary_result.get(pmid, {})
        title = str(record.get("title", "")) if isinstance(record, dict) else ""
        journal = str(record.get("source", "")) if isinstance(record, dict) else ""
        pubdate = str(record.get("pubdate", "")) if isinstance(record, dict) else ""
        abstract_text = str(abstract.get("text_head") or "")
        records.append(
            {
                "pmid": pmid,
                "title": title,
                "journal": journal,
                "pubdate": pubdate,
                "query": lane_query.query,
                "abstract_summary": await _summarize_abstract_text(
                    question=lane_query.query,
                    title=title,
                    journal=journal,
                    abstract_text=abstract_text,
                    local_model_status=local_model_status,
                    runtime_calls=runtime_calls,
                ),
            }
        )

    return {
        "lane": lane_query.lane,
        "source": "ncbi-entrez",
        "records": records,
    }


async def _summarize_abstract_text(
    *,
    question: str,
    title: str,
    journal: str,
    abstract_text: str,
    local_model_status: dict[str, Any],
    runtime_calls: list[dict[str, Any]],
) -> str:
    if local_model_status["available"] and abstract_text:
        prompt = (
            "Summarize this biomedical abstract snippet for a governed healthcare research router. "
            "Return one concise evidence sentence focused on what the paper adds for the question.\n"
            f"Question: {question}\n"
            f"Title: {title}\n"
            f"Journal: {journal}\n"
            f"Abstract snippet: {abstract_text}\n"
            "Return JSON with a single key named summary."
        )
        try:
            response = await _complete_local_model_json(
                prompt=prompt,
                schema_name="abstract_summary",
                parser=_parse_abstract_summary_response,
                local_model_status=local_model_status,
                runtime_calls=runtime_calls,
                operation="local_model:abstract_summary",
            )
        except RuntimeError as exc:
            logger.warning("Local abstract summarizer fallback: %s", exc)
        else:
            return response.summary

    cleaned = abstract_text.replace("\n", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return ""

    start_index = 0
    for label in ABSTRACT_LABELS:
        label_index = cleaned.find(label)
        if label_index != -1:
            start_index = label_index
            break
    cleaned = cleaned[start_index:]
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    selected: list[str] = []
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        selected.append(sentence)
        if len(selected) == 2:
            break
    summary = " ".join(selected)
    return summary[:480] + ("..." if len(summary) > 480 else "")


async def _run_proteomexchange_plan(
    plan: dict[str, Any],
    runtime_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    script = _resolve_runtime_script(PROXI_SCRIPT_RELATIVE)
    accessions = plan["entities"].get("accessions", [])
    if not accessions:
        return _runtime_failure(
            "ProteomeXchange routing requires an explicit accession in the de-identified entity payload."
        )
    path = f"datasets/{accessions[0].upper()}"

    result = await _run_and_trace(
        script_path=script,
        call=RuntimeCall(
            runtime="proteomexchange",
            operation="proxi_lookup",
            request={
                "base_url": PROXI_BASE_URL,
                "path": path,
                "max_items": 5,
            },
            audit_payload={
                "base_url": PROXI_BASE_URL,
                "path": path,
            },
        ),
        runtime_calls=runtime_calls,
    )
    if not result.get("ok"):
        message = result.get("error", {}).get("message", "ProteomeXchange lookup failed")
        return _runtime_failure(message)

    records = _extract_proteomexchange_items(result)
    return {
        "direct_answer": (
            f"ClinicalGuard routed this to ProteomeXchange runtime lookup and found {len(records)} dataset-level record(s)."
        ),
        "evidence_by_lane": [
            {
                "lane": "proteomics_dataset_context",
                "source": "proteomexchange-proxi",
                "records": records,
            }
        ],
        "main_caveats": [
            "This is dataset discovery metadata, not peptide-spectrum or quantitative reanalysis.",
            "ProteomeXchange summaries need direct follow-up for assay design and downstream biological interpretation.",
        ],
        "recommended_next_steps": [
            "Query a specific PXD accession for deeper metadata if available",
            "Use a narrower protein, disease, or assay term if you need more targeted proteomics context",
        ],
    }


def _extract_proteomexchange_items(result: dict[str, Any]) -> list[dict[str, str]]:
    datasets = result.get("summary", {}).get("datasets", [])
    items: list[dict[str, str]] = []
    for row in datasets:
        if not isinstance(row, list) or len(row) < 4:
            continue
        items.append(
            {
                "accession": str(row[0]),
                "title": str(row[1]),
                "repository": str(row[2]),
                "species": str(row[3]),
            }
        )
    if items:
        return items

    summary = result.get("summary", {})
    accession = summary.get("id") or summary.get("dataset_id") or result.get("path", "")
    if isinstance(summary, dict) and accession:
        return [
            {
                "accession": str(accession),
                "title": str(summary.get("title", "")),
                "repository": str(summary.get("repository", "")),
                "species": str(summary.get("species", "")),
            }
        ]
    return []


async def _record_research_audit_entries(
    *,
    question: str,
    plan: dict[str, Any],
    result: dict[str, Any],
    runtime_calls: list[dict[str, Any]],
    audit_log: AuditLog | None,
    on_persist: Any,
) -> None:
    if audit_log is None:
        return

    timestamp = datetime.now(UTC)
    router_audit_id = f"HC-RESEARCH-{timestamp.strftime('%Y%m%d')}-{uuid.uuid4().hex[:10].upper()}"
    query_entry = AuditEntry(
        id=router_audit_id,
        type="research_router_query",
        agent_id="research_router",
        action="research_router request",
        valid=bool(result.get("runtime", {}).get("retrieval_runtime_available", False)),
        violations=[],
        constitutional_hash=CONSTITUTIONAL_HASH,
        latency_ms=0.0,
        metadata={
            "downstream_runtime": result.get("runtime", {}).get("downstream_runtime"),
            "local_model_available": result.get("runtime", {})
            .get("local_model", {})
            .get("available", False),
            "record_count": sum(
                len(lane.get("records", [])) for lane in result.get("evidence_by_lane", [])
            ),
            "runtime_calls": len(runtime_calls),
            "sensitivity_findings": result.get("sensitivity_findings", []),
        },
    )

    batch: list[AuditEntry] = [query_entry]
    for call in runtime_calls:
        entry = AuditEntry(
            id=f"HC-RTCALL-{timestamp.strftime('%Y%m%d')}-{uuid.uuid4().hex[:10].upper()}",
            type="research_router_runtime_call",
            agent_id=call["runtime"],
            action=str(call["operation"])[:500],
            valid=bool(call["ok"]),
            violations=[],
            constitutional_hash=CONSTITUTIONAL_HASH,
            latency_ms=float(call["latency_ms"]),
            metadata={
                "operation": call["operation"],
                "error": call["error"],
            },
        )
        batch.append(entry)

    try:
        audit_log.record_atomic_many(batch, persist=on_persist)
    except OSError as exc:
        raise RuntimeError(
            f"Research-router audit persistence failed: {type(exc).__name__}"
        ) from exc


def _runtime_failure(message: str) -> dict[str, Any]:
    return {
        "direct_answer": "ClinicalGuard could not complete the requested life-science routing lookup.",
        "evidence_by_lane": [],
        "main_caveats": [message],
        "recommended_next_steps": [
            "Retry the query with a narrower scientific target or accession",
            "Check that the life-science runtime scripts are present and network access is available",
        ],
    }
