"""
Definição dos relatórios (os "mapas"). Cada `Relatorio` diz de qual stream ler,
para onde escrever e o mapa `{coluna_do_csv: origem_no_payload}`.

Para criar um relatório novo: adicione um `Relatorio(...)` e inclua-o em
`RELATORIOS`. Nada de motor aqui — só dados.

spec de coluna (ver tenable_core.resolver_valor):
  "plugin.name"                  -> chave achatada do payload
  ("asset.hostname", "url")      -> cadeia de fallback (1º não vazio vence)
  data("first_found")            -> formata dd/mm/aaaa
  <callable>                     -> fn(registro) -> valor
"""

from __future__ import annotations

import datetime as dt

from tenable_core import Relatorio, data, dias_entre, montar_objeto_por_prefixo


# ---------------------------------------------------------------------------
# Helpers de coluna específicos de relatório
# ---------------------------------------------------------------------------
_SEVERIDADE_PT = {
    "info": "Informativo",
    "low": "Baixo",
    "medium": "Médio",
    "high": "Alto",
    "critical": "Crítico",
}


def _prioridade(registro):
    return _SEVERIDADE_PT.get(str(registro.get("severity") or "").strip().lower())


def _aging(registro):
    """Dias em aberto: hoje - first_found; se corrigido, last_fixed - first_found."""
    inicio = registro.get("first_found")
    if not inicio:
        return None
    if str(registro.get("state") or "").strip().upper() == "FIXED":
        fim = registro.get("last_fixed") or registro.get("last_found")
    else:
        fim = dt.datetime.now(dt.timezone.utc).isoformat()
    return dias_entre(inicio, fim)


def _dias_para_corrigir(registro):
    """days_taken_to_fix: first_found -> last_fixed (ou last_found se ainda aberto)."""
    fim = registro.get("last_fixed") or registro.get("last_found")
    return dias_entre(registro.get("first_found"), fim)


def _recast_properties_obj(registro):
    return montar_objeto_por_prefixo(registro, "enriched.recast_properties")


# ---------------------------------------------------------------------------
# Fragmento reaproveitável: colunas de recast/aceite (stream
# finding_enriched_attributes). Anexado a intranet e internet.
# ---------------------------------------------------------------------------
COLUNAS_RECAST_ENRICHED = {
    "severity_modification_type": "severity_modification_type",
    "enriched.recast_properties.source": "enriched.recast_properties.source",
    "enriched.recast_properties.recast_annotation.rule_id": "enriched.recast_properties.recast_annotation.rule_id",
    "enriched.recast_properties.recast_annotation.rule_comment": "enriched.recast_properties.recast_annotation.rule_comment",
    "enriched.recast_properties.recast_annotation.modification_target": "enriched.recast_properties.recast_annotation.modification_target",
    "enriched.recast_properties.recast_annotation.recasted_severity": "enriched.recast_properties.recast_annotation.recasted_severity",
    "recast_modification": "enriched.recast_properties.recast_annotation.modification",
    "recast_properties.recast_annotation.modification": "enriched.recast_properties.recast_annotation.modification",
    "recast_properties": _recast_properties_obj,
}


# ---------------------------------------------------------------------------
# vm_findings / was_findings — conjunto enxuto, nomes crus do payload, datas
# em dd/mm/aaaa. Recast vem inline (severity_modification_type). Sem dedupe.
# ---------------------------------------------------------------------------
VM_FINDINGS = Relatorio(
    nome="vm_findings",
    fontes=("vm",),
    saida="csv/tenable_vm_findings_completov2.csv",
    dias_last_found=30,
    record_source=True,
    colunas={
        "finding_id": "finding_id",
        "output": "output",
        "state": "state",
        "severity": "severity",
        "severity_id": "severity_id",
        "severity_modification_type": "severity_modification_type",
        "recast_reason": "recast_reason",
        "recast_rule_uuid": "recast_rule_uuid",
        "first_found": data("first_found"),
        "last_found": data("last_found"),
        "last_fixed": data("last_fixed"),
        "indexed": data("indexed"),
        "plugin.id": "plugin.id",
        "plugin.name": "plugin.name",
        "plugin.solution": "plugin.solution",
        "plugin.synopsis": "plugin.synopsis",
        "plugin.description": "plugin.description",
        "plugin.exploitability_ease": "plugin.exploitability_ease",
        "plugin.cvss3_base_score": "plugin.cvss3_base_score",
        "plugin.cve": "plugin.cve",
        "asset.uuid": "asset.uuid",
        "asset.fqdn": "asset.fqdn",
        "asset.ipv4": "asset.ipv4",
    },
)

WAS_FINDINGS = Relatorio(
    nome="was_findings",
    fontes=("was",),
    saida="csv/tenable_was_findings_completov2.csv",
    dias_last_found=7,
    record_source=True,
    colunas={
        "finding_id": "finding_id",
        "url": "url",
        "output": "output",
        "state": "state",
        "severity": "severity",
        "severity_id": "severity_id",
        "severity_modification_type": "severity_modification_type",
        "recast_reason": "recast_reason",
        "recast_rule_uuid": "recast_rule_uuid",
        "first_found": data("first_found"),
        "last_found": data("last_found"),
        "last_fixed": data("last_fixed"),
        "last_observed": data("last_observed"),
        "indexed_at": data("indexed_at"),
        "plugin.id": "plugin.id",
        "plugin.intel_type": "plugin.intel_type",
        "plugin.name": "plugin.name",
        "plugin.solution": "plugin.solution",
        "plugin.synopsis": "plugin.synopsis",
        "plugin.description": "plugin.description",
        "plugin.exploitability_ease": "plugin.exploitability_ease",
        "plugin.cvss3_base_score": "plugin.cvss3_base_score",
        "plugin.see_also": "plugin.see_also",
        "asset.uuid": "asset.uuid",
        "asset.fqdn": "asset.fqdn",
        "asset.ipv4s": "asset.ipv4s",
    },
)


# ---------------------------------------------------------------------------
# intranet (VM) / internet (WAS) — nomes "amigáveis", datas ISO cruas,
# dedupe + merge do stream enriched. CVSS v2: VM usa `cvss_*`, WAS `cvss2_*`.
# ---------------------------------------------------------------------------
INTRANET = Relatorio(
    nome="intranet",
    fontes=("vm",),
    saida="csv/tenable_intranet_full.csv",
    dedupe=True,
    merge_enriched=True,
    colunas={
        "finding_id": "finding_id",
        "asset_uuid": "asset.uuid",
        "asset_name": ("asset.hostname", "asset.fqdn"),
        "fqdn": "asset.fqdn",
        "ipv4": "asset.ipv4",
        "operating_system": "asset.operating_system",
        "device_type": "asset.device_type",
        "cve": "plugin.cve",
        "family": "plugin.family",
        "name": "plugin.name",
        "plugin_id": "plugin.id",
        "exploit_available": "plugin.exploit_available",
        "exploitability_ease": "plugin.exploitability_ease",
        "description": "plugin.description",
        "see_also": "plugin.see_also",
        "solution": "plugin.solution",
        "has_patch": "plugin.has_patch",
        "patch_publication_date": "plugin.patch_publication_date",
        "unsupported_by_vendor": "plugin.unsupported_by_vendor",
        "cvss3_base_score": "plugin.cvss3_base_score",
        "cvss3_vector_raw": "plugin.cvss3_vector.raw",
        "cvss3_access_vector": "plugin.cvss3_vector.access_vector",
        "cvss3_access_complexity": "plugin.cvss3_vector.access_complexity",
        "cvss3_availability_impact": "plugin.cvss3_vector.availability_impact",
        "cvss3_confidentiality_impact": "plugin.cvss3_vector.confidentiality_impact",
        "cvss3_integrity_impact": "plugin.cvss3_vector.integrity_impact",
        "cvss3_temporal_vector_raw": "plugin.cvss3_temporal_vector.raw",
        "cvss3_exploitability": "plugin.cvss3_temporal_vector.exploitability",
        "cvss3_remediation_level": "plugin.cvss3_temporal_vector.remediation_level",
        "cvss3_report_confidence": "plugin.cvss3_temporal_vector.report_confidence",
        "cvss2_base_score": ("plugin.cvss_base_score", "plugin.cvss2_base_score"),
        "cvss2_vector_raw": ("plugin.cvss_vector.raw", "plugin.cvss2_vector.raw"),
        "cvss2_access_vector": ("plugin.cvss_vector.access_vector", "plugin.cvss2_vector.access_vector"),
        "cvss2_access_complexity": ("plugin.cvss_vector.access_complexity", "plugin.cvss2_vector.access_complexity"),
        "cvss2_authentication": ("plugin.cvss_vector.authentication", "plugin.cvss2_vector.authentication"),
        "cvss2_availability_impact": ("plugin.cvss_vector.availability_impact", "plugin.cvss2_vector.availability_impact"),
        "cvss2_confidentiality_impact": ("plugin.cvss_vector.confidentiality_impact", "plugin.cvss2_vector.confidentiality_impact"),
        "cvss2_integrity_impact": ("plugin.cvss_vector.integrity_impact", "plugin.cvss2_vector.integrity_impact"),
        "cvss2_temporal_vector_raw": ("plugin.cvss_temporal_vector.raw", "plugin.cvss2_temporal_vector.raw"),
        "cvss2_exploitability": ("plugin.cvss_temporal_vector.exploitability", "plugin.cvss2_temporal_vector.exploitability"),
        "cvss2_remediation_level": ("plugin.cvss_temporal_vector.remediation_level", "plugin.cvss2_temporal_vector.remediation_level"),
        "cvss2_report_confidence": ("plugin.cvss_temporal_vector.report_confidence", "plugin.cvss2_temporal_vector.report_confidence"),
        "first_found": "first_found",
        "last_found": "last_found",
        "last_fixed": "last_fixed",
        "output": "output",
        "protocol": "port.protocol",
        "port": "port.port",
        "severity": "severity",
        "source": "source",
        "state": "state",
        "has_workaround": ("plugin.has_workaround", "has_workaround"),
        "workaround": ("plugin.workaround", "workaround"),
        "days_taken_to_fix": _dias_para_corrigir,
        **COLUNAS_RECAST_ENRICHED,
    },
)

INTERNET = Relatorio(
    nome="internet",
    fontes=("was",),
    saida="csv/tenable_internet_full.csv",
    dedupe=True,
    merge_enriched=True,
    colunas={
        "finding_id": "finding_id",
        "asset_uuid": "asset.uuid",
        "asset_name": ("url", "asset.fqdn"),
        "fqdn": "asset.fqdn",
        "ipv4": ("asset.ipv4", "asset.ipv4s"),
        "cve": "plugin.cve",
        "name": "plugin.name",
        "plugin_id": "plugin.id",
        "exploitability_ease": "plugin.exploitability_ease",
        "description": "plugin.description",
        "see_also": "plugin.see_also",
        "wasc": "plugin.wasc",
        "vpr": ("plugin.vpr.score", "plugin.vpr_v2.score"),
        "solution": "plugin.solution",
        "cvss3_base_score": "plugin.cvss3_base_score",
        "cvss3_vector_raw": "plugin.cvss3_vector.raw",
        "cvss3_access_vector": "plugin.cvss3_vector.access_vector",
        "cvss3_access_complexity": "plugin.cvss3_vector.access_complexity",
        "cvss3_availability_impact": "plugin.cvss3_vector.availability_impact",
        "cvss3_confidentiality_impact": "plugin.cvss3_vector.confidentiality_impact",
        "cvss3_integrity_impact": "plugin.cvss3_vector.integrity_impact",
        "cvss2_base_score": ("plugin.cvss2_base_score", "plugin.cvss_base_score"),
        "cvss2_vector_raw": ("plugin.cvss2_vector.raw", "plugin.cvss_vector.raw"),
        "cvss2_access_vector": ("plugin.cvss2_vector.access_vector", "plugin.cvss_vector.access_vector"),
        "cvss2_access_complexity": ("plugin.cvss2_vector.access_complexity", "plugin.cvss_vector.access_complexity"),
        "cvss2_authentication": ("plugin.cvss2_vector.authentication", "plugin.cvss_vector.authentication"),
        "cvss2_availability_impact": ("plugin.cvss2_vector.availability_impact", "plugin.cvss_vector.availability_impact"),
        "cvss2_confidentiality_impact": ("plugin.cvss2_vector.confidentiality_impact", "plugin.cvss_vector.confidentiality_impact"),
        "cvss2_integrity_impact": ("plugin.cvss2_vector.integrity_impact", "plugin.cvss_vector.integrity_impact"),
        "first_found": "first_found",
        "last_found": "last_found",
        "last_fixed": "last_fixed",
        "output": "output",
        "severity": "severity",
        "state": "state",
        **COLUNAS_RECAST_ENRICHED,
    },
)


# ---------------------------------------------------------------------------
# gestao_vuln — VM + WAS num CSV só, layout da planilha de gestão. Só as
# colunas que o Tenable preenche nativamente. Datas dd/mm/aaaa. Dedupe.
# ---------------------------------------------------------------------------
GESTAO_VULN = Relatorio(
    nome="gestao_vuln",
    fontes=("vm", "was"),
    saida="csv/tenable_gestao_vuln.csv",
    dedupe=True,
    record_source=True,
    colunas={
        "Prioridade": _prioridade,
        "Tenable ID": "asset.uuid",
        "ID Vulnerabilidade": "finding_id",
        "asset_name": ("asset.hostname", "url", "asset.fqdn"),
        "first_found": data("first_found"),
        "last_found": data("last_found"),
        "Nome Vulnerabilidade": "plugin.name",
        "Causa Raiz": "plugin.family",
        "output": "output",
        "Status Vulnerabilidade": "state",
        "Aging": _aging,
        "cve": "plugin.cve",
        "description": "plugin.description",
        "ipv4": ("asset.ipv4", "asset.ipv4s"),
        "operating_system": "asset.operating_system",
        "patch_publication_date": data("plugin.patch_publication_date"),
        "see_also": "plugin.see_also",
        "solution": "plugin.solution",
        "unsupported_by_vendor": "plugin.unsupported_by_vendor",
        "resurfaced_date": data("resurfaced_date"),
    },
)


RELATORIOS = {
    r.nome: r for r in (VM_FINDINGS, WAS_FINDINGS, INTRANET, INTERNET, GESTAO_VULN)
}
