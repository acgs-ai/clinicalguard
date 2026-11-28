# Changelog

All notable changes to ClinicalGuard are documented here.

## [Unreleased]

### Security

- **Authentication fails closed.** With no `CLINICALGUARD_API_KEY` (or a whitespace-only one), task requests
  (`POST /`) are now refused with 401. Previously they were accepted. `/health` and the agent card stay open.
  `CLINICALGUARD_ALLOW_UNAUTHENTICATED=1` restores the old behaviour for local development and logs a
  warning at startup; it is ignored when a key is set, and it does not apply to a whitespace-only key, which
  is always refused. **Deployments without a key must set one.**
- **`python -m clinicalguard.main` binds `127.0.0.1`** instead of `0.0.0.0`. Set `HOST` to change it. The
  Docker image is unchanged (it starts `uvicorn --host 0.0.0.0`).
- **PHI is checked before the LLM is called.** `validate_clinical_action` now runs the constitutional rules
  first. Text that matches a privacy rule (HC-003 or a `PHI-*` validator) is not sent to the LLM provider;
  the response is rule-only with `llm_available: false`. Previously the text was sent to the model before
  the rules ran. The same applies when a custom validator fails. When PHI is withheld (privacy match or
  validator failure), the audit trail stores a SHA-256 redaction summary (`metadata["action_redacted"] = true`)
  instead of raw input text. The decision for text with detected
  PHI is unchanged (REJECTED). A failed validator now also gives REJECTED; before, it could give
  CONDITIONAL when the LLM approved. In both cases the reported `confidence` is lower because no LLM
  assessment is included. Detection is pattern-based: PHI the patterns do not match is not covered.

### Fixed

- **`check_hipaa_compliance` no longer reports `compliant: true` for unassessed checklists.** Items with
  status `pending`, a missing status or an unrecognised status were counted as neither passing nor failing,
  so a checklist with nothing assessed passed. They are now counted in the new `items_unresolved` field and
  make the result non-compliant, with an `INCOMPLETE` summary. An empty checklist is also non-compliant.

## [1.1.0] - 2026-11-15

### Changed

- **Requires acgs-lite 4.x** (`acgs-lite>=4,<5`, previously `>=2.5`). No code changes were needed for the
  acgs-lite 4.0.0 API: every internal import ClinicalGuard uses is unchanged and the full test suite passes.
  Only acgs-lite 3.0.1 was tested on the 3.x line (it gives different verdicts, below); other versions below 4
  were not tested.
- **Deliberate behaviour change, from an acgs-lite engine bug fix.** acgs-lite 3.0.1 ran a rule's regex only
  when the text contained one of the engine's heuristic anchor words. HC-003's MRN pattern was given the
  anchor `number`, so an MRN such as `MRN: A12345` skipped HC-003 unless the text happened to contain an
  anchor word (for example `patient` or `review`). acgs-lite 4.0.0 derives anchors from the regex and always
  evaluates this pattern. On their own, `MRN: A12345` and `MRN: ABC1234567` were CONDITIONALLY_APPROVED on
  3.0.1 and are REJECTED (CRITICAL, HC-003) on 4.0.0. Numeric MRNs such as `MRN: 00123456` were already
  rejected by the `PHI-MRN` validator and now additionally report HC-003. The rule text and
  `constitution/healthcare_v1.yaml` are unchanged. This is why the floor is 4. A fresh
  `pip install clinicalguard==1.0.1` already resolved acgs-lite 4.0.0, so this is the behaviour new installs
  were getting.
- **Audit log default path moved out of `/tmp`.** `CLINICALGUARD_AUDIT_LOG` still overrides it. The default is
  now `$XDG_STATE_HOME/clinicalguard/clinicalguard_audit.json` (`~/.local/state/...` when unset); the Docker
  image sets `/data/clinicalguard_audit.json`. The service now refuses to start when the configured audit log
  path cannot be written (it is a directory, an unwritable existing file or symlink, or in an unwritable
  directory), instead of running without a persistent audit trail. A relative path is made absolute. A
  directory the service creates is mode 0700 and a new log file is mode 0600; existing directories and files
  keep their modes and content. A blank `CLINICALGUARD_AUDIT_LOG` counts as unset, and devices or FIFOs
  (for example `/dev/null`) are rejected. Write failures after startup (for example the file being made
  read-only later) are still only logged as warnings. Existing deployments that relied on the old `/tmp/clinicalguard_audit.json`
  default must set `CLINICALGUARD_AUDIT_LOG` (or copy the file) to keep their history.
- The agent card now reports the package version instead of a hard-coded `1.0.0`.

### Added

- Standalone `Dockerfile` (`pip install ".[anthropic]"`, non-root user, `/data` volume, `/health`
  `HEALTHCHECK`) and `.dockerignore`. The image includes the `anthropic` extra: with `ANTHROPIC_API_KEY` set the
  LLM layer works, without it the service answers rule-only.
- README Docker quickstart; deployment guide corrected (no monorepo paths).
- Regression tests for the audit path (default, override, unwritable path variants, file modes) and for MRN handling through
  `validate_clinical_action` and the A2A route.

### Removed

- `[tool.uv.sources] acgs-lite = { workspace = true }`, which assumed a monorepo.
