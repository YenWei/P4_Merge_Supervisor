from __future__ import annotations

import json
from pathlib import Path

from .doctor_models import DoctorBlockedCase, DoctorDecision, DoctorLLMDecision, ResumeState
from .doctor_provider import ALLOWED_DOCTOR_ACTIONS, DoctorProviderError, OllamaDoctorProvider, OpenAIDoctorProvider


class DoctorEngine:
    @staticmethod
    def _read_excerpt(path: Path, limit: int = 20) -> list[str]:
        if not path.exists():
            return []
        lines: list[str] = []
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.rstrip()
                if not line:
                    continue
                lines.append(line)
                if len(lines) >= limit:
                    break
        return lines

    @staticmethod
    def find_latest_doctor_input(runs_dir: Path, staged_change: int | None = None) -> tuple[Path, dict] | tuple[None, None]:
        status_files = sorted(runs_dir.glob("*/status.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for status_file in status_files:
            status = json.loads(status_file.read_text(encoding="utf-8"))
            if status.get("phase") == "doctor":
                continue
            if status.get("result") not in {"BLOCKED_RETRYABLE", "BLOCKED_HUMAN"}:
                continue
            if staged_change is not None and status.get("staged_change") != staged_change:
                continue
            return status_file.parent, status
        return None, None

    @staticmethod
    def determine_doctor_input(runs_dir: Path, staged_change: int | None, error_type) -> tuple[Path, dict]:
        run_dir, status = DoctorEngine.find_latest_doctor_input(runs_dir, staged_change=staged_change)
        if status is None:
            if staged_change is None:
                raise error_type("Could not find a blocked prior phase status.json for doctor.")
            raise error_type(f"Could not find a blocked prior phase status.json for staged pending changelist {staged_change}.")
        if status.get("result") not in {"BLOCKED_RETRYABLE", "BLOCKED_HUMAN"}:
            raise error_type("Doctor only runs against blocked phase artifacts.")
        return run_dir, status

    @staticmethod
    def classify_case(
        phase: str,
        result: str,
        blocker_category: str | None,
        reason: str | None,
        failing_command: list[str] | None,
        *,
        staged_change: int | None = None,
        batch_changes: list[dict] | None = None,
        opened_file_count: int = 0,
    ) -> DoctorDecision:
        lower_reason = (reason or "").lower()
        category = blocker_category or "unknown"
        has_staged_batch_scope = bool(staged_change or batch_changes)

        if "can't translate" in lower_reason or "translation of file content failed" in lower_reason or "unicode" in lower_reason:
            return DoctorDecision(
                "resolve_charset",
                "high" if phase == "resolve" else "medium",
                "retry_resolve_with_charset_override",
                False,
                True,
                "Apply the approved charset recovery path, then rerun the blocked phase.",
                "reason",
                phase,
                result,
            )

        if category == "resolve_failed" and phase == "resolve" and has_staged_batch_scope:
            return DoctorDecision(
                "resolve_failed",
                "high",
                "isolate_conflicted_files_and_continue",
                False,
                True,
                "Isolate the unresolved files into a dedicated conflict changelist, then retry resolve on the preserved staged batch changelist.",
                "blocker_category",
                phase,
                result,
            )

        exact_category_rules = {
            "p4_auth": DoctorDecision("p4_auth", "high", "retry_after_login_refresh", False, True, "Run p4 login, then rerun the blocked phase.", "blocker_category", phase, result),
            "p4_connectivity": DoctorDecision("p4_connectivity", "high", "retry_after_connectivity_restore", False, True, "Restore Perforce connectivity, then rerun the blocked phase.", "blocker_category", phase, result),
            "p4_connectivity_blocked": DoctorDecision("p4_connectivity", "high", "retry_after_connectivity_restore", False, True, "Rerun with approved network access, then rerun the blocked phase.", "blocker_category", phase, result),
            "p4_env_missing": DoctorDecision("p4_env_missing", "high", "retry_after_env_restore", False, True, "Restore P4PORT/P4USER/P4CLIENT or use the wrapper, then rerun the blocked phase.", "blocker_category", phase, result),
            "resolve_charset": DoctorDecision("resolve_charset", "high", "retry_resolve_with_charset_override", False, True, "Apply the approved charset recovery path, then rerun the blocked phase.", "blocker_category", phase, result),
            "suspected_hang": DoctorDecision("suspected_hang", "medium", "kill_and_retry_same_phase_after_hang", False, False, "Inspect the clean checkpoint and decide whether the blocked phase should be killed and retried from the recorded resume point.", "blocker_category", phase, result),
            "dirty_default_cl": DoctorDecision("dirty_default_cl", "high", "manual_workspace_cleanup_required", True, False, "Clean or move the existing opened files manually before rerunning the blocked phase.", "blocker_category", phase, result),
            "wrong_client_stream": DoctorDecision("wrong_client_stream", "high", "manual_workspace_selection_required", True, False, "Switch to the workspace mapped to the intended target stream before rerunning the blocked phase.", "blocker_category", phase, result),
            "wrong_client_type": DoctorDecision("wrong_client_type", "high", "manual_workspace_selection_required", True, False, "Switch to the intended stream client before rerunning the blocked phase.", "blocker_category", phase, result),
            "invalid_p4_cwd": DoctorDecision("invalid_p4_cwd", "high", "manual_path_fix_required", True, False, "Correct --p4-cwd to a valid workspace path before rerunning the blocked phase.", "blocker_category", phase, result),
            "p4_missing": DoctorDecision("p4_missing", "high", "manual_tool_install_required", True, False, "Install the p4 CLI or fix PATH before rerunning the blocked phase.", "blocker_category", phase, result),
            "missing_source_boundary": DoctorDecision("missing_source_boundary", "high", "manual_boundary_selection_required", True, False, "Confirm the source boundary rule or job tag before rerunning the blocked phase.", "blocker_category", phase, result),
            "invalid_batch": DoctorDecision("invalid_batch", "high", "manual_batch_fix_required", True, False, "Correct the requested batch names before rerunning the blocked phase.", "blocker_category", phase, result),
            "partial_merge_failure": DoctorDecision("partial_merge_failure", "high", "manual_partial_merge_review_required", True, False, "Inspect the partially staged workspace manually before retrying the run phase.", "blocker_category", phase, result),
            "merge_failed": DoctorDecision("merge_failed", "medium", "manual_merge_investigation_required", True, False, "Inspect the failing merge command and workspace state before retrying the run phase.", "blocker_category", phase, result),
            "resolve_failed": DoctorDecision("resolve_failed", "medium", "manual_resolve_investigation_required", True, False, "Inspect the resolve failure and current staged state before retrying the run phase.", "blocker_category", phase, result),
            "opened_snapshot_failed": DoctorDecision("opened_snapshot_failed", "medium", "manual_workspace_investigation_required", True, False, "Inspect the workspace state manually before continuing.", "blocker_category", phase, result),
            "split_failed": DoctorDecision("split_failed", "medium", "manual_split_investigation_required", True, False, "Inspect the split input changelist and artifacts manually before retrying split.", "blocker_category", phase, result),
            "sanitize_failed": DoctorDecision("sanitize_failed", "medium", "manual_sanitize_investigation_required", True, False, "Inspect the sanitize inputs and split changelists manually before retrying sanitize.", "blocker_category", phase, result),
        }
        if category in exact_category_rules:
            return exact_category_rules[category]

        if "wsaeacces" in lower_reason or "forbidden by its access permissions" in lower_reason:
            return DoctorDecision("p4_connectivity", "medium", "retry_after_connectivity_restore", False, True, "Rerun with approved network access, then rerun the blocked phase.", "reason", phase, result)
        if "tcp connect to perforce:1666 failed" in lower_reason:
            return DoctorDecision("p4_env_missing", "medium", "retry_after_env_restore", False, True, "Restore P4 environment variables or use the wrapper, then rerun the blocked phase.", "reason", phase, result)
        if failing_command and len(failing_command) >= 2 and failing_command[1] == "resolve":
            return DoctorDecision("resolve_failed", "low", "manual_resolve_investigation_required", True, False, "Inspect the resolve failure manually before retrying the blocked phase.", "failing_command", phase, result)
        return DoctorDecision("unknown", "low", "manual_investigation_required", True, False, "Inspect the run artifacts manually before deciding how to continue.", "fallback", phase, result)

    @staticmethod
    def confidence_label(value: float) -> str:
        if value >= 0.85:
            return "high"
        if value >= 0.55:
            return "medium"
        return "low"

    @staticmethod
    def action_allowed_in_scaffold(action: str) -> bool:
        return action in {
            "retry_after_login_refresh",
            "retry_after_connectivity_restore",
            "retry_after_env_restore",
            "retry_resolve_with_charset_override",
            "isolate_conflicted_files_and_continue",
        }

    @staticmethod
    def default_verification_plan(recommended_action: str, resume_from_phase: str | None) -> dict:
        if recommended_action == "retry_resolve_with_charset_override":
            return {
                "strategy_id": "verify_charset_retry",
                "requires_verification": True,
                "resume_from_phase": resume_from_phase,
                "checks": [
                    "rerun resolve against the preserved staged change",
                    "confirm unresolved file count does not increase",
                ],
            }
        if recommended_action == "isolate_conflicted_files_and_continue":
            return {
                "strategy_id": "verify_conflict_isolation",
                "requires_verification": True,
                "resume_from_phase": resume_from_phase,
                "checks": [
                    "confirm unresolved files moved to the isolated conflict changelist",
                    "confirm the preserved staged change is still resumable",
                ],
            }
        if recommended_action == "pause_cleanly":
            return {
                "strategy_id": "manual_review_handoff",
                "requires_verification": False,
                "resume_from_phase": resume_from_phase,
                "checks": ["human reviews the blocked artifacts before resuming"],
            }
        return {
            "strategy_id": "verify_recovery_before_resume",
            "requires_verification": True,
            "resume_from_phase": resume_from_phase,
            "checks": ["confirm the recovery action produced a clean resumable state"],
        }

    @staticmethod
    def _coerce_prior_attempts(prior_status: dict) -> list[dict]:
        raw_attempts = prior_status.get("prior_attempts")
        if isinstance(raw_attempts, list):
            return [dict(item) for item in raw_attempts if isinstance(item, dict)]

        attempted_primitive = prior_status.get("attempted_primitive")
        if isinstance(attempted_primitive, dict):
            verifier_outcome = prior_status.get("verifier_outcome")
            failure_reason = None
            if isinstance(verifier_outcome, dict):
                failure_reason = verifier_outcome.get("failure_reason")
            return [
                {
                    "primitive_id": attempted_primitive.get("primitive_id") or prior_status.get("recommended_action") or "unknown",
                    "outcome": (
                        "succeeded"
                        if attempted_primitive.get("executed") and attempted_primitive.get("exit_code") in (None, 0)
                        else "failed"
                        if attempted_primitive.get("executed")
                        else "proposed"
                    ),
                    "result_label": attempted_primitive.get("result_label") or prior_status.get("result"),
                    "failure_reason": failure_reason or prior_status.get("reason"),
                }
            ]
        return []

    @staticmethod
    def build_blocked_case(prior_run_dir: Path, prior_status: dict, resume_state: ResumeState) -> DoctorBlockedCase:
        return DoctorBlockedCase(
            phase=prior_status.get("phase", "unknown"),
            prior_result=prior_status.get("result", "unknown"),
            blocker_category=prior_status.get("blocker_category"),
            reason=prior_status.get("reason"),
            failing_command=prior_status.get("failing_command"),
            source_stream=prior_status.get("source_stream"),
            target_stream=prior_status.get("target_stream"),
            job_tag=prior_status.get("job_tag"),
            p4_cwd=prior_status.get("p4_cwd"),
            selected_cl=prior_status.get("selected_cl"),
            staged_change=prior_status.get("staged_change"),
            batch_changes=list(prior_status.get("batch_changes", []) or []),
            opened_file_count=int(prior_status.get("opened_file_count", 0) or 0),
            unresolved_file_count=int(prior_status.get("unresolved_file_count", 0) or 0),
            conflict_buckets=list(prior_status.get("conflict_buckets", []) or []),
            resume_from_phase=resume_state.resume_from_phase,
            safe_to_resume=resume_state.safe_to_resume,
            resume_command=resume_state.resume_command,
            prior_run_dir=str(prior_run_dir),
            p4_error_excerpt=DoctorEngine._read_excerpt(prior_run_dir / "p4-errors.log"),
            p4_command_excerpt=DoctorEngine._read_excerpt(prior_run_dir / "p4-commands.log"),
            allowed_actions=list(ALLOWED_DOCTOR_ACTIONS),
            prior_attempts=DoctorEngine._coerce_prior_attempts(prior_status),
        )

    @staticmethod
    def build_deterministic_llm_decision(blocked_case: DoctorBlockedCase) -> DoctorLLMDecision:
        deterministic = DoctorEngine.classify_case(
            blocked_case.phase,
            blocked_case.prior_result,
            blocked_case.blocker_category,
            blocked_case.reason,
            blocked_case.failing_command,
            staged_change=blocked_case.staged_change,
            batch_changes=blocked_case.batch_changes,
            opened_file_count=blocked_case.opened_file_count,
        )
        recommended_action = deterministic.recommended_action if deterministic.recommended_action in ALLOWED_DOCTOR_ACTIONS else "pause_cleanly"
        confidence_map = {"high": 0.95, "medium": 0.7, "low": 0.35}
        safe_to_execute = bool(
            deterministic.allowed
            and not deterministic.requires_human_review
            and blocked_case.safe_to_resume
            and recommended_action != "pause_cleanly"
        )
        return DoctorLLMDecision(
            failure_type=deterministic.failure_type,
            confidence=confidence_map.get(deterministic.confidence, 0.35),
            recommended_action=recommended_action,
            reasoning_summary=deterministic.doctor_next_action,
            safe_to_resume=blocked_case.safe_to_resume,
            resume_from_phase=blocked_case.resume_from_phase,
            needs_human_review=deterministic.requires_human_review,
            recovery_primitive_id=recommended_action,
            safe_to_execute=safe_to_execute,
            verification_plan=DoctorEngine.default_verification_plan(recommended_action, blocked_case.resume_from_phase),
            prior_attempts=list(blocked_case.prior_attempts),
        )

    @staticmethod
    def diagnose_with_mode(
        blocked_case: DoctorBlockedCase,
        *,
        mode: str,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int = 60,
    ) -> tuple[DoctorLLMDecision, str]:
        normalized_mode = (mode or "deterministic").strip().lower()
        if normalized_mode == "deterministic":
            return DoctorEngine.build_deterministic_llm_decision(blocked_case), "deterministic_scaffold"
        if normalized_mode == "openai":
            provider = OpenAIDoctorProvider(model=model, base_url=base_url, timeout_seconds=timeout_seconds)
            return provider.diagnose(blocked_case), "openai"
        if normalized_mode == "ollama":
            provider = OllamaDoctorProvider(model=model, base_url=base_url, timeout_seconds=timeout_seconds)
            return provider.diagnose(blocked_case), "ollama"
        raise DoctorProviderError(f"Unsupported doctor mode: {mode!r}")

    @staticmethod
    def llm_decision_to_doctor_decision(blocked_case: DoctorBlockedCase, llm_decision: DoctorLLMDecision) -> DoctorDecision:
        recommended_action = llm_decision.recommended_action
        recovery_primitive_id = llm_decision.recovery_primitive_id or recommended_action
        confidence = DoctorEngine.confidence_label(llm_decision.confidence)
        allowed = DoctorEngine.action_allowed_in_scaffold(recommended_action)
        safe_to_execute = bool(
            llm_decision.safe_to_execute
            if llm_decision.safe_to_execute is not None
            else allowed and llm_decision.safe_to_resume and recommended_action != "pause_cleanly"
        )
        requires_human_review = (
            llm_decision.needs_human_review
            or not allowed
            or recommended_action == "pause_cleanly"
            or not safe_to_execute
        )

        next_action_map = {
            "retry_after_login_refresh": "Run p4 login, then rerun the blocked phase.",
            "retry_after_connectivity_restore": "Restore Perforce connectivity, then rerun the blocked phase.",
            "retry_after_env_restore": "Restore P4PORT/P4USER/P4CLIENT or use the wrapper, then rerun the blocked phase.",
            "retry_resolve_with_charset_override": "Apply the approved charset recovery path, then rerun the blocked phase.",
            "kill_and_retry_same_phase_after_hang": "Inspect the clean checkpoint and decide whether the blocked phase should be killed and retried from the recorded resume point.",
            "isolate_conflicted_files_and_continue": "Isolate the unresolved files into a dedicated conflict changelist, then retry the same staged batch changelist.",
            "pause_cleanly": "Pause cleanly and use the recorded resume command or manual recovery notes.",
        }
        verification_plan = dict(llm_decision.verification_plan or DoctorEngine.default_verification_plan(recovery_primitive_id, llm_decision.resume_from_phase))
        return DoctorDecision(
            failure_type=llm_decision.failure_type,
            confidence=confidence,
            recommended_action=recommended_action,
            requires_human_review=requires_human_review,
            allowed=allowed and not llm_decision.needs_human_review and safe_to_execute,
            doctor_next_action=next_action_map.get(recommended_action, "Inspect the run artifacts manually before deciding how to continue."),
            matched_from="llm_scaffold",
            phase_under_review=blocked_case.phase,
            prior_result=blocked_case.prior_result,
            recovery_primitive_id=recovery_primitive_id,
            safe_to_execute=safe_to_execute,
            verification_plan=verification_plan,
            prior_attempts=list(llm_decision.prior_attempts or blocked_case.prior_attempts),
        )

    @staticmethod
    def quote_command_arg(value: str) -> str:
        if any(char.isspace() for char in value) or '"' in value:
            return '"' + value.replace('"', '\\"') + '"'
        return value

    @staticmethod
    def build_command_string(parts: list[str]) -> str:
        return " ".join(DoctorEngine.quote_command_arg(part) for part in parts)

    @staticmethod
    def build_resume_state(prior_run_dir: Path, prior_status: dict, runs_dir: str | None, p4_cwd: str | None) -> ResumeState:
        phase = prior_status.get("phase")
        staged_change = prior_status.get("staged_change")
        base_args: list[str] = []
        if runs_dir:
            base_args.extend(["--runs-dir", str(runs_dir)])
        if p4_cwd:
            base_args.extend(["--p4-cwd", str(p4_cwd)])

        resume_from_phase = phase
        safe_to_resume = False
        human_action_required = True
        why_doctor_stopped = "Doctor did not find a clean resumable state yet."
        resume_command = ""

        if phase == "dry-run":
            safe_to_resume = True
            human_action_required = False
            why_doctor_stopped = "Dry-run is read-only and can be rerun safely."
            resume_command = DoctorEngine.build_command_string(["python", ".\\p4_weekly_merge.py", "dry-run", *base_args])
        elif phase == "run":
            if staged_change:
                resume_from_phase = "resolve"
                safe_to_resume = True
                human_action_required = False
                why_doctor_stopped = "Run left staged batch changelists that can continue from resolve."
                resume_command = DoctorEngine.build_command_string(["python", ".\\p4_weekly_merge.py", "resolve", "--change", str(staged_change), *base_args])
            else:
                why_doctor_stopped = "Run blocked before a resumable staged pending changelist was recorded."
                resume_command = DoctorEngine.build_command_string(["python", ".\\p4_weekly_merge.py", "run", *base_args])
        elif phase == "resolve":
            if staged_change:
                safe_to_resume = True
                human_action_required = False
                why_doctor_stopped = "Resolve can retry against the existing staged batch changelist."
                resume_command = DoctorEngine.build_command_string(["python", ".\\p4_weekly_merge.py", "resolve", "--change", str(staged_change), *base_args])
            else:
                why_doctor_stopped = "Resolve did not record a staged batch changelist to resume from."
        elif phase == "split":
            if staged_change:
                safe_to_resume = True
                human_action_required = False
                why_doctor_stopped = "Split can retry against the existing staged pending changelist."
                resume_command = DoctorEngine.build_command_string(["python", ".\\p4_weekly_merge.py", "split", "--change", str(staged_change), *base_args])
            else:
                why_doctor_stopped = "Split did not record a staged pending changelist to resume from."
        elif phase == "sanitize":
            if staged_change:
                safe_to_resume = True
                human_action_required = False
                why_doctor_stopped = "Sanitize can retry against the existing staged pending changelist."
                resume_command = DoctorEngine.build_command_string(["python", ".\\p4_weekly_merge.py", "sanitize", "--change", str(staged_change), *base_args])
            else:
                why_doctor_stopped = "Sanitize did not record a staged pending changelist to resume from."

        pending_changes = [staged_change] if staged_change else []
        return ResumeState(
            blocked_phase=phase,
            resume_from_phase=resume_from_phase,
            safe_to_resume=safe_to_resume,
            resume_command=resume_command,
            selected_cl=prior_status.get("selected_cl"),
            staged_change=staged_change,
            pending_changes=pending_changes,
            last_successful_step=prior_status.get("note"),
            failed_step=prior_status.get("failing_command"),
            human_action_required=human_action_required,
            why_doctor_stopped=why_doctor_stopped,
            prior_run_dir=str(prior_run_dir),
        )

    @staticmethod
    def write_resume_artifacts(run_dir: Path, resume_state: dict | ResumeState) -> None:
        state_dict = resume_state.to_report_dict() if isinstance(resume_state, ResumeState) else dict(resume_state)
        (run_dir / "resume.json").write_text(json.dumps(state_dict, indent=2), encoding="utf-8")
        lines = [
            f"blocked phase: {state_dict.get('blocked_phase') or 'unknown'}",
            f"resume from phase: {state_dict.get('resume_from_phase') or 'unknown'}",
            f"safe to resume: {'yes' if state_dict.get('safe_to_resume') else 'no'}",
            f"selected CL: {state_dict.get('selected_cl') or '-'}",
            f"staged change: {state_dict.get('staged_change') or '-'}",
            "",
            "why doctor stopped:",
            state_dict.get("why_doctor_stopped") or "none",
            "",
            "resume command:",
            state_dict.get("resume_command") or "none",
        ]
        (run_dir / "resume.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def write_doctor_report(run_dir: Path, report: dict, write_status_artifacts, write_command_logs, p4=None) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        doctor_summary = {
            "status": report["status"],
            "phase": report["phase"],
            "prior_phase": report.get("prior_phase"),
            "prior_result": report.get("prior_result"),
            "prior_run_dir": report.get("prior_run_dir"),
            "staged_change": report.get("staged_change"),
            "failure_type": report.get("failure_type"),
            "confidence": report.get("confidence"),
            "recommended_action": report.get("recommended_action"),
            "recovery_primitive_id": report.get("recovery_primitive_id"),
            "safe_to_execute": report.get("safe_to_execute"),
            "verification_plan": report.get("verification_plan", {}),
            "prior_attempts": report.get("prior_attempts", []),
            "requires_human_review": report.get("requires_human_review"),
            "allowed": report.get("allowed"),
            "matched_from": report.get("matched_from"),
            "reasoning_summary": report.get("reasoning_summary"),
            "llm_doctor_mode": report.get("llm_doctor_mode"),
            "allowed_actions": report.get("allowed_actions", []),
            "policy_final_action": report.get("policy_final_action"),
            "policy_execute_recovery": report.get("policy_execute_recovery"),
            "policy_pause_cleanly": report.get("policy_pause_cleanly"),
            "policy_allowed": report.get("policy_allowed"),
            "policy_reason": report.get("policy_reason"),
            "policy_level": report.get("policy_level"),
            "recovery_executed": report.get("recovery_executed"),
            "recovery_result": report.get("recovery_result"),
            "recovery_command": report.get("recovery_command"),
            "recovery_exit_code": report.get("recovery_exit_code"),
            "retry_run_dir": report.get("retry_run_dir"),
            "retry_phase": report.get("retry_phase"),
            "retry_result": report.get("retry_result"),
            "recovery_target_scope": report.get("recovery_target_scope"),
            "recovery_target_count": report.get("recovery_target_count"),
            "recovery_stdout_excerpt": report.get("recovery_stdout_excerpt"),
            "recovery_stderr_excerpt": report.get("recovery_stderr_excerpt"),
            "reason": report.get("reason"),
            "next_action": report.get("next_action"),
            "resume_from_phase": report.get("resume_from_phase"),
            "safe_to_resume": report.get("safe_to_resume"),
            "resume_command": report.get("resume_command"),
            "why_doctor_stopped": report.get("why_doctor_stopped"),
            "runtime_result": report.get("runtime_result"),
            "attempted_primitive": report.get("attempted_primitive"),
            "timestamp": report.get("timestamp"),
        }
        (run_dir / "doctor-summary.json").write_text(json.dumps(doctor_summary, indent=2), encoding="utf-8")

        summary_lines = [
            f"status: {report['status']}",
            f"prior phase: {report.get('prior_phase')}",
            f"prior result: {report.get('prior_result')}",
            f"staged pending CL: {report.get('staged_change')}",
            f"failure type: {report.get('failure_type', 'unknown')}",
            f"confidence: {report.get('confidence', 'unknown')}",
            f"recommended action: {report.get('recommended_action', 'none')}",
            f"recovery primitive id: {report.get('recovery_primitive_id') or 'none'}",
            f"safe to execute: {'yes' if report.get('safe_to_execute') else 'no'}",
            f"verification strategy: {(report.get('verification_plan') or {}).get('strategy_id') or 'none'}",
            f"prior attempts: {len(report.get('prior_attempts') or [])}",
            f"requires human review: {'yes' if report.get('requires_human_review') else 'no'}",
            f"allowed: {'yes' if report.get('allowed') else 'no'}",
            f"matched from: {report.get('matched_from', 'unknown')}",
            f"llm doctor mode: {report.get('llm_doctor_mode', 'unknown')}",
            f"policy final action: {report.get('policy_final_action', 'unknown')}",
            f"policy level: {report.get('policy_level') or 'unknown'}",
            f"policy execute recovery: {'yes' if report.get('policy_execute_recovery') else 'no'}",
            f"recovery executed: {'yes' if report.get('recovery_executed') else 'no'}",
            f"recovery result: {report.get('recovery_result', 'none')}",
            f"retry run dir: {report.get('retry_run_dir') or 'none'}",
            f"retry phase: {report.get('retry_phase') or 'none'}",
            f"retry result: {report.get('retry_result') or 'none'}",
            f"recovery target scope: {report.get('recovery_target_scope') or 'none'}",
            f"recovery target count: {report.get('recovery_target_count') or 0}",
            f"resume from phase: {report.get('resume_from_phase') or 'unknown'}",
            f"safe to resume: {'yes' if report.get('safe_to_resume') else 'no'}",
            "",
            "reasoning summary:",
            report.get("reasoning_summary") or "none",
            "",
            "policy reason:",
            report.get("policy_reason") or "none",
            "",
            "reason:",
            report.get("reason") or "none",
            "",
            "next action:",
            report.get("next_action") or "none",
            "",
            "verification checks:",
            *(report.get("verification_plan", {}).get("checks") or ["none"]),
            "",
            "recovery command:",
            report.get("recovery_command") or "none",
            "",
            "recovery stdout excerpt:",
            report.get("recovery_stdout_excerpt") or "none",
            "",
            "recovery stderr excerpt:",
            report.get("recovery_stderr_excerpt") or "none",
            "",
            "resume command:",
            report.get("resume_command") or "none",
        ]
        if "error" in report:
            summary_lines.extend(["", f"error: {report['error']}"])
        (run_dir / "doctor-summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

        doctor_decision = {
            "llm_doctor_mode": report.get("llm_doctor_mode"),
            "failure_type": report.get("failure_type"),
            "confidence": report.get("confidence"),
            "recommended_action": report.get("recommended_action"),
            "recovery_primitive_id": report.get("recovery_primitive_id"),
            "safe_to_execute": report.get("safe_to_execute"),
            "verification_plan": report.get("verification_plan", {}),
            "prior_attempts": report.get("prior_attempts", []),
            "reasoning_summary": report.get("reasoning_summary"),
            "policy_final_action": report.get("policy_final_action"),
            "policy_execute_recovery": report.get("policy_execute_recovery"),
            "policy_pause_cleanly": report.get("policy_pause_cleanly"),
            "policy_allowed": report.get("policy_allowed"),
            "policy_reason": report.get("policy_reason"),
            "policy_level": report.get("policy_level"),
            "recovery_executed": report.get("recovery_executed"),
            "recovery_result": report.get("recovery_result"),
            "recovery_command": report.get("recovery_command"),
            "recovery_exit_code": report.get("recovery_exit_code"),
            "retry_run_dir": report.get("retry_run_dir"),
            "retry_phase": report.get("retry_phase"),
            "retry_result": report.get("retry_result"),
            "recovery_target_scope": report.get("recovery_target_scope"),
            "recovery_target_count": report.get("recovery_target_count"),
            "runtime_result": report.get("runtime_result"),
            "attempted_primitive": report.get("attempted_primitive"),
        }
        (run_dir / "doctor-decision.json").write_text(json.dumps(doctor_decision, indent=2), encoding="utf-8")

        decision_lines = [
            f"mode: {doctor_decision.get('llm_doctor_mode') or 'unknown'}",
            f"failure type: {doctor_decision.get('failure_type') or 'unknown'}",
            f"confidence: {doctor_decision.get('confidence') or 'unknown'}",
            f"recommended action: {doctor_decision.get('recommended_action') or 'none'}",
            f"recovery primitive id: {doctor_decision.get('recovery_primitive_id') or 'none'}",
            f"safe to execute: {'yes' if doctor_decision.get('safe_to_execute') else 'no'}",
            f"verification strategy: {(doctor_decision.get('verification_plan') or {}).get('strategy_id') or 'none'}",
            f"prior attempts: {len(doctor_decision.get('prior_attempts') or [])}",
            f"policy final action: {doctor_decision.get('policy_final_action') or 'none'}",
            f"policy level: {doctor_decision.get('policy_level') or 'unknown'}",
            f"policy execute recovery: {'yes' if doctor_decision.get('policy_execute_recovery') else 'no'}",
            f"policy allowed: {'yes' if doctor_decision.get('policy_allowed') else 'no'}",
            f"recovery executed: {'yes' if doctor_decision.get('recovery_executed') else 'no'}",
            f"recovery result: {doctor_decision.get('recovery_result') or 'none'}",
            f"retry run dir: {doctor_decision.get('retry_run_dir') or 'none'}",
            f"retry phase: {doctor_decision.get('retry_phase') or 'none'}",
            f"retry result: {doctor_decision.get('retry_result') or 'none'}",
            "",
            "policy reason:",
            report.get("policy_reason") or "none",
            "",
            "reasoning summary:",
            report.get("reasoning_summary") or "none",
        ]
        (run_dir / "doctor-decision.txt").write_text("\n".join(decision_lines) + "\n", encoding="utf-8")

        write_status_artifacts(run_dir, report)
        try:
            from merge_support import artifacts as artifact_support

            artifact_support.write_operator_artifacts(run_dir, report)
        except Exception:
            pass
        DoctorEngine.write_resume_artifacts(
            run_dir,
            {
                "blocked_phase": report.get("prior_phase"),
                "resume_from_phase": report.get("resume_from_phase"),
                "safe_to_resume": report.get("safe_to_resume", False),
                "resume_command": report.get("resume_command"),
                "selected_cl": report.get("selected_cl"),
                "staged_change": report.get("staged_change"),
                "pending_changes": report.get("pending_changes", []),
                "last_successful_step": report.get("last_successful_step"),
                "failed_step": report.get("failed_step"),
                "human_action_required": report.get("requires_human_review", True),
                "why_doctor_stopped": report.get("why_doctor_stopped"),
                "prior_run_dir": report.get("prior_run_dir"),
            },
        )
        if p4 is not None:
            write_command_logs(run_dir, p4)







