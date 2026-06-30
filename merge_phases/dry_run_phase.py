from __future__ import annotations

import sys


class DryRunPhase:
    def run_dry_run(self) -> int:
        import p4_weekly_merge as core

        run_dir = self._new_run_dir()
        report = self._base_report("dry-run", run_dir)
        self._dashboard_update(phase="dry-run", step="STARTING", status="running")
        try:
            p4_cwd = core.validate_p4_cwd(self.args.p4_cwd)
        except core.P4Error as error:
            classified_error = core.classify_p4_error(str(error))
            report.update(
                {
                    "error": classified_error,
                    **core.classify_dry_run_failure(classified_error),
                    "successful_batches": [],
                    "failed_batches": [],
                    "failing_command": None,
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

        try:
            self._dashboard_update(step="PREFLIGHT", target=self.args.target_stream, batch=", ".join(self.args.batches))
            preflight_result = core.execute_preflight(p4, self.args, include_preview=True)
            self._dashboard_command(p4)
            self._dashboard_update(
                step="PREVIEWING",
                selected_cl=str(preflight_result["selected_cl"]),
                item="preflight checks complete",
                progress=f"path 0/{len(preflight_result['merge_paths'])}",
                status="ready",
            )
            self._record_progress("dry-run preflight completed")
            report.update(
                {
                    "status": "DRY_RUN_READY",
                    "result": "READY",
                    "blocker_category": None,
                    "retryable": False,
                    "reason": "Dry-run completed successfully.",
                    "next_action": "Review preview output, then run the staged merge command when ready.",
                    **preflight_result,
                    "note": "Dry run stopped before executing p4 merge.",
                }
            )
            core.write_report(run_dir, report, p4)
            self._finish("done")
            print(f"[DRY RUN READY] selected {self.args.job_tag} CL: {preflight_result['selected_cl']}")
            print("Batches: " + ", ".join(self.args.batches))
            print(f"Preview files: {preflight_result['total_preview_file_count']}")
            for merge_command in preflight_result["merge_commands"]:
                print("Would run: " + " ".join(merge_command))
            print(f"Report: {run_dir}")
            return 0
        except core.P4Error as error:
            classified_error = core.classify_p4_error(str(error))
            dry_run_failure = core.classify_dry_run_failure(classified_error, p4.last_command())
            report.update(
                {
                    "status": "BLOCKED_REQUIRES_CODEX_OR_USER",
                    "error": classified_error,
                    **dry_run_failure,
                    "selected_cl": report.get("selected_cl"),
                    "successful_batches": [],
                    "failed_batches": [],
                    "failing_command": p4.last_command(),
                }
            )
            core.write_report(run_dir, report, p4)
            self._dashboard_command(p4)
            self._dashboard_update(step="BLOCKED", status="blocked")
            self._finish("blocked")
            print(f"[BLOCKED] {classified_error}", file=sys.stderr)
            print(f"Report: {run_dir}", file=sys.stderr)
            return 20
