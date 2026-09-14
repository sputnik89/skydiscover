import json
import sys
from pathlib import Path

import pytest

from skydiscover.synthesize.scripts import run_iterations as runner
from skydiscover.synthesize.spec import checkpoint, loop, proof
from tests.spec.test_proof_evaluation import formal  # noqa: F401


def available(monkeypatch):
    original = runner.shutil.which
    monkeypatch.setattr(
        runner.shutil, "which", lambda name: "/fake/claude" if name == "claude" else original(name)
    )


def test_controller_runs_n_scored_iterations_and_finalizes(formal, tmp_path, monkeypatch, capsys):
    run, _ = formal
    available(monkeypatch)
    calls = []

    def invoke(command, prompt, current, timeout, label):
        calls.append(label)
        assert command[:4] == ["claude", "-p", "--output-format", "text"]
        if label == "finalize":
            checkpoint.restore_best(run.path)
            checkpoint.stamp_audit(run.path, [])
            return tmp_path / "final.log"
        loop.begin(run.path)
        loop.cycle(run.path)
        (run.impl / "program.py").write_text(
            f'print(\'{{"metrics":{{"cost":{10-len(calls)}}}}}\')\n'
        )
        proof.evaluate(run.path)
        checkpoint.stamp_audit(run.path, [])
        checkpoint.snapshot_run(run.path, tmp_path, became_best=True)
        return tmp_path / "iteration.log"

    monkeypatch.setattr(runner, "invoke", invoke)
    assert runner.main(["2", "--run", str(run.path)]) == 0
    assert calls == ["iteration-1", "iteration-2", "finalize"]
    assert "Completed 2/2" in capsys.readouterr().out
    assert runner.status(run)["scored"] == 2
    assert runner.status(run)["cycles"] == 2


def test_dry_run_changes_nothing(formal, capsys):
    run, _ = formal
    before = {p: p.read_bytes() for p in run.path.rglob("*") if p.is_file()}
    assert runner.main(["2", "--run", str(run.path), "--agent", "fcc-claude", "--dry-run"]) == 0
    assert "Launch: fcc-claude -p" in capsys.readouterr().out
    assert before == {p: p.read_bytes() for p in run.path.rglob("*") if p.is_file()}


def codex_wired(root, monkeypatch, trusted=True):
    """Lay out what `skydiscover init --agent codex` writes, and a Codex home trusting root."""
    for name in runner.CODEX_WIRING:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("")
    home = root / "codex-home"
    home.mkdir()
    level = "trusted" if trusted else "untrusted"
    (home / "config.toml").write_text(f'[projects."{root}"]\ntrust_level = "{level}"\n')
    monkeypatch.setenv("CODEX_HOME", str(home))
    original = runner.shutil.which
    monkeypatch.setattr(
        runner.shutil, "which", lambda name: "/fake/codex" if name == "codex" else original(name)
    )


def test_codex_dry_run_reads_the_prompt_from_stdin(formal, capsys):
    run, _ = formal
    argv = ["2", "--run", str(run.path), "--agent", "codex", "--model", "gpt-5", "--dry-run"]
    assert runner.main(argv) == 0
    assert (
        "Launch: codex exec --skip-git-repo-check --color never --sandbox workspace-write "
        "--model gpt-5 -" in capsys.readouterr().out
    )


def test_codex_without_wiring_stops_before_launch(formal, tmp_path, monkeypatch, capsys):
    run, _ = formal
    codex_wired(tmp_path, monkeypatch)
    (tmp_path / ".codex/agents/dsa.toml").unlink()
    monkeypatch.setattr(runner, "invoke", lambda *args: pytest.fail("agent must not launch"))
    assert runner.main(["2", "--run", str(run.path), "--agent", "codex"]) == 1
    assert "skydiscover init --agent codex" in capsys.readouterr().err


def test_codex_untrusted_project_stops_before_launch(formal, tmp_path, monkeypatch, capsys):
    run, _ = formal
    codex_wired(tmp_path, monkeypatch, trusted=False)
    monkeypatch.setattr(runner, "invoke", lambda *args: pytest.fail("agent must not launch"))
    assert runner.main(["2", "--run", str(run.path), "--agent", "codex"]) == 1
    assert "does not trust" in capsys.readouterr().err


def test_codex_sandbox_cannot_nest_proof_sandbox_on_macos(formal, tmp_path, monkeypatch, capsys):
    run, _ = formal
    codex_wired(tmp_path, monkeypatch)
    boundary = tmp_path / "boundary"
    boundary.mkdir()
    (boundary / "boundary.json").write_text('{"isolation": "sandbox"}')
    monkeypatch.setattr(runner.proof, "trusted", lambda current: (boundary, "sha256:x"))
    monkeypatch.setattr(runner.sys, "platform", "darwin")
    launched = []
    monkeypatch.setattr(runner, "invoke", lambda command, *args: launched.append(command))
    assert runner.main(["2", "--run", str(run.path), "--agent", "codex"]) == 1
    assert "--codex-sandbox off" in capsys.readouterr().err
    assert launched == []
    argv = ["2", "--run", str(run.path), "--agent", "codex", "--codex-sandbox", "off"]
    assert runner.main(argv) == 1  # launched; the stub makes no counted progress
    assert launched and "--dangerously-bypass-approvals-and-sandbox" in launched[0]
    assert "--sandbox" not in launched[0]


def test_resume_keeps_existing_limits(formal, monkeypatch, capsys):
    run, _ = formal
    assert runner.main(["3", "--run", str(run.path), "--dry-run"]) == 1
    assert "Existing budget" in capsys.readouterr().err
    assert runner.status(run)["limits"] == {"iterations": 2, "cycles": 6, "wall_secs": 0}


def test_no_progress_stops_instead_of_spinning(formal, monkeypatch, capsys):
    run, _ = formal
    available(monkeypatch)
    calls = []
    monkeypatch.setattr(runner, "invoke", lambda *args: calls.append(args[-1]))
    assert runner.main(["2", "--run", str(run.path)]) == 1
    assert calls == ["iteration-1"]
    assert "no counted progress" in capsys.readouterr().err


def test_construction_exhaustion_cannot_claim_n_completed(formal, monkeypatch, capsys):
    run, _ = formal
    available(monkeypatch)
    calls = []

    def fail(*args):
        calls.append(args[-1])
        loop.begin(run.path)
        for _ in range(6):
            loop.cycle(run.path)
        loop.fail(run.path, "unclosed obligation")
        return Path("failure.log")

    monkeypatch.setattr(runner, "invoke", fail)
    assert runner.main(["2", "--run", str(run.path)]) == 1
    assert calls == ["iteration-1"]
    assert "exhausted at 0/2" in capsys.readouterr().err


def test_missing_anchor_prevents_agent_launch(formal, monkeypatch, capsys):
    run, _ = formal
    available(monkeypatch)
    monkeypatch.delenv("SKYDISCOVER_PROOF_CONTRACT")
    monkeypatch.setattr(runner, "invoke", lambda *args: pytest.fail("agent must not launch"))
    assert runner.main(["2", "--run", str(run.path)]) == 1
    assert "Missing external proof anchor" in capsys.readouterr().err


def test_invoke_passes_prompt_as_stdin_without_shell_evaluation(formal):
    run, _ = formal
    prompt = 'literal $(touch SENTINEL) `echo bad` and "quoted text"'
    log = runner.invoke(
        [sys.executable, "-c", "import sys; print(sys.stdin.read())"], prompt, run, 10, "test"
    )
    assert log.read_text().strip() == prompt
    assert log.with_suffix(".prompt.md").read_text().strip() == prompt
    assert not (run.path / "SENTINEL").exists()


def test_invoke_timeout_preserves_logs_and_stops_child(formal):
    run, _ = formal
    with pytest.raises(RuntimeError, match="exceeded 1s"):
        runner.invoke(
            [sys.executable, "-c", "import time; time.sleep(60)"], "work", run, 1, "timeout"
        )
    assert list((run.synthesis / "launcher").glob("timeout-*.log"))


@pytest.mark.parametrize("value", ["0", "-1"])
def test_nonpositive_iteration_count_is_rejected(value):
    with pytest.raises(SystemExit):
        runner.main([value, "--run", "/unused"])
