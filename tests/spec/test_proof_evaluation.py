"""Control-flow/security tests use a small checker fixture; real Verus replay lives in examples."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from skydiscover.synthesize.spec import checkpoint, loop, proof
from skydiscover.synthesize.spec import run as finish
from skydiscover.synthesize.spec.paths import Run


@pytest.fixture
def formal(tmp_path, monkeypatch):
    run = Run(tmp_path / "run").create()
    run.task.write_text(
        "---\nchecked_by: proof\nevaluation: scored\ndomain: arithmetic\n---\nFixture\n"
    )
    for path in (run.impl, run.interface, run.tests):
        path.mkdir(parents=True, exist_ok=True)
    (run.interface / "target.txt").write_text("Pinned target and allowed assumptions\n")
    (run.impl / "program.py").write_text('print(\'{"metrics":{"cost":10}}\')\n')
    (run.impl / "proof.txt").write_text("complete\n")
    run.test_script.write_text('set -eu\ngrep -qx complete "$SKYDISCOVER_IMPL/proof.txt"\n')
    (run.tests / "proof.txt").write_text("suite entry\n")
    cfg = {
        "timeout": 30,
        "objective": "cost",
        "direction": "min",
        "config": {},
        "workload": "one invocation",
        "executable": "program.py",
        "trust_boundary": "unit-test checker fixture, not a mathematical proof",
        "toolchain": [sys.executable, "--version"],
        "build": [
            sys.executable,
            "-c",
            "import shutil,sys; shutil.copyfile(sys.argv[1],sys.argv[2])",
            "{impl}/program.py",
            "{executable}",
        ],
        "benchmark": [sys.executable, "{executable}"],
        "held_out_benchmark": [sys.executable, "-c", "print('{{\"metrics\":{{\"cost\":99}}}}')"],
    }
    run.proof_config.write_text(json.dumps(cfg))
    trust = tmp_path / "trusted"
    anchor = proof.freeze(run.path, trust, isolation="external")
    monkeypatch.setenv("SKYDISCOVER_PROOF_TRUST", str(trust))
    monkeypatch.setenv("SKYDISCOVER_PROOF_CONTRACT", anchor)
    loop.initialize(run.path, 2, 6)
    return run, trust


def candidate(run):
    loop.begin(run.path)
    loop.cycle(run.path)


def score(run, tmp_path, best=False):
    proof.evaluate(run.path)
    checkpoint.stamp_audit(run.path, [])
    return checkpoint.snapshot_run(run.path, tmp_path, became_best=best)


def test_proof_and_evaluation_modes_are_independent(tmp_path):
    run = Run(tmp_path / "run").create()
    for front, expected in (
        ("", True),
        ("checked_by: proof\n", False),
        ("checked_by: proof\nevaluation: scored\n", True),
    ):
        run.task.write_text(f"---\n{front}---\nTask\n")
        assert run.requires_evaluation() is expected
    for front in ("evaluation: wrong", "evaluation: proof-only"):
        run.task.write_text(f"---\n{front}\n---\n")
        with pytest.raises(ValueError):
            run.requires_evaluation()


def test_verified_candidates_share_scoring_and_budget(formal, tmp_path):
    run, _ = formal
    candidate(run)
    loop.cycle(run.path)  # multiple DSA substeps, one scored iteration
    out, first = score(run, tmp_path, best=True)
    assert checkpoint.read_score(first)["score"] == {"cost": 10}
    candidate(run)
    (run.impl / "program.py").write_text('print(\'{"metrics":{"cost":20}}\')\n')
    _, second = score(run, tmp_path)  # a regression counts too
    state = json.loads(run.loop_state.read_text())
    assert state["cycles"] == 3
    assert [a["status"] for a in state["attempts"]] == ["scored", "scored"]
    assert checkpoint.selected_best_checkpoint(out) == first
    assert checkpoint.read_record(second)["proof"]["complete"] is True
    with pytest.raises(ValueError, match="iteration budget"):
        loop.begin(run.path)
    recovery = checkpoint.restore_best(run.path)
    assert "20" in (recovery / "impl/program.py").read_text()
    assert "10" in (run.impl / "program.py").read_text()
    checkpoint.stamp_audit(run.path, [])
    lines = finish.export_deliverable(run.path, tmp_path)
    assert any("best ->" in line for line in lines)


def test_held_out_draw_is_recorded_but_never_scored(formal, tmp_path):
    run, _ = formal
    candidate(run)
    scored = proof.evaluate(run.path)
    held_out = proof.evaluate(run.path, draw="held-out")
    assert (scored["draw"], held_out["draw"]) == ("scored", "held-out")
    assert held_out["metrics"] == {"cost": 99}
    assert (run.bench / f"attempt-{held_out['attempt']}.held-out.json").is_file()
    checkpoint.stamp_audit(run.path, [])
    _, first = checkpoint.snapshot_run(run.path, tmp_path, became_best=True)
    assert checkpoint.read_score(first)["score"] == {"cost": 10}
    with pytest.raises(ValueError, match="draw"):
        proof.evaluate(run.path, draw="bogus")


def test_held_out_draw_requires_a_declared_command(formal, monkeypatch):
    run, _ = formal
    candidate(run)
    declared = proof.config
    monkeypatch.setattr(
        proof,
        "config",
        lambda r: {k: v for k, v in declared(r).items() if k != "held_out_benchmark"},
    )
    with pytest.raises(ValueError, match="held_out_benchmark"):
        proof.evaluate(run.path, draw="held-out")
    assert not run.leaderboard.exists()


def test_failed_proof_never_scores_and_budget_survives_resume(formal):
    run, _ = formal
    candidate(run)
    (run.impl / "proof.txt").write_text("partial\n")
    with pytest.raises(ValueError, match="Command failed"):
        proof.evaluate(run.path)
    assert not run.leaderboard.exists()
    loop.fail(run.path, "remaining obligation")
    loop.initialize(run.path, 2, 6)
    state = json.loads(run.loop_state.read_text())
    assert state["cycles"] == 1 and state["attempts"][0]["status"] == "failed"
    with pytest.raises(ValueError, match="cannot reset"):
        loop.initialize(run.path, 2, 60)


@pytest.mark.parametrize("target", ["spec", "checker", "task", "config", "anchor"])
def test_trusted_inputs_cannot_be_replaced(formal, target):
    run, trust = formal
    path = {
        "spec": run.interface / "target.txt",
        "checker": run.test_script,
        "task": run.task,
        "config": run.proof_config,
        "anchor": trust / "boundary.json",
    }[target]
    path.write_text(path.read_text() + "\nchanged\n")
    with pytest.raises(ValueError, match="changed"):
        proof.verify(run.path)


def test_missing_anchor_fails_closed(formal, monkeypatch):
    run, _ = formal
    monkeypatch.delenv("SKYDISCOVER_PROOF_CONTRACT")
    with pytest.raises(ValueError, match="Missing external"):
        proof.verify(run.path)


def test_changing_correctness_mode_cannot_disable_verification(formal, tmp_path):
    run, _ = formal
    run.task.write_text("---\nchecked_by: tests\n---\nDowngraded\n")
    with pytest.raises(ValueError, match="Pinned formal input changed"):
        checkpoint.score_from_run(run.path)


def test_checker_cannot_substitute_sources_before_build(formal, monkeypatch):
    run, _ = formal
    original = proof.execute

    def changed(command, cwd, env, timeout):
        output = original(command, cwd, env, timeout)
        if command == ["bash", "test.sh"]:
            (Path(env["SKYDISCOVER_IMPL"]) / "program.py").write_text("unverified replacement")
        return output

    monkeypatch.setattr(proof, "execute", changed)
    with pytest.raises(ValueError, match="changed candidate sources"):
        proof.verify(run.path)


@pytest.mark.parametrize("target", ["source", "proof", "executable"])
def test_changed_measured_bytes_invalidate_score(formal, tmp_path, target):
    run, _ = formal
    candidate(run)
    proof.evaluate(run.path)
    path = {
        "source": run.impl / "program.py",
        "proof": run.impl / "proof.txt",
        "executable": run.synthesis / "verified-build/program.py",
    }[target]
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="No leaderboard"):
        checkpoint.snapshot_run(run.path, tmp_path)


def test_forged_evidence_and_missing_score_are_rejected(formal, tmp_path):
    run, _ = formal
    candidate(run)
    with pytest.raises(ValueError, match="No leaderboard"):
        checkpoint.snapshot_run(run.path, tmp_path, require_evaluation=False)
    row = proof.evaluate(run.path)
    row["proof"]["contract"] = "forged"
    run.leaderboard.write_text(json.dumps([row]))
    with pytest.raises(ValueError, match="does not match"):
        checkpoint.snapshot_run(run.path, tmp_path)


def test_budget_exhaustion_allows_only_inflight_completion(formal, tmp_path):
    run, _ = formal
    candidate(run)
    for _ in range(5):
        loop.cycle(run.path)
    with pytest.raises(ValueError, match="cycle budget"):
        loop.cycle(run.path)
    score(run, tmp_path, best=True)
    with pytest.raises(ValueError, match="cycle budget"):
        loop.begin(run.path)


def test_crash_after_checkpoint_recovers_counter(formal, tmp_path, monkeypatch):
    run, _ = formal
    candidate(run)
    proof.evaluate(run.path)
    original = loop.complete
    monkeypatch.setattr(loop, "complete", lambda *args: None)
    checkpoint.snapshot_run(run.path, tmp_path)
    monkeypatch.setattr(loop, "complete", original)
    assert loop.begin(run.path) == 2
    state = json.loads(run.loop_state.read_text())
    assert state["attempts"][0]["status"] == "scored"


def test_legacy_proof_only_delivers_without_score(tmp_path, monkeypatch):
    run = Run(tmp_path / "legacy").create()
    run.task.write_text("---\nchecked_by: proof\ndomain: arithmetic\n---\nProof-only\n")
    for path in (run.impl, run.interface, run.tests):
        path.mkdir(parents=True, exist_ok=True)
    (run.impl / "proof.txt").write_text("complete\n")
    run.test_script.write_text('grep -qx complete "$SKYDISCOVER_IMPL/proof.txt"\n')
    (run.tests / "proof.txt").write_text("suite entry\n")
    trust = tmp_path / "trusted"
    anchor = proof.freeze(run.path, trust, isolation="external")
    monkeypatch.setenv("SKYDISCOVER_PROOF_TRUST", str(trust))
    monkeypatch.setenv("SKYDISCOVER_PROOF_CONTRACT", anchor)
    checkpoint.stamp_audit(run.path, [])
    lines = finish.export_deliverable(run.path, tmp_path)
    assert any("proof-only" in line for line in lines)
    assert not any("WARNING" in line for line in lines)


def test_partial_delivery_cannot_bypass_frozen_checker(formal):
    run, _ = formal
    (run.impl / "proof.txt").write_text("partial")
    script = (
        Path(__file__).resolve().parents[2] / "skydiscover/synthesize/workflow/scripts/run_tests.py"
    )
    proc = subprocess.run(
        [sys.executable, str(script), "--run", str(run.path), "--test", "nonexistent"],
        capture_output=True,
    )
    assert proc.returncode != 0


def test_unsupported_sandbox_never_silently_runs(monkeypatch):
    monkeypatch.setattr(proof.sys, "platform", "unsupported")
    with pytest.raises(ValueError, match="No enforced sandbox"):
        proof.confined(["true"], [])


def test_enforced_worker_cannot_change_contract(formal):
    run, trust = formal
    try:
        proof.worker(run.path, [sys.executable, "-c", "pass"], 10)
    except ValueError as exc:
        if "Operation not permitted" in str(exc) or "No enforced sandbox" in str(exc):
            pytest.skip("Host does not allow nested sandbox creation; run this check outside it")
        raise
    before = proof.digest(trust)
    command = [
        sys.executable,
        "-c",
        "import pathlib,sys; pathlib.Path('progress.txt').write_text('ok'); "
        "pathlib.Path(sys.argv[1]).write_text('weakened')",
        str(trust / "boundary.json"),
    ]
    with pytest.raises(ValueError, match="Command failed"):
        proof.worker(run.path, command, 10)
    assert (run.impl / "progress.txt").read_text() == "ok"
    assert proof.digest(trust) == before
    command[-1] = str(run.interface / "target.txt")
    with pytest.raises(ValueError, match="Command failed"):
        proof.worker(run.path, command, 10)
    proof.trusted(run)


def test_delivery_requires_a_score_even_when_proof_passes(formal):
    run, _ = formal
    script = (
        Path(__file__).resolve().parents[2] / "skydiscover/synthesize/workflow/scripts/run_tests.py"
    )
    proc = subprocess.run(
        [sys.executable, str(script), "--run", str(run.path), "--delivery"], capture_output=True
    )
    assert proc.returncode != 0 and b"No leaderboard" in proc.stderr


def test_construction_time_budget_is_persistent(formal, monkeypatch):
    run, _ = formal
    with loop.locked_state(run.path) as state:
        state["limits"]["wall_secs"] = 1
        state["started"] = 0
    with pytest.raises(ValueError, match="time budget"):
        loop.begin(run.path)
