"""Configuração do pipeline: env vars, whitelist de tipos e versões esperadas.

A whitelist da seção 4.2 é **fixa aqui**, não descoberta pela estrutura do
bucket. Adicionar um tipo é mudança de código consciente — é o que impede o
`tds_test_file/` (arquivo de teste de conectividade, sem manifest) e o
`host_audit_finding/` (compliance, não vulnerabilidade) de entrarem na tabela
unificada e corromperem todas as contagens.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Mapping

from .erros import ErroConfiguracao

MODO_SEED = "SEED"
MODO_INCREMENTAL = "INCREMENTAL"
MODOS = (MODO_SEED, MODO_INCREMENTAL)

PRODUTO_VM = "VM"
PRODUTO_WAS = "WAS"


# ===========================================================================
# Whitelist de tipos de payload (seção 4.2)
# ===========================================================================
@dataclass(frozen=True)
class TipoPayload:
    """Um dos três tipos que o sistema processa.

    `campo_id_delete` é o nome oficial do campo em `deletes[]`; os fallbacks
    são compatibilidade explícita com formatos já observados. A preferência
    fica fixa por tipo para uma divergência do produtor nunca ser silenciosa."""

    nome: str
    diretorio_payload: str
    diretorio_manifest: str
    tipo_manifest: str
    produto: str | None          # None para o stream enriched (cobre VM e WAS)
    campo_id_delete: str
    campos_id_delete_fallback: tuple[str, ...] = ()


TIPOS_PAYLOAD: Mapping[str, TipoPayload] = {
    tipo.nome: tipo
    for tipo in (
        TipoPayload(
            nome="FINDING",
            diretorio_payload="finding",
            diretorio_manifest="manifest_finding",
            tipo_manifest="MANIFEST_FINDING",
            produto=PRODUTO_VM,
            campo_id_delete="_id",
            campos_id_delete_fallback=("id",),
        ),
        TipoPayload(
            nome="WAS_FINDING",
            diretorio_payload="was_finding",
            diretorio_manifest="manifest_was_finding",
            tipo_manifest="MANIFEST_WAS_FINDING",
            produto=PRODUTO_WAS,
            campo_id_delete="id",
            campos_id_delete_fallback=("_id",),
        ),
        TipoPayload(
            nome="FINDING_ENRICHED_ATTRIBUTES",
            diretorio_payload="finding_enriched_attributes",
            diretorio_manifest="manifest_finding_enriched_attributes",
            tipo_manifest="MANIFEST_FINDING_ENRICHED_ATTRIBUTES",
            produto=None,
            campo_id_delete="id",
            campos_id_delete_fallback=("_id",),
        ),
    )
}

VERSOES_ESPERADAS_PADRAO: Mapping[str, int] = {nome: 1 for nome in TIPOS_PAYLOAD}


# ===========================================================================
# Leitura de env
# ===========================================================================
def _texto(nome: str, default: str = "") -> str:
    valor = os.getenv(nome)
    return valor.strip() if valor and valor.strip() else default


def _inteiro(nome: str, default: int) -> int:
    valor = _texto(nome)
    if not valor:
        return default
    try:
        return int(valor)
    except ValueError as erro:
        raise ErroConfiguracao(f"{nome} deve ser inteiro, veio {valor!r}") from erro


def _decimal(nome: str, default: float) -> float:
    valor = _texto(nome)
    if not valor:
        return default
    try:
        return float(valor)
    except ValueError as erro:
        raise ErroConfiguracao(f"{nome} deve ser numérico, veio {valor!r}") from erro


def _booleano(nome: str, default: bool) -> bool:
    valor = _texto(nome)
    return valor.lower() in {"1", "true", "yes", "on"} if valor else default


def _versoes_esperadas() -> Mapping[str, int]:
    bruto = _texto("EXPECTED_SCHEMA_VERSION")
    if not bruto:
        return VERSOES_ESPERADAS_PADRAO
    try:
        carregado = json.loads(bruto)
    except json.JSONDecodeError as erro:
        raise ErroConfiguracao(
            f"EXPECTED_SCHEMA_VERSION deve ser um JSON objeto, veio {bruto!r}"
        ) from erro
    if not isinstance(carregado, dict):
        raise ErroConfiguracao("EXPECTED_SCHEMA_VERSION deve ser um JSON objeto")
    versoes = dict(VERSOES_ESPERADAS_PADRAO)
    for nome, versao in carregado.items():
        if nome not in TIPOS_PAYLOAD:
            raise ErroConfiguracao(
                f"EXPECTED_SCHEMA_VERSION cita o tipo {nome!r}, fora da whitelist"
            )
        versoes[nome] = int(versao)
    return versoes


# ===========================================================================
# Config
# ===========================================================================
@dataclass(frozen=True)
class Config:
    bucket: str
    prefixo: str
    pg_dsn: str
    modo_forcado: str | None = None
    max_attempts: int = 3
    versoes_esperadas: Mapping[str, int] = field(
        default_factory=lambda: VERSOES_ESPERADAS_PADRAO
    )
    cloudwatch_namespace: str = "TenableIngestion"
    cloudwatch_habilitado: bool = False
    retention_months: int = 24
    retencao_ingest_file_dias: int = 90
    horas_sem_manifest_alerta: int = 6

    region_name: str = "us-east-1"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None

    s3_retry_max_attempts: int = 8
    s3_retry_base_delay_seconds: float = 0.25
    s3_retry_max_delay_seconds: float = 6.0

    log_level: str = "INFO"

    def prefixo_de(self, diretorio: str) -> str:
        """Prefixo S3 completo de um diretório do stream, com barra final."""
        partes = [p for p in (self.prefixo.strip("/"), diretorio.strip("/")) if p]
        return "/".join(partes) + "/"

    def prefixo_manifest(self, tipo: TipoPayload) -> str:
        return self.prefixo_de(tipo.diretorio_manifest)

    def versao_esperada(self, payload_type: str) -> int | None:
        return self.versoes_esperadas.get(payload_type)


def carregar_config() -> Config:
    """Lê o ambiente (e o .env, se python-dotenv estiver instalado)."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ModuleNotFoundError:
        pass

    bucket = _texto("TENABLE_BUCKET")
    if not bucket:
        raise ErroConfiguracao("TENABLE_BUCKET é obrigatório")

    pg_dsn = _texto("PG_DSN")
    if not pg_dsn:
        raise ErroConfiguracao("PG_DSN é obrigatório")

    modo = _texto("INGESTION_MODE").upper() or None
    if modo and modo not in MODOS:
        raise ErroConfiguracao(f"INGESTION_MODE deve ser um de {MODOS}, veio {modo!r}")

    max_attempts = _inteiro("MAX_ATTEMPTS", 3)
    if max_attempts < 1:
        raise ErroConfiguracao("MAX_ATTEMPTS deve ser >= 1")

    retention = _inteiro("RETENTION_MONTHS", 24)
    if retention < 1:
        raise ErroConfiguracao("RETENTION_MONTHS deve ser >= 1")

    return Config(
        bucket=bucket,
        prefixo=_texto("TENABLE_PREFIX"),
        pg_dsn=pg_dsn,
        modo_forcado=modo,
        max_attempts=max_attempts,
        versoes_esperadas=_versoes_esperadas(),
        cloudwatch_namespace=_texto("CLOUDWATCH_NAMESPACE", "TenableIngestion"),
        cloudwatch_habilitado=_booleano("CLOUDWATCH_ENABLED", False),
        retention_months=retention,
        retencao_ingest_file_dias=_inteiro("INGEST_FILE_RETENTION_DAYS", 90),
        horas_sem_manifest_alerta=_inteiro("MANIFEST_STALE_HOURS", 6),
        region_name=_texto("AWS_REGION", "us-east-1"),
        aws_access_key_id=_texto("AWS_ACCESS_KEY_ID") or None,
        aws_secret_access_key=_texto("AWS_SECRET_ACCESS_KEY") or None,
        aws_session_token=_texto("AWS_SESSION_TOKEN") or None,
        s3_retry_max_attempts=_inteiro("S3_RETRY_MAX_ATTEMPTS", 8),
        s3_retry_base_delay_seconds=_decimal("S3_RETRY_BASE_DELAY_SECONDS", 0.25),
        s3_retry_max_delay_seconds=_decimal("S3_RETRY_MAX_DELAY_SECONDS", 6.0),
        log_level=_texto("LOG_LEVEL", "INFO").upper(),
    )


def configurar_logging(nivel: str = "INFO") -> None:
    """Logging estruturado o suficiente para o CloudWatch Logs agrupar.

    Níveis conforme a seção 13.4: INFO por payload, WARNING para skip/versão
    divergente/quarentena, ERROR para integridade e exceção."""
    logging.basicConfig(
        level=getattr(logging, nivel, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def resumo_config(config: Config) -> dict[str, Any]:
    """Config sem segredo, para log de início de execução."""
    return {
        "bucket": config.bucket,
        "prefixo": config.prefixo or "(raiz)",
        "tipos": list(TIPOS_PAYLOAD),
        "modo_forcado": config.modo_forcado,
        "max_attempts": config.max_attempts,
        "retention_months": config.retention_months,
        "cloudwatch": config.cloudwatch_namespace if config.cloudwatch_habilitado else "off",
    }
