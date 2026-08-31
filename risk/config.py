"""Configuração do motor.

Reaproveita `PG_DSN` da ingestão de propósito: é o mesmo banco, e duplicar a
variável só criaria a chance de os dois jobs apontarem para lugares diferentes.
O resto é específico do motor — as credenciais das fontes que o Data Stream não
cobre.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ingestion.erros import ErroConfiguracao

# Sobe a cada mudança de regra que altere o resultado do cálculo. Fica gravada
# em cada linha de `finding_risk`: é o que permite saber com que versão do motor
# um score foi produzido, e reverter a imagem sem tocar na ingestão.
VERSAO_MOTOR = "1.0.0"

CSV_ARQUITETURA_PADRAO = Path(__file__).parent / "referencia" / "arquitetura.csv"


@dataclass(frozen=True)
class ConfigMotor:
    pg_dsn: str
    csv_arquitetura: Path = CSV_ARQUITETURA_PADRAO
    versao: str = VERSAO_MOTOR
    lote: int = 20_000
    log_level: str = "INFO"

    # CMDB (Atlassian Assets/JSM)
    jira_email: str | None = None
    jira_token: str | None = None
    jira_base_url: str | None = None
    jira_cloud_id: str | None = None
    cmdb_cache_horas: float = 24.0

    # Threat intel (API clássica do Tenable)
    tenable_access_key: str | None = None
    tenable_secret_key: str | None = None
    verify_tls: bool = True

    # Vault (keywords de camada)
    vault_mount: str = "config"
    vault_layer_secret_path: str = "tenable-vulnerabilities-templates"

    @property
    def tem_cmdb(self) -> bool:
        return bool(self.jira_email and self.jira_token and self.jira_base_url)

    @property
    def tem_intel(self) -> bool:
        return bool(self.tenable_access_key and self.tenable_secret_key)


def _booleano(nome: str, padrao: bool) -> bool:
    valor = os.getenv(nome)
    if valor is None:
        return padrao
    return valor.strip().lower() not in {"0", "false", "no", "nao", "não"}


def carregar_config() -> ConfigMotor:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ModuleNotFoundError:  # pragma: no cover - ambiente sem dotenv
        pass

    dsn = os.getenv("PG_DSN", "").strip()
    if not dsn:
        raise ErroConfiguracao("PG_DSN não definido — o motor precisa dele para conectar")

    csv_arquitetura = os.getenv("RISK_ARQUITETURA_CSV", "").strip()

    return ConfigMotor(
        pg_dsn=dsn,
        csv_arquitetura=Path(csv_arquitetura) if csv_arquitetura else CSV_ARQUITETURA_PADRAO,
        versao=os.getenv("RISK_ENGINE_VERSION", VERSAO_MOTOR),
        lote=int(os.getenv("RISK_LOTE", "20000")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        jira_email=os.getenv("JIRA_EMAIL") or None,
        jira_token=os.getenv("JIRA_TOKEN") or None,
        jira_base_url=os.getenv("JIRA_BASE_URL") or None,
        jira_cloud_id=os.getenv("JIRA_CLOUD_ID") or None,
        cmdb_cache_horas=float(os.getenv("CMDB_CACHE_HOURS", "24")),
        tenable_access_key=os.getenv("ACCESS_KEY") or None,
        tenable_secret_key=os.getenv("SECRET_KEY") or None,
        verify_tls=_booleano("VERIFY_TLS", True),
        vault_mount=os.getenv("VAULT_MOUNT", "config"),
        vault_layer_secret_path=os.getenv(
            "VAULT_LAYER_SECRET_PATH", "tenable-vulnerabilities-templates"
        ),
    )
