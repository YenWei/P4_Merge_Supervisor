import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from merge_support import artifacts as artifact_support
from merge_phases import DoctorPhase, DryRunPhase, ResolveConflictsPhase, ResolvePhase, RunPhase, SanitizePhase, SplitPhase
from merge_supervisor import DoctorEngine, DoctorExecutor, DoctorPolicy, DoctorProviderError
from merge_support import p4_output as p4_output_support
from pathlib import Path, PurePosixPath
from time import monotonic, sleep


type MergePath = str | tuple[str, str]


SOURCE_STREAM = "//ExampleDepot/Mainline_Source"
TARGET_STREAM = "//ExampleDepot/Release_Target"
JOB_TAG = "Level1"

MERGE_BATCHES = {
    "artres": [
        "Project/Content/ArtRes/...",
    ],
    "audio-config": [
        "Project/Content/Audio/AudioPakConfig/...",
    ],
    "audio-generated-external-sources-android": [
        "Project/Content/Audio/GeneratedExternalSources/Android/...",
    ],
    "audio-generated-external-sources-ps5": [
        "Project/Content/Audio/GeneratedExternalSources/PS5/...",
    ],
    "audio-generated-external-sources-windows": [
        "Project/Content/Audio/GeneratedExternalSources/Windows/...",
    ],
    "audio-generated-external-sources-ios": [
        "Project/Content/Audio/GeneratedExternalSources/iOS/...",
    ],
    "audio-generated-soundbanks": [
        "Project/Content/Audio/GeneratedSoundBanks/...",
    ],
    "audio-wwise": [
        "Project/Content/Audio/WwiseAudio/...",
    ],
    "external-actors": [
        "Project/Content/__ExternalActors__/...",
    ],
    "batch2": [
        "Project/Content/Effects/...",
        "Project/Content/MasterMaterial/...",
        "Project/Content/Movies/...",
        "Project/Content/Font/...",
        "Project/Content/Splash/...",
    ],
    "extras": [
        "Project/Content/BlueprintClass/...",
        "Project/Content/LogicRes/...",
        "Project/Content/UI/...",
        "Project/Content/SPSkill/...",
        "Project/Content/Maps/...",
        "Project/Content/LevelSequence/...",
        "Project/Content/__ExternalObjects__/...",
        "Project/Content/InteractableObjectActor/...",
    ],
    "plugins": [
        "Project/Plugins/...",
    ],
    "project-tools": [
        "Project/Tool/...",
    ],
    "project-rest": [
        "Project/*",
        "Project/AutoImportConfig/...",
        "Project/BatchFiles/...",
        "Project/Binaries/...",
        "Project/Build/...",
        "Project/Config/...",
        "Project/Designer/...",
        "Project/Platforms/...",
        "Project/QuickStartBat/...",
        "Project/Content/AutoConfig/...",
        "Project/Content/Avatar/...",
        "Project/Content/ClientTemp/...",
        "Project/Content/DestructableObject/...",
        "Project/Content/EngineMap/...",
        "Project/Content/Proto/...",
        "Project/Content/SceneManagement/...",
        "Project/Content/Script/...",
        "Project/Content/ScriptBytecode/...",
        "Project/Content/ServerData/...",
        "Project/Content/StartUpDev/...",
        "Project/Content/TableData/...",
    ],
    "engine": [
        ("Editor/Engine/...", "Engine/..."),
    ],
}
MERGE_BATCHES["audio"] = (
    MERGE_BATCHES["audio-config"]
    + MERGE_BATCHES["audio-generated-external-sources-android"]
    + MERGE_BATCHES["audio-generated-external-sources-ps5"]
    + MERGE_BATCHES["audio-generated-external-sources-windows"]
    + MERGE_BATCHES["audio-generated-external-sources-ios"]
    + MERGE_BATCHES["audio-generated-soundbanks"]
    + MERGE_BATCHES["audio-wwise"]
)
MERGE_BATCHES["audio-generated-external-sources"] = (
    MERGE_BATCHES["audio-generated-external-sources-android"]
    + MERGE_BATCHES["audio-generated-external-sources-ps5"]
    + MERGE_BATCHES["audio-generated-external-sources-windows"]
    + MERGE_BATCHES["audio-generated-external-sources-ios"]
)
MERGE_BATCHES["default-content"] = MERGE_BATCHES["batch2"] + MERGE_BATCHES["extras"]
MERGE_BATCH_PRESETS = {
    "review-focused": [
        "plugins",
        "project-tools",
        "engine",
    ],
    "bulk-source-accept": [
        "artres",
        "audio-config",
        "audio-generated-external-sources-android",
        "audio-generated-external-sources-ps5",
        "audio-generated-external-sources-windows",
        "audio-generated-external-sources-ios",
        "audio-generated-soundbanks",
        "audio-wwise",
        "external-actors",
        "batch2",
        "extras",
        "project-rest",
    ],
}
DEFAULT_RUN_BATCHES = [
    "artres",
    "audio-config",
    "audio-generated-external-sources-android",
    "audio-generated-external-sources-ps5",
    "audio-generated-external-sources-windows",
    "audio-generated-external-sources-ios",
    "audio-generated-soundbanks",
    "audio-wwise",
    "external-actors",
    "batch2",
    "extras",
    "project-tools",
    "plugins",
    "project-rest",
    "engine",
]
SOURCE_ACCEPT_BATCHES = set(DEFAULT_RUN_BATCHES) - {"plugins", "project-tools"}

PLUGIN_MANUAL_REVIEW_EXTENSIONS = {
    ".cpp",
    ".h",
    ".hpp",
    ".c",
    ".cc",
    ".inl",
    ".cs",
    ".ini",
    ".uplugin",
}
PLUGIN_MANUAL_REVIEW_SUFFIXES = {
    ".build.cs",
}

CONFLICT_ACCEPT_SOURCE_BATCHES = SOURCE_ACCEPT_BATCHES


def available_batch_names() -> list[str]:
    return sorted(set(MERGE_BATCHES) | set(MERGE_BATCH_PRESETS))


def available_batch_help_text() -> str:
    return ", ".join(available_batch_names())


def expand_requested_batches(batch_names: list[str]) -> list[str]:
    expanded: list[str] = []
    seen: set[str] = set()
    for batch_name in batch_names:
        if batch_name in MERGE_BATCH_PRESETS:
            concrete_batches = MERGE_BATCH_PRESETS[batch_name]
        elif batch_name in MERGE_BATCHES:
            concrete_batches = [batch_name]
        else:
            available = available_batch_help_text()
            raise P4Error(f"Unknown merge batch {batch_name!r}. Available batches and presets: {available}")
        for concrete_batch in concrete_batches:
            if concrete_batch not in seen:
                seen.add(concrete_batch)
                expanded.append(concrete_batch)
    return expanded


def normalize_requested_batches(args) -> None:
    if hasattr(args, "batches") and getattr(args, "batches") is not None:
        args.batches = expand_requested_batches(list(args.batches))


class P4Error(RuntimeError):
    pass


class P4HangError(P4Error):
    pass


@dataclass
class CommandResult:
    args: list[str]
    exit_code: int
    stdout: str
    stderr: str


class Watchdog:
    def __init__(self, no_progress_seconds: int = 900, check_interval_seconds: float = 1.0):
        self.no_progress_seconds = no_progress_seconds
        self.check_interval_seconds = check_interval_seconds
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._phase = ""
        self._step = ""
        self._current_command = ""
        self._current_target = ""
        self._last_progress_at = monotonic()
        self._suspected_hang = False
        self._hang_reason = ""

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.5)

    def update(self, phase: str | None = None, step: str | None = None, command: list[str] | None = None, target: str | None = None) -> None:
        with self._lock:
            if phase is not None:
                self._phase = phase
            if step is not None:
                self._step = step
            if command is not None:
                self._current_command = " ".join(command)
            if target is not None:
                self._current_target = target
            self._last_progress_at = monotonic()
            self._suspected_hang = False
            self._hang_reason = ""

    def should_cancel(self) -> bool:
        with self._lock:
            return self._suspected_hang

    def hang_reason(self) -> str:
        with self._lock:
            return self._hang_reason

    def _run(self) -> None:
        while not self._stop_event.wait(self.check_interval_seconds):
            with self._lock:
                if self._suspected_hang:
                    continue
                elapsed = monotonic() - self._last_progress_at
                if elapsed < self.no_progress_seconds:
                    continue
                command = self._current_command or "unknown command"
                target = self._current_target or "-"
                self._suspected_hang = True
                self._hang_reason = (
                    f"Suspected hang detected after {int(elapsed)}s without progress "
                    f"during phase={self._phase or '-'} step={self._step or '-'} "
                    f"target={target} command={command}"
                )


class LiveDashboard:
    def __init__(self, stream = None, enabled: bool = True):
        self.stream = stream or sys.stdout
        self.enabled = enabled and bool(getattr(self.stream, "isatty", lambda: False)())
        self.state = {
            "title": "Merge Supervisor",
            "phase": "",
            "step": "",
            "batch": "",
            "item": "",
            "target": "",
            "progress": "",
            "selected_cl": "",
            "staged_cl": "",
            "elapsed": "00:00:00",
            "last_command": "",
            "last_progress": "",
            "status": "",
        }
        self._lock = threading.Lock()
        self._last_line_count = 0
        self._start = monotonic()
        self._last_progress_at = self._start
        self._stop_event = threading.Event()
        self._ticker_thread = None
        if self.enabled:
            self._ticker_thread = threading.Thread(target=self._ticker_loop, daemon=True)
            self._ticker_thread.start()

    def update(self, **fields) -> None:
        with self._lock:
            self.state.update({key: value for key, value in fields.items() if value is not None})
            self.state["elapsed"] = self._format_elapsed()
        self.render()

    def command(self, args: list[str] | None) -> None:
        if args:
            self.update(last_command=" ".join(args))

    def mark_progress(self, note: str | None = None) -> None:
        with self._lock:
            self._last_progress_at = monotonic()
            if note:
                self.state["last_progress"] = note
            elif not self.state["last_progress"]:
                self.state["last_progress"] = "progress recorded"
            self.state["elapsed"] = self._format_elapsed()
        self.render()

    def render(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self.state["elapsed"] = self._format_elapsed()
            last_progress = self._format_last_progress()
            lines = [
                self._colorize(self.state["title"], "bright_cyan"),
                self._row("Phase", self.state["phase"] or "-", self._phase_color(self.state["phase"])),
                self._row("Step", self.state["step"] or "-", self._step_color(self.state["step"])),
                self._row("Batch", self.state["batch"] or "-", "white"),
                self._row("Item", self.state["item"] or "-", "white"),
                self._row("Target", self.state["target"] or "-", "white"),
                self._row("Progress", self.state["progress"] or "-", "bright_cyan"),
                self._row("Selected CL", self.state["selected_cl"] or "-", "bright_white"),
                self._row("Staged CL", self.state["staged_cl"] or "-", "bright_white"),
                self._row("Elapsed", self.state["elapsed"], "bright_yellow"),
                self._row("Last command", self.state["last_command"] or "-", "dim"),
                self._row("Last progress", last_progress, "green"),
                self._row("Result", self.state["status"] or "-", self._status_color(self.state["status"])),
            ]
            self.stream.write("\x1b[H\x1b[J")
            if self._last_line_count:
                self.stream.write("\r")
                if self._last_line_count > 1:
                    self.stream.write("\x1b[{}A".format(self._last_line_count - 1))
            for index, line in enumerate(lines):
                clear = "\x1b[2K"
                if index < len(lines) - 1:
                    self.stream.write(f"{clear}\r{line}\n")
                else:
                    self.stream.write(f"{clear}\r{line}")
            self.stream.flush()
            self._last_line_count = len(lines)

    def finish(self, status: str | None = None) -> None:
        self._stop_event.set()
        if status is not None:
            with self._lock:
                self.state["status"] = status
        if self._ticker_thread is not None and self._ticker_thread.is_alive():
            self._ticker_thread.join(timeout=0.5)
        if self.enabled:
            self.render()
            self.stream.write("\n")
            self.stream.flush()
            self._last_line_count = 0

    def _format_elapsed(self) -> str:
        elapsed_seconds = max(0.0, monotonic() - self._start)
        whole_seconds = int(elapsed_seconds)
        tenths = int((elapsed_seconds - whole_seconds) * 10)
        hours, remainder = divmod(whole_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{tenths}"

    def _ticker_loop(self) -> None:
        while not self._stop_event.wait(0.2):
            self.render()

    def _format_last_progress(self) -> str:
        age_seconds = max(0.0, monotonic() - self._last_progress_at)
        whole_seconds = int(age_seconds)
        hours, remainder = divmod(whole_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        age = f"{hours:02d}:{minutes:02d}:{seconds:02d} ago"
        note = self.state["last_progress"] or "waiting for first completed command"
        return f"{age} - {note}"

    def _row(self, label: str, value: str, value_color: str) -> str:
        return f"{self._colorize(label + ':', 'dim')} {self._colorize(value, value_color)}"

    def _colorize(self, text: str, color: str) -> str:
        colors = {
            "dim": "\x1b[2m",
            "white": "\x1b[37m",
            "bright_white": "\x1b[97m",
            "yellow": "\x1b[33m",
            "bright_yellow": "\x1b[93m",
            "green": "\x1b[32m",
            "bright_green": "\x1b[92m",
            "red": "\x1b[31m",
            "bright_red": "\x1b[91m",
            "magenta": "\x1b[35m",
            "bright_cyan": "\x1b[96m",
            "cyan": "\x1b[36m",
        }
        prefix = colors.get(color, "")
        suffix = "\x1b[0m" if prefix else ""
        return f"{prefix}{text}{suffix}"

    def _phase_color(self, phase: str) -> str:
        if phase == "doctor":
            return "bright_cyan"
        if phase in {"run", "resolve", "split", "sanitize", "resolve-conflicts"}:
            return "bright_white"
        if phase == "dry-run":
            return "cyan"
        return "white"

    def _step_color(self, step: str) -> str:
        if step in {"BLOCKED"}:
            return "bright_red"
        if step in {"DONE"}:
            return "bright_green"
        if step in {"MERGING", "RESOLVING", "SPLITTING", "SANITIZING", "CLASSIFYING", "PREVIEWING", "STAGING_PENDING_CL"}:
            return "bright_yellow"
        if step in {"PREFLIGHT", "INPUT_RESOLUTION", "STARTING", "SPLIT_PHASE", "SANITIZE_PHASE", "CONFLICT_RESOLUTION"}:
            return "cyan"
        return "white"

    def _status_color(self, status: str) -> str:
        upper_status = (status or "").upper()
        if "BLOCKED" in upper_status:
            return "bright_red"
        if upper_status in {"REQUIRES_HUMAN_REVIEW", "SPLIT_WITH_CONFLICT_BUCKETS", "REVIEW_WITH_CONFLICT_BUCKETS"}:
            return "magenta"
        if upper_status in {"READY", "READY_TO_SPLIT", "READY_TO_RESOLVE", "READY_NO_CHANGES", "READY_FOR_REVIEW", "READY_FOR_ACTION", "DONE"}:
            return "bright_green"
        if upper_status in {"RUNNING", "STARTED"}:
            return "bright_yellow"
        return "white"


class P4Runner:
    def __init__(
        self,
        cwd: Path | None = None,
        watchdog: Watchdog | None = None,
        progress_callback=None,
        command_callback=None,
    ):
        self.cwd = cwd
        self.results: list[CommandResult] = []
        self.executable = shutil.which("p4") or "p4"
        self.watchdog = watchdog
        self.progress_callback = progress_callback
        self.command_callback = command_callback

    def run(self, *args: str) -> str:
        return self.run_with_input(*args)

    def run_result(self, *args: str, stdin_text: str | None = None) -> CommandResult:
        command_args = [self.executable, *args]
        if self.command_callback is not None:
            self.command_callback(command_args)
        if self.watchdog is not None:
            self.watchdog.update(command=command_args)
        try:
            process = subprocess.Popen(
                command_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE if stdin_text is not None else None,
                text=True,
                cwd=self.cwd,
            )
            try:
                while True:
                    if self.watchdog is not None and self.watchdog.should_cancel():
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                        raise P4HangError(self.watchdog.hang_reason())
                    try:
                        stdout, stderr = process.communicate(input=stdin_text, timeout=0.5)
                        break
                    except subprocess.TimeoutExpired:
                        stdin_text = None
                        continue
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
        except FileNotFoundError as error:
            if self.cwd is not None and not Path(self.cwd).exists():
                raise P4Error(f"Perforce working directory does not exist: {self.cwd}") from error
            if self.executable == "p4":
                raise P4Error("p4 executable was not found on PATH.") from error
            raise P4Error(f"Failed to launch {self.executable}: {error.strerror or error}") from error
        result = CommandResult(
            args=command_args,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )
        self.results.append(result)
        if self.progress_callback is not None:
            self.progress_callback()
        return result

    def run_with_input(self, *args: str, stdin_text: str | None = None) -> str:
        result = self.run_result(*args, stdin_text=stdin_text)
        if result.exit_code != 0:
            message = result.stderr.strip() or result.stdout.strip()
            raise P4Error(f"p4 {' '.join(args)} failed: {message}")
        return result.stdout

    def last_command(self) -> list[str] | None:
        if not self.results:
            return None
        return self.results[-1].args


def parse_client_stream(client_spec: str) -> str | None:
    for line in client_spec.splitlines():
        if line.startswith("Stream:"):
            return line.split(":", 1)[1].strip()
    return None


def validate_p4_cwd(path: str | None) -> Path | None:
    if path is None:
        return None
    cwd = Path(path)
    if not cwd.is_dir():
        raise P4Error(f"Perforce working directory does not exist: {path}")
    return cwd


def classify_p4_error(message: str) -> str:
    lower_message = message.lower()
    if "suspected hang detected after" in lower_message:
        return (
            f"{message}\n\n"
            "Diagnosis: the phase stopped making progress for too long and the watchdog terminated the current Perforce command.\n"
            "Fix: inspect the current checkpoint and let doctor decide whether the phase can be safely retried."
        )
    if "wsaeacces" in lower_message or "forbidden by its access permissions" in lower_message:
        return (
            f"{message}\n\n"
            "Diagnosis: Codex or the current shell blocked network socket access to the Perforce server. "
            "This is not a merge conflict and not a branch problem.\n"
            "Fix: rerun with elevated/approved network permissions, then continue the same merge command."
        )
    if "tcp connect to perforce:1666 failed" in lower_message:
        return (
            f"{message}\n\n"
            "Diagnosis: P4PORT was not available to this process, so p4 fell back to perforce:1666.\n"
            "Fix: run through the wrapper batch file so P4PORT, P4USER, and P4CLIENT are set."
        )
    return message


def classify_dry_run_failure(message: str, failing_command: list[str] | None = None) -> dict:
    lower_message = message.lower()
    category = "unknown"
    retryable = False
    next_action = "Inspect the run artifacts and resolve the blocker before rerunning dry-run."

    if "perforce working directory does not exist" in lower_message:
        category = "invalid_p4_cwd"
        next_action = "Correct --p4-cwd to a valid workspace path, then rerun dry-run."
    elif "suspected hang detected after" in lower_message:
        category = "suspected_hang"
        retryable = True
        next_action = "Run doctor to inspect the suspected hang checkpoint before retrying dry-run."
    elif "p4 executable was not found on path" in lower_message:
        category = "p4_missing"
        next_action = "Install the p4 CLI or fix PATH so the p4 executable is available, then rerun dry-run."
    elif "your session has expired" in lower_message or "not logged in" in lower_message or "p4 login -s failed" in lower_message:
        category = "p4_auth"
        retryable = True
        next_action = "Run p4 login, then rerun dry-run."
    elif "wsaeacces" in lower_message or "forbidden by its access permissions" in lower_message:
        category = "p4_connectivity_blocked"
        retryable = True
        next_action = "Rerun with approved network access to the Perforce server, then retry dry-run."
    elif "tcp connect to perforce:1666 failed" in lower_message:
        category = "p4_env_missing"
        retryable = True
        next_action = "Run through the wrapper batch file or restore P4PORT/P4USER/P4CLIENT, then rerun dry-run."
    elif "connect to server failed" in lower_message or "tcp connect" in lower_message or "timed out" in lower_message:
        category = "p4_connectivity"
        retryable = True
        next_action = "Verify Perforce server/network availability, then rerun dry-run."
    elif "is not a stream client" in lower_message:
        category = "wrong_client_type"
        next_action = "Switch to the intended stream client/workspace, then rerun dry-run."
    elif "current p4 client stream is" in lower_message:
        category = "wrong_client_stream"
        next_action = "Switch to the workspace mapped to the target stream, then rerun dry-run."
    elif "default changelist already has opened files" in lower_message:
        category = "dirty_default_cl"
        next_action = "Clean, shelve, move, or submit the existing opened files before rerunning dry-run."
    elif "has a fix/job matching" in lower_message:
        category = "missing_source_boundary"
        next_action = "Confirm the job tag or boundary selection rule, then rerun dry-run."
    elif "unknown merge batch" in lower_message:
        category = "invalid_batch"
        next_action = "Fix the requested batch names, then rerun dry-run."
    elif failing_command and len(failing_command) >= 3 and failing_command[1] == "merge" and "-n" in failing_command:
        category = "preview_failed"
        next_action = "Inspect the failing preview command and Perforce state, then rerun dry-run after resolving the issue."

    return {
        "phase": "dry-run",
        "result": "BLOCKED_RETRYABLE" if retryable else "BLOCKED_HUMAN",
        "blocker_category": category,
        "retryable": retryable,
        "reason": message,
        "next_action": next_action,
    }


def classify_run_failure(message: str, failing_command: list[str] | None = None, merge_started: bool = False) -> dict:
    lower_message = message.lower()
    category = "unknown"
    retryable = False
    next_action = "Inspect the workspace state and run artifacts manually before deciding how to proceed."

    if "perforce working directory does not exist" in lower_message:
        category = "invalid_p4_cwd"
        next_action = "Correct --p4-cwd to a valid workspace path, then rerun run."
    elif "suspected hang detected after" in lower_message:
        category = "suspected_hang"
        retryable = True
        next_action = "Run doctor to inspect the suspected hang checkpoint before retrying run."
    elif "p4 executable was not found on path" in lower_message:
        category = "p4_missing"
        next_action = "Install the p4 CLI or fix PATH so the p4 executable is available, then rerun run."
    elif "your session has expired" in lower_message or "not logged in" in lower_message or "p4 login -s failed" in lower_message:
        category = "p4_auth"
        retryable = True
        next_action = "Run p4 login, then rerun run."
    elif "wsaeacces" in lower_message or "forbidden by its access permissions" in lower_message:
        category = "p4_connectivity_blocked"
        retryable = True
        next_action = "Rerun with approved network access to the Perforce server, then retry run."
    elif "tcp connect to perforce:1666 failed" in lower_message:
        category = "p4_env_missing"
        retryable = True
        next_action = "Run through the wrapper batch file or restore P4PORT/P4USER/P4CLIENT, then rerun run."
    elif "connect to server failed" in lower_message or "tcp connect" in lower_message or "timed out" in lower_message:
        category = "p4_connectivity"
        retryable = True
        next_action = "Verify Perforce server/network availability, then retry or resume run."
    elif "default changelist already has opened files" in lower_message:
        category = "dirty_default_cl"
        next_action = "Clean, shelve, move, or submit the existing opened files before rerunning run."
    elif "can't translate" in lower_message or "translation of file content failed" in lower_message or "unicode" in lower_message:
        category = "resolve_charset"
        retryable = True
        next_action = "Apply the approved charset recovery path, then retry the resolve step before split."
    elif "is not a stream client" in lower_message:
        category = "wrong_client_type"
        next_action = "Switch to the intended stream client/workspace, then rerun run."
    elif "current p4 client stream is" in lower_message:
        category = "wrong_client_stream"
        next_action = "Switch to the workspace mapped to the target stream, then rerun run."
    elif "has a fix/job matching" in lower_message:
        category = "missing_source_boundary"
        next_action = "Confirm the job tag or boundary selection rule, then rerun run."
    elif "unknown merge batch" in lower_message:
        category = "invalid_batch"
        next_action = "Fix the requested batch names, then rerun run."
    elif failing_command and len(failing_command) >= 3 and failing_command[1] == "merge":
        category = "partial_merge_failure" if merge_started else "merge_failed"
        next_action = (
            "Inspect the partially staged workspace and decide whether a safe continuation or cleanup path exists before split."
            if merge_started
            else "Inspect the failing merge command and workspace state before retrying run."
        )
    elif failing_command and len(failing_command) >= 3 and failing_command[1] == "resolve":
        category = "resolve_failed"
        next_action = "Inspect the resolve failure and current staged state before deciding how to continue."
    elif failing_command and len(failing_command) >= 3 and failing_command[1] == "opened":
        category = "opened_snapshot_failed"
        next_action = "Inspect the workspace state before continuing into split."

    return {
        "phase": "run",
        "result": "BLOCKED_RETRYABLE" if retryable else "BLOCKED_HUMAN",
        "blocker_category": category,
        "retryable": retryable,
        "reason": message,
        "next_action": next_action,
    }


def classify_resolve_failure(message: str, failing_command: list[str] | None = None) -> dict:
    lower_message = message.lower()
    category = "resolve_failed"
    retryable = True
    next_action = "Inspect the current batch resolve state and rerun resolve when safe."

    if "suspected hang detected after" in lower_message:
        category = "suspected_hang"
        next_action = "Run doctor to inspect the suspected hang checkpoint before retrying resolve."
    elif "can't translate" in lower_message or "translation of file content failed" in lower_message or "unicode" in lower_message:
        category = "resolve_charset"
        next_action = "Apply the approved charset recovery path, then retry the resolve step before continuing."
    elif failing_command and len(failing_command) >= 3 and failing_command[1] == "opened":
        category = "opened_snapshot_failed"
        next_action = "Inspect the workspace state before retrying resolve."

    return {
        "phase": "resolve",
        "result": "BLOCKED_RETRYABLE" if retryable else "BLOCKED_HUMAN",
        "blocker_category": category,
        "retryable": retryable,
        "reason": message,
        "next_action": next_action,
    }


def classify_split_failure(message: str, failing_command: list[str] | None = None) -> dict:
    lower_message = message.lower()
    category = "split_failed"
    retryable = False
    next_action = "Inspect the split input changelist and artifacts manually before retrying split."
    if "suspected hang detected after" in lower_message:
        category = "suspected_hang"
        retryable = True
        next_action = "Run doctor to inspect the suspected hang checkpoint before retrying split."
    elif "maximum of 2000 simultaneous commands" in lower_message or "please try again later when the load is lower" in lower_message:
        category = "p4_server_busy"
        retryable = True
        next_action = "Retry split after a short backoff or reduce split chunk size so reopen operations put less pressure on the server."
    return {
        "phase": "split",
        "result": "BLOCKED_RETRYABLE" if retryable else "BLOCKED_HUMAN",
        "blocker_category": category,
        "retryable": retryable,
        "reason": message,
        "next_action": next_action,
    }


def classify_sanitize_failure(message: str, failing_command: list[str] | None = None) -> dict:
    lower_message = message.lower()
    category = "sanitize_failed"
    retryable = False
    next_action = "Inspect the split changelists and sanitize artifacts manually before retrying sanitize."
    if "suspected hang detected after" in lower_message:
        category = "suspected_hang"
        retryable = True
        next_action = "Run doctor to inspect the suspected hang checkpoint before retrying sanitize."
    return {
        "phase": "sanitize",
        "result": "BLOCKED_RETRYABLE" if retryable else "BLOCKED_HUMAN",
        "blocker_category": category,
        "retryable": retryable,
        "reason": message,
        "next_action": next_action,
    }


def classify_conflict_resolution_failure(message: str, failing_command: list[str] | None = None) -> dict:
    lower_message = message.lower()
    category = "conflict_resolution_failed"
    retryable = False
    next_action = "Inspect the conflict buckets and conflict-resolution artifacts manually before retrying resolve-conflicts."
    if "suspected hang detected after" in lower_message:
        category = "suspected_hang"
        retryable = True
        next_action = "Run doctor to inspect the suspected hang checkpoint before retrying resolve-conflicts."
    return {
        "phase": "resolve-conflicts",
        "result": "BLOCKED_RETRYABLE" if retryable else "BLOCKED_HUMAN",
        "blocker_category": category,
        "retryable": retryable,
        "reason": message,
        "next_action": next_action,
    }


def classify_doctor_case(
    phase: str,
    result: str,
    blocker_category: str | None,
    reason: str | None,
    failing_command: list[str] | None,
) -> dict:
    return DoctorEngine.classify_case(phase, result, blocker_category, reason, failing_command).to_report_dict()


def parse_change_numbers(changes_output: str) -> list[int]:
    numbers = []
    for line in changes_output.splitlines():
        match = re.match(r"^Change\s+(\d+)\b", line)
        if match:
            numbers.append(int(match.group(1)))
    return numbers


def parse_created_change_number(output: str) -> int:
    match = re.search(r"Change\s+(\d+)\s+created", output)
    if not match:
        raise P4Error(f"Could not parse changelist number from output: {output.strip()}")
    return int(match.group(1))


def fixes_contain_job_tag(fixes_output: str, job_tag: str) -> bool:
    normalized_tag = job_tag.replace(" ", "").lower()
    for line in fixes_output.splitlines():
        normalized_line = line.replace(" ", "").lower()
        if normalized_tag in normalized_line:
            return True
    return False


def find_newest_level_job_cl(p4, source_path: str, job_tag: str, max_changes: int = 25) -> int:
    changes_output = p4.run("changes", "-s", "submitted", "-m", str(max_changes), source_path)
    change_numbers = parse_change_numbers(changes_output)
    for change in change_numbers:
        fixes_output = p4.run("fixes", "-c", str(change))
        if fixes_contain_job_tag(fixes_output, job_tag):
            return change
    raise P4Error(f"No submitted changelist under {source_path} has a fix/job matching {job_tag!r}.")


def get_merge_paths_for_batch(batch_name: str) -> list[MergePath]:
    if batch_name not in MERGE_BATCHES:
        available = available_batch_help_text()
        raise P4Error(f"Unknown merge batch {batch_name!r}. Available batches and presets: {available}")
    return list(MERGE_BATCHES[batch_name])


def get_merge_paths_for_batches(batch_names: list[str]) -> list[MergePath]:
    paths: list[MergePath] = []
    for batch_name in batch_names:
        paths.extend(get_merge_paths_for_batch(batch_name))
    return paths


def split_merge_path(relative_path: MergePath) -> tuple[str, str, str]:
    if isinstance(relative_path, tuple):
        if len(relative_path) != 2:
            raise P4Error(
                "Invalid merge path tuple shape "
                f"{relative_path!r}. Expected (source_path, target_path)."
            )
        source_path, target_path = relative_path
        return source_path, target_path, f"{source_path} -> {target_path}"
    return relative_path, relative_path, relative_path


def relative_target_patterns_for_batch(batch_name: str) -> list[str]:
    patterns = []
    for merge_path in MERGE_BATCHES[batch_name]:
        _, target_path, _ = split_merge_path(merge_path)
        patterns.append(target_path)
    return patterns


def find_batch_for_relative_path(relative_path: str) -> str | None:
    for batch_name in DEFAULT_RUN_BATCHES:
        for pattern in relative_target_patterns_for_batch(batch_name):
            if matches_relative_pattern(relative_path, pattern):
                return batch_name
    return None


def depot_to_relative_target_path(depot_path: str, target_stream: str) -> str:
    prefix = target_stream.rstrip("/") + "/"
    if depot_path.startswith(prefix):
        return depot_path[len(prefix):]
    return depot_path


def depot_to_relative_stream_path(depot_path: str, stream: str) -> str:
    prefix = stream.rstrip("/") + "/"
    if depot_path.startswith(prefix):
        return depot_path[len(prefix):]
    return depot_path


def p4_pattern_to_fnmatch(pattern: str) -> str:
    return pattern.replace("...", "*")


def matches_relative_pattern(relative_path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(relative_path, p4_pattern_to_fnmatch(pattern))


def build_merge_command(source_stream: str, target_stream: str, source_cl: int, relative_path = "...") -> list[str]:
    source_path, target_path, _ = split_merge_path(relative_path)
    return ["p4", "merge", "-F", f"{source_stream}/{source_path}@{source_cl}", f"{target_stream}/{target_path}"]


def build_target_filespecs(target_stream: str, relative_paths: list[MergePath]) -> list[str]:
    return p4_output_support.build_target_filespecs(target_stream, relative_paths, split_merge_path)


def run_p4_scoped_to_filespecs(p4, command_args: list[str], filespecs: list[str]) -> str:
    return p4_output_support.run_p4_scoped_to_filespecs(p4, command_args, filespecs)


def combine_command_outputs(outputs: list[str]) -> str:
    return p4_output_support.combine_command_outputs(outputs)


def count_preview_files(preview_output: str) -> int:
    return p4_output_support.count_preview_files(preview_output)


def count_output_entries(output: str, ignore_patterns: list[str] | None = None) -> int:
    return p4_output_support.count_output_entries(output, ignore_patterns)


def parse_depot_paths_from_output(output: str) -> list[str]:
    return p4_output_support.parse_depot_paths_from_output(output)


def is_plugin_manual_review_path(depot_path: str) -> bool:
    normalized = depot_path.replace("\\", "/").lower()
    if any(normalized.endswith(suffix) for suffix in PLUGIN_MANUAL_REVIEW_SUFFIXES):
        return True
    return PurePosixPath(normalized).suffix in PLUGIN_MANUAL_REVIEW_EXTENSIONS


def split_plugin_resolve_paths(opened_paths: list[str]) -> tuple[list[str], list[str]]:
    source_accept_paths: list[str] = []
    manual_review_paths: list[str] = []
    for depot_path in opened_paths:
        if is_plugin_manual_review_path(depot_path):
            manual_review_paths.append(depot_path)
        else:
            source_accept_paths.append(depot_path)
    return source_accept_paths, manual_review_paths


def chunked(values: list[str], size: int) -> list[list[str]]:
    return p4_output_support.chunked(values, size)


def chunked_by_command_length(
    values: list[str],
    fixed_args: list[str],
    max_chars: int = 7000,
    max_items: int = 50,
) -> list[list[str]]:
    return p4_output_support.chunked_by_command_length(values, fixed_args, max_chars, max_items)


def build_batch_preview_summary(batch_merge_paths: dict[str, list[MergePath]], path_results: list[dict]) -> list[dict]:
    return artifact_support.build_batch_preview_summary(batch_merge_paths, path_results)


def write_status_artifacts(run_dir: Path, report: dict) -> None:
    artifact_support.write_status_artifacts(run_dir, report)


def write_command_logs(run_dir: Path, p4: P4Runner) -> None:
    artifact_support.write_command_logs(run_dir, p4)


def execute_preflight(p4, args, include_preview: bool) -> dict:
    ensure_login(p4)
    actual_stream = ensure_client_stream(p4, args.target_stream)
    ensure_default_changelist_is_clean(p4)
    selected_cl = getattr(args, "selected_cl", None)
    if selected_cl is None:
        raise P4Error("Missing required --selected-cl. Automatic source changelist selection is no longer supported.")
    batch_merge_paths = {batch_name: get_merge_paths_for_batch(batch_name) for batch_name in args.batches}
    merge_paths = [relative_path for batch_paths in batch_merge_paths.values() for relative_path in batch_paths]
    merge_commands = [
        build_merge_command(args.source_stream, args.target_stream, selected_cl, relative_path)
        for relative_path in merge_paths
    ]
    result = {
        "client_stream": actual_stream,
        "selected_cl": selected_cl,
        "batches": args.batches,
        "batch_merge_paths": batch_merge_paths,
        "merge_paths": merge_paths,
        "merge_commands": merge_commands,
        "path_results": [],
        "total_preview_file_count": 0,
        "successful_batches": [],
        "failed_batches": [],
        "failing_command": None,
    }
    if include_preview:
        preview_result = preview_batch_merge_steps(
            p4,
            args.source_stream,
            args.target_stream,
            selected_cl,
            merge_paths,
            args.max_merge_files,
        )
        result.update(preview_result)
        result["successful_batches"] = build_batch_preview_summary(batch_merge_paths, preview_result["path_results"])
    return result


def build_run_changelist_description(args, selected_cl: int, result: str) -> str:
    summary = "Merge staged and ready to split." if result == "READY_TO_SPLIT" else "Merge staged with unresolved files to isolate during split."
    lines = [
        f"Run merge from {args.source_stream}@{selected_cl} into {args.target_stream}",
        "",
        f"Job tag: {args.job_tag}",
        f"Batches: {', '.join(args.batches)}",
        f"Result: {result}",
        summary,
        "No submit was performed.",
    ]
    return "\n".join(lines)


def build_batch_run_changelist_description(args, selected_cl: int, batch_name: str) -> str:
    return artifact_support.build_batch_run_changelist_description(args, selected_cl, batch_name)


def create_numbered_pending_changelist(p4, description: str) -> int:
    spec = p4.run("change", "-o")
    spec = spec.replace("<enter description here>", description.replace("\n", "\n\t"))
    created_output = p4.run_with_input("change", "-i", stdin_text=spec)
    return parse_created_change_number(created_output)


def move_default_opened_files_to_changelist(p4, change_number: int) -> str:
    p4.run("reopen", "-c", str(change_number), "//...")
    return p4.run("opened", "-c", str(change_number))


def find_latest_run_status(runs_dir: Path, staged_change: int | None = None) -> tuple[Path, dict] | tuple[None, None]:
    return artifact_support.find_latest_run_status(runs_dir, staged_change)


def determine_split_input(args) -> tuple[Path, dict, int]:
    return artifact_support.determine_split_input(args, error_cls=P4Error)


def determine_resolve_input(args) -> tuple[Path, dict, list[dict]]:
    return artifact_support.determine_resolve_input(args, error_cls=P4Error)


def classify_split_bucket(relative_path: str, unresolved_paths: set[str]) -> str:
    batch_name = find_batch_for_relative_path(relative_path)
    if relative_path in unresolved_paths:
        return f"conflict-{batch_name}-unresolved" if batch_name else "conflict-unresolved"
    if batch_name:
        return f"review-{batch_name}"
    return "holding-leftover"


def should_skip_review_bucket_for_batch(batch_name: str) -> bool:
    return batch_name in SOURCE_ACCEPT_BATCHES


def should_auto_accept_conflict_bucket(bucket_name: str) -> bool:
    if not bucket_name.startswith("conflict-") or not bucket_name.endswith("-unresolved"):
        return False
    bucket_key = bucket_name[len("conflict-"):-len("-unresolved")]
    if bucket_key.endswith("-autoaccept"):
        return True
    return bucket_key in CONFLICT_ACCEPT_SOURCE_BATCHES


def build_split_buckets(opened_paths: list[str], target_stream: str, unresolved_paths: set[str]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {}
    for depot_path in opened_paths:
        relative_path = depot_to_relative_target_path(depot_path, target_stream)
        bucket_name = classify_split_bucket(relative_path, unresolved_paths)
        buckets.setdefault(bucket_name, []).append(depot_path)
    return buckets


def create_split_bucket_changelist(p4, parent_change: int, bucket_name: str, staged_change: int) -> int:
    description = "\n".join(
        [
            f"Split bucket from staged merge CL {staged_change}",
            "",
            f"Bucket: {bucket_name}",
            f"Parent split source CL: {parent_change}",
            "Created automatically by split phase.",
            "No submit was performed.",
        ]
    )
    return create_numbered_pending_changelist(p4, description)


def create_conflict_bucket_changelist(p4, bucket_name: str, parent_change: int) -> int:
    return artifact_support.create_conflict_bucket_changelist(
        p4,
        bucket_name,
        parent_change,
        create_numbered_pending_changelist=create_numbered_pending_changelist,
    )


def move_files_to_changelist(
    p4,
    change_number: int,
    depot_paths: list[str],
    chunk_size: int = 50,
    progress_callback=None,
) -> dict:
    fixed_args = [p4.executable, "reopen", "-c", str(change_number)]
    safe_chunks = chunked_by_command_length(
        depot_paths,
        fixed_args=fixed_args,
        max_chars=7000,
        max_items=chunk_size,
    )
    total_files = len(depot_paths)
    total_chunks = len(safe_chunks)
    moved_files = 0
    for chunk_index, chunk in enumerate(safe_chunks, start=1):
        for attempt in range(1, 4):
            try:
                p4.run("reopen", "-c", str(change_number), *chunk)
                moved_files += len(chunk)
                if progress_callback is not None:
                    progress_callback(
                        {
                            "change": change_number,
                            "chunk_index": chunk_index,
                            "total_chunks": total_chunks,
                            "chunk_size": len(chunk),
                            "moved_files": moved_files,
                            "total_files": total_files,
                        }
                    )
                break
            except P4Error as error:
                message = str(error).lower()
                is_server_busy = (
                    "maximum of 2000 simultaneous commands" in message
                    or "please try again later when the load is lower" in message
                )
                if not is_server_busy or attempt == 3:
                    raise
                sleep(attempt * 5)
    return {
        "change": change_number,
        "total_files": total_files,
        "total_chunks": total_chunks,
        "moved_files": moved_files,
    }


def write_split_report(run_dir: Path, report: dict, p4: P4Runner | None = None) -> None:
    artifact_support.write_split_report(
        run_dir,
        report,
        status_writer=write_status_artifacts,
        command_log_writer=write_command_logs,
        p4=p4,
    )


def find_latest_resolve_summary(runs_dir: Path, change: int | None = None) -> tuple[Path, dict] | tuple[None, None]:
    return artifact_support.find_latest_resolve_summary(runs_dir, change)


def write_resolve_report(run_dir: Path, report: dict, p4: P4Runner | None = None) -> None:
    artifact_support.write_resolve_report(
        run_dir,
        report,
        status_writer=write_status_artifacts,
        command_log_writer=write_command_logs,
        p4=p4,
    )


def find_latest_split_summary(runs_dir: Path, staged_change: int | None = None) -> tuple[Path, dict] | tuple[None, None]:
    return artifact_support.find_latest_split_summary(runs_dir, staged_change)


def determine_sanitize_input(args) -> tuple[Path, dict, int]:
    return artifact_support.determine_sanitize_input(args, error_cls=P4Error)


def find_latest_sanitize_summary(runs_dir: Path, staged_change: int | None = None) -> tuple[Path, dict] | tuple[None, None]:
    return artifact_support.find_latest_sanitize_summary(runs_dir, staged_change)


def determine_conflict_resolution_input(args) -> tuple[Path, dict, int]:
    return artifact_support.determine_conflict_resolution_input(args, error_cls=P4Error)


def find_latest_doctor_input(runs_dir: Path, staged_change: int | None = None) -> tuple[Path, dict] | tuple[None, None]:
    return DoctorEngine.find_latest_doctor_input(runs_dir, staged_change)


def determine_doctor_input(args) -> tuple[Path, dict]:
    return DoctorEngine.determine_doctor_input(Path(args.runs_dir), args.change, P4Error)


def quote_command_arg(value: str) -> str:
    return DoctorEngine.quote_command_arg(value)


def build_command_string(parts: list[str]) -> str:
    return DoctorEngine.build_command_string(parts)


def build_resume_state(prior_run_dir: Path, prior_status: dict, args) -> dict:
    return DoctorEngine.build_resume_state(
        prior_run_dir,
        prior_status,
        getattr(args, "runs_dir", None),
        getattr(args, "p4_cwd", None),
    ).to_report_dict()


def write_resume_artifacts(run_dir: Path, resume_state: dict) -> None:
    DoctorEngine.write_resume_artifacts(run_dir, resume_state)


def write_doctor_report(run_dir: Path, report: dict, p4: P4Runner | None = None) -> None:
    DoctorEngine.write_doctor_report(run_dir, report, write_status_artifacts, write_command_logs, p4)


def write_sanitize_report(run_dir: Path, report: dict, p4: P4Runner | None = None) -> None:
    artifact_support.write_sanitize_report(
        run_dir,
        report,
        status_writer=write_status_artifacts,
        command_log_writer=write_command_logs,
        p4=p4,
    )


def write_conflict_resolution_report(run_dir: Path, report: dict, p4: P4Runner | None = None) -> None:
    artifact_support.write_conflict_resolution_report(
        run_dir,
        report,
        status_writer=write_status_artifacts,
        command_log_writer=write_command_logs,
        p4=p4,
    )


def perform_merge_steps(
    p4,
    source_stream: str,
    target_stream: str,
    source_cl: int,
    relative_path: str = "...",
    max_merge_files: int = 10000,
) -> dict:
    source_path, target_path, path_label = split_merge_path(relative_path)
    source_revspec = f"{source_stream}/{source_path}@{source_cl}"
    target_files = f"{target_stream}/{target_path}"
    preview_output = p4.run("merge", "-n", "-F", source_revspec, target_files)
    preview_count = count_preview_files(preview_output)
    merge_output = p4.run("merge", "-F", source_revspec, target_files)
    return {
        "merge_output": merge_output,
        "preview_output": preview_output,
        "preview_file_count": preview_count,
        "relative_path": path_label,
    }


def preview_batch_merge_steps(
    p4,
    source_stream: str,
    target_stream: str,
    source_cl: int,
    relative_paths: list[MergePath],
    max_merge_files: int = 10000,
    progress_callback=None,
) -> dict:
    path_results = []
    total_preview_file_count = 0
    total_paths = len(relative_paths)
    for index, relative_path in enumerate(relative_paths, start=1):
        source_path, target_path, path_label = split_merge_path(relative_path)
        if progress_callback is not None:
            progress_callback(index, total_paths, path_label)
        source_revspec = f"{source_stream}/{source_path}@{source_cl}"
        target_files = f"{target_stream}/{target_path}"
        preview_output = p4.run("merge", "-n", "-F", source_revspec, target_files)
        preview_count = count_preview_files(preview_output)
        path_results.append(
            {
                "relative_path": path_label,
                "preview_output": preview_output,
                "preview_file_count": preview_count,
            }
        )
        total_preview_file_count += preview_count
    return {
        "path_results": path_results,
        "total_preview_file_count": total_preview_file_count,
    }


def perform_batch_merge_steps(
    p4,
    source_stream: str,
    target_stream: str,
    source_cl: int,
    relative_paths: list[MergePath],
    batch_name: str | None = None,
    resolve_args: list[str] | None = None,
    max_merge_files: int = 10000,
    progress_callback=None,
) -> dict:
    path_results = []
    total_preview_file_count = 0
    total_paths = len(relative_paths)
    target_filespecs = build_target_filespecs(target_stream, relative_paths)
    resolve_args = list(resolve_args) if resolve_args is not None else ["resolve", "-am"]
    for index, relative_path in enumerate(relative_paths, start=1):
        _, _, path_label = split_merge_path(relative_path)
        if progress_callback is not None:
            progress_callback(index, total_paths, path_label)
        result = perform_merge_steps(
            p4,
            source_stream,
            target_stream,
            source_cl,
            relative_path,
            max_merge_files,
        )
        path_results.append(result)
        total_preview_file_count += result["preview_file_count"]
    opened_output = run_p4_scoped_to_filespecs(p4, ["opened", "-c", "default"], target_filespecs)
    opened_paths = parse_depot_paths_from_output(opened_output)
    resolve_outputs: list[str] = []
    resolve_strategy = " ".join(resolve_args)
    if not resolve_args:
        auto_resolve_output = ""
        unresolved_output = ""
        opened_output = run_p4_scoped_to_filespecs(p4, ["opened", "-c", "default"], target_filespecs)
        return {
            "path_results": path_results,
            "total_preview_file_count": total_preview_file_count,
            "auto_resolve_output": auto_resolve_output,
            "unresolved_output": unresolved_output,
            "opened_output": opened_output,
            "resolve_strategy": "not_run",
        }
    if batch_name == "plugins" and opened_paths:
        source_accept_paths, auto_merge_paths = split_plugin_resolve_paths(opened_paths)
        if source_accept_paths:
            resolve_outputs.append(run_p4_scoped_to_filespecs(p4, ["resolve", "-at"], source_accept_paths))
        if auto_merge_paths:
            resolve_outputs.append(run_p4_scoped_to_filespecs(p4, ["resolve", "-am"], auto_merge_paths))
        resolve_strategy = "plugins:mixed(-at .plist/.html, -am other)"
    else:
        resolve_outputs.append(run_p4_scoped_to_filespecs(p4, resolve_args, target_filespecs))
    auto_resolve_output = combine_command_outputs(resolve_outputs)
    unresolved_output = run_p4_scoped_to_filespecs(p4, ["resolve", "-n"], target_filespecs)
    opened_output = run_p4_scoped_to_filespecs(p4, ["opened", "-c", "default"], target_filespecs)
    return {
        "path_results": path_results,
        "total_preview_file_count": total_preview_file_count,
        "auto_resolve_output": auto_resolve_output,
        "unresolved_output": unresolved_output,
        "opened_output": opened_output,
        "resolve_strategy": resolve_strategy,
    }


def perform_single_batch_merge_cycle(
    p4,
    source_stream: str,
    target_stream: str,
    source_cl: int,
    batch_name: str,
    merge_paths: list[MergePath] | None = None,
    max_merge_files: int = 10000,
    progress_callback=None,
) -> dict:
    merge_paths = merge_paths if merge_paths is not None else get_merge_paths_for_batch(batch_name)
    resolve_args = ["resolve", "-at"] if batch_name in SOURCE_ACCEPT_BATCHES else ["resolve", "-am"]
    merge_result = perform_batch_merge_steps(
        p4,
        source_stream,
        target_stream,
        source_cl,
        merge_paths,
        batch_name=batch_name,
        resolve_args=resolve_args,
        max_merge_files=max_merge_files,
        progress_callback=progress_callback,
    )
    unresolved_file_count = count_output_entries(
        merge_result["unresolved_output"],
        ignore_patterns=["no file(s) to resolve", "file(s) not opened"],
    )
    opened_file_count = count_output_entries(
        merge_result["opened_output"],
        ignore_patterns=["file(s) not opened"],
    )
    return {
        "batch": batch_name,
        "merge_paths": merge_paths,
        "path_results": merge_result["path_results"],
        "preview_file_count": merge_result["total_preview_file_count"],
        "auto_resolve_output": merge_result["auto_resolve_output"],
        "unresolved_output": merge_result["unresolved_output"],
        "opened_output": merge_result["opened_output"],
        "resolve_strategy": merge_result["resolve_strategy"],
        "opened_file_count": opened_file_count,
        "unresolved_file_count": unresolved_file_count,
    }


def perform_single_batch_stage_cycle(
    p4,
    source_stream: str,
    target_stream: str,
    source_cl: int,
    batch_name: str,
    merge_paths: list[MergePath] | None = None,
    max_merge_files: int = 10000,
    progress_callback=None,
) -> dict:
    merge_paths = merge_paths if merge_paths is not None else get_merge_paths_for_batch(batch_name)
    merge_result = perform_batch_merge_steps(
        p4,
        source_stream,
        target_stream,
        source_cl,
        merge_paths,
        batch_name=batch_name,
        resolve_args=[],
        max_merge_files=max_merge_files,
        progress_callback=progress_callback,
    )
    opened_file_count = count_output_entries(
        merge_result["opened_output"],
        ignore_patterns=["file(s) not opened"],
    )
    return {
        "batch": batch_name,
        "merge_paths": merge_paths,
        "path_results": merge_result["path_results"],
        "preview_file_count": merge_result["total_preview_file_count"],
        "opened_output": merge_result["opened_output"],
        "opened_file_count": opened_file_count,
    }


def ensure_login(p4) -> None:
    p4.run("login", "-s")


def ensure_client_stream(p4, expected_stream: str) -> str:
    client_spec = p4.run("client", "-o")
    actual_stream = parse_client_stream(client_spec)
    if actual_stream is None:
        raise P4Error("Current P4 client is not a stream client; no Stream: line found in p4 client -o.")
    if actual_stream.lower() != expected_stream.lower():
        raise P4Error(f"Current P4 client stream is {actual_stream}; expected {expected_stream}.")
    return actual_stream


def ensure_default_changelist_is_clean(p4) -> None:
    opened_output = p4.run("opened", "-c", "default").strip()
    if opened_output and "File(s) not opened" not in opened_output:
        raise P4Error(
            "Default changelist already has opened files. "
            "Shelve/revert or submit those files before starting a new merge.\n\n"
            + opened_output
        )


def write_report(run_dir: Path, report: dict, p4: P4Runner | None = None) -> None:
    artifact_support.write_report(
        run_dir,
        report,
        split_merge_path=split_merge_path,
        status_writer=write_status_artifacts,
        command_log_writer=write_command_logs,
        p4=p4,
    )


class MergeSupervisor(DryRunPhase, RunPhase, ResolvePhase, SplitPhase, SanitizePhase, ResolveConflictsPhase, DoctorPhase):
    def __init__(self, args):
        self.args = args
        self.dashboard = LiveDashboard(enabled=bool(getattr(args, "_dashboard_enabled", True)))
        self.dashboard_bridge = getattr(args, "_dashboard_bridge", None)
        self.progress_bridge = getattr(args, "_progress_bridge", None)
        self.watchdog = Watchdog(
            no_progress_seconds=getattr(args, "watchdog_no_progress_seconds", 900),
            check_interval_seconds=1.0,
        )
        self.watchdog.start()

    def _new_run_dir(self) -> Path:
        return Path(self.args.runs_dir) / datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")

    def _base_report(self, phase: str, run_dir: Path) -> dict:
        return {
            "status": "STARTED",
            "phase": phase,
            "source_stream": self.args.source_stream,
            "target_stream": self.args.target_stream,
            "job_tag": self.args.job_tag,
            "run_dir": str(run_dir),
            "p4_cwd": self.args.p4_cwd,
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def _dashboard_update(self, **fields) -> None:
        self.dashboard.update(**fields)
        if self.dashboard_bridge is not None:
            self.dashboard_bridge(fields)
        self.watchdog.update(
            phase=fields.get("phase"),
            step=fields.get("step"),
            target=fields.get("target"),
        )

    def _command_callback(self, args: list[str] | None) -> None:
        self.dashboard.command(args)
        if args and self.dashboard_bridge is not None:
            self.dashboard_bridge({"last_command": " ".join(args)})
        if args and self.progress_bridge is not None:
            self.progress_bridge(f"started {' '.join(args)}")
        if args:
            self.watchdog.update(command=args)

    def _dashboard_command(self, p4: P4Runner | None) -> None:
        if p4 is not None:
            self.dashboard.command(p4.last_command())
            if self.dashboard_bridge is not None and p4.last_command() is not None:
                self.dashboard_bridge({"last_command": " ".join(p4.last_command())})
            self.watchdog.update(command=p4.last_command())

    def _mark_progress(self) -> None:
        self.dashboard.mark_progress()
        if self.progress_bridge is not None:
            self.progress_bridge(None)
        self.watchdog.update()

    def _record_progress(self, note: str) -> None:
        self.dashboard.mark_progress(note)
        if self.progress_bridge is not None:
            self.progress_bridge(note)
        self.watchdog.update()

    def _finish(self, status: str) -> None:
        self.watchdog.stop()
        self.dashboard.finish(status)


def run_dry_run(args) -> int:
    normalize_requested_batches(args)
    return MergeSupervisor(args).run_dry_run()


def run_merge(args) -> int:
    normalize_requested_batches(args)
    return MergeSupervisor(args).run_merge()


def run_resolve(args) -> int:
    return MergeSupervisor(args).run_resolve()


def run_split(args) -> int:
    return MergeSupervisor(args).run_split()


def run_sanitize(args) -> int:
    return MergeSupervisor(args).run_sanitize()


def run_resolve_conflicts(args) -> int:
    return MergeSupervisor(args).run_resolve_conflicts()


def run_doctor(args) -> int:
    return MergeSupervisor(args).run_doctor()


def run_supervise(args) -> int:
    normalize_requested_batches(args)
    if getattr(args, "supervise_resume_change", None) is None and getattr(args, "selected_cl", None) is None:
        raise SystemExit(
            "Fresh supervise runs require --selected-cl. Use --supervise-resume-change when resuming from an existing staged pending changelist."
        )
    from merge_supervisor import SupervisedRunner

    runner = SupervisedRunner(
        args=args,
        supervisor_factory=MergeSupervisor,
        runs_dir=Path(args.runs_dir),
        dashboard_factory=LiveDashboard,
        status_writer=write_status_artifacts,
    )
    return runner.run()


def add_watchdog_arguments(command_parser) -> None:
    command_parser.add_argument(
        "--watchdog-no-progress-seconds",
        type=int,
        default=900,
        help="Treat a long-running phase step as a suspected hang if no real progress is observed for this many seconds.",
    )


def add_run_like_arguments(command_parser, *, selected_cl_required: bool = True) -> None:
    command_parser.add_argument("--source-stream", default=SOURCE_STREAM)
    command_parser.add_argument("--target-stream", default=TARGET_STREAM)
    command_parser.add_argument("--job-tag", default=JOB_TAG)
    command_parser.add_argument(
        "--selected-cl",
        type=int,
        required=selected_cl_required,
        help=(
            "Required fixed source changelist to merge from the selected source stream."
            if selected_cl_required
            else "Fixed source changelist to merge from the selected source stream. Required for fresh run/supervise starts."
        ),
    )
    command_parser.add_argument("--max-changes", type=int, default=500)
    command_parser.add_argument(
        "--batches",
        nargs="+",
        default=DEFAULT_RUN_BATCHES,
        help="Named merge batches or presets. Available: " + available_batch_help_text(),
    )
    command_parser.add_argument(
        "--max-merge-files",
        type=int,
        default=0,
        help="Deprecated; file-count safety stops are disabled.",
    )
    command_parser.add_argument("--runs-dir", default="runs")
    command_parser.add_argument(
        "--p4-cwd",
        default=None,
        help="Directory to run p4 commands from, useful when .p4config lives in a stream workspace.",
    )


def add_resolve_tuning_arguments(command_parser) -> None:
    command_parser.add_argument(
        "--resolve-pass-file-limit",
        type=int,
        default=500,
        help="Maximum files to process in one logical resolve pass within a single staged batch changelist.",
    )
    command_parser.add_argument(
        "--resolve-command-file-limit",
        type=int,
        default=20,
        help="Maximum files to include in one individual p4 resolve command invocation.",
    )


def add_doctor_arguments(command_parser) -> None:
    command_parser.add_argument(
        "--doctor-mode",
        choices=["deterministic", "openai", "ollama"],
        default="deterministic",
        help="Doctor reasoning mode. 'deterministic' uses the local rule scaffold; 'openai' calls the OpenAI Responses provider; 'ollama' calls a local Ollama-compatible generate endpoint.",
    )
    command_parser.add_argument(
        "--doctor-model",
        default=None,
        help="Optional override model name for non-deterministic doctor mode. Defaults to OPENAI_DOCTOR_MODEL/gpt-4.1-mini for openai or OLLAMA_DOCTOR_MODEL/qwen2.5:7b-instruct-q4_K_M for ollama.",
    )
    command_parser.add_argument(
        "--doctor-base-url",
        default=None,
        help="Optional override provider base URL. Defaults to OPENAI_BASE_URL/https://api.openai.com/v1 for openai or OLLAMA_BASE_URL/http://127.0.0.1:11434 for ollama.",
    )
    command_parser.add_argument(
        "--doctor-timeout-seconds",
        type=int,
        default=60,
        help="HTTP timeout in seconds for non-deterministic doctor modes.",
    )
    command_parser.add_argument(
        "--doctor-min-confidence",
        type=float,
        default=0.85,
        help="Minimum confidence required before doctor policy will allow execution.",
    )
    command_parser.add_argument(
        "--doctor-execute-whitelist",
        action="store_true",
        help="Allow doctor to execute supported whitelist recovery actions after policy approval.",
    )
    command_parser.add_argument(
        "--doctor-retry-timeout-seconds",
        type=int,
        default=900,
        help="Timeout in seconds for a policy-approved recovery retry command.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Weekly Perforce merge helper for a source stream into a target stream.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    dry_run = subparsers.add_parser("dry-run", help="Validate P4 state and stop before p4 merge.")
    dry_run.add_argument("--source-stream", default=SOURCE_STREAM)
    dry_run.add_argument("--target-stream", default=TARGET_STREAM)
    dry_run.add_argument("--job-tag", default=JOB_TAG)
    dry_run.add_argument(
        "--selected-cl",
        type=int,
        required=True,
        help="Required fixed source changelist to preview from the selected source stream.",
    )
    dry_run.add_argument("--max-changes", type=int, default=500)
    dry_run.add_argument(
        "--batches",
        nargs="+",
        default=DEFAULT_RUN_BATCHES,
        help="Named merge batches or presets. Available: " + available_batch_help_text(),
    )
    dry_run.add_argument(
        "--max-merge-files",
        type=int,
        default=0,
        help="Deprecated; file-count safety stops are disabled.",
    )
    dry_run.add_argument("--runs-dir", default="runs")
    dry_run.add_argument(
        "--p4-cwd",
        default=None,
        help="Directory to run p4 commands from, useful when .p4config lives in a stream workspace.",
    )
    add_watchdog_arguments(dry_run)
    dry_run.set_defaults(func=run_dry_run)

    run = subparsers.add_parser("run", help="Stage each requested merge batch into its own pending changelist and report results.")
    add_run_like_arguments(run)
    add_watchdog_arguments(run)
    run.set_defaults(func=run_merge)

    resolve = subparsers.add_parser("resolve", help="Run per-batch resolve policy across staged batch changelists from the latest run.")
    resolve.add_argument("--source-stream", default=SOURCE_STREAM)
    resolve.add_argument("--target-stream", default=TARGET_STREAM)
    resolve.add_argument("--job-tag", default=JOB_TAG)
    resolve.add_argument(
        "--change",
        type=int,
        default=None,
        help="Optional staged batch changelist to resolve. Defaults to all eligible batch changelists from the latest run.",
    )
    resolve.add_argument("--runs-dir", default="runs")
    resolve.add_argument(
        "--p4-cwd",
        default=None,
        help="Directory to run p4 commands from, useful when .p4config lives in a stream workspace.",
    )
    add_watchdog_arguments(resolve)
    add_resolve_tuning_arguments(resolve)
    resolve.set_defaults(func=run_resolve)

    supervise = subparsers.add_parser(
        "supervise",
        help="Run a supervised end-to-end loop that chains run, resolve, doctor, and allowed retries.",
    )
    add_run_like_arguments(supervise, selected_cl_required=False)
    add_watchdog_arguments(supervise)
    add_resolve_tuning_arguments(supervise)
    add_doctor_arguments(supervise)
    supervise.add_argument(
        "--supervise-max-run-attempts",
        type=int,
        default=3,
        help="Maximum run attempts before the supervise loop stops.",
    )
    supervise.add_argument(
        "--supervise-max-doctor-cycles",
        type=int,
        default=0,
        help="Maximum doctor cycles allowed between supervised run attempts. Use 0 for no fixed cap.",
    )
    supervise.add_argument(
        "--supervise-resume-change",
        type=int,
        default=None,
        help="Resume supervise from an existing staged pending changelist instead of starting with a fresh run.",
    )
    supervise.add_argument(
        "--supervise-resume-from",
        choices=["auto", "run-recovery", "resolve", "split", "sanitize", "resolve-conflicts"],
        default="auto",
        help="Resume phase selection for --supervise-resume-change. 'auto' uses the latest available artifacts for that staged CL.",
    )
    supervise.add_argument(
        "--supervise-resume-unresolved-file",
        default=None,
        help="Optional saved resolve -n output file to use when resuming from a manually recovered blocked run into split.",
    )
    supervise.set_defaults(func=run_supervise)

    split = subparsers.add_parser("split", help="Split a staged merge CL into smaller review and special-case changelists.")
    split.add_argument("--source-stream", default=SOURCE_STREAM)
    split.add_argument("--target-stream", default=TARGET_STREAM)
    split.add_argument("--job-tag", default=JOB_TAG)
    split.add_argument("--change", type=int, default=None, help="Numbered pending changelist produced by run. Defaults to latest eligible run status.")
    split.add_argument(
        "--allow-recovered-blocked-run",
        action="store_true",
        help="One-time recovery override: allow split to continue from a manually checkpointed changelist after a blocked run.",
    )
    split.add_argument(
        "--unresolved-file",
        default=None,
        help="Optional path to a saved resolve -n output file when using a recovered blocked run override.",
    )
    split.add_argument("--runs-dir", default="runs")
    split.add_argument(
        "--p4-cwd",
        default=None,
        help="Directory to run p4 commands from, useful when .p4config lives in a stream workspace.",
    )
    add_watchdog_arguments(split)
    split.set_defaults(func=run_split)

    sanitize = subparsers.add_parser("sanitize", help="Apply safe cleanup to resolved batch changelists without making content decisions.")
    sanitize.add_argument("--source-stream", default=SOURCE_STREAM)
    sanitize.add_argument("--target-stream", default=TARGET_STREAM)
    sanitize.add_argument("--job-tag", default=JOB_TAG)
    sanitize.add_argument("--change", type=int, default=None, help="Optional changelist to scope sanitize lookup. Defaults to the latest eligible resolve or split summary.")
    sanitize.add_argument("--runs-dir", default="runs")
    sanitize.add_argument(
        "--p4-cwd",
        default=None,
        help="Directory to run p4 commands from, useful when .p4config lives in a stream workspace.",
    )
    add_watchdog_arguments(sanitize)
    sanitize.set_defaults(func=run_sanitize)

    resolve_conflicts = subparsers.add_parser(
        "resolve-conflicts",
        help="Apply whitelisted conflict-resolution policies to sanitized conflict buckets.",
    )
    resolve_conflicts.add_argument("--source-stream", default=SOURCE_STREAM)
    resolve_conflicts.add_argument("--target-stream", default=TARGET_STREAM)
    resolve_conflicts.add_argument("--job-tag", default=JOB_TAG)
    resolve_conflicts.add_argument(
        "--change",
        type=int,
        default=None,
        help="Optional changelist to scope conflict-resolution lookup. Defaults to the latest eligible sanitize summary.",
    )
    resolve_conflicts.add_argument("--runs-dir", default="runs")
    resolve_conflicts.add_argument(
        "--p4-cwd",
        default=None,
        help="Directory to run p4 commands from, useful when .p4config lives in a stream workspace.",
    )
    add_watchdog_arguments(resolve_conflicts)
    resolve_conflicts.set_defaults(func=run_resolve_conflicts)

    doctor = subparsers.add_parser("doctor", help="Diagnose a blocked phase artifact and recommend the next safe recovery action.")
    doctor.add_argument("--source-stream", default=SOURCE_STREAM)
    doctor.add_argument("--target-stream", default=TARGET_STREAM)
    doctor.add_argument("--job-tag", default=JOB_TAG)
    doctor.add_argument("--change", type=int, default=None, help="Optional staged pending changelist to locate the matching blocked artifact.")
    doctor.add_argument("--runs-dir", default="runs")
    doctor.add_argument(
        "--p4-cwd",
        default=None,
        help="Directory to run p4 commands from, useful when .p4config lives in a stream workspace.",
    )
    add_doctor_arguments(doctor)
    doctor.set_defaults(func=run_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())



