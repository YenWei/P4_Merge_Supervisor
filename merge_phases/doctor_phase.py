from __future__ import annotations

import sys
from pathlib import Path

from merge_supervisor.policy_ladder import PolicyLadder


class DoctorPhase:
    @staticmethod
    def _build_policy_tracking(*, blocked_case, doctor_case: dict, policy_level: str) -> tuple[list[dict], list[dict]]:
        prior_attempts = doctor_case.get("prior_attempts", []) or []
        pattern = None
        for attempt in prior_attempts:
            candidate = attempt.get("pattern")
            if isinstance(candidate, dict):
                pattern = dict(candidate)
                break
        if pattern is None and doctor_case.get("recommended_action") == "accept_source":
            batch_name = "unknown"
            if getattr(blocked_case, "batch_changes", None):
                batch_name = str(blocked_case.batch_changes[0].get("batch") or "unknown")
            pattern = {
                "phase": getattr(blocked_case, "phase", "unknown") or "unknown",
                "batch": batch_name,
                "path_family": "unknown",
                "filetype": "unknown",
                "blocker_type": getattr(blocked_case, "blocker_category", None) or "unknown",
                "suggested_action": doctor_case.get("recommended_action") or "unknown",
            }
        if pattern is None:
            return [], []

        ladder = PolicyLadder()
        for attempt in prior_attempts:
            prior_pattern = attempt.get("pattern")
            human_action = attempt.get("human_action")
            resumed_cleanly = attempt.get("resumed_cleanly")
            if isinstance(prior_pattern, dict) and isinstance(human_action, str) and resumed_cleanly is not None:
                ladder.record_human_outcome(prior_pattern, human_action=human_action, resumed_cleanly=bool(resumed_cleanly))

        effective_level = policy_level
        if effective_level == "human-only":
            effective_level = ladder.classify_pattern(**pattern)
        observation = ladder.build_observation(pattern, policy_level=effective_level, source="doctor")
        promotion_candidate = ladder.build_promotion_candidate(pattern)
        return [observation], [promotion_candidate] if promotion_candidate is not None else []

    @staticmethod
    def _build_doctor_report_payload(
        *,
        prior_run_dir: Path,
        prior_status: dict,
        blocked_case,
        llm_decision,
        llm_mode: str,
        doctor_case: dict,
        policy_decision,
        resume_state: dict,
        execution_result: dict,
    ) -> dict:
        recovery_completed_but_rerun_not_safe = (
            execution_result.get("executed")
            and execution_result.get("exit_code") == 0
            and policy_decision.final_action == "retry_resolve_with_charset_override"
            and not blocked_case.safe_to_resume
        )

        if recovery_completed_but_rerun_not_safe:
            result = "REQUIRES_HUMAN_REVIEW"
            doctor_case["requires_human_review"] = True
            doctor_case["allowed"] = False
            doctor_case["doctor_next_action"] = (
                "Charset recovery step completed, but the run still owns opened files in the default changelist. "
                "Inspect the current workspace state and continue manually instead of rerunning run from scratch."
            )
        elif execution_result.get("executed"):
            if execution_result.get("exit_code") not in (None, 0):
                result = "RECOVERY_EXECUTED_RETRY_FAILED"
            elif execution_result.get("verification_passed") is False:
                result = "RECOVERY_MUTATED_STATE_REQUIRES_REDIAGNOSIS"
                doctor_case["requires_human_review"] = True
                doctor_case["allowed"] = False
                doctor_case["doctor_next_action"] = (
                    "Recovery executed, but verification rejected the resulting workspace state. "
                    "Re-run doctor against the preserved staged change before resuming."
                )
            else:
                result = "RECOVERY_EXECUTED_RETRY_SUCCEEDED"
        else:
            result = "READY_FOR_ACTION" if doctor_case["allowed"] and not doctor_case["requires_human_review"] else "REQUIRES_HUMAN_REVIEW"

        policy_observations, policy_promotion_candidates = DoctorPhase._build_policy_tracking(
            blocked_case=blocked_case,
            doctor_case=doctor_case,
            policy_level=policy_decision.policy_level,
        )

        attempted_primitive = {
            "primitive_id": policy_decision.recovery_primitive_id or doctor_case.get("recovery_primitive_id") or doctor_case["recommended_action"],
            "executed": execution_result.get("executed", False),
            "exit_code": execution_result.get("exit_code"),
            "result_label": result,
            "policy_level": policy_decision.policy_level,
            "policy_observations": policy_observations,
            "policy_promotion_candidates": policy_promotion_candidates,
        }
        runtime_result = {
            "phase_name": "doctor",
            "result_kind": result,
            "next_phase": None,
            "blocked_case_id": f"doctor-{prior_status.get('staged_change') or prior_status.get('selected_cl') or 'latest'}",
            "resume_bundle": execution_result.get("resume_bundle") if execution_result.get("verification_passed") else None,
        }
        if runtime_result["resume_bundle"] is not None:
            runtime_result["next_phase"] = runtime_result["resume_bundle"].get("resume_from_phase")
        elif doctor_case["allowed"] and not doctor_case["requires_human_review"]:
            runtime_result["next_phase"] = resume_state.get("resume_from_phase")

        failure_reason = execution_result.get("failure_reason")
        execution_failure_reason = None
        verification_failure_reason = None
        if execution_result.get("executed") and execution_result.get("exit_code") not in (None, 0):
            execution_failure_reason = failure_reason or execution_result.get("stderr") or execution_result.get("result")
        elif execution_result.get("verification_passed") is False:
            verification_failure_reason = failure_reason

        return {
            "status": result,
            "result": result,
            "blocker_category": None,
            "retryable": False,
            "reason": prior_status.get("reason"),
            "failure_reason": failure_reason,
            "execution_failure_reason": execution_failure_reason,
            "verification_failure_reason": verification_failure_reason,
            "next_action": doctor_case["doctor_next_action"],
            "prior_phase": prior_status.get("phase"),
            "prior_result": prior_status.get("result"),
            "prior_run_dir": str(prior_run_dir),
            "staged_change": prior_status.get("staged_change"),
            "failure_type": doctor_case["failure_type"],
            "confidence": doctor_case["confidence"],
            "recommended_action": doctor_case["recommended_action"],
            "recovery_primitive_id": doctor_case.get("recovery_primitive_id"),
            "safe_to_execute": doctor_case.get("safe_to_execute"),
            "verification_plan": doctor_case.get("verification_plan", {}),
            "prior_attempts": doctor_case.get("prior_attempts", []),
            "requires_human_review": doctor_case["requires_human_review"],
            "allowed": doctor_case["allowed"],
            "matched_from": doctor_case["matched_from"],
            "reasoning_summary": llm_decision.reasoning_summary,
            "policy_final_action": policy_decision.final_action,
            "policy_execute_recovery": policy_decision.execute_recovery,
            "policy_pause_cleanly": policy_decision.pause_cleanly,
            "policy_allowed": policy_decision.allowed,
            "policy_reason": policy_decision.reason,
            "policy_level": policy_decision.policy_level,
            "policy_observations": policy_observations,
            "policy_promotion_candidates": policy_promotion_candidates,
            "recovery_executed": execution_result.get("executed", False),
            "recovery_result": execution_result.get("result"),
            "recovery_command": execution_result.get("command"),
            "recovery_exit_code": execution_result.get("exit_code"),
            "retry_run_dir": execution_result.get("retry_run_dir"),
            "retry_phase": execution_result.get("retry_phase"),
            "retry_result": execution_result.get("retry_result"),
            "recovery_target_scope": execution_result.get("recovery_target_scope"),
            "recovery_target_count": execution_result.get("recovery_target_count", 0),
            "recovery_preserved_change": execution_result.get("recovery_preserved_change"),
            "recovery_isolated_conflict_change": execution_result.get("recovery_isolated_conflict_change"),
            "recovery_isolated_conflict_count": execution_result.get("recovery_isolated_conflict_count", 0),
            "recovery_stdout_excerpt": (execution_result.get("stdout") or "")[:2000],
            "recovery_stderr_excerpt": (execution_result.get("stderr") or "")[:2000],
            "verification_passed": execution_result.get("verification_passed"),
            "verifier_outcome": execution_result.get("verifier_outcome"),
            "resume_bundle": execution_result.get("resume_bundle") if execution_result.get("verification_passed") else None,
            "failing_command": prior_status.get("failing_command"),
            "selected_cl": prior_status.get("selected_cl"),
            "opened_file_count": prior_status.get("opened_file_count", 0),
            "unresolved_file_count": prior_status.get("unresolved_file_count", 0),
            "resume_from_phase": resume_state["resume_from_phase"],
            "safe_to_resume": resume_state["safe_to_resume"],
            "resume_command": execution_result.get("resume_command_override") or resume_state["resume_command"],
            "pending_changes": resume_state["pending_changes"],
            "last_successful_step": resume_state["last_successful_step"],
            "failed_step": resume_state["failed_step"],
            "why_doctor_stopped": resume_state["why_doctor_stopped"],
            "successful_batches": [],
            "failed_batches": [],
            "conflict_buckets": prior_status.get("conflict_buckets", []),
            "llm_doctor_mode": llm_mode,
            "allowed_actions": blocked_case.allowed_actions,
            "runtime_result": runtime_result,
            "attempted_primitive": attempted_primitive,
            "note": "Doctor classified a blocked phase through the LLM-ready scaffold, applied the policy gate, executed a mapped recovery command when allowed, and verified resumability before handoff.",
        }

    def run_doctor(self) -> int:
        import p4_weekly_merge as core

        run_dir = self._new_run_dir()
        report = self._base_report("doctor", run_dir)
        self._dashboard_update(phase="doctor", step="STARTING", status="running")
        try:
            p4_cwd = core.validate_p4_cwd(self.args.p4_cwd)
        except core.P4Error as error:
            classified_error = core.classify_p4_error(str(error))
            report.update(
                {
                    "status": "BLOCKED_REQUIRES_CODEX_OR_USER",
                    "error": classified_error,
                    "result": "BLOCKED_HUMAN",
                    "blocker_category": "invalid_p4_cwd",
                    "retryable": False,
                    "reason": classified_error,
                    "next_action": "Correct --p4-cwd to a valid workspace path, then rerun doctor.",
                    "failure_type": "invalid_p4_cwd",
                    "confidence": "high",
                    "recommended_action": "manual_path_fix_required",
                    "requires_human_review": True,
                    "allowed": False,
                    "prior_phase": None,
                    "prior_result": None,
                    "prior_run_dir": None,
                }
            )
            core.write_doctor_report(run_dir, report)
            self._dashboard_update(step="BLOCKED", status="blocked")
            self._finish("blocked")
            print(f"[BLOCKED] {error}", file=sys.stderr)
            return 20

        report["p4_cwd"] = str(p4_cwd) if p4_cwd else None
        p4 = core.P4Runner(
            cwd=p4_cwd,
            watchdog=self.watchdog,
            progress_callback=self._mark_progress,
            command_callback=self._command_callback,
        )

        try:
            self._dashboard_update(step="INPUT_RESOLUTION")
            prior_run_dir, prior_status = core.determine_doctor_input(self.args)
            resume_state_obj = core.DoctorEngine.build_resume_state(
                prior_run_dir,
                prior_status,
                self.args.runs_dir,
                self.args.p4_cwd,
            )
            blocked_case = core.DoctorEngine.build_blocked_case(prior_run_dir, prior_status, resume_state_obj)
            llm_decision, llm_mode = core.DoctorEngine.diagnose_with_mode(
                blocked_case,
                mode=getattr(self.args, "doctor_mode", "deterministic"),
                model=getattr(self.args, "doctor_model", None),
                base_url=getattr(self.args, "doctor_base_url", None),
                timeout_seconds=getattr(self.args, "doctor_timeout_seconds", 60),
            )
            doctor_case = core.DoctorEngine.llm_decision_to_doctor_decision(blocked_case, llm_decision).to_report_dict()
            policy = core.DoctorPolicy(min_confidence=getattr(self.args, "doctor_min_confidence", 0.85))
            policy_decision = policy.evaluate(
                blocked_case,
                llm_decision,
                execution_enabled=getattr(self.args, "doctor_execute_whitelist", False),
            )
            resume_state = resume_state_obj.to_report_dict()
            execution_result = {
                "executed": False,
                "result": "paused_cleanly",
                "command": None,
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "retry_run_dir": None,
                "retry_phase": None,
                "retry_result": None,
                "recovery_target_scope": None,
                "recovery_target_count": 0,
                "recovery_preserved_change": None,
                "recovery_isolated_conflict_change": None,
                "recovery_isolated_conflict_count": 0,
                "resume_command_override": None,
                "verification_passed": False,
                "failure_reason": None,
                "verifier_outcome": None,
                "resume_bundle": None,
            }
            if policy_decision.execute_recovery:
                self._dashboard_update(step="EXECUTING_RECOVERY")
                executor = core.DoctorExecutor(
                    repo_root=Path(core.__file__).resolve().parent,
                    runs_dir=Path(self.args.runs_dir),
                    timeout_seconds=getattr(self.args, "doctor_retry_timeout_seconds", 900),
                )
                execution_result = executor.execute(blocked_case, policy_decision)

            self._dashboard_update(
                step="CLASSIFYING",
                selected_cl=str(prior_status.get("selected_cl") or ""),
                staged_cl=str(prior_status.get("staged_change") or ""),
            )
            report.update(
                self._build_doctor_report_payload(
                    prior_run_dir=prior_run_dir,
                    prior_status=prior_status,
                    blocked_case=blocked_case,
                    llm_decision=llm_decision,
                    llm_mode=llm_mode,
                    doctor_case=doctor_case,
                    policy_decision=policy_decision,
                    resume_state=resume_state,
                    execution_result=execution_result,
                )
            )
            core.write_doctor_report(run_dir, report, p4)
            self._dashboard_update(step="DONE", status=report["result"])
            self._finish(report["result"])
            print(f"[{report['result']}] prior phase {report['prior_phase']} from {prior_run_dir.name}")
            print(f"Failure type: {report['failure_type']}")
            print(f"Recommended action: {report['recommended_action']}")
            print(f"Next action: {report['next_action']}")
            print(f"Report: {run_dir}")
            return 10 if report["result"] in {"REQUIRES_HUMAN_REVIEW", "RECOVERY_MUTATED_STATE_REQUIRES_REDIAGNOSIS"} else 0
        except (core.P4Error, core.DoctorProviderError) as error:
            classified_error = core.classify_p4_error(str(error))
            blocker_category = "doctor_input_failed"
            failure_type = "doctor_input_failed"
            recommended_action = "manual_doctor_input_review_required"
            next_action = "Inspect prior phase artifacts and doctor inputs manually before retrying doctor."
            if isinstance(error, core.DoctorProviderError):
                blocker_category = "doctor_provider_failed"
                failure_type = "doctor_provider_failed"
                recommended_action = "manual_doctor_provider_review_required"
                next_action = "Fix the doctor provider configuration or API access, then rerun doctor."
            report.update(
                {
                    "status": "BLOCKED_REQUIRES_CODEX_OR_USER",
                    "error": classified_error,
                    "result": "BLOCKED_HUMAN",
                    "blocker_category": blocker_category,
                    "retryable": False,
                    "reason": classified_error,
                    "next_action": next_action,
                    "failure_type": failure_type,
                    "confidence": "medium",
                    "recommended_action": recommended_action,
                    "requires_human_review": True,
                    "allowed": False,
                    "prior_phase": None,
                    "prior_result": None,
                    "prior_run_dir": None,
                    "failing_command": p4.last_command(),
                }
            )
            core.write_doctor_report(run_dir, report, p4)
            self._dashboard_command(p4)
            self._dashboard_update(step="BLOCKED", status="blocked")
            self._finish("blocked")
            print(f"[BLOCKED] {classified_error}", file=sys.stderr)
            print(f"Report: {run_dir}", file=sys.stderr)
            return 20



