"""Repair tests: the path jail, the test gate, snapshot/restore and the revert rule.

The gate is narrowed to ``test_config`` throughout: it is the module that covers the file
these tests edit, it takes ~0.1 s, and it keeps the outer suite fast and deterministic.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import unittest
from typing import Any, Dict, Optional

from harness import doctor, repair

from .support import ROOT, TempDirTestCase

GOOD_MODEL_LINE = 'DEFAULT_MODEL = "deepseek-chat"'
BROKEN_MODEL_LINE = 'DEFAULT_MODEL = "broken-model"'
GATE = ("test_config",)


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RepairTestCase(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.path("copy")
        shutil.copytree(
            ROOT,
            self.root,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".scratch"),
        )
        self.backups = self.path("backups")

    def source(self, relative: str = "harness/config.py") -> str:
        with open(os.path.join(self.root, relative), encoding="utf-8") as handle:
            return handle.read()

    def edit(self, new_source: str, relative: str = "harness/config.py", **kwargs: Any) -> repair.RepairResult:
        kwargs.setdefault("test_modules", GATE)
        return repair.apply_source_edit(
            relative, new_source, "test edit", root=self.root, backups_dir=self.backups, **kwargs
        )

    def patch(self, old: str, new: str, relative: str = "harness/config.py", **kwargs: Any) -> repair.RepairResult:
        kwargs.setdefault("test_modules", GATE)
        return repair.apply_source_patch(
            relative, old, new, "test patch", root=self.root, backups_dir=self.backups, **kwargs
        )


class TestSnapshot(RepairTestCase):
    def test_snapshot_copies_the_runtime_and_hashes_it(self) -> None:
        result = repair.snapshot("unit", root=self.root, backups_dir=self.backups)
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.backup_id)
        manifest_path = os.path.join(self.backups, result.backup_id, "manifest.json")
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertIn("harness/config.py", manifest["files"])
        self.assertIn("run.py", manifest["files"])
        self.assertNotIn("tests/test_config.py", manifest["files"], "tests are not part of a snapshot")
        copied = os.path.join(self.backups, result.backup_id, "harness", "config.py")
        self.assertEqual(manifest["files"]["harness/config.py"]["sha256"], sha256(copied))
        self.assertEqual(manifest["files"]["harness/config.py"]["sha256"], sha256(os.path.join(self.root, "harness", "config.py")))

    def test_snapshot_reports_cannot_self_repair_on_a_read_only_root(self) -> None:
        os.chmod(self.root, 0o555)
        self.addCleanup(os.chmod, self.root, 0o755)
        if os.geteuid() == 0:  # pragma: no cover - root ignores the mode
            self.skipTest("running as root: a read-only directory is still writable")
        result = repair.snapshot("unit", root=self.root, backups_dir=self.backups)
        self.assertFalse(result.ok)
        self.assertEqual(result.decision, "cannot-self-repair")
        self.assertIn("not writable", result.reason)

    def test_list_backups_is_newest_first_and_reports_a_bad_manifest(self) -> None:
        first = repair.snapshot("one", root=self.root, backups_dir=self.backups)
        second = repair.snapshot("two", root=self.root, backups_dir=self.backups)
        os.makedirs(os.path.join(self.backups, "20300101-000000-ghost"))
        with open(os.path.join(self.backups, "20300101-000000-ghost", "manifest.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        entries = repair.list_backups(backups_dir=self.backups)
        ids = [entry["id"] for entry in entries]
        self.assertIn(first.backup_id, ids)
        self.assertIn(second.backup_id, ids)
        ghost = [entry for entry in entries if entry["id"] == "20300101-000000-ghost"][0]
        self.assertIn("error", ghost)
        self.assertEqual(entries[0]["id"], second.backup_id, "newest first by created_at")

    def test_no_secret_reaches_a_backup(self) -> None:
        canary = "sk-canary-abcdefghijklmnop0123456789"
        config_path = os.path.join(self.root, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"api_key": canary}, handle)
        result = repair.snapshot("leak-check", root=self.root, backups_dir=self.backups)
        directory = os.path.join(self.backups, result.backup_id)
        for current, _dirs, files in os.walk(directory):
            for name in files:
                with open(os.path.join(current, name), "r", encoding="utf-8", errors="replace") as handle:
                    self.assertNotIn(canary, handle.read(), "{} leaked a secret".format(name))
        self.assertFalse(os.path.exists(os.path.join(directory, "config.json")))


class TestPathJail(RepairTestCase):
    def test_refused_paths(self) -> None:
        cases = {
            "../evil.py": "..",
            "/etc/passwd": "absolute",
            "harness/../../x.py": "refused",
            "tests/test_config.py": "not writable",
            "harness/repair.py": "snapshot/restore gate",
            ".git/config": "not writable",
            "harness/notes.txt": "only .py",
            "examples/demo.py": "only run.py",
            "": "required",
        }
        for path, needle in cases.items():
            with self.subTest(path=path):
                result = self.edit(self.source(), relative=path)
                self.assertFalse(result.ok)
                self.assertEqual(result.decision, "refused")
                self.assertIn(needle, result.reason)

    def test_a_symlinked_file_is_refused(self) -> None:
        target = self.path("outside.py")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("X = 1\n")
        link = os.path.join(self.root, "harness", "linked.py")
        os.symlink(target, link)
        result = self.edit("X = 2\n", relative="harness/linked.py")
        self.assertFalse(result.ok)
        self.assertIn("symlink", result.reason)

    def test_a_symlinked_directory_is_refused(self) -> None:
        outside = self.path("outside-dir")
        os.makedirs(outside)
        link = os.path.join(self.root, "harness", "linked-dir")
        os.symlink(outside, link)
        result = self.edit("X = 2\n", relative="harness/linked-dir/mod.py")
        self.assertFalse(result.ok)
        self.assertIn("symlink", result.reason)

    def test_read_source_uses_the_same_jail(self) -> None:
        ok = repair.read_source("harness/errors.py", root=self.root)
        self.assertTrue(ok.ok)
        self.assertIn("Error taxonomy", ok.text)
        refused = repair.read_source("../../etc/passwd", root=self.root)
        self.assertFalse(refused.ok)
        self.assertEqual(refused.decision, "refused")

    def test_cannot_self_repair_when_there_is_no_harness(self) -> None:
        result = repair.apply_source_edit("harness/config.py", "X = 1\n", "nope", root=self.path("empty"))
        self.assertFalse(result.ok)
        self.assertEqual(result.decision, "cannot-self-repair")


class TestSourceGuards(RepairTestCase):
    def test_identical_source_is_refused(self) -> None:
        result = self.edit(self.source())
        self.assertFalse(result.ok)
        self.assertIn("identical", result.reason)

    def test_empty_source_is_refused(self) -> None:
        result = self.edit("   \n")
        self.assertFalse(result.ok)
        self.assertIn("deletion", result.reason)

    def test_syntax_error_is_refused_before_any_write(self) -> None:
        before = sha256(os.path.join(self.root, "harness", "config.py"))
        result = self.edit("def broken(:\n")
        self.assertFalse(result.ok)
        self.assertIn("not valid Python 3.10", result.reason)
        self.assertEqual(sha256(os.path.join(self.root, "harness", "config.py")), before)

    def test_python_311_syntax_is_refused(self) -> None:
        result = self.edit("try:\n    pass\nexcept* ValueError:\n    pass\n")
        self.assertFalse(result.ok)
        self.assertIn("3.10", result.reason)

    def test_third_party_import_is_refused(self) -> None:
        result = self.edit("import requests\n\nX = 1\n")
        self.assertFalse(result.ok)
        self.assertIn("outside the standard library", result.reason)

    def test_oversized_source_is_refused(self) -> None:
        result = self.edit("# pad\n" * (repair.MAX_SOURCE_BYTES // 3))
        self.assertFalse(result.ok)
        self.assertIn("above the", result.reason)

    def test_untested_edits_are_refused(self) -> None:
        result = repair.apply_source_edit(
            "harness/config.py", self.source() + "\n# x\n", "no gate", root=self.root, backups_dir=self.backups,
            run_tests=False,
        )
        self.assertFalse(result.ok)
        self.assertIn("ungated", result.reason)


class TestEditGate(RepairTestCase):
    def test_a_green_edit_is_promoted_with_a_diff_and_a_backup(self) -> None:
        updated = self.source().replace(GOOD_MODEL_LINE, 'DEFAULT_MODEL = "deepseek-chat"  # touched')
        result = self.edit(updated, full_gate=False)
        self.assertTrue(result.ok, result.render())
        self.assertEqual(result.decision, "promoted")
        self.assertEqual(result.tests["failures"], 0)
        self.assertGreater(result.tests["ran"], 0)
        self.assertIn("+", result.diff)
        self.assertIn("touched", self.source())
        self.assertTrue(result.backup_id)
        self.assertTrue(os.path.isdir(os.path.join(self.backups, result.backup_id)))
        self.assertTrue(any("restart" in warning for warning in result.warnings))

    def test_a_red_edit_is_reverted_byte_for_byte(self) -> None:
        before = sha256(os.path.join(self.root, "harness", "config.py"))
        result = self.edit(self.source().replace(GOOD_MODEL_LINE, BROKEN_MODEL_LINE))
        self.assertFalse(result.ok)
        self.assertEqual(result.decision, "reverted")
        self.assertEqual(sha256(os.path.join(self.root, "harness", "config.py")), before)
        self.assertEqual(result.tests["failures"], 1)
        self.assertIn("deepseek-chat", result.error, "the failure must be reported verbatim")
        self.assertIn("byte-for-byte", " ".join(result.warnings))
        self.assertTrue(os.path.isdir(os.path.join(self.backups, result.backup_id)))

    def test_a_patch_is_promoted_and_can_be_reverted(self) -> None:
        good = self.patch(GOOD_MODEL_LINE, GOOD_MODEL_LINE + "  # patched")
        self.assertTrue(good.ok, good.render())
        self.assertIn("# patched", self.source())
        bad = self.patch(GOOD_MODEL_LINE + "  # patched", BROKEN_MODEL_LINE)
        self.assertFalse(bad.ok)
        self.assertEqual(bad.decision, "reverted")
        self.assertNotIn("broken-model", self.source())

    def test_a_patch_that_matches_nothing_is_refused(self) -> None:
        result = self.patch("this text is not in the file", "x")
        self.assertFalse(result.ok)
        self.assertIn("not found", result.reason)

    def test_the_default_gate_is_bounded_and_reported(self) -> None:
        report = repair.run_gate(self.root, relative_path="harness/config.py", test_modules=GATE)
        self.assertTrue(report["ok"], report.get("output_tail"))
        self.assertEqual(report["gate"]["modules"], ["test_config"])
        self.assertFalse(report["gate"]["full"])
        self.assertIn("test_config", report["gate"]["coverage"])

    def test_gate_module_selection(self) -> None:
        self.assertEqual(repair.gate_modules("harness/config.py"), tuple(doctor.gate_modules_for("harness/config.py")))
        self.assertLess(len(repair.gate_modules("harness/config.py")), 10)
        self.assertEqual(repair.gate_modules("harness/config.py", full=True), ())
        self.assertEqual(repair.gate_modules("harness/llm.py", ("test_llm",)), ("test_llm",))

    def test_an_edit_outside_a_harness_is_reported_not_raised(self) -> None:
        os.chmod(self.root, 0o555)
        self.addCleanup(os.chmod, self.root, 0o755)
        if os.geteuid() == 0:  # pragma: no cover
            self.skipTest("running as root")
        result = self.edit(self.source() + "\n# x\n")
        self.assertFalse(result.ok)
        self.assertIn(result.decision, ("cannot-self-repair", "refused"))


class TestRestore(RepairTestCase):
    def test_restore_round_trip_is_verified_by_hash(self) -> None:
        original = self.source()
        snap = repair.snapshot("before", root=self.root, backups_dir=self.backups)
        changed = self.patch(GOOD_MODEL_LINE, GOOD_MODEL_LINE + "  # changed")
        self.assertTrue(changed.ok, changed.render())
        self.assertNotEqual(self.source(), original)
        result = repair.restore(snap.backup_id, root=self.root, backups_dir=self.backups, test_modules=GATE)
        self.assertTrue(result.ok, result.render())
        self.assertEqual(result.decision, "restored")
        self.assertEqual(self.source(), original)
        self.assertEqual(sha256(os.path.join(self.root, "harness", "config.py")), result.hashes["harness/config.py"])
        self.assertTrue(any("pre-restore" in warning for warning in result.warnings))

    def test_restore_with_failing_tests_says_so_loudly(self) -> None:
        snap = repair.snapshot("good", root=self.root, backups_dir=self.backups)
        # Break the tests themselves: they are not part of the snapshot, so the restore
        # cannot fix them, and the gate must report that instead of pretending success.
        with open(os.path.join(self.root, "tests", "test_config.py"), "w", encoding="utf-8") as handle:
            handle.write(
                "import unittest\n\n\nclass TestAlwaysFails(unittest.TestCase):\n"
                "    def test_fails(self):\n        self.assertEqual(1, 2)\n"
            )
        result = repair.restore(snap.backup_id, root=self.root, backups_dir=self.backups, test_modules=GATE)
        self.assertTrue(result.ok)
        self.assertEqual(result.decision, "restored-with-failing-tests")
        self.assertTrue(any("LOUD" in warning for warning in result.warnings))
        self.assertIn("does NOT pass", " ".join(result.warnings))

    def test_restore_refuses_an_unknown_or_unsafe_id(self) -> None:
        for backup_id in ("nope", "../etc", "a/b"):
            with self.subTest(backup_id=backup_id):
                result = repair.restore(backup_id, root=self.root, backups_dir=self.backups, run_tests=False)
                self.assertFalse(result.ok)
                self.assertEqual(result.decision, "refused")

    def test_restore_leaves_unknown_files_alone_and_warns(self) -> None:
        snap = repair.snapshot("snap", root=self.root, backups_dir=self.backups)
        extra = os.path.join(self.root, "harness", "extra_module.py")
        with open(extra, "w", encoding="utf-8") as handle:
            handle.write("X = 1\n")
        result = repair.restore(snap.backup_id, root=self.root, backups_dir=self.backups, test_modules=GATE)
        self.assertTrue(result.ok, result.render())
        self.assertTrue(os.path.exists(extra))
        self.assertTrue(any("left in place" in warning for warning in result.warnings))

    def test_restore_puts_back_the_gate_itself(self) -> None:
        """A snapshot may contain harness/repair.py; restoring it must be allowed."""
        snap = repair.snapshot("snap", root=self.root, backups_dir=self.backups)
        target = os.path.join(self.root, "harness", "repair.py")
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("\n# local modification\n")
        result = repair.restore(snap.backup_id, root=self.root, backups_dir=self.backups, test_modules=GATE)
        self.assertTrue(result.ok, result.render())
        with open(target, encoding="utf-8") as handle:
            self.assertNotIn("# local modification", handle.read())
        self.assertGreater(result.tests["ran"], 0)
        self.assertEqual(result.tests["failures"], 0)


class TestCopyStateDirectory(RepairTestCase):
    """The per-copy state directory beside a clone: visible name, legacy still readable."""

    def test_the_visible_name_is_used_for_a_copy(self) -> None:
        self.assertEqual(repair.COPY_STATE_DIR_NAME, "pyto_harness_state")
        self.assertFalse(repair.COPY_STATE_DIR_NAME.startswith("."), "the Files app must show it")
        path = repair._state_for(self.root)
        self.assertEqual(path, os.path.join(os.path.abspath(self.root), repair.COPY_STATE_DIR_NAME))
        self.assertEqual(repair._backups_dir_for(self.root, None), os.path.join(path, "backups"))

    def test_an_older_copy_keeps_its_hidden_state_readable(self) -> None:
        """The legacy fallback is read-only in effect: old snapshots stay listable."""
        legacy = os.path.join(self.root, repair.LEGACY_COPY_STATE_DIR_NAME)
        os.makedirs(legacy)
        with open(os.path.join(legacy, "marker.txt"), "w", encoding="utf-8") as handle:
            handle.write("old\n")
        self.assertEqual(repair._state_for(self.root), legacy)
        result = repair.snapshot("after-legacy", root=self.root)
        self.assertTrue(result.ok, result.render())
        backups = os.path.join(legacy, "backups")
        self.assertTrue(os.path.isdir(os.path.join(backups, result.backup_id)))
        self.assertIn(result.backup_id, [entry["id"] for entry in repair.list_backups(backups_dir=backups)])

        moved = os.path.join(self.root, repair.COPY_STATE_DIR_NAME)
        os.makedirs(moved)
        self.assertEqual(repair._state_for(self.root), moved, "the new name wins once it exists")
        self.assertTrue(os.path.isfile(os.path.join(legacy, "marker.txt")), "never deleted, never merged")


if __name__ == "__main__":
    unittest.main()
