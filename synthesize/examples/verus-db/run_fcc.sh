#!/usr/bin/env bash
# Launch the Verus database synthesis task through an already-running FCC server.
set -euo pipefail

example_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "$example_dir/../../.." && pwd)"
installer="$project_dir/synthesize/scripts/install.sh"
task="$example_dir/task.md"
dry_run=0
resume_run=""
claude_args=()

usage() {
  echo 'Usage: bash synthesize/examples/verus-db/run_fcc.sh [--dry-run] [--resume-run DIR] [-- CLAUDE_ARGS...]'
  echo 'Starts an interactive FCC Claude session with the database task already submitted.'
  echo 'Requires fcc-server to be running and configured in another terminal.'
  echo 'VERUS and VERUS_Z3_PATH override verifier and solver discovery.'
  echo '--dry-run checks prerequisites and prints the launch command without installing or launching.'
  echo '--resume-run DIR continues a specific existing SkySynth run from its files.'
}

fail() { echo "run_fcc: $*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=1; shift ;;
    --resume-run)
      [ "$#" -ge 2 ] || fail '--resume-run needs a directory'
      resume_run="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    --) shift; claude_args=("$@"); break ;;
    *) fail "unknown option '$1'; use -- before Claude Code options" ;;
  esac
done

for executable in python3 jq claude fcc-claude; do
  command -v "$executable" >/dev/null 2>&1 || fail "missing executable: $executable"
done
[ -f "$task" ] && [ -f "$installer" ] || fail 'cannot locate the task or SkySynth installer'

# Honor explicit settings first; otherwise use PATH or this machine's known locations.
if [ -z "${VERUS:-}" ]; then
  if command -v verus >/dev/null 2>&1; then
    VERUS="$(command -v verus)"
  elif [ -x "$HOME/verus/verus" ]; then
    VERUS="$HOME/verus/verus"
  else
    fail 'set VERUS to the Verus executable'
  fi
fi
VERUS="$(command -v "$VERUS")" || fail 'VERUS does not name an executable'
VERUS="$(cd "$(dirname "$VERUS")" && pwd)/$(basename "$VERUS")"
export VERUS

VERUS_Z3_PATH=$HOME/verus/z3
export VERUS_Z3_PATH
"$VERUS" --version
"$VERUS_Z3_PATH" -version

if [ -n "$resume_run" ]; then
  [ -d "$resume_run" ] || fail "run directory not found: $resume_run"
  resume_run="$(cd "$resume_run" && pwd)"
  [ -f "$resume_run/task.md" ] || fail "missing task.md in $resume_run"
  prompt="Use the /skysynth skill to continue the existing Verus database run at $resume_run. Read its task.md, plan, proof-log.md, and decision log; resume from disk. Complete verification, the report, and run finish as required by the formal workflow."
else
  prompt="/skysynth build and prove the database specified in $task. Follow the formal DSA/ISA workflow through verification, the report, and run finish."
fi

cd "$project_dir"
# Make source-checkout helpers available to agent subprocesses from run directories too.
export PYTHONPATH="$project_dir:$(dirname "$project_dir")${PYTHONPATH:+:$PYTHONPATH}"
export SKYDISCOVER_TEST_MAX_SECS="${SKYDISCOVER_TEST_MAX_SECS:-240}"
export SKYDISCOVER_SLOW_SECS="${SKYDISCOVER_SLOW_SECS:-600}"
python3 -c 'import skydiscover.synthesize.spec.paths' || fail 'cannot import SkySynth helpers'

printf 'Project: %s\nTask: %s\n' "$project_dir" "$task"
if [ "$dry_run" -eq 1 ]; then
  printf 'Setup: bash %q --agent claude %q\n' "$installer" "$project_dir"
  printf 'Launch: fcc-claude'
  if [ "${#claude_args[@]}" -gt 0 ]; then printf ' %q' "${claude_args[@]}"; fi
  printf ' %q\n' "$prompt"
  echo 'Dry run only; FCC connectivity is checked by fcc-claude at launch.'
  exit 0
fi

# Use the same skill, role, and delivery-hook installation as skydiscover init.
bash "$installer" --agent claude "$project_dir"
echo 'Launching through FCC. The session uses normal Claude permission prompts.'
if [ "${#claude_args[@]}" -gt 0 ]; then
  exec fcc-claude "${claude_args[@]}" "$prompt"
else
  exec fcc-claude "$prompt"
fi
