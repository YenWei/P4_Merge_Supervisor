from __future__ import annotations

from dataclasses import asdict
import json
import os
from typing import Mapping, Protocol
from urllib import error as urllib_error
from urllib import request as urllib_request

from .doctor_models import DoctorBlockedCase, DoctorLLMDecision
from .runtime_models import BlockedCaseSnapshot


ALLOWED_DOCTOR_ACTIONS = (
    "retry_after_login_refresh",
    "retry_after_connectivity_restore",
    "retry_after_env_restore",
    "retry_resolve_with_charset_override",
    "kill_and_retry_same_phase_after_hang",
    "isolate_conflicted_files_and_continue",
    "pause_cleanly",
)


class DoctorProviderError(RuntimeError):
    pass


class DoctorProvider(Protocol):
    def diagnose(self, blocked_case: DoctorBlockedCase) -> DoctorLLMDecision:
        ...


class StubDoctorProvider:
    """
    First LLM scaffold provider.

    This exists to lock the contract before a real OpenAI-backed provider is wired in.
    It intentionally does not call any external service yet.
    """

    def diagnose(self, blocked_case: DoctorBlockedCase) -> DoctorLLMDecision:
        raise DoctorProviderError(
            "No LLM doctor provider is configured yet. Use deterministic doctor mode or wire a real provider implementation."
        )


def _default_verification_plan(recommended_action: str, resume_from_phase: str | None) -> dict:
    if recommended_action == "retry_resolve_with_charset_override":
        return {
            "strategy_id": "verify_charset_retry",
            "requires_verification": True,
            "resume_from_phase": resume_from_phase,
            "checks": [
                "rerun resolve against the preserved staged change",
                "confirm unresolved file count does not increase",
            ],
        }
    if recommended_action == "isolate_conflicted_files_and_continue":
        return {
            "strategy_id": "verify_conflict_isolation",
            "requires_verification": True,
            "resume_from_phase": resume_from_phase,
            "checks": [
                "confirm unresolved files moved to the isolated conflict changelist",
                "confirm the preserved staged change is still resumable",
            ],
        }
    if recommended_action == "pause_cleanly":
        return {
            "strategy_id": "manual_review_handoff",
            "requires_verification": False,
            "resume_from_phase": resume_from_phase,
            "checks": ["human reviews the blocked artifacts before resuming"],
        }
    return {
        "strategy_id": "verify_recovery_before_resume",
        "requires_verification": True,
        "resume_from_phase": resume_from_phase,
        "checks": ["confirm the recovery action produced a clean resumable state"],
    }


def build_system_prompt() -> str:
    return (
        "You are the recovery diagnosis layer for a supervised Perforce merge workflow.\n\n"
        "Your job:\n"
        "- read a blocked workflow case\n"
        "- identify the most likely failure type\n"
        "- choose exactly one action from the allowed action list\n"
        "- set recovery_primitive_id to the exact primitive that automation would execute\n"
        "- set safe_to_execute to false when the workspace or artifact state is unsafe for automation\n"
        "- emit a small verification_plan describing how the recovery should be checked before resume\n"
        "- prefer pause_cleanly when the case is ambiguous or unsafe\n"
        "- never invent new actions\n"
        "- never recommend submit, client remap, stream remap, content conflict resolution, or broad revert\n\n"
        "Return only a JSON object that matches the required schema."
    )


def build_user_prompt(blocked_case: DoctorBlockedCase) -> str:
    payload = {
        "blocked_case_snapshot": BlockedCaseSnapshot.from_blocked_case(blocked_case).to_report_dict(),
        "blocked_case": blocked_case.to_prompt_payload(),
        "allowed_actions": list(blocked_case.allowed_actions),
        "required_output_schema": {
            "failure_type": "string",
            "confidence": "number between 0.0 and 1.0",
            "recommended_action": "one allowed action",
            "reasoning_summary": "short string",
            "safe_to_resume": "boolean",
            "resume_from_phase": "string or null",
            "needs_human_review": "boolean",
            "recovery_primitive_id": "one allowed action",
            "safe_to_execute": "boolean",
            "verification_plan": {
                "strategy_id": "string",
                "requires_verification": "boolean",
                "resume_from_phase": "string or null",
                "checks": ["string"],
            },
        },
    }
    return json.dumps(payload, indent=2)


def build_response_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "failure_type": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "recommended_action": {"type": "string", "enum": list(ALLOWED_DOCTOR_ACTIONS)},
            "reasoning_summary": {"type": "string"},
            "safe_to_resume": {"type": "boolean"},
            "resume_from_phase": {"type": ["string", "null"]},
            "needs_human_review": {"type": "boolean"},
            "recovery_primitive_id": {"type": "string", "enum": list(ALLOWED_DOCTOR_ACTIONS)},
            "safe_to_execute": {"type": "boolean"},
            "verification_plan": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "strategy_id": {"type": "string"},
                    "requires_verification": {"type": "boolean"},
                    "resume_from_phase": {"type": ["string", "null"]},
                    "checks": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["strategy_id", "requires_verification", "resume_from_phase", "checks"],
            },
        },
        "required": [
            "failure_type",
            "confidence",
            "recommended_action",
            "reasoning_summary",
            "safe_to_resume",
            "resume_from_phase",
            "needs_human_review",
            "recovery_primitive_id",
            "safe_to_execute",
            "verification_plan",
        ],
    }


class OpenAIDoctorProvider:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int = 60,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.model = model or os.environ.get("OPENAI_DOCTOR_MODEL") or "gpt-4.1-mini"
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.timeout_seconds = timeout_seconds

        if not self.api_key:
            raise DoctorProviderError("OPENAI_API_KEY is not set for doctor openai mode.")

    def diagnose(self, blocked_case: DoctorBlockedCase) -> DoctorLLMDecision:
        body = {
            "model": self.model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": build_system_prompt()}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": build_user_prompt(blocked_case)}],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "doctor_decision",
                    "strict": True,
                    "schema": build_response_schema(),
                }
            },
        }
        request = urllib_request.Request(
            url=f"{self.base_url}/responses",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib_request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise DoctorProviderError(f"OpenAI doctor request failed with HTTP {error.code}: {detail}") from error
        except urllib_error.URLError as error:
            raise DoctorProviderError(f"OpenAI doctor request failed: {error}") from error

        raw_json = extract_response_json(payload)
        return validate_llm_decision(raw_json, default_resume_from_phase=blocked_case.resume_from_phase)


class OllamaDoctorProvider:
    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int = 60,
    ):
        self.model = model or os.environ.get("OLLAMA_DOCTOR_MODEL") or "qwen2.5:7b-instruct-q4_K_M"
        self.base_url = (base_url or os.environ.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")
        self.timeout_seconds = timeout_seconds

    def diagnose(self, blocked_case: DoctorBlockedCase) -> DoctorLLMDecision:
        prompt = (
            f"{build_system_prompt()}\n\n"
            "Return only a JSON object matching this schema:\n"
            f"{json.dumps(build_response_schema(), indent=2)}\n\n"
            "Blocked workflow case:\n"
            f"{build_user_prompt(blocked_case)}"
        )
        body = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.0,
            },
        }
        request = urllib_request.Request(
            url=f"{self.base_url}/api/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib_request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise DoctorProviderError(f"Ollama doctor request failed with HTTP {error.code}: {detail}") from error
        except urllib_error.URLError as error:
            raise DoctorProviderError(f"Ollama doctor request failed: {error}") from error

        raw_json = extract_ollama_response_json(payload)
        return validate_llm_decision(raw_json, default_resume_from_phase=blocked_case.resume_from_phase)


def extract_response_json(payload: dict) -> dict:
    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return json.loads(payload["output_text"])

    for output_item in payload.get("output", []):
        for content_item in output_item.get("content", []):
            text = content_item.get("text")
            if isinstance(text, str) and text.strip():
                return json.loads(text)

    raise DoctorProviderError("OpenAI doctor response did not contain parseable JSON text.")


def _extract_json_object_from_text(text: str) -> dict:
    candidate = text.strip()
    if not candidate:
        raise DoctorProviderError("LLM doctor response text was empty.")

    fenced = candidate
    if fenced.startswith("```"):
        lines = fenced.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(candidate[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise DoctorProviderError("LLM doctor response did not contain a parseable JSON object.")


def extract_ollama_response_json(payload: dict) -> dict:
    response_text = payload.get("response")
    if isinstance(response_text, str) and response_text.strip():
        return _extract_json_object_from_text(response_text)
    raise DoctorProviderError("Ollama doctor response did not contain parseable JSON text.")


def _validated_verification_plan(raw_plan: object, *, recommended_action: str, resume_from_phase: str | None) -> dict:
    default_plan = _default_verification_plan(recommended_action, resume_from_phase)
    if not isinstance(raw_plan, Mapping):
        return default_plan

    strategy_id = raw_plan.get("strategy_id")
    requires_verification = raw_plan.get("requires_verification")
    plan_resume_phase = raw_plan.get("resume_from_phase", resume_from_phase)
    checks = raw_plan.get("checks")

    if not isinstance(strategy_id, str) or not strategy_id.strip():
        raise DoctorProviderError("LLM doctor verification_plan.strategy_id must be a non-empty string.")
    if not isinstance(requires_verification, bool):
        raise DoctorProviderError("LLM doctor verification_plan.requires_verification must be a boolean.")
    if plan_resume_phase is not None and not isinstance(plan_resume_phase, str):
        raise DoctorProviderError("LLM doctor verification_plan.resume_from_phase must be a string or null.")
    if not isinstance(checks, list) or any(not isinstance(item, str) or not item.strip() for item in checks):
        raise DoctorProviderError("LLM doctor verification_plan.checks must be a list of non-empty strings.")

    return {
        "strategy_id": strategy_id.strip(),
        "requires_verification": requires_verification,
        "resume_from_phase": plan_resume_phase,
        "checks": [item.strip() for item in checks],
    }


def validate_llm_decision(raw_decision: dict, *, default_resume_from_phase: str | None) -> DoctorLLMDecision:
    if not isinstance(raw_decision, dict):
        raise DoctorProviderError("LLM doctor output must be a JSON object.")

    recommended_action = raw_decision.get("recommended_action")
    if recommended_action not in ALLOWED_DOCTOR_ACTIONS:
        raise DoctorProviderError(f"LLM doctor returned unsupported action: {recommended_action!r}")

    confidence = raw_decision.get("confidence")
    if not isinstance(confidence, (int, float)):
        raise DoctorProviderError("LLM doctor confidence must be numeric.")
    confidence_value = float(confidence)
    if not 0.0 <= confidence_value <= 1.0:
        raise DoctorProviderError("LLM doctor confidence must be between 0.0 and 1.0.")

    failure_type = raw_decision.get("failure_type")
    if not isinstance(failure_type, str) or not failure_type.strip():
        raise DoctorProviderError("LLM doctor failure_type must be a non-empty string.")

    reasoning_summary = raw_decision.get("reasoning_summary")
    if not isinstance(reasoning_summary, str) or not reasoning_summary.strip():
        raise DoctorProviderError("LLM doctor reasoning_summary must be a non-empty string.")

    resume_from_phase = raw_decision.get("resume_from_phase", default_resume_from_phase)
    if resume_from_phase is not None and not isinstance(resume_from_phase, str):
        raise DoctorProviderError("LLM doctor resume_from_phase must be a string or null.")

    safe_to_resume = raw_decision.get("safe_to_resume")
    if not isinstance(safe_to_resume, bool):
        raise DoctorProviderError("LLM doctor safe_to_resume must be a boolean.")

    needs_human_review = raw_decision.get("needs_human_review")
    if not isinstance(needs_human_review, bool):
        raise DoctorProviderError("LLM doctor needs_human_review must be a boolean.")

    recovery_primitive_id = raw_decision.get("recovery_primitive_id", recommended_action)
    if recovery_primitive_id not in ALLOWED_DOCTOR_ACTIONS:
        raise DoctorProviderError(f"LLM doctor recovery_primitive_id must be one of the allowed actions, got {recovery_primitive_id!r}.")

    safe_to_execute = raw_decision.get("safe_to_execute")
    if safe_to_execute is None:
        safe_to_execute = recommended_action != "pause_cleanly" and not needs_human_review and safe_to_resume
    if not isinstance(safe_to_execute, bool):
        raise DoctorProviderError("LLM doctor safe_to_execute must be a boolean.")

    verification_plan = _validated_verification_plan(
        raw_decision.get("verification_plan"),
        recommended_action=recovery_primitive_id,
        resume_from_phase=resume_from_phase,
    )

    prior_attempts = raw_decision.get("prior_attempts", [])
    if not isinstance(prior_attempts, list) or any(not isinstance(item, dict) for item in prior_attempts):
        raise DoctorProviderError("LLM doctor prior_attempts must be a list of objects when provided.")

    return DoctorLLMDecision(
        failure_type=failure_type.strip(),
        confidence=confidence_value,
        recommended_action=recommended_action,
        reasoning_summary=reasoning_summary.strip(),
        safe_to_resume=safe_to_resume,
        resume_from_phase=resume_from_phase,
        needs_human_review=needs_human_review,
        recovery_primitive_id=recovery_primitive_id,
        safe_to_execute=safe_to_execute,
        verification_plan=verification_plan,
        prior_attempts=[dict(item) for item in prior_attempts],
    )


def llm_decision_to_debug_dict(decision: DoctorLLMDecision) -> dict:
    return asdict(decision)
