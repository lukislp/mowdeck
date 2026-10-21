# ClusterFuzzLite build image (docs/dev/fuzzing.md): the OSS-Fuzz Python builder (clang,
# libFuzzer, compile_python_fuzzer) plus Python 3.13, which the workspace requires; the
# builder's own interpreter is 3.11. uv installs a standalone CPython 3.13 into a virtual
# environment that comes first on PATH, so the build script, PyInstaller and the compile step
# (which copies Atheris's sanitizer runtime) all use it.
#
# Both images are pinned by digest; the uv image is the UV_IMAGE of infra/compose/.env.example
# (tools/repo-checks/tests/test_fuzz_targets.py keeps them equal).
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:21f6fab17ace4dc76e1c56de91ee95d3d03da021df94b41056ba036f6c6f55e9
COPY --from=ghcr.io/astral-sh/uv:0.12.24@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/opt/fuzz-venv \
    PATH=/opt/fuzz-venv/bin:$PATH
# Atheris, PyInstaller and coverage, locked with hashes (.clusterfuzzlite/requirements.in).
COPY .clusterfuzzlite/requirements.txt /opt/fuzz-requirements.txt
RUN uv venv --python 3.13 /opt/fuzz-venv \
    && uv pip install --require-hashes -r /opt/fuzz-requirements.txt
COPY . $SRC/mowdeck
WORKDIR $SRC/mowdeck
COPY .clusterfuzzlite/build.sh $SRC/
