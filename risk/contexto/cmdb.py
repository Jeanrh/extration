"""Sync do CMDB (JSM/Assets) para as tabelas de contexto.

Busca tudo primeiro, grava depois. A ordem importa: se o JSM cair no meio, as
tabelas nunca chegaram a ser tocadas e o snapshot anterior segue de pé — o
motor calcula com contexto de um ciclo atrás em vez de com contexto vazio. E
como a escrita não espera HTTP, a transação de escrita dura milissegundos, não
o tempo de paginar milhares de objetos.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from .procedencia import registrar_sync
from .siglas import indices_de_sigla, resolver_sigla

log = logging.getLogger(__name__)

FONTE = "CMDB"


@dataclass(frozen=True)
class ResultadoSync:
    siglas: int
    servidores: int
    urls: int
    times: int
    sincronizado_em: dt.datetime

    @property
    def total(self) -> int:
        return self.siglas + self.servidores + self.urls + self.times


def _indice_de_cockpit(times: list[dict]) -> dict[str, tuple[str, str]]:
    """`key` do cockpit → (unidade de negócio, tribo).

    Indexado pela chave do Jira, nunca pelo nome: o `name` do cockpit é o
    rótulo da **tribo** ("GARAGEM") e o `team` da sigla é o da **equipe**
    ("Plataforma de Deploy"). Casar esses dois por texto acerta por acidente.

    Unidade de negócio é a aliança — não a VP."""
    return {
        chave: (t.get("alianca", ""), t.get("tribo", ""))
        for t in times
        if (chave := (t.get("key") or "").strip())
    }


def _linhas_de_sigla(siglas: list[dict], cockpits: dict[str, tuple[str, str]]) -> list[tuple]:
    """A sigla já sai daqui com unidade de negócio e tribo resolvidas.

    Resolver na carga (dezenas de milhares de siglas) em vez de na leitura
    (centenas de milhares de findings) tira um JOIN do caminho quente. O
    `teamid` morre aqui: é chave de busca, não informação de negócio."""
    return [
        (
            s["acronym"].upper(),
            s.get("name", ""),
            s.get("status", ""),
            s.get("PCI", ""),
            s.get("BIA", ""),
            s.get("criticality", ""),
            *cockpits.get((s.get("teamid") or "").strip(), ("", "")),
            s.get("infrastructure", ""),
        )
        for s in siglas
        if s.get("acronym")
    ]


def _linhas_de_servidor(servidores: list[dict], codigos, nomes) -> list[tuple]:
    vistos: dict[str, tuple] = {}
    for s in servidores:
        nome = (s.get("name") or "").strip()
        if not nome:
            continue
        bruto = s.get("acronym", "")
        # dict por hostname: mesmo comportamento do índice do extraction, onde
        # o último registro do CMDB vence.
        vistos[nome.upper()] = (
            nome.upper(),
            (s.get("ipv4") or "").strip(),
            resolver_sigla(bruto, codigos, nomes),
            bruto,
            s.get("status", ""),
            s.get("infrastructure", ""),
            s.get("environment", ""),
        )
    return list(vistos.values())


def _linhas_de_url(urls: list[dict], codigos, nomes) -> list[tuple]:
    vistos: dict[str, tuple] = {}
    for u in urls:
        nome = (u.get("name") or "").strip()
        if not nome:
            continue
        bruto = u.get("acronym", "")
        vistos[nome.upper()] = (
            nome.upper(),
            resolver_sigla(bruto, codigos, nomes),
            bruto,
            u.get("status", ""),
            u.get("pci", ""),
            u.get("alliance", ""),
        )
    return list(vistos.values())


def _recarregar(cur, tabela: str, colunas: tuple[str, ...], linhas: list[tuple]) -> None:
    cur.execute(f"TRUNCATE {tabela}")
    if not linhas:
        return
    marcadores = ", ".join(["%s"] * len(colunas))
    cur.executemany(
        f"INSERT INTO {tabela} ({', '.join(colunas)}) VALUES ({marcadores})",
        linhas,
    )


def sincronizar_cmdb(extrator, conn, max_age_hours: float | None = None) -> ResultadoSync:
    """Recarrega as três tabelas do CMDB a partir do extrator do JSM.

    Os cockpits não viram tabela: eles são consumidos aqui mesmo, para
    carimbar unidade de negócio e tribo em cada sigla.

    `extrator` só precisa expor `extract_acronyms`, `extract_servers`,
    `extract_urls` e `extract_cockpits` — é o contrato do `CMDBExtractor`, e é
    o que permite testar o sync sem tocar a rede.
    """
    # --- busca: fora da transação, para que uma falha não encoste no banco ---
    siglas = extrator.extract_acronyms(max_age_hours=max_age_hours)
    servidores = extrator.extract_servers(max_age_hours=max_age_hours)
    urls = extrator.extract_urls(max_age_hours=max_age_hours)
    times = extrator.extract_cockpits(max_age_hours=max_age_hours)

    codigos, nomes = indices_de_sigla(siglas)
    linhas_sigla = _linhas_de_sigla(siglas, _indice_de_cockpit(times))
    linhas_servidor = _linhas_de_servidor(servidores, codigos, nomes)
    linhas_url = _linhas_de_url(urls, codigos, nomes)

    resultado = ResultadoSync(
        siglas=len(linhas_sigla),
        servidores=len(linhas_servidor),
        urls=len(linhas_url),
        times=len(times),
        sincronizado_em=dt.datetime.now(dt.timezone.utc),
    )

    # --- escrita: tudo ou nada ---
    with conn.transaction(), conn.cursor() as cur:
        _recarregar(cur, "cmdb_acronym", (
            "sigla", "nome", "status", "pci", "bia", "criticality",
            "unidade_negocio", "tribo", "infrastructure",
        ), linhas_sigla)
        _recarregar(cur, "cmdb_server", (
            "hostname", "ipv4", "sigla", "acronym_raw",
            "status", "infrastructure", "environment",
        ), linhas_servidor)
        _recarregar(cur, "cmdb_url", (
            "url", "sigla", "acronym_raw", "status", "pci", "alliance",
        ), linhas_url)

        registrar_sync(
            cur, FONTE, "OK", resultado.total,
            sincronizado_em=resultado.sincronizado_em,
        )

    log.info(
        "cmdb | sync | siglas=%s servidores=%s urls=%s times=%s",
        resultado.siglas, resultado.servidores, resultado.urls, resultado.times,
    )
    return resultado
