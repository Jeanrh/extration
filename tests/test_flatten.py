"""Achatamento dos payloads reais (SPEC seções 7.1, 7.2, 7.3, 7.4, 7.5).

Roda sem banco e sem AWS: é o teste da Fase 0 do plano de execução.
"""

from __future__ import annotations

import datetime as dt
import decimal
import re
from pathlib import Path

import pytest

from conftest import RAIZ  # noqa: F401  (garante sys.path)
from fixtures import Bucket, envelope, enriched, finding_vm, finding_was

from ingestion.config import TIPOS_PAYLOAD
from ingestion.erros import ErroParse
from ingestion.manifest import EntradaPayload, parse_manifest
from ingestion.payload import (
    LinhaFinding,
    LinhaPlugin,
    LinhaRecast,
    achatar_payload,
    booleano,
    colunas,
    limpar,
    lista_texto,
    natural_key_vm,
    natural_key_was,
    numero,
    timestamp,
)

VM = TIPOS_PAYLOAD["FINDING"]
WAS = TIPOS_PAYLOAD["WAS_FINDING"]
ENRICHED = TIPOS_PAYLOAD["FINDING_ENRICHED_ATTRIBUTES"]

ENTRADA = EntradaPayload(
    path="prod/finding/2026-08-27/finding-1.json.gz",
    md5=None,
    version=1,
    num_updates=None,
    num_deletes=None,
    first_record_timestamp=dt.datetime(2026, 8, 27, 10, 0, tzinfo=dt.timezone.utc),
    last_record_timestamp=dt.datetime(2026, 8, 27, 10, 30, tzinfo=dt.timezone.utc),
    scan_id="scan-1",
)


def _um(tipo, updates=None, deletes=None, entrada=ENTRADA):
    doc = envelope(tipo.nome, updates or [], deletes or [])
    return achatar_payload(tipo, doc, entrada)


def test_adaptador_de_registro_tem_interface_uniforme_para_os_tres_payload_types():
    from ingestion.payload import achatar_registro

    relogio = dt.datetime(2026, 8, 27, 12, 0, tzinfo=dt.timezone.utc)
    casos = [
        (
            VM,
            finding_vm(finding_id="vm-interface", indexed="2026-08-27T12:00:00Z"),
            False,
            ("vm-interface", "OPEN", 14272, None, relogio),
        ),
        (
            WAS,
            finding_was(
                finding_id="was-interface",
                state="ACTIVE",
                indexed_at="2026-08-27T12:00:00Z",
            ),
            False,
            ("was-interface", "OPEN", 114966, None, relogio),
        ),
        (
            VM,
            {"_id": "vm-oficial", "id": "vm-fallback"},
            True,
            ("vm-oficial", None, None, None, ENTRADA.last_record_timestamp),
        ),
        (
            WAS,
            {"id": "was-oficial", "_id": "was-fallback"},
            True,
            ("was-oficial", None, None, None, ENTRADA.last_record_timestamp),
        ),
        (
            ENRICHED,
            enriched(
                finding_id="recast-interface",
                updated_at="2026-08-27T12:00:00Z",
            ),
            False,
            (None, None, None, "recast-interface", relogio),
        ),
        (
            ENRICHED,
            {
                "id": "recast-delete-oficial",
                "_id": "recast-delete-fallback",
                "deleted_at": "2026-08-27T12:00:00Z",
            },
            True,
            (None, None, None, "recast-delete-oficial", relogio),
        ),
    ]

    for seq, (tipo, registro, is_delete, esperado) in enumerate(casos, start=10):
        finding = achatar_registro(
            tipo, registro, is_delete=is_delete, seq=seq, entrada=ENTRADA,
            destino="finding",
        )
        plugin = achatar_registro(
            tipo, registro, is_delete=is_delete, seq=seq, entrada=ENTRADA,
            destino="plugin",
        )
        recast = achatar_registro(
            tipo, registro, is_delete=is_delete, seq=seq, entrada=ENTRADA,
            destino="recast",
        )
        observado = (
            finding.finding_id if finding else None,
            finding.state if finding else None,
            plugin.plugin_id if plugin else None,
            recast.finding_id if recast else None,
            (
                recast.source_indexed
                if recast
                else plugin.indexed if plugin else finding.indexed if finding else None
            ),
        )
        assert observado == esperado
        assert (finding or plugin or recast).seq == seq


# ---------------------------------------------------------------------------
# Normalizadores
# ---------------------------------------------------------------------------
def test_string_null_vira_none():
    """`plugin.patch_publication_date` chega como a string "null" no VM real."""
    assert limpar("null") is None
    assert limpar("  null  ") is None
    assert limpar("NONE") == "NONE", "NONE é valor legítimo de vetor CVSS"
    assert limpar("nullable") == "nullable"


def test_lista_texto_normaliza_bid_de_int_e_de_string():
    assert lista_texto([14272]) == ["14272"]       # VM
    assert lista_texto(["114966"]) == ["114966"]   # WAS
    assert lista_texto(None) is None               # plugin.cve vem null
    assert lista_texto([]) == []


def test_numero_aceita_string():
    assert numero("7") == decimal.Decimal("7")
    assert numero(None) is None
    assert numero("") is None
    assert numero("nao-numero") is None


def test_booleano_e_timestamp():
    assert booleano(True) is True
    assert booleano("false") is False
    assert booleano(None) is None
    convertido = timestamp("2026-08-27T10:32:22.420Z")
    assert convertido == dt.datetime(2026, 8, 27, 10, 32, 22, 420000, tzinfo=dt.timezone.utc)
    assert timestamp("null") is None
    assert timestamp("1787826739356").year == 2026   # epoch ms em string


# ---------------------------------------------------------------------------
# VM (seção 7.1)
# ---------------------------------------------------------------------------
def test_vm_mapeia_o_payload_real():
    achatado = _um(VM, [finding_vm()])
    assert achatado.num_updates == 1
    linha = achatado.findings[0]

    assert linha.finding_id == "05da4cb0-8764-5c1f-ad0a-8aed238853b7"
    assert linha.product == "VM"
    assert linha.is_delete is False
    assert linha.state == "OPEN"
    assert linha.severity == "INFO", "VM manda 'info' minúsculo; normaliza na escrita"
    assert linha.severity_modification_type == "NONE"
    assert linha.plugin_id == 14272
    assert linha.plugin_name == "Netstat Portscanner (SSH)"
    assert linha.asset_hostname == "vvcelhml0146"
    assert linha.asset_fqdn == "vvcelhml0146.ccorp.local"
    assert linha.asset_ipv4 == "10.88.170.149"
    assert linha.asset_tracked is True
    assert linha.asset_operating_system[0].startswith("Linux Kernel")
    assert linha.port_number == 0
    assert linha.port_protocol == "TCP"
    assert linha.source == "AGENT"
    assert linha.time_taken_to_fix is None


def test_vm_usa_indexed_como_relogio_nao_last_found():
    """`last_found` NÃO serve de relógio (seção 6.4)."""
    linha = _um(VM, [finding_vm()]).findings[0]
    assert linha.indexed == dt.datetime(
        2026, 8, 27, 10, 32, 22, 420000, tzinfo=dt.timezone.utc
    )
    assert linha.last_found != linha.indexed


def test_vm_output_json_serializado_fica_texto():
    """O `output` do VM contém JSON como string: guardar como texto, NÃO parsear."""
    linha = _um(VM, [finding_vm()]).findings[0]
    assert isinstance(linha.output, str)
    assert linha.output.startswith('{"listening"')


def test_relogio_cai_no_manifest_quando_indexed_falta():
    linha = _um(VM, [finding_vm(indexed=None)]).findings[0]
    assert linha.indexed == ENTRADA.last_record_timestamp


def test_sem_relogio_algum_falha_alto():
    sem_relogio = EntradaPayload(
        path="p", md5=None, version=1, num_updates=None, num_deletes=None,
        first_record_timestamp=None, last_record_timestamp=None, scan_id=None,
    )
    with pytest.raises(ErroParse, match="relógio de versão"):
        _um(VM, [finding_vm(indexed=None)], entrada=sem_relogio)


# ---------------------------------------------------------------------------
# WAS (seção 7.2)
# ---------------------------------------------------------------------------
def test_was_mapeia_o_payload_real():
    linha = _um(WAS, [finding_was()]).findings[0]

    assert linha.product == "WAS"
    assert linha.state == "OPEN"
    assert linha.severity == "INFO"
    assert linha.plugin_id == 114966
    assert linha.url.startswith("https://meucheckoutsandbox")
    assert linha.input_type == "form"
    assert "<button" in linha.input_name
    assert linha.proof.startswith("<button")
    assert linha.payload == ""
    assert linha.asset_fqdn == "meucheckoutsandbox-akamai.braspag.com.br"
    # colunas de VM ficam nulas
    assert linha.port_number is None
    assert linha.asset_hostname is None
    assert linha.source is None


def test_was_indexed_at_vira_indexed():
    """WAS chama o relógio de `indexed_at`; o banco tem uma coluna só."""
    linha = _um(WAS, [finding_was()]).findings[0]
    assert linha.indexed == dt.datetime(
        2026, 8, 27, 12, 19, 34, 541000, tzinfo=dt.timezone.utc
    )
    # duas horas de defasagem contra last_found — daí não usar last_found
    assert (linha.indexed - linha.last_found).total_seconds() == pytest.approx(7200, abs=60)


def test_was_active_vira_open_sem_alterar_o_raw():
    """A documentação lista ACTIVE, mas define seu estado de API como OPEN."""
    linha = _um(WAS, [finding_was(state="ACTIVE")]).findings[0]

    assert linha.state == "OPEN"
    assert linha.raw["state"] == "ACTIVE"


# ---------------------------------------------------------------------------
# Plugin (seção 7.4)
# ---------------------------------------------------------------------------
def test_plugin_vm_normaliza_risk_factor_e_cvss2():
    plugin = _um(VM, [finding_vm()]).plugins[0]
    assert plugin.plugin_id == 14272
    assert plugin.risk_factor == "INFO", "VM manda 'info'"
    assert plugin.family == "Port scanners"
    assert plugin.see_also == ["https://en.wikipedia.org/wiki/Netstat"]
    assert plugin.cve is None, "plugin.cve vem null, não array vazio"
    assert plugin.cpe == []
    assert plugin.patch_publication_date is None, 'veio a string "null"'
    assert plugin.publication_date.year == 2004
    assert plugin.exploit_available is False


def test_plugin_was_usa_cvss2_base_score_e_ignora_vpr_v2():
    """VM chama `cvss_base_score`, WAS chama `cvss2_base_score`.

    `vpr_v2` foi deprecado em 01/07/2026 e sai em 01/10/2026: o valor tem que
    sair de `plugin.vpr`."""
    registro = finding_was()
    registro["plugin"]["cvss2_base_score"] = 4.3
    registro["plugin"]["vpr"]["score"] = 6.7
    registro["plugin"]["vpr_v2"]["score"] = 9.9

    plugin = _um(WAS, [registro]).plugins[0]
    assert plugin.cvss2_base_score == decimal.Decimal("4.3")
    assert plugin.vpr_score == decimal.Decimal("6.7")
    assert plugin.risk_factor == "INFO"
    assert plugin.cwe is None


def test_plugin_sem_id_nao_gera_linha():
    registro = finding_vm()
    registro["plugin"] = {"name": "sem id"}
    assert _um(VM, [registro]).plugins == []


# ---------------------------------------------------------------------------
# Deletes (seção 4.4 / 6.7)
# ---------------------------------------------------------------------------
def test_delete_de_finding_usa_underscore_id():
    """`deletes[]._id` no finding vs `deletes[].id` no asset. Um parser
    genérico que assuma `id` perde todos os deletes de finding em silêncio."""
    achatado = _um(
        VM, [], [{"_id": "abc-123", "deleted_at": "2026-08-27T11:00:00Z"}]
    )
    linha = achatado.findings[0]
    assert linha.finding_id == "abc-123"
    assert linha.is_delete is True
    assert linha.state is None, "delete não mexe em state — não é remediação"
    assert linha.deleted_at == dt.datetime(2026, 8, 27, 11, 0, tzinfo=dt.timezone.utc)
    assert linha.indexed == ENTRADA.last_record_timestamp


def test_delete_de_was_prefere_id_oficial_ao_fallback_legado():
    linha = _um(
        WAS,
        [],
        [{
            "id": "was-oficial",
            "_id": "was-legado",
            "deleted_at": "2026-08-27T11:00:00Z",
        }],
    ).findings[0]

    assert linha.finding_id == "was-oficial"
    assert linha.product == "WAS"


def test_delete_de_was_aceita_underscore_id_como_fallback():
    linha = _um(
        WAS,
        [],
        [{"_id": "was-legado", "deleted_at": "2026-08-27T11:00:00Z"}],
    ).findings[0]

    assert linha.finding_id == "was-legado"


def test_delete_sem_id_reconhecivel_falha():
    with pytest.raises(ErroParse, match="_id"):
        _um(VM, [], [{"deleted_at": "2026-08-27T11:00:00Z"}])


def test_seq_continua_dos_updates_para_os_deletes():
    achatado = _um(
        VM,
        [finding_vm(finding_id="a"), finding_vm(finding_id="b")],
        [{"_id": "c", "deleted_at": "2026-08-27T11:00:00Z"}],
    )
    assert [linha.seq for linha in achatado.findings] == [0, 1, 2]


# ---------------------------------------------------------------------------
# Enriched (seção 7.3)
# ---------------------------------------------------------------------------
def test_enriched_mapeia_recast():
    achatado = _um(ENRICHED, [enriched()])
    assert achatado.findings == [] and achatado.plugins == []
    recast = achatado.recasts[0]
    assert recast.finding_id == "c13f1d15-3b0a-520e-b5f0-8606db37f357"
    assert recast.source == "recast-platform"
    assert recast.rule_id == "f766e180-bf4b-45f0-8b90-c7b0d62bccd2"
    assert recast.modification == "RECASTED"
    assert recast.modification_target == "RISK"
    assert recast.recasted_severity == "NONE"
    assert recast.is_delete is False
    assert recast.rule_updated_at.year == 2026
    assert recast.source_indexed == recast.rule_updated_at


def test_enriched_update_usa_created_at_quando_updated_at_falta():
    recast = _um(
        ENRICHED,
        [enriched(updated_at=None, created_at="2026-08-26T09:00:00Z")],
    ).recasts[0]

    assert recast.source_indexed == dt.datetime(
        2026, 8, 26, 9, 0, tzinfo=dt.timezone.utc
    )


def test_enriched_update_usa_manifest_como_ultimo_fallback_de_relogio():
    recast = _um(
        ENRICHED,
        [enriched(updated_at=None, created_at=None)],
    ).recasts[0]

    assert recast.source_indexed == ENTRADA.last_record_timestamp


def test_enriched_update_sem_finding_id_falha_alto():
    with pytest.raises(ErroParse, match="enriched.*finding_id"):
        _um(ENRICHED, [{"recast_properties": {"source": "x"}}])


def test_enriched_update_sem_relogio_falha_alto():
    sem_relogio = EntradaPayload(
        path="enriched-sem-relogio",
        md5=None,
        version=1,
        num_updates=None,
        num_deletes=None,
        first_record_timestamp=None,
        last_record_timestamp=None,
        scan_id=None,
    )

    with pytest.raises(ErroParse, match="enriched.*relógio"):
        _um(
            ENRICHED,
            [enriched(updated_at=None, created_at=None)],
            entrada=sem_relogio,
        )


def test_enriched_delete_prefere_id_oficial_e_usa_deleted_at_como_relogio():
    recast = _um(
        ENRICHED,
        [],
        [{
            "id": "atributo-oficial",
            "_id": "atributo-legado",
            "deleted_at": "2026-08-27T11:00:00Z",
        }],
    ).recasts[0]

    esperado = dt.datetime(2026, 8, 27, 11, 0, tzinfo=dt.timezone.utc)
    assert recast.finding_id == "atributo-oficial"
    assert recast.deleted_at == esperado
    assert recast.source_indexed == esperado


def test_enriched_delete_aceita_underscore_id_como_fallback():
    recast = _um(
        ENRICHED,
        [],
        [{"_id": "atributo-legado", "deleted_at": "2026-08-27T11:00:00Z"}],
    ).recasts[0]

    assert recast.finding_id == "atributo-legado"


def test_enriched_delete_sem_identidade_falha_alto():
    with pytest.raises(ErroParse, match="enriched.*delete.*id"):
        _um(ENRICHED, [], [{"deleted_at": "2026-08-27T11:00:00Z"}])


def test_enriched_delete_sem_relogio_falha_alto():
    sem_relogio = EntradaPayload(
        path="enriched-delete-sem-relogio",
        md5=None,
        version=1,
        num_updates=None,
        num_deletes=None,
        first_record_timestamp=None,
        last_record_timestamp=None,
        scan_id=None,
    )

    with pytest.raises(ErroParse, match="enriched.*relógio"):
        _um(ENRICHED, [], [{"id": "atributo"}], entrada=sem_relogio)


# ---------------------------------------------------------------------------
# Chave natural (seção 5.4)
# ---------------------------------------------------------------------------
def test_natural_key_e_estavel_e_insensivel_a_caixa():
    a = natural_key_vm("HOST01", 14272, 22, "tcp")
    b = natural_key_vm("host01", 14272, 22, "TCP")
    assert a == b
    assert len(a) == 64
    assert a != natural_key_vm("host02", 14272, 22, "TCP")


def test_natural_key_was_absorve_input_name_gigante():
    grande = "x" * 5000
    chave = natural_key_was("site.com.br", 114966, "https://SITE.com.br/a", grande)
    assert len(chave) == 64
    assert chave != natural_key_was("site.com.br", 114966, "https://site.com.br/a", "y")


def test_natural_key_do_payload_real_bate_com_o_calculo_direto():
    linha = _um(VM, [finding_vm()]).findings[0]
    assert linha.natural_key == natural_key_vm("vvcelhml0146", 14272, 0, "TCP")


# ---------------------------------------------------------------------------
# Contrato Python ↔ SQL
# ---------------------------------------------------------------------------
def _colunas_do_ddl(tabela: str) -> list[str]:
    ddl = (RAIZ / "ingestion" / "sql" / "00_staging.sql").read_text(encoding="utf-8")
    bloco = re.search(rf"CREATE TEMP TABLE {tabela} \((.*?)\n\) ON COMMIT DROP;", ddl, re.S)
    assert bloco, f"tabela {tabela} não encontrada em 00_staging.sql"
    nomes = []
    for linha in bloco.group(1).splitlines():
        limpa = linha.strip()
        if not limpa or limpa.startswith("--"):
            continue
        nomes.append(limpa.split()[0])
    return nomes


@pytest.mark.parametrize(
    "tabela,classe",
    [("stg_finding", LinhaFinding), ("stg_plugin", LinhaPlugin), ("stg_recast", LinhaRecast)],
)
def test_staging_bate_com_dataclass(tabela, classe):
    """O COPY monta a lista de colunas a partir da ordem dos campos do
    dataclass. Se o SQL e o Python saírem de sincronia, os valores entram na
    coluna errada em silêncio — este teste é o que impede isso."""
    assert _colunas_do_ddl(tabela) == list(colunas(classe))


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def test_manifest_preserva_a_ordem_do_array():
    bucket = Bucket()
    primeiro = bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="a")]))
    segundo = bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="b")]))
    caminho = bucket.fechar_manifest("FINDING")

    import json

    manifest = parse_manifest(caminho, json.loads(bucket.store[caminho]))
    assert [e.path for e in manifest.payloads] == [primeiro, segundo]
    assert manifest.payload_type == "FINDING"
    assert manifest.payloads[0].num_updates == 1
    assert manifest.payloads[0].last_record_timestamp.year == 2026


def test_manifest_scan_id_vazio_vira_none():
    import json

    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm()]), scan_id="")
    caminho = bucket.fechar_manifest("FINDING")
    manifest = parse_manifest(caminho, json.loads(bucket.store[caminho]))
    assert manifest.payloads[0].scan_id is None


def test_whitelist_tem_exatamente_tres_tipos():
    """A lista é fixa em configuração; adicionar um tipo é mudança de código
    consciente. É o que mantém `tds_test_file` e `host_audit_finding` fora."""
    assert set(TIPOS_PAYLOAD) == {
        "FINDING", "WAS_FINDING", "FINDING_ENRICHED_ATTRIBUTES"
    }
    assert "HOST_AUDIT_FINDING" not in TIPOS_PAYLOAD


def test_plugin_promove_exploitability_ease_como_texto():
    """`nota_exploit` do motor de risco lê este campo como texto.

    O Data Stream manda `null` onde a API clássica manda string, e a regra de
    scoring trata os dois: vazio ou "no known exploit" vale 10, qualquer outro
    texto vale 100. Por isso o campo é promovido como texto, não como boolean."""
    assert _um(VM, [finding_vm()]).plugins[0].exploitability_ease is None

    registro = finding_vm()
    registro["plugin"]["exploitability_ease"] = "Exploits are available"
    assert (
        _um(VM, [registro]).plugins[0].exploitability_ease == "Exploits are available"
    )
