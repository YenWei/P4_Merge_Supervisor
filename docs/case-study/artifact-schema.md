# Artifact Schema

## Purpose

Each run writes structured artifacts so a blocked workflow can be inspected and resumed without depending on a live terminal session.

## Representative Artifacts

| Artifact | Role |
|---|---|
| `merge-report.json` | machine-readable run summary |
| `merge-report.txt` | operator-facing summary |
| `unresolved.txt` | unresolved file list for review |
| `opened.txt` | staged opened-file snapshot |
| `p4-commands.log` | command audit trail |
| `p4-errors.log` | filtered error stream |
| `doctor-summary.*` | diagnosis summary |
| `doctor-decision.*` | structured doctor output |
| `resume.*` | next-step hints or rerun guidance |

## Example Sanitized Fields

```json
{
  "phase": "run",
  "status": "blocked",
  "blocker_category": "resolve_charset",
  "recovery_action": "retry_resolve_with_charset_override",
  "recovery_scope": "targeted_files",
  "target_count": 230
}
```

## Why Artifacts Matter

Artifacts make the workflow reviewable after the fact. They give the operator and doctor layer a shared record of what happened, what was attempted, and what recovery path was considered safe enough to take.
