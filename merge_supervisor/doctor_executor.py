from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from .doctor_models import DoctorBlockedCase, DoctorPolicyDecision
from .doctor_provider import DoctorProviderError
from .recovery_verifier import RecoveryVerifier


class DoctorExecutor:
    _TRANSLATION_FILE_RE = re.compile(r"Translation of file content failed near line \d+ file (?P<path>.+)$")
    _TAMPERED_FILE_RE = re.compile(r"^(?P<path>[A-Za-z]:\\.+?) tampered with before resolve - edit or revert\.$")

    def __init__(self, *, repo_root: Path, runs_dir: Path, timeout_seconds: int = 900):
        self.repo_root = repo_root
        self.runs_dir = runs_dir
        self.timeout_seconds = timeout_seconds
        self.recovery_verifier = RecoveryVerifier()

    def _find_latest_retry_status(self, started_at: datetime, expected_phase: str | None) -> tuple[str | None, str | None, str | None]:
        latest_path = None
        latest_mtime = None
        for status_path in self.runs_dir.glob("*/status.json"):
            if status_path.parent.name == "doctor":
                continue
            mtime = datetime.fromtimestamp(status_path.stat().st_mtime)
            if mtime < started_at:
                continue
            if latest_mtime is None or mtime > latest_mtime:
                latest_mtime = mtime
                latest_path = status_path
        if latest_path is None:
            return None, None, None

        import json

        status = json.loads(latest_path.read_text(encoding="utf-8"))
        phase = status.get("phase")
        result = status.get("result")
        if expected_phase and phase != expected_phase:
            return str(latest_path.parent), phase, result
        return str(latest_path.parent), phase, result

    def _p4_executable(self) -> str:
        return shutil.which("p4") or "p4"

    def _run_subprocess(self, args: list[str], *, cwd: str | None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=cwd or str(self.repo_root),
            timeout=self.timeout_seconds,
            capture_output=True,
            text=True,
        )

    def _run_subprocess_with_input(self, args: list[str], *, cwd: str | None, stdin_text: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=cwd or str(self.repo_root),
            timeout=self.timeout_seconds,
            capture_output=True,
            text=True,
            input=stdin_text,
        )

    def _iter_error_lines(self, blocked_case: DoctorBlockedCase) -> list[str]:
        lines: list[str] = []
        if blocked_case.reason:
            lines.extend(blocked_case.reason.splitlines())
        run_dir = Path(blocked_case.prior_run_dir) if blocked_case.prior_run_dir else None
        if run_dir is not None:
            error_log = run_dir / "p4-errors.log"
            if error_log.exists():
                lines.extend(error_log.read_text(encoding="utf-8", errors="replace").splitlines())
        return lines

    def _extract_charset_target_paths(self, blocked_case: DoctorBlockedCase) -> list[str]:
        seen: set[str] = set()
        ordered_paths: list[str] = []
        for line in self._iter_error_lines(blocked_case):
            match = self._TRANSLATION_FILE_RE.search(line.strip())
            if not match:
                continue
            candidate = match.group("path").strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            ordered_paths.append(candidate)
        return ordered_paths

    def _extract_tampered_local_paths(self, blocked_case: DoctorBlockedCase) -> list[str]:
        seen: set[str] = set()
        ordered_paths: list[str] = []
        for line in self._iter_error_lines(blocked_case):
            match = self._TAMPERED_FILE_RE.search(line.strip())
            if not match:
                continue
            candidate = str(Path(match.group("path").strip()))
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            ordered_paths.append(candidate)
        return ordered_paths

    def _map_local_paths_to_opened_depot_paths(self, blocked_case: DoctorBlockedCase, opened_paths: list[str], local_paths: list[str]) -> list[str]:
        if not blocked_case.p4_cwd or not blocked_case.target_stream:
            return []
        workspace_root = Path(blocked_case.p4_cwd)
        prefix = blocked_case.target_stream.rstrip("/") + "/"
        local_lookup: dict[str, str] = {}
        for depot_path in opened_paths:
            relative = depot_path[len(prefix):] if depot_path.startswith(prefix) else depot_path
            candidate = workspace_root / Path(relative.replace("/", "\\"))
            local_lookup[self._normalize_local_path(candidate)] = depot_path
        ordered: list[str] = []
        seen: set[str] = set()
        for local_path in local_paths:
            depot_path = local_lookup.get(self._normalize_local_path(local_path))
            if not depot_path or depot_path in seen:
                continue
            seen.add(depot_path)
            ordered.append(depot_path)
        return ordered

    @staticmethod
    def _normalize_local_path(path_value: str | Path) -> str:
        return os.path.normcase(os.path.normpath(str(path_value)))

    @staticmethod
    def _parse_depot_paths(output: str) -> list[str]:
        paths: list[str] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line.startswith("//"):
                continue
            paths.append(line.split("#", 1)[0])
        return paths

    @staticmethod
    def _parse_opened_paths(output: str) -> list[str]:
        paths: list[str] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            path_part = line.split(" - ", 1)[0].strip()
            if not path_part:
                continue
            paths.append(path_part.split("#", 1)[0])
        return paths

    @staticmethod
    def _parse_created_change_number(output: str) -> int:
        match = re.search(r"Change\s+(\d+)\s+created", output)
        if not match:
            raise DoctorProviderError(f"Could not parse created changelist number from output: {output.strip()}")
        return int(match.group(1))

    @staticmethod
    def _truncate(text: str, limit: int = 2000) -> str:
        if len(text) <= limit:
            return text
        return text[:limit]

    def _chunk_targets(self, p4exe: str, base_args: list[str], targets: list[str], max_files: int = 100, max_command_chars: int = 7000) -> list[list[str]]:
        chunks: list[list[str]] = []
        current: list[str] = []
        base_length = len(subprocess.list2cmdline([p4exe, *base_args]))
        current_length = base_length
        for target in targets:
            target_length = len(subprocess.list2cmdline([target])) + 1
            if current and (len(current) >= max_files or current_length + target_length > max_command_chars):
                chunks.append(current)
                current = []
                current_length = base_length
            current.append(target)
            current_length += target_length
        if current:
            chunks.append(current)
        return chunks

    def _execute_charset_retry(self, blocked_case: DoctorBlockedCase) -> dict:
        if blocked_case.phase not in {"run", "resolve"}:
            raise DoctorProviderError("Charset override recovery is only supported for blocked run/resolve-phase cases.")
        if not blocked_case.target_stream:
            raise DoctorProviderError("Charset override recovery requires the blocked case target stream.")

        p4exe = self._p4_executable()
        targets = self._extract_charset_target_paths(blocked_case)
        target_scope = "targeted_files" if targets else "full_stream_fallback"
        if targets:
            am_base = ["-C", "utf8", "resolve", "-am"]
            n_base = ["-C", "utf8", "resolve", "-n"]
            am_commands = [[p4exe, *am_base, *chunk] for chunk in self._chunk_targets(p4exe, am_base, targets)]
            n_commands = [[p4exe, *n_base, *chunk] for chunk in self._chunk_targets(p4exe, n_base, targets)]
        else:
            am_commands = [[p4exe, "-C", "utf8", "resolve", "-am", f"{blocked_case.target_stream}/..."]]
            n_commands = [[p4exe, "-C", "utf8", "resolve", "-n", f"{blocked_case.target_stream}/..."]]

        command_strings: list[str] = []
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code = 0

        for command in am_commands:
            command_strings.append(subprocess.list2cmdline(command))
            result = self._run_subprocess(command, cwd=blocked_case.p4_cwd)
            if result.stdout:
                stdout_parts.append(result.stdout)
            if result.stderr:
                stderr_parts.append(result.stderr)
            if result.returncode != 0 and exit_code == 0:
                exit_code = result.returncode

        for command in n_commands:
            command_strings.append(subprocess.list2cmdline(command))
            result = self._run_subprocess(command, cwd=blocked_case.p4_cwd)
            if result.stdout:
                stdout_parts.append(result.stdout)
            if result.stderr:
                stderr_parts.append(result.stderr)
            if result.returncode != 0 and exit_code == 0:
                exit_code = result.returncode

        combined_stdout = "\n".join(part for part in stdout_parts if part)
        combined_stderr = "\n".join(part for part in stderr_parts if part)
        return {
            "executed": True,
            "result": "recovery_executed_recovery_step_completed" if exit_code == 0 else "recovery_executed_retry_failed",
            "command": " && ".join(command_strings),
            "exit_code": exit_code,
            "stdout": combined_stdout,
            "stderr": combined_stderr,
            "retry_run_dir": None,
            "retry_phase": blocked_case.resume_from_phase or blocked_case.phase,
            "retry_result": "resolve_rechecked_after_charset_override" if exit_code == 0 else "charset_override_failed",
            "recovery_target_scope": target_scope,
            "recovery_target_count": len(targets),
            "recovery_preserved_change": blocked_case.staged_change,
        }

    def _create_conflict_change(self, p4exe: str, blocked_case: DoctorBlockedCase, parent_change: int) -> tuple[int, str]:
        description = (
            f"Doctor-isolated unresolved files from staged batch CL {parent_change}\n\n"
            f"Prior blocked phase: {blocked_case.phase}\n"
            "Created automatically by doctor recovery. No submit."
        )
        spec_result = self._run_subprocess([p4exe, "change", "-o"], cwd=blocked_case.p4_cwd)
        if spec_result.returncode != 0:
            raise DoctorProviderError(spec_result.stderr.strip() or spec_result.stdout.strip() or "Failed to fetch changelist spec.")
        spec = spec_result.stdout.replace("<enter description here>", description.replace("\n", "\n\t"))
        create_result = self._run_subprocess_with_input([p4exe, "change", "-i"], cwd=blocked_case.p4_cwd, stdin_text=spec)
        if create_result.returncode != 0:
            raise DoctorProviderError(create_result.stderr.strip() or create_result.stdout.strip() or "Failed to create conflict changelist.")
        return self._parse_created_change_number(create_result.stdout), create_result.stdout

    def _execute_isolate_conflicted_files(self, blocked_case: DoctorBlockedCase) -> dict:
        if blocked_case.phase != "resolve":
            raise DoctorProviderError("Conflict isolation recovery is only supported for blocked resolve-phase cases.")
        if not blocked_case.staged_change:
            raise DoctorProviderError("Conflict isolation recovery requires a staged batch changelist.")

        p4exe = self._p4_executable()
        opened_result = self._run_subprocess([p4exe, "opened", "-c", str(blocked_case.staged_change)], cwd=blocked_case.p4_cwd)
        if opened_result.returncode != 0:
            raise DoctorProviderError(opened_result.stderr.strip() or opened_result.stdout.strip() or "Failed to inspect opened files in staged batch changelist.")

        opened_paths = self._parse_opened_paths(opened_result.stdout)
        if not opened_paths:
            return {
                "executed": True,
                "result": "recovery_executed_recovery_step_completed",
                "command": subprocess.list2cmdline([p4exe, "opened", "-c", str(blocked_case.staged_change)]),
                "exit_code": 0,
                "stdout": opened_result.stdout,
                "stderr": opened_result.stderr,
                "retry_run_dir": None,
                "retry_phase": "resolve",
                "retry_result": "no_opened_files_left_in_staged_change",
                "recovery_target_scope": "staged_change",
                "recovery_target_count": 0,
                "recovery_preserved_change": blocked_case.staged_change,
                "recovery_isolated_conflict_change": None,
                "recovery_isolated_conflict_count": 0,
                "resume_command_override": None,
            }

        unresolved_outputs: list[str] = []
        stderr_parts: list[str] = []
        command_strings: list[str] = [subprocess.list2cmdline([p4exe, "opened", "-c", str(blocked_case.staged_change)])]
        resolve_n_base = ["resolve", "-n"]
        for chunk in self._chunk_targets(p4exe, resolve_n_base, opened_paths):
            command = [p4exe, *resolve_n_base, *chunk]
            command_strings.append(subprocess.list2cmdline(command))
            chunk_result = self._run_subprocess(command, cwd=blocked_case.p4_cwd)
            if chunk_result.stdout:
                unresolved_outputs.append(chunk_result.stdout)
            if chunk_result.stderr:
                stderr_parts.append(chunk_result.stderr)
            if chunk_result.returncode != 0:
                raise DoctorProviderError(chunk_result.stderr.strip() or chunk_result.stdout.strip() or "Failed while scanning unresolved files for conflict isolation.")

        unresolved_targets = self._parse_depot_paths("\n".join(unresolved_outputs))
        if not unresolved_targets:
            unresolved_targets = self._extract_tampered_local_paths(blocked_case)
        if not unresolved_targets:
            return {
                "executed": True,
                "result": "recovery_executed_recovery_step_completed",
                "command": " && ".join(command_strings),
                "exit_code": 0,
                "stdout": "\n".join(unresolved_outputs),
                "stderr": "\n".join(stderr_parts),
                "retry_run_dir": None,
                "retry_phase": "resolve",
                "retry_result": "no_unresolved_files_detected_after_recheck",
                "recovery_target_scope": "staged_change",
                "recovery_target_count": 0,
                "recovery_preserved_change": blocked_case.staged_change,
                "recovery_isolated_conflict_change": None,
                "recovery_isolated_conflict_count": 0,
                "resume_command_override": None,
            }

        conflict_change, conflict_create_stdout = self._create_conflict_change(p4exe, blocked_case, blocked_case.staged_change)
        command_strings.append(subprocess.list2cmdline([p4exe, "change", "-i"]))
        reopen_base = ["reopen", "-c", str(conflict_change)]
        for chunk in self._chunk_targets(p4exe, reopen_base, unresolved_targets):
            command = [p4exe, *reopen_base, *chunk]
            command_strings.append(subprocess.list2cmdline(command))
            chunk_result = self._run_subprocess(command, cwd=blocked_case.p4_cwd)
            if chunk_result.stdout:
                unresolved_outputs.append(chunk_result.stdout)
            if chunk_result.stderr:
                stderr_parts.append(chunk_result.stderr)
            if chunk_result.returncode != 0:
                raise DoctorProviderError(chunk_result.stderr.strip() or chunk_result.stdout.strip() or "Failed to move unresolved files into the isolated conflict changelist.")

        return {
            "executed": True,
            "result": "recovery_executed_recovery_step_completed",
            "command": " && ".join(command_strings),
            "exit_code": 0,
            "stdout": self._truncate("\n".join([conflict_create_stdout, *unresolved_outputs])),
            "stderr": self._truncate("\n".join(stderr_parts)),
            "retry_run_dir": None,
            "retry_phase": "resolve",
            "retry_result": "conflicted_files_isolated",
            "recovery_target_scope": "staged_change",
            "recovery_target_count": len(unresolved_targets),
            "recovery_preserved_change": blocked_case.staged_change,
            "recovery_isolated_conflict_change": conflict_change,
            "recovery_isolated_conflict_count": len(unresolved_targets),
            "resume_command_override": None,
        }

    def execute(self, blocked_case: DoctorBlockedCase, policy_decision: DoctorPolicyDecision) -> dict:
        if not policy_decision.execute_recovery:
            return {
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
                "verification_passed": False,
                "failure_reason": None,
                "verifier_outcome": None,
                "resume_bundle": None,
            }

        action = policy_decision.final_action
        if action not in {
            "retry_after_login_refresh",
            "retry_after_connectivity_restore",
            "retry_after_env_restore",
            "retry_resolve_with_charset_override",
            "kill_and_retry_same_phase_after_hang",
            "isolate_conflicted_files_and_continue",
        }:
            raise DoctorProviderError(f"No executor is wired for doctor action {action!r}.")

        if action == "retry_resolve_with_charset_override":
            execution_result = self._execute_charset_retry(blocked_case)
        elif action == "isolate_conflicted_files_and_continue":
            execution_result = self._execute_isolate_conflicted_files(blocked_case)
        else:
            command = blocked_case.resume_command
            if not command:
                raise DoctorProviderError("Doctor executor cannot retry because no resume command was recorded.")

            started_at = datetime.now()
            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(self.repo_root),
                timeout=self.timeout_seconds,
                capture_output=True,
                text=True,
            )
            retry_run_dir, retry_phase, retry_result = self._find_latest_retry_status(started_at, blocked_case.resume_from_phase)
            execution_result = {
                "executed": True,
                "result": "recovery_executed_retry_succeeded" if completed.returncode == 0 else "recovery_executed_retry_failed",
                "command": command,
                "exit_code": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "retry_run_dir": retry_run_dir,
                "retry_phase": retry_phase,
                "retry_result": retry_result,
                "recovery_target_scope": "resume_command",
                "recovery_target_count": 0,
            }

        verifier_outcome = self.recovery_verifier.verify(
            primitive_id=policy_decision.recovery_primitive_id or action,
            blocked_case=blocked_case,
            execution_result=execution_result,
            verification_plan=policy_decision.verification_plan,
        )
        execution_result["verification_passed"] = verifier_outcome.verification_passed
        execution_result["failure_reason"] = verifier_outcome.failure_reason
        execution_result["verifier_outcome"] = verifier_outcome.to_report_dict()
        execution_result["resume_bundle"] = (
            verifier_outcome.resume_bundle.to_report_dict() if verifier_outcome.resume_bundle is not None else None
        )

        if not execution_result.get("executed") or execution_result.get("exit_code") not in (0, None):
            execution_result["result"] = "recovery_executed_retry_failed"
        elif verifier_outcome.verification_passed:
            execution_result["result"] = "recovery_executed_retry_succeeded"
        else:
            execution_result["result"] = "recovery_mutated_state_requires_rediagnosis"

        return execution_result
