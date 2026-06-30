from __future__ import annotations

import sys
from pathlib import Path


class SplitPhase:
    def run_split(self) -> int:
        import p4_weekly_merge as core

        run_dir = self._new_run_dir()
        report = self._base_report("split", run_dir)
        self._dashboard_update(phase="split", step="STARTING", status="running")
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
                    "next_action": "Correct --p4-cwd to a valid workspace path, then rerun split.",
                    "opened_file_count": 0,
                    "unresolved_file_count": 0,
                    "bucket_summaries": [],
                    "split_changes": [],
                    "failed_batches": [],
                    "successful_batches": [],
                    "conflict_buckets": [],
                }
            )
            core.write_split_report(run_dir, report)
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
            previous_run_dir, previous_status, staged_change = core.determine_split_input(self.args)
            report.update(
                {
                    "source_stream": previous_status.get("source_stream", self.args.source_stream),
                    "target_stream": previous_status.get("target_stream", self.args.target_stream),
                    "job_tag": previous_status.get("job_tag", self.args.job_tag),
                    "selected_cl": previous_status.get("selected_cl"),
                    "staged_change": staged_change,
                }
            )
            self._dashboard_update(step="SPLITTING", staged_cl=str(staged_change), selected_cl=str(report.get("selected_cl") or ""))
            opened_output = p4.run("opened", "-c", str(staged_change))
            self._dashboard_command(p4)
            opened_paths = core.parse_depot_paths_from_output(opened_output)
            unresolved_paths = set()
            unresolved_file = Path(self.args.unresolved_file) if getattr(self.args, "unresolved_file", None) else (previous_run_dir / "unresolved.txt")
            if unresolved_file.exists():
                unresolved_paths = {
                    core.depot_to_relative_stream_path(
                        core.depot_to_relative_stream_path(path, report["source_stream"]),
                        report["target_stream"],
                    )
                    for path in core.parse_depot_paths_from_output(unresolved_file.read_text(encoding="utf-8"))
                }
            if not opened_paths:
                raise core.P4Error(f"Pending changelist {staged_change} has no opened files to split.")

            buckets = core.build_split_buckets(opened_paths, report["target_stream"], unresolved_paths)
            unresolved_bucket_names = [
                bucket_name
                for bucket_name in buckets
                if bucket_name.startswith("conflict-") and bucket_name.endswith("-unresolved")
            ]
            if unresolved_paths and not unresolved_bucket_names:
                raise core.P4Error(
                    "Split detected unresolved files from the prior run, but none were classified into "
                    "a conflict unresolved bucket. Inspect unresolved path normalization before continuing."
                )
            bucket_summaries = []
            split_changes = []
            total_buckets = len(buckets)
            for index, (bucket_name, depot_paths) in enumerate(buckets.items(), start=1):
                self._dashboard_update(
                    batch=bucket_name,
                    item="preparing child changelist",
                    target=f"{len(depot_paths)} file(s)",
                    progress=f"bucket {index}/{total_buckets}",
                )
                child_change = core.create_split_bucket_changelist(p4, staged_change, bucket_name, staged_change)
                self._dashboard_command(p4)
                self._dashboard_update(
                    batch=bucket_name,
                    item=f"child CL {child_change}",
                    staged_cl=str(staged_change),
                    target=f"files 0/{len(depot_paths)}",
                    progress=f"bucket {index}/{total_buckets} | chunk 0/?",
                )
                move_summary = core.move_files_to_changelist(
                    p4,
                    child_change,
                    depot_paths,
                    progress_callback=lambda info, bucket_name=bucket_name, index=index, total_buckets=total_buckets: self._dashboard_update(
                        batch=bucket_name,
                        item=(
                            f"child CL {info['change']} | "
                            f"chunk {info['chunk_index']}/{info['total_chunks']}"
                        ),
                        target=f"files {info['moved_files']}/{info['total_files']}",
                        progress=f"bucket {index}/{total_buckets} | chunk {info['chunk_index']}/{info['total_chunks']}",
                    ),
                )
                self._dashboard_command(p4)
                self._record_progress(f"bucket {index}/{total_buckets} ready: {bucket_name} -> {child_change}")
                bucket_summary = {
                    "bucket": bucket_name,
                    "change": child_change,
                    "file_count": len(depot_paths),
                    "chunk_count": move_summary["total_chunks"],
                }
                bucket_summaries.append(bucket_summary)
                split_changes.append({"bucket": bucket_name, "change": child_change})

            conflict_buckets = [bucket for bucket in bucket_summaries if bucket["bucket"].startswith("conflict-") or bucket["bucket"].startswith("holding-")]
            result = "REVIEW_WITH_CONFLICT_BUCKETS" if conflict_buckets else "READY_FOR_REVIEW"
            report.update(
                {
                    "status": result,
                    "result": result,
                    "blocker_category": None,
                    "retryable": False,
                    "reason": (
                        "Split completed successfully with isolated conflict/holding buckets."
                        if conflict_buckets
                        else "Split completed successfully and all files are in reviewable pending changelists."
                    ),
                    "next_action": (
                        "Review the normal split changelists and separately inspect the conflict/holding buckets."
                        if conflict_buckets
                        else "Review the split changelists and continue validation."
                    ),
                    "opened_file_count": len(opened_paths),
                    "unresolved_file_count": len(unresolved_paths),
                    "bucket_summaries": bucket_summaries,
                    "split_changes": split_changes,
                    "successful_batches": bucket_summaries,
                    "failed_batches": [],
                    "conflict_buckets": conflict_buckets,
                    "opened_output": opened_output,
                    "note": "Split reorganized the staged pending changelist into review and special-case buckets. No submit was attempted.",
                }
            )
            core.write_split_report(run_dir, report, p4)
            self._dashboard_update(step="DONE", status=result)
            self._finish(result)
            print(f"[{result}] staged CL {staged_change}")
            for bucket in bucket_summaries:
                print(f"Bucket {bucket['bucket']}: change {bucket['change']} ({bucket['file_count']} file(s))")
            print(f"Report: {run_dir}")
            return 10 if conflict_buckets else 0
        except core.P4Error as error:
            classified_error = core.classify_p4_error(str(error))
            split_failure = core.classify_split_failure(classified_error, p4.last_command())
            report.update(
                {
                    "status": "BLOCKED_REQUIRES_CODEX_OR_USER",
                    "error": classified_error,
                    **split_failure,
                    "opened_file_count": report.get("opened_file_count", 0),
                    "unresolved_file_count": report.get("unresolved_file_count", 0),
                    "bucket_summaries": report.get("bucket_summaries", []),
                    "split_changes": report.get("split_changes", []),
                    "successful_batches": report.get("successful_batches", []),
                    "failed_batches": [],
                    "conflict_buckets": report.get("conflict_buckets", []),
                    "failing_command": p4.last_command(),
                }
            )
            core.write_split_report(run_dir, report, p4)
            self._dashboard_command(p4)
            self._dashboard_update(step="BLOCKED", status="blocked")
            self._finish("blocked")
            print(f"[BLOCKED] {classified_error}", file=sys.stderr)
            print(f"Report: {run_dir}", file=sys.stderr)
            return 20
