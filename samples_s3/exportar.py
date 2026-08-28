"""
CLI do extrator do Tenable Data Stream.

    python exportar.py                     # roda todos os relatórios
    python exportar.py gestao_vuln         # roda um
    python exportar.py intranet internet   # roda vários
    python exportar.py --list              # lista os relatórios

Config via .env (veja .env.example). Os relatórios são definidos em reports.py.
"""

from __future__ import annotations

import datetime as dt
import sys
import time

from tenable_core import (
    carregar_config,
    criar_cliente_s3,
    executar_relatorio,
    formatar_duracao,
)
from reports import RELATORIOS


def _listar() -> None:
    print("Relatórios disponíveis:\n")
    for nome, rel in RELATORIOS.items():
        print(f"  {nome:13} {rel.descricao()}")


def main() -> None:
    argv = sys.argv[1:]
    if "--list" in argv or "-l" in argv or "--help" in argv or "-h" in argv:
        _listar()
        return

    alvos = [a for a in argv if not a.startswith("-")] or list(RELATORIOS)
    desconhecidos = [a for a in alvos if a not in RELATORIOS]
    if desconhecidos:
        print(f"Relatório(s) desconhecido(s): {', '.join(desconhecidos)}")
        print(f"Disponíveis: {', '.join(RELATORIOS)}")
        sys.exit(1)

    inicio = time.time()
    print(f"Início: {dt.datetime.now():%Y-%m-%d %H:%M:%S}")

    config = carregar_config()
    s3_client = criar_cliente_s3(
        config, max_pool_connections=max(config["s3_max_workers"], 10)
    )
    cache_enriched: dict = {}

    for nome in alvos:
        rel = RELATORIOS[nome]
        print(f"\n=== {nome} ===")
        stats = executar_relatorio(rel, config, s3_client, cache_enriched)
        print(
            f"[{nome}] {stats['linhas_csv']} linha(s) -> {stats['saida']} | "
            f"updates={stats['updates']} descartados_filtro={stats['descartados_filtro']} "
            f"falhas_arquivo={stats['falhas_arquivo']} retries_throttle={stats['retries_throttle']}"
        )

    print(
        f"\nFim: {dt.datetime.now():%Y-%m-%d %H:%M:%S} | "
        f"duração {formatar_duracao(time.time() - inicio)}"
    )


if __name__ == "__main__":
    main()
