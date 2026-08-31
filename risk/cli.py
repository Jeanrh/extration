"""CLI do motor: `python -m risk.cli`.

Três comandos, e a separação entre eles é deliberada. Mexer num peso e querer
ver o efeito é a operação mais frequente da vida deste sistema — `run` sozinho
recalcula tudo em cima do contexto já sincronizado, sem esperar por JSM, Vault
ou pela API clássica.
"""

from __future__ import annotations

import argparse
import logging
import sys

from ingestion.db import conectar
from ingestion.erros import ErroIngestao

from .config import ConfigMotor, carregar_config
from .derivacoes.camada import derivar_camadas
from .executor import recalcular

log = logging.getLogger("risk")


def configurar_logging(nivel: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, nivel.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
        force=True,
    )


def cmd_sync_context(config: ConfigMotor, args: argparse.Namespace) -> int:
    """Recarrega CMDB, arquitetura e threat intel.

    Cada fonte é independente: uma indisponível não impede as outras, e o
    snapshot anterior dela sobrevive. O motor prefere contexto de um ciclo
    atrás a contexto vazio, que produziria score plausível e errado."""
    from .contexto.arquitetura import carregar_arquitetura

    with conectar(config.pg_dsn) as conn:
        if config.tem_cmdb:
            from .contexto.jsm import extrator_de_cmdb
            from .contexto.cmdb import sincronizar_cmdb

            resultado = sincronizar_cmdb(
                extrator_de_cmdb(config), conn, max_age_hours=config.cmdb_cache_horas
            )
            log.info(
                "cmdb | siglas=%s servidores=%s urls=%s times=%s",
                resultado.siglas, resultado.servidores, resultado.urls, resultado.times,
            )
        else:
            log.warning("cmdb | credenciais do JSM ausentes — snapshot anterior mantido")

        siglas = carregar_arquitetura(config.csv_arquitetura, conn)
        log.info("arquitetura | siglas=%s", siglas)

        if config.tem_intel:
            from .contexto.intel import sincronizar_threat_intel
            from .contexto.tenable import extrator_de_intel

            log.info("intel | findings=%s", sincronizar_threat_intel(extrator_de_intel(config), conn))
        else:
            log.warning("intel | ACCESS_KEY/SECRET_KEY ausentes — snapshot anterior mantido")

    return 0


def cmd_run(config: ConfigMotor, args: argparse.Namespace) -> int:
    """Deriva a camada e recalcula o risco de todo finding não deletado."""
    from .contexto.vault import keywords_de_camada

    indice = keywords_de_camada(config)
    with conectar(config.pg_dsn) as conn:
        plugins = derivar_camadas(conn, indice)
        log.info("camada | plugins=%s", plugins)

        resultado = recalcular(conn, engine_version=config.versao, lote=config.lote)

    print(f"  Calculados: {resultado.calculados:,}")
    print(f"  Gravados:   {resultado.gravados:,}  (só o que mudou)")
    print(f"  Eventos:    {resultado.eventos:,}  RISK_CHANGED")
    return 0


CONSULTA_STATUS = """
SELECT (SELECT count(*) FROM finding_current WHERE deleted_at IS NULL) AS findings,
       (SELECT count(*) FROM finding_risk)                             AS com_risco,
       (SELECT max(computed_at) FROM finding_risk)                     AS ultimo_calculo,
       (SELECT count(*) FROM plugin_layer)                             AS camadas
"""


def cmd_status(config: ConfigMotor, args: argparse.Namespace) -> int:
    with conectar(config.pg_dsn) as conn, conn.cursor() as cur:
        cur.execute(CONSULTA_STATUS)
        resumo = cur.fetchone()

        print(f"  Versão do motor:   {config.versao}")
        print(f"  Findings ativos:   {resumo['findings']:,}")
        print(f"  Com risco:         {resumo['com_risco']:,}")
        print(f"  Último cálculo:    {resumo['ultimo_calculo'] or '(nunca)'}")
        print(f"  Camadas derivadas: {resumo['camadas']:,}")

        cur.execute(
            "SELECT source, status, synced_at, row_count FROM context_sync ORDER BY source"
        )
        linhas = cur.fetchall()
        print("  Contexto:")
        if not linhas:
            print("    (nenhuma fonte sincronizada)")
        for linha in linhas:
            print(
                f"    {linha['source']:<14} {linha['status']:<7} "
                f"{linha['synced_at']}  linhas={linha['row_count']}"
            )

        cur.execute(
            "SELECT priority_name, count(*) AS total FROM finding_risk "
            " WHERE priority_name IS NOT NULL GROUP BY priority_name ORDER BY min(priority_id)"
        )
        distribuicao = cur.fetchall()
        if distribuicao:
            print("  Prioridade:")
            for linha in distribuicao:
                print(f"    {linha['priority_name']:<12} {linha['total']:,}")

    return 0


COMANDOS = {
    "sync-context": cmd_sync_context,
    "run": cmd_run,
    "status": cmd_status,
}


def montar_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m risk.cli",
        description="Motor de risco: recalcula a prioridade de todo finding, sem filtro de tempo.",
    )
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("sync-context", help="recarrega CMDB, arquitetura e threat intel")
    sub.add_parser("run", help="deriva camadas e recalcula o risco de tudo")
    sub.add_parser("status", help="saúde do motor e distribuição de prioridade")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = montar_parser().parse_args(argv)
    try:
        config = carregar_config()
    except ErroIngestao as erro:
        configurar_logging()
        log.error("%s", erro)
        return 2

    configurar_logging(config.log_level)
    try:
        return COMANDOS[args.comando](config, args)
    except ErroIngestao as erro:
        log.error("%s", erro)
        return 1
    except KeyboardInterrupt:
        log.warning("interrompido")
        return 130


if __name__ == "__main__":
    sys.exit(main())
