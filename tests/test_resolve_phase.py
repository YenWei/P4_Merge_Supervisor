from __future__ import annotations

import unittest

import p4_weekly_merge as core
from merge_phases.resolve_phase import ResolvePhase


class ResolvePhaseHelperTests(unittest.TestCase):
    def test_split_plugin_resolve_paths_routes_manual_review_allowlist(self) -> None:
        source_accept_paths, manual_review_paths = core.split_plugin_resolve_paths(
            [
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Binaries/Win64/Foo.dll",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Source/Foo/Private/Foo.cpp",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Source/Foo/Public/Foo.h",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Source/Foo/Public/Foo.hpp",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Source/Foo/Private/Foo.c",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Source/Foo/Private/Foo.cc",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Source/Foo/Public/Foo.inl",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Source/Foo/Foo.Build.cs",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Source/Foo/Foo.cs",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Config/DefaultFoo.ini",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Foo.uplugin",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Resources/Icon128.png",
            ]
        )

        self.assertEqual(
            source_accept_paths,
            [
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Binaries/Win64/Foo.dll",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Resources/Icon128.png",
            ],
        )
        self.assertEqual(
            manual_review_paths,
            [
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Source/Foo/Private/Foo.cpp",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Source/Foo/Public/Foo.h",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Source/Foo/Public/Foo.hpp",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Source/Foo/Private/Foo.c",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Source/Foo/Private/Foo.cc",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Source/Foo/Public/Foo.inl",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Source/Foo/Foo.Build.cs",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Source/Foo/Foo.cs",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Config/DefaultFoo.ini",
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Foo.uplugin",
            ],
        )

    def test_should_skip_review_bucket_for_source_accept_batches(self) -> None:
        self.assertTrue(core.should_skip_review_bucket_for_batch("external-actors"))
        self.assertTrue(core.should_skip_review_bucket_for_batch("artres"))
        self.assertFalse(core.should_skip_review_bucket_for_batch("plugins"))
        self.assertFalse(core.should_skip_review_bucket_for_batch("project-tools"))

    def test_should_auto_accept_plugin_autoaccept_conflict_bucket(self) -> None:
        self.assertTrue(core.should_auto_accept_conflict_bucket("conflict-plugins-autoaccept-unresolved"))
        self.assertFalse(core.should_auto_accept_conflict_bucket("conflict-plugins-review-unresolved"))

    def test_classify_resolve_failure_prefers_charset_for_translation_errors(self) -> None:
        result = core.classify_resolve_failure(
            r"Translation of file content failed near line 5 file S:\\ws\\Project\\Tool\\Foo.rs",
            ["p4", "resolve", "-n"],
        )

        self.assertEqual(result["blocker_category"], "resolve_charset")
        self.assertTrue(result["retryable"])
        self.assertIn("charset recovery", result["next_action"].lower())

    def test_extract_policy_observation_from_plugin_binary_path(self) -> None:
        observation = ResolvePhase._build_policy_observation(
            batch_name="plugins",
            blocker_type="resolve_failed",
            unresolved_targets=[
                "//ExampleDepot/Release_Target/Project/Plugins/Foo/Binaries/Win64/UnrealEditor.modules"
            ],
        )

        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation["suggested_action"], "accept_source")
        self.assertEqual(observation["path_family"], "Project/Plugins/Foo/Binaries/...")
        self.assertEqual(observation["filetype"], "binary")

    def test_extract_policy_observation_returns_none_for_non_plugin_paths(self) -> None:
        observation = ResolvePhase._build_policy_observation(
            batch_name="engine",
            blocker_type="resolve_failed",
            unresolved_targets=["//ExampleDepot/Release_Target/Engine/Source/Foo.cpp"],
        )

        self.assertIsNone(observation)
    def test_parse_tampered_local_paths_keeps_unique_order(self) -> None:
        text = r"""
S:\ws\Project\Foo\A.modules tampered with before resolve - edit or revert.
ignored line
S:\ws\Project\Foo\A.modules tampered with before resolve - edit or revert.
S:\ws\Project\Foo\B.modules tampered with before resolve - edit or revert.
"""
        self.assertEqual(
            ResolvePhase._parse_tampered_local_paths(text),
            [
                r"S:\ws\Project\Foo\A.modules",
                r"S:\ws\Project\Foo\B.modules",
            ],
        )

    def test_tolerable_resolve_error_accepts_tampered_and_no_files_lines(self) -> None:
        text = r"""
S:\ws\Project\Foo\A.modules tampered with before resolve - edit or revert.
//depot/file - no file(s) to resolve.
"""
        self.assertTrue(ResolvePhase._is_tolerable_resolve_error(text))

    def test_tolerable_resolve_error_rejects_unexpected_errors(self) -> None:
        self.assertFalse(ResolvePhase._is_tolerable_resolve_error("Perforce client error: TCP connect failed"))

    def test_dedupe_paths_preserves_first_occurrence(self) -> None:
        self.assertEqual(
            ResolvePhase._dedupe_paths(["//a", "//b", "//a", r"S:\x", r"S:\x"]),
            ["//a", "//b", r"S:\x"],
        )

    def test_build_passes_returns_single_pass_when_under_limit(self) -> None:
        class FakeCore:
            @staticmethod
            def chunked(values, size):
                return [values[index:index + size] for index in range(0, len(values), size)]

        values = [f"//depot/{index}" for index in range(12)]
        self.assertEqual(ResolvePhase._build_passes(FakeCore, values, 20), [values])

    def test_build_passes_splits_large_batches_by_limit(self) -> None:
        class FakeCore:
            @staticmethod
            def chunked(values, size):
                return [values[index:index + size] for index in range(0, len(values), size)]

        values = [f"//depot/{index}" for index in range(11)]
        self.assertEqual(
            ResolvePhase._build_passes(FakeCore, values, 4),
            [values[0:4], values[4:8], values[8:11]],
        )


if __name__ == "__main__":
    unittest.main()



