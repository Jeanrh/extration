from __future__ import annotations

import datetime as dt

import pytest

from ingestion import partitions

pytestmark = pytest.mark.banco


def _drop(conn, *nomes):
    from psycopg import sql

    with conn.transaction(), conn.cursor() as cur:
        for nome in nomes:
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(nome)))


def _bounds(conn, nome):
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")
        cur.execute(
            "SELECT pg_get_expr(c.relpartbound, c.oid) AS bound "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relname = %s",
            (nome,),
        )
        return cur.fetchone()["bound"]


def test_garante_mes_atual_e_seguinte_com_bounds_utc_e_e_idempotente(conn):
    nomes = ("finding_event_2036_08", "finding_event_2036_09")
    _drop(conn, *nomes)
    try:
        assert partitions.garantir_particoes(
            conn, hoje=dt.date(2036, 8, 27)
        ) == list(nomes)
        assert _bounds(conn, nomes[0]) == (
            "FOR VALUES FROM ('2036-08-01 00:00:00+00') "
            "TO ('2036-09-01 00:00:00+00')"
        )
        assert _bounds(conn, nomes[1]) == (
            "FOR VALUES FROM ('2036-09-01 00:00:00+00') "
            "TO ('2036-10-01 00:00:00+00')"
        )
        assert partitions.garantir_particoes(conn, hoje=dt.date(2036, 8, 27)) == []
    finally:
        _drop(conn, *nomes)


def test_tabela_com_nome_conflitante_falha_visivelmente(conn):
    nome = "finding_event_2037_08"
    _drop(conn, nome)
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(f"CREATE TABLE {nome} (id integer)")

        with pytest.raises(RuntimeError, match="não é partição"):
            partitions.garantir_particoes(
                conn, meses_adiante=0, hoje=dt.date(2037, 8, 1)
            )
    finally:
        _drop(conn, nome)


def test_particao_com_bound_errado_falha_visivelmente(conn):
    nome = "finding_event_2038_08"
    _drop(conn, nome)
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE {nome} PARTITION OF finding_event "
                "FOR VALUES FROM ('2038-07-01T00:00:00Z') "
                "TO ('2038-08-01T00:00:00Z')"
            )

        with pytest.raises(RuntimeError, match="bound"):
            partitions.garantir_particoes(
                conn, meses_adiante=0, hoje=dt.date(2038, 8, 1)
            )
    finally:
        _drop(conn, nome)


def test_move_linhas_da_default_atomicamente_sem_perder_id_ou_conteudo(conn):
    nome = "finding_event_2039_08"
    _drop(conn, nome)
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO finding_event "
                "(finding_id, product, event_type, occurred_at, old_value, new_value) "
                "VALUES "
                "('f-1', 'VM', 'OPENED', '2039-08-12T12:34:56Z', "
                " '{\"a\": 1}'::jsonb, '{\"b\": 2}'::jsonb) "
                "RETURNING id"
            )
            evento_id = cur.fetchone()["id"]
            cur.execute(
                "SELECT count(*) AS total, "
                "md5(string_agg(row_to_json(e)::text, '' ORDER BY id)) AS hash "
                "FROM finding_event e WHERE occurred_at >= '2039-08-01T00:00:00Z' "
                "AND occurred_at < '2039-09-01T00:00:00Z'"
            )
            antes = cur.fetchone()

        assert partitions.garantir_particoes(
            conn, meses_adiante=0, hoje=dt.date(2039, 8, 1)
        ) == [nome]

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS total, "
                "md5(string_agg(row_to_json(e)::text, '' ORDER BY id)) AS hash "
                "FROM finding_event e WHERE occurred_at >= '2039-08-01T00:00:00Z' "
                "AND occurred_at < '2039-09-01T00:00:00Z'"
            )
            depois = cur.fetchone()
            cur.execute(f"SELECT id FROM {nome}")
            assert cur.fetchone()["id"] == evento_id
            cur.execute(
                "SELECT count(*) AS total FROM finding_event_default "
                "WHERE occurred_at >= '2039-08-01T00:00:00Z' "
                "AND occurred_at < '2039-09-01T00:00:00Z'"
            )
            assert cur.fetchone()["total"] == 0
        assert depois == antes
    finally:
        _drop(conn, nome)


def test_retencao_dropa_somente_meses_gerenciados_inteiramente_vencidos(conn):
    from psycopg import sql

    mensais = (
        "finding_event_2040_05",
        "finding_event_2040_06",
        "finding_event_2040_07",
        "finding_event_2040_08",
    )
    legado = "finding_event_legacy"
    comum = "finding_event_archive"
    _drop(conn, *mensais, legado, comum)
    try:
        with conn.transaction(), conn.cursor() as cur:
            for mes in (5, 6, 7, 8):
                inicio = dt.datetime(2040, mes, 1, tzinfo=dt.timezone.utc)
                if mes == 12:
                    fim = dt.datetime(2041, 1, 1, tzinfo=dt.timezone.utc)
                else:
                    fim = dt.datetime(2040, mes + 1, 1, tzinfo=dt.timezone.utc)
                cur.execute(
                    sql.SQL(
                        "CREATE TABLE {} PARTITION OF finding_event "
                        "FOR VALUES FROM ({}) TO ({})"
                    ).format(
                        sql.Identifier(f"finding_event_2040_{mes:02d}"),
                        sql.Literal(inicio),
                        sql.Literal(fim),
                    )
                )
            cur.execute(
                f"CREATE TABLE {legado} PARTITION OF finding_event "
                "FOR VALUES FROM ('2000-01-01T00:00:00Z') TO ('2000-02-01T00:00:00Z')"
            )
            cur.execute(f"CREATE TABLE {comum} (id integer)")

        assert partitions.expurgar_particoes(
            conn, retention_months=24, hoje=dt.date(2042, 8, 15)
        ) == list(mensais[:3])
        assert set(partitions.listar_particoes(conn)) >= {
            mensais[3], legado, "finding_event_default"
        }
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s) AS rel", (comum,))
            assert cur.fetchone()["rel"] == comum
    finally:
        _drop(conn, *mensais, legado, comum)


def test_retencao_preserva_relacao_que_substitui_candidata_stale(
    conn, config_teste
):
    from psycopg import sql

    from ingestion.db import conectar

    nome = "finding_event_2044_01"
    _drop(conn, nome)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "CREATE TABLE {} PARTITION OF finding_event "
                "FOR VALUES FROM ({}) TO ({})"
            ).format(
                sql.Identifier(nome),
                sql.Literal(dt.datetime(2044, 1, 1, tzinfo=dt.timezone.utc)),
                sql.Literal(dt.datetime(2044, 2, 1, tzinfo=dt.timezone.utc)),
            )
        )

    def substituir_candidata():
        with conectar(config_teste.pg_dsn) as outra:
            with outra.transaction(), outra.cursor() as cur:
                cur.execute(
                    sql.SQL("DROP TABLE {} ").format(sql.Identifier(nome))
                )
                cur.execute(
                    sql.SQL("CREATE TABLE {} (marcador text)").format(
                        sql.Identifier(nome)
                    )
                )

    class _ConexaoComTroca:
        def __init__(self, real):
            self.real = real
            self.trocou = False

        def cursor(self):
            return self.real.cursor()

        def transaction(self):
            if not self.trocou:
                substituir_candidata()
                self.trocou = True
            return self.real.transaction()

    try:
        with pytest.raises(RuntimeError, match="candidata.*mudou"):
            partitions.expurgar_particoes(
                _ConexaoComTroca(conn),
                retention_months=24,
                hoje=dt.date(2046, 3, 15),
            )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.relkind, a.attname "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.oid "
                "WHERE n.nspname = current_schema() AND c.relname = %s "
                "AND a.attname = 'marcador' AND NOT a.attisdropped",
                (nome,),
            )
            assert cur.fetchone() == {"relkind": "r", "attname": "marcador"}
    finally:
        _drop(conn, nome)


def test_retencao_ingest_file_preserva_quarentena(conn):
    with conn.transaction(), conn.cursor() as cur:
        for path, status in (("antigo-ok", "OK"), ("antigo-q", "QUARANTINED")):
            cur.execute(
                "INSERT INTO ingest_file "
                "(path, payload_type, manifest_path, status, mode, processed_at) "
                "VALUES (%s, 'FINDING', 'manifest', %s, 'SEED', now() - interval '100 days')",
                (path, status),
            )

    assert partitions.expurgar_ingest_file(conn, 90) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT path FROM ingest_file ORDER BY path")
        assert [row["path"] for row in cur.fetchall()] == ["antigo-q"]


class _NaoPodeTocarBanco:
    def cursor(self):
        raise AssertionError("retenção inválida tocou o banco")

    def transaction(self):
        raise AssertionError("retenção inválida abriu transação")


@pytest.mark.parametrize(
    ("funcao", "valor"),
    [
        (partitions.expurgar_particoes, 0),
        (partitions.expurgar_ingest_file, 0),
    ],
)
def test_retencao_invalida_falha_antes_de_ddl_ou_dml(funcao, valor):
    with pytest.raises(ValueError, match=">= 1"):
        funcao(_NaoPodeTocarBanco(), valor)
