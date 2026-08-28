"""Criação verificável e retenção segura das partições de ``finding_event``."""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

TABELA = "finding_event"
PADRAO_NOME = re.compile(rf"^{TABELA}_(\d{{4}})_(\d{{2}})$")
PADRAO_BOUND = re.compile(
    r"^FOR VALUES FROM \('([^']+)'\) TO \('([^']+)'\)$"
)


@dataclass(frozen=True)
class _Relacao:
    oid: int
    schema: str
    nome: str
    parent_oid: int | None
    bound: str | None


def _primeiro_dia(referencia: dt.date, meses: int = 0) -> dt.date:
    total = referencia.year * 12 + (referencia.month - 1) + meses
    return dt.date(total // 12, total % 12 + 1, 1)


def _inicio_utc(referencia: dt.date, meses: int = 0) -> dt.datetime:
    primeiro = _primeiro_dia(referencia, meses)
    return dt.datetime(
        primeiro.year, primeiro.month, primeiro.day, tzinfo=dt.timezone.utc
    )


def nome_particao(inicio: dt.date | dt.datetime) -> str:
    return f"{TABELA}_{inicio.year:04d}_{inicio.month:02d}"


def _resolver_parent(cur) -> _Relacao:
    cur.execute(
        "SELECT c.oid, n.nspname AS schema, c.relname AS nome "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.oid = to_regclass(%s) AND c.relkind = 'p'",
        (TABELA,),
    )
    linha = cur.fetchone()
    if linha is None:
        raise RuntimeError(f"{TABELA} não existe ou não é uma tabela particionada")
    return _Relacao(int(linha["oid"]), linha["schema"], linha["nome"], None, None)


def _buscar_relacao(cur, schema: str, nome: str) -> _Relacao | None:
    cur.execute(
        "SELECT c.oid, n.nspname AS schema, c.relname AS nome, "
        "i.inhparent AS parent_oid, pg_get_expr(c.relpartbound, c.oid) AS bound "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_inherits i ON i.inhrelid = c.oid "
        "WHERE n.nspname = %s AND c.relname = %s",
        (schema, nome),
    )
    linha = cur.fetchone()
    if linha is None:
        return None
    return _Relacao(
        int(linha["oid"]),
        linha["schema"],
        linha["nome"],
        int(linha["parent_oid"]) if linha["parent_oid"] is not None else None,
        linha["bound"],
    )


def _filhos(cur, parent: _Relacao) -> list[_Relacao]:
    cur.execute(
        "SELECT c.oid, n.nspname AS schema, c.relname AS nome, "
        "i.inhparent AS parent_oid, pg_get_expr(c.relpartbound, c.oid) AS bound "
        "FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE i.inhparent = %s ORDER BY c.relname",
        (parent.oid,),
    )
    return [
        _Relacao(
            int(linha["oid"]),
            linha["schema"],
            linha["nome"],
            int(linha["parent_oid"]),
            linha["bound"],
        )
        for linha in cur.fetchall()
    ]


def _datetime_bound(valor: str) -> dt.datetime:
    convertido = dt.datetime.fromisoformat(valor.replace(" ", "T", 1))
    if convertido.tzinfo is None:
        raise RuntimeError(f"partition bound sem timezone explícito: {valor}")
    return convertido.astimezone(dt.timezone.utc)


def _bounds(relacao: _Relacao) -> tuple[dt.datetime, dt.datetime]:
    casamento = PADRAO_BOUND.fullmatch(relacao.bound or "")
    if casamento is None:
        raise RuntimeError(
            f"partição {relacao.schema}.{relacao.nome} tem bound inválido: {relacao.bound}"
        )
    return _datetime_bound(casamento.group(1)), _datetime_bound(casamento.group(2))


def _validar_particao(
    relacao: _Relacao,
    parent: _Relacao,
    inicio: dt.datetime,
    fim: dt.datetime,
) -> None:
    if relacao.parent_oid != parent.oid:
        raise RuntimeError(
            f"{relacao.schema}.{relacao.nome} existe, mas não é partição de "
            f"{parent.schema}.{parent.nome}"
        )
    if _bounds(relacao) != (inicio, fim):
        raise RuntimeError(
            f"partição {relacao.schema}.{relacao.nome} tem bound diferente de "
            f"[{inicio.isoformat()}, {fim.isoformat()})"
        )


def _particao_default(cur, parent: _Relacao) -> _Relacao | None:
    defaults = [relacao for relacao in _filhos(cur, parent) if relacao.bound == "DEFAULT"]
    if len(defaults) > 1:
        raise RuntimeError(f"{parent.schema}.{parent.nome} tem mais de uma DEFAULT")
    return defaults[0] if defaults else None


def _criar_particao(
    conn, nome: str, inicio: dt.datetime, fim: dt.datetime
) -> bool:
    from psycopg import sql

    with conn.transaction(), conn.cursor() as cur:
        parent = _resolver_parent(cur)
        existente = _buscar_relacao(cur, parent.schema, nome)
        if existente is not None:
            _validar_particao(existente, parent, inicio, fim)
            return False

        default = _particao_default(cur, parent)
        stage = f"stage_{nome}"
        movidas = 0
        if default is not None:
            cur.execute(
                sql.SQL("LOCK TABLE {}.{} IN ACCESS EXCLUSIVE MODE").format(
                    sql.Identifier(default.schema), sql.Identifier(default.nome)
                )
            )
            cur.execute(
                sql.SQL(
                    "CREATE TEMP TABLE {} ON COMMIT DROP AS "
                    "SELECT * FROM {}.{} WITH NO DATA"
                ).format(
                    sql.Identifier(stage),
                    sql.Identifier(parent.schema),
                    sql.Identifier(parent.nome),
                )
            )
            cur.execute(
                sql.SQL(
                    "WITH movidas AS ("
                    "DELETE FROM {}.{} WHERE occurred_at >= %s AND occurred_at < %s "
                    "RETURNING *) INSERT INTO {} SELECT * FROM movidas"
                ).format(
                    sql.Identifier(default.schema),
                    sql.Identifier(default.nome),
                    sql.Identifier(stage),
                ),
                (inicio, fim),
            )
            movidas = max(cur.rowcount, 0)

        cur.execute(
            sql.SQL(
                "CREATE TABLE {}.{} PARTITION OF {}.{} "
                "FOR VALUES FROM ({}) TO ({})"
            ).format(
                sql.Identifier(parent.schema),
                sql.Identifier(nome),
                sql.Identifier(parent.schema),
                sql.Identifier(parent.nome),
                sql.Literal(inicio),
                sql.Literal(fim),
            )
        )

        if movidas:
            cur.execute(
                sql.SQL(
                    "INSERT INTO {}.{} OVERRIDING SYSTEM VALUE SELECT * FROM {}"
                ).format(
                    sql.Identifier(parent.schema),
                    sql.Identifier(parent.nome),
                    sql.Identifier(stage),
                )
            )
            if cur.rowcount != movidas:
                raise RuntimeError(
                    f"relocação incompleta da DEFAULT: removidas={movidas}, "
                    f"reinseridas={cur.rowcount}"
                )

        criada = _buscar_relacao(cur, parent.schema, nome)
        if criada is None:
            raise RuntimeError(f"CREATE TABLE não criou {parent.schema}.{nome}")
        _validar_particao(criada, parent, inicio, fim)
    return True


def garantir_particoes(
    conn, meses_adiante: int = 1, hoje: dt.date | None = None
) -> list[str]:
    hoje = hoje or dt.datetime.now(dt.timezone.utc).date()
    criadas: list[str] = []
    for deslocamento in range(0, meses_adiante + 1):
        inicio = _inicio_utc(hoje, deslocamento)
        fim = _inicio_utc(hoje, deslocamento + 1)
        nome = nome_particao(inicio)
        if _criar_particao(conn, nome, inicio, fim):
            log.info("partição criada: %s [%s, %s)", nome, inicio, fim)
            criadas.append(nome)
    return criadas


def expurgar_particoes(
    conn, retention_months: int, hoje: dt.date | None = None
) -> list[str]:
    if retention_months < 1:
        raise ValueError("retention_months deve ser >= 1")
    from psycopg import sql

    hoje = hoje or dt.datetime.now(dt.timezone.utc).date()
    cutoff = _inicio_utc(hoje, -retention_months)
    with conn.cursor() as cur:
        parent = _resolver_parent(cur)
        candidatas = _filhos(cur, parent)

    vencidas: list[_Relacao] = []
    for relacao in candidatas:
        casamento = PADRAO_NOME.fullmatch(relacao.nome)
        if casamento is None:
            continue
        mes = dt.date(int(casamento.group(1)), int(casamento.group(2)), 1)
        inicio_esperado = _inicio_utc(mes)
        fim_esperado = _inicio_utc(mes, 1)
        _validar_particao(relacao, parent, inicio_esperado, fim_esperado)
        if fim_esperado <= cutoff:
            vencidas.append(relacao)

    dropadas: list[str] = []
    for relacao in vencidas:
        log.warning(
            "expurgando partição vencida: %s (retenção %d meses)",
            relacao.nome,
            retention_months,
        )
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                sql.SQL("DROP TABLE {}.{}").format(
                    sql.Identifier(relacao.schema), sql.Identifier(relacao.nome)
                )
            )
        dropadas.append(relacao.nome)
    return dropadas


def listar_particoes(conn) -> list[str]:
    with conn.cursor() as cur:
        parent = _resolver_parent(cur)
        return [relacao.nome for relacao in _filhos(cur, parent)]


def contar_default(conn) -> int:
    from psycopg import sql

    with conn.cursor() as cur:
        parent = _resolver_parent(cur)
        default = _particao_default(cur, parent)
        if default is None:
            return 0
        cur.execute(
            sql.SQL("SELECT count(*) AS total FROM {}.{}").format(
                sql.Identifier(default.schema), sql.Identifier(default.nome)
            )
        )
        return int(cur.fetchone()["total"])


def expurgar_ingest_file(conn, dias: int) -> int:
    if dias < 1:
        raise ValueError("dias deve ser >= 1")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "DELETE FROM ingest_file "
            "WHERE processed_at < now() - make_interval(days => %s) "
            "AND status <> 'QUARANTINED'",
            (dias,),
        )
        return max(cur.rowcount, 0)
