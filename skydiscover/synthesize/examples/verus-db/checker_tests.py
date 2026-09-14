"""Checker regression tests; run with python3 checker_tests.py (no Verus required)."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("verus_db_proof", ROOT / "evaluator/tests/proof.py")
proof = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proof)


def summary(**changes):
    fields = {"success": True, "verified": 6, "errors": 0,
              "is-verifying-entire-crate": True, "encountered-error": False,
              "encountered-vir-error": False}
    fields.update(changes)
    return json.dumps({"verification-results": fields})


class CheckerTests(unittest.TestCase):
    def test_rejects_proof_shortcuts(self):
        for source in (
            "fn f() { assume(false); }", "fn f() { admit(); }",
            "#[verifier::external_body] fn f() {}",
            "#[verifier::axiom] proof fn f() {}",
            "// assume(false)\n",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                proof.check_source(source)

    def test_allows_ordinary_rust_and_verus_syntax(self):
        proof.check_source("""
            use vstd::prelude::*;
            mod helpers;
            #[derive(Clone)]
            pub struct Label { text: String }
            fn label() -> Label { Label { text: String::from("数据库") } }
            verus! {
                fn values() -> Vec<u64> { vec![1, 2] }
            }
        """)

    def test_requires_complete_verification_evidence(self):
        self.assertTrue(proof.successful_summary(summary()))
        for output in ("", "{}", summary(verified=0), summary(success=False),
                       summary(errors=1), summary(**{"is-verifying-entire-crate": False}),
                       summary(**{"encountered-vir-error": True}),
                       summary() + summary()):
            with self.subTest(output=output):
                self.assertFalse(proof.successful_summary(output))

    def run_gate(self, root, response=None):
        env = {"SKYDISCOVER_IMPL": str(root / "candidate"),
               "SKYDISCOVER_INTERFACE": str(root / "interface")}
        with patch.dict(os.environ, env), patch.object(proof.shutil, "which", return_value="/fake/verus"), \
                patch.object(proof.subprocess, "run", return_value=response) as runner, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            proof.main()
        return runner.call_args_list

    def fixture(self, root):
        shutil.copytree(ROOT / "evaluator", root / "interface")
        (root / "candidate").mkdir()
        (root / "candidate/implementation.rs").write_text("use vstd::prelude::*;\nverus! { pub fn unrelated() {} }\n")

    def test_build_preserves_spec_test_and_candidate_modules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            expected = {"spec/" + name: (root / "interface" / name).read_bytes() for name in proof.HASHES}
            expected["database_test.rs"] = (ROOT / "evaluator/tests/database_test.rs").read_bytes()
            (root / "candidate/helpers").mkdir()
            (root / "candidate/helpers/mod.rs").write_text("pub fn helper() {}\n")
            expected["impl/helpers/mod.rs"] = (root / "candidate/helpers/mod.rs").read_bytes()
            expected["impl/implementation.rs"] = (root / "candidate/implementation.rs").read_bytes()

            def inspect_build(command, **kwargs):
                build = Path(kwargs["cwd"])
                for name, data in expected.items():
                    self.assertEqual((build / name).read_bytes(), data)
                self.assertEqual(command[1], "database_test.rs")
                return subprocess.CompletedProcess(command, 1, "", "expected stop after inspection")

            with patch.dict(os.environ, {"SKYDISCOVER_IMPL": str(root / "candidate"),
                                        "SKYDISCOVER_INTERFACE": str(root / "interface")}), \
                    patch.object(proof.shutil, "which", return_value="/fake/verus"), \
                    patch.object(proof.subprocess, "run", side_effect=inspect_build), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaisesRegex(ValueError, "did not completely verify"):
                proof.main()

    def test_altered_spec_never_runs_verus(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            path = root / "interface/mod.rs"
            path.write_text(path.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "immutable file changed"):
                self.run_gate(root)

    def test_relocated_suite_checks_shortcuts_in_submodules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "interface").mkdir()
            shutil.copy2(ROOT / "evaluator/mod.rs", root / "interface/mod.rs")
            shutil.copytree(ROOT / "evaluator/tests", root / "suite")
            (root / "candidate/helpers").mkdir(parents=True)
            (root / "candidate/implementation.rs").write_text("mod helpers;\n")
            (root / "candidate/helpers/mod.rs").write_text("proof fn cheat() { admit(); }\n")
            result = subprocess.run(
                ["bash", str(root / "suite/test.sh")], cwd=root,
                env={**os.environ, "SKYDISCOVER_IMPL": str(root / "candidate"),
                     "SKYDISCOVER_INTERFACE": str(root / "interface")},
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("forbidden proof shortcut: admit", result.stderr)

    def test_zero_exit_without_evidence_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            with self.assertRaisesRegex(ValueError, "did not completely verify"):
                self.run_gate(root, subprocess.CompletedProcess([], 0, "", ""))

    def test_verifier_failure_fails_even_with_success_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            with self.assertRaisesRegex(ValueError, "did not completely verify"):
                self.run_gate(root, subprocess.CompletedProcess([], 1, summary(), "error"))

    def test_verification_is_followed_by_execution(self):
        # This mocks the tool protocol, not a verified database implementation.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            calls = self.run_gate(root, subprocess.CompletedProcess([], 0, summary(), ""))
            self.assertEqual(len(calls), 2)
            command = calls[0].args[0]
            self.assertIn("--no-cheating", command)
            self.assertIn("--compile", command)
            self.assertNotIn("--no-verify", command)
            self.assertNotIn("--no-lifetime", command)
            self.assertTrue(calls[1].kwargs["check"])
            self.assertEqual(calls[1].kwargs["timeout"], 10)

    def test_named_test_rejects_unknown_files(self):
        result = subprocess.run(["bash", str(ROOT / "evaluator/tests/test.sh"), "missing.py"],
                                env={**os.environ, "SKYDISCOVER_IMPL": "/unused"},
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown test", result.stderr)


if __name__ == "__main__":
    unittest.main()
