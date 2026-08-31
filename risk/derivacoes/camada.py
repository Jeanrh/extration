"""Camada tecnológica e família de um plugin.

Porte de `src/scoring/layer_resolver.py` do extraction, com uma diferença de
forma: as keywords do Vault entram como argumento em vez de serem lidas de um
cache global. O motor recalcula tudo a cada execução, então uma variável de
módulo que sobrevive entre chamadas é justamente o que não se quer.

A ordem de resolução espelha o DAX (`familia_vulnerabilidade`): classifica pela
VULNERABILIDADE, não pelo tipo do ativo. Um patch de kernel num servidor de
banco de dados é "Sistema Operacional", não "Banco de Dados".
"""

from __future__ import annotations

import json
from typing import Any

# Prioridade de desempate quando o nome do plugin casa com mais de uma camada.
PRIORIDADE = (
    "aplicacao",
    "banco de dados",
    "appliance",
    "middleware",
    "sistema operacional",
    "hardening",
)

# Substring da `plugin.family` → camada. Cobre também os nomes de família que o
# Tenable usa ("Databases", "Web Servers", "Windows : *", "Red Hat Local
# Security Checks"), que é o fallback disponível no banco: `asset_category`
# vinha das tags do Tenable, e o Data Stream não publica tags no payload de
# finding.
_REGRAS_FAMILY: list[tuple[str, str]] = [
    ("banco de dados", "banco de dados"),
    ("database", "banco de dados"),
    ("databases", "banco de dados"),
    ("appliance", "appliance"),
    ("middleware", "middleware"),
    ("web server", "middleware"),
    ("web servers", "middleware"),
    ("aplicacao", "aplicacao"),
    ("application", "aplicacao"),
    ("sistema operacional", "sistema operacional"),
    ("operating system", "sistema operacional"),
    ("windows", "sistema operacional"),
    ("local security", "sistema operacional"),
    ("hardening", "hardening"),
]

# Substring no NOME DA CHAVE do segredo → camada. Ordem importa: a primeira
# correspondência vence, e "app" casaria dentro de "appliance".
_REGRAS_CHAVE: list[tuple[str, str]] = [
    ("aplicacao", "aplicacao"),
    ("app", "aplicacao"),
    ("middleware", "middleware"),
    ("banco de dados", "banco de dados"),
    ("banco_de_dados", "banco de dados"),
    ("banco", "banco de dados"),
    ("database", "banco de dados"),
    ("_db", "banco de dados"),
    ("hardening", "hardening"),
    ("so_", "sistema operacional"),
    ("_so", "sistema operacional"),
    ("sistema operacional", "sistema operacional"),
    ("sistema_operacional", "sistema operacional"),
    ("sistema", "sistema operacional"),
    ("_os", "sistema operacional"),
    ("operating", "sistema operacional"),
    ("appliance", "appliance"),
]

TAMANHO_MINIMO_KEYWORD = 2


def _keywords(valor: Any) -> list[str]:
    """Keywords de um valor do segredo: string, JSON serializado ou dict.

    Keyword com menos de dois caracteres ou contendo ':' é descartada — o match
    é por substring, e "a" casaria com quase todo nome de plugin.
    """
    bruto = ""
    if isinstance(valor, dict):
        bruto = valor.get("familia") or valor.get("keywords") or ""
    elif isinstance(valor, str):
        texto = valor.strip()
        if texto.startswith("{"):
            try:
                analisado = json.loads(texto)
                bruto = analisado.get("familia") or analisado.get("keywords") or ""
            except (json.JSONDecodeError, ValueError, AttributeError):
                bruto = texto
        else:
            bruto = texto

    return [
        kw.strip().lower()
        for kw in str(bruto).split(",")
        if kw.strip() and len(kw.strip()) >= TAMANHO_MINIMO_KEYWORD and ":" not in kw
    ]


def _camada_da_chave(chave: str) -> str | None:
    minuscula = chave.lower().strip()
    for padrao, camada in _REGRAS_CHAVE:
        if padrao in minuscula:
            return camada
    return None


def indexar_keywords(segredo: dict[str, Any]) -> dict[str, list[str]]:
    """Segredo do Vault → {camada: [keyword, ...]}."""
    indice: dict[str, list[str]] = {}
    for chave, valor in segredo.items():
        camada = _camada_da_chave(chave)
        if not camada:
            continue
        keywords = _keywords(valor)
        if keywords:
            indice.setdefault(camada, []).extend(keywords)
    return indice


def _da_family(family: str) -> str:
    minuscula = (family or "").lower().strip()
    if not minuscula:
        return ""
    for substring, camada in _REGRAS_FAMILY:
        if substring in minuscula:
            return camada
    return ""


def resolver_camada(
    family: str,
    plugin_name: str,
    indice: dict[str, list[str]],
) -> tuple[str, str, str]:
    """Devolve (camada, familia, origem).

    `familia` é a keyword que fez o match ("tomcat", "oracle") e só existe
    quando quem decidiu foi o nome do plugin. `origem` registra qual regra
    decidiu — é o que permite medir, depois de uma rodada, quanto da camada
    veio do Vault e quanto veio do fallback.

    Camada vazia não é erro: o scoring aplica o default 30, o mesmo de
    "sistema operacional", como faz o DAX.
    """
    nome = (plugin_name or "").lower().strip()
    if nome and indice:
        for camada in PRIORIDADE:
            for keyword in indice.get(camada, []):
                if keyword and keyword in nome:
                    return camada, keyword, "plugin_name"

    camada = _da_family(family)
    if camada:
        return camada, "", "family"

    return "", "", "nenhum"


# ---------------------------------------------------------------------------
# Materialização
# ---------------------------------------------------------------------------

CONSULTA_PLUGINS = "SELECT plugin_id, name, family FROM plugin"


def derivar_camadas(conn, indice: dict[str, list[str]], lote: int = 5_000) -> int:
    """Recalcula `plugin_layer` para todos os plugins. Devolve quantos entraram.

    Recarga completa por transação: a tabela tem uma linha por plugin, não por
    finding, então reescrevê-la inteira custa pouco e mantém a derivação
    idempotente — rodar duas vezes seguidas dá o mesmo resultado.
    """
    linhas: list[tuple] = []

    # Leitura e escrita na MESMA transação: o cursor server-side (que mantém a
    # memória constante) só existe dentro de um bloco transacional, e a conexão
    # do projeto é autocommit por padrão. De quebra, plugin criado no meio do
    # caminho não escapa entre o SELECT e o TRUNCATE.
    with conn.transaction():
        with conn.cursor(name="plugins_para_camada") as leitura:
            leitura.itersize = lote
            leitura.execute(CONSULTA_PLUGINS)
            for plugin in leitura:
                camada, familia, origem = resolver_camada(
                    plugin["family"] or "", plugin["name"] or "", indice
                )
                linhas.append((plugin["plugin_id"], camada, familia, origem))

        with conn.cursor() as cur:
            cur.execute("TRUNCATE plugin_layer")
            if linhas:
                cur.executemany(
                    "INSERT INTO plugin_layer (plugin_id, layer, familia, resolved_by) "
                    "VALUES (%s, %s, %s, %s)",
                    linhas,
                )

    return len(linhas)
