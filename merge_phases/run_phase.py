from __future__ import annotations

import sys


class RunPhase:
    def run_merge(self) -> int:
        import p4_weekly_merge as core

        run_dir = self._new_run_dir()
        report = self._base_report("run", run_dir)
        self._dashboard_update(phase="run", step="STARTING", status="running")
        try:
            p4_cwd = core.validate_p4_cwd(self.args.p4_cwd)
        except core.P4Error as error:
            classified_error = core.classify_p4_error(str(error))
            report.update(
                {
                    "error": classified_error,
                    **core.classify_run_failure(classified_error),
                    "successful_batches": [],
                    "failed_batches": [],
                    "conflict_buckets": [],
                    "failing_command": None,
                    "opened_file_count": 0,
                    "unresolved_file_count": 0,
                }
            )
            core.write_report(run_dir, report)
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
        preflight_result = {
            "selected_cl": None,
            "batches": self.args.batches,
            "batch_merge_paths": {},
            "merge_paths": [],
            "merge_commands": [],
            "path_results": [],
            "total_preview_file_count": 0,
            "successful_batches": [],
            "failed_batches": [],
            "failing_command": None,
        }
        batch_results = []
        completed_batches = []
        path_results = []
        failed_batch = None
        total_preview_file_count = 0
        total_opened_file_count = 0
        batch_changes = []
        current_batch_paths = []

        try:
            self._dashboard_update(step="PREFLIGHT", target=self.args.target_stream, batch=", ".join(self.args.batches))
            preflight_result = core.execute_preflight(p4, self.args, include_preview=False)
            self._dashboard_command(p4)
            batch_count = len(self.args.batches)
            self._dashboard_update(
                step="MERGING",
                selected_cl=str(preflight_result["selected_cl"]),
                progress=f"batch 0/{batch_count} | path 0/0",
            )
            for batch_index, batch_name in enumerate(self.args.batches, start=1):
                current_batch_paths = preflight_result["batch_merge_paths"][batch_name]
                self._dashboard_update(
                    batch=batch_name,
                    item="-",
                    progress=f"batch {batch_index}/{batch_count} | path 0/{len(current_batch_paths)}",
                )
                try:
                    batch_result = core.perform_single_batch_stage_cycle(
                        p4,
                        self.args.source_stream,
                        self.args.target_stream,
                        preflight_result["selected_cl"],
                        batch_name,
                        merge_paths=current_batch_paths,
                        max_merge_files=self.args.max_merge_files,
                        progress_callback=lambda index, total, path_label, batch_index=batch_index, batch_count=batch_count: self._dashboard_update(
                            item=path_label,
                            progress=f"batch {batch_index}/{batch_count} | path {index}/{total}",
                        ),
                    )
                except core.P4Error:
                    failed_batch = {
                        "batch": batch_name,
                        "batch_index": batch_index,
                        "batch_count": batch_count,
                        "merge_paths": current_batch_paths,
                    }
                    raise
                total_preview_file_count += batch_result["preview_file_count"]
                path_results.extend(batch_result["path_results"])
                opened_paths = core.parse_depot_paths_from_output(batch_result["opened_output"])
                if opened_paths:
                    batch_change = core.create_numbered_pending_changelist(
                        p4,
                        core.build_batch_run_changelist_description(
                            self.args,
                            preflight_result["selected_cl"],
                            batch_name,
                        ),
                    )
                    self._dashboard_command(p4)
                    self._dashboard_update(
                        batch=batch_name,
                        step="STAGING_PENDING_CL",
                        item=f"child CL {batch_change}",
                        staged_cl=str(batch_change),
                        progress=f"batch {batch_index}/{batch_count} | staging",
                    )
                    core.move_files_to_changelist(
                        p4,
                        batch_change,
                        opened_paths,
                        progress_callback=lambda info, batch_name=batch_name, batch_index=batch_index, batch_count=batch_count: self._dashboard_update(
                            batch=batch_name,
                            item=f"child CL {info['change']} | chunk {info['chunk_index']}/{info['total_chunks']}",
                            staged_cl=str(info["change"]),
                            target=f"files {info['moved_files']}/{info['total_files']}",
                            progress=f"batch {batch_index}/{batch_count} | chunk {info['chunk_index']}/{info['total_chunks']}",
                        ),
                    )
                    self._dashboard_command(p4)
                    opened_after_output = p4.run("opened", "-c", str(batch_change))
                    self._dashboard_command(p4)
                    opened_after_count = core.count_output_entries(
                        opened_after_output,
                        ignore_patterns=["file(s) not opened"],
                    )
                    batch_stage = {
                        "batch": batch_name,
                        "change": batch_change,
                        "file_count": opened_after_count,
                        "preview_file_count": batch_result["preview_file_count"],
                        "status": "STAGED_FOR_RESOLVE",
                    }
                    batch_changes.append(batch_stage)
                    batch_results.append(batch_stage)
                    total_opened_file_count += opened_after_count
                else:
                    batch_stage = {
                        "batch": batch_name,
                        "change": None,
                        "file_count": 0,
                        "preview_file_count": batch_result["preview_file_count"],
                        "status": "NO_FILES",
                    }
                    batch_results.append(batch_stage)
                completed_batches.append(batch_name)
                self._record_progress(f"completed batch {batch_index}/{batch_count}: {batch_name}")
            self._dashboard_command(p4)
            self._record_progress("all requested batches applied")
            if total_opened_file_count == 0:
                result = "READY_NO_CHANGES"
                next_action = "No matching files were opened for the requested batches. Pick another source changelist or batch set."
                reason = "Run completed cleanly, but the selected source changelist did not produce any opened files for the requested batches."
            else:
                result = "READY_TO_RESOLVE"
                next_action = "Run resolve to process each staged batch changelist independently."
                reason = "Run completed and staged each batch into its own pending changelist."
            report.update(
                {
                    "status": result,
                    "result": result,
                    "blocker_category": None,
                    "retryable": False,
                    "reason": reason,
                    "next_action": next_action,
                    **preflight_result,
                    "path_results": path_results,
                    "total_preview_file_count": total_preview_file_count,
                    "successful_batches": batch_results,
                    "batch_changes": batch_changes,
                    "completed_batches": completed_batches,
                    "failed_batch": None,
                    "failed_batches": [],
                    "opened_file_count": total_opened_file_count,
                    "unresolved_file_count": 0,
                    "conflict_buckets": [],
                    "note": (
                        "Merge was staged into per-batch pending changelists. Resolve has not been run yet."
                        if total_opened_file_count > 0
                        else "Requested batches completed with no opened files to stage."
                    ),
                }
            )
            core.write_report(run_dir, report, p4)
            self._dashboard_update(step="DONE", status=result)
            self._finish(result)
            print(f"[{result}] selected {self.args.job_tag} CL: {preflight_result['selected_cl']}")
            print("Batches: " + ", ".join(self.args.batches))
            for batch_stage in batch_changes:
                print(f"Batch {batch_stage['batch']}: change {batch_stage['change']} ({batch_stage['file_count']} file(s))")
            print(f"Report: {run_dir}")
            return 0
        except core.P4Error as error:
            failing_command = p4.last_command()
            classified_error = core.classify_p4_error(str(error))
            merge_started = any(
                len(result.args) >= 2 and result.args[1] == "merge" and "-n" not in result.args
                for result in p4.results
            )
            failure_opened_output = ""
            failure_opened_file_count = total_opened_file_count

            if failed_batch is not None:
                snapshot_errors = []
                current_batch_filespecs = core.build_target_filespecs(self.args.target_stream, current_batch_paths)
                try:
                    failure_opened_output = core.run_p4_scoped_to_filespecs(
                        p4,
                        ["opened", "-c", "default"],
                        current_batch_filespecs,
                    )
                    failure_opened_file_count = core.count_output_entries(
                        failure_opened_output,
                        ignore_patterns=["file(s) not opened"],
                    )
                except core.P4Error as snapshot_error:
                    snapshot_errors.append(f"opened snapshot failed: {core.classify_p4_error(str(snapshot_error))}")
                failed_batch = {
                    **failed_batch,
                    "failing_command": failing_command,
                    "opened_output": failure_opened_output,
                    "opened_file_count": failure_opened_file_count,
                    "opened_file_count_delta": failure_opened_file_count,
                }
                if snapshot_errors:
                    failed_batch["snapshot_errors"] = snapshot_errors
                total_opened_file_count += failure_opened_file_count

            report.update(
                {
                    "status": "BLOCKED_REQUIRES_CODEX_OR_USER",
                    "error": classified_error,
                    **core.classify_run_failure(classified_error, failing_command, merge_started=merge_started),
                    **preflight_result,
                    "path_results": path_results,
                    "total_preview_file_count": total_preview_file_count,
                    "staged_change": report.get("staged_change"),
                    "successful_batches": batch_results,
                    "batch_changes": batch_changes,
                    "completed_batches": completed_batches,
                    "failed_batch": failed_batch,
                    "failed_batches": [failed_batch] if failed_batch is not None else [],
                    "opened_output": failure_opened_output,
                    "conflict_buckets": [],
                    "failing_command": failing_command,
                    "opened_file_count": total_opened_file_count,
                    "unresolved_file_count": 0,
                }
            )
            core.write_report(run_dir, report, p4)
            self._dashboard_command(p4)
            self._dashboard_update(step="BLOCKED", status="blocked")
            self._finish("blocked")
            print(f"[BLOCKED] {classified_error}", file=sys.stderr)
            print(f"Report: {run_dir}", file=sys.stderr)
            return 20
