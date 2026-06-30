from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Mapping

from .doctor_models import DoctorBlockedCase, ResumeState

PolicyLevel = Literal["auto-approved", "candidate", "shadow-validated", "human-only"]
RuntimeResultKind = Literal[
    "BLOCKED_HUMAN",
    "BLOCKED_RETRYABLE",
    "READY_FOR_REVIEW",
    "READY_NO_CHANGES",
    "READY_TO_RESOLVE",
    "READY_TO_SPLIT",
    "RECOVERY_EXECUTED_RETRY_FAILED",
    "RECOVERY_EXECUTED_RETRY_SUCCEEDED",
    "REVIEW_WITH_CONFLICT_BUCKETS",
    "SPLIT_WITH_CONFLICT_BUCKETS",
]


def _int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _resume_bundle_payload(status_payload: Mapping[str, object]) -> Mapping[str, object] | None:
    nested_payload = status_payload.get("resume_bundle")
    if isinstance(nested_payload, Mapping):
        return nested_payload
    return None


@dataclass
class ResumeBundle:
    resume_from_phase: str | None
    resume_target_change: int | None
    safe_to_resume: bool
    resume_command: str
    verification_passed: bool
    remaining_risks: list[str] = field(default_factory=list)

    @classmethod
    def from_resume_state(
        cls,
        resume_state: ResumeState,
        *,
        verification_passed: bool,
        remaining_risks: list[str] | None = None,
    ) -> ResumeBundle:
        return cls(
            resume_from_phase=resume_state.resume_from_phase,
            resume_target_change=resume_state.staged_change,
            safe_to_resume=resume_state.safe_to_resume,
            resume_command=resume_state.resume_command,
            verification_passed=verification_passed,
            remaining_risks=list(remaining_risks or []),
        )

    @classmethod
    def from_status_payload(
        cls,
        status_payload: Mapping[str, object],
        *,
        verification_passed: bool,
        remaining_risks: list[str] | None = None,
    ) -> ResumeBundle:
        bundle_payload = _resume_bundle_payload(status_payload) or status_payload
        payload_remaining_risks = bundle_payload.get("remaining_risks")
        return cls(
            resume_from_phase=_string_or_none(bundle_payload.get("resume_from_phase")),
            resume_target_change=_int_or_none(bundle_payload.get("resume_target_change") or bundle_payload.get("staged_change") or bundle_payload.get("current_change")),
            safe_to_resume=bool(bundle_payload.get("safe_to_resume", False)),
            resume_command=str(bundle_payload.get("resume_command") or ""),
            verification_passed=bool(bundle_payload.get("verification_passed")) if "verification_passed" in bundle_payload else verification_passed,
            remaining_risks=list(payload_remaining_risks) if isinstance(payload_remaining_risks, list) else list(remaining_risks or []),
        )

    def to_report_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerifierResult:
    verification_passed: bool
    failure_reason: str | None
    resume_bundle: ResumeBundle | None
    current_opened_file_count: int = 0
    current_unresolved_file_count: int = 0

    @classmethod
    def from_resume_state(
        cls,
        resume_state: ResumeState,
        *,
        verification_passed: bool,
        failure_reason: str | None = None,
        current_opened_file_count: int = 0,
        current_unresolved_file_count: int = 0,
        remaining_risks: list[str] | None = None,
    ) -> VerifierResult:
        return cls(
            verification_passed=verification_passed,
            failure_reason=failure_reason,
            resume_bundle=ResumeBundle.from_resume_state(
                resume_state,
                verification_passed=verification_passed,
                remaining_risks=remaining_risks,
            ),
            current_opened_file_count=current_opened_file_count,
            current_unresolved_file_count=current_unresolved_file_count,
        )

    @classmethod
    def from_status_payload(
        cls,
        status_payload: Mapping[str, object],
        *,
        verification_passed: bool,
        failure_reason: str | None = None,
        current_opened_file_count: int | None = None,
        current_unresolved_file_count: int | None = None,
        remaining_risks: list[str] | None = None,
    ) -> VerifierResult:
        resume_bundle_payload = _resume_bundle_payload(status_payload)
        resume_bundle = None
        if resume_bundle_payload is not None:
            resume_bundle = ResumeBundle.from_status_payload(
                status_payload,
                verification_passed=verification_passed,
                remaining_risks=remaining_risks,
            )
        return cls(
            verification_passed=bool(status_payload.get("verification_passed")) if "verification_passed" in status_payload else verification_passed,
            failure_reason=_string_or_none(status_payload.get("failure_reason")) or failure_reason,
            resume_bundle=resume_bundle,
            current_opened_file_count=(
                int(current_opened_file_count)
                if current_opened_file_count is not None
                else int(status_payload.get("current_opened_file_count", status_payload.get("opened_file_count", 0)) or 0)
            ),
            current_unresolved_file_count=(
                int(current_unresolved_file_count)
                if current_unresolved_file_count is not None
                else int(status_payload.get("current_unresolved_file_count", status_payload.get("unresolved_file_count", 0)) or 0)
            ),
        )

    def to_report_dict(self) -> dict:
        return asdict(self)


@dataclass
class PhaseOutcome:
    phase_name: str
    result_kind: RuntimeResultKind | str
    next_phase: str | None
    blocked_case_id: str | None
    resume_bundle: ResumeBundle | None

    @classmethod
    def from_status_payload(
        cls,
        status_payload: Mapping[str, object],
        *,
        next_phase: str | None = None,
        blocked_case_id: str | None = None,
        verification_passed: bool | None = None,
        remaining_risks: list[str] | None = None,
    ) -> PhaseOutcome:
        resume_bundle_payload = _resume_bundle_payload(status_payload)
        resume_bundle = None
        if resume_bundle_payload is not None:
            resume_bundle = ResumeBundle.from_status_payload(
                status_payload,
                verification_passed=False if verification_passed is None else verification_passed,
                remaining_risks=remaining_risks,
            )
        return cls(
            phase_name=str(status_payload.get("phase") or status_payload.get("phase_name") or "unknown"),
            result_kind=str(status_payload.get("result") or status_payload.get("result_kind") or "unknown"),
            next_phase=_string_or_none(status_payload.get("next_phase")) or next_phase,
            blocked_case_id=_string_or_none(status_payload.get("blocked_case_id")) or blocked_case_id,
            resume_bundle=resume_bundle,
        )

    def to_report_dict(self) -> dict:
        payload = asdict(self)
        payload["result"] = self.result_kind
        return payload


@dataclass
class BlockedCaseSnapshot:
    blocked_phase: str
    phase_result: RuntimeResultKind | str
    blocker_category: str | None
    failing_command: list[str] | None
    selected_cl: int | None
    staged_change: int | None
    opened_file_count: int
    unresolved_file_count: int
    sample_file_paths: list[str] = field(default_factory=list)
    allowed_recovery_primitives: list[str] = field(default_factory=list)

    @classmethod
    def from_blocked_case(
        cls,
        blocked_case: DoctorBlockedCase,
        *,
        sample_file_paths: list[str] | None = None,
    ) -> BlockedCaseSnapshot:
        return cls(
            blocked_phase=blocked_case.phase,
            phase_result=blocked_case.prior_result,
            blocker_category=blocked_case.blocker_category,
            failing_command=list(blocked_case.failing_command) if blocked_case.failing_command is not None else None,
            selected_cl=blocked_case.selected_cl,
            staged_change=blocked_case.staged_change,
            opened_file_count=blocked_case.opened_file_count,
            unresolved_file_count=blocked_case.unresolved_file_count,
            sample_file_paths=list(sample_file_paths or []),
            allowed_recovery_primitives=list(blocked_case.allowed_actions),
        )

    @classmethod
    def from_status_payload(
        cls,
        status_payload: Mapping[str, object],
        *,
        sample_file_paths: list[str] | None = None,
    ) -> BlockedCaseSnapshot:
        failing_command = status_payload.get("failing_command")
        return cls(
            blocked_phase=str(status_payload.get("phase") or "unknown"),
            phase_result=str(status_payload.get("result") or status_payload.get("prior_result") or "unknown"),
            blocker_category=_string_or_none(status_payload.get("blocker_category")),
            failing_command=list(failing_command) if isinstance(failing_command, list) else None,
            selected_cl=_int_or_none(status_payload.get("selected_cl")),
            staged_change=_int_or_none(status_payload.get("staged_change")),
            opened_file_count=int(status_payload.get("opened_file_count", 0) or 0),
            unresolved_file_count=int(status_payload.get("unresolved_file_count", 0) or 0),
            sample_file_paths=list(sample_file_paths or []),
            allowed_recovery_primitives=list(status_payload.get("allowed_actions", []) or []),
        )

    def to_report_dict(self) -> dict:
        return asdict(self)


@dataclass
class RecoveryExecutionResult:
    primitive_id: str
    executed: bool
    exit_code: int | None
    result_label: RuntimeResultKind | str
    policy_level: PolicyLevel = "human-only"

    @classmethod
    def from_status_payload(
        cls,
        status_payload: Mapping[str, object],
        *,
        policy_level: PolicyLevel = "human-only",
    ) -> RecoveryExecutionResult:
        return cls(
            primitive_id=str(
                status_payload.get("primitive_id")
                or status_payload.get("recommended_action")
                or status_payload.get("policy_final_action")
                or "unknown"
            ),
            executed=bool(status_payload.get("executed", status_payload.get("recovery_executed", False))),
            exit_code=_int_or_none(status_payload.get("exit_code", status_payload.get("recovery_exit_code"))),
            result_label=str(status_payload.get("result_label") or status_payload.get("result") or status_payload.get("recovery_result") or "unknown"),
            policy_level=str(status_payload.get("policy_level") or policy_level),
        )

    def to_report_dict(self) -> dict:
        return asdict(self)


__all__ = [
    "BlockedCaseSnapshot",
    "PhaseOutcome",
    "PolicyLevel",
    "RecoveryExecutionResult",
    "ResumeBundle",
    "RuntimeResultKind",
    "VerifierResult",
]
