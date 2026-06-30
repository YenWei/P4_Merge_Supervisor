from __future__ import annotations

from .doctor_models import DoctorBlockedCase
from .runtime_models import ResumeBundle, VerifierResult


class RecoveryVerifier:
    def verify(
        self,
        *,
        primitive_id: str,
        blocked_case: DoctorBlockedCase,
        execution_result: dict,
        verification_plan: dict | None = None,
    ) -> VerifierResult:
        verification_plan = dict(verification_plan or {})
        if not execution_result.get("executed") or execution_result.get("exit_code") != 0:
            return VerifierResult(
                verification_passed=False,
                failure_reason="recovery command did not complete successfully",
                resume_bundle=None,
                current_opened_file_count=blocked_case.opened_file_count,
                current_unresolved_file_count=blocked_case.unresolved_file_count,
            )

        preserved_change = execution_result.get("recovery_preserved_change")
        if preserved_change in (None, ""):
            preserved_change = blocked_case.staged_change

        resume_from_phase = (
            execution_result.get("retry_phase")
            or verification_plan.get("resume_from_phase")
            or blocked_case.resume_from_phase
        )
        requires_verification = bool(verification_plan.get("requires_verification", True))

        if primitive_id in {"retry_resolve_with_charset_override", "isolate_conflicted_files_and_continue"} and not preserved_change:
            failure_reason = "preserved staged change is missing after recovery execution"
            if primitive_id == "retry_resolve_with_charset_override":
                failure_reason = "preserved staged change is missing after charset retry"
            elif primitive_id == "isolate_conflicted_files_and_continue":
                failure_reason = "preserved staged change is missing after conflict isolation"
            return VerifierResult(
                verification_passed=False,
                failure_reason=failure_reason,
                resume_bundle=None,
                current_opened_file_count=blocked_case.opened_file_count,
                current_unresolved_file_count=blocked_case.unresolved_file_count,
            )

        if not requires_verification and not blocked_case.safe_to_resume:
            return VerifierResult(
                verification_passed=False,
                failure_reason="recovery completed, but the recorded state is not marked safe to resume",
                resume_bundle=None,
                current_opened_file_count=blocked_case.opened_file_count,
                current_unresolved_file_count=blocked_case.unresolved_file_count,
            )

        resume_bundle = ResumeBundle(
            resume_from_phase=resume_from_phase,
            resume_target_change=preserved_change,
            safe_to_resume=bool(preserved_change) and blocked_case.safe_to_resume,
            resume_command=str(execution_result.get("resume_command_override") or blocked_case.resume_command or ""),
            verification_passed=True,
            remaining_risks=[],
        )
        return VerifierResult(
            verification_passed=True,
            failure_reason=None,
            resume_bundle=resume_bundle,
            current_opened_file_count=blocked_case.opened_file_count,
            current_unresolved_file_count=blocked_case.unresolved_file_count,
        )
