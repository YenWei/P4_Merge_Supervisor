# P4 Merge Supervisor — Code Structure

This file shows the project structure and key interfaces.
Full implementation is internal to the studio environment.

## Project Layout

```
p4-merge-supervisor/
├── supervisor.py          # Main entry point — owns phase execution and run state
├── ai_doctor.py           # AI diagnostic layer — model-agnostic provider abstraction  
├── phases/
│   ├── preflight.py       # Login, workspace, stream validation
│   ├── find_boundary.py   # Locate correct integration CL in Dev history
│   ├── merge.py           # P4 integrate execution
│   ├── resolve.py         # Safe binary resolve, risky asset flagging
│   ├── sanitize.py        # Junk removal, CL splitting
│   └── report.py          # Submit command generation and human-readable report
├── whitelist.py           # Safe action definitions — what AI is allowed to recommend
├── error_packet.py        # Error context extraction — what gets sent to AI Doctor
└── config.py              # Stream names, batch sizes, model selection
```

## Key Interfaces

### Supervisor

```python
class Supervisor:
    def __init__(self, config: Config):
        self.state = RunState()
        self.ai_doctor = AIDoctor(provider=config.ai_provider)
    
    def run(self):
        for phase in self.phases:
            result = phase.execute()
            if result.failed:
                self.handle_failure(result)
    
    def handle_failure(self, result: PhaseResult):
        packet = ErrorPacket.from_result(result)
        recommendation = self.ai_doctor.diagnose(packet)
        
        if recommendation.requires_human_review:
            self.pause_for_human(recommendation)
        elif recommendation.action in SAFE_WHITELIST:
            self.execute_safe_action(recommendation.action)
        else:
            self.pause_for_human(recommendation)
```

### AI Doctor

```python
class AIDoctor:
    def __init__(self, provider: AIProvider):
        self.provider = provider
    
    def diagnose(self, error_packet: ErrorPacket) -> Recommendation:
        response = self.provider.diagnose(error_packet.to_dict())
        return Recommendation.from_json(response)


class AIProvider:
    def diagnose(self, error_packet: dict) -> dict:
        raise NotImplementedError


class AnthropicProvider(AIProvider):
    def diagnose(self, error_packet: dict) -> dict:
        # Returns structured JSON recommendation
        pass


class OpenAIProvider(AIProvider):
    def diagnose(self, error_packet: dict) -> dict:
        # Returns structured JSON recommendation
        pass
```

### Error Packet

```python
class ErrorPacket:
    phase: str           # Which phase failed
    command: str         # What P4 command was running
    error_type: str      # Classified error category
    last_output: str     # Last N lines of output (not full logs)
    context: dict        # Phase-specific context
    
    def to_dict(self) -> dict:
        # Compact representation sent to AI — not raw logs
        pass
```

### Safe Action Whitelist

```python
SAFE_WHITELIST = {
    "clear_stale_lock",
    "retry_connection",
    "skip_binary_conflict",
    "rerun_resolve",
    "split_changelist",
    "refresh_client_spec",
}

# Actions that always require human approval regardless of AI confidence
HUMAN_REQUIRED = {
    "submit",
    "resolve_source_conflict", 
    "modify_stream_spec",
    "delete_files",
}
```
