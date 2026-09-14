#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
: "${SKYDISCOVER_IMPL:?set SKYDISCOVER_IMPL to the candidate directory}"
export SKYDISCOVER_INTERFACE="${SKYDISCOVER_INTERFACE:-$(cd .. && pwd)}"
if [ "$#" -eq 0 ]; then set -- proof.py; fi
for test in "$@"; do
  case "$test" in
    proof.py) python3 proof.py ;;
    *) echo "FAIL: unknown test: $test" >&2; exit 1 ;;
  esac
done
