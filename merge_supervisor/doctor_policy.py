from __future__ import annotations

from .doctor_models import DoctorBlockedCase, DoctorLLMDecision, DoctorPolicyDecision
from .doctor_provider import ALLOWED_DOCTOR_ACTIONS


class DoctorPolicy:
    def __init__(self, *, min_confidence: float = 0.85):
        self.min_confidence = min_confidence

    @staticmethod
    def _decision(
        llm_decision: DoctorLLMDecision,
        *,
        final_action: str,
        execute_recovery: bool,
        pause_cleanly: bool,
        allowed: bool,
        reason: str,
        policy_level: str,
    ) -> DoctorPolicyDecision:
        return DoctorPolicyDecision(
            final_action=final_action,
            execute_recovery=execute_recovery,
            pause_cleanly=pause_cleanly,
            allowed=allowed,
            reason=reason,
            safe_to_execute=bool(llm_decision.safe_to_execute),
            recovery_primitive_id=llm_decision.recovery_primitive_id or llm_decision.recommended_action,
            verification_plan=dict(llm_decision.verification_plan or {}),
            prior_attempts=list(llm_decision.prior_attempts or []),
            policy_level=policy_level,
        )

    def evaluate(
        self,
        blocked_case: DoctorBlockedCase,
        llm_decision: DoctorLLMDecision,
        *,
        execution_enabled: bool,
    ) -> DoctorPolicyDecision:
        action = llm_decision.recommended_action

        if action not in ALLOWED_DOCTOR_ACTIONS:
            return self._decision(
                llm_decision,
                final_action="pause_cleanly",
                execute_recovery=False,
                pause_cleanly=True,
                allowed=False,
                reason="Recommended action is outside the allowed doctor action enum.",
                policy_level="human-only",
            )

        if action == "pause_cleanly":
            return self._decision(
                llm_decision,
                final_action="pause_cleanly",
                execute_recovery=False,
                pause_cleanly=True,
                allowed=False,
                reason="The doctor explicitly recommended a clean pause.",
                policy_level="human-only",
            )

        if llm_decision.needs_human_review:
            return self._decision(
                llm_decision,
                final_action="pause_cleanly",
                execute_recovery=False,
                pause_cleanly=True,
                allowed=False,
                reason="The doctor indicated this case still needs human review.",
                policy_level="human-only",
            )

        if llm_decision.safe_to_execute is False:
            return self._decision(
                llm_decision,
                final_action="pause_cleanly",
                execute_recovery=False,
                pause_cleanly=True,
                allowed=False,
                reason="The doctor marked this recovery primitive as unsafe to execute automatically.",
                policy_level="human-only",
            )

        if llm_decision.confidence < self.min_confidence:
            return self._decision(
                llm_decision,
                final_action="pause_cleanly",
                execute_recovery=False,
                pause_cleanly=True,
                allowed=False,
                reason=f"Doctor confidence {llm_decision.confidence:.2f} is below the execution threshold {self.min_confidence:.2f}.",
                policy_level="human-only",
            )

        if action == "kill_and_retry_same_phase_after_hang" and blocked_case.phase not in {"dry-run"}:
            return self._decision(
                llm_decision,
                final_action="pause_cleanly",
                execute_recovery=False,
                pause_cleanly=True,
                allowed=False,
                reason="Kill-and-retry after hang is only enabled for dry-run in the first execution cut.",
                policy_level="human-only",
            )

        if action == "retry_resolve_with_charset_override":
            return self._decision(
                llm_decision,
                final_action=action,
                execute_recovery=execution_enabled,
                pause_cleanly=not execution_enabled,
                allowed=True,
                reason="Charset retry is allowed for run-phase blocked resolve cases and can execute when whitelist execution is enabled.",
                policy_level="auto-approved" if execution_enabled else "candidate",
            )

        if action == "isolate_conflicted_files_and_continue":
            if blocked_case.phase != "resolve" or not blocked_case.staged_change:
                return self._decision(
                    llm_decision,
                    final_action="pause_cleanly",
                    execute_recovery=False,
                    pause_cleanly=True,
                    allowed=False,
                    reason="Conflict isolation is only enabled for blocked resolve-phase cases with a recorded staged batch changelist.",
                    policy_level="human-only",
                )
            return self._decision(
                llm_decision,
                final_action=action,
                execute_recovery=execution_enabled,
                pause_cleanly=not execution_enabled,
                allowed=True,
                reason="Conflict isolation is allowed for blocked resolve-phase staged batch changelists and can execute when whitelist execution is enabled.",
                policy_level="auto-approved" if execution_enabled else "candidate",
            )

        if not execution_enabled:
            return self._decision(
                llm_decision,
                final_action=action,
                execute_recovery=False,
                pause_cleanly=True,
                allowed=True,
                reason="Recovery action is allowed by policy, but execution is disabled for this doctor run.",
                policy_level="candidate",
            )

        return self._decision(
            llm_decision,
            final_action=action,
            execute_recovery=True,
            pause_cleanly=False,
            allowed=True,
            reason="Recovery action is allowed, above confidence threshold, and execution is enabled.",
            policy_level="auto-approved",
        )
