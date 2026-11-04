"""Chart packages in a fixed entry order (tools/repo-checks/scripts/normalize_chart.py).

Helm 4 writes the subcharts' files in map order, so two packages of the same chart differ; after
normalize_chart.py they are byte-identical and still hold the same files.
"""

from __future__ import annotations

import gzip
import importlib.util
import io
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import yaml

FILES = {
    "mowdeck/Chart.yaml": b"apiVersion: v2\nname: mowdeck\nversion: 1.0.0\n",
    "mowdeck/values.yaml": b"web: {}\n",
    "mowdeck/charts/nats/Chart.yaml": b"name: nats\n",
    "mowdeck/charts/nats/values.yaml": b"a: 1\n",
    "mowdeck/charts/openfga/Chart.yaml": b"name: openfga\n",
    "mowdeck/charts/openfga/values.yaml": b"b: 2\n",
}


def _load(repo_root: Path) -> ModuleType:
    path = repo_root / "tools" / "repo-checks" / "scripts" / "normalize_chart.py"
    spec = importlib.util.spec_from_file_location("normalize_chart", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _package(path: Path, order: list[str], gzip_time: int) -> None:
    """A chart package as Helm writes it: one time stamp for every entry, gzip with a time."""
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name in order:
            info = tarfile.TarInfo(name)
            info.size = len(FILES[name])
            info.mtime = 1_793_804_625
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(FILES[name]))
    with path.open("wb") as file, gzip.GzipFile(mode="wb", fileobj=file, mtime=gzip_time) as gz:
        gz.write(archive.getvalue())


def test_two_orders_give_the_same_package(repo_root: Path, tmp_path: Path) -> None:
    script = _load(repo_root)
    names = list(FILES)
    first = tmp_path / "a.tgz"
    second = tmp_path / "b.tgz"
    # The subcharts swapped, as two helm package runs can write them.
    _package(first, names, gzip_time=1)
    _package(second, names[:2] + names[4:] + names[2:4], gzip_time=2)
    assert first.read_bytes() != second.read_bytes()

    assert script.main([str(first), str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()

    with tarfile.open(first, "r:gz") as tar:
        members = tar.getmembers()
        assert [member.name for member in members] == sorted(FILES)
        for member in members:
            assert member.mtime == 1_793_804_625
            extracted = tar.extractfile(member)
            assert extracted is not None
            assert extracted.read() == FILES[member.name]
    chart = yaml.safe_load(FILES["mowdeck/Chart.yaml"])
    assert chart["name"] == "mowdeck"


def test_normalizing_twice_changes_nothing(repo_root: Path, tmp_path: Path) -> None:
    script = _load(repo_root)
    package = tmp_path / "chart.tgz"
    _package(package, list(FILES), gzip_time=5)
    script.normalize(package)
    once = package.read_bytes()
    script.normalize(package)
    assert package.read_bytes() == once
