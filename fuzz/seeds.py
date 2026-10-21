"""Write the seed corpus of every fuzz target: ``python fuzz/seeds.py OUT_DIR``.

Each ``fuzz_<name>.py`` builds its seeds with the code it fuzzes (``seeds()``), so the corpus
follows the formats as they change; the files land in ``OUT_DIR/fuzz_<name>/``.
"""

import hashlib
import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def targets() -> list[str]:
    """The module names of the fuzz targets."""
    return sorted(path.stem for path in HERE.glob("fuzz_*.py"))


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: seeds.py OUT_DIR", file=sys.stderr)
        return 2
    out = Path(argv[0])
    sys.path.insert(0, str(HERE))
    for name in targets():
        module = importlib.import_module(name)
        folder = out / name
        folder.mkdir(parents=True, exist_ok=True)
        for seed in module.seeds():
            (folder / hashlib.sha1(seed, usedforsecurity=False).hexdigest()).write_bytes(seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
