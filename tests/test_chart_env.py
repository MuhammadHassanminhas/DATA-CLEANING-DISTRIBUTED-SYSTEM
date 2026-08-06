"""The chart must pin every env var that collides with a Kubernetes
service link.

This file exists because of a real outage rather than a hypothetical.
Step 3.5 moved the coordinator's entrypoint from `uvicorn app.main:app`
(port as a CMD flag) to `python -m app.serve`, which reads
`COORDINATOR_PORT` from the environment. kubelet injects a Docker-link
variable `<SERVICE>_PORT=tcp://<ip>:<port>` into every pod for every
Service in the namespace, and the chart ships a Service named
`coordinator`. So in Kubernetes — and only in Kubernetes — the deployed
coordinator read `tcp://10.0.67.120:8443`, raised

    ValueError: invalid literal for int() with base 10: 'tcp://10.0.67.120:8443'

and crashlooped, stalling the staging rollout at 1/3 replicas.

Nothing local could catch it: Compose injects no service links, so every
demo and all 438 tests passed. `POSTGRES_PORT` and `REDIS_PORT` have the
same collision and have always worked, precisely because the ConfigMap
pins them — an explicit `env`/`envFrom` value overrides the injected one.

No Postgres gate and no imports from `app`: this reads files, so it runs
everywhere the suite runs.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "infra" / "helm" / "platform" / "templates"


def _service_names() -> set[str]:
    """Every `kind: Service` in the chart. These are what kubelet injects for."""
    names: set[str] = set()
    for path in TEMPLATES.glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        for block in re.split(r"^---$", text, flags=re.M):
            if re.search(r"^kind:\s*Service\s*$", block, flags=re.M):
                match = re.search(r"^\s+name:\s*(\S+)\s*$", block, flags=re.M)
                if match:
                    names.add(match.group(1))
    return names


def _env_names_read_by(module: Path) -> set[str]:
    """Env vars the module reads, whether or not it supplies a default."""
    source = module.read_text(encoding="utf-8")
    return set(re.findall(r"""os\.environ(?:\.get)?\(?\[?["']([A-Z][A-Z0-9_]*)["']""", source))


def _names_pinned_in_chart() -> set[str]:
    """Keys the chart sets explicitly, in the ConfigMap or a container `env:`."""
    pinned: set[str] = set()
    for path in TEMPLATES.glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        pinned |= set(re.findall(r"^\s+([A-Z][A-Z0-9_]*):\s", text, flags=re.M))
        pinned |= set(re.findall(r"^\s+-\s*name:\s*([A-Z][A-Z0-9_]*)\s*$", text, flags=re.M))
    return pinned


def test_the_chart_has_a_service_named_coordinator():
    """The premise of the test below. If this ever stops being true the
    collision goes away and the pinning becomes belt-and-braces rather than
    load-bearing — worth noticing rather than silently over-asserting."""
    assert "coordinator" in _service_names()


def test_every_env_the_entrypoint_reads_that_a_service_link_would_shadow_is_pinned():
    """The regression guard for the crashloop described in this module's docstring.

    Only names kubelet actually injects are checked — `<SERVICE>_PORT` and
    `<SERVICE>_SERVICE_HOST` / `<SERVICE>_SERVICE_PORT`. `COORDINATOR_HOST`
    and `COORDINATOR_TLS_CERT` are NOT of that shape and are deliberately
    not required here, so this asserts the real hazard rather than a
    tidier-looking one.
    """
    injected: set[str] = set()
    for service in _service_names():
        upper = service.upper().replace("-", "_")
        injected |= {f"{upper}_PORT", f"{upper}_SERVICE_HOST", f"{upper}_SERVICE_PORT"}

    read = _env_names_read_by(ROOT / "coordinator" / "app" / "serve.py")
    read |= _env_names_read_by(ROOT / "coordinator" / "app" / "config.py")

    shadowed = read & injected
    unpinned = shadowed - _names_pinned_in_chart()

    assert not unpinned, (
        f"{sorted(unpinned)} are read by the coordinator and are also injected by "
        "kubelet as `tcp://<ip>:<port>` service links. Pin them in "
        "infra/helm/platform/templates/configmap.yaml or the pod will crashloop "
        "in Kubernetes while every local Compose run passes."
    )


def test_the_collision_is_real_and_this_test_would_have_caught_it():
    """A guard against the guard being vacuous.

    If `_names_pinned_in_chart` ever silently matched everything, the test
    above would pass no matter what. Here the pinning is removed on purpose
    and the same computation must then flag COORDINATOR_PORT.
    """
    injected = {"COORDINATOR_PORT"}
    read = _env_names_read_by(ROOT / "coordinator" / "app" / "serve.py")
    assert "COORDINATOR_PORT" in read, "serve.py no longer reads COORDINATOR_PORT"
    assert injected & read - (_names_pinned_in_chart() - {"COORDINATOR_PORT"}) == {
        "COORDINATOR_PORT"
    }
