# P4 Merge Supervisor

A Python-based agentic workflow supervisor for large-scale Perforce branch integrations, built from a real operational problem and being packaged into a cleaner portfolio-safe repository.

The core idea is simple: do not treat a massive Perforce merge as one opaque command. Break it into explicit phases, write structured artifacts at each step, and preserve a human boundary before irreversible submit.

---

## The Problem

Running a large Perforce branch integration sounds straightforward. In practice, it is not.

When a branch is extremely large, a merge can take hours to run. During that time:

- the process can fail with weak recovery paths
- the operator ends up manually babysitting long-running commands
- the resulting changelist can be too large and painful to review or submit
- failures are expensive when there is no clean, structured way to resume from the point of failure

This tool was built to reduce that operational pain, not as a toy demo and not as an attempt to fully automate irreversible decisions.

---

## How It Evolved

### v0 - Preflight and validation

Started as a Python script to validate Perforce login state, workspace configuration, merge boundaries, and source or target assumptions before starting a merge.

### v1 - Artifact-driven merge staging

As long-running merges proved fragile, the tool gained better reporting, explicit run artifacts, and a more structured staged-merge workflow.

### v2 - Batch-oriented review flow

Once large all-in-one merges became painful to inspect, the workflow shifted toward batch-scoped staging, cleanup, and changelist organization.

### v3 - Supervision and Doctor recovery

After repeated failure patterns appeared across runs, the system gained a supervised runtime, typed status and resume models, a Doctor layer, policy-gated recovery, and verification-aware retry handling.

---

## Current State

The repository currently represents the merge-preparation and supervised recovery core.

### Implemented phases

| Phase | Status | What it does |
|---|---|---|
| `dry-run` | Live | Read-only preflight, boundary selection, merge preview |
| `run` | Live | Batch-scoped staging of merge work and run artifact writing |
| `resolve` | Live | Apply batch-specific resolve strategy and isolate leftovers |
| `sanitize` | Live | Clean staged result and preserve review buckets |
| `resolve-conflicts` | Live | Selectively push safe conflict buckets further while preserving human review |
| `split` | Live | Break staged work into smaller review-oriented changelists when needed |
| `doctor` | Live (supervised) | Diagnose blocked states, apply policy, and support bounded recovery |
| `supervise` | Live | Coordinate multi-phase automated merge preparation flow |

### Current automation level

**Supervised automation**.

The system is validated for automated merge preparation, but it still preserves a human authority boundary before final submit.

- End-to-end validated for automated merge preparation
- Not yet end-to-end validated for submit or production completion

---

## Architecture

```text
Operator
  |
  +--> dry-run              Read-only preflight and preview
  |
  +--> supervise            Parent runtime for phase coordination
  |       |
  |       +--> run              Stage merge work into changelists
  |       |
  |       +--> resolve          Apply resolve strategy per batch
  |       |       |
  |       |       +--> [if blocked] --> doctor
  |       |                              |
  |       |                              +--> blocked case
  |       |                              +--> deterministic or llm diagnosis
  |       |                              +--> policy gate
  |       |                              +--> optional bounded recovery
  |       |
  |       +--> sanitize         Clean staged result
  |       |
  |       +--> resolve-conflicts
  |               |
  |               +--> auto-acceptable conflict buckets only
  |
  +--> split                Optional review-oriented CL restructuring
  |
  +--> [human submit]       Always manual, never automated
```

**Core principle: Prepare everything, submit nothing.**

This is intentionally a human-in-the-loop system. The AI can help diagnose blocked states and select from constrained recovery actions, but it does not get unlimited authority.

---

## Doctor Architecture

The Doctor layer is split into focused components:

| Module | Responsibility |
|---|---|
| `doctor_models.py` | Typed runtime and decision models |
| `doctor_layer.py` | Diagnosis orchestration and report shaping |
| `doctor_provider.py` | Deterministic and LLM-backed diagnosis providers |
| `doctor_policy.py` | Safety gate and execution policy |
| `doctor_executor.py` | Recovery execution for allowed actions only |
| `recovery_verifier.py` | Post-recovery verification and trusted resume bundle generation |

### Doctor flow

```text
1. Load blocked phase artifact
2. Build typed blocked case and resume state
3. Choose diagnosis mode
4. Produce structured decision
5. Validate machine-usable fields
6. Apply policy gate
7. Optionally run bounded recovery
8. Verify recovery result and emit trusted resume state
9. Write doctor-summary.*, doctor-decision.*, resume.*
```

### Recovery model

The recovery path is deliberately conservative:

- actions are selected from constrained, code-defined recovery paths
- execution is policy-gated rather than open-ended
- verifier output determines whether the system can safely resume
- failures fall back to pause or re-diagnosis rather than pretending recovery succeeded

---

## Safety Boundaries

These boundaries are intentional and non-negotiable.

### Always allowed

- read Perforce state such as login validation, opened files, and stream history
- stage merge work and write reports for operator review
- run diagnosis and policy evaluation on blocked states
- reorganize staged work into review-oriented buckets

### Never automated

- **No submit** - the tool never performs final team-wide submit autonomously
- **No hidden judgement on hard conflicts** - unresolved source, asset, or project-specific conflicts remain human decisions
- **No unrestricted AI actions** - recovery is bounded by code and policy, not free-form model autonomy
- **No silent environment remapping** - stream, client, or ownership assumptions should never change invisibly

---

## Observability

Every phase writes structured artifacts so the operator can inspect what happened, why it happened, and what should run next.

Examples include:

```text
status.json
status.txt
merge-report.json
resolve-summary.json
sanitize-summary.json
conflict-resolution-summary.json
doctor-summary.json
doctor-decision.json
resume-state.json
p4-commands.log
p4-errors.log
```

This is a key part of the design. The goal is not just automation, but debuggable automation.

---

## Key Design Decisions

**Why phases instead of one large script?**  
Explicit phases make the workflow easier to reason about, easier to resume, and easier to test in isolation.

**Why artifacts everywhere?**  
Long-running operational workflows need inspectable checkpoints. Terminal output alone is not enough.

**Why a policy gate before recovery execution?**  
The model can be useful without being trusted absolutely. Low-confidence or out-of-policy decisions should pause cleanly.

**Why separate deterministic and LLM-backed diagnosis?**  
Known patterns can be handled cheaply and predictably with rules. Ambiguous cases benefit from model assistance.

**Why does the human always submit?**  
Perforce submissions affect the whole team. The final irreversible step remains manual by design.

**Why subprocess rather than P4Python?**  
CLI execution is easier to debug, the command logs are directly readable, and the workflow does not require a heavier SDK abstraction to be effective.

---

## What I Learned

- defining the boundary of AI authority is harder than wiring up the model call
- context engineering matters as much as prompt wording in operational systems
- typed models and structured outputs make AI-in-the-loop workflows safer
- conservative defaults build trust faster than aggressive autonomy
- real workflows expose edge cases that clean architecture diagrams do not predict in advance

---

## Tech Stack

- Python
- Perforce CLI via `subprocess`
- phase-based workflow architecture
- typed runtime and artifact models
- deterministic and LLM-backed Doctor diagnosis

---

## Repository Layout

- `p4_weekly_merge.py`: CLI entrypoint, batch configuration, and shared orchestration helpers
- `merge_phases/`: individual phase implementations
- `merge_supervisor/`: supervision runtime models, doctor policy, provider, executor, and recovery verification logic
- `merge_support/`: artifact handling and output helpers
- `tests/`: regression coverage for resolve behavior, doctor recovery behavior, and public snapshot sanitization

Future public repo structure will likely add:

- `docs/architecture/`
- `docs/case-study/`
- `docs/visuals/`

---

## Status

`dry-run`, `run`, `resolve`, `sanitize`, `resolve-conflicts`, `split`, `doctor`, and `supervise` are present in this snapshot.

The tool is currently operating as a **supervised merge-preparation system**.
Final submit and production completion remain explicit human responsibility.
