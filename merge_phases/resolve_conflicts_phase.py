from __future__ import annotations

import sys


class ResolveConflictsPhase:
    def run_resolve_conflicts(self) -> int:
        import p4_weekly_merge as core

        run_dir = self._new_run_dir()
        report = self._base_report("resolve-conflicts", run_dir)
        self._dashboard_update(phase="resolve-conflicts", step="STARTING", status="running")
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
                    "next_action": "Correct --p4-cwd to a valid workspace path, then rerun resolve-conflicts.",
                    "opened_file_count": 0,
                    "unresolved_file_count": 0,
                    "bucket_summaries": [],
                    "resolved_conflict_buckets": [],
                    "conflict_buckets": [],
                    "failed_batches": [],
                    "successful_batches": [],
                }
            )
            core.write_conflict_resolution_report(run_dir, report)
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
            _, sanitize_summary, staged_change = core.determine_conflict_resolution_input(self.args)
            report.update(
                {
                    "source_stream": sanitize_summary.get("source_stream", self.args.source_stream),
                    "target_stream": sanitize_summary.get("target_stream", self.args.target_stream),
                    "staged_change": staged_change,
                    "unresolved_file_count": sanitize_summary.get("unresolved_file_count", 0),
                }
            )
            self._dashboard_update(step="RESOLVING", staged_cl=str(staged_change))
            bucket_summaries = []
            resolved_conflict_buckets = []
            remaining_conflict_buckets = []
            opened_total_after = 0
            unresolved_total_after = 0
            input_buckets = sanitize_summary.get("bucket_summaries", [])
            total_buckets = len(input_buckets)
            for index, bucket in enumerate(input_buckets, start=1):
                change_number = int(bucket["change"])
                bucket_name = bucket["bucket"]
                self._dashboard_update(
                    batch=bucket_name,
                    item=f"change {change_number}",
                    target=f"change {change_number}",
                    progress=f"bucket {index}/{total_buckets}",
                )
                opened_before_output = p4.run("opened", "-c", str(change_number))
                self._dashboard_command(p4)
                opened_paths = core.parse_depot_paths_from_output(opened_before_output)
                opened_before = core.count_output_entries(opened_before_output, ignore_patterns=["file(s) not opened"])
                action = "preserved"
                if core.should_auto_accept_conflict_bucket(bucket_name) and opened_paths:
                    core.run_p4_scoped_to_filespecs(p4, ["resolve", "-at"], opened_paths)
                    self._dashboard_command(p4)
                    action = "accept_source"
                unresolved_after_output = core.run_p4_scoped_to_filespecs(p4, ["resolve", "-n"], opened_paths)
                opened_after_output = p4.run("opened", "-c", str(change_number))
                self._dashboard_command(p4)
                self._record_progress(f"resolve-conflicts bucket {index}/{total_buckets}: {bucket_name}")
                unresolved_after = core.count_output_entries(
                    unresolved_after_output,
                    ignore_patterns=["no file(s) to resolve", "file(s) not opened"],
                )
                opened_after = core.count_output_entries(opened_after_output, ignore_patterns=["file(s) not opened"])
                opened_total_after += opened_after
                unresolved_total_after += unresolved_after
                bucket_summary = {
                    "bucket": bucket_name,
                    "change": change_number,
                    "opened_before": opened_before,
                    "opened_after": opened_after,
                    "unresolved_after": unresolved_after,
                    "action": action,
                }
                bucket_summaries.append(bucket_summary)

                is_conflict_bucket = bucket_name.startswith("conflict-") or bucket_name.startswith("holding-")
                if core.should_auto_accept_conflict_bucket(bucket_name) and unresolved_after == 0:
                    resolved_conflict_buckets.append(
                        {
                            "bucket": bucket_name,
                            "change": change_number,
                            "file_count": opened_after,
                            "action": action,
                        }
                    )
                elif is_conflict_bucket:
                    remaining_conflict_buckets.append(
                        {
                            "bucket": bucket_name,
                            "change": change_number,
                            "file_count": opened_after,
                            "unresolved_after": unresolved_after,
                            "action": action,
                        }
                    )

            result = "REVIEW_WITH_CONFLICT_BUCKETS" if remaining_conflict_buckets else "READY_FOR_REVIEW"
            report.update(
                {
                    "status": result,
                    "result": result,
                    "blocker_category": None,
                    "retryable": False,
                    "reason": (
                        "Conflict resolution completed, but some conflict/holding buckets still need human review."
                        if remaining_conflict_buckets
                        else "Conflict resolution completed and all remaining buckets are ready for review."
                    ),
                    "next_action": (
                        "Review the normal changelists and manually inspect the remaining conflict/holding buckets."
                        if remaining_conflict_buckets
                        else "Review the resolved changelists and continue validation."
                    ),
                    "opened_file_count": opened_total_after,
                    "unresolved_file_count": unresolved_total_after,
                    "bucket_summaries": bucket_summaries,
                    "resolved_conflict_buckets": resolved_conflict_buckets,
                    "successful_batches": bucket_summaries,
                    "failed_batches": [],
                    "conflict_buckets": remaining_conflict_buckets,
                    "note": "resolve-conflicts applied accept-source only to whitelisted conflict buckets and preserved everything else.",
                }
            )
            core.write_conflict_resolution_report(run_dir, report, p4)
            self._dashboard_update(step="DONE", status=result)
            self._finish(result)
            print(f"[{result}] staged CL {staged_change}")
            for bucket in bucket_summaries:
                print(
                    f"Bucket {bucket['bucket']}: change {bucket['change']} "
                    f"opened_before={bucket['opened_before']} opened_after={bucket['opened_after']} "
                    f"unresolved_after={bucket['unresolved_after']} action={bucket['action']}"
                )
            print(f"Report: {run_dir}")
            return 10 if remaining_conflict_buckets else 0
        except core.P4Error as error:
            classified_error = core.classify_p4_error(str(error))
            conflict_failure = core.classify_conflict_resolution_failure(classified_error, p4.last_command())
            report.update(
                {
                    "status": "BLOCKED_REQUIRES_CODEX_OR_USER",
                    "error": classified_error,
                    **conflict_failure,
                    "opened_file_count": report.get("opened_file_count", 0),
                    "unresolved_file_count": report.get("unresolved_file_count", 0),
                    "bucket_summaries": report.get("bucket_summaries", []),
                    "resolved_conflict_buckets": report.get("resolved_conflict_buckets", []),
                    "successful_batches": report.get("successful_batches", []),
                    "failed_batches": [],
                    "conflict_buckets": report.get("conflict_buckets", []),
                    "failing_command": p4.last_command(),
                }
            )
            core.write_conflict_resolution_report(run_dir, report, p4)
            self._dashboard_command(p4)
            self._dashboard_update(step="BLOCKED", status="blocked")
            self._finish("blocked")
            print(f"[BLOCKED] {classified_error}", file=sys.stderr)
            print(f"Report: {run_dir}", file=sys.stderr)
            return 20
