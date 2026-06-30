from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class DoctorDecision:
    failure_type: str
    confidence: str
    recommended_action: str
    requires_human_review: bool
    allowed: bool
    doctor_next_action: str
    matched_from: str
    phase_under_review: str
    prior_result: str
    recovery_primitive_id: str | None = None
    safe_to_execute: bool = False
    verification_plan: dict = field(default_factory=dict)
    prior_attempts: list[dict] = field(default_factory=list)

    def to_report_dict(self) -> dict:
        return asdict(self)


@dataclass
class DoctorBlockedCase:
    phase: str
    prior_result: str
    blocker_category: str | None
    reason: str | None
    failing_command: list[str] | None
    source_stream: str | None
    target_stream: str | None
    job_tag: str | None
    p4_cwd: str | None
    selected_cl: int | None
    staged_change: int | None
    batch_changes: list[dict]
    opened_file_count: int
    unresolved_file_count: int
    conflict_buckets: list[dict]
    resume_from_phase: str | None
    safe_to_resume: bool
    resume_command: str
    prior_run_dir: str
    p4_error_excerpt: list[str]
    p4_command_excerpt: list[str]
    allowed_actions: list[str]
    prior_attempts: list[dict] = field(default_factory=list)

    def to_prompt_payload(self) -> dict:
        return asdict(self)


@dataclass
class DoctorLLMDecision:
    failure_type: str
    confidence: float
    recommended_action: str
    reasoning_summary: str
    safe_to_resume: bool
    resume_from_phase: str | None
    needs_human_review: bool
    recovery_primitive_id: str | None = None
    safe_to_execute: bool | None = None
    verification_plan: dict = field(default_factory=dict)
    prior_attempts: list[dict] = field(default_factory=list)

    def to_report_dict(self) -> dict:
        return asdict(self)


@dataclass
class DoctorPolicyDecision:
    final_action: str
    execute_recovery: bool
    pause_cleanly: bool
    allowed: bool
    reason: str
    safe_to_execute: bool = False
    recovery_primitive_id: str | None = None
    verification_plan: dict = field(default_factory=dict)
    prior_attempts: list[dict] = field(default_factory=list)
    policy_level: str = "human-only"

    def to_report_dict(self) -> dict:
        return asdict(self)


@dataclass
class ResumeState:
    blocked_phase: str | None
    resume_from_phase: str | None
    safe_to_resume: bool
    resume_command: str
    selected_cl: int | None
    staged_change: int | None
    pending_changes: list[int]
    last_successful_step: str | None
    failed_step: list[str] | None
    human_action_required: bool
    why_doctor_stopped: str
    prior_run_dir: str

    def to_report_dict(self) -> dict:
        return asdict(self)
