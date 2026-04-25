import os
from pathlib import Path

import braintrust
from autoevals import Factuality
from braintrust import Eval

from acgs_lite.audit import AuditLog
from acgs_lite.constitution import Constitution
from acgs_lite.engine import GovernanceEngine
from clinicalguard.skills.validate_clinical import validate_clinical_action

# ── Setup ──────────────────────────────────────────────────────────────────


def get_engine():
    yaml_path = Path(__file__).parent.parent / "constitution" / "healthcare_v1.yaml"
    constitution = Constitution.from_yaml(str(yaml_path))
    return GovernanceEngine(constitution, strict=False)


def get_audit_log():
    return AuditLog()


# ── Task ──────────────────────────────────────────────────────────────────


# We trace the eval task so it creates a distinct root span for each evaluation run.
@braintrust.traced
async def run_real_validation(input_text):
    """
    Runs the full validation including the un-mocked LLM call.
    Requires ANTHROPIC_API_KEY to be set in the environment or `pi` to be available.
    """
    # Ensure external LLM usage is enabled for the eval
    os.environ["CLINICALGUARD_ENABLE_EXTERNAL_CLINICAL_LLM"] = "true"

    engine = get_engine()
    audit_log = get_audit_log()

    result = await validate_clinical_action(input_text, engine=engine, audit_log=audit_log)

    # We return a structured output so we can score multiple aspects of it
    return {
        "decision": result["decision"],
        "reasoning": result["reasoning"],
        "risk_tier": result["risk_tier"],
        "llm_available": result["llm_available"],
    }


# ── Custom Scorers ────────────────────────────────────────────────────────


def decision_match(input, output, expected=None):
    """Score whether the final decision matches what we expect."""
    if expected is None:
        return 1.0  # No expected value, skip scoring

    return 1.0 if output["decision"] == expected else 0.0


def llm_available(input, output, expected=None):
    """Score whether the LLM was successfully reached (not fallen back to rules-only)."""
    return 1.0 if output.get("llm_available", False) else 0.0


# ── Eval ──────────────────────────────────────────────────────────────────

# Check if we have Anthropic configured, otherwise the 'Real' eval will just fall back to rules
has_llm = os.environ.get("ANTHROPIC_API_KEY") is not None


# The Factuality evaluator from autoevals acts as a LLM-as-a-judge.
# It checks if the `output` is factually consistent with the `input` and `expected` truth.
# We map our structured output's reasoning into the format it expects.
def reasoning_factuality(input, output, expected=None):
    if not has_llm or not os.environ.get("OPENAI_API_KEY"):
        # Factuality requires OPENAI_API_KEY by default
        return None

    return Factuality()(
        input=input,
        output=output["reasoning"],
        expected=expected,  # we pass the expected decision as context
    )


Eval(
    "ClinicalGuard Real LLM Evaluation",
    # Pull data dynamically from the dataset we just created
    data=braintrust.init_dataset(project="acgs", name="ClinicalGuard Scenarios"),
    task=run_real_validation,
    scores=[decision_match, llm_available, reasoning_factuality],
    metadata={"type": "un-mocked", "engine": "acgs_lite"},
)
