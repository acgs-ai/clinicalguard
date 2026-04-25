# ClinicalGuard Local Model Training

This directory contains the starter path for training the local `research_router`
model used by `clinicalguard`.

## Target base model

- `unsloth/Qwen3.5-4B-Base`

## Scope

Train only bounded research-router tasks:

- lane selection
- entity-to-query shaping
- concise abstract summary generation
- structured JSON output

Do **not** train this model to become the final clinical or governance
authority. `clinicalguard` keeps deterministic policy, privacy, and audit
enforcement outside the model.

## Install

```bash
pip install -e ".[train]"
```

## Dataset shape

Each sample should contain:

- `messages`: a small chat transcript for SFT
- `task_type`: `lane_plan` or `abstract_summary`
- `expected_json`: the exact JSON object the model should learn to emit

Recommended split:

- 70% train
- 15% validation
- 15% held-out eval

## Suggested evals

- lane accuracy
- JSON schema validity
- abstract faithfulness
- abstain/escalate correctness
- no-PHI-in-output checks

## Starter script

Use `research_router_sft.py` as the initial Unsloth/TRL training entrypoint.
