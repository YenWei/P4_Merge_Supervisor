# P4 Merge Supervisor

A Python-based AI-supervised workflow for large-scale Perforce branch integrations.

---

## The Problem

Running a Dev-to-TA branch merge in Perforce sounds straightforward. In practice, it isn't.

When your branch has close to **1 million files**, a merge takes 4–5 hours to run. During that time:

- The script can silently fail with no recovery
- You end up manually babysitting the process, poking it every 10 minutes to check if it's still alive
- When it finally finishes, the resulting changelist is so large it becomes almost impossible to submit — another 4–5 hours of pain
- Failures are expensive. There's no easy way to resume from where things went wrong

This tool was built to solve those problems — not from a design document, but from living through every one of those failures.

---

## How It Evolved

### v0 — Simple preflight checker
Started as a basic Python script to validate Perforce login state, workspace configuration, and source/target streams before starting a merge. Useful, but once execution started, you were on your own.

### v1 — Monitoring & restart automation
After watching the merge silently die mid-run with no recovery, added a monitoring layer that could detect failures and attempt automatic restarts. Reduced babysitting significantly.

### v2 — Batch processing & smaller CLs
Discovered that merging the entire branch in one shot created an unsubmittable giant CL. Redesigned around batching — breaking the integration into manageable chunks and splitting submissions into smaller, reviewable CLs.

### v3 — AI-assisted diagnostic layer (current)
After seeing the same failure patterns repeat across runs, added an AI Doctor layer: instead of manually diagnosing each failure, the supervisor packages the error context and asks an LLM to classify it and recommend a safe recovery action.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  Supervisor Script               │
│  Owns run state, phase order, command logs       │
│  Runs deterministic P4 commands                  │
│  Stops when human judgement is required          │
└──────────────────────┬──────────────────────────┘
                       │ on failure
                       ▼
┌─────────────────────────────────────────────────┐
│                   AI Doctor                      │
│  Receives compact error packet (not raw logs)    │
│  Classifies failure mode                         │
│  Returns structured JSON recommendation          │
│  Can only recommend from a safe action whitelist │
└──────────────────────┬──────────────────────────┘
                       │ recommendation
                       ▼
┌─────────────────────────────────────────────────┐
│                    Human                         │
│  Reviews AI recommendation                       │
│  Reviews conflict files and risky assets         │
│  Runs final build validation                     │
│  Submits manually                                │
└─────────────────────────────────────────────────┘
```

**Core principle: Prepare everything, submit nothing.**

The supervisor automates what is safe to automate. The AI diagnoses what is hard to diagnose manually. The human stays in control of anything irreversible.

---

## The 7-Phase Workflow

| Phase | What happens |
|---|---|
| 1. Preflight | Login, client, stream, workspace validation |
| 2. Find boundary CL | Scan Dev history for the correct merge boundary |
| 3. Merge | Deterministic P4 integrate into target branch |
| 4. Resolve | Apply safe binary rules, flag risky assets for human review |
| 5. AI Doctor | Classify failures, propose whitelisted safe actions as structured JSON |
| 6. Sanitize & split | Remove junk, isolate blockers, split into reviewable CLs |
| 7. Human submit | Generate submit commands and report, tool stops |

---

## AI Doctor Design

### Why not just let the AI fix things?

Because some failures are irreversible. A wrong submit in Perforce affects the entire team. The cost of an AI making an incorrect autonomous decision is too high.

The AI Doctor is designed with explicit constraints:

- **Input**: Compact error packet — not raw logs. Structured context about what phase failed, what command ran, and what the error output was.
- **Output**: Structured JSON only. A classification of the failure and a recommended action from a predefined whitelist.
- **Whitelist**: The set of safe actions the AI can recommend is fixed. It cannot recommend submitting, resolving source conflicts, or modifying specs without explicit human confirmation.

### Example AI Doctor response

```json
{
  "failure_type": "stale_client_lock",
  "confidence": "high",
  "recommended_action": "clear_stale_lock",
  "reasoning": "The error indicates a stale lock from a previous interrupted run. This is safe to clear automatically.",
  "requires_human_review": false
}
```

### Model abstraction

The AI Doctor is designed to be model-agnostic:

```python
class AIProvider:
    def diagnose(self, error_packet: dict) -> dict:
        raise NotImplementedError

class AnthropicProvider(AIProvider):
    def diagnose(self, error_packet: dict) -> dict:
        # Claude API call
        pass

class OpenAIProvider(AIProvider):
    def diagnose(self, error_packet: dict) -> dict:
        # OpenAI API call
        pass
```

---

## Key Design Decisions

**Why compact error packets instead of raw logs?**
Raw P4 logs are noisy. Sending 10,000 lines of output to an LLM wastes tokens and reduces diagnosis quality. The supervisor extracts only what's relevant: the failing command, the error type, and the last N lines of output.

**Why a whitelist instead of open-ended AI actions?**
The AI is good at classification. It is not always right. Constraining it to a known set of safe actions means a wrong recommendation fails safely — the worst case is the human has to intervene manually, which is no worse than the pre-AI baseline.

**Why does the human always submit?**
Perforce submissions affect everyone on the team. There is no undo. This boundary is intentional and non-negotiable regardless of how confident the AI is.

**Why batching?**
A single giant CL with 50,000 files is impossible to review and prone to submission failures. Smaller CLs are reviewable, submittable, and recoverable if something goes wrong.

---

## What I Learned

Building this taught me more about AI-assisted workflows than any tutorial could:

- The hardest part isn't the AI integration — it's deciding where the AI boundary should be
- Context management matters: what you send to the LLM is as important as the prompt
- Structured outputs are essential for reliable AI-in-the-loop systems
- Real production workflows have edge cases that no design document anticipates — you find them by running the thing

---

## Status

Currently running against real branch integrations. The AI Doctor layer (v3) is in active testing.

---

## Tech Stack

- Python
- Perforce Python API (P4Python)
- OpenAI API / Anthropic API (model-agnostic)
- Unreal Engine project context (game development workflow)
