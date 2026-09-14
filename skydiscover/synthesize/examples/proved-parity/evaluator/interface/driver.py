"""Trusted wrapper: candidate body cannot change the theorem or introduce unchecked code."""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

VERUS = os.environ.get("VERUS", "verus")


def source(impl):
    body = (Path(impl) / "body.rs").read_text()
    if re.search(r"[^a-zA-Z0-9_\s{};=%<>\-,]", body):
        raise ValueError("Candidate is outside the permitted parity expression language")
    allowed = {"let", "mut", "k", "n", "while", "invariant", "decreases", "true", "false"}
    if set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", body)) - allowed:
        raise ValueError("Declarations, macros, attributes, and proof escape hatches are forbidden")
    depth = 0
    for char in body:
        depth += (char == "{") - (char == "}")
        if depth < 0:
            raise ValueError("Candidate escapes its function body")
    if depth:
        raise ValueError("Unclosed candidate block")
    return (
        """use vstd::prelude::*;
verus! {
fn even(n: u64) -> (r: bool)
    ensures r == (n % 2 == 0),
{
"""
        + body
        + """
}
}
fn main() {
    let count: u64 = std::env::args().nth(1).unwrap().parse().unwrap();
    let start = std::time::Instant::now();
    let mut checksum: u64 = 0;
    for i in 0..count {
        let n = std::hint::black_box(10000 + i % 512);
        let r = std::hint::black_box(even(n));
        assert_eq!(r, n % 2 == 0);
        checksum += r as u64;
    }
    std::hint::black_box(checksum);
    println!("{{\\"metrics\\":{{\\"latency_ns\\":{}}}}}", start.elapsed().as_nanos());
}
"""
    )


def main():
    action = sys.argv[1]
    if action == "version":
        subprocess.run([VERUS, "--version"], check=True)
        z3 = os.environ.get("VERUS_Z3_PATH", "z3")
        subprocess.run([z3, "--version"], check=True)
        return
    with tempfile.TemporaryDirectory(prefix="parity-") as temp:
        path = Path(temp) / "parity.rs"
        path.write_text(source(sys.argv[2]))
        command = [VERUS, str(path), "--no-cheating"]
        if action == "build":
            command += [
                "--compile",
                "--crate-name",
                "skysynth_parity",
                "-C",
                "opt-level=0",
                "-C",
                "debuginfo=0",
                "--remap-path-prefix",
                str(Path(temp)) + "=/proof-build",
                "-o",
                str(Path(sys.argv[3]) / "program"),
            ]
        elif action != "verify":
            raise ValueError("Expected verify or build")
        subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
