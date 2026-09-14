"""Exercise real verification, measurement, feedback, checkpoints, and final export.

This deterministic fixture supplies candidate designs; it does not impersonate DSA/ISA agents.
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from skydiscover.synthesize.spec import checkpoint, loop, proof
from skydiscover.synthesize.spec.paths import Run
from skydiscover.synthesize.spec.run import export_deliverable

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--isolation", choices=["sandbox", "external"], default="sandbox")
    args = ap.parse_args()
    root = args.output.resolve()
    if root.exists():
        raise ValueError("Use a fresh output directory; replay never overwrites an existing run")
    run = Run(root / "run").create()
    shutil.copy2(HERE / "task.md", run.task)
    shutil.copytree(HERE / "evaluator/interface", run.interface)
    shutil.copytree(HERE / "evaluator/tests", run.tests)
    shutil.copy2(HERE / "evaluator/proof.json", run.proof_config)
    run.impl.mkdir(exist_ok=True)
    trust = root / "trusted"
    anchor = proof.freeze(run.path, trust, isolation=args.isolation)
    os.environ["SKYDISCOVER_PROOF_TRUST"] = str(trust)
    os.environ["SKYDISCOVER_PROOF_CONTRACT"] = anchor
    loop.initialize(run.path, 2, 6)
    scores = []
    for name in ("subtract", "modulo"):
        attempt = loop.begin(run.path)
        # Two reserved construction substeps model code/proof progress for the fixture.
        first = loop.cycle(run.path)
        shutil.copy2(HERE / "candidates" / f"{name}.rs", run.impl / "body.rs")
        second = loop.cycle(run.path)
        row = proof.evaluate(run.path)
        scores.append(row["metrics"]["latency_ns"])
        checkpoint.stamp_audit(run.path, [])  # fixed fixture contract reviewed with this example
        _, cp = checkpoint.snapshot_run(run.path, root, became_best=scores[-1] <= min(scores))
        with run.proof_log.open("a") as log:
            log.write(
                f"Attempt {attempt}, cycles {first['cycle']}/{second['cycle']}: {name}; full proof passed; {cp.name}\n"
            )
        (run.synthesis / "plan.md").write_text(
            f"# Fixture feedback\n{name}: {scores[-1]} ns. "
            "Repeated subtraction executes a loop per input; next try constant-work modulo under the same theorem.\n"
        )
    checkpoint.restore_best(run.path)
    checkpoint.stamp_audit(run.path, [])
    run.report.write_text(
        "# Scored formal replay\n2/2 scored iterations; 4/6 reserved DSA cycles. "
        "Stopped at the scored budget. Both universal parity proofs passed. No open obligations. "
        "Candidates are supplied fixtures, not autonomous agent search.\n"
        + json.dumps({"latency_ns": scores, "contract": anchor})
        + "\n"
    )
    print("\n".join(export_deliverable(run.path, root)))
    print(run.report.read_text())


if __name__ == "__main__":
    main()
