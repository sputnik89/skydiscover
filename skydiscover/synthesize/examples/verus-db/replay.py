"""Exercise the actual scored loop with supplied verified database fixtures.

This validates integration, not autonomous agent search. Default traces and timing
are deliberately small; pass --full-workload for the task's default workload.
"""

import argparse
import json
import os
import shutil
from pathlib import Path

from prepare import HERE, prepare

from skydiscover.synthesize.spec import checkpoint, loop, proof
from skydiscover.synthesize.spec.paths import Run
from skydiscover.synthesize.spec.run import export_deliverable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--full-workload", action="store_true")
    parser.add_argument("--verus")
    parser.add_argument("--z3")
    args = parser.parse_args()
    output = args.output.resolve()
    options = (
        {} if args.full_workload else dict(load_count=128, run_count=1024, seconds=0.1, repeats=1)
    )
    control = prepare(
        output / "run",
        output / "trust",
        iterations=2,
        cycles=4,
        verus=args.verus,
        z3=args.z3,
        **options,
    )
    os.environ.update(
        SKYDISCOVER_PROOF_TRUST=control["trust"], SKYDISCOVER_PROOF_CONTRACT=control["contract"]
    )
    run = Run(output / "run")
    scores = []
    for iteration in range(2):
        loop.begin(run.path)
        loop.cycle(run.path)
        shutil.copy2(HERE / "candidates/baseline.rs", run.impl / "implementation.rs")
        with (run.impl / "implementation.rs").open("a") as stream:
            stream.write(f"\n// Replay fixture iteration {iteration + 1}.\n")
        row = proof.evaluate(run.path)
        scores.append(row["metrics"]["throughput_ops_per_sec"])
        # Best selection is checked on the frozen held-out draw; that row never becomes the score.
        held_out = proof.evaluate(run.path, draw="held-out")
        assert held_out["draw"] == "held-out" and row["draw"] == "scored"
        checkpoint.stamp_audit(run.path, [])
        checkpoint.snapshot_run(run.path, output, became_best=scores[-1] >= max(scores))
        print(f"Checkpoint {iteration + 1}: {scores[-1]} ops/s", flush=True)
    try:
        loop.begin(run.path)
    except ValueError:
        pass
    else:
        raise AssertionError("Iteration budget did not stop a third attempt")
    # Restore must preserve unfinished progress and reselect an eligible measured candidate.
    (run.impl / "implementation.rs").write_text("// unfinished redesign\n")
    checkpoint.restore_best(run.path)
    checkpoint.stamp_audit(run.path, [])
    run.report.write_text(
        "# Verus DB integration replay\nTwo verified, measured checkpoints; budget enforced; best restored. "
        "Supplied fixtures, not autonomous search. No remaining proof obligations.\n"
        + json.dumps(scores)
        + "\n"
    )
    print("\n".join(export_deliverable(run.path, output)))


if __name__ == "__main__":
    main()
