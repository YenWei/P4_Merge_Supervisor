# P4 Merge Supervisor

A Python-based agentic workflow supervisor for large-scale Perforce branch integrations, built from a real operational problem and packaged as a clear AI engineering case study.

Currently running in supervised automation mode against a real Unreal Engine game project with close to 1 million files.

---

## The Problem

Running a large Perforce branch integration sounds straightforward. In practice, it is not.

When your branch has close to **1 million files**, a merge can take 4-5 hours to run. During that time:

- The script can silently fail with no recovery mechanism
- You end up manually babysitting the process, checking whether it is still alive
- When it does finish, the resulting changelist can be too large and painful to submit
- Failures are expensive because there is no clean, structured way to resume from the point of failure

This tool was built to reduce that operational pain, not as a toy demo and not as an attempt to fully automate irreversible decisions.

---

## How It Evolved

### v0 - Simple preflight checker

Started as a basic Python script to validate Perforce login state, workspace configuration, and source/target streams before starting a merge.

### v1 - Monitoring and babysitting reduction

After watching merges die mid-run with weak diagnostics, the tool gained better logging, artifacts, and watchdog-based detection for no-progress hangs.

### v2 - Batch processing and smaller CLs

Once it became clear that giant all-in-one merges produced painful, hard-to-submit changelists, the workflow shifted to path-scoped batches.

### v3 - AI-assisted diagnostic layer

After repeated failure patterns showed up across runs, the supervisor gained a modular Doctor layer with deterministic and LLM-backed diagnosis, a policy gate, and a recovery executor.

---

## Current State

The tool is in weekly use against a real Unreal Engine game project.

### Implemented phases

| Phase | Status | What it does |
|---|---|---|
| `dry-run` | Live | Read-only preflight, boundary CL selection, merge preview |
| `run` | Live | Batch-scoped merges, safe trivial resolves, artifact writing, watchdog hang detection |
| `sanitize` | Live | Clean staged result, revert known junk, isolate blockers |
| `split` | Live (hardening) | Break large CL into reviewable area-grouped changelists |
| `doctor` | Live (supervised) | AI-assisted failure diagnosis with policy gate and recovery executor |

### Current automation level

**Supervised automation**. Phases are still run manually in sequence. The operator reviews artifacts, invokes Doctor when needed, and retains final authority over anything irreversible.

---

## Architecture

```text
Operator
  |
  +--> dry-run          Read-only preflight and preview
  |
  +--> run              Batch merges, resolve, watchdog, artifacts
  |       |
  |       +--> [if blocked] --> doctor
  |                              |
  |                              +--> DoctorBlockedCase
  |                              |
  |                              +--> diagnosis mode
  |                                      - deterministic
  |                                      - llm
  |                              |
  |                              +--> DoctorDecision
  |                              |
  |                              +--> policy gate
  |                                      - execute action
  |                                      - pause cleanly
  |
  +--> sanitize         Clean staged result
  |
  +--> split            Break into reviewable CLs
  |
  +--> [human submit]   Always manual, never automated
```

**Core principle: Prepare everything, submit nothing.**

This is intentionally a human-in-the-loop system. The AI can help classify failures and select from constrained recovery actions, but it does not get unlimited authority.

---

## Doctor Architecture

The Doctor layer is split into focused components:

| Module | Responsibility |
|---|---|
| `doctor_models.py` | Typed data models such as blocked cases and decisions |
| `doctor_layer.py` | Orchestration: load artifacts, choose mode, write outputs |
| `doctor_provider.py` | Diagnosis providers: deterministic and current LLM provider implementation |
| `doctor_policy.py` | Safety gate: validate decision against policy and threshold |
| `doctor_executor.py` | Recovery execution for whitelisted actions only |

### Doctor flow

```text
1. Load latest blocked phase artifact
2. Build typed blocked case
3. Choose mode: deterministic | llm
4. Produce structured decision
5. Validate output
6. Apply policy gate
7. Optionally run executor
8. Write doctor-summary.*, doctor-decision.*, resume.*
```

### Safe action whitelist

```python
SAFE_WHITELIST = {
    "retry_after_login_refresh",
    "retry_after_connectivity_restore",
    "retry_after_env_restore",
    "retry_resolve_with_charset_override",
    "kill_and_retry_same_phase_after_hang",
    "pause_cleanly",
}
```

Execution only happens with `--doctor-execute-whitelist`. Default behavior is conservative.

### Current LLM provider

The present LLM-backed provider is OpenAI-backed, but the README describes the architecture in provider-agnostic terms because the important boundary is deterministic vs LLM-backed diagnosis, not a specific vendor name.

---

## Safety Boundaries

These boundaries are intentional and non-negotiable.

### Always allowed

- Read P4 state such as login validation, opened files, and stream history
- Stage safe merge work such as preview merges and trivial resolves
- Write reports and artifacts for operator review
- Run Doctor diagnosis and policy evaluation

### Never automated

- **No submit** - the tool never runs `p4 submit`
- **No content judgement** - unresolved Blueprint, source, and asset conflicts always go to a human
- **No broad rollback** - large revert or backout actions are out of scope
- **No hidden remapping** - stream or client ownership changes are never silent

---

## Real Recovery: Charset Override

The first real AI-driven recovery action is implemented:

`retry_resolve_with_charset_override`

The executor tries to target only files implicated by translation-failure evidence in the blocked artifact and logs. It falls back to a wider retry only if no targeted file set can be extracted.

That reflects the broader design philosophy: be as surgical as possible and minimize blast radius.

---

## Watchdog

The supervisor includes a watchdog that monitors P4 subprocess progress:

- Detects no-progress windows during long-running operations
- Kills stuck subprocesses cleanly
- Surfaces a `suspected_hang` blocked case for Doctor diagnosis
- Uses configurable thresholds per phase

One current hardening area is threshold tuning. Early real runs suggested that `900s` was too aggressive for large engine-heavy resolve work, and longer per-phase thresholds are more realistic.

---

## Observability

Every run writes structured artifacts to `runs/<timestamp>/` so the operator can inspect what happened and why:

```text
runs/2026-06-10_112728_573079/
+-- merge-report.json
+-- merge-report.txt
+-- unresolved.txt
+-- opened.txt
+-- p4-commands.log
+-- p4-errors.log
+-- doctor-summary.*
+-- doctor-decision.*
+-- resume.*
```

This is a key part of the design. The goal is not just automation, but debuggable automation.

---

## Key Design Decisions

**Why compact error packets instead of raw logs?**
Raw P4 logs are noisy. The Doctor should receive the failing command, the error type, implicated files, and the most relevant recent lines rather than thousands of lines of low-signal output.

**Why a whitelist instead of open-ended AI actions?**
The system uses the LLM for constrained classification and recovery selection, not unrestricted autonomy. A wrong decision should fail conservatively.

**Why a policy threshold before execution?**
The model can be useful without being trusted absolutely. Below threshold, the system should pause cleanly and hand control back to the operator.

**Why separate deterministic and LLM modes?**
Known failure patterns can be handled cheaply and reliably with rules. Novel or ambiguous failures benefit from LLM-backed diagnosis. Keeping those paths separate makes the system easier to reason about and test.

**Why does the human always submit?**
Perforce submissions affect the whole team. The final irreversible step remains manual by design.

**Why subprocess instead of P4Python?**
Subprocess calls are simpler to debug, the command logs are directly readable, and the workflow does not need a richer SDK abstraction to be effective.

---

## What I Learned

- Defining the boundary of AI authority is harder than wiring up the API call
- Context engineering matters as much as prompt wording in real systems
- Structured outputs and typed models make AI-in-the-loop workflows much safer
- Conservative defaults build trust faster than aggressive autonomy
- Real workflows expose edge cases that no clean design document predicts in advance

---

## Tech Stack

- Python
- Perforce CLI via subprocess
- Unreal Engine game project context
- LLM-backed Doctor provider
- Current provider implementation: OpenAI

---

## Status

`dry-run`, `run`, `sanitize`, `split`, and `doctor` exist and are in active use.
The tool is currently operating in **supervised automation mode**.
Full unattended orchestration remains a future milestone rather than a current claim.
