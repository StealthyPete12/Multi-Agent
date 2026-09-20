"""Static validation of the Phase 5 observability config files —
``observability/**`` and the docker-compose services that mount them.
These aren't runtime tests (no Prometheus/Grafana/Phoenix required); they
catch the class of bug this phase actually hit once (a metric renamed in
code but not in the dashboard JSON that queries it) before it reaches a
live stack.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
OBSERVABILITY_DIR = REPO_ROOT / "observability"
DASHBOARDS_DIR = OBSERVABILITY_DIR / "grafana" / "provisioning" / "dashboards" / "json"


def test_prometheus_config_is_valid_yaml_with_expected_jobs():
    config_path = OBSERVABILITY_DIR / "prometheus" / "prometheus.yml"
    config = yaml.safe_load(config_path.read_text())

    job_names = {job["job_name"] for job in config["scrape_configs"]}
    assert job_names == {"watcher", "researcher", "reviewer", "rabbitmq", "postgres", "prometheus"}

    for job in config["scrape_configs"]:
        for static_config in job["static_configs"]:
            for target in static_config["targets"]:
                assert re.match(r"^[\w.\-]+:\d+$", target), f"malformed target: {target!r}"


def test_grafana_datasource_config_points_at_prometheus():
    datasource_path = (
        OBSERVABILITY_DIR / "grafana" / "provisioning" / "datasources" / "datasources.yml"
    )
    config = yaml.safe_load(datasource_path.read_text())

    datasources = config["datasources"]
    assert len(datasources) == 1
    assert datasources[0]["type"] == "prometheus"
    assert datasources[0]["uid"] == "prometheus"
    assert datasources[0]["url"].startswith("http://")


def test_grafana_dashboard_provider_points_at_the_json_directory():
    provider_path = OBSERVABILITY_DIR / "grafana" / "provisioning" / "dashboards" / "dashboards.yml"
    config = yaml.safe_load(provider_path.read_text())

    provider = config["providers"][0]
    assert provider["options"]["path"] == "/etc/grafana/provisioning/dashboards/json"


DASHBOARD_FILES = sorted(DASHBOARDS_DIR.glob("*.json"))


@pytest.mark.parametrize("dashboard_path", DASHBOARD_FILES, ids=lambda p: p.stem)
def test_dashboard_json_is_valid_and_self_consistent(dashboard_path: Path):
    dashboard = json.loads(dashboard_path.read_text())

    assert dashboard["title"]
    assert dashboard["uid"]

    panel_ids = [panel["id"] for panel in dashboard["panels"]]
    assert len(panel_ids) == len(set(panel_ids)), "duplicate panel ids"
    assert len(dashboard["panels"]) > 0

    for panel in dashboard["panels"]:
        assert panel["datasource"] == {"type": "prometheus", "uid": "prometheus"}
        assert panel["targets"], f"panel {panel['title']!r} has no queries"
        ref_ids = [t["refId"] for t in panel["targets"]]
        assert len(ref_ids) == len(set(ref_ids)), f"panel {panel['title']!r} has duplicate refIds"


def test_dashboard_panel_metrics_match_shared_telemetry_catalog():
    """Every `swarm_*` metric name a dashboard queries must actually be
    produced by shared/telemetry.py::Metrics — this is the exact class of
    bug this phase hit (a "_ms" suffix left in a dashboard query after the
    Prometheus exporter's own unit-suffixing made the wire name
    "_milliseconds" instead)."""
    telemetry_source = (REPO_ROOT / "shared" / "telemetry.py").read_text()
    # Every create_counter/create_histogram/create_observable_gauge call's
    # first (name) argument, as actually registered with the SDK (both the
    # `m.create_*` calls in Metrics.__post_init__ and the
    # `meter.create_observable_gauge` call in register_pool_gauges).
    registered_names = set(re.findall(r'(?:m|meter)\.create_\w+\(\s*"([\w]+)"', telemetry_source))
    assert "swarm_events_processed_total" in registered_names  # sanity: the regex found something
    assert "swarm_db_pool_connections" in registered_names

    # Prometheus appends a unit suffix for a few OTel units used here.
    unit_suffixes = {"ms": "_milliseconds", "s": "_seconds"}
    unit_by_name = dict(
        re.findall(r'(?:m|meter)\.create_\w+\(\s*"(\w+)",\s*\n?\s*unit="(\w+)"', telemetry_source)
    )

    def exposed_names(base: str) -> set[str]:
        suffix = unit_suffixes.get(unit_by_name.get(base, ""), "")
        return {
            base + suffix,
            base + suffix + "_bucket",
            base + suffix + "_sum",
            base + suffix + "_count",
        }

    all_exposed = {"up"}  # Prometheus's own synthetic metric
    for name in registered_names:
        all_exposed |= exposed_names(name)

    for dashboard_path in DASHBOARD_FILES:
        dashboard = json.loads(dashboard_path.read_text())
        for panel in dashboard["panels"]:
            for target in panel["targets"]:
                expr = target["expr"]
                for metric_name in re.findall(r"\bswarm_[a-z0-9_]+\b", expr):
                    assert metric_name in all_exposed, (
                        f"{dashboard_path.name} panel {panel['title']!r} queries "
                        f"{metric_name!r}, which shared/telemetry.py does not "
                        f"register (or registers under a different Prometheus "
                        f"wire name after unit-suffixing)"
                    )


def test_rabbitmq_enabled_plugins_file_enables_prometheus_plugin():
    content = (OBSERVABILITY_DIR / "rabbitmq" / "enabled_plugins").read_text()
    assert "rabbitmq_prometheus" in content
    assert "rabbitmq_management" in content


def test_docker_compose_mounts_every_observability_config_file():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    services = compose["services"]

    for service in ("phoenix", "prometheus", "grafana", "postgres_exporter"):
        assert service in services, f"docker-compose.yml is missing the {service} service"

    rabbitmq_volumes = " ".join(services["rabbitmq"]["volumes"])
    assert "observability/rabbitmq/enabled_plugins" in rabbitmq_volumes

    prometheus_volumes = " ".join(services["prometheus"]["volumes"])
    assert "observability/prometheus/prometheus.yml" in prometheus_volumes

    grafana_volumes = " ".join(services["grafana"]["volumes"])
    assert "observability/grafana/provisioning" in grafana_volumes
