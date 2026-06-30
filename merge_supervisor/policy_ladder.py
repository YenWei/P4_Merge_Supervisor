from __future__ import annotations

from dataclasses import asdict, dataclass


_POLICY_LEVELS = {"auto-approved", "candidate", "shadow-validated", "human-only"}


@dataclass
class PolicyObservation:
    phase: str
    batch: str
    path_family: str
    filetype: str
    blocker_type: str
    suggested_action: str
    policy_level: str
    matched_human_action: str | None = None
    resumed_cleanly: bool | None = None
    source: str | None = None

    def to_report_dict(self) -> dict:
        return asdict(self)


@dataclass
class PolicyPromotionCandidate:
    phase: str
    batch: str
    path_family: str
    filetype: str
    blocker_type: str
    suggested_action: str
    policy_level: str
    matched_clean_resumes: int

    def to_report_dict(self) -> dict:
        return asdict(self)


class PolicyLadder:
    def __init__(self) -> None:
        self._patterns: dict[tuple[str, str, str, str, str, str], dict[str, int]] = {}

    def _normalize_pattern(self, pattern: dict) -> dict[str, str]:
        return {
            "phase": str(pattern.get("phase") or "unknown"),
            "batch": str(pattern.get("batch") or "unknown"),
            "path_family": str(pattern.get("path_family") or "unknown"),
            "filetype": str(pattern.get("filetype") or "unknown"),
            "blocker_type": str(pattern.get("blocker_type") or "unknown"),
            "suggested_action": str(pattern.get("suggested_action") or "unknown"),
        }

    def _key(self, **pattern: str) -> tuple[str, str, str, str, str, str]:
        normalized = self._normalize_pattern(pattern)
        return (
            normalized["phase"],
            normalized["batch"],
            normalized["path_family"],
            normalized["filetype"],
            normalized["blocker_type"],
            normalized["suggested_action"],
        )

    def classify_pattern(self, **pattern: str) -> str:
        normalized = self._normalize_pattern(pattern)
        if normalized["suggested_action"] != "accept_source":
            return "human-only"
        counters = self._patterns.get(self._key(**normalized))
        if counters is None:
            return "candidate"
        if counters.get("matched_clean_resumes", 0) >= 3:
            return "shadow-validated"
        return "candidate"

    def record_human_outcome(self, pattern: dict, *, human_action: str, resumed_cleanly: bool) -> None:
        normalized = self._normalize_pattern(pattern)
        counters = self._patterns.setdefault(
            self._key(**normalized),
            {"matched_clean_resumes": 0, "observed_human_matches": 0, "observed_total": 0},
        )
        counters["observed_total"] += 1
        if human_action == normalized["suggested_action"]:
            counters["observed_human_matches"] += 1
            if resumed_cleanly:
                counters["matched_clean_resumes"] += 1

    def build_observation(
        self,
        pattern: dict,
        *,
        policy_level: str,
        matched_human_action: str | None = None,
        resumed_cleanly: bool | None = None,
        source: str | None = None,
    ) -> dict:
        if policy_level not in _POLICY_LEVELS:
            policy_level = self.classify_pattern(**pattern)
        return PolicyObservation(
            policy_level=policy_level,
            matched_human_action=matched_human_action,
            resumed_cleanly=resumed_cleanly,
            source=source,
            **self._normalize_pattern(pattern),
        ).to_report_dict()

    def build_promotion_candidate(self, pattern: dict) -> dict | None:
        normalized = self._normalize_pattern(pattern)
        key = self._key(**normalized)
        counters = self._patterns.get(key)
        if not counters:
            return None
        matched_clean_resumes = counters.get("matched_clean_resumes", 0)
        if matched_clean_resumes < 3:
            return None
        return PolicyPromotionCandidate(
            policy_level="shadow-validated",
            matched_clean_resumes=matched_clean_resumes,
            **normalized,
        ).to_report_dict()
