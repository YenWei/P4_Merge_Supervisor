# Safety Model

## Design Principle

The system is intentionally human-in-the-loop. It prepares work, diagnoses some blocked states, and can execute narrowly whitelisted recovery actions, but it does not take irreversible team-wide actions on its own.

## Always Allowed

- read Perforce state
- stage merge work
- write run artifacts
- classify blocked cases
- evaluate a policy gate
- execute approved low-risk recovery actions when explicitly enabled

## Never Automated

- final submit
- content judgment on unresolved source or asset conflicts
- broad rollback or destructive cleanup
- hidden workspace or stream remapping

## Why The Boundary Matters

Perforce submit is a team-level irreversible action. The project uses AI as a constrained classifier and recovery assistant, not as an unlimited agent with broad operational authority.

## Control Mechanisms

- explicit safe-action whitelist
- policy threshold before execution
- conservative default behavior
- manual enablement for execution-capable doctor flows
