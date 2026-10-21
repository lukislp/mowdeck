# Fuzz targets

Atheris fuzz targets of the Python code, one module `fuzz_<name>.py` per target, built and run
by ClusterFuzzLite (`.clusterfuzzlite/`, `.github/workflows/fuzzing.yml`). What each target
checks, how to run one locally and how to reproduce a crash: [docs/dev/fuzzing.md](../docs/dev/fuzzing.md).

- `harness.py`: shared helpers (standard library only).
- `seeds.py`: writes every target's seed corpus (`python fuzz/seeds.py OUT_DIR`).
- `json.dict`: the libFuzzer dictionary of all targets.
