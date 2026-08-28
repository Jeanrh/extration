"""Manutenção das partições de `finding_event` (seção 14).

`finding_event` nasce particionada por mês, e isso é requisito da v1, não
melhoria futura: converter uma tabela com milhões de linhas depois é cirurgia.
Com partição, o expurgo do mês 25 é `DROP TABLE` — instantâneo, sem lock e sem
bloat — em vez de um `DELETE` que trava a tabela.
"""

from __future__ import annotations

import datetime as dt
import logging
import re

log = logging.getLogger(__name__)

TABELA = "finding_event"
PARTICAO_DEFAULT = f"{TABELA}_default"
PADRAO_NOME = re.compile(rf"^{TABELA}_(\d{{4}})_(\d{{2}})$")


def _primeiro_dia(referencia: dt.date, meses: int = 0) -> dt.date:
    total = referencia.year * 12 + (referencia.month - 1) + meses
    return dt.date(total // 12, total % 12 + 1, 1)


def nome_particao(inicio: dt.date) -> str:
    return f"{TABELA}_{inicio.year:04d}_{inicio.month:02d}"


def garantir_particoes(conn, meses_adiante: int = 1, hoje: dt.date | None = None) -> list[str]:
    """Cria as partições do mês corrente e dos próximos `meses_adiante`.

    Roda ao fim de todo ciclo: se o job rodar em 31/08 e a partição de setembro
    não existir, o primeiro evento de 01/09 cairia na DEFAULT."""
    from psycopg import errors as pg_errors  # import tardio: driver opcional

    hoje = hoje or dt.date.today()
    existentes = set(listar_particoes(conn))
    criadas: list[str] = []

    for deslocamento in range(0, meses_adiante + 1):
        inicio = _primeiro_dia(hoje, deslocamento)
        nome = nome_particao(inicio)
        if nome in existentes:
            continue
        fim = _primeiro_dia(inicio, 1)
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {nome} PARTITION OF {TABELA} "
                    "FOR VALUES FROM (%s) TO (%s)",
                    (inicio, fim),
                )
            log.info("partição criada: %s [%s, %s)", nome, inicio, fim)
            criadas.append(nome)
        except pg_errors.InvalidObjectDefinition as erro:
            # A DEFAULT já tem linhas dessa faixa. O Postgres recusa a criação
            # em vez de mover as linhas sozinho — é preciso mover à mão.
            log.error(
                "não foi possível criar %s: %s. Mova as linhas de %s para a nova "
                "faixa antes de tentar de novo.", nome, erro, PARTICAO_DEFAULT,
            )
    return criadas


def expurgar_particoes(
    conn, retention_months: int, hoje: dt.date | None = None
) -> list[str]:
    """Dropa partições inteiramente anteriores a (hoje − retention_months).

    Só toca partições mensais nomeadas pelo padrão; a DEFAULT nunca é dropada,
    porque ela guarda os OPENED retroativos da regra 2 da seção 8.3."""
    hoje = hoje or dt.date.today()
    corte = _primeiro_dia(hoje, -retention_months)
    dropadas: list[str] = []

    for nome in listar_particoes(conn):
        casamento = PADRAO_NOME.match(nome)
        if casamento is None:
            continue
        inicio = dt.date(int(casamento.group(1)), int(casamento.group(2)), 1)
        if _primeiro_dia(inicio, 1) <= corte:
            log.warning("expurgando partição vencida: %s (retenção %d meses)",
                        nome, retention_months)
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {nome}")
            dropadas.append(nome)
    return dropadas


def listar_particoes(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname AS nome "
            "FROM pg_inherits i "
            "JOIN pg_class c   ON c.oid = i.inhrelid "
            "JOIN pg_class p   ON p.oid = i.inhparent "
            "WHERE p.relname = %s ORDER BY 1",
            (TABELA,),
        )
        return [linha["nome"] for linha in cur.fetchall()]


def contar_default(conn) -> int:
    """Linhas na partição DEFAULT.

    Algumas são esperadas (OPENED retroativo de anos atrás). Crescimento
    contínuo indica evento com data fora de faixa — investigar (seção 14.3)."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS total FROM {PARTICAO_DEFAULT}")
        return int(cur.fetchone()["total"])


def expurgar_ingest_file(conn, dias: int) -> int:
    """Retenção de `ingest_file` (seção 14.1): 90 dias, por DELETE mesmo — o
    volume é baixo e não justifica particionar.

    Arquivos em quarentena ficam: são o registro do buraco conhecido."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "DELETE FROM ingest_file "
            "WHERE processed_at < now() - make_interval(days => %s) "
            "  AND status <> 'QUARANTINED'",
            (dias,),
        )
        return max(cur.rowcount, 0)
