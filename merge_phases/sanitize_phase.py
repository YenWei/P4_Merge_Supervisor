from __future__ import annotations

import sys


class SanitizePhase:
    def run_sanitize(self) -> int:
        import p4_weekly_merge as core

        run_dir = self._new_run_dir()
        report = self._base_report("sanitize", run_dir)
        self._dashboard_update(phase="sanitize", step="STARTING", status="running")
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
                    "next_action": "Correct --p4-cwd to a valid workspace path, then rerun sanitize.",
                    "opened_file_count": 0,
                    "unresolved_file_count": 0,
                    "bucket_summaries": [],
                    "sanitized_changes": [],
                    "failed_batches": [],
                    "successful_batches": [],
                    "conflict_buckets": [],
                }
            )
            core.write_sanitize_report(run_dir, report)
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
            _, split_summary, staged_change = core.determine_sanitize_input(self.args)
            report.update(
                {
                    "source_stream": split_summary.get("source_stream", self.args.source_stream),
                    "target_stream": split_summary.get("target_stream", self.args.target_stream),
                    "staged_change": staged_change,
                    "unresolved_file_count": split_summary.get("unresolved_file_count", 0),
                }
            )
            self._dashboard_update(step="SANITIZING", staged_cl=str(staged_change))
            bucket_summaries = []
            sanitized_changes = []
            opened_total_after = 0
            conflict_buckets = []
            total_buckets = len(split_summary.get("bucket_summaries", []))
            for index, bucket in enumerate(split_summary.get("bucket_summaries", []), start=1):
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
                opened_before = core.count_output_entries(opened_before_output, ignore_patterns=["file(s) not opened"])
                p4.run("revert", "-a", "-c", str(change_number), "//...")
                self._dashboard_command(p4)
                opened_after_output = p4.run("opened", "-c", str(change_number))
                self._dashboard_command(p4)
                self._record_progress(f"sanitize bucket {index}/{total_buckets}: {bucket_name}")
                opened_after = core.count_output_entries(opened_after_output, ignore_patterns=["file(s) not opened"])
                opened_total_after += opened_after
                sanitized_bucket = {
                    "bucket": bucket_name,
                    "change": change_number,
                    "opened_before": opened_before,
                    "opened_after": opened_after,
                }
                bucket_summaries.append(sanitized_bucket)
                sanitized_changes.append({"bucket": bucket_name, "change": change_number})
                if bucket_name.startswith("conflict-") or bucket_name.startswith("holding-"):
                    conflict_buckets.append(
                        {
                            "bucket": bucket_name,
                            "change": change_number,
                            "file_count": opened_after,
                        }
                    )

            result = "REVIEW_WITH_CONFLICT_BUCKETS" if conflict_buckets else "READY_FOR_REVIEW"
            report.update(
                {
                    "status": result,
                    "result": result,
                    "blocker_category": None,
                    "retryable": False,
                    "reason": (
                        "Sanitize completed with preserved conflict/holding buckets."
                        if conflict_buckets
                        else "Sanitize completed and review buckets are ready for validation."
                    ),
                    "next_action": (
                        "Review normal sanitized changelists and separately inspect preserved conflict/holding buckets."
                        if conflict_buckets
                        else "Review sanitized changelists and continue validation."
                    ),
                    "opened_file_count": opened_total_after,
                    "bucket_summaries": bucket_summaries,
                    "sanitized_changes": sanitized_changes,
                    "successful_batches": bucket_summaries,
                    "failed_batches": [],
                    "conflict_buckets": conflict_buckets,
                    "note": "Sanitize applied revert -a and preserved split bucket structure. No submit was attempted.",
                }
            )
            core.write_sanitize_report(run_dir, report, p4)
            self._dashboard_update(step="DONE", status=result)
            self._finish(result)
            print(f"[{result}] staged CL {staged_change}")
            for bucket in bucket_summaries:
                print(
                    f"Bucket {bucket['bucket']}: change {bucket['change']} opened_before={bucket['opened_before']} opened_after={bucket['opened_after']}"
                )
            print(f"Report: {run_dir}")
            return 10 if conflict_buckets else 0
        except core.P4Error as error:
            classified_error = core.classify_p4_error(str(error))
            sanitize_failure = core.classify_sanitize_failure(classified_error, p4.last_command())
            report.update(
                {
                    "status": "BLOCKED_REQUIRES_CODEX_OR_USER",
                    "error": classified_error,
                    **sanitize_failure,
                    "opened_file_count": report.get("opened_file_count", 0),
                    "unresolved_file_count": report.get("unresolved_file_count", 0),
                    "bucket_summaries": report.get("bucket_summaries", []),
                    "sanitized_changes": report.get("sanitized_changes", []),
                    "successful_batches": report.get("successful_batches", []),
                    "failed_batches": [],
                    "conflict_buckets": report.get("conflict_buckets", []),
                    "failing_command": p4.last_command(),
                }
            )
            core.write_sanitize_report(run_dir, report, p4)
            self._dashboard_command(p4)
            self._dashboard_update(step="BLOCKED", status="blocked")
            self._finish("blocked")
            print(f"[BLOCKED] {classified_error}", file=sys.stderr)
            print(f"Report: {run_dir}", file=sys.stderr)
            return 20
