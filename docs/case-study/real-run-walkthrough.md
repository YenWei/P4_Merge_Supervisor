# Real Run Walkthrough

## Scenario

One supervised `run` phase entered a blocked state during resolve work on a large integration. The blocked artifact was captured, classified, and later used to drive a targeted recovery path.

## Sanitized Facts

| Item | Value |
|---|---|
| blocker family | `resolve_charset` |
| recovery mode | deterministic doctor |
| recovery action | `retry_resolve_with_charset_override` |
| recovery scope | `targeted_files` |
| targeted file count | `230` |
| recovery outcome | `RECOVERY_EXECUTED_RETRY_SUCCEEDED` |

## What Happened

1. A real `run` phase produced a blocked artifact rather than silently failing.
2. The doctor layer loaded that artifact and built a typed blocked-case model.
3. Deterministic rules classified the failure as a charset-related resolve blocker.
4. The executor targeted only files implicated by the failure evidence instead of retrying the broader integration blindly.
5. The recovery step completed successfully and the recheck passed.

## Why This Matters

The important result was not just that the command ran again. The system recovered in a narrower, lower-blast-radius way based on structured evidence from the blocked run.

## What This Proves

- the artifact model was useful in a real blocked run
- the deterministic doctor path was operational, not theoretical
- targeted recovery can be safer than a broad retry

## What This Does Not Prove

- that the entire pipeline now runs unattended
- that all future blockers can be resolved automatically
- that a successful recovery step guarantees full end-to-end merge completion
