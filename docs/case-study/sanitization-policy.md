# Sanitization Policy

This repository does not publish raw operational logs from the original internal environment.

## Removed Or Generalized

- stream and branch identifiers
- workspace names
- internal file paths
- project or studio identifiers
- raw command transcripts
- raw failure output containing sensitive context

## Preserved

- failure categories
- action names
- counts where they support the case study
- architectural relationships
- operational lessons

## Publishing Rule

When real evidence is used, it is rewritten into sanitized prose, tables, or narrow examples rather than committed as a raw artifact dump.
