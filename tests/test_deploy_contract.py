from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from ingestion.cli import montar_parser
from ingestion.config import VERSOES_ESPERADAS_PADRAO
from ingestion.metrics import EstadoMetricas, coletar


ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "deploy" / "k8s"
EXPECTED_RESOURCES = {
    "service-account.yaml",
    "configmap.yaml",
    "cronjob.yaml",
    "reconciliation-cronjob.yaml",
}
EXPECTED_CONFIG = {
    "MAX_ATTEMPTS": "3",
    "EXPECTED_SCHEMA_VERSION": json.dumps(
        dict(VERSOES_ESPERADAS_PADRAO), separators=(",", ":")
    ),
    "CLOUDWATCH_NAMESPACE": "TenableIngestion",
    "CLOUDWATCH_ENABLED": "true",
    "RETENTION_MONTHS": "24",
    "INGEST_FILE_RETENTION_DAYS": "90",
    "MANIFEST_STALE_HOURS": "6",
}
ALARM_METRICS = {
    "HoursSinceLastManifest",
    "FilesQuarantined",
    "JobDurationSeconds",
    "FindingsOpenChangePercent",
}


def _yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        document = yaml.safe_load(stream)
    assert isinstance(document, dict), f"{path} must contain one YAML mapping"
    return document


class _CloudFormationLoader(yaml.SafeLoader):
    pass


def _cloudformation_tag(loader, tag_suffix, node):
    key = tag_suffix.removeprefix("!")
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {key: value}


_CloudFormationLoader.add_multi_constructor("!", _cloudformation_tag)


def _dockerfile_instructions() -> list[tuple[str, str]]:
    logical_lines: list[str] = []
    pending = ""
    for raw_line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        pending = f"{pending} {line}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical_lines.append(pending)
        pending = ""
    assert not pending, "Dockerfile has an unfinished continuation"
    return [tuple(line.split(maxsplit=1)) for line in logical_lines]


def _instruction_values(name: str) -> list[str]:
    return [value for instruction, value in _dockerfile_instructions() if instruction.upper() == name]


def _copy_sources(value: str) -> list[str]:
    if value.startswith("["):
        return json.loads(value)[:-1]
    return shlex.split(value, posix=True)[:-1]


def _dockerignore_matches(path: str) -> bool:
    ignored = False
    candidate = PurePosixPath(path)
    for raw_pattern in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        pattern = raw_pattern.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negated = pattern.startswith("!")
        pattern = pattern.removeprefix("!").lstrip("/").rstrip("/")
        parts = candidate.parts
        matches = fnmatch.fnmatch(path, pattern) or candidate.match(pattern)
        if "/" not in pattern:
            matches = matches or any(fnmatch.fnmatch(part, pattern) for part in parts)
        if path == pattern or path.startswith(f"{pattern}/"):
            matches = True
        if matches:
            ignored = not negated
    return ignored


def _job(path: str) -> tuple[dict, dict, dict]:
    document = _yaml(K8S / path)
    assert document["apiVersion"] == "batch/v1"
    assert document["kind"] == "CronJob"
    pod_spec = document["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert len(pod_spec["containers"]) == 1
    return document, pod_spec, pod_spec["containers"][0]


def test_dockerfile_builds_minimal_fixed_non_root_cli_image():
    assert _instruction_values("FROM") == ["python:3.13-slim"]

    copied = {
        source.rstrip("/")
        for value in _instruction_values("COPY")
        for source in _copy_sources(value)
    }
    assert {"requirements.txt", "ingestion", "migrations", "alembic.ini"} <= copied

    run_tokens = [
        token
        for command in _instruction_values("RUN")
        for token in shlex.split(command.replace("&&", " "), posix=True)
    ]
    assert "10001" in run_tokens
    assert any(token.endswith("pip") for token in run_tokens)
    assert "requirements.txt" in run_tokens
    assert _instruction_values("USER") == ["10001:10001"]
    assert [json.loads(value) for value in _instruction_values("ENTRYPOINT")] == [
        ["python", "-m", "ingestion.cli"]
    ]
    assert [json.loads(value) for value in _instruction_values("CMD")] == [["run"]]


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        ".worktrees/task/HEAD",
        ".superpowers/sdd/brief.md",
        ".venv/pyvenv.cfg",
        "ingestion/__pycache__/config.pyc",
        ".pytest_cache/CACHEDIR.TAG",
        "tests/test_cli.py",
        "samples/example.json",
        ".env.production",
        "export.csv",
        "deploy/k8s/secret.local.yaml",
        "deploy/k8s/overlays/prod/kustomization.yaml",
    ],
)
def test_docker_context_excludes_local_or_sensitive_material(path: str):
    assert _dockerignore_matches(path), path


@pytest.mark.parametrize(
    "path",
    [
        "deploy/k8s/secret.local.yaml",
        "deploy/k8s/overlays/prod/kustomization.yaml",
        "deploy/k8s/prod-overlay.yaml",
    ],
)
def test_gitignore_protects_local_secret_and_overlay_material(path: str):
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", path],
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, path


def test_gitignore_keeps_versioned_base_manifests_visible():
    for path in EXPECTED_RESOURCES | {"kustomization.yaml"}:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", f"deploy/k8s/{path}"],
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 1, path


def test_configmap_uses_only_supported_non_secret_production_settings():
    document = _yaml(K8S / "configmap.yaml")
    assert document["apiVersion"] == "v1"
    assert document["kind"] == "ConfigMap"
    assert document["metadata"] == {"name": "tenable-ingestion-config"}
    assert document["data"] == EXPECTED_CONFIG
    assert not {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "TENABLE_BUCKET",
        "PG_DSN",
        "INGESTION_MODE",
    } & document["data"].keys()


def test_service_account_has_no_fake_identity_or_fixed_namespace():
    document = _yaml(K8S / "service-account.yaml")
    assert document == {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": "tenable-ingestion"},
    }


@pytest.mark.parametrize(
    ("path", "name", "schedule", "args"),
    [
        ("cronjob.yaml", "tenable-ingestion", "0 3 * * *", ["run"]),
        (
            "reconciliation-cronjob.yaml",
            "tenable-ingestion-reconciliation",
            "0 12 * * 0",
            ["reconcile", "--output", "-"],
        ),
    ],
)
def test_cronjobs_keep_entrypoint_and_have_exact_schedules_and_arguments(
    path: str, name: str, schedule: str, args: list[str]
):
    document, pod_spec, container = _job(path)
    assert document["metadata"] == {"name": name}
    assert document["spec"]["schedule"] == schedule
    assert document["spec"]["timeZone"] == "America/Sao_Paulo"
    assert document["spec"]["concurrencyPolicy"] == "Forbid"
    assert document["spec"]["startingDeadlineSeconds"] > 0
    assert document["spec"]["successfulJobsHistoryLimit"] >= 0
    assert document["spec"]["failedJobsHistoryLimit"] >= 0
    assert document["spec"]["jobTemplate"]["spec"]["backoffLimit"] >= 0
    assert document["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] > 0
    assert pod_spec["restartPolicy"] == "Never"
    assert pod_spec["serviceAccountName"] == "tenable-ingestion"
    assert container["image"] == "tenable-ingestion:latest"
    assert container["args"] == args
    assert "command" not in container
    montar_parser().parse_args(args)


@pytest.mark.parametrize("path", ["cronjob.yaml", "reconciliation-cronjob.yaml"])
def test_cronjobs_load_exact_secret_refs_and_shared_config(path: str):
    _, _, container = _job(path)
    assert container["envFrom"] == [
        {"configMapRef": {"name": "tenable-ingestion-config"}}
    ]
    env = {item["name"]: item for item in container["env"]}
    assert env["TENABLE_BUCKET"] == {
        "name": "TENABLE_BUCKET",
        "valueFrom": {
            "secretKeyRef": {
                "name": "tenable-ingestion-secret",
                "key": "TENABLE_BUCKET",
            }
        },
    }
    assert env["PG_DSN"] == {
        "name": "PG_DSN",
        "valueFrom": {
            "secretKeyRef": {
                "name": "tenable-ingestion-secret",
                "key": "PG_DSN",
            }
        },
    }
    assert env["TMPDIR"] == {"name": "TMPDIR", "value": "/tmp"}
    assert set(env) == {"TENABLE_BUCKET", "PG_DSN", "TMPDIR"}


@pytest.mark.parametrize("path", ["cronjob.yaml", "reconciliation-cronjob.yaml"])
def test_cronjobs_enforce_non_root_security_resources_and_disk_tmp(path: str):
    _, pod_spec, container = _job(path)
    assert pod_spec["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "fsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert set(container["resources"]) == {"requests", "limits"}
    assert set(container["resources"]["requests"]) == {
        "cpu",
        "memory",
        "ephemeral-storage",
    }
    assert set(container["resources"]["limits"]) == {
        "cpu",
        "memory",
        "ephemeral-storage",
    }
    assert pod_spec["volumes"] == [{"name": "tmp", "emptyDir": {}}]
    assert container["volumeMounts"] == [{"name": "tmp", "mountPath": "/tmp"}]


def test_kustomization_renders_exactly_four_namespace_free_non_secret_resources():
    document = _yaml(K8S / "kustomization.yaml")
    assert document["apiVersion"] == "kustomize.config.k8s.io/v1beta1"
    assert document["kind"] == "Kustomization"
    assert set(document) == {"apiVersion", "kind", "resources"}
    assert len(document["resources"]) == 4
    assert set(document["resources"]) == EXPECTED_RESOURCES

    for resource in document["resources"]:
        manifest = _yaml(K8S / resource)
        assert manifest["kind"] != "Secret"
        assert "namespace" not in manifest["metadata"]

    all_manifests = [_yaml(path) for path in K8S.glob("*.yaml")]
    assert all(document["kind"] != "Secret" for document in all_manifests)


def test_cloudformation_declares_exact_alarm_identities_and_actions():
    with (ROOT / "deploy" / "cloudwatch-alarms.yaml").open(encoding="utf-8") as stream:
        template = yaml.load(stream, Loader=_CloudFormationLoader)

    assert template["Parameters"] == {
        "Namespace": {"Type": "String", "Default": EXPECTED_CONFIG["CLOUDWATCH_NAMESPACE"]},
        "AlarmTopicArn": {"Type": "String"},
    }
    alarms = {
        logical_id: resource
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::CloudWatch::Alarm"
    }
    assert len(alarms) == 4
    assert len(template["Resources"]) == 4
    by_metric = {alarm["Properties"]["MetricName"]: alarm for alarm in alarms.values()}
    assert set(by_metric) == ALARM_METRICS
    emitted_metrics = {
        metric.nome
        for metric in coletar(
            SimpleNamespace(
                payloads_ok=1,
                registros=1,
                eventos=1,
                horas_desde_ultimo_manifest=1.0,
            ),
            EstadoMetricas(quarentena=0, findings_open=1, change_percent=0.0),
            duracao_segundos=1.0,
        )
    }
    assert set(by_metric) <= emitted_metrics

    for alarm in by_metric.values():
        properties = alarm["Properties"]
        assert properties["Namespace"] == {"Ref": "Namespace"}
        assert properties["AlarmActions"] == [{"Ref": "AlarmTopicArn"}]
        assert properties["OKActions"] == [{"Ref": "AlarmTopicArn"}]
        assert "Dimensions" not in properties
        assert "Unit" not in properties

    expected_evaluation = {
        "HoursSinceLastManifest": {
            "Statistic": "Maximum",
            "Period": 86400,
            "EvaluationPeriods": 1,
            "Threshold": int(EXPECTED_CONFIG["MANIFEST_STALE_HOURS"]),
            "ComparisonOperator": "GreaterThanThreshold",
            "TreatMissingData": "breaching",
        },
        "FilesQuarantined": {
            "Statistic": "Maximum",
            "Period": 86400,
            "EvaluationPeriods": 1,
            "Threshold": 0,
            "ComparisonOperator": "GreaterThanThreshold",
            "TreatMissingData": "notBreaching",
        },
        "JobDurationSeconds": {
            "Statistic": "SampleCount",
            "Period": 3600,
            "EvaluationPeriods": 26,
            "DatapointsToAlarm": 26,
            "Threshold": 1,
            "ComparisonOperator": "LessThanThreshold",
            "TreatMissingData": "breaching",
        },
        "FindingsOpenChangePercent": {
            "Statistic": "Maximum",
            "Period": 86400,
            "EvaluationPeriods": 1,
            "Threshold": 20,
            "ComparisonOperator": "GreaterThanThreshold",
            "TreatMissingData": "notBreaching",
        },
    }
    for metric_name, expected in expected_evaluation.items():
        properties = by_metric[metric_name]["Properties"]
        assert {key: properties[key] for key in expected} == expected


def test_cloudformation_passes_installed_console_linter():
    executable = Path(sys.executable).with_name(
        "cfn-lint.exe" if os.name == "nt" else "cfn-lint"
    )
    assert executable.is_file(), f"cfn-lint console script not found beside {sys.executable}"
    subprocess.run(
        [str(executable), "deploy/cloudwatch-alarms.yaml"],
        cwd=ROOT,
        check=True,
    )
