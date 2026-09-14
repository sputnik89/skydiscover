#!/usr/bin/env bash
set -euo pipefail
# Every invocation verifies the pinned full postcondition, even if a caller names a subtest.
python3 "$SKYDISCOVER_INTERFACE/driver.py" verify "$SKYDISCOVER_IMPL"
