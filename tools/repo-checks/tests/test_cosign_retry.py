"""Release signing survives Sigstore hiccups and stays verified (scripts/cosign_retry.sh).

* every ``cosign sign``/``sign-blob``/``attest`` of the workflows goes through the script;
* the retry classification: Rekor 409 conflicts, TUF/CDN fetches, network errors, timeouts,
  429 and 5xx are transient, everything else fails at once;
* a retry deletes the bundle of the failed attempt and signs anew, every bundle is verified, and
  a bundle that already verifies is kept (rerun of the publish job);
* the publish job's section of the release notes replaces an earlier copy
  (``release_assets.py notes``).
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

SCRIPT = Path("tools") / "repo-checks" / "scripts" / "cosign_retry.sh"
IDENTITY = "https://github.com/lukislp/mowdeck/.github/workflows/release.yml@refs/heads/main"
WORKFLOWS = Path(".github") / "workflows"
SIGNING = re.compile(r"(?<![\w-])cosign (sign|sign-blob|attest)(?![\w-])")


def _find_bash() -> str | None:
    """bash from PATH; on Windows Git Bash (the bash.exe of WSL cannot run these scripts)."""
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if os.name == "nt" and git_bash.exists():
        return str(git_bash)
    return shutil.which("bash")


BASH = _find_bash()
bash_only = pytest.mark.skipif(BASH is None, reason="bash not installed")


# --- the workflows ------------------------------------------------------------------------------


def _jobs() -> list[tuple[str, str, dict[str, Any]]]:
    root = Path(__file__).resolve().parents[3]
    jobs = []
    for path in sorted((root / WORKFLOWS).glob("*.yml")):
        workflow = yaml.safe_load(path.read_text("utf-8"))
        for name, job in (workflow.get("jobs") or {}).items():
            jobs.append((path.name, name, job))
    return jobs


def test_workflows_sign_only_through_the_retry_script() -> None:
    signing_steps = 0
    for workflow, job_name, job in _jobs():
        steps = job.get("steps", [])
        for index, step in enumerate(steps):
            script = step.get("run", "")
            lines = [ln for ln in script.splitlines() if not ln.strip().startswith(("#", "echo"))]
            for line in lines:
                assert not SIGNING.search(line), f"{workflow} {job_name}: bare {line.strip()}"
            if "cosign_sign" in script or "cosign_retry " in script:
                signing_steps += 1
                assert "scripts/cosign_retry.sh" in script, f"{workflow} {job_name}"
                assert "set -eu" in script, f"{workflow} {job_name}"
                installers = [
                    s
                    for s in steps[:index]
                    if s.get("uses", "").startswith("sigstore/cosign-installer@")
                ]
                assert installers, f"{workflow} {job_name}: no cosign-installer before signing"
    # images, chart and publish of release.yml.
    assert signing_steps >= 3


def test_release_signs_images_chart_and_assets_verified(repo_root: Path) -> None:
    text = (repo_root / WORKFLOWS / "release.yml").read_text("utf-8")
    workflow = yaml.safe_load(text)
    runs = {
        name: "\n".join(s.get("run", "") for s in job.get("steps", []))
        for name, job in workflow["jobs"].items()
    }
    assert 'cosign_sign_verified "$IDENTITY" "$IMAGE@$DIGEST"' in runs["images"]
    assert 'cosign_sign_verified "$IDENTITY" "$CHART@$DIGEST"' in runs["chart"]
    assert 'cosign_sign_blob_verified "$IDENTITY" "${files[@]}"' in runs["publish"]
    # The notes section is merged, never appended a second time.
    assert ">> notes.md" not in runs["publish"]
    assert "release_assets.py notes --body notes.md" in runs["publish"]


# --- retry classification -----------------------------------------------------------------------

TRANSIENT = [
    "Error: signing assets/mowdeck-0.42.0.apk: signing bundle: error signing bundle: "
    "[POST /api/v1/log/entries][409] createLogEntryConflict "
    '{"code":409,"message":"an equivalent entry already exists in the transparency log"}',
    "Error: error getting signing config from TUF: tuf refresh failed: "
    'Get "https://tuf-repo-cdn.sigstore.dev/14.root.json": dial tcp 1.2.3.4:443: i/o timeout',
    "error getting trusted root: unexpected EOF",
    "[POST /api/v2/signingCert][503] createSigningCertificateServiceUnavailable",
    "POST https://rekor.sigstore.dev/api/v1/log/entries giving up after 4 attempt(s)",
    "net/http: TLS handshake timeout",
    "context deadline exceeded",
    "read tcp 10.1.0.4:443: connection reset by peer",
    "lookup fulcio.sigstore.dev: no such host",
    "GET https://ghcr.io/v2/: 502 Bad Gateway",
    "Too Many Requests",
]
PERMANENT = [
    "Error: none of the expected identities matched what was in the certificate",
    "Error: open assets/missing.apk: no such file or directory",
    "Error: unknown flag: --bundel",
    "Error: invalid signature when validating ASN.1 encoded signature",
    "Error: no matching signatures: certificate subject mismatch",
    "MANIFEST_UNKNOWN: manifest unknown",
]


def _classify(tmp_path: Path, repo_root: Path, message: str) -> bool:
    assert BASH is not None
    err = tmp_path / "err"
    err.write_text(message + "\n", "utf-8", newline="\n")
    script = f'. "{(repo_root / SCRIPT).as_posix()}"; cosign_retry_transient "{err.as_posix()}"'
    result = subprocess.run(  # noqa: S603
        [BASH, "-c", script], capture_output=True, text=True, check=False, timeout=300
    )
    return result.returncode == 0


@bash_only
@pytest.mark.parametrize("message", TRANSIENT)
def test_transient_errors_are_retried(tmp_path: Path, repo_root: Path, message: str) -> None:
    assert _classify(tmp_path, repo_root, message)


@bash_only
@pytest.mark.parametrize("message", PERMANENT)
def test_other_errors_are_not_retried(tmp_path: Path, repo_root: Path, message: str) -> None:
    assert not _classify(tmp_path, repo_root, message)


# --- the signing functions against a fake cosign -----------------------------------------------

# One answer per call from FAKE_ANSWERS: "<exit code>|<stderr>|<write>", where write=bundle makes
# the call write the --bundle file. Every call is logged as "<subcommand> bundle=<0|1>", with 1
# when the --bundle file existed at the start of the call.
FAKE_COSIGN = """#!/usr/bin/env bash
n=$(( $(cat "$FAKE_COUNT" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$FAKE_COUNT"
bundle=""
prev=""
for arg in "$@"; do
  [ "$prev" = "--bundle" ] && bundle="$arg"
  prev="$arg"
done
existed=0
[ -n "$bundle" ] && [ -e "$bundle" ] && existed=1
echo "$1 bundle=$existed" >> "$FAKE_LOG"
line="$(sed -n "${n}p" "$FAKE_ANSWERS")"
IFS='|' read -r code err write <<<"$line"
[ "$write" = bundle ] && [ -n "$bundle" ] && echo '{"fake":true}' > "$bundle"
[ -n "$err" ] && echo "$err" >&2
exit "${code:-0}"
"""

OK_SIGN = "0||bundle"
OK = "0||"
CONFLICT = (
    '1|Error: [POST /api/v1/log/entries][409] createLogEntryConflict {"code":409,'
    '"message":"an equivalent entry already exists in the transparency log"}|bundle'
)
TUF = "1|Error: tuf refresh failed: dial tcp: i/o timeout|"
NO_IDENTITY = "1|Error: none of the expected identities matched what was in the certificate|"


class Fake:
    """A temporary directory with the fake cosign, an asset and the call log."""

    def __init__(self, tmp_path: Path, repo_root: Path) -> None:
        self.tmp = tmp_path
        self.repo_root = repo_root
        self.cosign = tmp_path / "cosign"
        self.cosign.write_text(FAKE_COSIGN, "utf-8", newline="\n")
        self.cosign.chmod(0o755)
        self.asset = tmp_path / "assets" / "mowdeck-1.2.3.apk"
        self.asset.parent.mkdir()
        self.asset.write_bytes(b"apk")
        self.bundle = Path(f"{self.asset}.sigstore.json")

    def run(self, answers: list[str], call: str) -> subprocess.CompletedProcess[str]:
        assert BASH is not None
        (self.tmp / "answers").write_text("\n".join(answers) + "\n", "utf-8", newline="\n")
        script = f'set -eu; . "{(self.repo_root / SCRIPT).as_posix()}"; {call}'
        env = {
            **os.environ,
            "COSIGN": self.cosign.as_posix(),
            "COSIGN_RETRY_DELAYS": "0 0 0 0",
            "FAKE_ANSWERS": (self.tmp / "answers").as_posix(),
            "FAKE_COUNT": (self.tmp / "count").as_posix(),
            "FAKE_LOG": (self.tmp / "log").as_posix(),
        }
        return subprocess.run(  # noqa: S603
            [BASH, "-c", script], capture_output=True, text=True, env=env, check=False, timeout=300
        )

    def sign_blob(self, answers: list[str]) -> subprocess.CompletedProcess[str]:
        return self.run(
            answers, f'cosign_sign_blob_verified "{IDENTITY}" "{self.asset.as_posix()}"'
        )

    def sign_ref(self, answers: list[str]) -> subprocess.CompletedProcess[str]:
        return self.run(answers, f'cosign_sign_verified "{IDENTITY}" "ghcr.io/x/y@sha256:00"')

    def calls(self) -> list[str]:
        log = self.tmp / "log"
        return log.read_text("utf-8").splitlines() if log.exists() else []


@pytest.fixture
def fake(tmp_path: Path, repo_root: Path) -> Fake:
    """The fake cosign in a temporary directory."""
    return Fake(tmp_path, repo_root)


@bash_only
def test_sign_blob_signs_and_verifies(fake: Fake) -> None:
    result = fake.sign_blob([OK_SIGN, OK])
    assert result.returncode == 0, result.stderr
    assert fake.calls() == ["sign-blob bundle=0", "verify-blob bundle=1"]
    assert fake.bundle.is_file()


@bash_only
def test_conflict_is_signed_again_without_the_stale_bundle(fake: Fake) -> None:
    # The failed attempt left a bundle behind; the next attempt starts without it.
    result = fake.sign_blob([CONFLICT, TUF, OK_SIGN, OK])
    assert result.returncode == 0, result.stderr
    assert fake.calls() == [
        "sign-blob bundle=0",
        "sign-blob bundle=0",
        "sign-blob bundle=0",
        "verify-blob bundle=1",
    ]
    assert result.stderr.count("::warning::") == 2
    assert fake.bundle.is_file()


@bash_only
def test_failed_verification_signs_again(fake: Fake) -> None:
    result = fake.sign_blob([OK_SIGN, TUF, OK_SIGN, OK])
    assert result.returncode == 0, result.stderr
    assert fake.calls() == [
        "sign-blob bundle=0",
        "verify-blob bundle=1",
        "sign-blob bundle=0",
        "verify-blob bundle=1",
    ]


@bash_only
def test_permanent_error_fails_at_once_without_a_bundle(fake: Fake) -> None:
    result = fake.sign_blob([OK_SIGN, NO_IDENTITY])
    assert result.returncode != 0
    assert len(fake.calls()) == 2
    assert "::error::" in result.stderr
    assert not fake.bundle.exists()


@bash_only
def test_gives_up_after_five_attempts(fake: Fake) -> None:
    result = fake.sign_blob([CONFLICT] * 7)
    assert result.returncode != 0
    assert len(fake.calls()) == 5
    assert result.stderr.count("::warning::") == 4
    assert "::error::" in result.stderr
    assert not fake.bundle.exists()


@bash_only
def test_existing_valid_bundle_is_kept(fake: Fake) -> None:
    fake.bundle.write_text("{}", "utf-8")
    result = fake.sign_blob([OK])
    assert result.returncode == 0, result.stderr
    assert fake.calls() == ["verify-blob bundle=1"]


@bash_only
def test_existing_invalid_bundle_is_replaced(fake: Fake) -> None:
    fake.bundle.write_text("{}", "utf-8")
    result = fake.sign_blob([NO_IDENTITY, OK_SIGN, OK])
    assert result.returncode == 0, result.stderr
    assert fake.calls() == ["verify-blob bundle=1", "sign-blob bundle=0", "verify-blob bundle=1"]


@bash_only
def test_image_signature_retried_and_verified(fake: Fake) -> None:
    unsigned = "1|Error: no signatures found|"
    unavailable = "1|Error: POST https://fulcio.sigstore.dev: 503 Service Unavailable|"
    result = fake.sign_ref([unsigned, unavailable, OK, OK])
    assert result.returncode == 0, result.stderr
    assert fake.calls() == ["verify bundle=0", "sign bundle=0", "sign bundle=0", "verify bundle=0"]


@bash_only
def test_signed_image_is_not_signed_again(fake: Fake) -> None:
    result = fake.sign_ref([OK])
    assert result.returncode == 0, result.stderr
    assert fake.calls() == ["verify bundle=0"]


# --- release notes ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def assets(repo_root: Path) -> ModuleType:
    """scripts/release_assets.py as a module."""
    path = repo_root / "tools" / "repo-checks" / "scripts" / "release_assets.py"
    spec = importlib.util.spec_from_file_location("release_assets_notes", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SECTION = "## Install\n\nhelm install\n\n## Images and chart\n\n| `a` | `sha256:1` |\n"


def test_notes_section_is_added_once(assets: ModuleType) -> None:
    body = "### Features\n\n* something\n"
    once = assets.merge_notes(body, SECTION)
    assert once.startswith(body.rstrip() + "\n\n<!-- mowdeck-release-assets -->\n## Install")
    assert assets.merge_notes(once, SECTION) == once
    other = SECTION.replace("sha256:1", "sha256:2")
    twice = assets.merge_notes(once, other)
    assert twice.count("## Images and chart") == 1
    assert "sha256:2" in twice
    assert "sha256:1" not in twice
    assert twice.startswith("### Features")


def test_notes_section_written_before_the_markers_is_replaced(assets: ModuleType) -> None:
    legacy = "### Bug Fixes\n\n* fix\n\n" + SECTION + "\n## Verify\n\ncosign verify\n"
    merged = assets.merge_notes(legacy, SECTION)
    assert merged.count("## Install") == 1
    assert "## Verify" not in merged
    assert merged.startswith("### Bug Fixes\n\n* fix\n\n<!-- mowdeck-release-assets -->")


def test_notes_command_line(assets: ModuleType, tmp_path: Path) -> None:
    notes = tmp_path / "notes.md"
    section = tmp_path / "section.md"
    notes.write_text("", "utf-8")
    section.write_text(SECTION, "utf-8")
    args = ["notes", "--body", str(notes), "--section", str(section), "--out", str(notes)]
    assert assets.main(args) == 0
    assert assets.main(args) == 0
    text = notes.read_text("utf-8")
    assert text.startswith("<!-- mowdeck-release-assets -->\n## Install")
    assert text.count("## Install") == 1
