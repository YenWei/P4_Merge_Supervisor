from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime
from merge_support import artifacts as artifact_support
from merge_supervisor.runtime_models import PhaseOutcome, RecoveryExecutionResult, VerifierResult
from pathlib import Path


@dataclass
class SupervisionState:
    parent_run_dir: Path
    run_attempts: int = 0
    doctor_cycles: int = 0
    resumed_from_phase: str | None = None
    resume_change: int | None = None
    child_run_dirs: list[str] = field(default_factory=list)
    child_doctor_dirs: list[str] = field(default_factory=list)
    child_resolve_dirs: list[str] = field(default_factory=list)
    child_sanitize_dirs: list[str] = field(default_factory=list)
    child_conflict_resolution_dirs: list[str] = field(default_factory=list)
    last_blocker_category: str | None = None
    last_recommended_action: str | None = None
    recovery_executed: bool = False
    final_result: str = "STARTED"
    stop_reason: str = ""


class SupervisedRunner:
    def __init__(
        self,
        *,
        args,
        supervisor_factory,
        runs_dir: Path,
        dashboard_factory=None,
        status_writer=None,
    ):
        self.args = args
        self.supervisor_factory = supervisor_factory
        self.runs_dir = runs_dir
        self.dashboard = dashboard_factory() if dashboard_factory is not None else None
        self.status_writer = status_writer

    def run(self) -> int:
        state = SupervisionState(parent_run_dir=self._new_parent_run_dir())
        self._dashboard_update(phase="supervise", step="STARTING", status="running")
        exit_code = 20
        last_run_status: dict | None = None
        last_doctor_status: dict | None = None
        last_resolve_status: dict | None = None
        last_sanitize_status: dict | None = None
        last_conflict_resolution_status: dict | None = None
        try:
            if getattr(self.args, "supervise_resume_change", None) is not None:
                state.resume_change = int(self.args.supervise_resume_change)
                (
                    exit_code,
                    last_run_status,
                    last_doctor_status,
                    last_resolve_status,
                    last_sanitize_status,
                    last_conflict_resolution_status,
                ) = self._resume_from_existing_staged_change(state)
            else:
                (
                    exit_code,
                    last_run_status,
                    last_doctor_status,
                    last_resolve_status,
                    last_sanitize_status,
                    last_conflict_resolution_status,
                ) = self._run_supervision_loop(state)
        except Exception as error:
            state.final_result = "SUPERVISION_ORCHESTRATION_FAILED"
            state.stop_reason = str(error)
            self._write_parent_reports(
                state,
                last_run_status,
                last_doctor_status,
                last_resolve_status,
                last_sanitize_status,
                last_conflict_resolution_status,
            )
            self._finish("blocked")
            raise

        if last_doctor_status is None and state.child_doctor_dirs:
            try:
                last_doctor_status = self._read_status(Path(state.child_doctor_dirs[-1]))
            except Exception:
                pass

        self._write_parent_reports(
            state,
            last_run_status,
            last_doctor_status,
            last_resolve_status,
            last_sanitize_status,
            last_conflict_resolution_status,
        )
        if state.final_result in {"SUPERVISION_REVIEW_READY", "SUPERVISION_CONFLICTS_PENDING"}:
            final_status = "done"
            final_step = "DONE"
        else:
            final_status = "done" if exit_code == 0 else "blocked" if exit_code == 20 else "paused"
            final_step = "DONE" if exit_code == 0 else "PAUSED"
        self._dashboard_update(
            phase="supervise",
            step=final_step,
            item=state.stop_reason,
            progress=f"runs={state.run_attempts} doctor={state.doctor_cycles}",
            status=final_status,
        )
        self._finish(final_status)
        self._print_terminal_summary(
            state,
            last_run_status,
            last_doctor_status,
            last_resolve_status,
            last_sanitize_status,
            last_conflict_resolution_status,
        )
        print(f"[{state.final_result}] {state.stop_reason}")
        print(f"Report: {state.parent_run_dir}")
        return exit_code

    def _run_supervision_loop(
        self,
        state: SupervisionState,
    ) -> tuple[int, dict | None, dict | None, dict | None, dict | None, dict | None]:
        exit_code = 20
        last_run_status: dict | None = None
        last_doctor_status: dict | None = None
        last_resolve_status: dict | None = None
        last_sanitize_status: dict | None = None
        last_conflict_resolution_status: dict | None = None
        while state.run_attempts < self.args.supervise_max_run_attempts:
            state.run_attempts += 1
            self._dashboard_update(
                phase="supervise",
                step="RUN_ATTEMPT",
                item=f"attempt {state.run_attempts}",
                progress=f"run {state.run_attempts}/{self.args.supervise_max_run_attempts}",
                status="running",
            )
            run_exit_code, run_dir = self._invoke_run()
            if run_dir is None:
                state.final_result = "SUPERVISION_ORCHESTRATION_FAILED"
                state.stop_reason = "run child did not produce a new run directory"
                exit_code = 20
                break
            state.child_run_dirs.append(str(run_dir))
            run_status = self._read_status(run_dir)
            last_run_status = run_status
            state.last_blocker_category = run_status.get("blocker_category")

            run_outcome = self._phase_outcome_from_status(run_status)
            if run_outcome.result_kind == "READY_TO_RESOLVE":
                (
                    exit_code,
                    last_doctor_status,
                    last_resolve_status,
                    last_sanitize_status,
                    last_conflict_resolution_status,
                ) = self._continue_from_run_artifact(state)
                break
            if run_outcome.result_kind == "READY_NO_CHANGES":
                state.final_result = "SUPERVISION_NO_CHANGES"
                state.stop_reason = run_status.get("next_action") or "selected source changelist produced no matching files for the requested batches"
                exit_code = 0
                break
            if run_outcome.result_kind not in {"BLOCKED_RETRYABLE", "BLOCKED_HUMAN"}:
                state.final_result = "SUPERVISION_STOPPED_CHILD_FAILURE"
                state.stop_reason = f"unexpected run result: {run_outcome.result_kind or 'unknown'}"
                exit_code = run_exit_code if run_exit_code else 20
                break

            doctor_exit_code, doctor_status = self._run_doctor_cycle(state)
            last_doctor_status = doctor_status
            if doctor_status is None:
                exit_code = doctor_exit_code
                break
            if doctor_status.get("result") != "RECOVERY_EXECUTED_RETRY_SUCCEEDED":
                exit_code = doctor_exit_code
                break

            state.recovery_executed = True
            self._dashboard_update(
                phase="supervise",
                step="RETRYING_RUN",
                item="approved recovery completed",
                progress=f"run {state.run_attempts + 1}/{self.args.supervise_max_run_attempts}",
                status="running",
            )
        else:
            state.final_result = "SUPERVISION_STOPPED_RETRY_LIMIT"
            state.stop_reason = "run attempt budget exhausted"
            exit_code = 20
        return exit_code, last_run_status, last_doctor_status, last_resolve_status, last_sanitize_status, last_conflict_resolution_status

    def _resume_from_existing_staged_change(
        self,
        state: SupervisionState,
    ) -> tuple[int, dict | None, dict | None, dict | None, dict | None, dict | None]:
        staged_change = int(self.args.supervise_resume_change)
        resume_from = getattr(self.args, "supervise_resume_from", "auto")
        self._dashboard_update(
            phase="supervise",
            step="RESUME_LOOKUP",
            item=f"staged CL {staged_change}",
            progress=f"resume {resume_from}",
            staged_cl=str(staged_change),
            status="running",
        )

        run_status, resolve_status, sanitize_status, conflict_status = self._load_resume_artifacts(staged_change)
        doctor_status = None

        if resume_from == "auto":
            if conflict_status is not None:
                state.resumed_from_phase = "resolve-conflicts"
            elif sanitize_status is not None:
                state.resumed_from_phase = (
                    "resolve-conflicts"
                    if sanitize_status.get("result") == "REVIEW_WITH_CONFLICT_BUCKETS"
                    else "sanitize"
                )
            elif resolve_status is not None:
                state.resumed_from_phase = "resolve"
            else:
                state.resumed_from_phase = "run-recovery"
        else:
            state.resumed_from_phase = resume_from

        if state.resumed_from_phase == "resolve-conflicts":
            if conflict_status is not None and conflict_status.get("result") in {"READY_FOR_REVIEW", "REVIEW_WITH_CONFLICT_BUCKETS"}:
                return self._finalize_conflict_outcome(state, run_status, doctor_status, resolve_status, sanitize_status, conflict_status)
            if sanitize_status is None:
                raise RuntimeError(f"Could not find sanitize summary for staged CL {staged_change} to resume resolve-conflicts.")
            exit_code, conflict_status = self._run_conflict_resolution_phase(state, staged_change)
            return exit_code, run_status, doctor_status, resolve_status, sanitize_status, conflict_status

        if state.resumed_from_phase == "sanitize":
            if sanitize_status is not None and sanitize_status.get("result") == "REVIEW_WITH_CONFLICT_BUCKETS":
                exit_code, conflict_status = self._run_conflict_resolution_phase(state, staged_change)
                return exit_code, run_status, doctor_status, resolve_status, sanitize_status, conflict_status
            if sanitize_status is not None and sanitize_status.get("result") == "READY_FOR_REVIEW":
                return self._finalize_sanitize_outcome(state, run_status, doctor_status, resolve_status, sanitize_status, conflict_status)
            if resolve_status is None:
                raise RuntimeError(f"Could not find resolve summary for staged CL {staged_change} to resume sanitize.")
            exit_code, sanitize_status, conflict_status = self._run_sanitize_and_maybe_conflicts(state, staged_change)
            return exit_code, run_status, doctor_status, resolve_status, sanitize_status, conflict_status

        if state.resumed_from_phase in {"split", "resolve"}:
            if resolve_status is None:
                raise RuntimeError(f"Could not find resolve artifacts for staged CL {staged_change} to resume from resolve.")
            if self._resolve_artifact_is_completed(resolve_status):
                exit_code, sanitize_status, conflict_status = self._run_sanitize_and_maybe_conflicts(state, staged_change)
                return exit_code, run_status, doctor_status, resolve_status, sanitize_status, conflict_status
            exit_code, doctor_status, resolve_status, sanitize_status, conflict_status = self._run_resolve_pipeline(state, staged_change)
            return exit_code, run_status, doctor_status, resolve_status, sanitize_status, conflict_status

        exit_code, doctor_status, resolve_status, sanitize_status, conflict_status = self._run_resolve_pipeline(state, staged_change)
        return exit_code, run_status, doctor_status, resolve_status, sanitize_status, conflict_status

    @staticmethod
    def _resolve_artifact_is_completed(resolve_status: dict | None) -> bool:
        return (resolve_status or {}).get("result") in {"READY_FOR_REVIEW", "REVIEW_WITH_CONFLICT_BUCKETS"}

    @staticmethod
    def _resolve_blocker_signature(status: dict | None) -> tuple | None:
        if not status:
            return None
        if status.get("phase") != "resolve":
            return None
        if status.get("result") not in {"BLOCKED_RETRYABLE", "BLOCKED_HUMAN"}:
            return None
        failing_command = status.get("failing_command")
        normalized_command = tuple(failing_command) if isinstance(failing_command, list) else failing_command
        return (
            status.get("blocker_category"),
            status.get("staged_change") or status.get("current_change"),
            normalized_command,
            int(status.get("opened_file_count", 0) or 0),
            int(status.get("unresolved_file_count", 0) or 0),
        )

    def _phase_outcome_from_status(self, status: dict | None) -> PhaseOutcome:
        if not status:
            return PhaseOutcome(
                phase_name="unknown",
                result_kind="unknown",
                next_phase=None,
                blocked_case_id=None,
                resume_bundle=None,
            )

        phase_name = str(status.get("phase") or status.get("phase_name") or "unknown")
        result_kind = str(status.get("result") or status.get("result_kind") or "unknown")
        blocked_case_id = None
        if result_kind in {"BLOCKED_RETRYABLE", "BLOCKED_HUMAN"}:
            blocked_case_id = f"{phase_name}-{status.get('staged_change') or status.get('current_change') or 'latest'}"

        next_phase_map = {
            ("run", "READY_TO_RESOLVE"): "resolve",
            ("resolve", "READY_FOR_REVIEW"): "sanitize",
            ("resolve", "REVIEW_WITH_CONFLICT_BUCKETS"): "sanitize",
            ("sanitize", "REVIEW_WITH_CONFLICT_BUCKETS"): "resolve-conflicts",
        }
        return PhaseOutcome.from_status_payload(
            status,
            next_phase=next_phase_map.get((phase_name, result_kind)),
            blocked_case_id=blocked_case_id,
            verification_passed=bool(status.get("verification_passed", False)),
        )

    def _handle_recovery_failure(self, doctor_status: dict | None) -> tuple[int, dict | None]:
        if doctor_status is None:
            return 20, None
        failed_status = dict(doctor_status)
        if failed_status.get("verification_passed") is False:
            failed_status["result"] = "RECOVERY_MUTATED_STATE_REQUIRES_REDIAGNOSIS"
            failed_status.setdefault("next_action", "Re-run doctor on the preserved staged change before resuming.")
            return 10, failed_status
        return 20, failed_status

    def _continue_from_run_artifact(
        self,
        state: SupervisionState,
    ) -> tuple[int, dict | None, dict | None, dict | None, dict | None]:
        return self._run_resolve_pipeline(
            state,
            int(self.args.supervise_resume_change) if getattr(self.args, "supervise_resume_change", None) is not None else None,
        )

    def _run_resolve_pipeline(
        self,
        state: SupervisionState,
        staged_change: int | None,
    ) -> tuple[int, dict | None, dict | None, dict | None, dict | None]:
        doctor_status = None
        resolve_status = None
        sanitize_status = None
        conflict_status = None
        seen_blocked_resolve_signatures: dict[tuple, int] = {}
        while True:
            self._dashboard_update(
                phase="supervise",
                step="RESOLVE_PHASE",
                item=f"staged CL {staged_change}" if staged_change is not None else "resolving staged batch changelists",
                progress="resolve 1/1",
                staged_cl=str(staged_change) if staged_change is not None else "-",
                status="running",
            )
            resolve_exit_code, resolve_dir = self._invoke_resolve(staged_change)
            if resolve_dir is None:
                state.final_result = "SUPERVISION_ORCHESTRATION_FAILED"
                state.stop_reason = "resolve child did not produce a new run directory"
                return 20, doctor_status, resolve_status, sanitize_status, conflict_status
            state.child_resolve_dirs.append(str(resolve_dir))
            resolve_status = self._read_status(resolve_dir)
            resolve_outcome = self._phase_outcome_from_status(resolve_status)
            state.last_blocker_category = resolve_status.get("blocker_category")
            resolve_signature = self._resolve_blocker_signature(resolve_status)
            if resolve_signature is not None:
                seen_blocked_resolve_signatures[resolve_signature] = seen_blocked_resolve_signatures.get(resolve_signature, 0) + 1
                if seen_blocked_resolve_signatures[resolve_signature] > 1:
                    state.final_result = "SUPERVISION_PAUSED"
                    state.last_recommended_action = "manual_non_converging_resolve_review_required"
                    state.stop_reason = "resolve recovery is not converging; the same blocked resolve signature repeated after an approved recovery"
                    return 10, doctor_status, resolve_status, sanitize_status, conflict_status
            if resolve_outcome.result_kind in {"READY_FOR_REVIEW", "REVIEW_WITH_CONFLICT_BUCKETS"}:
                break
            if resolve_outcome.result_kind not in {"BLOCKED_RETRYABLE", "BLOCKED_HUMAN"}:
                state.final_result = "SUPERVISION_STOPPED_RESOLVE_FAILURE"
                state.stop_reason = resolve_status.get("reason") or f"unexpected resolve result: {resolve_outcome.result_kind or 'unknown'}"
                return resolve_exit_code if resolve_exit_code else 20, doctor_status, resolve_status, sanitize_status, conflict_status

            doctor_exit_code, doctor_status = self._run_doctor_cycle(state)
            if doctor_status is None:
                return doctor_exit_code, doctor_status, resolve_status, sanitize_status, conflict_status
            if doctor_status.get("result") != "RECOVERY_EXECUTED_RETRY_SUCCEEDED":
                return doctor_exit_code, doctor_status, resolve_status, sanitize_status, conflict_status
            state.recovery_executed = True
            preserved_change = doctor_status.get("recovery_preserved_change") or staged_change
            if preserved_change is not None:
                staged_change = int(preserved_change)
        exit_code, sanitize_status, conflict_status = self._run_sanitize_and_maybe_conflicts(state, staged_change)
        return exit_code, doctor_status, resolve_status, sanitize_status, conflict_status

    def _run_doctor_cycle(self, state: SupervisionState) -> tuple[int, dict | None]:
        max_doctor_cycles = int(getattr(self.args, "supervise_max_doctor_cycles", 0) or 0)
        if max_doctor_cycles > 0 and state.doctor_cycles >= max_doctor_cycles:
            state.final_result = "SUPERVISION_STOPPED_RETRY_LIMIT"
            state.stop_reason = "doctor cycle budget exhausted"
            return 20, None

        state.doctor_cycles += 1
        doctor_progress = (
            f"doctor {state.doctor_cycles}/{max_doctor_cycles}"
            if max_doctor_cycles > 0
            else f"doctor {state.doctor_cycles}/unbounded"
        )
        self._dashboard_update(
            phase="supervise",
            step="DOCTOR_ANALYSIS",
            item=f"cycle {state.doctor_cycles}",
            progress=doctor_progress,
            status="running",
        )
        doctor_exit_code, doctor_dir = self._invoke_doctor()
        if doctor_dir is None:
            state.final_result = "SUPERVISION_ORCHESTRATION_FAILED"
            state.stop_reason = "doctor child did not produce a new run directory"
            return 20, None

        state.child_doctor_dirs.append(str(doctor_dir))
        doctor_status = self._read_status(doctor_dir)
        state.last_recommended_action = doctor_status.get("recommended_action") or doctor_status.get("next_action")

        if doctor_status.get("result") == "RECOVERY_EXECUTED_RETRY_SUCCEEDED" and doctor_status.get("verification_passed") is False:
            state.final_result = "SUPERVISION_PAUSED"
            state.stop_reason = doctor_status.get("failure_reason") or doctor_status.get("next_action") or "verifier rejected the recovery result"
            return self._handle_recovery_failure(doctor_status)

        if doctor_status.get("result") != "RECOVERY_EXECUTED_RETRY_SUCCEEDED":
            state.final_result = "SUPERVISION_PAUSED"
            state.stop_reason = doctor_status.get("next_action") or "doctor did not authorize retry"
            return 10, doctor_status

        return doctor_exit_code, doctor_status

    def _run_sanitize_and_maybe_conflicts(
        self,
        state: SupervisionState,
        staged_change: int | None,
    ) -> tuple[int, dict | None, dict | None]:
        sanitize_status = None
        conflict_status = None
        self._dashboard_update(
            phase="supervise",
            step="SANITIZE_PHASE",
            item=f"staged CL {staged_change}" if staged_change is not None else "latest resolve artifact",
            progress="sanitize 1/1",
            staged_cl=str(staged_change) if staged_change is not None else "-",
            status="running",
        )
        sanitize_exit_code, sanitize_dir = self._invoke_sanitize(staged_change)
        if sanitize_dir is None:
            state.final_result = "SUPERVISION_ORCHESTRATION_FAILED"
            state.stop_reason = "sanitize child did not produce a new run directory"
            return 20, sanitize_status, conflict_status
        state.child_sanitize_dirs.append(str(sanitize_dir))
        sanitize_status = self._read_status(sanitize_dir)
        sanitize_outcome = self._phase_outcome_from_status(sanitize_status)
        sanitize_result = sanitize_outcome.result_kind
        state.last_blocker_category = sanitize_status.get("blocker_category")
        if sanitize_result == "REVIEW_WITH_CONFLICT_BUCKETS":
            exit_code, conflict_status = self._run_conflict_resolution_phase(state, staged_change)
            return exit_code, sanitize_status, conflict_status
        if sanitize_result == "READY_FOR_REVIEW":
            self._mark_review_ready(state, sanitize_status, "sanitize completed successfully")
            return 0, sanitize_status, conflict_status
        state.final_result = "SUPERVISION_STOPPED_SANITIZE_FAILURE"
        state.stop_reason = sanitize_status.get("reason") or f"unexpected sanitize result: {sanitize_result or 'unknown'}"
        return sanitize_exit_code if sanitize_exit_code else 20, sanitize_status, conflict_status

    def _run_conflict_resolution_phase(
        self,
        state: SupervisionState,
        staged_change: int | None,
    ) -> tuple[int, dict | None]:
        conflict_status = None
        self._dashboard_update(
            phase="supervise",
            step="CONFLICT_RESOLUTION",
            item=f"staged CL {staged_change}" if staged_change is not None else "latest sanitize artifact",
            progress="resolve-conflicts 1/1",
            staged_cl=str(staged_change) if staged_change is not None else "-",
            status="running",
        )
        conflict_exit_code, conflict_dir = self._invoke_resolve_conflicts(staged_change)
        if conflict_dir is None:
            state.final_result = "SUPERVISION_ORCHESTRATION_FAILED"
            state.stop_reason = "resolve-conflicts child did not produce a new run directory"
            return 20, conflict_status
        state.child_conflict_resolution_dirs.append(str(conflict_dir))
        conflict_status = self._read_status(conflict_dir)
        conflict_outcome = self._phase_outcome_from_status(conflict_status)
        conflict_result = conflict_outcome.result_kind
        state.last_blocker_category = conflict_status.get("blocker_category")
        if conflict_result == "READY_FOR_REVIEW":
            self._mark_review_ready(state, conflict_status, "conflict resolution completed successfully")
            return 0, conflict_status
        if conflict_result == "REVIEW_WITH_CONFLICT_BUCKETS":
            self._mark_conflicts_pending(state, conflict_status, "conflict buckets still require review")
            return 10, conflict_status
        state.final_result = "SUPERVISION_STOPPED_CONFLICT_RESOLUTION_FAILURE"
        state.stop_reason = conflict_status.get("reason") or f"unexpected resolve-conflicts result: {conflict_result or 'unknown'}"
        return conflict_exit_code if conflict_exit_code else 20, conflict_status

    def _load_resume_artifacts(
        self,
        staged_change: int,
    ) -> tuple[dict | None, dict | None, dict | None, dict | None]:
        run_status = self._find_latest_status_for_phase("run", staged_change)
        resolve_status = self._find_latest_summary("resolve-summary.json", staged_change)
        if resolve_status is None:
            resolve_status = self._find_latest_status_for_phase("resolve", staged_change)
        sanitize_status = self._find_latest_summary("sanitize-summary.json", staged_change)
        conflict_status = self._find_latest_summary("conflict-resolution-summary.json", staged_change)
        return run_status, resolve_status, sanitize_status, conflict_status

    def _finalize_sanitize_outcome(
        self,
        state: SupervisionState,
        run_status: dict | None,
        doctor_status: dict | None,
        resolve_status: dict | None,
        sanitize_status: dict | None,
        conflict_status: dict | None,
    ) -> tuple[int, dict | None, dict | None, dict | None, dict | None, dict | None]:
        self._mark_review_ready(state, sanitize_status, "sanitize already produced a reviewable result")
        return 0, run_status, doctor_status, resolve_status, sanitize_status, conflict_status

    def _finalize_conflict_outcome(
        self,
        state: SupervisionState,
        run_status: dict | None,
        doctor_status: dict | None,
        resolve_status: dict | None,
        sanitize_status: dict | None,
        conflict_status: dict | None,
    ) -> tuple[int, dict | None, dict | None, dict | None, dict | None, dict | None]:
        if conflict_status is not None and conflict_status.get("result") == "READY_FOR_REVIEW":
            self._mark_review_ready(state, conflict_status, "conflict resolution already produced a reviewable result")
            return 0, run_status, doctor_status, resolve_status, sanitize_status, conflict_status
        self._mark_conflicts_pending(state, conflict_status, "conflict resolution already produced pending conflict buckets")
        return 10, run_status, doctor_status, resolve_status, sanitize_status, conflict_status

    def _mark_review_ready(self, state: SupervisionState, status: dict | None, fallback_reason: str) -> None:
        state.final_result = "SUPERVISION_REVIEW_READY"
        state.stop_reason = (status or {}).get("next_action") or fallback_reason
        state.last_recommended_action = (status or {}).get("next_action")
        state.last_blocker_category = (status or {}).get("blocker_category")

    def _mark_conflicts_pending(self, state: SupervisionState, status: dict | None, fallback_reason: str) -> None:
        state.final_result = "SUPERVISION_CONFLICTS_PENDING"
        state.stop_reason = (status or {}).get("next_action") or fallback_reason
        state.last_recommended_action = (status or {}).get("next_action")
        state.last_blocker_category = (status or {}).get("blocker_category")

    def _known_run_dirs(self) -> set[str]:
        if not self.runs_dir.exists():
            return set()
        return {path.name for path in self.runs_dir.iterdir() if path.is_dir()}

    def _detect_new_child_run_dir(self, before: set[str]) -> Path | None:
        after = self._known_run_dirs()
        created = sorted(after - before)
        if not created:
            return None
        if len(created) > 1:
            return max((self.runs_dir / name for name in created), key=lambda path: path.stat().st_mtime)
        return self.runs_dir / created[0]

    def _new_parent_run_dir(self) -> Path:
        return self.runs_dir / datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")

    def _child_args(self, command: str, **overrides):
        values = vars(self.args).copy()
        values["command"] = command
        values["_dashboard_enabled"] = False
        values["_dashboard_bridge"] = self._child_dashboard_update
        values["_progress_bridge"] = self._child_progress_update
        values.setdefault("change", None)
        values.setdefault("allow_recovered_blocked_run", False)
        values.setdefault("unresolved_file", None)
        values.update(overrides)
        return argparse.Namespace(**values)

    def _invoke_run(self) -> tuple[int, Path | None]:
        before = self._known_run_dirs()
        exit_code = self.supervisor_factory(self._child_args("run")).run_merge()
        return exit_code, self._detect_new_child_run_dir(before)

    def _invoke_doctor(self) -> tuple[int, Path | None]:
        before = self._known_run_dirs()
        exit_code = self.supervisor_factory(self._child_args("doctor")).run_doctor()
        return exit_code, self._detect_new_child_run_dir(before)

    def _invoke_resolve(self, change: int | None = None) -> tuple[int, Path | None]:
        before = self._known_run_dirs()
        exit_code = self.supervisor_factory(self._child_args("resolve", change=change)).run_resolve()
        return exit_code, self._detect_new_child_run_dir(before)

    def _invoke_sanitize(self, staged_change: int | None) -> tuple[int, Path | None]:
        before = self._known_run_dirs()
        exit_code = self.supervisor_factory(self._child_args("sanitize", change=staged_change)).run_sanitize()
        return exit_code, self._detect_new_child_run_dir(before)

    def _invoke_resolve_conflicts(self, staged_change: int | None) -> tuple[int, Path | None]:
        before = self._known_run_dirs()
        exit_code = self.supervisor_factory(self._child_args("resolve-conflicts", change=staged_change)).run_resolve_conflicts()
        return exit_code, self._detect_new_child_run_dir(before)

    def _read_status(self, run_dir: Path) -> dict:
        status_path = run_dir / "status.json"
        if not status_path.exists():
            raise RuntimeError(f"Missing child status.json in {run_dir}")
        return json.loads(status_path.read_text(encoding="utf-8"))

    def _find_latest_summary(self, filename: str, staged_change: int) -> dict | None:
        summary_files = sorted(self.runs_dir.glob(f"*/{filename}"), key=lambda path: path.stat().st_mtime, reverse=True)
        for summary_file in summary_files:
            summary = json.loads(summary_file.read_text(encoding="utf-8"))
            if summary.get("staged_change") == staged_change:
                return summary
        return None

    def _find_latest_status_for_phase(self, phase: str, staged_change: int) -> dict | None:
        status_files = sorted(self.runs_dir.glob("*/status.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for status_file in status_files:
            status = json.loads(status_file.read_text(encoding="utf-8"))
            if status.get("phase") == phase and status.get("staged_change") == staged_change:
                return status
        return None

    def _print_terminal_summary(
        self,
        state: SupervisionState,
        run_status: dict | None,
        doctor_status: dict | None,
        resolve_status: dict | None,
        sanitize_status: dict | None,
        conflict_resolution_status: dict | None,
    ) -> None:
        report = self._report_dict(
            state,
            run_status,
            doctor_status,
            resolve_status,
            sanitize_status,
            conflict_resolution_status,
        )
        lines: list[str] = ["Summary:"]
        lines.extend(artifact_support.build_operator_summary_lines(report))
        for line in lines:
            print(line)

    def _report_dict(
        self,
        state: SupervisionState,
        run_status: dict | None = None,
        doctor_status: dict | None = None,
        resolve_status: dict | None = None,
        sanitize_status: dict | None = None,
        conflict_resolution_status: dict | None = None,
    ) -> dict:
        final_status = conflict_resolution_status or sanitize_status or resolve_status or run_status or {}
        runtime_status = doctor_status if state.final_result == "SUPERVISION_PAUSED" and doctor_status is not None else final_status
        runtime_result = self._phase_outcome_from_status(runtime_status).to_report_dict() if runtime_status else None
        attempted_primitive = None
        verifier_outcome = None
        trusted_resume_bundle = None
        if doctor_status is not None:
            attempted_primitive = RecoveryExecutionResult.from_status_payload(doctor_status).to_report_dict()
            if any(key in doctor_status for key in ("verification_passed", "failure_reason", "resume_bundle")):
                verifier_model = VerifierResult.from_status_payload(
                    doctor_status,
                    verification_passed=bool(doctor_status.get("verification_passed", False)),
                )
                verifier_outcome = verifier_model.to_report_dict()
                trusted_resume_bundle = verifier_model.resume_bundle
        if trusted_resume_bundle is None and runtime_result is not None and runtime_result.get("resume_bundle") is not None:
            trusted_resume_bundle = PhaseOutcome.from_status_payload(runtime_status).resume_bundle
        final_review_buckets = self._collect_bucket_details(resolve_status, final_status, include_review=True, include_conflict=False)
        final_conflict_buckets = self._collect_bucket_details(resolve_status, final_status, include_review=False, include_conflict=True)
        empty_buckets = self._collect_empty_bucket_details(resolve_status, final_status)
        staged_change = self._report_staged_change(
            state=state,
            run_status=run_status,
            doctor_status=doctor_status,
            resolve_status=resolve_status,
            sanitize_status=sanitize_status,
            conflict_resolution_status=conflict_resolution_status,
            trusted_resume_bundle=trusted_resume_bundle,
        )
        return {
            "status": state.final_result,
            "result": state.final_result,
            "phase": "supervise",
            "source_stream": self.args.source_stream,
            "target_stream": self.args.target_stream,
            "job_tag": self.args.job_tag,
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "run_attempts": state.run_attempts,
            "doctor_cycles": state.doctor_cycles,
            "resumed_from_phase": state.resumed_from_phase,
            "resume_change": state.resume_change,
            "child_run_dirs": state.child_run_dirs,
            "child_doctor_dirs": state.child_doctor_dirs,
            "child_resolve_dirs": state.child_resolve_dirs,
            "child_sanitize_dirs": state.child_sanitize_dirs,
            "child_conflict_resolution_dirs": state.child_conflict_resolution_dirs,
            "last_blocker_category": state.last_blocker_category,
            "last_recommended_action": state.last_recommended_action,
            "recovery_executed": state.recovery_executed,
            "stop_reason": state.stop_reason,
            "reason": state.stop_reason,
            "next_action": state.last_recommended_action or state.stop_reason,
            "blocker_category": state.last_blocker_category,
            "retryable": False,
            "opened_file_count": final_status.get("opened_file_count", 0),
            "unresolved_file_count": final_status.get("unresolved_file_count", 0),
            "staged_change": staged_change,
            "bucket_summaries": final_status.get("bucket_summaries", []),
            "conflict_buckets": final_status.get("conflict_buckets", []),
            "resolved_conflict_buckets": final_status.get("resolved_conflict_buckets", []),
            "final_review_buckets": final_review_buckets,
            "final_conflict_buckets": final_conflict_buckets,
            "empty_buckets": empty_buckets,
            "unexpected_unresolved_batches": (run_status or {}).get("unexpected_unresolved_batches", []),
            "completed_phases": self._completed_phases(run_status, resolve_status, sanitize_status, conflict_resolution_status),
            "stopped_phase": self._stopped_phase(state, run_status, doctor_status, resolve_status, sanitize_status, conflict_resolution_status),
            "runtime_result": runtime_result,
            "attempted_primitive": attempted_primitive,
            "verifier_outcome": verifier_outcome,
            "safe_to_resume": trusted_resume_bundle.safe_to_resume if trusted_resume_bundle is not None else None,
            "resume_command": trusted_resume_bundle.resume_command if trusted_resume_bundle is not None else None,
            "last_successful_step": final_status.get("last_successful_step"),
            "failed_step": final_status.get("failed_step"),
            "current_batch": final_status.get("current_batch"),
            "current_change": final_status.get("current_change"),
            "current_pass_index": final_status.get("current_pass_index"),
            "current_pass_count": final_status.get("current_pass_count"),
            "current_step_label": final_status.get("current_step_label"),
            "current_command_chunk_index": final_status.get("current_command_chunk_index"),
            "current_command_chunk_count": final_status.get("current_command_chunk_count"),
            "inspect_command": f"Get-Content -Path '{state.parent_run_dir}\\operator-summary.txt'",
        }


    @staticmethod
    def _report_staged_change(
        *,
        state: SupervisionState,
        run_status: dict | None,
        doctor_status: dict | None,
        resolve_status: dict | None,
        sanitize_status: dict | None,
        conflict_resolution_status: dict | None,
        trusted_resume_bundle,
    ) -> int | None:
        if trusted_resume_bundle is not None and trusted_resume_bundle.resume_target_change is not None:
            return trusted_resume_bundle.resume_target_change
        for status in (conflict_resolution_status, sanitize_status, resolve_status, doctor_status, run_status):
            if status is None:
                continue
            for key in ("staged_change", "current_change", "recovery_preserved_change"):
                value = status.get(key)
                if value not in (None, ""):
                    return int(value)
        if state.resume_change is not None:
            return int(state.resume_change)
        return None

    @staticmethod
    def _completed_phases(
        run_status: dict | None,
        resolve_status: dict | None,
        sanitize_status: dict | None,
        conflict_resolution_status: dict | None,
    ) -> list[str]:
        phases: list[str] = []
        if run_status is not None and run_status.get("result") in {"READY_TO_RESOLVE", "READY_NO_CHANGES"}:
            phases.append("run")
        if resolve_status is not None and resolve_status.get("result") in {"READY_FOR_REVIEW", "REVIEW_WITH_CONFLICT_BUCKETS"}:
            phases.append("resolve")
        if sanitize_status is not None and sanitize_status.get("result") in {"READY_FOR_REVIEW", "REVIEW_WITH_CONFLICT_BUCKETS"}:
            phases.append("sanitize")
        if conflict_resolution_status is not None and conflict_resolution_status.get("result") in {"READY_FOR_REVIEW", "REVIEW_WITH_CONFLICT_BUCKETS"}:
            phases.append("resolve-conflicts")
        return phases

    @staticmethod
    def _stopped_phase(
        state: SupervisionState,
        run_status: dict | None,
        doctor_status: dict | None,
        resolve_status: dict | None,
        sanitize_status: dict | None,
        conflict_resolution_status: dict | None,
    ) -> str:
        if state.final_result == "SUPERVISION_PAUSED" and doctor_status is not None:
            return doctor_status.get("prior_phase") or "doctor"
        if state.final_result == "SUPERVISION_NO_CHANGES":
            return "run"
        for status in (conflict_resolution_status, sanitize_status, resolve_status, run_status):
            if status is not None and status.get("result") not in {"READY_FOR_REVIEW", "REVIEW_WITH_CONFLICT_BUCKETS", "READY_TO_RESOLVE", "READY_NO_CHANGES"}:
                return status.get("phase") or "unknown"
        return "supervise"

    @staticmethod
    def _collect_bucket_details(
        resolve_status: dict | None,
        final_status: dict | None,
        *,
        include_review: bool,
        include_conflict: bool,
    ) -> list[dict]:
        if resolve_status is None:
            return []
        split_buckets = {bucket["bucket"]: bucket for bucket in resolve_status.get("bucket_summaries", [])}
        final_buckets = {}
        if final_status is not None:
            final_buckets = {bucket["bucket"]: bucket for bucket in final_status.get("bucket_summaries", [])}
        details: list[dict] = []
        for bucket_name, split_bucket in split_buckets.items():
            is_review_bucket = not (bucket_name.startswith("conflict-") or bucket_name.startswith("holding-"))
            if is_review_bucket and not include_review:
                continue
            if (not is_review_bucket) and not include_conflict:
                continue
            final_bucket = final_buckets.get(bucket_name, {})
            details.append(
                {
                    "bucket": bucket_name,
                    "change": final_bucket.get("change", split_bucket.get("change")),
                    "file_count": split_bucket.get("file_count"),
                    "opened_after": final_bucket.get("opened_after"),
                    "unresolved_after": final_bucket.get("unresolved_after"),
                    "action": final_bucket.get("action"),
                }
            )
        return details

    @staticmethod
    def _collect_empty_bucket_details(resolve_status: dict | None, final_status: dict | None) -> list[dict]:
        if resolve_status is None or final_status is None:
            return []
        split_buckets = {bucket["bucket"]: bucket for bucket in resolve_status.get("bucket_summaries", [])}
        final_buckets = {bucket["bucket"]: bucket for bucket in final_status.get("bucket_summaries", [])}
        details: list[dict] = []
        for bucket_name, split_bucket in split_buckets.items():
            final_bucket = final_buckets.get(bucket_name)
            if final_bucket is None or final_bucket.get("opened_after") != 0:
                continue
            details.append(
                {
                    "bucket": bucket_name,
                    "change": final_bucket.get("change", split_bucket.get("change")),
                    "file_count": split_bucket.get("file_count"),
                }
            )
        return details

    def _write_parent_reports(
        self,
        state: SupervisionState,
        run_status: dict | None = None,
        doctor_status: dict | None = None,
        resolve_status: dict | None = None,
        sanitize_status: dict | None = None,
        conflict_resolution_status: dict | None = None,
    ) -> None:
        report = self._report_dict(state, run_status, doctor_status, resolve_status, sanitize_status, conflict_resolution_status)
        run_dir = state.parent_run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "supervision-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        summary_lines = [
            f"status: {report['status']}",
            f"source: {report['source_stream']}",
            f"target: {report['target_stream']}",
            f"job tag: {report['job_tag']}",
            f"run attempts: {report['run_attempts']}",
            f"doctor cycles: {report['doctor_cycles']}",
            f"resumed from phase: {report.get('resumed_from_phase') or 'none'}",
            f"resume staged change: {report.get('resume_change') or 'none'}",
            f"last blocker category: {report.get('last_blocker_category') or 'none'}",
            f"last recommended action: {report.get('last_recommended_action') or 'none'}",
            f"recovery executed: {'yes' if report.get('recovery_executed') else 'no'}",
            "",
            "child run dirs:",
            *([f"  {path}" for path in report["child_run_dirs"]] or ["  none"]),
            "",
            "child doctor dirs:",
            *([f"  {path}" for path in report["child_doctor_dirs"]] or ["  none"]),
            "",
            "child resolve dirs:",
            *([f"  {path}" for path in report.get("child_resolve_dirs", [])] or ["  none"]),
            "",
            "child sanitize dirs:",
            *([f"  {path}" for path in report["child_sanitize_dirs"]] or ["  none"]),
            "",
            "child conflict resolution dirs:",
            *([f"  {path}" for path in report["child_conflict_resolution_dirs"]] or ["  none"]),
            "",
            "stop reason:",
            report["stop_reason"] or "none",
        ]
        if report.get("final_review_buckets"):
            summary_lines.extend(
                [
                    "",
                    "final review buckets:",
                    *[
                        f"  {bucket['bucket']} -> {bucket['change']} ({bucket['file_count']} file(s))"
                        for bucket in report["final_review_buckets"]
                    ],
                ]
            )
        if report.get("final_conflict_buckets"):
            summary_lines.extend(
                [
                    "",
                    "final conflict buckets:",
                    *[
                        f"  {bucket['bucket']} -> {bucket['change']} "
                        f"(file_count={bucket['file_count']}, unresolved_after={bucket.get('unresolved_after')}, action={bucket.get('action')})"
                        for bucket in report["final_conflict_buckets"]
                    ],
                ]
            )
        if report.get("empty_buckets"):
            summary_lines.extend(
                [
                    "",
                    "empty leftover buckets:",
                    *[
                        f"  {bucket['bucket']} -> {bucket['change']} ({bucket['file_count']} original file(s))"
                        for bucket in report["empty_buckets"]
                    ],
                ]
            )
        (run_dir / "supervision-report.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
        artifact_support.write_operator_artifacts(run_dir, report)
        if self.status_writer is not None:
            self.status_writer(run_dir, report)

    def _dashboard_update(self, **fields) -> None:
        if self.dashboard is not None:
            self.dashboard.update(**fields)

    def _finish(self, status: str) -> None:
        if self.dashboard is not None:
            self.dashboard.finish(status)

    def _child_dashboard_update(self, fields: dict) -> None:
        if self.dashboard is None:
            return
        update = {}
        child_phase = fields.get("phase")
        if child_phase:
            update["phase"] = f"supervise/{child_phase}"
        for key in ("step", "batch", "item", "target", "progress", "selected_cl", "staged_cl", "status", "last_command"):
            if key in fields and fields.get(key) is not None:
                update[key] = fields.get(key)
        if update:
            self.dashboard.update(**update)

    def _child_progress_update(self, note: str | None) -> None:
        if self.dashboard is None:
            return
        self.dashboard.mark_progress(note or "child phase made progress")
