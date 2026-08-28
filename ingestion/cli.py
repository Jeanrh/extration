"""CLI do pipeline de ingestão.

    python -m ingestion.cli init-db
    python -m ingestion.cli run [--seed] [--limit N] [--mode SEED|INCREMENTAL]
    python -m ingestion.cli set-mode INCREMENTAL --cutoff 2026-09-05 --notes "..."
    python -m ingestion.cli reprocess --path <key-do-payload>
    python -m ingestion.cli status
    python -m ingestion.cli quarantine

Ordem de execução do `run` (seção 3.3): ingestão → motor de risco → manutenção.
O motor de risco é externo a este projeto e entra como etapa 2 do mesmo job;
por isso o `run` deixa o ponto de encaixe explícito em vez de calcular risco —
a ingestão NÃO DEVE ter cálculo de severidade, score ou quadrante (seção 3.2).
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time

from . import metrics, partitions, reconcile
from .config import MODOS, Config, carregar_config, configurar_logging, resumo_config
from .db import aplicar_migracoes, conectar, travar_pipeline
from .erros import ErroIngestao
from .loader import Ingestor
from .s3 import ClienteS3

log = logging.getLogger("ingestion")


# ===========================================================================
# Comandos
# ===========================================================================
def cmd_init_db(config: Config, args: argparse.Namespace) -> int:
    with conectar(config.pg_dsn) as conn:
        aplicadas = aplicar_migracoes(conn)
    if aplicadas:
        print("Migrações aplicadas: " + ", ".join(aplicadas))
    else:
        print("Schema já está atualizado.")
    return 0


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    inicio_job = time.monotonic()
    modo = "SEED" if args.seed else args.mode
    with conectar(config.pg_dsn) as conn:
        with travar_pipeline(conn) as obtido:
            if not obtido:
                # Rede de segurança do `concurrencyPolicy: Forbid` (seção 11):
                # outra execução está rodando. Sair com 0 — não é erro.
                log.warning("outra execução detém o lock do pipeline; encerrando")
                return 0

            log.info("config: %s", resumo_config(config))
            cliente = ClienteS3(config)
            ingestor = Ingestor(config, cliente, conn)

            # --- etapa 1: ingestão fiel -------------------------------------
            resultado = ingestor.executar(limite=args.limit, modo=modo)

            # --- etapa 2: motor de risco (fora do escopo deste projeto) -----
            log.info(
                "motor de risco: etapa não instalada neste job; finding_risk "
                "permanece como está e as views caem no severity nativo"
            )

            # --- etapa 3: manutenção ---------------------------------------
            criadas = partitions.garantir_particoes(conn)
            dropadas = partitions.expurgar_particoes(conn, config.retention_months)
            removidas = partitions.expurgar_ingest_file(
                conn, config.retencao_ingest_file_dias
            )
            na_default = partitions.contar_default(conn)
            log.info(
                "manutenção: %d partição(ões) criada(s), %d dropada(s), "
                "%d linha(s) de ingest_file expurgada(s), %d linha(s) na partição DEFAULT",
                len(criadas), len(dropadas), removidas, na_default,
            )

            estado = metrics.capturar_estado(conn)
            metricas_ciclo = metrics.coletar(resultado, estado, 0.0)
            duracao_job = time.monotonic() - inicio_job
            resultado.duracao_segundos = duracao_job
            metricas_ciclo = [
                metrics.Metrica(m.nome, duracao_job, m.unidade)
                if m.nome == "JobDurationSeconds"
                else m
                for m in metricas_ciclo
            ]
            metrics.Publicador(config).publicar(metricas_ciclo)

    _resumo_ciclo(config, resultado, estado.quarentena, estado.findings_open)
    return 1 if resultado.payloads_falhos or resultado.erros_manifest else 0


def _resumo_ciclo(config: Config, resultado, quarentena: int, abertos: int) -> None:
    horas = resultado.horas_desde_ultimo_manifest
    print(
        f"\nmodo={resultado.modo} "
        f"manifests={resultado.manifests_lidos} "
        f"payloads_ok={resultado.payloads_ok} "
        f"pulados={resultado.payloads_pulados} "
        f"falhas={resultado.payloads_falhos} "
        f"quarentena_novos={resultado.payloads_quarentena}\n"
        f"registros={resultado.registros} eventos={resultado.eventos} "
        f"abertos={abertos} quarentena_total={quarentena} "
        f"duração={resultado.duracao_segundos:.1f}s"
    )
    if horas is None:
        log.error("ALARME: nenhum manifest encontrado no bucket")
    elif horas > config.horas_sem_manifest_alerta:
        log.error(
            "ALARME: manifest mais recente tem %.1f h (limite %d h) — "
            "o stream pode estar parado (seção 12.5)",
            horas, config.horas_sem_manifest_alerta,
        )


def cmd_set_mode(config: Config, args: argparse.Namespace) -> int:
    """A troca é manual e deliberada — NÃO DEVE ser automática (seção 9.5).

    Critério da seção 9.4: virar para INCREMENTAL quando a contagem de findings
    inéditos por dia ficar estável (variação < 20%) por 3 dias seguidos. Antes
    disso, o backfill ainda está drenando e cada pedaço de histórico viraria um
    "abriu hoje"."""
    corte = None
    if args.cutoff:
        corte = dt.datetime.fromisoformat(args.cutoff)
        if corte.tzinfo is None:
            corte = corte.replace(tzinfo=dt.timezone.utc)

    with conectar(config.pg_dsn) as conn:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "UPDATE pipeline_control "
                "SET mode = %s, "
                "    cutoff_at = COALESCE(%s, cutoff_at), "
                "    notes = COALESCE(%s, notes) "
                "WHERE id = 1 RETURNING mode, cutoff_at, notes",
                (args.modo, corte, args.notes),
            )
            linha = cur.fetchone()
    print(f"modo={linha['mode']} corte={linha['cutoff_at']} notas={linha['notes']}")
    if args.modo == "INCREMENTAL" and linha["cutoff_at"] is None:
        log.warning(
            "cutoff_at está nulo. Os consumidores usam esse marco para não "
            "exibir tendência anterior ao corte — grave-o com --cutoff."
        )
    return 0


def cmd_reprocess(config: Config, args: argparse.Namespace) -> int:
    with conectar(config.pg_dsn) as conn:
        with travar_pipeline(conn) as obtido:
            if not obtido:
                log.error("outra execução detém o lock do pipeline")
                return 1
            ingestor = Ingestor(config, ClienteS3(config), conn)
            resultado = ingestor.reprocessar(args.path)
    print(f"{resultado.path}: {resultado.status} "
          f"registros={resultado.registros} eventos={resultado.eventos}")
    if resultado.erro:
        print(f"erro: {resultado.erro}")
    return 0 if resultado.status == "OK" else 1


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    with conectar(config.pg_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM vw_pipeline_saude")
        saude = cur.fetchone()
        cur.execute(
            "SELECT payload_type, status, count(*) AS total "
            "FROM ingest_file GROUP BY 1, 2 ORDER BY 1, 2"
        )
        por_tipo = cur.fetchall()
        cur.execute("SELECT count(*) AS total FROM finding_event")
        eventos = cur.fetchone()["total"]

    print("--- pipeline ---")
    for chave, valor in saude.items():
        print(f"  {chave:16} {valor}")
    print(f"  {'eventos':16} {eventos}")
    print("\n--- arquivos por tipo/status ---")
    for linha in por_tipo:
        print(f"  {linha['payload_type']:30} {linha['status']:12} {linha['total']}")
    return 0


def cmd_quarantine(config: Config, args: argparse.Namespace) -> int:
    with conectar(config.pg_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT path, payload_type, attempt_count, error_message, processed_at "
            "FROM ingest_file WHERE status = 'QUARANTINED' ORDER BY processed_at DESC"
        )
        linhas = cur.fetchall()
    if not linhas:
        print("Nenhum arquivo em quarentena.")
        return 0
    print(f"{len(linhas)} arquivo(s) em quarentena:\n")
    for linha in linhas:
        print(f"  {linha['processed_at']:%Y-%m-%d %H:%M}  {linha['payload_type']:28} "
              f"tentativas={linha['attempt_count']}")
        print(f"    {linha['path']}")
        print(f"    {linha['error_message']}")
    print("\nDepois de corrigir: python -m ingestion.cli reprocess --path <path>")
    return 0


def cmd_reconcile(config: Config, args: argparse.Namespace) -> int:
    with conectar(config.pg_dsn) as conn:
        with travar_pipeline(conn) as obtido:
            if not obtido:
                log.error("outra execução detém o lock do pipeline; reconciliação abortada")
                return 1
            relatorio = reconcile.gerar_relatorio(
                conn,
                console_vm_open=args.console_vm_open,
                console_was_open=args.console_was_open,
            )
            reconcile.escrever_relatorio(relatorio, args.output)
    return 0


# ===========================================================================
# Parser
# ===========================================================================
def montar_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ingestion.cli",
        description="Ingestão do Tenable Data Stream (S3) para PostgreSQL.",
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    sub.add_parser("init-db", help="aplica as migrações de schema")

    p_run = sub.add_parser("run", help="roda um ciclo de ingestão")
    p_run.add_argument("--limit", type=int, default=None,
                       help="processa no máximo N payloads (validação inicial)")
    p_run.add_argument("--mode", choices=MODOS, default=None,
                       help="sobrescreve o modo desta execução")
    p_run.add_argument("--seed", action="store_true",
                       help="atalho para --mode SEED")

    p_modo = sub.add_parser("set-mode", help="troca o modo do pipeline")
    p_modo.add_argument("modo", choices=MODOS)
    p_modo.add_argument("--cutoff", default=None,
                        help="data T em ISO (ex.: 2026-09-05)")
    p_modo.add_argument("--notes", default=None,
                        help="por que a decisão foi tomada")

    p_re = sub.add_parser("reprocess", help="reprocessa um payload específico")
    p_re.add_argument("--path", required=True, help="key do payload no S3")

    sub.add_parser("status", help="saúde do pipeline")
    sub.add_parser("quarantine", help="lista os arquivos em quarentena")

    p_reconcile = sub.add_parser("reconcile", help="gera reconciliação semanal local")
    p_reconcile.add_argument("--output", default="-", help="arquivo JSON ou - para stdout")
    p_reconcile.add_argument("--console-vm-open", type=_contagem_nao_negativa)
    p_reconcile.add_argument("--console-was-open", type=_contagem_nao_negativa)
    return parser


def _contagem_nao_negativa(valor: str) -> int:
    numero = int(valor)
    if numero < 0:
        raise argparse.ArgumentTypeError("a contagem não pode ser negativa")
    return numero


COMANDOS = {
    "init-db": cmd_init_db,
    "run": cmd_run,
    "set-mode": cmd_set_mode,
    "reprocess": cmd_reprocess,
    "status": cmd_status,
    "quarantine": cmd_quarantine,
    "reconcile": cmd_reconcile,
}


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
