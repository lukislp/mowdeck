"""The fuzz targets in fuzz/ (docs/dev/fuzzing.md) are complete and replay cleanly.

ClusterFuzzLite builds every ``fuzz/fuzz_*.py`` (``.clusterfuzzlite/build.sh``); OpenSSF
Scorecard's Fuzzing check finds them by their ``import atheris`` line. Atheris itself is only
installed in the ClusterFuzzLite build image, so this test replaces it with a stand-in and
replays each target's seed corpus plus pseudo-random inputs: a crash a fuzzer found and the fix
of which added a seed stays fixed, and a target that no longer imports fails here first.
"""

import importlib
import random
import re
import sys
import types
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from pathlib import Path

import pytest

RANDOM_INPUTS = 300
FROM_RE = re.compile(r"^FROM\s+(\S+)", re.MULTILINE)
COPY_FROM_RE = re.compile(r"^COPY\s+--from=(\S+)", re.MULTILINE)
ENV_LINE_RE = re.compile(r"^(\w+_IMAGE)=(\S*)\s*$", re.MULTILINE)


def _fuzz_dir(repo_root: Path) -> Path:
    return repo_root / "fuzz"


def _target_names(repo_root: Path) -> list[str]:
    return sorted(path.stem for path in _fuzz_dir(repo_root).glob("fuzz_*.py"))


@pytest.fixture(scope="module")
def fake_atheris(repo_root: Path) -> Iterator[None]:
    """``atheris`` as a stand-in module, ``fuzz/`` importable; both undone afterwards."""
    fake = types.ModuleType("atheris")
    fake.instrument_imports = lambda *args, **kwargs: nullcontext()  # type: ignore[attr-defined]
    fake.Setup = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    fake.Fuzz = lambda: None  # type: ignore[attr-defined]
    real = sys.modules.get("atheris")
    sys.modules["atheris"] = fake
    sys.path.insert(0, str(_fuzz_dir(repo_root)))
    try:
        yield
    finally:
        sys.path.remove(str(_fuzz_dir(repo_root)))
        if real is None:
            sys.modules.pop("atheris", None)
        else:
            sys.modules["atheris"] = real


def test_there_are_fuzz_targets(repo_root: Path) -> None:
    assert len(_target_names(repo_root)) >= 8


def test_targets_follow_the_layout(repo_root: Path) -> None:
    for name in _target_names(repo_root):
        text = (_fuzz_dir(repo_root) / f"{name}.py").read_text("utf-8")
        # The exact line OpenSSF Scorecard looks for (checks/raw/fuzzing.go, Python: Atheris).
        assert re.search(r"^import atheris$", text, re.MULTILINE), name
        assert "with atheris.instrument_imports():" in text, name
        for function in ("def test_one_input(data: bytes) -> None:", "def seeds()", "def main()"):
            assert function in text, f"{name}: {function}"
        assert text.rstrip().endswith('if __name__ == "__main__":\n    main()'), name


def test_targets_are_documented(repo_root: Path) -> None:
    doc = (repo_root / "docs" / "dev" / "fuzzing.md").read_text("utf-8")
    for name in _target_names(repo_root):
        assert f"`{name}`" in doc, f"{name} is missing from docs/dev/fuzzing.md"


def test_build_image_uses_the_pinned_uv(repo_root: Path) -> None:
    dockerfile = (repo_root / ".clusterfuzzlite" / "Dockerfile").read_text("utf-8")
    env = (repo_root / "infra" / "compose" / ".env.example").read_text("utf-8")
    images = dict(ENV_LINE_RE.findall(env))
    (base,) = FROM_RE.findall(dockerfile)
    assert re.fullmatch(r"gcr\.io/oss-fuzz-base/base-builder-python@sha256:[0-9a-f]{64}", base)
    assert COPY_FROM_RE.findall(dockerfile) == [images["UV_IMAGE"]]


def test_build_script_builds_every_target(repo_root: Path) -> None:
    script = (repo_root / ".clusterfuzzlite" / "build.sh").read_text("utf-8")
    assert "for target in fuzz/fuzz_*.py" in script
    assert "compile_python_fuzzer" in script
    # The export is installed as is: hash-checked, no resolution, no project config (its
    # override-dependencies would add unpinned requirements).
    assert "uv pip install --no-deps --no-config --require-hashes" in script


@pytest.mark.usefixtures("fake_atheris")
def test_pattern_target_registers_every_built_in_pattern() -> None:
    from mowdeck_patterns import default_registry

    module = importlib.import_module("fuzz_patterns")
    assert default_registry().keys() == module.KEYS


@pytest.mark.parametrize("name", _target_names(Path(__file__).resolve().parents[3]))
@pytest.mark.usefixtures("fake_atheris")
def test_target_replays_seeds_and_random_inputs(name: str) -> None:
    module = importlib.import_module(name)
    run: Callable[[bytes], None] = module.test_one_input
    seeds: list[bytes] = module.seeds()
    assert seeds, f"{name} has no seeds"
    for seed in seeds:
        run(seed)
    rng = random.Random(name)  # noqa: S311 - reproducible test inputs, not cryptography
    for _ in range(RANDOM_INPUTS):
        seed = rng.choice(seeds)
        data = bytearray(seed)
        for _ in range(rng.randint(1, 8)):
            if data and rng.random() < 0.7:
                data[rng.randrange(len(data))] = rng.randrange(256)
            else:
                data.insert(rng.randrange(len(data) + 1), rng.randrange(256))
        run(bytes(data))
        run(rng.randbytes(rng.randint(0, 64)))
