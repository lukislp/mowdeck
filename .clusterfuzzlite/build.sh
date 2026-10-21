#!/bin/bash -eu
# ClusterFuzzLite build script (docs/dev/fuzzing.md): one Atheris fuzzer binary per
# fuzz/fuzz_*.py, each with its generated seed corpus.
cd "$SRC/mowdeck"

# The third-party dependencies of the fuzzed packages, exactly as locked in uv.lock (with
# hashes). The workspace's own sources are not installed: PyInstaller finds them through
# --paths and bundles the modules a target imports.
uv export --frozen --no-dev --no-emit-workspace --no-header \
  --package agora-bridge \
  --package camera-relay \
  --package identity \
  --package mowdeck-auth \
  --package mowdeck-authz \
  --package mowdeck-contracts \
  --package mowdeck-control-mammotion \
  --package mowdeck-patterns \
  --package realtime \
  --package scheduler \
  --package vision \
  --output-file /tmp/fuzz-deps.txt
uv pip install --require-hashes -r /tmp/fuzz-deps.txt

srcs=(fuzz integrations/agora-bridge/src libs/*/src services/*/src)
paths=()
for src in "${srcs[@]}"; do
  paths+=(--paths "$src")
done

seeds="$WORK/seeds"
rm -rf "$seeds"
(IFS=:; PYTHONPATH="${srcs[*]}" python fuzz/seeds.py "$seeds")

build() {
  local target=$1 name
  name=$(basename -s .py "$target")
  # pymammotion reads its error code table (a CSV next to the code) at import time.
  compile_python_fuzzer "$target" "${paths[@]}" --collect-data pymammotion
  cp fuzz/json.dict "$OUT/$name.dict"
  if [ -d "$seeds/$name" ]; then
    (cd "$seeds/$name" && zip -q -r "$OUT/${name}_seed_corpus.zip" .)
  fi
}

# PyInstaller takes most of the build, so the targets are built in parallel, each with its own
# work directory (compile_python_fuzzer works in $SRC/pyfuzzworkdir). Coverage builds stay
# sequential: compile_python_fuzzer rewrites the target in place and shares /medio.
if [[ $SANITIZER = *coverage* ]]; then
  for target in fuzz/fuzz_*.py; do
    build "$target"
  done
else
  pids=()
  for target in fuzz/fuzz_*.py; do
    work="$WORK/pyinstaller/$(basename -s .py "$target")"
    mkdir -p "$work"
    SRC="$work" build "$target" > "$work.log" 2>&1 &
    pids+=($!)
  done
  failed=0
  for pid in "${pids[@]}"; do
    wait "$pid" || failed=1
  done
  if [ "$failed" -ne 0 ]; then
    cat "$WORK"/pyinstaller/*.log
    exit 1
  fi
fi
