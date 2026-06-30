# P4 Merge Supervisor - Code Structure

This file explains the current code structure of the public snapshot.

It is meant to complement [README.md](/S:/GitHub_P4_MergeTool/README.md): the README explains the problem, operating model, and safety boundaries, while this file explains how the code is organized today.

## Project Layout

```text
P4_Merge_Supervisor/
|- p4_weekly_merge.py              # CLI entrypoint and shared orchestration helpers
|- merge_phases/
|  |- dry_run_phase.py             # Read-only preflight and preview
|  |- run_phase.py                 # Batch-scoped merge staging
|  |- resolve_phase.py             # Resolve strategy and leftover isolation
|  |- sanitize_phase.py            # Cleanup and review-bucket preservation
|  |- resolve_conflicts_phase.py   # Selective follow-up on safe conflict buckets
|  |- split_phase.py               # Review-oriented changelist restructuring
|  `- doctor_phase.py              # Blocked-state diagnosis and recovery flow
|- merge_supervisor/
|  |- supervised_runner.py         # End-to-end supervised phase coordination
|  |- runtime_models.py            # Typed phase outcome and resume-state models
|  |- doctor_models.py             # Typed diagnosis and recovery decision models
|  |- doctor_layer.py              # Doctor orchestration and report shaping
|  |- doctor_provider.py           # Deterministic and LLM-backed diagnosis providers
|  |- doctor_policy.py             # Policy gate for recovery execution
|  |- doctor_executor.py           # Execution of bounded recovery actions
|  |- recovery_verifier.py         # Verify recovery and emit trusted resume bundles
|  `- policy_ladder.py             # Tracks repeated validated recovery patterns
|- merge_support/
|  |- artifacts.py                 # Artifact reading, writing, and resume helpers
|  `- p4_output.py                 # P4 output normalization helpers
`- tests/
   |- test_doctor_recovery.py      # Doctor and recovery regression coverage
   |- test_resolve_phase.py        # Resolve helper behavior coverage
   `- test_public_sanitization.py  # Public snapshot sanitization guard
```

## Structural Overview

The codebase is organized around three layers:

1. `merge_phases/`
Implements the individual operational phases. Each phase has a narrow responsibility and produces structured artifacts that can be inspected or resumed later.

2. `merge_supervisor/`
Implements the cross-phase runtime, typed state models, Doctor diagnosis flow, policy gating, bounded recovery execution, and verification-aware resume handling.

3. `merge_support/`
Provides shared helpers for artifact persistence and P4 output shaping so phase logic stays focused on operational decisions instead of file-format plumbing.

`p4_weekly_merge.py` remains the CLI-facing entrypoint and shared orchestration surface that binds the pieces together.

## Phase Responsibilities

### `dry-run`

Purpose:
Read-only preflight and preview before any merge work is staged.

Responsibilities:
- validate P4 working context
- identify merge boundaries
- run preview-style checks
- write status and report artifacts without changing submit state

### `run`

Purpose:
Stage merge work into batch-scoped changelists.

Responsibilities:
- execute staged merge batches
- create or track child changelists
- persist run artifacts
- surface blocked states when staging cannot continue cleanly

### `resolve`

Purpose:
Apply the correct resolve strategy for each staged batch and isolate leftovers.

Responsibilities:
- apply batch-specific `p4 resolve` behavior
- distinguish plugin or special-case paths from normal paths
- re-scan unresolved or tampered files
- move leftovers into conflict-oriented buckets

### `sanitize`

Purpose:
Clean staged results and preserve the review structure.

Responsibilities:
- run cleanup such as `revert -a`
- remove stale opened files
- preserve review, conflict, and holding buckets
- produce cleaner submit-plan artifacts for humans

### `resolve-conflicts`

Purpose:
Push only low-risk conflict buckets further while keeping the human boundary intact.

Responsibilities:
- inspect conflict and holding buckets
- apply source-accept only to approved bucket types
- re-check unresolved state
- leave risky conflicts visible for human review

### `split`

Purpose:
Reorganize one staged changelist into smaller review-oriented units.

Responsibilities:
- inspect opened files in a staged changelist
- classify files into review, conflict, or holding buckets
- create child changelists per bucket
- preserve reviewable structure for manual follow-up

### `doctor`

Purpose:
Diagnose blocked states and support bounded recovery.

Responsibilities:
- load blocked artifacts
- build typed blocked-case context
- choose deterministic or LLM-backed diagnosis
- validate machine-usable decision fields
- apply policy gate
- optionally execute bounded recovery
- verify recovery outcome and emit trusted resume information

### `supervise`

Purpose:
Coordinate multiple phases as one supervised merge-preparation flow.

Responsibilities:
- run child phases in sequence
- track phase outcomes through typed runtime models
- invoke Doctor on retryable blocked states
- decide whether to continue, retry, pause, or stop

## Current Runtime Flow

The current supervised path is:

```text
supervise -> run -> resolve -> sanitize -> resolve-conflicts
```

The compatible manual or review-oriented path is:

```text
dry-run -> run -> split -> sanitize -> resolve-conflicts
```

The final submit step remains intentionally outside the automated path.

## Key Runtime Objects

### `PhaseOutcome`

Defined in `merge_supervisor/runtime_models.py`.

Represents the result of a phase in a machine-usable form, including:
- phase identity
- runtime result status
- whether the state is retryable
- trusted resume bundle data when available

This object is the handoff contract between child phases and the supervised runner.

### `Resume Bundle` and `Resume State`

Derived from trusted artifacts and verifier output.

Used to:
- resume the correct next phase
- preserve the live staged change when recovery succeeds
- avoid inventing runtime state from ambiguous artifacts

### `Blocked Case`

Defined in the Doctor model layer.

Packages the information needed to diagnose a blocked run:
- failing phase
- error class
- recent context
- prior attempts
- resume metadata

### `DoctorDecision`

Represents the structured diagnosis output after validation and policy handling.

It is designed to be:
- machine-usable
- auditable in artifacts
- safe to reject when confidence or policy requirements are not met

## Doctor Execution Boundary

The Doctor layer is intentionally split so diagnosis, policy, execution, and verification are separate concerns:

1. `doctor_provider.py`
Produces deterministic or LLM-backed diagnosis output.

2. `doctor_policy.py`
Decides whether the proposed action is allowed, should pause, or needs escalation.

3. `doctor_executor.py`
Runs only bounded recovery actions that are explicitly supported in code.

4. `recovery_verifier.py`
Checks whether recovery actually produced a safe, trusted resume state.

This separation is one of the key safety properties of the system.

## Artifact Philosophy

Artifacts are first-class runtime checkpoints, not just logs.

The code relies on artifacts so the system can:
- inspect what happened after long-running operations
- resume from trusted state
- diagnose failures without replaying the whole run
- preserve operator visibility into automated decisions

Common artifact families include:
- `status.*`
- `merge-report.*`
- `resolve-summary.*`
- `sanitize-summary.*`
- `conflict-resolution-summary.*`
- `doctor-summary.*`
- `doctor-decision.*`
- `resume-state.json`
- `p4-commands.log`
- `p4-errors.log`

## Public Snapshot Notes

This public snapshot intentionally keeps:
- the core Python implementation
- the regression tests
- the architecture surface of the supervised runtime

It intentionally excludes:
- raw operational run directories
- submit history artifacts
- local helper batch scripts
- internal environment metadata

That boundary keeps the repo portfolio-safe while still exposing the real code structure and operating model.
