# P4 Merge Supervisor

A Python workflow supervisor for large-scale Perforce branch integrations - built to solve real operational pain, not as a portfolio exercise.

---

## The Problem

Running a large Perforce branch integration sounds straightforward. In practice, it isn't.

When your branch has close to **1 million files**, a merge takes 4-5 hours to run. During that time:

- The script can silently fail with no recovery mechanism
- You end up manually babysitting the process, checking every 10 minutes if it's still alive
- When it finally finishes, the resulting changelist is so large it becomes almost impossible to submit - another 4-5 hours of pain
- Failures are expensive. There's no easy way to resume from where things went wrong

This tool was built to solve those problems - not from a design document, but from living through every one of those failures.

---

## How It Evolved

### v0 - Simple preflight checker

Started as a basic Python script to validate Perforce login state, workspace configuration, and source/target streams before starting a merge. Useful, but once execution started, you were on your own.

### v1 - Monitoring & babysitting reduction

After watching the merge silently die mid-run with no recovery, added better error capture and logging so failures left enough evidence to understand what happened without replaying the whole run.

### v2 - Batch processing & smaller CLs

Discovered that merging the entire branch in one shot created an unsubmittable giant CL. Redesigned around path-scoped batches - breaking the integration into manageable chunks so submissions stay reviewable.

### v3 - Planned: AI-assisted diagnostic layer

After seeing the same failure patterns repeat across runs, the next step is an AI Doctor layer: instead of manually diagnosing each failure, the supervisor packages the error context and asks an LLM to classify it and recommend a safe recovery action from a whitelist.

---

## Current State

The tool runs weekly in production against a real Unreal Engine game project.

### What is implemented

| Phase | Status | What it does |
|---|---|---|
| `dry-run` | Live | Preflight checks, boundary CL selection, merge preview - read-only, no files staged |
| `run` | Live | Batch-scoped merges, safe trivial resolves (`p4 resolve -am`), unresolved file capture, artifact writing |

### What is planned

| Phase | Status | What it will do |
|---|---|---|
| `status` | Planned | Structured `status.txt` / `status.json` output - current state, blockers, explicit next action |
| `sanitize` | Planned | Clean staged result: revert known junk, isolate known blockers, normalize whitelisted cases |
| `split` | Planned | Break large CL into numbered, area-grouped changelists for reviewable submission |
| `doctor` | Planned | AI-assisted failure diagnosis - classify blocker, propose whitelisted safe actions only |

---

## The 7-Phase Workflow

This is the full intended workflow for the tool. Not every phase is implemented yet, but this is the operating model the supervisor is being built toward.

| Phase | What happens |
|---|---|
| 1. Preflight | Login, client, stream, workspace validation |
| 2. Find boundary CL | Scan source branch history for the correct merge boundary |
| 3. Merge | Deterministic P4 merge into the target branch |
| 4. Resolve | Apply safe trivial resolve rules, surface risky conflicts for human review |
| 5. AI Doctor | Classify failures, decide whether they are safely recoverable, or escalate to human |
| 6. Sanitize & split | Remove junk, isolate blockers, split into reviewable CLs |
| 7. Human submit | Final validation and submit stay manual |

---

## Architecture

```text
Operator
  |
  +--> dry-run          Read-only preflight and preview
  |
  +--> run              Stage merges, capture state, stop before submit
        |
        v
    Artifacts           merge-report.json / .txt
    (written to         unresolved.txt
    runs/<timestamp>)   opened.txt
                        p4-commands.log
                        p4-errors.log
        |
        v
    Manual Review       Operator inspects artifacts
        |
        v
    Manual Submit       p4 submit - always manual, never automated
```

**Core principle: Prepare everything, submit nothing.**

---

## Safety Boundaries

These are non-negotiable by design.

### Always allowed

- Read P4 state: `login -s`, `opened`, `changes`, `fixes`, `client -o`
- Stage safe merge work: preview merges, perform merges, `resolve -am` for trivial merges
- Write reports and artifacts for operator review

### Never automated

- **No submit** - the tool never runs `p4 submit` on behalf of the operator
- **No content judgement** - unresolved merge conflicts for Blueprints, source files, or assets are always left for human decision
- **No broad rollback** - large revert or backout actions are outside scope
- **No hidden remapping** - stream or client ownership changes are never silent

---

## Why Not Just Let AI Fix Things?

Because some failures are irreversible. A wrong submit in Perforce affects the entire team. The cost of an AI making an incorrect autonomous decision is too high.

The Doctor phase is intended to work inside strict limits:

- It should diagnose and classify failures, not act with unlimited authority
- It should only recommend or trigger actions from an explicit safe whitelist
- It should escalate to the human whenever content judgement or risky state changes are involved
- It must never cross the final manual submit boundary

---

## Key Design Decisions

**Why compact error packets instead of raw logs?**
Raw P4 logs are noisy. The supervisor will extract only what's relevant: the failing command, error type, and last N lines of output. Sending 10,000 lines to an LLM wastes tokens and reduces diagnosis quality.

**Why a whitelist instead of open-ended AI actions?**
The AI is good at classification. It is not always right. Constraining it to a known set of safe actions means a wrong recommendation fails safely - the worst case is the human has to intervene manually, which is no worse than the pre-AI baseline.

**Why does the human always submit?**
Perforce submissions affect the entire team. There is no undo. This boundary is intentional and non-negotiable regardless of AI confidence level.

**Why batching?**
A single giant CL with tens of thousands of files is impossible to review and prone to submission failures. Smaller, path-scoped CLs are reviewable, submittable, and recoverable.

**Why shell out via subprocess instead of P4Python?**
The current implementation calls `p4` directly via subprocess - simpler to debug, easier to read command logs, and produces the same artifacts without an additional dependency.

---

## What I Learned

- The hardest part of building AI-assisted workflows isn't the AI integration - it's deciding where the boundary of AI authority should be
- Context management matters: what you send to the LLM is as important as the prompt itself
- Structured outputs are essential for reliable AI-in-the-loop systems
- Real production workflows have failure modes that no design document anticipates - you find them by running the thing

---

## Tech Stack

- Python (subprocess-based P4 calls)
- Unreal Engine game project context
- Planned: OpenAI API / Anthropic API (model-agnostic provider layer)

---

## Status

`dry-run` and `run` phases are in weekly production use.
`status`, `sanitize`, `split`, and `doctor` phases are in active development.
