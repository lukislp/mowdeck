"""The Helm chart (infra/helm/mowdeck) and the Compose stack cannot drift apart.

* Every service of infra/compose/docker-compose.yml has a template in the chart (a workload, a
  migration Job, the authorization bootstrap or a named third-party workload), or is replaced by
  a named alternative.
* Every variable of infra/compose/.env.example appears in values.yaml, and every `config` and
  `secrets` key of values.yaml is a variable of .env.example (same names on both sides).
* The third-party images of values.yaml are the ones pinned in infra/compose/.env.example.
* Rendered (when helm and the chart dependencies are available): every MOWDECK_* variable a
  Compose service receives reaches the same Kubernetes container, directly or as a _FILE secret
  (the MTX_* variables for MediaMTX).
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ENV_NAME_RE = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=", re.MULTILINE)

# Compose services the chart replaces instead of templating them.
REPLACED = {
    "traefik": "the cluster's Ingress controller (templates/ingress.yaml)",
    "whoami": "Compose demo route only",
    "nats": "the official NATS subchart",
    "postgres": "CloudNativePG (templates/postgresql.yaml) or an external PostgreSQL",
    "openfga": "the official OpenFGA subchart",
    "openfga-migrate": "the OpenFGA subchart's migrate init container",
    "agora-bridge": "a second container of the camera-relay pod (cameraRelay.agoraBridge)",
}

# One-shot Compose jobs the chart runs as a Job per release revision (templates/<name>.yaml).
JOBS = {
    "authz-bootstrap": "the OpenFGA store and model",
    "nats-streams": "the JetStream streams (ADR 0074)",
}

# Compose services the chart runs with a template of their own instead of mowdeck.workload.
THIRD_PARTY = {
    "mediamtx": "MediaMTX, a third-party image with its own config (files/mediamtx.yml)",
}


def _chart(repo_root: Path) -> Path:
    return repo_root / "infra" / "helm" / "mowdeck"


def _compose(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "infra" / "compose" / "docker-compose.yml"
    data: dict[str, Any] = yaml.safe_load(path.read_text("utf-8"))["services"]
    return data


def _env_example_names(repo_root: Path) -> set[str]:
    text = (repo_root / "infra" / "compose" / ".env.example").read_text("utf-8")
    return set(ENV_NAME_RE.findall(text))


def _values(repo_root: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((_chart(repo_root) / "values.yaml").read_text("utf-8"))
    return data


def _images(repo_root: Path) -> dict[str, str]:
    """Variable -> image of the ``*_IMAGE=`` lines of .env.example (the pinned image list)."""
    text = (repo_root / "infra" / "compose" / ".env.example").read_text("utf-8")
    return dict(re.findall(r"^(\w+_IMAGE)=(\S+)\s*$", text, re.MULTILINE))


def test_every_compose_service_has_a_template(repo_root: Path) -> None:
    templates = _chart(repo_root) / "templates"
    for name in _compose(repo_root):
        if name in REPLACED:
            continue
        if name.endswith("-migrate"):
            path = templates / f"{name.removesuffix('-migrate')}.yaml"
            assert path.is_file(), f"{name}: missing {path.name}"
            assert "mowdeck.migrationJob" in path.read_text("utf-8"), f"{name}: no migration Job"
            continue
        path = templates / f"{name}.yaml"
        assert path.is_file(), f"Compose service {name} has no template {path.name} in the chart"
        text = path.read_text("utf-8")
        if name in THIRD_PARTY:
            assert "kind: Deployment" in text, f"{name}: no Deployment"
        elif name in JOBS:
            assert "kind: Job" in text, f"{name}: no Job"
        else:
            assert "mowdeck.workload" in text, f"{name}: no workload"


def test_env_example_variables_appear_in_values(repo_root: Path) -> None:
    text = (_chart(repo_root) / "values.yaml").read_text("utf-8")
    missing = sorted(
        name for name in _env_example_names(repo_root) if not re.search(rf"\b{name}\b", text)
    )
    assert not missing, f"values.yaml does not mention these .env.example variables: {missing}"


def test_values_settings_exist_in_env_example(repo_root: Path) -> None:
    names = _env_example_names(repo_root)
    values = _values(repo_root)
    keys = [*values["config"], *(k for k in values["secrets"] if k.isupper())]
    unknown = sorted(k for k in keys if k not in names)
    assert not unknown, f"values.yaml config/secrets keys missing in .env.example: {unknown}"


def test_secrets_have_no_default_values(repo_root: Path) -> None:
    secrets = _values(repo_root)["secrets"]
    for key, value in secrets.items():
        if key.isupper():
            assert value == "", f"secrets.{key} must not have a default value"


def test_chart_images_come_from_the_image_list(repo_root: Path) -> None:
    images = _images(repo_root)
    values = _values(repo_root)
    assert values["nats"]["container"]["image"]["fullImageName"] == images["NATS_IMAGE"]
    openfga = values["openfga"]["image"]
    assert f"{openfga['repository']}:{openfga['tag']}" == images["OPENFGA_IMAGE"]
    mediamtx = values["services"]["cameraRelay"]["mediamtx"]["image"]
    assert mediamtx == images["MEDIAMTX_IMAGE"]


def test_service_template_has_no_helm_values(repo_root: Path) -> None:
    """The umbrella chart replaced the per-service value stubs."""
    assert not list((repo_root / "services").glob("*/helm/values.yaml"))
    assert not (repo_root / "templates" / "service" / "template" / "helm").exists()


def test_chart_plans_file_is_the_repository_plans_file(repo_root: Path) -> None:
    """The chart cannot read outside its directory: files/plans.yaml must be an identical copy."""
    source = repo_root / "infra" / "entitlements" / "plans.yaml"
    copy = _chart(repo_root) / "files" / "plans.yaml"
    assert copy.read_bytes() == source.read_bytes(), (
        "infra/helm/mowdeck/files/plans.yaml differs from infra/entitlements/plans.yaml: "
        "copy the file again"
    )


def test_chart_prices_file_is_the_repository_prices_file(repo_root: Path) -> None:
    """files/prices.yaml (Billing's price file) must be an identical copy as well."""
    source = repo_root / "infra" / "billing" / "prices.yaml"
    copy = _chart(repo_root) / "files" / "prices.yaml"
    assert copy.read_bytes() == source.read_bytes(), (
        "infra/helm/mowdeck/files/prices.yaml differs from infra/billing/prices.yaml: "
        "copy the file again"
    )


def test_chart_mediamtx_file_is_the_compose_mediamtx_file(repo_root: Path) -> None:
    """files/mediamtx.yml (MediaMTX's config) must be an identical copy as well."""
    source = repo_root / "infra" / "compose" / "mediamtx" / "mediamtx.yml"
    copy = _chart(repo_root) / "files" / "mediamtx.yml"
    assert copy.read_bytes() == source.read_bytes(), (
        "infra/helm/mowdeck/files/mediamtx.yml differs from infra/compose/mediamtx/mediamtx.yml: "
        "copy the file again"
    )


def test_billing_is_off_by_default_and_a_compose_profile(repo_root: Path) -> None:
    """billing.enabled=false: Compose starts it only with the profile, Helm renders nothing."""
    compose = _compose(repo_root)
    for name in ("billing", "billing-migrate"):
        profiles = compose[name].get("profiles")
        assert profiles == ["billing"], f"{name} must be in the billing profile"
    assert _values(repo_root)["services"]["billing"]["enabled"] is False
    traefik = compose["traefik"].get("depends_on", {})
    assert "billing" not in traefik, "traefik must not wait for an optional profile service"


def test_camera_relay_is_off_by_default_and_a_compose_profile(repo_root: Path) -> None:
    """cameraRelay.enabled=false and the "relay" profile (ADR 0045). camera-relay is internal
    (no Traefik route); of MediaMTX only the WHEP signalling is routed, with /media/whep
    stripped, like the -media Ingress of the chart."""
    compose = _compose(repo_root)
    relay, mediamtx = compose["camera-relay"], compose["mediamtx"]
    assert relay.get("profiles") == ["relay"]
    assert mediamtx.get("profiles") == ["relay"]
    assert not any("traefik" in label for label in relay.get("labels", []))
    assert set(mediamtx.get("labels", [])) == {
        "traefik.enable=true",
        "traefik.http.routers.mediamtx-whep.rule=PathPrefix(`/media/whep/r/`)",
        "traefik.http.routers.mediamtx-whep.entrypoints=web",
        "traefik.http.routers.mediamtx-whep.middlewares=mediamtx-whep-strip",
        "traefik.http.middlewares.mediamtx-whep-strip.stripprefix.prefixes=/media/whep",
        "traefik.http.services.mediamtx-whep.loadbalancer.server.port=8889",
    }
    assert _values(repo_root)["services"]["cameraRelay"]["enabled"] is False
    traefik = compose["traefik"].get("depends_on", {})
    for name in ("camera-relay", "mediamtx"):
        assert name not in traefik, "traefik must not wait for an optional profile service"


def _render(repo_root: Path) -> list[dict[str, Any]]:
    chart = _chart(repo_root)
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "helm",
            "template",
            "mowdeck",
            str(chart),
            "-f",
            str(chart / "ci" / "test-values.yaml"),
            "--set",
            "config.NOTIFICATIONS_VAPID_PUBLIC_KEY=placeholder",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_rendered_containers_get_the_compose_settings(repo_root: Path) -> None:
    if not list((_chart(repo_root) / "charts").glob("*.tgz")):
        pytest.skip("chart dependencies missing (helm dependency build infra/helm/mowdeck)")
    docs = _render(repo_root)
    config_maps = {
        d["metadata"]["name"]: d.get("data", {}) for d in docs if d["kind"] == "ConfigMap"
    }
    workloads = {
        d["metadata"]["name"]: d for d in docs if d["kind"] in {"Deployment", "StatefulSet"}
    }
    jobs = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Job"}
    for name, service in _compose(repo_root).items():
        if name in REPLACED:
            continue
        if name.endswith("-migrate") or name == "authz-bootstrap":
            assert any(job.startswith(f"mowdeck-{name}") for job in jobs), f"no Job for {name}"
            continue
        if name in JOBS:
            job = next((d for j, d in jobs.items() if j.startswith(f"mowdeck-{name}")), None)
            assert job, f"no Job for {name}"
            container = job["spec"]["template"]["spec"]["containers"][0]
        else:
            workload = workloads.get(f"mowdeck-{name}")
            assert workload, f"Compose service {name} has no Deployment or StatefulSet"
            container = workload["spec"]["template"]["spec"]["containers"][0]
        rendered = set(config_maps.get(f"mowdeck-{name}", {}))
        rendered |= {e["name"] for e in container.get("env", [])}
        prefix = "MTX_" if name in THIRD_PARTY else "MOWDECK_"
        for var in service.get("environment", {}):
            if not var.startswith(prefix):
                continue
            assert var in rendered or f"{var}_FILE" in rendered, (
                f"{name}: Compose sets {var}, the chart does not"
            )
