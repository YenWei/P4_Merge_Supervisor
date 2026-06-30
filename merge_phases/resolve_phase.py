from __future__ import annotations

import re
import sys

from merge_supervisor.policy_ladder import PolicyLadder


class ResolvePhase:
    _TAMPERED_FILE_RE = re.compile(r"^(?P<path>[A-Za-z]:\\.+?) tampered with before resolve - edit or revert\.$")
    _TOLERABLE_RESOLVE_ERROR_SNIPPETS = (
        "tampered with before resolve - edit or revert.",
        "no file(s) to resolve.",
    )

    @classmethod
    def _parse_tampered_local_paths(cls, text: str) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for raw_line in text.splitlines():
            match = cls._TAMPERED_FILE_RE.search(raw_line.strip())
            if not match:
                continue
            candidate = match.group("path").strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            ordered.append(candidate)
        return ordered

    @staticmethod
    def _policy_path_family(path: str) -> str | None:
        normalized = path.replace("\\", "/")
        parts = [part for part in normalized.split("/") if part and part != "."]
        if "Project" in parts:
            parts = parts[parts.index("Project"): ]
        if len(parts) >= 4 and parts[0] == "Project" and parts[1] == "Plugins":
            return "/".join(parts[0:4]) + "/..."
        return None

    @staticmethod
    def _policy_filetype(path: str) -> str:
        normalized = path.replace("\\", "/").lower()
        if "/binaries/" in normalized or normalized.endswith((".dll", ".exe", ".lib", ".pdb", ".modules")):
            return "binary"
        return "text"

    @classmethod
    def _build_policy_observation(
        cls,
        *,
        batch_name: str,
        blocker_type: str,
        unresolved_targets: list[str],
    ) -> dict | None:
        if batch_name != "plugins":
            return None
        for target in unresolved_targets:
            path_family = cls._policy_path_family(target)
            if path_family is None:
                continue
            pattern = {
                "phase": "resolve",
                "batch": batch_name,
                "path_family": path_family,
                "filetype": cls._policy_filetype(target),
                "blocker_type": blocker_type,
                "suggested_action": "accept_source",
            }
            ladder = PolicyLadder()
            level = ladder.classify_pattern(**pattern)
            return ladder.build_observation(pattern, policy_level=level, source="resolve")
        return None

    @classmethod
    def _is_tolerable_resolve_error(cls, text: str) -> bool:
        meaningful_lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not meaningful_lines:
            return True
        for line in meaningful_lines:
            lower_line = line.lower()
            if any(snippet in lower_line for snippet in cls._TOLERABLE_RESOLVE_ERROR_SNIPPETS):
                continue
            return False
        return True

    @staticmethod
    def _dedupe_paths(paths: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for path in paths:
            if not path or path in seen:
                continue
            seen.add(path)
            ordered.append(path)
        return ordered

    @staticmethod
    def _build_passes(core, filespecs: list[str], pass_file_limit: int) -> list[list[str]]:
        if not filespecs:
            return []
        if pass_file_limit <= 0 or len(filespecs) <= pass_file_limit:
            return [list(filespecs)]
        return [list(chunk) for chunk in core.chunked(filespecs, pass_file_limit)]

    def _resolve_pass_file_limit(self) -> int:
        return max(1, int(getattr(self.args, "resolve_pass_file_limit", 500) or 500))

    def _resolve_command_file_limit(self) -> int:
        return max(1, int(getattr(self.args, "resolve_command_file_limit", 20) or 20))

    def _run_scoped_capture(
        self,
        core,
        p4,
        command_args: list[str],
        filespecs: list[str],
        *,
        batch_name: str,
        change_number: int,
        batch_index: int,
        total_batches: int,
        pass_index: int,
        pass_count: int,
        step_label: str,
        state: dict,
    ) -> list:
        if not filespecs:
            return []
        safe_chunks = core.chunked_by_command_length(
            filespecs,
            fixed_args=[p4.executable, *command_args],
            max_chars=7000,
            max_items=self._resolve_command_file_limit(),
        )
        results = []
        total_command_chunks = len(safe_chunks)
        for command_chunk_index, chunk in enumerate(safe_chunks, start=1):
            progress_label = (
                f"batch {batch_index}/{total_batches} | pass {pass_index}/{pass_count} | "
                f"cmd {command_chunk_index}/{total_command_chunks}"
            )
            item_label = (
                f"{step_label} pass {pass_index}/{pass_count} | "
                f"cmd {command_chunk_index}/{total_command_chunks}"
            )
            target_label = f"{len(chunk)} file(s) in current command chunk"
            state.update(
                {
                    "current_batch": batch_name,
                    "current_change": change_number,
                    "current_pass_index": pass_index,
                    "current_pass_count": pass_count,
                    "current_step_label": step_label,
                    "current_command_chunk_index": command_chunk_index,
                    "current_command_chunk_count": total_command_chunks,
                    "failed_step": item_label,
                }
            )
            self._dashboard_update(
                batch=batch_name,
                item=item_label,
                target=target_label,
                progress=progress_label,
                staged_cl=str(change_number),
            )
            result = p4.run_result(*command_args, *chunk)
            self._dashboard_command(p4)
            results.append(result)
            state["last_successful_step"] = item_label
            self._record_progress(
                f"resolved {batch_name} pass {pass_index}/{pass_count} {step_label} chunk {command_chunk_index}/{total_command_chunks}"
            )
        return results

    def run_resolve(self) -> int:
        import p4_weekly_merge as core

        run_dir = self._new_run_dir()
        report = self._base_report("resolve", run_dir)
        self._dashboard_update(phase="resolve", step="STARTING", status="running")
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
                    "next_action": "Correct --p4-cwd to a valid workspace path, then rerun resolve.",
                    "opened_file_count": 0,
                    "unresolved_file_count": 0,
                    "bucket_summaries": [],
                    "resolved_batches": [],
                    "failed_batches": [],
                    "conflict_buckets": [],
                }
            )
            core.write_resolve_report(run_dir, report)
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
            _, run_summary, batch_changes = core.determine_resolve_input(self.args)
            report.update(
                {
                    "source_stream": run_summary.get("source_stream", self.args.source_stream),
                    "target_stream": run_summary.get("target_stream", self.args.target_stream),
                    "selected_cl": run_summary.get("selected_cl"),
                    "batch_changes": batch_changes,
                    "resolve_pass_file_limit": self._resolve_pass_file_limit(),
                    "resolve_command_file_limit": self._resolve_command_file_limit(),
                }
            )
            self._dashboard_update(step="RESOLVING", selected_cl=str(report.get("selected_cl") or ""))
            bucket_summaries = []
            resolved_batches = []
            conflict_buckets = []
            policy_observations = []
            policy_promotion_candidates = []
            opened_total_after = 0
            unresolved_total_after = 0
            total_batches = len(batch_changes)
            current_batch_info = None
            progress_state = {
                "current_batch": None,
                "current_change": None,
                "current_pass_index": None,
                "current_pass_count": None,
                "current_step_label": None,
                "current_command_chunk_index": None,
                "current_command_chunk_count": None,
                "last_successful_step": "input resolution completed",
                "failed_step": None,
            }
            for index, batch_info in enumerate(batch_changes, start=1):
                current_batch_info = batch_info
                batch_name = batch_info["batch"]
                change_number = int(batch_info["change"])
                self._dashboard_update(
                    batch=batch_name,
                    item=f"change {change_number}",
                    target=f"change {change_number}",
                    progress=f"batch {index}/{total_batches}",
                    staged_cl=str(change_number),
                )
                opened_before_output = p4.run("opened", "-c", str(change_number))
                self._dashboard_command(p4)
                opened_paths = core.parse_depot_paths_from_output(opened_before_output)
                opened_before = core.count_output_entries(opened_before_output, ignore_patterns=["file(s) not opened"])
                if not opened_paths:
                    self._record_progress(f"resolve batch {index}/{total_batches}: {batch_name} already empty")
                    continue

                pass_file_limit = self._resolve_pass_file_limit()
                resolve_strategy = "auto-merge"
                resolve_results = []
                total_passes = 0
                completed_passes = 0
                if batch_name == "plugins":
                    source_accept_paths, manual_review_paths = core.split_plugin_resolve_paths(opened_paths)
                    source_accept_passes = self._build_passes(core, source_accept_paths, pass_file_limit)
                    manual_review_passes = self._build_passes(core, manual_review_paths, pass_file_limit)
                    total_passes = len(source_accept_passes) + len(manual_review_passes)
                    for pass_paths in source_accept_passes:
                        completed_passes += 1
                        resolve_results.extend(
                            self._run_scoped_capture(
                                core,
                                p4,
                                ["resolve", "-at"],
                                pass_paths,
                                batch_name=batch_name,
                                change_number=change_number,
                                batch_index=index,
                                total_batches=total_batches,
                                pass_index=completed_passes,
                                pass_count=total_passes,
                                step_label="resolve -at",
                                state=progress_state,
                            )
                        )
                    for pass_paths in manual_review_passes:
                        completed_passes += 1
                        resolve_results.extend(
                            self._run_scoped_capture(
                                core,
                                p4,
                                ["resolve", "-am"],
                                pass_paths,
                                batch_name=batch_name,
                                change_number=change_number,
                                batch_index=index,
                                total_batches=total_batches,
                                pass_index=completed_passes,
                                pass_count=total_passes,
                                step_label="resolve -am",
                                state=progress_state,
                            )
                        )
                    resolve_strategy = "plugins:mixed(-at non-review, -am review-allowlist)"
                else:
                    opened_passes = self._build_passes(core, opened_paths, pass_file_limit)
                    total_passes = len(opened_passes)
                    resolve_command = ["resolve", "-at"] if batch_name in core.SOURCE_ACCEPT_BATCHES else ["resolve", "-am"]
                    resolve_strategy = "accept_source" if batch_name in core.SOURCE_ACCEPT_BATCHES else "auto-merge"
                    for pass_index, pass_paths in enumerate(opened_passes, start=1):
                        completed_passes = pass_index
                        resolve_results.extend(
                            self._run_scoped_capture(
                                core,
                                p4,
                                resolve_command,
                                pass_paths,
                                batch_name=batch_name,
                                change_number=change_number,
                                batch_index=index,
                                total_batches=total_batches,
                                pass_index=pass_index,
                                pass_count=total_passes,
                                step_label=" ".join(resolve_command),
                                state=progress_state,
                            )
                        )

                scan_passes = self._build_passes(core, opened_paths, pass_file_limit)
                scan_results = []
                total_scan_passes = len(scan_passes)
                for scan_index, pass_paths in enumerate(scan_passes, start=1):
                    scan_results.extend(
                        self._run_scoped_capture(
                            core,
                            p4,
                            ["resolve", "-n"],
                            pass_paths,
                            batch_name=batch_name,
                            change_number=change_number,
                            batch_index=index,
                            total_batches=total_batches,
                            pass_index=scan_index,
                            pass_count=total_scan_passes,
                            step_label="resolve -n",
                            state=progress_state,
                        )
                    )
                result_outputs = [result.stdout for result in resolve_results if result.stdout]
                result_errors = [result.stderr for result in resolve_results if result.stderr]
                scan_outputs = [result.stdout for result in scan_results if result.stdout]
                scan_errors = [result.stderr for result in scan_results if result.stderr]
                combined_scan_output = core.combine_command_outputs([*scan_outputs, *scan_errors])
                unresolved_paths = core.parse_depot_paths_from_output(combined_scan_output)
                tampered_paths = self._parse_tampered_local_paths(
                    core.combine_command_outputs([*result_outputs, *result_errors, *scan_outputs, *scan_errors])
                )
                unresolved_targets = self._dedupe_paths([*unresolved_paths, *tampered_paths])
                unexpected_errors = []
                for result in [*resolve_results, *scan_results]:
                    if result.exit_code == 0:
                        continue
                    message = result.stderr.strip() or result.stdout.strip()
                    if self._is_tolerable_resolve_error(message):
                        continue
                    unexpected_errors.append(message)
                if unexpected_errors:
                    raise core.P4Error(core.combine_command_outputs(unexpected_errors))

                if unresolved_targets:
                    policy_observation = self._build_policy_observation(
                        batch_name=batch_name,
                        blocker_type="resolve_failed",
                        unresolved_targets=unresolved_targets,
                    )
                    if policy_observation is not None:
                        policy_observations.append(policy_observation)
                    unresolved_groups = [(f"conflict-{batch_name}-unresolved", unresolved_targets)]
                    if batch_name == "plugins":
                        auto_accept_targets = [target for target in unresolved_targets if not core.is_plugin_manual_review_path(target)]
                        manual_review_targets = [target for target in unresolved_targets if core.is_plugin_manual_review_path(target)]
                        unresolved_groups = []
                        if auto_accept_targets:
                            unresolved_groups.append(("conflict-plugins-autoaccept-unresolved", auto_accept_targets))
                        if manual_review_targets:
                            unresolved_groups.append(("conflict-plugins-review-unresolved", manual_review_targets))
                    for conflict_bucket_name, conflict_targets in unresolved_groups:
                        conflict_change = core.create_conflict_bucket_changelist(p4, conflict_bucket_name, change_number)
                        self._dashboard_command(p4)
                        core.move_files_to_changelist(p4, conflict_change, conflict_targets)
                        self._dashboard_command(p4)
                        conflict_opened_output = p4.run("opened", "-c", str(conflict_change))
                        self._dashboard_command(p4)
                        conflict_opened_after = core.count_output_entries(conflict_opened_output, ignore_patterns=["file(s) not opened"])
                        conflict_bucket = {
                            "bucket": conflict_bucket_name,
                            "change": conflict_change,
                            "file_count": conflict_opened_after,
                            "unresolved_after": conflict_opened_after,
                            "action": resolve_strategy,
                            "pass_count": total_passes,
                            "command_chunk_limit": self._resolve_command_file_limit(),
                        }
                        conflict_buckets.append(conflict_bucket)
                        bucket_summaries.append(conflict_bucket)

                opened_after_output = p4.run("opened", "-c", str(change_number))
                self._dashboard_command(p4)
                opened_after = core.count_output_entries(opened_after_output, ignore_patterns=["file(s) not opened"])
                opened_total_after += opened_after
                unresolved_total_after += len(unresolved_targets)
                if not core.should_skip_review_bucket_for_batch(batch_name):
                    review_bucket = {
                        "bucket": f"review-{batch_name}",
                        "change": change_number,
                        "opened_before": opened_before,
                        "opened_after": opened_after,
                        "unresolved_after": 0,
                        "action": resolve_strategy,
                        "pass_count": total_passes,
                        "command_chunk_limit": self._resolve_command_file_limit(),
                    }
                    bucket_summaries.append(review_bucket)
                resolved_batches.append(
                    {
                        "batch": batch_name,
                        "change": change_number,
                        "opened_before": opened_before,
                        "opened_after": opened_after,
                        "unresolved_after": len(unresolved_targets),
                        "resolve_strategy": resolve_strategy,
                        "resolve_pass_count": total_passes,
                        "resolve_pass_file_limit": pass_file_limit,
                        "resolve_command_file_limit": self._resolve_command_file_limit(),
                    }
                )
                progress_state["last_successful_step"] = f"completed resolve batch {index}/{total_batches}: {batch_name}"
                self._record_progress(f"resolve batch {index}/{total_batches}: {batch_name}")

            result = "REVIEW_WITH_CONFLICT_BUCKETS" if conflict_buckets else "READY_FOR_REVIEW"
            report.update(
                {
                    "status": result,
                    "result": result,
                    "blocker_category": None,
                    "retryable": False,
                    "reason": (
                        "Resolve completed, but some unresolved files were isolated into conflict buckets."
                        if conflict_buckets
                        else "Resolve completed and all batch changelists are ready for validation."
                    ),
                    "next_action": (
                        "Review resolved batch changelists and separately inspect the remaining conflict buckets."
                        if conflict_buckets
                        else "Review resolved batch changelists and continue validation."
                    ),
                    "opened_file_count": opened_total_after,
                    "unresolved_file_count": unresolved_total_after,
                    "bucket_summaries": bucket_summaries,
                    "resolved_batches": resolved_batches,
                    "successful_batches": resolved_batches,
                    "failed_batches": [],
                    "conflict_buckets": conflict_buckets,
                    "policy_observations": policy_observations,
                    "policy_promotion_candidates": policy_promotion_candidates,
                    "note": "Resolve applied per-batch resolve policy, completed a full sweep, and isolated remaining unresolved files into policy-specific conflict changelists.",
                    "last_successful_step": progress_state.get("last_successful_step"),
                    "failed_step": None,
                    "current_batch": None,
                    "current_change": None,
                    "current_pass_index": None,
                    "current_pass_count": None,
                    "current_step_label": None,
                    "current_command_chunk_index": None,
                    "current_command_chunk_count": None,
                }
            )
            core.write_resolve_report(run_dir, report, p4)
            self._dashboard_update(step="DONE", status=result)
            self._finish(result)
            print(f"[{result}] selected {self.args.job_tag} CL: {report.get('selected_cl')}")
            for bucket in bucket_summaries:
                print(f"Bucket {bucket['bucket']}: change {bucket['change']}")
            print(f"Report: {run_dir}")
            return 10 if conflict_buckets else 0
        except core.P4Error as error:
            classified_error = core.classify_p4_error(str(error))
            failure_details = core.classify_resolve_failure(classified_error, p4.last_command())
            current_change = None
            current_batch_changes = list(report.get("batch_changes", []) or [])
            if "current_batch_info" in locals() and current_batch_info is not None:
                current_change = int(current_batch_info.get("change"))
                current_batch_changes = [dict(current_batch_info)]
                if not report.get("opened_file_count"):
                    report["opened_file_count"] = int(current_batch_info.get("file_count", 0) or 0)
            report.update(
                {
                    "status": "BLOCKED_REQUIRES_CODEX_OR_USER",
                    "error": classified_error,
                    "result": "BLOCKED_RETRYABLE",
                    "blocker_category": failure_details["blocker_category"],
                    "retryable": failure_details["retryable"],
                    "reason": classified_error,
                    "next_action": failure_details["next_action"],
                    "staged_change": current_change,
                    "batch_changes": current_batch_changes,
                    "bucket_summaries": report.get("bucket_summaries", []),
                    "resolved_batches": report.get("resolved_batches", []),
                    "failed_batches": report.get("failed_batches", []),
                    "conflict_buckets": report.get("conflict_buckets", []),
                    "policy_observations": report.get("policy_observations", []),
                    "policy_promotion_candidates": report.get("policy_promotion_candidates", []),
                    "failing_command": p4.last_command(),
                    "last_successful_step": progress_state.get("last_successful_step") if 'progress_state' in locals() else None,
                    "failed_step": progress_state.get("failed_step") if 'progress_state' in locals() else None,
                    "current_batch": progress_state.get("current_batch") if 'progress_state' in locals() else None,
                    "current_change": progress_state.get("current_change") if 'progress_state' in locals() else current_change,
                    "current_pass_index": progress_state.get("current_pass_index") if 'progress_state' in locals() else None,
                    "current_pass_count": progress_state.get("current_pass_count") if 'progress_state' in locals() else None,
                    "current_step_label": progress_state.get("current_step_label") if 'progress_state' in locals() else None,
                    "current_command_chunk_index": progress_state.get("current_command_chunk_index") if 'progress_state' in locals() else None,
                    "current_command_chunk_count": progress_state.get("current_command_chunk_count") if 'progress_state' in locals() else None,
                }
            )
            core.write_resolve_report(run_dir, report, p4)
            self._dashboard_command(p4)
            self._dashboard_update(step="BLOCKED", status="blocked")
            self._finish("blocked")
            print(f"[BLOCKED] {classified_error}", file=sys.stderr)
            print(f"Report: {run_dir}", file=sys.stderr)
            return 20


