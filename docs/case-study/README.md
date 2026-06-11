# Case Study: Supervised Merge Recovery

This case study documents how the merge supervisor evolved from a monitoring script into a human-in-the-loop automation system for large-scale Perforce integrations.

It is based on real supervised runs. Sensitive project details, stream names, and raw logs have been removed or generalized before publishing.

## What This Covers

- the operational problem
- the system architecture
- the safety model
- a real blocked-run recovery walkthrough
- the shape of the artifacts produced during a run
- the sanitization approach used for publishing

## Suggested Reading Order

1. [Real Run Walkthrough](./real-run-walkthrough.md)
2. [Safety Model](./safety-model.md)
3. [Architecture](./architecture.md)
4. [Artifact Schema](./artifact-schema.md)
5. [Sanitization Policy](./sanitization-policy.md)

## What Was Validated

- supervised real runs were used
- blocked artifacts were captured and analyzed
- deterministic doctor logic handled a real charset-related blocker
- targeted recovery completed successfully on a real blocked case

## What Was Not Claimed

- full unattended orchestration
- proven autonomous end-to-end merge completion
- safe automated handling for every blocker family
