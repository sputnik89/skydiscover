#!/usr/bin/env python3
"""Check one candidate against the pinned database contract (Python stdlib only)."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HASHES = {
    "mod.rs": "b22f651ce5002f6f5fb733ccde9cc0b433ae2f5555d33571f5ddc5f7652b0d92",
}
FORBIDDEN = {
    "assume",
    "admit",
    "assume_specification",
    "axiom",
    "external_body",
    "external",
    "inline_air_stmt",
    "exec_allows_no_decreases_clause",
    "assume_termination",
}


def check_source(source):
    """Like the Rocq example, reject proof shortcuts even in comments."""
    for word in sorted(FORBIDDEN):
        if re.search(r"\b" + re.escape(word) + r"\b", source):
            raise ValueError(f"forbidden proof shortcut: {word}")


def successful_summary(stdout):
    decoder = json.JSONDecoder()
    summaries = []
    for i, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stdout[i:])
        except ValueError:
            continue
        if isinstance(value, dict) and "verification-results" in value:
            summaries.append(value["verification-results"])
    if len(summaries) != 1 or not isinstance(summaries[0], dict):
        return False
    result = summaries[0]
    return (
        result.get("success") is True
        and result.get("is-verifying-entire-crate") is True
        and type(result.get("verified")) is int
        and result["verified"] > 0
        and result.get("errors") == 0
        and result.get("encountered-error") is False
        and result.get("encountered-vir-error") is False
    )


def main():
    impl = Path(os.environ["SKYDISCOVER_IMPL"]).resolve()
    spec = Path(os.environ["SKYDISCOVER_INTERFACE"]).resolve()
    fixed = {}
    for name, expected in HASHES.items():
        data = (spec / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"immutable file changed: {name}")
        fixed[name] = data
    if not (impl / "implementation.rs").is_file():
        raise ValueError("candidate must contain implementation.rs")
    sources = {}
    for path in impl.rglob("*.rs"):
        data = path.read_bytes()
        check_source(data.decode("utf-8"))
        sources[path.relative_to(impl)] = data
    test = Path(__file__).with_name("database_test.rs").read_bytes()
    binary = shutil.which(os.environ.get("VERUS", "verus"))
    pinned = spec / "toolchain.json"
    settings = json.loads(pinned.read_text()) if pinned.is_file() else None
    if settings is not None:
        for path, expected in settings["files"].items():
            if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
                raise ValueError(f"Pinned toolchain file changed: {path}")
        binary = settings["verus"]
    if binary is None:
        raise ValueError("Verus not found; set VERUS to its executable path")
    binary = str(Path(binary).resolve())
    with tempfile.TemporaryDirectory(prefix="skysynth-verus-db-") as scratch:
        build = Path(scratch)
        (build / "spec").mkdir()
        for name, data in fixed.items():
            (build / "spec" / name).write_bytes(data)
        for name, data in sources.items():
            target = build / "impl" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (build / "database_test.rs").write_bytes(test)
        command = [
            binary,
            "database_test.rs",
            "--crate-type",
            "bin",
            "--crate-name",
            "skysynth_database",
            "--no-cheating",
            "--output-json",
            "--expand-errors",
            "--rlimit",
            "10",
            "--num-threads",
            "1",
            "--compile",
            "-o",
            "database",
        ]
        env = os.environ.copy()
        if settings is not None:
            env["VERUS_Z3_PATH"] = settings["z3"]
        # Do not let inherited Rust/Verus flags weaken the fixed verification command.
        for name in ("VERUS_EXTRA_ARGS", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
            env.pop(name, None)
        result = subprocess.run(
            command, cwd=build, env=env, capture_output=True, text=True, timeout=180
        )
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        if result.returncode != 0 or not successful_summary(result.stdout):
            raise ValueError("Verus did not completely verify and compile the crate")
        subprocess.run([str(build / "database")], cwd=build, env=env, check=True, timeout=10)
    print("PROOF PASSED: original Database contract, empty constructor, and concrete exercise")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
