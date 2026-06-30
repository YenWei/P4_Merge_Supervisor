from __future__ import annotations

import json
from pathlib import Path


def build_batch_preview_summary(batch_merge_paths: dict[str, list], path_results: list[dict]) -> list[dict]:
    summary = []
    path_index = 0
    for batch_name, merge_paths in batch_merge_paths.items():
        batch_count = 0
        for _ in merge_paths:
            if path_index >= len(path_results):
                break
            batch_count += path_results[path_index]["preview_file_count"]
            path_index += 1
        summary.append({"batch": batch_name, "preview_file_count": batch_count})
    return summary


def write_status_artifacts(run_dir: Path, report: dict) -> None:
    status_report = {
        "phase": report["phase"],
        "result": report["result"],
        "blocker_category": report.get("blocker_category"),
        "retryable": report.get("retryable", False),
        "reason": report.get("reason"),
        "next_action": report.get("next_action"),
        "source_stream": report.get("source_stream"),
        "target_stream": report.get("target_stream"),
        "job_tag": report.get("job_tag"),
        "selected_cl": report.get("selected_cl"),
        "staged_change": report.get("staged_change"),
        "batch_changes": report.get("batch_changes", []),
        "batches": report.get("batches", []),
        "merge_paths": report.get("merge_paths", []),
        "preview_file_count": report.get("total_preview_file_count", 0),
        "opened_file_count": report.get("opened_file_count", 0),
        "unresolved_file_count": report.get("unresolved_file_count", 0),
        "successful_batches": report.get("successful_batches", []),
        "failed_batches": report.get("failed_batches", []),
        "conflict_buckets": report.get("conflict_buckets", []),
        "failing_command": report.get("failing_command"),
        "failure_type": report.get("failure_type"),
        "confidence": report.get("confidence"),
        "recommended_action": report.get("recommended_action"),
        "requires_human_review": report.get("requires_human_review"),
        "allowed": report.get("allowed"),
        "prior_phase": report.get("prior_phase"),
        "prior_result": report.get("prior_result"),
        "prior_run_dir": report.get("prior_run_dir"),
        "resume_from_phase": report.get("resume_from_phase"),
        "safe_to_resume": report.get("safe_to_resume"),
        "resume_command": report.get("resume_command"),
        "runtime_result": report.get("runtime_result"),
        "attempted_primitive": report.get("attempted_primitive"),
        "verifier_outcome": report.get("verifier_outcome"),
        "run_dir": str(run_dir),
        "timestamp": report.get("timestamp"),
        "current_batch": report.get("current_batch"),
        "current_change": report.get("current_change"),
        "current_pass_index": report.get("current_pass_index"),
        "current_pass_count": report.get("current_pass_count"),
        "current_step_label": report.get("current_step_label"),
        "current_command_chunk_index": report.get("current_command_chunk_index"),
        "current_command_chunk_count": report.get("current_command_chunk_count"),
        "last_successful_step": report.get("last_successful_step"),
        "failed_step": report.get("failed_step"),
    }
    (run_dir / "status.json").write_text(json.dumps(status_report, indent=2), encoding="utf-8")

    status_lines = [
        f"phase: {status_report['phase']}" ,
        f"result: {status_report['result']}" ,
        f"category: {status_report['blocker_category'] or 'none'}" ,
        f"retryable: {'yes' if status_report['retryable'] else 'no'}" ,
        "",
        "reason:",
        status_report["reason"] or "none",
        "",
        "next action:",
        status_report["next_action"] or "none",
        "",
        f"opened file count: {status_report['opened_file_count']}" ,
        f"unresolved file count: {status_report['unresolved_file_count']}" ,
    ]
    if status_report["phase"] == "doctor":
        status_lines.extend(
            [
                "",
                f"failure type: {status_report['failure_type'] or 'unknown'}" ,
                f"confidence: {status_report['confidence'] or 'unknown'}" ,
                f"recommended action: {status_report['recommended_action'] or 'none'}" ,
                f"requires human review: {'yes' if status_report.get('requires_human_review') else 'no'}" ,
                f"allowed: {'yes' if status_report.get('allowed') else 'no'}" ,
                f"resume from phase: {status_report.get('resume_from_phase') or 'unknown'}" ,
                f"safe to resume: {'yes' if status_report.get('safe_to_resume') else 'no'}" ,
                f"resume command: {status_report.get('resume_command') or 'none'}" ,
                f"runtime result: {(status_report.get('runtime_result') or {}).get('result_kind') or 'none'}" ,
                f"attempted primitive: {(status_report.get('attempted_primitive') or {}).get('primitive_id') or 'none'}" ,
                f"verifier passed: {'yes' if (status_report.get('verifier_outcome') or {}).get('verification_passed') else 'no' if status_report.get('verifier_outcome') is not None else 'unknown'}" ,
            ]
        )
    if status_report.get("current_batch"):
        status_lines.extend(
            [
                "",
                f"current batch: {status_report.get('current_batch')}" ,
                f"current staged change: {status_report.get('current_change') or '-'}" ,
                f"current pass: {status_report.get('current_pass_index') or '-'}/{status_report.get('current_pass_count') or '-'}" ,
                f"current step label: {status_report.get('current_step_label') or '-'}" ,
                f"current command chunk: {status_report.get('current_command_chunk_index') or '-'}/{status_report.get('current_command_chunk_count') or '-'}" ,
            ]
        )
    if status_report.get("last_successful_step"):
        status_lines.extend(["", f"last successful step: {status_report.get('last_successful_step')}"])
    if status_report.get("failed_step"):
        status_lines.extend(["", f"failed step: {status_report.get('failed_step')}"])
    status_lines.extend(["", "run dir:", str(run_dir)])
    (run_dir / "status.txt").write_text("\n".join(status_lines) + "\n", encoding="utf-8")
    write_operator_artifacts(run_dir, report)


def write_command_logs(run_dir: Path, p4) -> None:
    command_lines = []
    error_lines = []
    for result in p4.results:
        command_lines.append("$ " + " ".join(result.args))
        if result.stdout.strip():
            command_lines.append(result.stdout.rstrip())
        if result.stderr.strip():
            error_lines.append("$ " + " ".join(result.args))
            error_lines.append(result.stderr.rstrip())
    (run_dir / "p4-commands.log").write_text("\n".join(command_lines) + "\n", encoding="utf-8")
    (run_dir / "p4-errors.log").write_text("\n".join(error_lines) + "\n", encoding="utf-8")


def build_operator_summary_lines(report: dict) -> list[str]:
    completed_phases = report.get("completed_phases", [])
    blocker_category = report.get("blocker_category") or report.get("last_blocker_category") or "none"
    blocker_reason = report.get("reason") or report.get("stop_reason") or "none"
    stopped_phase = report.get("stopped_phase") or report.get("prior_phase") or report.get("phase") or "unknown"
    next_action = report.get("next_action") or "none"
    safe_to_resume = report.get("safe_to_resume")
    resume_command = report.get("resume_command")
    runtime_result = report.get("runtime_result") or {}
    attempted_primitive = report.get("attempted_primitive") or {}
    verifier_outcome = report.get("verifier_outcome")
    inspect_command = report.get("inspect_command")
    lines = [
        f"outcome: {report.get('result') or report.get('status') or 'unknown'}" ,
        f"stopped phase: {stopped_phase}" ,
        f"completed phases: {', '.join(completed_phases) if completed_phases else 'none'}" ,
        f"blocker category: {blocker_category}" ,
        "",
        "what happened:",
        blocker_reason,
        "",
        f"safe to continue in place: {'yes' if safe_to_resume else 'no'}" if safe_to_resume is not None else "safe to continue in place: unknown",
        "",
        "next step:",
        next_action,
    ]
    if report.get("current_batch") or report.get("current_change"):
        lines.extend(
            [
                "",
                "current work:",
                f"batch: {report.get('current_batch') or '-'}" ,
                f"staged change: {report.get('current_change') or '-'}" ,
                f"pass: {report.get('current_pass_index') or '-'}/{report.get('current_pass_count') or '-'}" ,
                f"step: {report.get('current_step_label') or '-'}" ,
                f"command chunk: {report.get('current_command_chunk_index') or '-'}/{report.get('current_command_chunk_count') or '-'}" ,
            ]
        )
    if report.get("last_successful_step"):
        lines.extend(["", "last successful step:", str(report.get("last_successful_step"))])
    if report.get("failed_step"):
        lines.extend(["", "failed step:", str(report.get("failed_step"))])
    opened_count = report.get("opened_file_count")
    unresolved_count = report.get("unresolved_file_count")
    if opened_count is not None or unresolved_count is not None:
        lines.extend(
            [
                "",
                f"opened file count: {opened_count if opened_count is not None else '-'}" ,
                f"unresolved file count: {unresolved_count if unresolved_count is not None else '-'}" ,
            ]
        )
    if resume_command:
        lines.extend(["", "resume command:", resume_command])
    if runtime_result:
        lines.extend(["", f"runtime result: {runtime_result.get('result_kind') or 'unknown'}"])
    if attempted_primitive:
        lines.extend([f"attempted primitive: {attempted_primitive.get('primitive_id') or 'unknown'}"])
    if verifier_outcome is not None:
        verifier_label = 'yes' if verifier_outcome.get('verification_passed') else 'no'
        lines.extend([f"verifier passed: {verifier_label}"])
    if inspect_command:
        lines.extend(["", "inspect command:", inspect_command])
    return lines


def write_operator_artifacts(run_dir: Path, report: dict) -> None:
    inspect_command = report.get("inspect_command") or f"Get-Content -Path '{run_dir}\\operator-summary.txt'"
    operator_state = {
        "result": report.get("result") or report.get("status"),
        "phase": report.get("phase"),
        "stopped_phase": report.get("stopped_phase") or report.get("prior_phase") or report.get("phase"),
        "completed_phases": report.get("completed_phases", []),
        "blocker_category": report.get("blocker_category") or report.get("last_blocker_category"),
        "reason": report.get("reason") or report.get("stop_reason"),
        "next_action": report.get("next_action"),
        "safe_to_resume": report.get("safe_to_resume"),
        "resume_command": report.get("resume_command"),
        "runtime_result": report.get("runtime_result"),
        "attempted_primitive": report.get("attempted_primitive"),
        "verifier_outcome": report.get("verifier_outcome"),
        "inspect_command": inspect_command,
        "selected_cl": report.get("selected_cl"),
        "staged_change": report.get("staged_change"),
        "opened_file_count": report.get("opened_file_count"),
        "unresolved_file_count": report.get("unresolved_file_count"),
        "last_successful_step": report.get("last_successful_step"),
        "failed_step": report.get("failed_step"),
        "current_batch": report.get("current_batch"),
        "current_change": report.get("current_change"),
        "current_pass_index": report.get("current_pass_index"),
        "current_pass_count": report.get("current_pass_count"),
        "current_step_label": report.get("current_step_label"),
        "current_command_chunk_index": report.get("current_command_chunk_index"),
        "current_command_chunk_count": report.get("current_command_chunk_count"),
        "run_dir": str(run_dir),
        "timestamp": report.get("timestamp"),
    }
    operator_report = dict(report)
    operator_report["inspect_command"] = inspect_command
    (run_dir / "resume-state.json").write_text(json.dumps(operator_state, indent=2), encoding="utf-8")
    (run_dir / "operator-summary.txt").write_text("\n".join(build_operator_summary_lines(operator_report)) + "\n", encoding="utf-8")


def build_batch_run_changelist_description(args, selected_cl: int, batch_name: str) -> str:
    lines = [
        f"Batch merge from {args.source_stream}@{selected_cl} into {args.target_stream}",
        "",
        f"Job tag: {args.job_tag}",
        f"Batch: {batch_name}",
        "Created automatically by run phase.",
        "Resolve has not been run yet.",
        "No submit was performed.",
    ]
    return "\n".join(lines)


def find_latest_run_status(runs_dir: Path, staged_change: int | None = None) -> tuple[Path, dict] | tuple[None, None]:
    status_files = sorted(runs_dir.glob("*/status.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for status_file in status_files:
        status = json.loads(status_file.read_text(encoding="utf-8"))
        if status.get("phase") != "run":
            continue
        if staged_change is None or status.get("staged_change") == staged_change or any(
            int(entry.get("change")) == staged_change
            for entry in status.get("batch_changes", [])
            if entry.get("change") is not None
        ):
            return status_file.parent, status
    return None, None


def determine_split_input(args, *, error_cls):
    runs_dir = Path(args.runs_dir)
    staged_change = args.change
    if getattr(args, "allow_recovered_blocked_run", False):
        if staged_change is None:
            raise error_cls("Recovered blocked run split requires --change with the manually staged pending changelist.")
        run_dir, status = None, None
        status_files = sorted(runs_dir.glob("*/status.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for status_file in status_files:
            candidate = json.loads(status_file.read_text(encoding="utf-8"))
            if candidate.get("phase") != "run":
                continue
            if candidate.get("result") not in {"BLOCKED_RETRYABLE", "BLOCKED_HUMAN"}:
                continue
            run_dir, status = status_file.parent, candidate
            break
    else:
        run_dir, status = find_latest_run_status(runs_dir, staged_change=staged_change)
    if status is None:
        if staged_change is None:
            raise error_cls("Could not find a prior run status.json with a staged pending changelist to split.")
        raise error_cls(f"Could not find a prior run status.json for staged pending changelist {staged_change}.")
    if status.get("phase") != "run":
        raise error_cls("Selected status artifact is not from the run phase.")
    if getattr(args, "allow_recovered_blocked_run", False):
        if status.get("result") not in {"BLOCKED_RETRYABLE", "BLOCKED_HUMAN"}:
            raise error_cls(f"Recovered split override requires a blocked run artifact, got {status.get('result')!r}.")
        staged_change = int(staged_change)
        status = dict(status)
        status["staged_change"] = staged_change
        return run_dir, status, staged_change
    staged_change = status.get("staged_change")
    if not staged_change:
        raise error_cls("Selected run status does not contain a staged pending changelist number.")
    if status.get("result") not in {"READY_TO_SPLIT", "SPLIT_WITH_CONFLICT_BUCKETS"}:
        raise error_cls(f"Run result {status.get('result')!r} is not eligible for split.")
    return run_dir, status, int(staged_change)


def determine_resolve_input(args, *, error_cls):
    runs_dir = Path(args.runs_dir)
    run_dir, status = find_latest_run_status(runs_dir, staged_change=args.change)
    if status is None:
        if args.change is None:
            raise error_cls("Could not find a prior run status.json with batch changelists to resolve.")
        raise error_cls(f"Could not find a prior run status.json for staged batch changelist {args.change}.")
    if status.get("phase") != "run":
        raise error_cls("Selected status artifact is not from the run phase.")
    if status.get("result") not in {"READY_TO_RESOLVE", "BLOCKED_RETRYABLE", "BLOCKED_HUMAN"}:
        raise error_cls(f"Run result {status.get('result')!r} is not eligible for resolve.")
    batch_changes = list(status.get("batch_changes", []))
    if args.change is not None:
        batch_changes = [entry for entry in batch_changes if int(entry.get("change")) == int(args.change)]
    if not batch_changes:
        raise error_cls("Selected run status does not contain any eligible batch changelists to resolve.")
    return run_dir, status, batch_changes


def create_conflict_bucket_changelist(p4, bucket_name: str, parent_change: int, *, create_numbered_pending_changelist) -> int:
    normalized_bucket_name = bucket_name
    if not normalized_bucket_name.startswith("conflict-"):
        normalized_bucket_name = f"conflict-{normalized_bucket_name}-unresolved"
    description = "\n".join(
        [
            f"Conflict bucket from resolved batch CL {parent_change}",
            "",
            f"Bucket: {normalized_bucket_name}",
            f"Parent batch change: {parent_change}",
            "Created automatically by resolve phase.",
            "No submit was performed.",
        ]
    )
    return create_numbered_pending_changelist(p4, description)


def _write_summary_files(run_dir: Path, summary_name: str, summary_data: dict, summary_lines: list[str], submit_plan_lines: list[str] | None = None) -> None:
    (run_dir / f"{summary_name}.json").write_text(json.dumps(summary_data, indent=2), encoding="utf-8")
    (run_dir / f"{summary_name}.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    if submit_plan_lines is not None:
        (run_dir / "submit-plan.txt").write_text("\n".join(submit_plan_lines) + "\n", encoding="utf-8")


def write_split_report(run_dir: Path, report: dict, *, status_writer, command_log_writer, p4=None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    split_summary = {
        "status": report["status"],
        "phase": report["phase"],
        "source_stream": report.get("source_stream"),
        "target_stream": report.get("target_stream"),
        "staged_change": report.get("staged_change"),
        "split_changes": report.get("split_changes", []),
        "bucket_summaries": report.get("bucket_summaries", []),
        "opened_file_count": report.get("opened_file_count", 0),
        "unresolved_file_count": report.get("unresolved_file_count", 0),
        "result": report.get("result"),
        "reason": report.get("reason"),
        "next_action": report.get("next_action"),
        "timestamp": report.get("timestamp"),
    }
    summary_lines = [
        f"status: {report['status']}",
        f"source: {report['source_stream']}",
        f"target: {report['target_stream']}",
        f"staged pending CL: {report.get('staged_change')}",
        f"opened file count: {report.get('opened_file_count', 0)}",
        f"unresolved file count: {report.get('unresolved_file_count', 0)}",
        "",
        "bucket summaries:",
    ]
    for bucket in report.get("bucket_summaries", []):
        summary_lines.append(
            f"  {bucket['bucket']}: {bucket['file_count']} file(s) -> change {bucket['change']} "
            f"(chunks={bucket.get('chunk_count', '?')})"
        )
    if "error" in report:
        summary_lines.extend(["", f"error: {report['error']}"])
    submit_plan_lines = ["Split review order:"]
    for bucket in report.get("bucket_summaries", []):
        submit_plan_lines.append(
            f"- {bucket['bucket']}: change {bucket['change']} "
            f"({bucket['file_count']} file(s), chunks={bucket.get('chunk_count', '?')})"
        )
    _write_summary_files(run_dir, "split-summary", split_summary, summary_lines, submit_plan_lines)
    status_writer(run_dir, report)
    write_operator_artifacts(run_dir, report)
    if p4 is not None:
        command_log_writer(run_dir, p4)


def find_latest_resolve_summary(runs_dir: Path, change: int | None = None) -> tuple[Path, dict] | tuple[None, None]:
    summary_files = sorted(runs_dir.glob("*/resolve-summary.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for summary_file in summary_files:
        summary = json.loads(summary_file.read_text(encoding="utf-8"))
        if change is None:
            return summary_file.parent, summary
        if any(int(bucket.get("change")) == int(change) for bucket in summary.get("bucket_summaries", []) if bucket.get("change") is not None):
            return summary_file.parent, summary
    return None, None


def write_resolve_report(run_dir: Path, report: dict, *, status_writer, command_log_writer, p4=None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    resolve_summary = {
        "status": report["status"],
        "phase": report["phase"],
        "source_stream": report.get("source_stream"),
        "target_stream": report.get("target_stream"),
        "selected_cl": report.get("selected_cl"),
        "batch_changes": report.get("batch_changes", []),
        "resolved_batches": report.get("resolved_batches", []),
        "bucket_summaries": report.get("bucket_summaries", []),
        "opened_file_count": report.get("opened_file_count", 0),
        "unresolved_file_count": report.get("unresolved_file_count", 0),
        "result": report.get("result"),
        "reason": report.get("reason"),
        "next_action": report.get("next_action"),
        "timestamp": report.get("timestamp"),
    }
    summary_lines = [
        f"status: {report['status']}",
        f"source: {report['source_stream']}",
        f"target: {report['target_stream']}",
        f"selected CL: {report.get('selected_cl')}",
        f"opened file count: {report.get('opened_file_count', 0)}",
        f"unresolved file count: {report.get('unresolved_file_count', 0)}",
        "",
        "resolve bucket summaries:",
    ]
    for bucket in report.get("bucket_summaries", []):
        summary_lines.append(
            "  "
            + f"{bucket['bucket']}: change {bucket['change']} "
            + f"opened_before={bucket.get('opened_before', 0)} "
            + f"opened_after={bucket.get('opened_after', 0)} "
            + f"unresolved_after={bucket.get('unresolved_after', 0)} "
            + f"action={bucket.get('action', 'preserved')} "
            + f"passes={bucket.get('pass_count', '?')} "
            + f"cmd_limit={bucket.get('command_chunk_limit', '?')}"
        )
    if "error" in report:
        summary_lines.extend(["", f"error: {report['error']}"])
    _write_summary_files(run_dir, "resolve-summary", resolve_summary, summary_lines)
    status_writer(run_dir, report)
    write_operator_artifacts(run_dir, report)
    if p4 is not None:
        command_log_writer(run_dir, p4)


def find_latest_split_summary(runs_dir: Path, staged_change: int | None = None) -> tuple[Path, dict] | tuple[None, None]:
    summary_files = sorted(runs_dir.glob("*/split-summary.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for summary_file in summary_files:
        summary = json.loads(summary_file.read_text(encoding="utf-8"))
        if staged_change is None or summary.get("staged_change") == staged_change:
            return summary_file.parent, summary
    return None, None


def determine_sanitize_input(args, *, error_cls):
    runs_dir = Path(args.runs_dir)
    staged_change = args.change
    resolve_run_dir, resolve_summary = find_latest_resolve_summary(runs_dir, change=staged_change)
    if resolve_summary is not None:
        resolved_change = staged_change
        if resolved_change is None:
            for bucket in resolve_summary.get("bucket_summaries", []):
                if bucket.get("change") is not None:
                    resolved_change = int(bucket["change"])
                    break
        if resolved_change is None:
            raise error_cls("Selected resolve summary does not contain a staged batch changelist number.")
        return resolve_run_dir, resolve_summary, int(resolved_change)
    split_run_dir, split_summary = find_latest_split_summary(runs_dir, staged_change=staged_change)
    if split_summary is None:
        if staged_change is None:
            raise error_cls("Could not find a prior resolve-summary.json or split-summary.json to sanitize.")
        raise error_cls(f"Could not find a prior resolve-summary.json or split-summary.json for staged pending changelist {staged_change}.")
    staged_change = split_summary.get("staged_change")
    if not staged_change:
        raise error_cls("Selected split summary does not contain a staged pending changelist number.")
    if split_summary.get("phase") not in {"split", "resolve"}:
        raise error_cls("Selected summary artifact is not from the split or resolve phase.")
    if split_summary.get("result") not in {"READY_FOR_REVIEW", "REVIEW_WITH_CONFLICT_BUCKETS"}:
        raise error_cls(f"Split/resolve result {split_summary.get('result')!r} is not eligible for sanitize.")
    return split_run_dir, split_summary, int(staged_change)


def find_latest_sanitize_summary(runs_dir: Path, staged_change: int | None = None) -> tuple[Path, dict] | tuple[None, None]:
    summary_files = sorted(runs_dir.glob("*/sanitize-summary.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for summary_file in summary_files:
        summary = json.loads(summary_file.read_text(encoding="utf-8"))
        if staged_change is None or summary.get("staged_change") == staged_change:
            return summary_file.parent, summary
    return None, None


def determine_conflict_resolution_input(args, *, error_cls):
    runs_dir = Path(args.runs_dir)
    staged_change = args.change
    sanitize_run_dir, sanitize_summary = find_latest_sanitize_summary(runs_dir, staged_change=staged_change)
    if sanitize_summary is None:
        if staged_change is None:
            raise error_cls("Could not find a prior sanitize-summary.json to resolve conflicts from.")
        raise error_cls(f"Could not find a prior sanitize-summary.json for staged pending changelist {staged_change}.")
    staged_change = sanitize_summary.get("staged_change")
    if not staged_change:
        raise error_cls("Selected sanitize summary does not contain a staged pending changelist number.")
    if sanitize_summary.get("phase") != "sanitize":
        raise error_cls("Selected summary artifact is not from the sanitize phase.")
    if sanitize_summary.get("result") not in {"READY_FOR_REVIEW", "REVIEW_WITH_CONFLICT_BUCKETS"}:
        raise error_cls(f"Sanitize result {sanitize_summary.get('result')!r} is not eligible for resolve-conflicts.")
    return sanitize_run_dir, sanitize_summary, int(staged_change)


def write_sanitize_report(run_dir: Path, report: dict, *, status_writer, command_log_writer, p4=None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    sanitize_summary = {
        "status": report["status"],
        "phase": report["phase"],
        "source_stream": report.get("source_stream"),
        "target_stream": report.get("target_stream"),
        "staged_change": report.get("staged_change"),
        "sanitized_changes": report.get("sanitized_changes", []),
        "bucket_summaries": report.get("bucket_summaries", []),
        "opened_file_count": report.get("opened_file_count", 0),
        "unresolved_file_count": report.get("unresolved_file_count", 0),
        "result": report.get("result"),
        "reason": report.get("reason"),
        "next_action": report.get("next_action"),
        "timestamp": report.get("timestamp"),
    }
    summary_lines = [
        f"status: {report['status']}",
        f"source: {report['source_stream']}",
        f"target: {report['target_stream']}",
        f"staged pending CL: {report.get('staged_change')}",
        f"opened file count: {report.get('opened_file_count', 0)}",
        f"unresolved file count: {report.get('unresolved_file_count', 0)}",
        "",
        "sanitize bucket summaries:",
    ]
    for bucket in report.get("bucket_summaries", []):
        summary_lines.append(
            f"  {bucket['bucket']}: change {bucket['change']} opened_before={bucket['opened_before']} opened_after={bucket['opened_after']}"
        )
    if "error" in report:
        summary_lines.extend(["", f"error: {report['error']}"])
    submit_plan_lines = ["Sanitized review order:"]
    for bucket in report.get("bucket_summaries", []):
        submit_plan_lines.append(
            f"- {bucket['bucket']}: change {bucket['change']} ({bucket['opened_after']} opened file(s) after sanitize)"
        )
    _write_summary_files(run_dir, "sanitize-summary", sanitize_summary, summary_lines, submit_plan_lines)
    status_writer(run_dir, report)
    write_operator_artifacts(run_dir, report)
    if p4 is not None:
        command_log_writer(run_dir, p4)


def write_conflict_resolution_report(run_dir: Path, report: dict, *, status_writer, command_log_writer, p4=None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "status": report["status"],
        "phase": report["phase"],
        "source_stream": report.get("source_stream"),
        "target_stream": report.get("target_stream"),
        "staged_change": report.get("staged_change"),
        "bucket_summaries": report.get("bucket_summaries", []),
        "resolved_conflict_buckets": report.get("resolved_conflict_buckets", []),
        "conflict_buckets": report.get("conflict_buckets", []),
        "opened_file_count": report.get("opened_file_count", 0),
        "unresolved_file_count": report.get("unresolved_file_count", 0),
        "result": report.get("result"),
        "reason": report.get("reason"),
        "next_action": report.get("next_action"),
        "timestamp": report.get("timestamp"),
    }
    summary_lines = [
        f"status: {report['status']}",
        f"source: {report['source_stream']}",
        f"target: {report['target_stream']}",
        f"staged pending CL: {report.get('staged_change')}",
        f"opened file count: {report.get('opened_file_count', 0)}",
        f"unresolved file count: {report.get('unresolved_file_count', 0)}",
        "",
        "bucket summaries:",
    ]
    for bucket in report.get("bucket_summaries", []):
        summary_lines.append(
            "  "
            + f"{bucket['bucket']}: change {bucket['change']} "
            + f"opened_before={bucket.get('opened_before', 0)} "
            + f"opened_after={bucket.get('opened_after', 0)} "
            + f"unresolved_after={bucket.get('unresolved_after', 0)} "
            + f"action={bucket.get('action', 'preserved')} "
            + f"passes={bucket.get('pass_count', '?')} "
            + f"cmd_limit={bucket.get('command_chunk_limit', '?')}"
        )
    if "error" in report:
        summary_lines.extend(["", f"error: {report['error']}"])
    submit_plan_lines = ["Conflict resolution review order:"]
    for bucket in report.get("bucket_summaries", []):
        submit_plan_lines.append(
            f"- {bucket['bucket']}: change {bucket['change']} "
            f"(opened_after={bucket.get('opened_after', 0)}, unresolved_after={bucket.get('unresolved_after', 0)})"
        )
    _write_summary_files(run_dir, "conflict-resolution-summary", summary, summary_lines, submit_plan_lines)
    status_writer(run_dir, report)
    write_operator_artifacts(run_dir, report)
    if p4 is not None:
        command_log_writer(run_dir, p4)


def write_report(run_dir: Path, report: dict, *, split_merge_path, status_writer, command_log_writer, p4=None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "merge-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary_lines = [
        f"status: {report['status']}",
        f"source: {report['source_stream']}",
        f"target: {report['target_stream']}",
    ]
    if "selected_cl" in report:
        summary_lines.append(f"selected Level1 CL: {report['selected_cl']}")
    if "staged_change" in report:
        summary_lines.append(f"staged pending CL: {report['staged_change']}")
    if "batch_changes" in report:
        summary_lines.append("batch changelists:")
        for batch_change in report["batch_changes"]:
            summary_lines.append(
                f"  {batch_change['batch']}: change {batch_change.get('change')} "
                f"(file_count={batch_change.get('file_count', 0)}, status={batch_change.get('status', 'unknown')})"
            )
    if "merge_command" in report:
        label = "merge command:"
        if report["status"] == "DRY_RUN_READY":
            label = "merge command not executed:"
        summary_lines.append(label)
        summary_lines.append("  " + " ".join(report["merge_command"]))
    if "merge_commands" in report:
        summary_lines.append("merge commands:")
        for command in report["merge_commands"]:
            summary_lines.append("  " + " ".join(command))
    if "merge_paths" in report:
        summary_lines.append("merge paths:")
        for merge_path in report["merge_paths"]:
            _, _, path_label = split_merge_path(merge_path)
            summary_lines.append("  " + path_label)
    if "total_preview_file_count" in report:
        summary_lines.append(f"preview file count: {report['total_preview_file_count']}")
    if "unresolved_output" in report:
        summary_lines.extend(["", "files still needing manual resolution:"])
        unresolved = report["unresolved_output"].strip()
        summary_lines.append(unresolved if unresolved else "  none")
    if "opened_output" in report:
        summary_lines.append("")
        if "staged_change" in report:
            summary_lines.append(f"opened files in pending changelist {report['staged_change']}:")
        else:
            summary_lines.append("opened files in default changelist:")
        opened = report["opened_output"].strip()
        summary_lines.append(opened if opened else "  none")
    if "error" in report:
        summary_lines.append(f"error: {report['error']}")
    (run_dir / "merge-report.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    if "unresolved_output" in report:
        (run_dir / "unresolved.txt").write_text(report["unresolved_output"].strip() + "\n", encoding="utf-8")
    if "opened_output" in report:
        (run_dir / "opened.txt").write_text(report["opened_output"].strip() + "\n", encoding="utf-8")
    if "path_results" in report:
        preview_lines = []
        for path_result in report["path_results"]:
            preview_lines.append(f"### {path_result['relative_path']}")
            preview_lines.append(f"preview_file_count: {path_result['preview_file_count']}")
            preview = path_result.get("preview_output", "").strip()
            preview_lines.append(preview if preview else "no files")
            preview_lines.append("")
        (run_dir / "preview.txt").write_text("\n".join(preview_lines), encoding="utf-8")
    if "phase" in report and "result" in report:
        status_writer(run_dir, report)
        write_operator_artifacts(run_dir, report)
    if p4 is not None:
        command_log_writer(run_dir, p4)


