"""Achatamento dos payloads: JSON do Tenable → linhas de staging.

Este é o **único** módulo que conhece o formato do Tenable. Uma função de
achatamento por `payload_type`, mesma assinatura. Quando o Tenable mudar o
schema, o estrago fica contido aqui.

Toda divergência entre VM e WAS é resolvida na escrita, nunca na query
(seção 6.5). Se `'INFO'` e `'info'` chegarem juntos ao banco, um dia alguém
compara os dois e perde metade do resultado.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import logging
from dataclasses import dataclass, field, fields
from typing import Any, Iterable, Mapping

from .config import PRODUTO_VM, PRODUTO_WAS, TipoPayload
from .erros import ErroParse
from .manifest import EntradaPayload

log = logging.getLogger(__name__)


# ===========================================================================
# Normalização de valores  (armadilhas reais da seção 7.5)
# ===========================================================================
def limpar(valor: Any) -> Any:
    """A Tenable serializa alguns campos ausentes como a **string** `"null"`
    (visto em `plugin.version`, `plugin.patch_publication_date`,
    `plugin.vuln_publication_date`, `plugin.vpr.updated`).

    `"NONE"` é preservado de propósito: é valor legítimo de vetor CVSS e de
    `recasted_severity`."""
    if isinstance(valor, str) and valor.strip() == "null":
        return None
    return valor


def texto(valor: Any) -> str | None:
    valor = limpar(valor)
    if valor is None:
        return None
    if isinstance(valor, str):
        return valor
    if isinstance(valor, bool):
        return "true" if valor else "false"
    if isinstance(valor, (dict, list)):
        raise ErroParse(f"esperava escalar, veio {type(valor).__name__}")
    return str(valor)


def maiusculo(valor: Any) -> str | None:
    convertido = texto(valor)
    return convertido.upper() if convertido else convertido


def minusculo(valor: Any) -> str | None:
    convertido = texto(valor)
    return convertido.lower() if convertido else convertido


def inteiro(valor: Any) -> int | None:
    valor = limpar(valor)
    if valor is None or valor == "" or isinstance(valor, bool):
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        try:
            return int(float(valor))
        except (TypeError, ValueError):
            return None


def numero(valor: Any) -> decimal.Decimal | None:
    """`acr_score`/`exposure_score` chegam como string (`"7"`) — seção 7.5."""
    valor = limpar(valor)
    if valor is None or valor == "" or isinstance(valor, bool):
        return None
    try:
        return decimal.Decimal(str(valor))
    except (decimal.InvalidOperation, TypeError, ValueError):
        return None


def booleano(valor: Any) -> bool | None:
    valor = limpar(valor)
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, str):
        baixo = valor.strip().lower()
        if baixo in {"true", "t", "1", "yes"}:
            return True
        if baixo in {"false", "f", "0", "no"}:
            return False
    return None


def lista_texto(valor: Any) -> list[str] | None:
    """Normaliza para `text[]`.

    Cobre dois casos reais: `plugin.cve` vem `null` em vez de array vazio, e
    `plugin.bid` vem `[14272]` (int) no VM e `["114966"]` (string) no WAS."""
    valor = limpar(valor)
    if valor is None:
        return None
    if not isinstance(valor, list):
        valor = [valor]
    saida: list[str] = []
    for item in valor:
        item = limpar(item)
        if item is None:
            continue
        saida.append(item if isinstance(item, str) else str(item))
    return saida


def timestamp(valor: Any) -> dt.datetime | None:
    """ISO com `Z` → timestamptz UTC. Aceita também epoch ms.

    Grava sempre em UTC, como o Tenable manda (seção 15). Converter na
    gravação perderia a referência original."""
    valor = limpar(valor)
    if valor is None or valor == "":
        return None
    if isinstance(valor, dt.datetime):
        return valor if valor.tzinfo else valor.replace(tzinfo=dt.timezone.utc)
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        return dt.datetime.fromtimestamp(float(valor) / 1000, tz=dt.timezone.utc)
    if not isinstance(valor, str):
        return None

    bruto = valor.strip()
    if not bruto:
        return None
    if bruto.isdigit():
        return dt.datetime.fromtimestamp(int(bruto) / 1000, tz=dt.timezone.utc)
    if bruto.endswith("Z"):
        bruto = f"{bruto[:-1]}+00:00"
    try:
        convertido = dt.datetime.fromisoformat(bruto)
    except ValueError:
        return None
    return convertido if convertido.tzinfo else convertido.replace(tzinfo=dt.timezone.utc)


def _primeiro(objeto: Mapping[str, Any], *chaves: str) -> Any:
    """Primeiro valor não nulo entre `chaves`. VM e WAS batizam o mesmo dado
    de formas diferentes (`cvss_base_score` vs `cvss2_base_score`)."""
    for chave in chaves:
        valor = limpar(objeto.get(chave))
        if valor is not None:
            return valor
    return None


# ===========================================================================
# Chave natural  (seção 5.4)
# ===========================================================================
def _hash_natural(partes: Iterable[Any]) -> str:
    texto_chave = "|".join("" if p is None else str(p) for p in partes)
    return hashlib.sha256(texto_chave.encode("utf-8")).hexdigest()


def natural_key_vm(
    asset_hostname: str | None,
    plugin_id: int | None,
    port_number: int | None,
    port_protocol: str | None,
) -> str:
    return _hash_natural(
        [
            (asset_hostname or "").lower(),
            "" if plugin_id is None else plugin_id,
            "" if port_number is None else port_number,
            (port_protocol or "").upper(),
        ]
    )


def natural_key_was(
    asset_fqdn: str | None,
    plugin_id: int | None,
    url: str | None,
    input_name: str | None,
) -> str:
    """`input_name` do WAS tem centenas de caracteres com HTML dentro — por
    isso entra no hash, não como coluna."""
    return _hash_natural(
        [
            (asset_fqdn or "").lower(),
            "" if plugin_id is None else plugin_id,
            (url or "").lower(),
            input_name or "",
        ]
    )


# ===========================================================================
# Linhas de staging
# ===========================================================================
@dataclass
class LinhaFinding:
    """A ordem dos campos É a ordem das colunas de `stg_finding`
    (00_staging.sql). `tests/test_flatten.py` guarda esse acordo."""

    seq: int
    finding_id: str
    product: str
    is_delete: bool
    state: str | None = None
    severity: str | None = None
    severity_id: int | None = None
    severity_default_id: int | None = None
    severity_modification_type: str | None = None
    recast_reason: str | None = None
    recast_rule_uuid: str | None = None
    plugin_id: int | None = None
    plugin_name: str | None = None
    asset_uuid: str | None = None
    asset_fqdn: str | None = None
    asset_hostname: str | None = None
    asset_ipv4: str | None = None
    asset_ipv6: str | None = None
    asset_mac_address: str | None = None
    asset_operating_system: list[str] | None = None
    asset_device_type: str | None = None
    asset_agent_uuid: str | None = None
    asset_network_id: str | None = None
    asset_tracked: bool | None = None
    port_number: int | None = None
    port_protocol: str | None = None
    port_service: str | None = None
    url: str | None = None
    input_type: str | None = None
    input_name: str | None = None
    http_method: str | None = None
    output: str | None = None
    proof: str | None = None
    payload: str | None = None
    first_found: dt.datetime | None = None
    last_found: dt.datetime | None = None
    last_fixed: dt.datetime | None = None
    last_observed: dt.datetime | None = None
    resurfaced_date: dt.datetime | None = None
    time_taken_to_fix: int | None = None
    indexed: dt.datetime | None = None
    scan_uuid: str | None = None
    scan_schedule_uuid: str | None = None
    scan_started_at: dt.datetime | None = None
    scan_completed_at: dt.datetime | None = None
    scan_target: str | None = None
    source: str | None = None
    natural_key: str = ""
    deleted_at: dt.datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LinhaPlugin:
    seq: int
    plugin_id: int
    indexed: dt.datetime | None = None
    name: str | None = None
    family: str | None = None
    risk_factor: str | None = None
    type: str | None = None
    synopsis: str | None = None
    description: str | None = None
    solution: str | None = None
    see_also: list[str] | None = None
    cve: list[str] | None = None
    cwe: list[str] | None = None
    cpe: list[str] | None = None
    cvss2_base_score: decimal.Decimal | None = None
    cvss3_base_score: decimal.Decimal | None = None
    cvss4_base_score: decimal.Decimal | None = None
    epss_score: decimal.Decimal | None = None
    vpr_score: decimal.Decimal | None = None
    exploit_available: bool | None = None
    exploited_by_malware: bool | None = None
    in_the_news: bool | None = None
    has_patch: bool | None = None
    unsupported_by_vendor: bool | None = None
    publication_date: dt.datetime | None = None
    patch_publication_date: dt.datetime | None = None
    modification_date: dt.datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LinhaRecast:
    seq: int
    finding_id: str
    is_delete: bool = False
    source: str | None = None
    rule_id: str | None = None
    rule_comment: str | None = None
    modification: str | None = None
    modification_target: str | None = None
    recasted_severity: str | None = None
    changed_result: str | None = None
    rule_created_at: dt.datetime | None = None
    rule_updated_at: dt.datetime | None = None
    deleted_at: dt.datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def colunas(classe: type) -> tuple[str, ...]:
    """Nomes das colunas de staging na ordem do dataclass. É o que monta o
    `COPY` no loader — assim o Python nunca fica fora de ordem com o SQL."""
    return tuple(f.name for f in fields(classe))


@dataclass
class PayloadAchatado:
    findings: list[LinhaFinding] = field(default_factory=list)
    plugins: list[LinhaPlugin] = field(default_factory=list)
    recasts: list[LinhaRecast] = field(default_factory=list)
    num_updates: int = 0
    num_deletes: int = 0
    version: int | None = None

    @property
    def registros_lidos(self) -> int:
        return self.num_updates + self.num_deletes


# ===========================================================================
# Achatamento por tipo
# ===========================================================================
def achatar_payload(
    tipo: TipoPayload, doc: dict[str, Any], entrada: EntradaPayload
) -> PayloadAchatado:
    """Ponto único de entrada. Despacha para a função do `payload_type`."""
    updates = doc.get("updates") or []
    deletes = doc.get("deletes") or []
    if not isinstance(updates, list) or not isinstance(deletes, list):
        raise ErroParse(f"{entrada.path}: 'updates'/'deletes' devem ser listas")

    resultado = PayloadAchatado(
        num_updates=len(updates),
        num_deletes=len(deletes),
        version=_inteiro_ou_none(doc.get("version")),
    )
    fallback = entrada.relogio_fallback

    if tipo.nome == "FINDING_ENRICHED_ATTRIBUTES":
        _achatar_enriched(resultado, tipo, updates, deletes, fallback)
        return resultado

    achatar_um = _achatar_vm if tipo.produto == PRODUTO_VM else _achatar_was
    for seq, registro in enumerate(updates):
        if not isinstance(registro, dict):
            raise ErroParse(f"{entrada.path}: updates[{seq}] não é um objeto")
        linha = achatar_um(seq, registro, fallback)
        if linha.indexed is None:
            # Sem relógio de versão não há como ordenar o registro contra o que
            # já está no banco, e a guarda da seção 6.4 deixaria de proteger.
            # Melhor o arquivo falhar alto e ir para a fila de tentativas do
            # que entrar com a ordenação quebrada.
            raise ErroParse(
                f"{entrada.path}: finding {linha.finding_id} sem relógio de versão "
                "(indexed/indexed_at nulos e manifest sem last_record_timestamp)"
            )
        resultado.findings.append(linha)
        plugin = _achatar_plugin(seq, registro.get("plugin"), linha.indexed)
        if plugin is not None:
            resultado.plugins.append(plugin)

    deslocamento = len(updates)
    for posicao, registro in enumerate(deletes):
        if not isinstance(registro, dict):
            raise ErroParse(f"{entrada.path}: deletes[{posicao}] não é um objeto")
        resultado.findings.append(
            _achatar_delete(deslocamento + posicao, registro, tipo, fallback)
        )

    return resultado


def _achatar_vm(
    seq: int, registro: dict[str, Any], fallback: dt.datetime | None
) -> LinhaFinding:
    """FINDING (VM) → stg_finding (mapa da seção 7.1)."""
    asset = registro.get("asset") or {}
    porta = registro.get("port") or {}
    scan = registro.get("scan") or {}
    plugin = registro.get("plugin") or {}

    finding_id = texto(registro.get("finding_id"))
    if not finding_id:
        raise ErroParse("finding VM sem finding_id")

    plugin_id = inteiro(plugin.get("id"))
    hostname = minusculo(asset.get("hostname"))
    port_number = inteiro(porta.get("port"))
    port_protocol = maiusculo(porta.get("protocol"))

    return LinhaFinding(
        seq=seq,
        finding_id=finding_id,
        product=PRODUTO_VM,
        is_delete=False,
        state=maiusculo(registro.get("state")),
        severity=maiusculo(registro.get("severity")),
        severity_id=inteiro(registro.get("severity_id")),
        severity_default_id=inteiro(registro.get("severity_default_id")),
        severity_modification_type=maiusculo(registro.get("severity_modification_type")),
        recast_reason=texto(registro.get("recast_reason")),
        recast_rule_uuid=texto(registro.get("recast_rule_uuid")),
        plugin_id=plugin_id,
        plugin_name=texto(plugin.get("name")),
        asset_uuid=texto(asset.get("uuid")),
        asset_fqdn=minusculo(asset.get("fqdn")),
        asset_hostname=hostname,
        asset_ipv4=texto(asset.get("ipv4")),
        asset_ipv6=texto(asset.get("ipv6")),
        asset_mac_address=texto(asset.get("mac_address")),
        asset_operating_system=lista_texto(asset.get("operating_system")),
        asset_device_type=texto(asset.get("device_type")),
        asset_agent_uuid=texto(asset.get("agent_uuid")),
        asset_network_id=texto(asset.get("network_id")),
        asset_tracked=booleano(asset.get("tracked")),
        port_number=port_number,
        port_protocol=port_protocol,
        port_service=texto(porta.get("service")),
        # `output` do VM contém JSON serializado como string: guardar como
        # texto, NÃO parsear (seção 7.5).
        output=texto(registro.get("output")),
        first_found=timestamp(registro.get("first_found")),
        last_found=timestamp(registro.get("last_found")),
        last_fixed=timestamp(registro.get("last_fixed")),
        resurfaced_date=timestamp(registro.get("resurfaced_date")),
        time_taken_to_fix=inteiro(registro.get("time_taken_to_fix")),
        indexed=timestamp(registro.get("indexed")) or fallback,
        scan_uuid=texto(scan.get("uuid")),
        scan_schedule_uuid=texto(scan.get("schedule_uuid")),
        scan_started_at=timestamp(scan.get("started_at")),
        scan_target=texto(scan.get("target")),
        source=maiusculo(registro.get("source")),
        natural_key=natural_key_vm(hostname, plugin_id, port_number, port_protocol),
        raw=registro,
    )


def _achatar_was(
    seq: int, registro: dict[str, Any], fallback: dt.datetime | None
) -> LinhaFinding:
    """WAS_FINDING → stg_finding (mapa da seção 7.2)."""
    asset = registro.get("asset") or {}
    scan = registro.get("scan") or {}
    plugin = registro.get("plugin") or {}

    finding_id = texto(registro.get("finding_id"))
    if not finding_id:
        raise ErroParse("finding WAS sem finding_id")

    plugin_id = inteiro(plugin.get("id"))
    fqdn = minusculo(asset.get("fqdn"))
    url = texto(registro.get("url"))
    input_name = texto(registro.get("input_name"))

    return LinhaFinding(
        seq=seq,
        finding_id=finding_id,
        product=PRODUTO_WAS,
        is_delete=False,
        state=maiusculo(registro.get("state")),
        severity=maiusculo(registro.get("severity")),
        severity_id=inteiro(registro.get("severity_id")),
        severity_default_id=inteiro(registro.get("severity_default_id")),
        severity_modification_type=maiusculo(registro.get("severity_modification_type")),
        recast_reason=texto(registro.get("recast_reason")),
        recast_rule_uuid=texto(registro.get("recast_rule_uuid")),
        plugin_id=plugin_id,
        plugin_name=texto(plugin.get("name")),
        asset_uuid=texto(asset.get("uuid")),
        asset_fqdn=fqdn,
        url=url,
        input_type=texto(registro.get("input_type")),
        input_name=input_name,
        http_method=maiusculo(registro.get("http_method")),
        output=texto(registro.get("output")),
        proof=texto(registro.get("proof")),
        payload=texto(registro.get("payload")),
        first_found=timestamp(registro.get("first_found")),
        last_found=timestamp(registro.get("last_found")),
        last_fixed=timestamp(registro.get("last_fixed")),
        last_observed=timestamp(registro.get("last_observed")),
        # WAS chama o relógio de `indexed_at`; o banco tem uma coluna só.
        indexed=timestamp(registro.get("indexed_at")) or fallback,
        scan_uuid=texto(scan.get("uuid")),
        scan_schedule_uuid=texto(scan.get("schedule_uuid")),
        scan_started_at=timestamp(scan.get("started_at")),
        scan_completed_at=timestamp(scan.get("completed_at")),
        scan_target=texto(scan.get("target")),
        natural_key=natural_key_was(fqdn, plugin_id, url, input_name),
        raw=registro,
    )


def _achatar_delete(
    seq: int,
    registro: dict[str, Any],
    tipo: TipoPayload,
    fallback: dt.datetime | None,
) -> LinhaFinding:
    """`deletes[]` → linha marcada com `is_delete`.

    O registro traz só o ID e `deleted_at` — daí a linha vir quase toda nula.
    O nome do campo de ID varia por tipo (`_id` no finding, `id` no asset),
    então vem da whitelist, nunca adivinhado (seção 4.4).

    Isto NÃO é remediação: é o finding sumindo do Tenable (asset removido,
    purge, licença). O `state` não vira FIXED (seção 6.7)."""
    finding_id = texto(registro.get(tipo.campo_id_delete)) or texto(registro.get("id"))
    if not finding_id:
        raise ErroParse(
            f"delete de {tipo.nome} sem '{tipo.campo_id_delete}': {sorted(registro)}"
        )
    return LinhaFinding(
        seq=seq,
        finding_id=finding_id,
        product=tipo.produto or PRODUTO_VM,
        is_delete=True,
        deleted_at=timestamp(registro.get("deleted_at")) or fallback,
        indexed=fallback,
        raw=registro,
    )


def _achatar_plugin(
    seq: int, plugin: Any, indexed: dt.datetime | None
) -> LinhaPlugin | None:
    """Objeto `plugin` → tabela `plugin` (mapa da seção 7.4).

    O plugin é 41% do tamanho de um finding VM e 66% de um WAS, e é idêntico
    para todo finding do mesmo `plugin_id`. Guardá-lo por finding significaria
    gravar a mesma descrição dezenas de milhares de vezes."""
    if not isinstance(plugin, dict):
        return None
    plugin_id = inteiro(plugin.get("id"))
    if plugin_id is None:
        return None

    vpr = plugin.get("vpr")
    # `vpr_v2` foi deprecado em 01/07/2026 e sai em 01/10/2026: o projeto NÃO
    # DEVE depender dele (seção 7.4).
    vpr_score = numero(vpr.get("score")) if isinstance(vpr, dict) else None

    return LinhaPlugin(
        seq=seq,
        plugin_id=plugin_id,
        indexed=indexed,
        name=texto(plugin.get("name")),
        family=texto(plugin.get("family")),
        risk_factor=maiusculo(plugin.get("risk_factor")),
        type=texto(plugin.get("type")),
        synopsis=texto(plugin.get("synopsis")),
        description=texto(plugin.get("description")),
        solution=texto(plugin.get("solution")),
        see_also=lista_texto(plugin.get("see_also")),
        cve=lista_texto(plugin.get("cve")),
        cwe=lista_texto(plugin.get("cwe")),
        cpe=lista_texto(plugin.get("cpe")),
        # VM chama de `cvss_base_score`, WAS de `cvss2_base_score`.
        cvss2_base_score=numero(_primeiro(plugin, "cvss_base_score", "cvss2_base_score")),
        cvss3_base_score=numero(plugin.get("cvss3_base_score")),
        cvss4_base_score=numero(plugin.get("cvss4_base_score")),
        epss_score=numero(plugin.get("epss_score")),
        vpr_score=vpr_score,
        exploit_available=booleano(plugin.get("exploit_available")),
        exploited_by_malware=booleano(plugin.get("exploited_by_malware")),
        in_the_news=booleano(plugin.get("in_the_news")),
        has_patch=booleano(plugin.get("has_patch")),
        unsupported_by_vendor=booleano(plugin.get("unsupported_by_vendor")),
        publication_date=timestamp(plugin.get("publication_date")),
        patch_publication_date=timestamp(plugin.get("patch_publication_date")),
        modification_date=timestamp(plugin.get("modification_date")),
        raw=plugin,
    )


def _achatar_enriched(
    resultado: PayloadAchatado,
    tipo: TipoPayload,
    updates: list[Any],
    deletes: list[Any],
    fallback: dt.datetime | None,
) -> None:
    """FINDING_ENRICHED_ATTRIBUTES → finding_recast (mapa da seção 7.3).

    Complemento, não bloqueador de ordem: a fonte primária do evento de recast
    é o próprio finding, que já traz `severity_modification_type` (seção 8.6).
    Aqui vem o detalhe da regra."""
    for seq, registro in enumerate(updates):
        if not isinstance(registro, dict):
            raise ErroParse(f"enriched: updates[{seq}] não é um objeto")
        propriedades = registro.get("recast_properties") or {}
        finding_id = texto(propriedades.get("finding_id"))
        if not finding_id:
            log.debug("enriched sem finding_id em updates[%d]; ignorado", seq)
            continue
        anotacao = propriedades.get("recast_annotation") or {}
        resultado.recasts.append(
            LinhaRecast(
                seq=seq,
                finding_id=finding_id,
                is_delete=False,
                source=texto(propriedades.get("source")),
                rule_id=texto(anotacao.get("rule_id")),
                rule_comment=texto(anotacao.get("rule_comment")),
                modification=maiusculo(anotacao.get("modification")),
                modification_target=maiusculo(anotacao.get("modification_target")),
                recasted_severity=maiusculo(anotacao.get("recasted_severity")),
                changed_result=texto(anotacao.get("changed_result")),
                rule_created_at=timestamp(anotacao.get("created_at")),
                rule_updated_at=timestamp(anotacao.get("updated_at")),
                raw=registro,
            )
        )

    deslocamento = len(updates)
    for posicao, registro in enumerate(deletes):
        if not isinstance(registro, dict):
            raise ErroParse(f"enriched: deletes[{posicao}] não é um objeto")
        propriedades = registro.get("recast_properties") or {}
        finding_id = (
            texto(registro.get(tipo.campo_id_delete))
            or texto(registro.get("id"))
            or texto(propriedades.get("finding_id"))
        )
        if not finding_id:
            log.warning("enriched: delete sem id (%s); ignorado", sorted(registro))
            continue
        resultado.recasts.append(
            LinhaRecast(
                seq=deslocamento + posicao,
                finding_id=finding_id,
                is_delete=True,
                deleted_at=timestamp(registro.get("deleted_at")) or fallback,
                raw=registro,
            )
        )


def _inteiro_ou_none(valor: Any) -> int | None:
    return inteiro(valor)
