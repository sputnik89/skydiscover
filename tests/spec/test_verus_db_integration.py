"""Database-specific contract setup and fail-closed evaluator regressions."""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from skydiscover.synthesize.spec import loop, proof
from skydiscover.synthesize.spec.paths import Run

EXAMPLE = Path(__file__).resolve().parents[2] / "synthesize/examples/verus-db"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def setup():
    return module("database_prepare", EXAMPLE / "prepare.py")


@pytest.fixture
def driver():
    return module("database_driver", EXAMPLE / "evaluator/interface/driver.py")


def test_original_trait_and_packaged_example_match():
    expected = "b22f651ce5002f6f5fb733ccde9cc0b433ae2f5555d33571f5ddc5f7652b0d92"
    assert hashlib.sha256((EXAMPLE / "evaluator/mod.rs").read_bytes()).hexdigest() == expected
    assert (EXAMPLE / "evaluator/mod.rs").read_bytes() == (
        EXAMPLE / "evaluator/interface/mod.rs"
    ).read_bytes()
    packaged = EXAMPLE.parents[2] / "skydiscover/synthesize/examples/verus-db"
    for source in EXAMPLE.rglob("*"):
        if source.is_file() and "__pycache__" not in source.parts:
            assert (packaged / source.relative_to(EXAMPLE)).read_bytes() == source.read_bytes()


def test_prepare_freezes_workload_and_retains_external_anchor(tmp_path, monkeypatch, setup):
    monkeypatch.setattr(setup, "tools_config", lambda *args: {"files": {}})
    run = Run(tmp_path / "run")
    trust = tmp_path / "trust"
    control = setup.prepare(
        run.path, trust, iterations=3, load_count=32, run_count=128, seconds=0.01, repeats=1,
        threads=4, optimize_for="scan", scan_width=7,
    )
    monkeypatch.setenv("SKYDISCOVER_PROOF_TRUST", control["trust"])
    monkeypatch.setenv("SKYDISCOVER_PROOF_CONTRACT", control["contract"])
    proof.trusted(run)
    assert run.requires_evaluation()
    assert json.loads(run.loop_state.read_text())["limits"]["iterations"] == 3
    assert setup.controller_path(trust).is_file()
    workload = json.loads((run.interface / "workload.json").read_text())
    draws = workload["draws"]
    assert set(draws) == {"scored", "held-out"}
    assert draws["scored"]["seed"] != draws["held-out"]["seed"]
    for draw in draws:
        metadata = json.loads(
            next((run.interface / "traces" / draw).glob("*.meta.json")).read_text()
        )
        assert not Path(metadata["load_file"]).is_absolute()
        assert metadata["params"]["theta"] == 0.99 and metadata["params"]["scramble"]
        assert metadata["params"]["seed"] == draws[draw]["seed"]
    cfg = json.loads(run.proof_config.read_text())
    assert cfg["objective"] == "scan_ops_per_sec"
    assert cfg["config"]["threads"] == 4
    assert cfg["config"]["scan_width"] == 7
    assert cfg["config"]["operators"] == ["get", "put", "scan", "sort"]
    assert "held_out_benchmark" in cfg
    assert json.loads(run.workload_card.read_text())["scored_configuration"] == cfg["config"]
    for name in (
        "mod.rs",
        "benchmark.rs",
        "workload.json",
        "traces/" + draws["scored"]["load_file"],
        "traces/" + draws["held-out"]["run_file"],
    ):
        path = run.interface / name
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        with pytest.raises(ValueError, match="Pinned formal input changed"):
            proof.trusted(run)
        path.write_bytes(original)
    with pytest.raises(ValueError, match="fresh"):
        setup.prepare(run.path, trust)
    with pytest.raises(ValueError, match="reset"):
        loop.initialize(run.path, 4, 30)
    command = [
        sys.executable,
        str(EXAMPLE / "launch.py"),
        "--run",
        str(run.path),
        "--trust-root",
        str(trust),
        "--iterations",
        "3",
        "--prepare-only",
    ]
    resumed = subprocess.run(command, capture_output=True, text=True)
    assert resumed.returncode == 0, resumed.stderr
    changed = subprocess.run(command + ["--seconds", "2"], capture_output=True, text=True)
    assert changed.returncode != 0 and "Cannot change frozen workload" in changed.stderr
    for args in (["--threads", "8"], ["--optimize-for", "put"], ["--scan-width", "8"]):
        changed = subprocess.run(command + args, capture_output=True, text=True)
        assert changed.returncode != 0 and "Cannot change frozen workload" in changed.stderr


@pytest.mark.parametrize("operator", ["get", "put", "scan", "sort"])
def test_objective_configuration(tmp_path, monkeypatch, setup, operator):
    monkeypatch.setattr(setup, "tools_config", lambda *args: {"files": {}})
    setup.prepare(tmp_path / "run", tmp_path / "trust", load_count=4, run_count=8,
                  seconds=.01, repeats=1, optimize_for=operator)
    config = json.loads(Run(tmp_path / "run").proof_config.read_text())
    assert config["objective"] == operator + "_ops_per_sec"


@pytest.mark.parametrize("options", [
    {"threads": 0}, {"threads": -1}, {"threads": 1.5},
    {"scan_width": 0}, {"optimize_for": "mixed"},
])
def test_invalid_concurrency_settings(tmp_path, setup, options):
    with pytest.raises(ValueError):
        setup.prepare(tmp_path / "run", tmp_path / "trust", **options)
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), 0, -1])
def test_invalid_timing_fails_before_setup(tmp_path, setup, seconds):
    with pytest.raises(ValueError):
        setup.prepare(tmp_path / "run", tmp_path / "trust", seconds=seconds)
    assert not (tmp_path / "run").exists()


def test_driver_rejects_changed_toolchain_file(tmp_path, monkeypatch, driver):
    binary = tmp_path / "compiler"
    binary.write_bytes(b"original")
    settings = {"files": {str(binary): hashlib.sha256(binary.read_bytes()).hexdigest()}}
    (tmp_path / "toolchain.json").write_text(json.dumps(settings))
    monkeypatch.setattr(driver, "HERE", tmp_path)
    assert driver.toolchain() == settings
    binary.write_bytes(b"replacement")
    with pytest.raises(ValueError, match="Pinned toolchain"):
        driver.toolchain()


def test_operator_trials_are_measured_separately(tmp_path, monkeypatch, driver, capsys):
    config = {
        "trace_sha256": {}, "draws": {"scored": {
            "load_file": "load", "run_file": "run", "operation_seed": 213,
        }}, "operators": ["get", "put", "scan", "sort"], "threads": 8,
        "scan_width": 16, "repeats": 3, "seconds": 1,
    }
    (tmp_path / "workload.json").write_text(json.dumps(config))
    monkeypatch.setattr(driver, "HERE", tmp_path)
    calls = []

    def measure(command, **kwargs):
        op = command[6]
        calls.append(op)
        rate = (config["operators"].index(op) + 1) * 100
        result = {"operator": op, "threads": 8, "operations": rate,
                  "seconds": 1, "ops_per_second": rate, "runtime_checks_passed": True}
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

    monkeypatch.setattr(driver.subprocess, "run", measure)
    driver.benchmark("program")
    result = json.loads(capsys.readouterr().out)
    assert result["metrics"] == {
        "get_ops_per_sec": 100, "put_ops_per_sec": 200,
        "scan_ops_per_sec": 300, "sort_ops_per_sec": 400,
    }
    assert calls == [op for op in config["operators"] for _ in range(3)]

    def wrong_operator(command, **kwargs):
        result = json.loads(measure(command).stdout)
        result["operator"] = "mixed"
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

    monkeypatch.setattr(driver.subprocess, "run", wrong_operator)
    with pytest.raises(ValueError, match="validation failed"):
        driver.benchmark("program")


def build_fixture(tmp_path, monkeypatch, driver):
    impl, output = tmp_path / "impl", tmp_path / "build"
    impl.mkdir()
    output.mkdir()
    (impl / "implementation.rs").write_text("verus! { pub fn body() {} }")
    monkeypatch.setattr(driver, "toolchain", lambda: {"verus": "/pinned/verus", "z3": "/pinned/z3"})
    return impl, output


def test_measured_build_keeps_original_checks_and_requires_full_evidence(
    tmp_path, monkeypatch, driver
):
    impl, output = build_fixture(tmp_path, monkeypatch, driver)
    original = (EXAMPLE / "evaluator/tests/database_test.rs").read_text()

    def compiler(argv, **kwargs):
        measured = (Path(kwargs["cwd"]) / "database.rs").read_text()
        assert original.split("fn main()")[0] in measured
        assert "benchmark::run()" in measured
        assert "--no-cheating" in argv and "--compile" in argv
        assert not any(
            arg.startswith(("--no-verify", "--verify-", "--no-lifetime")) for arg in argv
        )
        assert kwargs["env"]["VERUS_Z3_PATH"] == "/pinned/z3"
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(driver.subprocess, "run", compiler)
    with pytest.raises(ValueError, match="Full measured-crate verification failed"):
        driver.build(impl, output)


@pytest.mark.parametrize(
    "source",
    [
        "#[cfg(not(verus_keep_ghost))] fn f() {}",
        'include!("elsewhere.rs");',
        '#[path="../../elsewhere.rs"] mod trick;',
        "macro_rules! fake { () => {} }",
        "unsafe fn f() {}",
        "verus! { proof fn fake() { admit(); } }",
    ],
)
def test_build_rejects_alternative_or_unchecked_sources(tmp_path, monkeypatch, driver, source):
    impl, output = build_fixture(tmp_path, monkeypatch, driver)
    (impl / "implementation.rs").write_text(source)
    monkeypatch.setattr(
        driver.subprocess, "run", lambda *a, **kw: pytest.fail("compiler must not run")
    )
    with pytest.raises(ValueError):
        driver.build(impl, output)


@pytest.mark.skipif(
    os.environ.get("VERUS_DB_NATIVE_TESTS") != "1",
    reason="requires Verus and native process sandbox",
)
def test_native_failed_proof_and_worker_write_boundary(tmp_path, monkeypatch, setup):
    run = Run(tmp_path / "run")
    control = setup.prepare(
        run.path,
        tmp_path / "trust",
        iterations=1,
        load_count=32,
        run_count=128,
        seconds=0.01,
        repeats=1,
    )
    monkeypatch.setenv("SKYDISCOVER_PROOF_TRUST", control["trust"])
    monkeypatch.setenv("SKYDISCOVER_PROOF_CONTRACT", control["contract"])
    loop.begin(run.path)
    loop.cycle(run.path)
    source = (EXAMPLE / "candidates/baseline.rs").read_text()
    (run.impl / "implementation.rs").write_text(
        source + "\nverus! { proof fn impossible() ensures false {} }\n"
    )
    with pytest.raises(ValueError, match="Command failed"):
        proof.evaluate(run.path)
    assert not run.leaderboard.exists()
    script = """import pathlib, sys
pathlib.Path(sys.argv[1]).write_text('allowed')
for forbidden in sys.argv[2:]:
    try:
        pathlib.Path(forbidden).write_text('forbidden')
    except PermissionError:
        continue
    raise SystemExit('worker escaped write boundary: ' + forbidden)
"""
    proof.worker(
        run.path,
        [
            sys.executable,
            "-c",
            script,
            str(run.impl / "worker-note"),
            str(run.interface / "mod.rs"),
            str(run.loop_state),
            str(setup.controller_path(Path(control["trust"]))),
        ],
        timeout=30,
    )
    proof.trusted(run)
    assert (run.impl / "worker-note").read_text() == "allowed"
