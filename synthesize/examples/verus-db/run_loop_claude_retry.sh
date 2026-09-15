#!/usr/bin/env bash
# Run run_loop.sh repeatedly across Claude Code usage-limit windows.
#
# The wrapped loop remains the source of truth for progress. A failed session is
# retried only when its output says that Claude is rate/usage limited; all other
# failures are returned immediately for inspection.
#
# Configuration (environment variables):
#   CLAUDE_USAGE_WAIT_SECS       fallback wait when no reset time is reported (default 18000 = 5h)
#   CLAUDE_USAGE_MAX_RETRIES     0 means unlimited (default 0)
#   CLAUDE_USAGE_WAIT_ONCE       if set to 1, wait only once then stop (default 0)
#   CLAUDE_AGENT_TIMEOUT          lead-session watchdog in seconds (default 2147483647)
#
# Example:
#   bash synthesize/examples/verus-db/run_loop_claude_retry.sh 3 \
#     --run .skydiscover/verus-db-claude \
#     --trust-root outputs/verus-db-claude-contract \
#     --agent claude --model sonnet --permission-mode bypassPermissions

set -uo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
loop_script="$script_dir/run_loop.sh"

[[ -x "$loop_script" ]] || {
  echo "claude-retry: run_loop.sh is not executable: $loop_script" >&2
  exit 2
}

wait_secs="${CLAUDE_USAGE_WAIT_SECS:-18000}"
max_retries="${CLAUDE_USAGE_MAX_RETRIES:-0}"
wait_once="${CLAUDE_USAGE_WAIT_ONCE:-0}"
agent_timeout="${CLAUDE_AGENT_TIMEOUT:-2147483647}"

[[ "$wait_secs" =~ ^[1-9][0-9]*$ ]] || {
  echo "claude-retry: CLAUDE_USAGE_WAIT_SECS must be a positive integer" >&2
  exit 2
}
[[ "$max_retries" =~ ^[0-9]+$ ]] || {
  echo "claude-retry: CLAUDE_USAGE_MAX_RETRIES must be a nonnegative integer" >&2
  exit 2
}
[[ "$wait_once" == 0 || "$wait_once" == 1 ]] || {
  echo "claude-retry: CLAUDE_USAGE_WAIT_ONCE must be 0 or 1" >&2
  exit 2
}
[[ "$agent_timeout" =~ ^[1-9][0-9]*$ ]] || {
  echo "claude-retry: CLAUDE_AGENT_TIMEOUT must be a positive integer" >&2
  exit 2
}
(( $# > 0 )) || {
  echo "usage: $0 N [run_loop.sh options...]" >&2
  exit 2
}

if [[ "$1" == "-h" || "$1" == "--help" ]]; then
  exec "$loop_script" "$@"
fi

# run_loop.sh requires a positive watchdog value and has no disabled state. Use
# a multi-decade value by default; callers can override it with
# CLAUDE_AGENT_TIMEOUT. Appending makes this wrapper's setting win over an
# accidental --agent-timeout in the forwarded arguments.
set -- "$@" --agent-timeout "$agent_timeout"

usage_signal() {
  # Keep this deliberately focused on provider/session-limit language. A proof
  # obligation containing the ordinary word "limit" must not trigger a retry.
  local file="$1"
  grep -Eiq \
    'usage[[:space:]_-]*(limit|exhausted)|session[[:space:]]+limit|weekly[[:space:]]+limit|rate[[:space:]_-]*limit|rate[[:space:]]+limited|too[[:space:]]+many[[:space:]]+requests|quota[[:space:]]+(exceeded|exhausted)|you['"'"'’]?ve[[:space:]]+hit[[:space:]]+(your|the)[[:space:]]+limit|hit[[:space:]]+your[[:space:]]+limit|resets?_at|resets?[[:space:]]+(at|in)|try[[:space:]]+again[[:space:]]+(at|in)|HTTP/[0-9.]+[[:space:]]+429' \
    "$file"
}

reset_wait_secs() {
  local file="$1" text reset now wait hours minutes seconds
  text="$(cat "$file")"

  # Prefer the machine-readable field used by Claude Code status-line data.
  # Accept both seconds and millisecond Unix timestamps.
  reset="$(grep -Eo '"resets_at"[[:space:]]*:[[:space:]]*[0-9]{10,}' "$file" \
    | tail -1 | grep -Eo '[0-9]{10,}' || true)"
  if [[ -n "$reset" ]]; then
    if (( ${#reset} >= 13 )); then
      reset=$((reset / 1000))
    fi
    now="$(date +%s)"
    wait=$((reset - now + 5))
    (( wait > 0 )) && { printf '%s\n' "$wait"; return 0; }
  fi

  # Also understand common human-readable relative forms from CLI errors.
  if [[ "$text" =~ resets?[[:space:]]+in[[:space:]]+([0-9]+)[[:space:]]*hours?[[:space:]]*([0-9]+)?[[:space:]]*minutes? ]]; then
    hours="${BASH_REMATCH[1]}"
    minutes="${BASH_REMATCH[2]:-0}"
    printf '%s\n' $((hours * 3600 + minutes * 60 + 5))
    return 0
  fi
  if [[ "$text" =~ resets?[[:space:]]+in[[:space:]]+([0-9]+)[[:space:]]*hours? ]]; then
    printf '%s\n' $((BASH_REMATCH[1] * 3600 + 5))
    return 0
  fi
  if [[ "$text" =~ resets?[[:space:]]+in[[:space:]]+([0-9]+)[[:space:]]*minutes? ]]; then
    printf '%s\n' $((BASH_REMATCH[1] * 60 + 5))
    return 0
  fi

  return 1
}

countdown() {
  local remaining="$1" shown
  while (( remaining > 0 )); do
    if (( remaining >= 3600 )); then
      shown="$((remaining / 3600))h $(((remaining % 3600) / 60))m"
    elif (( remaining >= 60 )); then
      shown="$((remaining / 60))m $((remaining % 60))s"
    else
      shown="${remaining}s"
    fi
    printf '\rclaude-retry: next attempt in %-12s' "$shown" >&2
    sleep "$((remaining > 60 ? 60 : remaining))"
    remaining=$((remaining > 60 ? remaining - 60 : 0))
  done
  printf '\rclaude-retry: retrying now.%-20s\n' '' >&2
}

run_number=0
usage_retries=0

while true; do
  run_number=$((run_number + 1))
  capture="$(mktemp "${TMPDIR:-/tmp}/claude-loop.XXXXXX")"
  trap 'rm -f "$capture"; exit 130' INT TERM

  echo "claude-retry: starting loop invocation $run_number" >&2
  set +e
  "$loop_script" "$@" 2>&1 | tee "$capture"
  rc=${PIPESTATUS[0]}
  set -e

  if (( rc == 0 )); then
    rm -f "$capture"
    exit 0
  fi

  evidence="$(mktemp "${TMPDIR:-/tmp}/claude-loop-evidence.XXXXXX")"
  cp "$capture" "$evidence"

  # run_loop prints the durable launcher-log path before invoking Claude. Add
  # those logs to the evidence because the agent's output is redirected there.
  while IFS= read -r log_path; do
    [[ -f "$log_path" ]] && tail -240 "$log_path" >>"$evidence"
  done < <(grep -Eo '/[^[:space:]]+\.log' "$capture" | sort -u)

  if ! usage_signal "$evidence"; then
    echo "claude-retry: non-usage-limit failure (exit $rc); not retrying" >&2
    echo "claude-retry: launcher output: $capture" >&2
    rm -f "$evidence"
    exit "$rc"
  fi

  usage_retries=$((usage_retries + 1))
  if (( max_retries > 0 && usage_retries > max_retries )); then
    echo "claude-retry: usage-limit retry budget exhausted ($max_retries)" >&2
    exit "$rc"
  fi

  echo "claude-retry: Claude usage limit detected; progress is preserved" >&2
  if reset_wait="$(reset_wait_secs "$capture")"; then
    wait_source="captured reset time"
  else
    reset_wait="$(reset_wait_secs "$evidence" 2>/dev/null || true)"
    if [[ -n "$reset_wait" ]]; then
      wait_source="reported reset time"
    else
      reset_wait="$wait_secs"
      wait_source="fallback"
    fi
  fi
  echo "claude-retry: waiting ${reset_wait}s ($wait_source) before retry $usage_retries" >&2
  countdown "$reset_wait"
  rm -f "$evidence"

  if (( wait_once == 1 )); then
    echo "claude-retry: CLAUDE_USAGE_WAIT_ONCE=1; stopping after the wait" >&2
    exit "$rc"
  fi

  rm -f "$capture"
  trap - INT TERM
done
