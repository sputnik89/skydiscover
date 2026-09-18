"""Execute the trusted harness against a small reference and deliberately broken stores."""

import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[2] / (
    "synthesize/examples/verus-db/evaluator/interface/benchmark.rs"
)

REFERENCE = r'''
use std::sync::atomic::{AtomicUsize, Ordering};
static ACTIVE: AtomicUsize = AtomicUsize::new(0);
static MAX: AtomicUsize = AtomicUsize::new(0);
mod db {
    pub trait Database {
        fn get(&self, key: &String) -> Option<&i32>;
        fn put(&mut self, key: String, value: i32);
        fn scan(&self, lo: &String, hi: &String) -> Vec<(String, i32)>;
        fn sort(&self) -> Vec<(String, i32)>;
    }
}
mod candidate {
    use super::*;
    pub struct VerifiedDb(std::collections::BTreeMap<String, i32>);
    impl VerifiedDb { pub fn new() -> Self { Self(Default::default()) } }
    impl db::Database for VerifiedDb {
        fn get(&self, key: &String) -> Option<&i32> {
            if self.0.len() > 3 {
                let n = ACTIVE.fetch_add(1, Ordering::SeqCst) + 1;
                MAX.fetch_max(n, Ordering::SeqCst);
                std::thread::sleep(std::time::Duration::from_millis(1));
                ACTIVE.fetch_sub(1, Ordering::SeqCst);
            }
            self.0.get(key)
        }
        fn put(&mut self, key: String, value: i32) {
            if std::env::var("BROKEN").as_deref() == Ok("put") && self.0.len() > 3
                && self.0.contains_key(&key) { return; }
            self.0.insert(key, value);
        }
        fn scan(&self, lo: &String, hi: &String) -> Vec<(String, i32)> {
            let mut rows: Vec<_> = self.0.range(lo.clone()..=hi.clone())
                .map(|(k,v)| (k.clone(), *v)).collect();
            if std::env::var("BROKEN").as_deref() == Ok("scan") && self.0.len() > 3 {
                rows.reverse();
            }
            rows
        }
        fn sort(&self) -> Vec<(String, i32)> {
            let mut rows: Vec<_> = self.0.iter().map(|(k,v)| (k.clone(), *v)).collect();
            if std::env::var("BROKEN").as_deref() == Ok("sort") && self.0.len() > 3 {
                rows.pop();
            }
            rows
        }
    }
}
mod benchmark;
fn main() {
    benchmark::run();
    let args: Vec<_> = std::env::args().collect();
    if args[6] == "get" && args[5] != "1" {
        assert!(MAX.load(Ordering::SeqCst) > 1, "reads were serialized");
    }
}
'''


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    if not shutil.which("rustc"):
        pytest.skip("requires rustc")
    path = tmp_path_factory.mktemp("concurrent-harness")
    (path / "main.rs").write_text(REFERENCE)
    shutil.copy2(HARNESS, path / "benchmark.rs")
    subprocess.run(["rustc", "--edition=2021", "-O", str(path / "main.rs"),
                    "-o", str(path / "program")], check=True, capture_output=True)
    (path / "load").write_bytes(struct.pack("<16Q", *range(16)))
    (path / "run").write_bytes(struct.pack("<64Q", *(list(range(16)) * 4)))
    return path


def execute(harness, operator, threads, env=None):
    return subprocess.run([
        str(harness / "program"), str(harness / "load"), str(harness / "run"),
        "0.05", "213", str(threads), operator, "4",
    ], text=True, capture_output=True, timeout=10, env=env)


@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("operator", ["get", "put", "scan", "sort"])
def test_shared_store_operator_trials(harness, operator, threads):
    run = execute(harness, operator, threads)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["operator"] == operator and result["threads"] == threads
    assert result["runtime_checks_passed"] is True
    assert len(result["workers"]) == threads
    assert result["operations"] == sum(w["operations"] for w in result["workers"])
    assert result["seconds"] == max(w["seconds"] for w in result["workers"])
    assert result["ops_per_second"] == pytest.approx(result["operations"] / result["seconds"])


@pytest.mark.parametrize("operator", ["put", "scan", "sort"])
def test_incorrect_concurrent_results_fail(harness, operator):
    import os

    run = execute(harness, operator, 4, {**os.environ, "BROKEN": operator})
    assert run.returncode != 0
    assert "runtime_checks_passed" not in run.stdout
