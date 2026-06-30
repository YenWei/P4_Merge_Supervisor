from __future__ import annotations

import re


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def chunked_by_command_length(
    values: list[str],
    fixed_args: list[str],
    max_chars: int = 7000,
    max_items: int = 50,
) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = len(" ".join(fixed_args))
    for value in values:
        value_len = len(value) + 1
        if current and (len(current) >= max_items or current_len + value_len > max_chars):
            chunks.append(current)
            current = []
            current_len = len(" ".join(fixed_args))
        current.append(value)
        current_len += value_len
    if current:
        chunks.append(current)
    return chunks


def build_target_filespecs(target_stream: str, relative_paths: list[str | tuple[str, str]], split_merge_path) -> list[str]:
    filespecs = []
    seen = set()
    for relative_path in relative_paths:
        _, target_path, _ = split_merge_path(relative_path)
        filespec = f"{target_stream}/{target_path}"
        if filespec in seen:
            continue
        seen.add(filespec)
        filespecs.append(filespec)
    return filespecs


def combine_command_outputs(outputs: list[str]) -> str:
    combined = [output.strip() for output in outputs if output and output.strip()]
    return "\n".join(combined)


def count_preview_files(preview_output: str) -> int:
    return sum(1 for line in preview_output.splitlines() if line.startswith("//"))


def run_p4_scoped_to_filespecs(p4, command_args: list[str], filespecs: list[str]) -> str:
    if not filespecs:
        return ""
    safe_chunks = chunked_by_command_length(
        filespecs,
        fixed_args=[p4.executable, *command_args],
        max_chars=7000,
        max_items=50,
    )
    outputs = [p4.run(*command_args, *chunk) for chunk in safe_chunks]
    return combine_command_outputs(outputs)


def count_output_entries(output: str, ignore_patterns: list[str] | None = None) -> int:
    ignore_patterns = ignore_patterns or []
    count = 0
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(pattern in stripped for pattern in ignore_patterns):
            continue
        count += 1
    return count


def parse_depot_paths_from_output(output: str) -> list[str]:
    paths = []
    for line in output.splitlines():
        match = re.search(r"(//[^\s#]+)", line)
        if match:
            paths.append(match.group(1))
    return paths
