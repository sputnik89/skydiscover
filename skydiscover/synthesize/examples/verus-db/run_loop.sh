#!/usr/bin/env bash
# Prepare (or resume) a scored verus-db run and drive the synthesis loop for N scored iterations.
#
#   bash skydiscover/synthesize/examples/verus-db/run_loop.sh N \
#     --run .skydiscover/verus-db-throughput --trust-root outputs/verus-db-throughput-contract
#
# First launch prepares the run: it stages the immutable spec, checker, and benchmark; pins the
# Verus/Z3/Rust/Python toolchain by hash; generates the scored and held-out traces with
# single-machine-kvstore's generator; writes proof.json, workload.json, and the workload card;
# freezes the contract; and saves the anchor to <trust-root>.controller.json.
#
# Each iteration is one lead agent session that completes exactly one scored iteration
# (planner -> DSA/ISA -> proof evaluate, scored and held-out -> checkpoint -> audit -> critic).
# Between sessions this script checks the budget record and the trust bundle, and stops on a reset,
# an overrun, a changed bundle, or a session that made no counted progress. After N scored
# iterations one more session prepares delivery, and `run finish --export-to` publishes the result.
#
# The loop itself is this script. It calls only the framework's commands (spec.proof freeze,
# spec.loop init/status, spec.run finish) and the reused trace generator.
set -euo pipefail

usage() {
  cat <<'EOF'
usage: run_loop.sh N --run DIR --trust-root DIR [options]

  N                    total scored iterations (the run's budget, not N more on resume)
  --run DIR            run directory; prepared here when it does not exist yet
  --trust-root DIR     frozen contract location, outside the run (created on first launch)

Budget:
  --cycles M           total DSA cycles (new run default: 10*N)
  --wall-secs S        construction time limit in seconds (default: 0, none)

Workload (new runs; on resume they must match the frozen values):
  --load-count N       preloaded keys (default 1000000)
  --run-count N        run keys per draw (default 2000000)
  --seconds S          seconds per timed trial (default 30)
  --repeats N          trials per measurement (default 3)
  --seed N             scored draw seed (default 211)
  --held-out-seed N    held-out draw seed (default 223)
  --seed-from DIR      copy an existing candidate into the new run as an unverified seed

Toolchain:
  --verus PATH         Verus executable (default: $VERUS, else verus on PATH)
  --z3 PATH            Z3 executable (default: $VERUS_Z3_PATH, else z3 beside Verus)

Agent:
  --agent NAME         claude (default) or fcc-claude
  --model NAME         model passed to the agent
  --agent-timeout S    seconds per lead session (default 3600)

Other:
  --export-to DIR      where run finish publishes outputs/synthesize/ (default: .)
  --prepare-only       prepare or validate the run, then stop before any agent session
  --dry-run            print the plan and the first prompt; change nothing
  -h, --help           show this help

Environment: PYTHON selects the interpreter (default python3); it needs NumPy and skydiscover.
EOF
}

die() {
  echo "run_loop: $*" >&2
  exit 1
}

example="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
kit="$(cd "$example/../.." && pwd)"
python="${PYTHON:-python3}"
command -v "$python" >/dev/null || die "Python not found: $python"
command -v jq >/dev/null || die "jq is required"
py_bin="$(realpath "$(command -v "$python")")"
# The framework commands import skydiscover.synthesize from the kit's grandparent.
export PYTHONPATH="$(cd "$kit/../.." && pwd):$(cd "$kit/.." && pwd)${PYTHONPATH:+:$PYTHONPATH}"

iterations=""
run=""
trust=""
cycles=""
wall_secs=""
load_count=""
run_count=""
seconds=""
repeats=""
seed=""
held_out_seed=""
seed_from=""
verus_arg=""
z3_arg=""
agent="claude"
model=""
agent_timeout="3600"
export_to="."
prepare_only=0
dry_run=0

need() { [[ $# -ge 2 && -n "$2" ]] || die "$1 needs a value"; }
while (($#)); do
  case "$1" in
    -h | --help) usage; exit 0 ;;
    --run) need "$@"; run="$2"; shift 2 ;;
    --trust-root) need "$@"; trust="$2"; shift 2 ;;
    --cycles) need "$@"; cycles="$2"; shift 2 ;;
    --wall-secs) need "$@"; wall_secs="$2"; shift 2 ;;
    --load-count) need "$@"; load_count="$2"; shift 2 ;;
    --run-count) need "$@"; run_count="$2"; shift 2 ;;
    --seconds) need "$@"; seconds="$2"; shift 2 ;;
    --repeats) need "$@"; repeats="$2"; shift 2 ;;
    --seed) need "$@"; seed="$2"; shift 2 ;;
    --held-out-seed) need "$@"; held_out_seed="$2"; shift 2 ;;
    --seed-from) need "$@"; seed_from="$2"; shift 2 ;;
    --verus) need "$@"; verus_arg="$2"; shift 2 ;;
    --z3) need "$@"; z3_arg="$2"; shift 2 ;;
    --agent) need "$@"; agent="$2"; shift 2 ;;
    --model) need "$@"; model="$2"; shift 2 ;;
    --agent-timeout) need "$@"; agent_timeout="$2"; shift 2 ;;
    --export-to) need "$@"; export_to="$2"; shift 2 ;;
    --prepare-only) prepare_only=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    -*) usage >&2; die "unknown option: $1" ;;
    *) [[ -z "$iterations" ]] || die "unexpected argument: $1"; iterations="$1"; shift ;;
  esac
done

positive='^[1-9][0-9]*$'
nonnegative='^[0-9]+$'
[[ -n "$iterations" && -n "$run" && -n "$trust" ]] || { usage >&2; die "N, --run, and --trust-root are required"; }
[[ "$iterations" =~ $positive ]] || die "N must be a positive integer: $iterations"
[[ -z "$cycles" || "$cycles" =~ $positive ]] || die "--cycles must be a positive integer"
[[ -z "$wall_secs" || "$wall_secs" =~ $nonnegative ]] || die "--wall-secs must be a nonnegative integer"
[[ -z "$load_count" || ("$load_count" =~ $positive && "$load_count" -ge 2) ]] || die "--load-count must be an integer >= 2"
[[ -z "$run_count" || "$run_count" =~ $positive ]] || die "--run-count must be a positive integer"
[[ -z "$repeats" || "$repeats" =~ $positive ]] || die "--repeats must be a positive integer"
[[ -z "$seed" || "$seed" =~ $nonnegative ]] || die "--seed must be a nonnegative integer"
[[ -z "$held_out_seed" || "$held_out_seed" =~ $nonnegative ]] || die "--held-out-seed must be a nonnegative integer"
if [[ -n "$seconds" ]]; then
  [[ "$seconds" =~ ^([0-9]+\.?[0-9]*|\.[0-9]+)$ ]] && awk -v s="$seconds" 'BEGIN { exit !(s > 0) }' \
    || die "--seconds must be a positive number"
fi
[[ "$agent_timeout" =~ $positive ]] || die "--agent-timeout must be a positive integer"
[[ "$agent" == claude || "$agent" == fcc-claude ]] || die "--agent must be claude or fcc-claude"

# Resolve the parent physically (symlinks included) so the same path always compares equal.
absolute() {
  local parent
  parent="$(cd -P "$(dirname "$1")" 2>/dev/null && pwd -P)" || die "parent directory does not exist: $(dirname "$1")"
  printf '%s/%s\n' "$parent" "$(basename "$1")"
}
run="$(absolute "$run")"
trust="$(absolute "$trust")"
case "$trust/" in "$run"/*) die "--trust-root must be outside the run directory" ;; esac
case "$run/" in "$trust"/*) die "the run directory must not be inside --trust-root" ;; esac
controller="$trust.controller.json"

interface="$run/synthesis/evaluator/interface"
proof_config="$run/synthesis/evaluator/proof.json"
loop_state="$run/synthesis/loop.json"

fw() { "$python" -m "skydiscover.synthesize.spec.$1" "${@:2}"; }

# A plain content hash of the trust bundle, recorded at freeze and rechecked between sessions.
# The framework's own anchor check still runs inside every evaluation, checkpoint, and export.
bundle_digest() {
  (cd "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 shasum -a 256 | shasum -a 256 | cut -d' ' -f1)
}

# "hash  path" lines on stdin -> {"path": "hash"}.
hashes_to_json() {
  jq -R -n '[inputs | capture("^(?<h>[0-9a-f]{64}) [ *](?<p>.*)$") | {(.p): .h}] | add // {}'
}

pin_toolchain() {
  local verus_bin solver_name solver version_json toolchain rustc z3_version files_json
  verus_bin="$(command -v "${verus_arg:-${VERUS:-verus}}")" || die "Verus not found; set VERUS or --verus"
  verus_bin="$(realpath "$verus_bin")"
  solver_name="${z3_arg:-${VERUS_Z3_PATH:-$(dirname "$verus_bin")/z3}}"
  solver="$(realpath "$(command -v "$solver_name" || echo "$solver_name")" 2>/dev/null)" \
    || die "Z3 not found at $solver_name; pass --z3"
  [[ -f "$solver" && -x "$solver" ]] || die "Z3 not found at $solver; pass --z3"
  version_json="$("$verus_bin" --version --output-json)"
  toolchain="$(jq -r .verus.toolchain <<<"$version_json")"
  rustc="$(realpath "$(rustup which --toolchain "$toolchain" rustc)")" || die "rustup has no $toolchain toolchain"
  z3_version="$("$solver" --version)"
  files_json="$(
    {
      find "$(dirname "$verus_bin")" -maxdepth 1 -type f \( -name verus -o -name rust_verify -o -name vstd.vir \
        -o -name '*.rlib' -o -name '*.dylib' -o -name '*.so' \) -print0
      printf '%s\0' "$solver" "$rustc" "$py_bin"
      find "$(dirname "$(dirname "$rustc")")/lib" -type f \( -name '*.rlib' -o -name '*.dylib' -o -name '*.so' \) -print0
    } | LC_ALL=C sort -zu | xargs -0 shasum -a 256 | hashes_to_json
  )"
  jq -n --arg verus "$verus_bin" --arg z3 "$solver" --argjson verus_version "$version_json" \
    --arg z3_version "$z3_version" --argjson files "$files_json" \
    '{verus: $verus, z3: $z3, verus_version: $verus_version, z3_version: $z3_version, files: $files}'
}

prepare_run() {
  load_count="${load_count:-1000000}"
  run_count="${run_count:-2000000}"
  seconds="${seconds:-30}"
  repeats="${repeats:-3}"
  seed="${seed:-211}"
  held_out_seed="${held_out_seed:-223}"
  [[ "$held_out_seed" != "$seed" ]] || die "the held-out draw needs a seed different from --seed"
  [[ -z "$seed_from" || -d "$seed_from" ]] || die "--seed-from is not a directory: $seed_from"
  local toolchain_json
  toolchain_json="$(pin_toolchain)"

  echo "Preparing $run"
  mkdir -p "$run/specification/cards" "$run/synthesis/evaluator" "$run/synthesis/impl" "$run/review"
  cp "$example/task.md" "$run/task.md"
  rsync -a --exclude __pycache__ "$example/evaluator/interface/" "$interface/"
  rsync -a --exclude __pycache__ "$example/evaluator/tests/" "$run/synthesis/tests/"
  printf '%s\n' "$toolchain_json" >"$interface/toolchain.json"
  local generator="$kit/examples/single-machine-kvstore/evaluator"
  mkdir -p "$interface/trace-generator"
  cp "$generator/generate.py" "$interface/trace-generator/generate.py"
  rsync -a --exclude __pycache__ "$generator/generators/" "$interface/trace-generator/generators/"

  # Two frozen draws from the same generator: the scored trace every checkpoint is measured on,
  # and a held-out trace measured only before a new best is recorded.
  local draws_json='{}' draw draw_seed outdir meta
  for draw in scored held-out; do
    draw_seed="$seed"
    [[ "$draw" == scored ]] || draw_seed="$held_out_seed"
    outdir="$interface/traces/$draw"
    PYTHONDONTWRITEBYTECODE=1 "$python" "$interface/trace-generator/generate.py" zipf --theta 0.99 \
      --load-count "$load_count" --run-count "$run_count" --seed "$draw_seed" --outdir "$outdir"
    meta=("$outdir"/*.meta.json)
    [[ ${#meta[@]} -eq 1 && -f "${meta[0]}" ]] || die "expected one trace metadata file in $outdir"
    # Generator metadata uses absolute paths; frozen artifacts must relocate.
    jq '.load_file |= (split("/") | last) | .run_file |= (split("/") | last)' "${meta[0]}" >"${meta[0]}.tmp"
    mv "${meta[0]}.tmp" "${meta[0]}"
    draws_json="$(jq --arg draw "$draw" --argjson seed "$draw_seed" --slurpfile meta "${meta[0]}" \
      '.[$draw] = {seed: $seed, operation_seed: ($seed + 2),
                   load_file: ($draw + "/" + $meta[0].load_file), run_file: ($draw + "/" + $meta[0].run_file)}' \
      <<<"$draws_json")"
  done

  local timeout workload_text
  timeout="$(awk -v r="$repeats" -v s="$seconds" 'BEGIN { t = int(r * (s + 120)) + 60; print (t > 600 ? t : 600) }')"
  workload_text="$load_count shuffled keys as fixed-width decimal strings; $run_count scrambled Zipf(theta=0.99) trace, seed $seed (held-out draw: seed $held_out_seed); 50:50 get/put with i32 values; median of $repeats fresh ${seconds}s trials"
  jq --arg py "$py_bin" --arg workload "$workload_text" --argjson timeout "$timeout" \
    --argjson load_count "$load_count" --argjson run_count "$run_count" --argjson seconds "$seconds" \
    --argjson repeats "$repeats" --argjson seed "$seed" --argjson held_out_seed "$held_out_seed" \
    '.config += {load_count: $load_count, run_count: $run_count, seconds: $seconds, repeats: $repeats,
                 seed: $seed, held_out_seed: $held_out_seed}
     | .workload = $workload | .timeout = $timeout
     | reduce ("toolchain", "build", "benchmark", "held_out_benchmark") as $field (.; .[$field][0] = $py)' \
    "$example/evaluator/proof.json" >"$proof_config"

  local hashes_json
  hashes_json="$(cd "$interface/traces" && find scored held-out -name '*.dat' -print0 | LC_ALL=C sort -z \
    | xargs -0 shasum -a 256 | hashes_to_json)"
  jq -n --slurpfile proof "$proof_config" --argjson draws "$draws_json" --argjson hashes "$hashes_json" \
    '$proof[0].config + {draws: $draws, trace_sha256: $hashes}' >"$interface/workload.json"
  # The workload card the planner and evaluator read, with the configuration this run froze.
  jq --slurpfile proof "$proof_config" '.scored_configuration = $proof[0].config' \
    "$example/spec/workload.json" >"$run/specification/cards/workload.json"

  if [[ -n "$seed_from" ]]; then
    rsync -a --exclude __pycache__ "$seed_from/" "$run/synthesis/impl/"
  fi
  printf '%s\n' "# Scored database run" \
    "Preserve the Database contract. Resolve all proof obligations, then measure throughput and checkpoint. Improve the representation using measured feedback." \
    >"$run/synthesis/plan.md"

  echo "Freezing the contract into $trust"
  contract="$(fw proof freeze "$run" --trust-root "$trust" | jq -r .SKYDISCOVER_PROOF_CONTRACT)"
  [[ "$contract" == sha256:* ]] || die "freeze did not return a contract digest"
  cycles="${cycles:-$((10 * iterations))}"
  wall_secs="${wall_secs:-0}"
  fw loop init "$run" --iterations "$iterations" --cycles "$cycles" --wall-secs "$wall_secs" >/dev/null
  (
    umask 077
    jq -n --arg run "$run" --arg trust "$trust" --arg contract "$contract" \
      --argjson iterations "$iterations" --argjson cycles "$cycles" --argjson wall_secs "$wall_secs" \
      --arg bundle "$(bundle_digest "$trust")" \
      '{run: $run, trust: $trust, contract: $contract, iterations: $iterations, cycles: $cycles,
        wall_secs: $wall_secs, bundle_sha256: $bundle}' >"$controller"
  )
  echo "Saved the anchor to $controller (keep it outside worker access)"
}

resume_run() {
  [[ -z "$seed_from" ]] || die "--seed-from is for new runs only"
  [[ -d "$trust" ]] || die "controller record exists but the trust bundle is missing: $trust"
  [[ "$(jq -r .run "$controller")" == "$run" ]] || die "controller record belongs to another run: $controller"
  [[ "$(jq -r .trust "$controller")" == "$trust" ]] || die "controller record names another trust root"
  [[ "$(jq -r .iterations "$controller")" == "$iterations" ]] \
    || die "resume must keep N=$(jq -r .iterations "$controller") (got $iterations)"
  local saved
  saved="$(jq -r .cycles "$controller")"
  [[ -z "$cycles" || "$cycles" == "$saved" ]] || die "resume must keep --cycles $saved"
  cycles="$saved"
  saved="$(jq -r .wall_secs "$controller")"
  [[ -z "$wall_secs" || "$wall_secs" == "$saved" ]] || die "resume must keep --wall-secs $saved"
  wall_secs="$saved"
  local name value frozen
  for name in load_count run_count seconds repeats seed held_out_seed; do
    value="${!name}"
    [[ -n "$value" ]] || continue
    frozen="$(jq -r --arg name "$name" '.config[$name]' "$proof_config")"
    awk -v a="$value" -v b="$frozen" 'BEGIN { exit !(a + 0 == b + 0) }' \
      || die "cannot change the frozen workload on resume: --${name//_/-} is $frozen"
  done
  for name in verus z3; do
    value="$([[ $name == verus ]] && echo "$verus_arg" || echo "$z3_arg")"
    [[ -n "$value" ]] || continue
    frozen="$(jq -r --arg name "$name" '.[$name]' "$interface/toolchain.json")"
    [[ "$(realpath "$(command -v "$value" || echo "$value")")" == "$frozen" ]] \
      || die "cannot change the frozen toolchain on resume: --$name is $frozen"
  done
  contract="$(jq -r .contract "$controller")"
  echo "Resuming $run with the saved anchor"
}

check_bundle() {
  [[ "$(bundle_digest "$trust")" == "$(jq -r .bundle_sha256 "$controller")" ]] \
    || die "the trust bundle changed since it was frozen; stopping"
}

# Budget record fields: scored iterations, cycles, active attempt, limits, start time.
status_json() { fw loop status "$run"; }

iteration_prompt() {
  local st="$1"
  cat <<EOF
Use the SkySynth workflow at $kit/workflow/SKILL.md.
Continue the prepared scored proof run at $run; do not create a new run or change task.md.
Read its plan, proof strategy/log, decision log, synthesis/loop.json, and specification/cards/workload.json before acting.
The trusted contract is already frozen. Retain the inherited external anchor values.
Complete exactly ONE further scored iteration, then return to this controller.
Current scores: $(jq -r .scored_iterations <<<"$st")/$iterations; DSA cycles: $(jq -r .cycles <<<"$st")/$cycles.
Do not reset or edit the budget record. Use loop begin only if there is no active attempt;
resume an active attempt otherwise. Reserve loop cycle before each DSA substep. Use isolated
DSA workers and ISA on stalls, preserving the pinned theorem, checker, and benchmark.
The evaluator must run proof evaluate, then proof evaluate --draw held-out, then checkpoint snapshot
(including regressions), passing --became-best only when the candidate also wins on the held-out draw,
followed by the required audit decision and critic feedback. Keep the best candidate selected.
If construction fails, record loop fail with the remaining obligation. On budget exhaustion,
save progress and return; never fabricate a score. Do not start another scored iteration or
perform final delivery in this invocation. Report which checkpoint was completed or why none was.
EOF
}

finalize_prompt() {
  cat <<EOF
Follow $kit/workflow/SKILL.md for final delivery of $run.
The run has reached $iterations scored iterations. Do not begin any new attempt or DSA cycle.
Preserve unfinished work, restore the selected best with checkpoint restore-best if needed,
perform the final audit and stamp it, and write report.md with counts, stop reason, and obligations.
Keep the run directory for replay. Return after preparing final delivery; this controller will
independently verify and export it. Do not change the frozen contract or budget.
EOF
}

agent_command=("$agent" -p --output-format text --plugin-dir "$kit/workflow")
[[ -z "$model" ]] || agent_command+=(--model "$model")

agent_pgid=""
watchdog_pgid=""
cleanup() {
  [[ -z "$agent_pgid" ]] || kill -TERM -- "-$agent_pgid" 2>/dev/null || true
  [[ -z "$watchdog_pgid" ]] || kill -TERM -- "-$watchdog_pgid" 2>/dev/null || true
}
trap 'cleanup; exit 130' INT TERM

# Run one lead session with the prompt on stdin, in its own process group, killed on timeout.
invoke() {
  local label="$1" prompt="$2" logs log rc timed_out
  logs="$run/synthesis/launcher"
  mkdir -p "$logs"
  log="$(mktemp "$logs/$label-XXXXXX")"
  mv "$log" "$log.log"
  log="$log.log"
  printf '%s\n' "$prompt" >"${log%.log}.prompt.md"
  timed_out="${log%.log}.timed-out"
  echo "$label: agent output -> $log"
  set -m
  (cd "$run" && SKYDISCOVER_RUN="$run" SKYDISCOVER_PROOF_TRUST="$trust" SKYDISCOVER_PROOF_CONTRACT="$contract" \
    exec "${agent_command[@]}" <"${log%.log}.prompt.md" >"$log" 2>&1) &
  agent_pgid=$!
  (sleep "$agent_timeout"; : >"$timed_out"; kill -TERM -- "-$agent_pgid" 2>/dev/null; sleep 5; kill -KILL -- "-$agent_pgid" 2>/dev/null) &
  watchdog_pgid=$!
  set +m
  rc=0
  wait "$agent_pgid" || rc=$?
  kill -TERM -- "-$watchdog_pgid" 2>/dev/null || true
  wait "$watchdog_pgid" 2>/dev/null || true
  agent_pgid=""
  watchdog_pgid=""
  if [[ -e "$timed_out" ]]; then
    die "agent exceeded ${agent_timeout}s; progress kept. See $log"
  fi
  ((rc == 0)) || die "agent exited $rc; progress kept. See $log"
}

# ---- prepare or resume ------------------------------------------------------------------------
if [[ -f "$controller" ]]; then
  [[ -d "$run" ]] || die "controller record exists but the run is missing: $run"
  resume_run
elif [[ -e "$run" || -e "$trust" ]]; then
  die "use fresh --run and --trust-root paths, or keep <trust-root>.controller.json to resume"
elif ((dry_run)); then
  cycles="${cycles:-$((10 * iterations))}"
  wall_secs="${wall_secs:-0}"
  echo "Would prepare $run, freeze it into $trust, and save the anchor to $controller"
  echo "Budget: $iterations scored iterations, $cycles DSA cycles, wall limit ${wall_secs}s"
  echo "Launch: ${agent_command[*]}"
  iteration_prompt "$(jq -n '{scored_iterations: 0, cycles: 0}')"
  echo "Dry run: no files changed."
  exit 0
else
  prepare_run
fi

export SKYDISCOVER_PROOF_TRUST="$trust" SKYDISCOVER_PROOF_CONTRACT="$contract"
check_bundle
status="$(status_json)"
jq -e --argjson i "$iterations" --argjson c "$cycles" --argjson w "$wall_secs" \
  '.limits == {iterations: $i, cycles: $c, wall_secs: $w}' <<<"$status" >/dev/null \
  || die "the recorded budget $(jq -c .limits <<<"$status") differs from the controller record"
started="$(jq -r .started <<<"$status")"

if ((dry_run)); then
  echo "Budget: $(jq -c .limits <<<"$status"); scored $(jq -r .scored_iterations <<<"$status")/$iterations"
  echo "Launch: ${agent_command[*]}"
  if (($(jq -r .scored_iterations <<<"$status") < iterations)); then
    iteration_prompt "$status"
  else
    echo "The scored budget is complete; only final delivery remains."
  fi
  echo "Dry run: no files changed."
  exit 0
fi
if ((prepare_only)); then
  echo "Prepared: $run (controller: $controller)"
  exit 0
fi
command -v "$agent" >/dev/null || die "missing agent executable: $agent"

# ---- the loop ---------------------------------------------------------------------------------
while true; do
  before="$(status_json)"
  jq -e --argjson s "$started" --argjson i "$iterations" --argjson c "$cycles" --argjson w "$wall_secs" \
    '.started == $s and .limits == {iterations: $i, cycles: $c, wall_secs: $w}' <<<"$before" >/dev/null \
    || die "the agent changed the persisted budget; refusing to continue"
  scored="$(jq -r .scored_iterations <<<"$before")"
  used="$(jq -r .cycles <<<"$before")"
  ((used <= cycles)) || die "the DSA cycle limit was exceeded"
  echo "Progress: $scored/$iterations scored, $used/$cycles DSA cycles"
  if ((scored >= iterations)); then
    ((scored == iterations)) || die "the scored iteration limit was exceeded"
    break
  fi
  exhausted=0
  ((used < cycles)) || exhausted=1
  if ((wall_secs > 0)) && awk -v now="$(date +%s)" -v s="$started" -v w="$wall_secs" 'BEGIN { exit !(now - s >= w) }'; then
    exhausted=1
  fi
  if ((exhausted)) && [[ "$(jq -r .active <<<"$before")" == null ]]; then
    die "construction budget exhausted at $scored/$iterations scored iterations; progress and best candidate kept"
  fi
  invoke "iteration-$((scored + 1))" "$(iteration_prompt "$before")"
  check_bundle
  after="$(status_json)"
  after_scored="$(jq -r .scored_iterations <<<"$after")"
  after_used="$(jq -r .cycles <<<"$after")"
  ((after_scored >= scored && after_used >= used)) || die "the agent reset a progress counter"
  ((after_scored > scored || after_used > used)) \
    || die "agent made no counted progress; stopping instead of retrying. See $run/synthesis/launcher/"
done

# ---- delivery ---------------------------------------------------------------------------------
# Finalization consumes no scored iteration; run finish independently verifies the export.
invoke finalize "$(finalize_prompt)"
check_bundle
final="$(status_json)"
jq -e --argjson s "$started" --argjson i "$iterations" --argjson c "$cycles" --argjson w "$wall_secs" \
  --argjson used "$used" \
  '.started == $s and .limits == {iterations: $i, cycles: $c, wall_secs: $w}
   and .scored_iterations == $i and .cycles == $used' <<<"$final" >/dev/null \
  || die "finalization changed the iteration/cycle budget or count"
fw run finish "$run" --export-to "$export_to"
echo "Completed $iterations/$iterations scored iterations. Run and logs kept at $run"
