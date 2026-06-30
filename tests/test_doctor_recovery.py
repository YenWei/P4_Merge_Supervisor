from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from merge_supervisor.doctor_executor import DoctorExecutor
from merge_supervisor.doctor_layer import DoctorEngine
from merge_supervisor.doctor_models import DoctorBlockedCase, DoctorLLMDecision, ResumeState
from merge_supervisor.doctor_policy import DoctorPolicy
from merge_supervisor.doctor_provider import extract_ollama_response_json, validate_llm_decision
from merge_supervisor.policy_ladder import PolicyLadder
from merge_supervisor.recovery_verifier import RecoveryVerifier
from merge_supervisor.runtime_models import (
    BlockedCaseSnapshot,
    PhaseOutcome,
    PolicyLevel,
    RecoveryExecutionResult,
    ResumeBundle,
    VerifierResult,
)
from merge_supervisor.supervised_runner import SupervisedRunner
from merge_phases.doctor_phase import DoctorPhase
from merge_support import artifacts as artifact_support


def make_blocked_case(**overrides) -> DoctorBlockedCase:
    payload = {
        "phase": "resolve",
        "prior_result": "BLOCKED_RETRYABLE",
        "blocker_category": "resolve_failed",
        "reason": "p4 resolve failed",
        "failing_command": ["p4", "resolve", "-am"],
        "source_stream": "//ExampleDepot/Mainline_Source",
        "target_stream": "//ExampleDepot/Release_Target",
        "job_tag": "Level1",
        "p4_cwd": r"S:\example_workspace",
        "selected_cl": 123456,
        "staged_change": 234567,
        "batch_changes": [{"batch": "engine", "change": 234567, "file_count": 230}],
        "opened_file_count": 230,
        "unresolved_file_count": 0,
        "conflict_buckets": [],
        "resume_from_phase": "resolve",
        "safe_to_resume": True,
        "resume_command": "python .\\p4_weekly_merge.py resolve --change 234567",
        "prior_run_dir": r"S:\example_workspace\public_merge_tool\runs\fake",
        "p4_error_excerpt": [],
        "p4_command_excerpt": [],
        "allowed_actions": [
            "retry_after_login_refresh",
            "retry_after_connectivity_restore",
            "retry_after_env_restore",
            "retry_resolve_with_charset_override",
            "kill_and_retry_same_phase_after_hang",
            "isolate_conflicted_files_and_continue",
            "pause_cleanly",
        ],
    }
    payload.update(overrides)
    return DoctorBlockedCase(**payload)


class DoctorRecoveryTests(unittest.TestCase):
    def test_policy_ladder_starts_new_pattern_as_candidate(self) -> None:
        ladder = PolicyLadder()

        level = ladder.classify_pattern(
            phase="resolve",
            batch="plugins",
            path_family="Project/Plugins/Foo/Binaries/...",
            filetype="binary",
            blocker_type="resolve_failed",
            suggested_action="accept_source",
        )

        self.assertEqual(level, "candidate")

    def test_policy_ladder_promotes_repeated_shadow_validated_pattern(self) -> None:
        ladder = PolicyLadder()
        pattern = {
            "phase": "resolve",
            "batch": "plugins",
            "path_family": "Project/Plugins/Foo/Binaries/...",
            "filetype": "binary",
            "blocker_type": "resolve_failed",
            "suggested_action": "accept_source",
        }

        for _ in range(3):
            ladder.record_human_outcome(
                pattern,
                human_action="accept_source",
                resumed_cleanly=True,
            )

        self.assertEqual(ladder.classify_pattern(**pattern), "shadow-validated")

    def test_policy_ladder_builds_promotion_candidate_from_recorded_matches(self) -> None:
        ladder = PolicyLadder()
        pattern = {
            "phase": "resolve",
            "batch": "plugins",
            "path_family": "Project/Plugins/Foo/Binaries/...",
            "filetype": "binary",
            "blocker_type": "resolve_failed",
            "suggested_action": "accept_source",
        }

        for _ in range(3):
            ladder.record_human_outcome(
                pattern,
                human_action="accept_source",
                resumed_cleanly=True,
            )

        candidate = ladder.build_promotion_candidate(pattern)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["policy_level"], "shadow-validated")
        self.assertEqual(candidate["matched_clean_resumes"], 3)
    def test_merge_supervisor_exports_runtime_models(self) -> None:
        from merge_supervisor import (  # pylint: disable=import-outside-toplevel
            BlockedCaseSnapshot as ExportedBlockedCaseSnapshot,
            PhaseOutcome as ExportedPhaseOutcome,
            PolicyLevel as ExportedPolicyLevel,
            RecoveryExecutionResult as ExportedRecoveryExecutionResult,
            ResumeBundle as ExportedResumeBundle,
            VerifierResult as ExportedVerifierResult,
        )

        self.assertIs(ExportedBlockedCaseSnapshot, BlockedCaseSnapshot)
        self.assertIs(ExportedPhaseOutcome, PhaseOutcome)
        self.assertIs(ExportedPolicyLevel, PolicyLevel)
        self.assertIs(ExportedRecoveryExecutionResult, RecoveryExecutionResult)
        self.assertIs(ExportedResumeBundle, ResumeBundle)
        self.assertIs(ExportedVerifierResult, VerifierResult)

    def test_resume_bundle_from_resume_state_uses_canonical_shape(self) -> None:
        resume_state = ResumeState(
            blocked_phase="resolve",
            resume_from_phase="resolve",
            safe_to_resume=True,
            resume_command="python .\\p4_weekly_merge.py resolve --change 234567",
            selected_cl=123456,
            staged_change=234567,
            pending_changes=[234567],
            last_successful_step="resolved most files",
            failed_step=["p4", "resolve", "-am"],
            human_action_required=False,
            why_doctor_stopped="Resolve can retry against the existing staged batch changelist.",
            prior_run_dir=r"S:\example_workspace\public_merge_tool\runs\fake",
        )

        bundle = ResumeBundle.from_resume_state(resume_state, verification_passed=True)

        self.assertEqual(bundle.resume_from_phase, "resolve")
        self.assertEqual(bundle.resume_target_change, 234567)
        self.assertTrue(bundle.safe_to_resume)
        self.assertEqual(bundle.remaining_risks, [])

    def test_resume_bundle_from_status_payload_reads_existing_status_shape(self) -> None:
        bundle = ResumeBundle.from_status_payload(
            {
                "resume_from_phase": "resolve",
                "safe_to_resume": True,
                "resume_command": "python .\\p4_weekly_merge.py resolve --change 234567",
                "staged_change": 234567,
            },
            verification_passed=False,
            remaining_risks=["manual verification still required"],
        )

        self.assertEqual(bundle.resume_target_change, 234567)
        self.assertFalse(bundle.verification_passed)
        self.assertEqual(bundle.remaining_risks, ["manual verification still required"])

    def test_resume_bundle_from_status_payload_reads_nested_resume_bundle(self) -> None:
        bundle = ResumeBundle.from_status_payload(
            {
                "resume_bundle": {
                    "resume_from_phase": "resolve",
                    "resume_target_change": 234567,
                    "safe_to_resume": True,
                    "resume_command": r"python .\p4_weekly_merge.py resolve --change 234567",
                    "verification_passed": True,
                    "remaining_risks": ["verify shelved conflict bucket ordering"],
                }
            },
            verification_passed=False,
        )

        self.assertEqual(bundle.resume_from_phase, "resolve")
        self.assertEqual(bundle.resume_target_change, 234567)
        self.assertTrue(bundle.verification_passed)
        self.assertEqual(bundle.remaining_risks, ["verify shelved conflict bucket ordering"])

    def test_resume_bundle_serializes_verified_runtime_state(self) -> None:
        bundle = ResumeBundle(
            resume_from_phase="resolve",
            resume_target_change=234567,
            safe_to_resume=True,
            resume_command="python .\\p4_weekly_merge.py resolve --change 234567",
            verification_passed=True,
            remaining_risks=[],
        )

        payload = bundle.to_report_dict()

        self.assertEqual(payload["resume_from_phase"], "resolve")
        self.assertEqual(payload["resume_target_change"], 234567)
        self.assertTrue(payload["verification_passed"])

    def test_resume_bundle_round_trips_serialized_payload(self) -> None:
        bundle = ResumeBundle(
            resume_from_phase="resolve",
            resume_target_change=234567,
            safe_to_resume=True,
            resume_command=r"python .\p4_weekly_merge.py resolve --change 234567",
            verification_passed=True,
            remaining_risks=["verify shelved conflict bucket ordering"],
        )

        reloaded = ResumeBundle.from_status_payload({"resume_bundle": bundle.to_report_dict()}, verification_passed=False)

        self.assertEqual(reloaded.to_report_dict(), bundle.to_report_dict())

    def test_phase_outcome_represents_retryable_blocked_state(self) -> None:
        outcome = PhaseOutcome(
            phase_name="resolve",
            result_kind="BLOCKED_RETRYABLE",
            next_phase=None,
            blocked_case_id="resolve-2026-06-29-1",
            resume_bundle=None,
        )

        payload = outcome.to_report_dict()

        self.assertEqual(payload["phase_name"], "resolve")
        self.assertEqual(payload["result_kind"], "BLOCKED_RETRYABLE")
        self.assertEqual(payload["blocked_case_id"], "resolve-2026-06-29-1")

    def test_phase_outcome_round_trips_serialized_payload(self) -> None:
        outcome = PhaseOutcome(
            phase_name="resolve",
            result_kind="READY_FOR_REVIEW",
            next_phase="review",
            blocked_case_id="resolve-2026-06-29-9",
            resume_bundle=ResumeBundle(
                resume_from_phase="sanitize",
                resume_target_change=234567,
                safe_to_resume=True,
                resume_command=r"python .\p4_weekly_merge.py sanitize --change 234567",
                verification_passed=True,
                remaining_risks=["manual review pending"],
            ),
        )

        reloaded = PhaseOutcome.from_status_payload(outcome.to_report_dict())

        self.assertEqual(reloaded.to_report_dict(), outcome.to_report_dict())

    def test_phase_outcome_from_status_payload_preserves_runtime_result_names(self) -> None:
        outcome = PhaseOutcome.from_status_payload(
            {
                "phase": "resolve",
                "result": "READY_FOR_REVIEW",
            }
        )

        payload = outcome.to_report_dict()

        self.assertEqual(outcome.result_kind, "READY_FOR_REVIEW")
        self.assertEqual(payload["result"], "READY_FOR_REVIEW")
        self.assertIsNone(outcome.resume_bundle)


    def test_phase_outcome_from_status_payload_does_not_invent_resume_bundle_from_generic_status(self) -> None:
        outcome = PhaseOutcome.from_status_payload(
            {
                "phase": "resolve",
                "result": "READY_FOR_REVIEW",
                "staged_change": 234567,
                "resume_from_phase": "sanitize",
                "safe_to_resume": True,
                "resume_command": "python .\\p4_weekly_merge.py sanitize --change 234567",
            }
        )

        self.assertIsNone(outcome.resume_bundle)

    def test_verifier_result_marks_untrusted_resume_state(self) -> None:
        verifier = VerifierResult(
            verification_passed=False,
            failure_reason="unresolved files still remain in preserved staged change",
            resume_bundle=None,
            current_opened_file_count=3,
            current_unresolved_file_count=1,
        )

        payload = verifier.to_report_dict()

        self.assertFalse(payload["verification_passed"])
        self.assertEqual(payload["current_unresolved_file_count"], 1)

    def test_verifier_result_round_trips_serialized_payload(self) -> None:
        verifier = VerifierResult(
            verification_passed=True,
            failure_reason=None,
            resume_bundle=ResumeBundle(
                resume_from_phase="resolve",
                resume_target_change=234567,
                safe_to_resume=True,
                resume_command=r"python .\p4_weekly_merge.py resolve --change 234567",
                verification_passed=True,
                remaining_risks=[],
            ),
            current_opened_file_count=2,
            current_unresolved_file_count=0,
        )

        reloaded = VerifierResult.from_status_payload(verifier.to_report_dict(), verification_passed=False)

        self.assertEqual(reloaded.to_report_dict(), verifier.to_report_dict())

    def test_verifier_result_from_resume_state_builds_resume_bundle(self) -> None:
        resume_state = ResumeState(
            blocked_phase="resolve",
            resume_from_phase="resolve",
            safe_to_resume=True,
            resume_command="python .\\p4_weekly_merge.py resolve --change 234567",
            selected_cl=123456,
            staged_change=234567,
            pending_changes=[234567],
            last_successful_step="resolved most files",
            failed_step=["p4", "resolve", "-am"],
            human_action_required=False,
            why_doctor_stopped="Resolve can retry against the existing staged batch changelist.",
            prior_run_dir=r"S:\example_workspace\public_merge_tool\runs\fake",
        )

        verifier = VerifierResult.from_resume_state(
            resume_state,
            verification_passed=True,
            current_opened_file_count=0,
            current_unresolved_file_count=0,
        )

        self.assertIsNotNone(verifier.resume_bundle)
        self.assertEqual(verifier.resume_bundle.resume_target_change, 234567)

    def test_verifier_result_from_status_payload_prefers_nested_resume_bundle(self) -> None:
        verifier = VerifierResult.from_status_payload(
            {
                "verification_passed": False,
                "failure_reason": "legacy top-level fields should not win",
                "opened_file_count": 2,
                "unresolved_file_count": 1,
                "resume_from_phase": "legacy",
                "staged_change": 111111,
                "safe_to_resume": False,
                "resume_command": "legacy resume command",
                "resume_bundle": {
                    "resume_from_phase": "resolve",
                    "resume_target_change": 234567,
                    "safe_to_resume": True,
                    "resume_command": r"python .\p4_weekly_merge.py resolve --change 234567",
                    "verification_passed": True,
                    "remaining_risks": [],
                },
            },
            verification_passed=False,
        )

        self.assertIsNotNone(verifier.resume_bundle)
        self.assertEqual(verifier.resume_bundle.resume_from_phase, "resolve")
        self.assertEqual(verifier.resume_bundle.resume_target_change, 234567)
        self.assertTrue(verifier.resume_bundle.verification_passed)


    def test_verifier_result_from_status_payload_does_not_invent_resume_bundle_from_generic_status(self) -> None:
        verifier = VerifierResult.from_status_payload(
            {
                "verification_passed": False,
                "opened_file_count": 2,
                "unresolved_file_count": 1,
                "resume_from_phase": "legacy",
                "staged_change": 111111,
                "safe_to_resume": False,
                "resume_command": "legacy resume command",
            },
            verification_passed=False,
        )

        self.assertIsNone(verifier.resume_bundle)

    def test_phase_outcome_from_status_payload_prefers_nested_resume_bundle_and_payload_fields(self) -> None:
        outcome = PhaseOutcome.from_status_payload(
            {
                "phase": "resolve",
                "result": "READY_FOR_REVIEW",
                "next_phase": "review",
                "blocked_case_id": "resolve-2026-06-29-9",
                "resume_from_phase": "legacy",
                "staged_change": 111111,
                "safe_to_resume": False,
                "resume_command": "legacy resume command",
                "resume_bundle": {
                    "resume_from_phase": "sanitize",
                    "resume_target_change": 234567,
                    "safe_to_resume": True,
                    "resume_command": r"python .\p4_weekly_merge.py sanitize --change 234567",
                    "verification_passed": True,
                    "remaining_risks": ["manual review pending"],
                },
            }
        )

        payload = outcome.to_report_dict()

        self.assertEqual(outcome.next_phase, "review")
        self.assertEqual(outcome.blocked_case_id, "resolve-2026-06-29-9")
        self.assertIsNotNone(outcome.resume_bundle)
        self.assertEqual(outcome.resume_bundle.resume_from_phase, "sanitize")
        self.assertEqual(outcome.resume_bundle.resume_target_change, 234567)
        self.assertTrue(outcome.resume_bundle.verification_passed)
        self.assertEqual(payload["resume_bundle"]["remaining_risks"], ["manual review pending"])

    def test_blocked_case_snapshot_from_blocked_case_uses_canonical_fields(self) -> None:
        snapshot = BlockedCaseSnapshot.from_blocked_case(
            make_blocked_case(),
            sample_file_paths=["//depot/A", "//depot/B"],
        )

        payload = snapshot.to_report_dict()

        self.assertEqual(payload["blocked_phase"], "resolve")
        self.assertEqual(payload["phase_result"], "BLOCKED_RETRYABLE")
        self.assertEqual(payload["selected_cl"], 123456)
        self.assertEqual(payload["sample_file_paths"], ["//depot/A", "//depot/B"])

    def test_blocked_case_snapshot_from_status_payload_reads_existing_runtime_shape(self) -> None:
        snapshot = BlockedCaseSnapshot.from_status_payload(
            {
                "phase": "resolve",
                "result": "BLOCKED_HUMAN",
                "blocker_category": "resolve_failed",
                "failing_command": ["p4", "resolve", "-am"],
                "selected_cl": 123456,
                "staged_change": 234567,
                "opened_file_count": 5,
                "unresolved_file_count": 2,
                "allowed_actions": ["pause_cleanly"],
            },
            sample_file_paths=["//depot/conflicted"],
        )

        self.assertEqual(snapshot.phase_result, "BLOCKED_HUMAN")
        self.assertEqual(snapshot.allowed_recovery_primitives, ["pause_cleanly"])
        self.assertEqual(snapshot.sample_file_paths, ["//depot/conflicted"])

    def test_recovery_execution_result_from_status_payload_tracks_policy_level(self) -> None:
        result = RecoveryExecutionResult.from_status_payload(
            {
                "recommended_action": "isolate_conflicted_files_and_continue",
                "recovery_executed": True,
                "recovery_exit_code": 0,
                "result": "RECOVERY_EXECUTED_RETRY_SUCCEEDED",
            },
            policy_level="candidate",
        )

        payload = result.to_report_dict()

        self.assertEqual(result.policy_level, "candidate")
        self.assertEqual(payload["result_label"], "RECOVERY_EXECUTED_RETRY_SUCCEEDED")

    def test_recovery_execution_result_round_trips_serialized_payload(self) -> None:
        result = RecoveryExecutionResult(
            primitive_id="isolate_conflicted_files_and_continue",
            executed=True,
            exit_code=0,
            result_label="RECOVERY_EXECUTED_RETRY_SUCCEEDED",
            policy_level="shadow-validated",
        )

        reloaded = RecoveryExecutionResult.from_status_payload(result.to_report_dict())

        self.assertEqual(reloaded.to_report_dict(), result.to_report_dict())

    def test_classify_resolve_failed_prefers_conflict_isolation_for_staged_batch(self) -> None:
        decision = DoctorEngine.classify_case(
            "resolve",
            "BLOCKED_RETRYABLE",
            "resolve_failed",
            "p4 resolve failed",
            ["p4", "resolve", "-am"],
            staged_change=234567,
            batch_changes=[{"batch": "engine", "change": 234567}],
            opened_file_count=230,
        )

        self.assertEqual(decision.recommended_action, "isolate_conflicted_files_and_continue")
        self.assertFalse(decision.requires_human_review)
        self.assertTrue(decision.allowed)

    def test_classify_resolve_failed_still_prefers_conflict_isolation_when_opened_count_missing(self) -> None:
        decision = DoctorEngine.classify_case(
            "resolve",
            "BLOCKED_RETRYABLE",
            "resolve_failed",
            "p4 resolve failed",
            ["p4", "resolve", "-am"],
            staged_change=234567,
            batch_changes=[{"batch": "plugins", "change": 234567}],
            opened_file_count=0,
        )

        self.assertEqual(decision.recommended_action, "isolate_conflicted_files_and_continue")

    def test_resume_state_for_resolve_retries_same_staged_change(self) -> None:
        resume_state = DoctorEngine.build_resume_state(
            Path(r"S:\example_workspace\public_merge_tool\runs\fake"),
            {
                "phase": "resolve",
                "staged_change": 234567,
                "selected_cl": 123456,
                "note": "blocked in resolve",
            },
            "runs",
            r"S:\example_workspace",
        )

        self.assertEqual(resume_state.resume_from_phase, "resolve")
        self.assertTrue(resume_state.safe_to_resume)
        self.assertIn("resolve --change 234567", resume_state.resume_command)

    def test_policy_allows_conflict_isolation_when_execution_is_enabled(self) -> None:
        policy = DoctorPolicy(min_confidence=0.85)
        blocked_case = make_blocked_case()
        llm_decision = DoctorLLMDecision(
            failure_type="resolve_failed",
            confidence=0.95,
            recommended_action="isolate_conflicted_files_and_continue",
            reasoning_summary="move unresolved files aside and retry resolve",
            safe_to_resume=True,
            resume_from_phase="resolve",
            needs_human_review=False,
        )

        result = policy.evaluate(blocked_case, llm_decision, execution_enabled=True)

        self.assertTrue(result.allowed)
        self.assertTrue(result.execute_recovery)
        self.assertEqual(result.final_action, "isolate_conflicted_files_and_continue")


    def test_llm_decision_to_doctor_decision_returns_machine_usable_fields(self) -> None:
        blocked_case = make_blocked_case(
            prior_attempts=[
                {
                    "primitive_id": "retry_after_env_restore",
                    "outcome": "failed",
                    "result_label": "RECOVERY_EXECUTED_RETRY_FAILED",
                    "failure_reason": "P4PORT still unset",
                }
            ]
        )
        llm_decision = DoctorLLMDecision(
            failure_type="resolve_charset",
            confidence=0.95,
            recommended_action="retry_resolve_with_charset_override",
            reasoning_summary="charset issue is safe to retry with override",
            safe_to_resume=True,
            resume_from_phase="resolve",
            needs_human_review=False,
            recovery_primitive_id="retry_resolve_with_charset_override",
            safe_to_execute=True,
            verification_plan={
                "strategy_id": "verify_charset_retry",
                "requires_verification": True,
                "resume_from_phase": "resolve",
                "checks": [
                    "rerun resolve against the preserved staged change",
                    "confirm unresolved file count does not increase",
                ],
            },
        )

        decision = DoctorEngine.llm_decision_to_doctor_decision(blocked_case, llm_decision)
        payload = decision.to_report_dict()

        self.assertEqual(payload["recovery_primitive_id"], "retry_resolve_with_charset_override")
        self.assertTrue(payload["safe_to_execute"])
        self.assertEqual(payload["verification_plan"]["strategy_id"], "verify_charset_retry")
        self.assertEqual(payload["prior_attempts"][0]["primitive_id"], "retry_after_env_restore")
        self.assertTrue(payload["allowed"])

    def test_policy_rejects_doctor_execution_when_safe_to_execute_is_false(self) -> None:
        policy = DoctorPolicy(min_confidence=0.85)
        blocked_case = make_blocked_case()
        llm_decision = DoctorLLMDecision(
            failure_type="resolve_charset",
            confidence=0.95,
            recommended_action="retry_resolve_with_charset_override",
            reasoning_summary="unsafe to automate in current workspace state",
            safe_to_resume=False,
            resume_from_phase="resolve",
            needs_human_review=False,
            recovery_primitive_id="retry_resolve_with_charset_override",
            safe_to_execute=False,
            verification_plan={
                "strategy_id": "verify_charset_retry",
                "requires_verification": True,
                "resume_from_phase": "resolve",
                "checks": ["confirm workspace is safe before retry"],
            },
        )

        result = policy.evaluate(blocked_case, llm_decision, execution_enabled=True)

        self.assertFalse(result.allowed)
        self.assertFalse(result.safe_to_execute)
        self.assertEqual(result.final_action, "pause_cleanly")

    def test_validate_llm_decision_reads_machine_usable_fields(self) -> None:
        decision = validate_llm_decision(
            {
                "failure_type": "resolve_charset",
                "confidence": 0.92,
                "recommended_action": "retry_resolve_with_charset_override",
                "reasoning_summary": "charset mismatch has a narrow retry path",
                "safe_to_resume": True,
                "resume_from_phase": "resolve",
                "needs_human_review": False,
                "recovery_primitive_id": "retry_resolve_with_charset_override",
                "safe_to_execute": True,
                "verification_plan": {
                    "strategy_id": "verify_charset_retry",
                    "requires_verification": True,
                    "resume_from_phase": "resolve",
                    "checks": [
                        "rerun resolve against the preserved staged change",
                        "confirm unresolved file count does not increase",
                    ],
                },
            },
            default_resume_from_phase="resolve",
        )

        payload = decision.to_report_dict()

        self.assertEqual(payload["recovery_primitive_id"], "retry_resolve_with_charset_override")
        self.assertTrue(payload["safe_to_execute"])
        self.assertEqual(payload["verification_plan"]["strategy_id"], "verify_charset_retry")

    def test_build_blocked_case_carries_structured_prior_attempts(self) -> None:
        resume_state = ResumeState(
            blocked_phase="resolve",
            resume_from_phase="resolve",
            safe_to_resume=False,
            resume_command="python .\\p4_weekly_merge.py resolve --change 234567",
            selected_cl=123456,
            staged_change=234567,
            pending_changes=[234567],
            last_successful_step="doctor retry mutated state",
            failed_step=["p4", "resolve", "-am"],
            human_action_required=True,
            why_doctor_stopped="Verifier rejected the mutated state.",
            prior_run_dir=r"S:\example_workspace\public_merge_tool\runs\fake",
        )

        blocked_case = DoctorEngine.build_blocked_case(
            Path(r"S:\example_workspace\public_merge_tool\runs\fake"),
            {
                "phase": "resolve",
                "result": "BLOCKED_RETRYABLE",
                "staged_change": 234567,
                "prior_attempts": [
                    {
                        "primitive_id": "retry_resolve_with_charset_override",
                        "outcome": "failed",
                        "result_label": "RECOVERY_MUTATED_STATE_REQUIRES_REDIAGNOSIS",
                        "failure_reason": "encoding still mismatched",
                    }
                ],
            },
            resume_state,
        )

        self.assertEqual(blocked_case.prior_attempts[0]["primitive_id"], "retry_resolve_with_charset_override")
        self.assertEqual(blocked_case.prior_attempts[0]["outcome"], "failed")
    def test_extract_ollama_response_json_reads_response_field(self) -> None:
        payload = {
            "response": json.dumps(
                {
                    "failure_type": "resolve_failed",
                    "confidence": 0.9,
                    "recommended_action": "pause_cleanly",
                    "reasoning_summary": "manual review is safer",
                    "safe_to_resume": False,
                    "resume_from_phase": "resolve",
                    "needs_human_review": True,
                }
            )
        }

        parsed = extract_ollama_response_json(payload)

        self.assertEqual(parsed["failure_type"], "resolve_failed")
        self.assertEqual(parsed["recommended_action"], "pause_cleanly")


    def test_extract_ollama_response_json_accepts_wrapped_json_text(self) -> None:
        payload = {
            "response": """Here is the diagnosis:
```json
{
  "failure_type": "resolve_failed",
  "confidence": 0.9,
  "recommended_action": "pause_cleanly",
  "reasoning_summary": "manual review is safer",
  "safe_to_resume": false,
  "resume_from_phase": "resolve",
  "needs_human_review": true
}
```"""
        }

        parsed = extract_ollama_response_json(payload)

        self.assertEqual(parsed["failure_type"], "resolve_failed")
        self.assertEqual(parsed["recommended_action"], "pause_cleanly")

    def test_find_latest_run_status_ignores_newer_non_run_artifacts_for_same_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runs_dir = Path(tmpdir)
            run_dir = runs_dir / 'run_artifact'
            run_dir.mkdir()
            (run_dir / 'status.json').write_text(json.dumps({
                'phase': 'run',
                'result': 'READY_TO_RESOLVE',
                'batch_changes': [{'batch': 'plugins', 'change': 759845}],
                'staged_change': None,
            }), encoding='utf-8')

            newer_dir = runs_dir / 'resolve_artifact'
            newer_dir.mkdir()
            (newer_dir / 'status.json').write_text(json.dumps({
                'phase': 'resolve',
                'result': 'BLOCKED_RETRYABLE',
                'staged_change': 759845,
                'batch_changes': [],
            }), encoding='utf-8')

            found_dir, found_status = artifact_support.find_latest_run_status(runs_dir, staged_change=759845)

            self.assertEqual(found_dir, run_dir)
            self.assertEqual(found_status['phase'], 'run')

    def test_determine_sanitize_input_raises_when_resolve_summary_has_no_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runs_dir = Path(tmpdir)
            resolve_dir = runs_dir / 'resolve_artifact'
            resolve_dir.mkdir()
            (resolve_dir / 'resolve-summary.json').write_text(json.dumps({
                'phase': 'resolve',
                'result': 'READY_FOR_REVIEW',
                'bucket_summaries': [],
            }), encoding='utf-8')

            args = argparse.Namespace(runs_dir=str(runs_dir), change=None)

            with self.assertRaises(RuntimeError):
                artifact_support.determine_sanitize_input(args, error_cls=RuntimeError)

    def test_resolve_artifact_completed_only_for_reviewable_results(self) -> None:
        self.assertTrue(SupervisedRunner._resolve_artifact_is_completed({"result": "READY_FOR_REVIEW"}))
        self.assertTrue(SupervisedRunner._resolve_artifact_is_completed({"result": "REVIEW_WITH_CONFLICT_BUCKETS"}))
        self.assertFalse(SupervisedRunner._resolve_artifact_is_completed({"result": "BLOCKED_RETRYABLE"}))
        self.assertFalse(SupervisedRunner._resolve_artifact_is_completed(None))

    def test_phase_outcome_from_ready_to_resolve_status_maps_to_next_phase(self) -> None:
        runner = object.__new__(SupervisedRunner)

        outcome = runner._phase_outcome_from_status(
            {
                "phase": "run",
                "result": "READY_TO_RESOLVE",
                "staged_change": 234567,
            }
        )

        self.assertEqual(outcome.phase_name, "run")
        self.assertEqual(outcome.result_kind, "READY_TO_RESOLVE")
        self.assertEqual(outcome.next_phase, "resolve")
        self.assertIsNone(outcome.resume_bundle)

    def test_phase_outcome_from_blocked_status_uses_trusted_nested_resume_bundle_only(self) -> None:
        runner = object.__new__(SupervisedRunner)

        outcome = runner._phase_outcome_from_status(
            {
                "phase": "resolve",
                "result": "BLOCKED_RETRYABLE",
                "staged_change": 234567,
                "resume_from_phase": "resolve",
                "safe_to_resume": True,
                "resume_command": "legacy top-level resume command",
                "resume_bundle": {
                    "resume_from_phase": "resolve",
                    "resume_target_change": 234567,
                    "safe_to_resume": True,
                    "resume_command": "python .\\p4_weekly_merge.py resolve --change 234567",
                    "verification_passed": True,
                    "remaining_risks": [],
                },
            }
        )

        self.assertEqual(outcome.result_kind, "BLOCKED_RETRYABLE")
        self.assertEqual(outcome.blocked_case_id, "resolve-234567")
        self.assertIsNotNone(outcome.resume_bundle)
        assert outcome.resume_bundle is not None
        self.assertEqual(outcome.resume_bundle.resume_command, "python .\\p4_weekly_merge.py resolve --change 234567")

    def test_handle_recovery_failure_marks_rediagnosis_when_verifier_fails(self) -> None:
        runner = object.__new__(SupervisedRunner)

        exit_code, doctor_status = runner._handle_recovery_failure(
            {
                "result": "RECOVERY_MUTATED_STATE_REQUIRES_REDIAGNOSIS",
                "verification_passed": False,
                "policy_final_action": "retry_resolve_with_charset_override",
            }
        )

        self.assertEqual(exit_code, 10)
        self.assertEqual(doctor_status["result"], "RECOVERY_MUTATED_STATE_REQUIRES_REDIAGNOSIS")

    def test_report_dict_includes_runtime_result_attempted_primitive_and_verifier_outcome(self) -> None:
        runner = object.__new__(SupervisedRunner)
        runner.args = argparse.Namespace(
            source_stream="//ExampleDepot/Mainline_Source",
            target_stream="//ExampleDepot/Release_Target",
            job_tag="Level1",
        )
        state = mock.Mock(
            final_result="SUPERVISION_PAUSED",
            run_attempts=1,
            doctor_cycles=1,
            resumed_from_phase=None,
            resume_change=None,
            child_run_dirs=[],
            child_doctor_dirs=[],
            child_resolve_dirs=[],
            child_sanitize_dirs=[],
            child_conflict_resolution_dirs=[],
            last_blocker_category="charset",
            last_recommended_action="retry_resolve_with_charset_override",
            recovery_executed=True,
            stop_reason="verifier rejected mutated state",
            parent_run_dir=Path(r"S:\example_workspace\public_merge_tool\runs\parent"),
        )

        report = runner._report_dict(
            state,
            run_status={"staged_change": 234567},
            doctor_status={
                "result": "RECOVERY_MUTATED_STATE_REQUIRES_REDIAGNOSIS",
                "policy_final_action": "retry_resolve_with_charset_override",
                "recovery_executed": True,
                "recovery_exit_code": 0,
                "verification_passed": False,
                "failure_reason": "encoding still mismatched",
                "resume_bundle": {
                    "resume_from_phase": "resolve",
                    "resume_target_change": 234567,
                    "safe_to_resume": False,
                    "resume_command": "python .\\p4_weekly_merge.py resolve --change 234567",
                    "verification_passed": False,
                    "remaining_risks": ["charset needs manual inspection"],
                },
            },
        )

        self.assertEqual(report["runtime_result"]["result_kind"], "RECOVERY_MUTATED_STATE_REQUIRES_REDIAGNOSIS")
        self.assertEqual(report["attempted_primitive"]["primitive_id"], "retry_resolve_with_charset_override")
        self.assertFalse(report["verifier_outcome"]["verification_passed"])
        self.assertEqual(report["verifier_outcome"]["resume_bundle"]["resume_target_change"], 234567)

    def test_write_status_artifacts_persists_runtime_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)

            artifact_support.write_status_artifacts(
                run_dir,
                {
                    "phase": "supervise",
                    "result": "SUPERVISION_PAUSED",
                    "reason": "verifier rejected mutated state",
                    "next_action": "re-run doctor on preserved staged change",
                    "runtime_result": {
                        "phase_name": "doctor",
                        "result_kind": "RECOVERY_MUTATED_STATE_REQUIRES_REDIAGNOSIS",
                        "next_phase": None,
                        "blocked_case_id": "doctor-234567",
                        "resume_bundle": {
                            "resume_from_phase": "resolve",
                            "resume_target_change": 234567,
                            "safe_to_resume": False,
                            "resume_command": "python .\\p4_weekly_merge.py resolve --change 234567",
                            "verification_passed": False,
                            "remaining_risks": ["charset needs manual inspection"],
                        },
                    },
                    "attempted_primitive": {
                        "primitive_id": "retry_resolve_with_charset_override",
                        "executed": True,
                        "exit_code": 0,
                        "result_label": "RECOVERY_MUTATED_STATE_REQUIRES_REDIAGNOSIS",
                        "policy_level": "candidate",
                    },
                    "verifier_outcome": {
                        "verification_passed": False,
                        "failure_reason": "encoding still mismatched",
                        "resume_bundle": {
                            "resume_from_phase": "resolve",
                            "resume_target_change": 234567,
                            "safe_to_resume": False,
                            "resume_command": "python .\\p4_weekly_merge.py resolve --change 234567",
                            "verification_passed": False,
                            "remaining_risks": ["charset needs manual inspection"],
                        },
                        "current_opened_file_count": 5,
                        "current_unresolved_file_count": 1,
                    },
                },
            )

            status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
            resume_state = json.loads((run_dir / "resume-state.json").read_text(encoding="utf-8"))

            self.assertEqual(status["runtime_result"]["result_kind"], "RECOVERY_MUTATED_STATE_REQUIRES_REDIAGNOSIS")
            self.assertEqual(status["attempted_primitive"]["primitive_id"], "retry_resolve_with_charset_override")
            self.assertEqual(status["verifier_outcome"]["resume_bundle"]["resume_target_change"], 234567)
            self.assertEqual(resume_state["runtime_result"]["result_kind"], "RECOVERY_MUTATED_STATE_REQUIRES_REDIAGNOSIS")
            self.assertFalse(resume_state["verifier_outcome"]["verification_passed"])


    def test_run_resolve_pipeline_pauses_when_same_blocker_repeats_after_successful_recovery(self) -> None:
        runner = object.__new__(SupervisedRunner)
        runner._dashboard_update = mock.Mock()
        resolve_dirs = [
            Path(r"S:\example_workspace\public_merge_tool\runs\resolve-child-1"),
            Path(r"S:\example_workspace\public_merge_tool\runs\resolve-child-2"),
        ]
        runner._invoke_resolve = mock.Mock(side_effect=[(10, resolve_dirs[0]), (10, resolve_dirs[1])])
        resolve_status = {
            "phase": "resolve",
            "result": "BLOCKED_RETRYABLE",
            "staged_change": 234567,
            "current_change": 234567,
            "blocker_category": "resolve_failed",
            "opened_file_count": 157,
            "unresolved_file_count": 0,
            "failing_command": ["p4", "resolve", "-n", "//ExampleDepot/Release_Target/TaskTool/file.py"],
        }
        runner._read_status = mock.Mock(side_effect=[resolve_status, dict(resolve_status)])
        runner._run_doctor_cycle = mock.Mock(return_value=(0, {
            "phase": "doctor",
            "result": "RECOVERY_EXECUTED_RETRY_SUCCEEDED",
            "verification_passed": True,
            "recovery_preserved_change": 234567,
        }))
        state = mock.Mock(
            child_resolve_dirs=[],
            recovery_executed=False,
            final_result="STARTED",
            stop_reason="",
            last_blocker_category=None,
            last_recommended_action=None,
        )

        exit_code, doctor_status, resolve_status_out, sanitize_status, conflict_status = runner._run_resolve_pipeline(state, 234567)

        self.assertEqual(exit_code, 10)
        self.assertEqual(state.final_result, "SUPERVISION_PAUSED")
        self.assertIn("not converging", state.stop_reason)
        self.assertEqual(state.last_recommended_action, "manual_non_converging_resolve_review_required")
        self.assertEqual(runner._run_doctor_cycle.call_count, 1)
        self.assertEqual(resolve_status_out["result"], "BLOCKED_RETRYABLE")
        self.assertIsNotNone(doctor_status)
        self.assertIsNone(sanitize_status)
        self.assertIsNone(conflict_status)

    def test_executor_charset_retry_supports_resolve_phase_cases(self) -> None:
        blocked_case = make_blocked_case(
            phase="resolve",
            reason=r"Translation of file content failed near line 5 file S:\example_workspace\Project\Tool\Foo.rs",
            p4_cwd=r"S:\example_workspace",
            target_stream="//ExampleDepot/Release_Target",
            resume_from_phase="resolve",
        )
        executor = DoctorExecutor(repo_root=Path.cwd(), runs_dir=Path.cwd(), timeout_seconds=30)

        def fake_run(args: list[str], *, cwd: str | None):
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(executor, "_run_subprocess", side_effect=fake_run):
            result = executor._execute_charset_retry(blocked_case)

        self.assertTrue(result["executed"])
        self.assertEqual(result["retry_phase"], "resolve")
        self.assertEqual(result["recovery_preserved_change"], 234567)
        self.assertEqual(result["recovery_target_count"], 1)

    def test_run_resolve_pipeline_returns_doctor_status_when_verification_pause_occurs(self) -> None:
        runner = object.__new__(SupervisedRunner)
        runner._dashboard_update = mock.Mock()
        runner._invoke_resolve = mock.Mock(return_value=(10, Path(r"S:\example_workspace\public_merge_tool\runs\resolve-child")))
        runner._read_status = mock.Mock(return_value={
            "phase": "resolve",
            "result": "BLOCKED_RETRYABLE",
            "staged_change": 234567,
            "blocker_category": "charset",
        })
        runner._run_doctor_cycle = mock.Mock(return_value=(10, {
            "phase": "doctor",
            "result": "RECOVERY_MUTATED_STATE_REQUIRES_REDIAGNOSIS",
            "policy_final_action": "retry_resolve_with_charset_override",
            "verification_passed": False,
            "failure_reason": "encoding still mismatched",
            "resume_bundle": {
                "resume_from_phase": "resolve",
                "resume_target_change": 234567,
                "safe_to_resume": False,
                "resume_command": r"python .\p4_weekly_merge.py resolve --change 234567",
                "verification_passed": False,
                "remaining_risks": ["charset needs manual inspection"],
            },
        }))
        state = mock.Mock(
            child_resolve_dirs=[],
            recovery_executed=False,
            final_result="SUPERVISION_PAUSED",
            stop_reason="verifier rejected mutated state",
            last_blocker_category=None,
        )

        exit_code, doctor_status, resolve_status, sanitize_status, conflict_status = runner._run_resolve_pipeline(state, 234567)

        self.assertEqual(exit_code, 10)
        self.assertIsNotNone(doctor_status)
        assert doctor_status is not None
        self.assertEqual(doctor_status["result"], "RECOVERY_MUTATED_STATE_REQUIRES_REDIAGNOSIS")
        self.assertEqual(resolve_status["result"], "BLOCKED_RETRYABLE")
        self.assertIsNone(sanitize_status)
        self.assertIsNone(conflict_status)

    def test_report_dict_prefers_live_staged_change_from_trusted_resume_bundle(self) -> None:
        runner = object.__new__(SupervisedRunner)
        runner.args = argparse.Namespace(
            source_stream="//ExampleDepot/Mainline_Source",
            target_stream="//ExampleDepot/Release_Target",
            job_tag="Level1",
        )
        state = mock.Mock(
            final_result="SUPERVISION_PAUSED",
            run_attempts=1,
            doctor_cycles=1,
            resumed_from_phase="resolve",
            resume_change=111111,
            child_run_dirs=[],
            child_doctor_dirs=[],
            child_resolve_dirs=[],
            child_sanitize_dirs=[],
            child_conflict_resolution_dirs=[],
            last_blocker_category="charset",
            last_recommended_action="retry_resolve_with_charset_override",
            recovery_executed=True,
            stop_reason="verifier rejected mutated state",
            parent_run_dir=Path(r"S:\example_workspace\public_merge_tool\runs\parent"),
        )

        report = runner._report_dict(
            state,
            run_status={"staged_change": 999999},
            doctor_status={
                "phase": "doctor",
                "result": "RECOVERY_MUTATED_STATE_REQUIRES_REDIAGNOSIS",
                "policy_final_action": "retry_resolve_with_charset_override",
                "recovery_executed": True,
                "recovery_exit_code": 0,
                "recovery_preserved_change": 222222,
                "verification_passed": False,
                "failure_reason": "encoding still mismatched",
                "resume_bundle": {
                    "resume_from_phase": "resolve",
                    "resume_target_change": 234567,
                    "safe_to_resume": False,
                    "resume_command": r"python .\p4_weekly_merge.py resolve --change 234567",
                    "verification_passed": False,
                    "remaining_risks": ["charset needs manual inspection"],
                },
            },
            resolve_status={"staged_change": 333333, "result": "BLOCKED_RETRYABLE"},
        )

        self.assertEqual(report["staged_change"], 234567)
    def test_executor_isolates_tampered_files_when_resolve_n_finds_nothing(self) -> None:
        blocked_case = make_blocked_case(
            reason=(
                "C:\\ws\\Project\\Plugins\\Foo\\Binaries\\Win64\\UnrealEditor.modules tampered with before resolve - edit or revert."
            ),
            p4_cwd=r"C:\ws",
            target_stream="//ExampleDepot/Release_Target",
        )
        executor = DoctorExecutor(repo_root=Path.cwd(), runs_dir=Path.cwd(), timeout_seconds=30)

        def fake_run(args: list[str], *, cwd: str | None):
            command = tuple(args[1:3])
            if command == ("opened", "-c"):
                return subprocess.CompletedProcess(
                    args,
                    0,
                    "//ExampleDepot/Release_Target/Project/Plugins/Foo/Binaries/Win64/UnrealEditor.modules#1 - edit change 234567 (text)\n",
                    "",
                )
            if command == ("resolve", "-n"):
                return subprocess.CompletedProcess(args, 0, "", "")
            if command == ("change", "-o"):
                return subprocess.CompletedProcess(args, 0, "Change: new\n\nDescription:\n\t<enter description here>\n", "")
            if command == ("reopen", "-c"):
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(f"Unexpected command: {args}")

        with (
            mock.patch.object(executor, "_p4_executable", return_value="p4"),
            mock.patch.object(executor, "_run_subprocess", side_effect=fake_run),
            mock.patch.object(
                executor,
                "_run_subprocess_with_input",
                return_value=subprocess.CompletedProcess(["p4", "change", "-i"], 0, "Change 456789 created.\n", ""),
            ),
        ):
            result = executor._execute_isolate_conflicted_files(blocked_case)

        self.assertEqual(result["retry_result"], "conflicted_files_isolated")
        self.assertEqual(result["recovery_target_count"], 1)
        self.assertEqual(result["recovery_isolated_conflict_change"], 456789)

    def test_executor_isolates_tampered_files_when_opened_output_uses_local_paths(self) -> None:
        blocked_case = make_blocked_case(
            reason=(
                r"S:\example_workspace\Project\Plugins\Foo\Binaries\Win64\UnrealEditor.modules tampered with before resolve - edit or revert."
            ),
            p4_cwd=r"S:\example_workspace",
            target_stream="//ExampleDepot/Release_Target",
        )
        executor = DoctorExecutor(repo_root=Path.cwd(), runs_dir=Path.cwd(), timeout_seconds=30)

        def fake_run(args: list[str], *, cwd: str | None):
            command = tuple(args[1:3])
            if command == ("opened", "-c"):
                return subprocess.CompletedProcess(
                    args,
                    0,
                    r"S:\example_workspace\Project\Plugins\Foo\Binaries\Win64\UnrealEditor.modules#1 - integrate change 234567 (text)\n",
                    "",
                )
            if command == ("resolve", "-n"):
                return subprocess.CompletedProcess(args, 0, "", "")
            if command == ("change", "-o"):
                return subprocess.CompletedProcess(args, 0, "Change: new\n\nDescription:\n\t<enter description here>\n", "")
            if command == ("reopen", "-c"):
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(f"Unexpected command: {args}")

        with (
            mock.patch.object(executor, "_p4_executable", return_value="p4"),
            mock.patch.object(executor, "_run_subprocess", side_effect=fake_run),
            mock.patch.object(
                executor,
                "_run_subprocess_with_input",
                return_value=subprocess.CompletedProcess(["p4", "change", "-i"], 0, "Change 567890 created.\n", ""),
            ),
        ):
            result = executor._execute_isolate_conflicted_files(blocked_case)

        self.assertEqual(result["retry_result"], "conflicted_files_isolated")
        self.assertEqual(result["recovery_target_count"], 1)
        self.assertEqual(result["recovery_isolated_conflict_change"], 567890)

    def test_executor_isolates_unresolved_files_into_new_change(self) -> None:
        blocked_case = make_blocked_case()
        executor = DoctorExecutor(repo_root=Path.cwd(), runs_dir=Path.cwd(), timeout_seconds=30)

        def fake_run(args: list[str], *, cwd: str | None):
            command = tuple(args[1:3])
            if command == ("opened", "-c"):
                return subprocess.CompletedProcess(args, 0, "//depot/A#1 - edit change 234567 (text)\n//depot/B#1 - edit change 234567 (text)\n", "")
            if command == ("resolve", "-n"):
                return subprocess.CompletedProcess(args, 0, "//depot/B#1 - edit change 234567 (text)\n", "")
            if command == ("change", "-o"):
                return subprocess.CompletedProcess(args, 0, "Change: new\n\nDescription:\n\t<enter description here>\n", "")
            if command == ("reopen", "-c"):
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(f"Unexpected command: {args}")

        with (
            mock.patch.object(executor, "_p4_executable", return_value="p4"),
            mock.patch.object(executor, "_run_subprocess", side_effect=fake_run),
            mock.patch.object(
                executor,
                "_run_subprocess_with_input",
                return_value=subprocess.CompletedProcess(["p4", "change", "-i"], 0, "Change 345678 created.\n", ""),
            ),
        ):
            result = executor._execute_isolate_conflicted_files(blocked_case)

        self.assertTrue(result["executed"])
        self.assertEqual(result["retry_phase"], "resolve")
        self.assertEqual(result["retry_result"], "conflicted_files_isolated")
        self.assertEqual(result["recovery_preserved_change"], 234567)
        self.assertEqual(result["recovery_isolated_conflict_change"], 345678)
        self.assertEqual(result["recovery_isolated_conflict_count"], 1)


    def test_recovery_verifier_returns_resume_bundle_for_safe_charset_retry(self) -> None:
        verifier = RecoveryVerifier()

        result = verifier.verify(
            primitive_id="retry_resolve_with_charset_override",
            blocked_case=make_blocked_case(phase="run", resume_from_phase="run"),
            execution_result={
                "executed": True,
                "exit_code": 0,
                "retry_phase": "run",
                "recovery_preserved_change": 234567,
            },
            verification_plan={
                "strategy_id": "verify_charset_retry",
                "requires_verification": True,
                "resume_from_phase": "run",
                "checks": ["preserved_change_present"],
            },
        )

        self.assertTrue(result.verification_passed)
        self.assertIsInstance(result.resume_bundle, ResumeBundle)
        assert result.resume_bundle is not None
        self.assertEqual(result.resume_bundle.resume_target_change, 234567)
        self.assertTrue(result.resume_bundle.verification_passed)

    def test_recovery_verifier_requires_rediagnosis_when_preserved_change_missing(self) -> None:
        verifier = RecoveryVerifier()

        result = verifier.verify(
            primitive_id="retry_resolve_with_charset_override",
            blocked_case=make_blocked_case(phase="run", staged_change=None, resume_from_phase="run"),
            execution_result={
                "executed": True,
                "exit_code": 0,
                "retry_phase": "run",
                "recovery_preserved_change": None,
            },
            verification_plan={
                "strategy_id": "verify_charset_retry",
                "requires_verification": True,
                "resume_from_phase": "run",
                "checks": ["preserved_change_present"],
            },
        )

        self.assertFalse(result.verification_passed)
        self.assertIn("preserved", result.failure_reason or "")
        self.assertIsNone(result.resume_bundle)

    def test_executor_attaches_trusted_resume_bundle_from_verifier(self) -> None:
        blocked_case = make_blocked_case(phase="run", resume_from_phase="run")
        policy_decision = mock.Mock(
            execute_recovery=True,
            final_action="retry_resolve_with_charset_override",
            recovery_primitive_id="retry_resolve_with_charset_override",
            verification_plan={
                "strategy_id": "verify_charset_retry",
                "requires_verification": True,
                "resume_from_phase": "run",
                "checks": ["preserved_change_present"],
            },
        )
        executor = DoctorExecutor(repo_root=Path.cwd(), runs_dir=Path.cwd(), timeout_seconds=30)

        with mock.patch.object(
            executor,
            "_execute_charset_retry",
            return_value={
                "executed": True,
                "result": "recovery_executed_recovery_step_completed",
                "command": "p4 -C utf8 resolve -am //ExampleDepot/...",
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "retry_run_dir": None,
                "retry_phase": "run",
                "retry_result": "resolve_rechecked_after_charset_override",
                "recovery_target_scope": "targeted_files",
                "recovery_target_count": 1,
                "recovery_preserved_change": 234567,
            },
        ):
            result = executor.execute(blocked_case, policy_decision)

        self.assertTrue(result["verification_passed"])
        self.assertIsNotNone(result["resume_bundle"])
        self.assertEqual(result["resume_bundle"]["resume_target_change"], 234567)
        self.assertEqual(result["result"], "recovery_executed_retry_succeeded")

    def test_executor_distinguishes_verifier_failure_from_executor_failure(self) -> None:
        blocked_case = make_blocked_case(phase="run", staged_change=None, resume_from_phase="run")
        policy_decision = mock.Mock(
            execute_recovery=True,
            final_action="retry_resolve_with_charset_override",
            recovery_primitive_id="retry_resolve_with_charset_override",
            verification_plan={
                "strategy_id": "verify_charset_retry",
                "requires_verification": True,
                "resume_from_phase": "run",
                "checks": ["preserved_change_present"],
            },
        )
        executor = DoctorExecutor(repo_root=Path.cwd(), runs_dir=Path.cwd(), timeout_seconds=30)

        with mock.patch.object(
            executor,
            "_execute_charset_retry",
            return_value={
                "executed": True,
                "result": "recovery_executed_recovery_step_completed",
                "command": "p4 -C utf8 resolve -am //ExampleDepot/...",
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "retry_run_dir": None,
                "retry_phase": "run",
                "retry_result": "resolve_rechecked_after_charset_override",
                "recovery_target_scope": "targeted_files",
                "recovery_target_count": 1,
                "recovery_preserved_change": None,
            },
        ):
            result = executor.execute(blocked_case, policy_decision)

        self.assertTrue(result["executed"])
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["verification_passed"])
        self.assertEqual(result["result"], "recovery_mutated_state_requires_rediagnosis")
        self.assertEqual(result["verifier_outcome"]["failure_reason"], result["failure_reason"])

    def test_doctor_phase_report_emits_policy_tracking_from_prior_attempts(self) -> None:
        blocked_case = make_blocked_case(
            batch_changes=[{"batch": "plugins", "change": 234567}],
            prior_attempts=[],
        )
        llm_decision = DoctorLLMDecision(
            failure_type="resolve_failed",
            confidence=0.99,
            recommended_action="accept_source",
            reasoning_summary="accept source for repeated plugin binary conflict",
            safe_to_resume=False,
            resume_from_phase="resolve",
            needs_human_review=True,
            recovery_primitive_id=None,
            safe_to_execute=False,
            verification_plan={},
            prior_attempts=[
                {
                    "pattern": {
                        "phase": "resolve",
                        "batch": "plugins",
                        "path_family": "Project/Plugins/Foo/Binaries/...",
                        "filetype": "binary",
                        "blocker_type": "resolve_failed",
                        "suggested_action": "accept_source",
                    },
                    "human_action": "accept_source",
                    "resumed_cleanly": True,
                },
                {
                    "pattern": {
                        "phase": "resolve",
                        "batch": "plugins",
                        "path_family": "Project/Plugins/Foo/Binaries/...",
                        "filetype": "binary",
                        "blocker_type": "resolve_failed",
                        "suggested_action": "accept_source",
                    },
                    "human_action": "accept_source",
                    "resumed_cleanly": True,
                },
                {
                    "pattern": {
                        "phase": "resolve",
                        "batch": "plugins",
                        "path_family": "Project/Plugins/Foo/Binaries/...",
                        "filetype": "binary",
                        "blocker_type": "resolve_failed",
                        "suggested_action": "accept_source",
                    },
                    "human_action": "accept_source",
                    "resumed_cleanly": True,
                },
            ],
        )
        policy_decision = DoctorPolicy(min_confidence=0.85).evaluate(
            blocked_case,
            llm_decision,
            execution_enabled=False,
        )

        result = DoctorPhase._build_doctor_report_payload(
            prior_run_dir=Path(r"S:\example_workspace\public_merge_tool\runs\fake"),
            prior_status={"phase": "resolve", "result": "BLOCKED_RETRYABLE", "staged_change": 234567},
            blocked_case=blocked_case,
            llm_decision=llm_decision,
            llm_mode="deterministic",
            doctor_case=DoctorEngine.llm_decision_to_doctor_decision(blocked_case, llm_decision).to_report_dict(),
            policy_decision=policy_decision,
            resume_state=ResumeState(
                blocked_phase="resolve",
                resume_from_phase="resolve",
                safe_to_resume=False,
                resume_command=r"python .\p4_weekly_merge.py resolve --change 234567",
                selected_cl=123456,
                staged_change=234567,
                pending_changes=[234567],
                last_successful_step="resolve paused",
                failed_step=["p4", "resolve", "-am"],
                human_action_required=True,
                why_doctor_stopped="manual accept-source validation required",
                prior_run_dir=r"S:\example_workspace\public_merge_tool\runs\fake",
            ).to_report_dict(),
            execution_result={
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
            },
        )

        self.assertEqual(result["policy_observations"][0]["policy_level"], "shadow-validated")
        self.assertEqual(result["policy_promotion_candidates"][0]["matched_clean_resumes"], 3)

    def test_doctor_phase_report_distinguishes_executor_failure_and_verifier_failure(self) -> None:
        blocked_case = make_blocked_case(phase="run", resume_from_phase="run")
        llm_decision = DoctorLLMDecision(
            failure_type="charset_translation_failure",
            confidence=0.99,
            recommended_action="retry_resolve_with_charset_override",
            reasoning_summary="retry with utf8 charset",
            safe_to_resume=True,
            resume_from_phase="run",
            needs_human_review=False,
            recovery_primitive_id="retry_resolve_with_charset_override",
            safe_to_execute=True,
            verification_plan={
                "strategy_id": "verify_charset_retry",
                "requires_verification": True,
                "resume_from_phase": "run",
                "checks": ["preserved_change_present"],
            },
        )
        policy_decision = DoctorPolicy(min_confidence=0.85).evaluate(
            blocked_case,
            llm_decision,
            execution_enabled=True,
        )
        execution_result = {
            "executed": True,
            "result": "recovery_mutated_state_requires_rediagnosis",
            "command": "p4 -C utf8 resolve -am //ExampleDepot/...",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "retry_run_dir": None,
            "retry_phase": "run",
            "retry_result": "resolve_rechecked_after_charset_override",
            "recovery_target_scope": "targeted_files",
            "recovery_target_count": 1,
            "recovery_preserved_change": None,
            "verification_passed": False,
            "failure_reason": "preserved staged change is missing after charset retry",
            "verifier_outcome": {
                "verification_passed": False,
                "failure_reason": "preserved staged change is missing after charset retry",
                "resume_bundle": None,
                "current_opened_file_count": 230,
                "current_unresolved_file_count": 0,
            },
            "resume_bundle": None,
        }

        result = DoctorPhase._build_doctor_report_payload(
            prior_run_dir=Path(r"S:\example_workspace\public_merge_tool\runs\fake"),
            prior_status={"phase": "run", "result": "BLOCKED_RETRYABLE", "staged_change": 234567},
            blocked_case=blocked_case,
            llm_decision=llm_decision,
            llm_mode="deterministic",
            doctor_case=DoctorEngine.llm_decision_to_doctor_decision(blocked_case, llm_decision).to_report_dict(),
            policy_decision=policy_decision,
            resume_state=ResumeState(
                blocked_phase="run",
                resume_from_phase="run",
                safe_to_resume=True,
                resume_command=r"python .\p4_weekly_merge.py run --change 234567",
                selected_cl=123456,
                staged_change=234567,
                pending_changes=[234567],
                last_successful_step="opened target files",
                failed_step=["p4", "resolve", "-am"],
                human_action_required=False,
                why_doctor_stopped="run can resume with preserved staged change",
                prior_run_dir=r"S:\example_workspace\public_merge_tool\runs\fake",
            ).to_report_dict(),
            execution_result=execution_result,
        )

        self.assertEqual(result["result"], "RECOVERY_MUTATED_STATE_REQUIRES_REDIAGNOSIS")
        self.assertTrue(result["recovery_executed"])
        self.assertEqual(result["recovery_exit_code"], 0)
        self.assertFalse(result["verification_passed"])
        self.assertEqual(result["failure_reason"], "preserved staged change is missing after charset retry")


if __name__ == "__main__":
    unittest.main()








