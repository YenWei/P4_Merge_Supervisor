from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNED_FILES = [
    REPO_ROOT / "p4_weekly_merge.py",
    *sorted(path for path in (REPO_ROOT / "tests").glob("*.py") if path.name != "test_public_sanitization.py"),
]
BANNED_SNIPPETS = [
    "Sil" + "ver Palace",
    "TA" + " stream",
    "TA" + "_Stream",
    "DEV" + "/TD branch",
    "TD" + "_Demo",
    "Ani" + "_TA_Demo",
    "S:" + "\\yen" + "wei.lim" + "_TA" + "_Stream",
    "//" + "Sil" + "ver/",
]


class PublicSanitizationTests(unittest.TestCase):
    def test_repo_snapshot_does_not_contain_internal_project_identifiers(self) -> None:
        hits: list[str] = []

        for file_path in SCANNED_FILES:
            content = file_path.read_text(encoding="utf-8")
            for snippet in BANNED_SNIPPETS:
                if snippet in content:
                    hits.append(f"{file_path.name}: {snippet}")

        self.assertEqual([], hits)


if __name__ == "__main__":
    unittest.main()



