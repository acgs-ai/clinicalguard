# Changelog

All notable changes to ClinicalGuard are documented here.

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
