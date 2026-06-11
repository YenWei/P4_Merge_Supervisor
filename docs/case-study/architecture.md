# Architecture

## High-Level Flow

```text
Operator
  -> dry-run
  -> run
      -> artifacts
      -> doctor (if blocked)
  -> sanitize
  -> split
  -> human submit
```

## Core Components

| Component | Responsibility |
|---|---|
| phase runner | executes merge workflow phases |
| watchdog | detects no-progress windows and surfaces suspected hangs |
| artifact writer | records structured outputs for review and recovery |
| doctor layer | loads blocked artifacts, chooses diagnosis mode, and writes decisions |
| policy gate | checks confidence and whitelist rules before execution |
| executor | performs narrowly scoped approved recovery actions |

## Why This Structure Was Chosen

The system is organized around observable phases and explicit artifacts so that blocked runs can be reviewed, diagnosed, and retried without relying on memory or raw terminal scrolling.

## Key Architectural Theme

The goal is not maximum autonomy. The goal is debuggable, constrained automation with clear pause points.
