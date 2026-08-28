"""Motor de eventos — as 6 regras de transição da SPEC seção 8.2.

Precisa de PostgreSQL: o diff vive no SQL, não em Python, então testá-lo sem
banco testaria outra coisa. Defina `TEST_PG_DSN` apontando para um banco
descartável; sem ela os testes são pulados.
"""

from __future__ import annotations

import datetime as dt

import pytest

from fixtures import Bucket, envelope, enriched, finding_vm, finding_was

pytestmark = pytest.mark.banco


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ciclo(ingestor, bucket, modo, tipos=("FINDING",)):
    for tipo in tipos:
        bucket.fechar_manifest(tipo)
    return ingestor(bucket.store).executar(modo=modo)


def eventos(conn, finding_id=None):
    with conn.cursor() as cur:
        if finding_id:
            cur.execute(
                "SELECT event_type, occurred_at, old_state, new_state, source_path "
                "FROM finding_event WHERE finding_id = %s ORDER BY occurred_at, event_type",
                (finding_id,),
            )
        else:
            cur.execute(
                "SELECT finding_id, event_type, occurred_at, old_state, new_state "
                "FROM finding_event ORDER BY occurred_at, event_type"
            )
        return cur.fetchall()


def tipos_de_evento(conn, finding_id=None):
    return [linha["event_type"] for linha in eventos(conn, finding_id)]


def estado(conn, finding_id):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM finding_current WHERE finding_id = %s", (finding_id,))
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Modo SEED (seção 9.2)
# ---------------------------------------------------------------------------
def test_seed_popula_estado_e_gera_zero_eventos(ingestor, conn):
    """Um snapshot não é uma sequência de mudanças. Gerar evento a partir dele
    inventaria história: centenas de milhares de OPENED que ninguém observou."""
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="f1")]))
    resultado = ciclo(ingestor, bucket, "SEED")

    assert resultado.payloads_ok == 1
    assert resultado.eventos == 0
    assert eventos(conn) == []
    assert estado(conn, "f1")["state"] == "OPEN"


def test_seed_grava_o_modo_em_ingest_file(ingestor, conn):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="f1")]))
    ciclo(ingestor, bucket, "SEED")
    with conn.cursor() as cur:
        cur.execute("SELECT mode, status, rows_read FROM ingest_file")
        linha = cur.fetchone()
    assert linha["mode"] == "SEED"
    assert linha["status"] == "OK"
    assert linha["rows_read"] == 1


# ---------------------------------------------------------------------------
# Regra 1 — inédito aberto
# ---------------------------------------------------------------------------
def test_regra1_inedito_aberto_gera_opened_datado_por_first_found(ingestor, conn):
    bucket = Bucket()
    bucket.adicionar(
        "FINDING",
        envelope("FINDING", [finding_vm(finding_id="f1", first_found="2024-03-06T14:53:09Z")]),
    )
    ciclo(ingestor, bucket, "INCREMENTAL")

    registros = eventos(conn, "f1")
    assert [e["event_type"] for e in registros] == ["OPENED"]
    assert registros[0]["occurred_at"].year == 2024, "data do dado, não do job"
    assert registros[0]["new_state"] == "OPEN"


# ---------------------------------------------------------------------------
# Regra 2 — inédito já FIXED (seção 8.3)
# ---------------------------------------------------------------------------
def test_regra2_inedito_fechado_gera_opened_retroativo_e_fixed(ingestor, conn):
    """Sem isto o fechamento desaparece: não gera OPENED (chegou fechado) nem
    FIXED (não havia linha aberta antes), e o trabalho de remediação some da
    estatística."""
    bucket = Bucket()
    bucket.adicionar(
        "FINDING",
        envelope("FINDING", [finding_vm(
            finding_id="f1",
            state="FIXED",
            first_found="2019-05-01T00:00:00Z",
            last_fixed="2026-08-20T10:00:00Z",
        )]),
    )
    ciclo(ingestor, bucket, "INCREMENTAL")

    registros = eventos(conn, "f1")
    assert [e["event_type"] for e in registros] == ["OPENED", "FIXED"]
    assert registros[0]["occurred_at"].year == 2019
    assert registros[1]["occurred_at"].year == 2026


def test_regra2_opened_de_2019_cai_na_particao_default(ingestor, conn):
    """É este caso que exige a partição DEFAULT: um OPENED de 2019 não cabe nas
    partições dos meses recentes, e sem ela o INSERT falharia."""
    bucket = Bucket()
    bucket.adicionar(
        "FINDING",
        envelope("FINDING", [finding_vm(
            finding_id="f1", state="FIXED",
            first_found="2019-05-01T00:00:00Z", last_fixed="2026-08-20T10:00:00Z",
        )]),
    )
    ciclo(ingestor, bucket, "INCREMENTAL")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS total FROM finding_event_default "
            "WHERE finding_id = 'f1' AND event_type = 'OPENED'"
        )
        assert cur.fetchone()["total"] == 1


def test_regra2c_inedito_reopened_gera_opened_e_reopened(ingestor, conn):
    """Seção 8.3: 'o mesmo vale para inédito chegando REOPENED'."""
    bucket = Bucket()
    bucket.adicionar(
        "FINDING",
        envelope("FINDING", [finding_vm(
            finding_id="f1", state="REOPENED",
            first_found="2024-01-01T00:00:00Z",
            resurfaced_date="2026-08-25T00:00:00Z",
        )]),
    )
    ciclo(ingestor, bucket, "INCREMENTAL")

    registros = eventos(conn, "f1")
    assert [e["event_type"] for e in registros] == ["OPENED", "REOPENED"]
    assert registros[0]["occurred_at"] == dt.datetime(
        2024, 1, 1, tzinfo=dt.timezone.utc
    )
    assert registros[1]["occurred_at"] == dt.datetime(
        2026, 8, 25, tzinfo=dt.timezone.utc
    )


# ---------------------------------------------------------------------------
# Regras 3 e 4 — reabertura e fechamento
# ---------------------------------------------------------------------------
def test_regra4_fechamento_datado_por_last_fixed(ingestor, conn):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", indexed="2026-08-27T10:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", state="FIXED",
        last_fixed="2026-08-28T09:00:00Z", indexed="2026-08-28T10:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    registros = eventos(conn, "f1")
    assert [e["event_type"] for e in registros] == ["OPENED", "FIXED"]
    fechamento = registros[1]
    assert fechamento["old_state"] == "OPEN"
    assert fechamento["new_state"] == "FIXED"
    assert fechamento["occurred_at"].day == 28
    assert estado(conn, "f1")["state"] == "FIXED"


def test_regra3_reabertura_depois_de_fixed(ingestor, conn):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", state="FIXED",
        last_fixed="2026-08-01T00:00:00Z", indexed="2026-08-01T10:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", state="REOPENED",
        resurfaced_date="2026-08-10T00:00:00Z", indexed="2026-08-10T10:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    assert tipos_de_evento(conn, "f1") == ["OPENED", "FIXED", "REOPENED"]
    assert estado(conn, "f1")["state"] == "REOPENED"


# ---------------------------------------------------------------------------
# Regra 5 — delete (seção 6.7)
# ---------------------------------------------------------------------------
def test_regra5_delete_gera_deleted_e_nunca_fixed(ingestor, conn):
    """Delete não é remediação: é o finding sumindo do Tenable. Se virasse
    FIXED, a métrica de remediação inflaria com trabalho que ninguém fez."""
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", indexed="2026-08-27T10:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    bucket.adicionar(
        "FINDING",
        envelope("FINDING", [], [{"_id": "f1", "deleted_at": "2026-08-28T12:00:00Z"}]),
    )
    ciclo(ingestor, bucket, "INCREMENTAL")

    assert tipos_de_evento(conn, "f1") == ["OPENED", "DELETED"]
    assert "FIXED" not in tipos_de_evento(conn, "f1")

    linha = estado(conn, "f1")
    assert linha["deleted_at"].day == 28
    assert linha["state"] == "OPEN", "o state NÃO DEVE ser alterado pelo delete"


def test_delete_repetido_nao_duplica_evento(ingestor, conn):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="f1")]))
    ciclo(ingestor, bucket, "INCREMENTAL")
    for _ in range(2):
        bucket.adicionar(
            "FINDING",
            envelope("FINDING", [], [{"_id": "f1", "deleted_at": "2026-08-28T12:00:00Z"}]),
        )
        ciclo(ingestor, bucket, "INCREMENTAL")
    assert tipos_de_evento(conn, "f1").count("DELETED") == 1


def test_update_depois_de_delete_ressuscita_o_finding(ingestor, conn):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", indexed="2026-08-27T10:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")
    bucket.adicionar(
        "FINDING",
        envelope("FINDING", [], [{"_id": "f1", "deleted_at": "2026-08-28T12:00:00Z"}]),
    )
    ciclo(ingestor, bucket, "INCREMENTAL")
    assert estado(conn, "f1")["deleted_at"] is not None

    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", indexed="2026-08-29T10:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")
    assert estado(conn, "f1")["deleted_at"] is None, "espelho puro: se voltou, existe"


# ---------------------------------------------------------------------------
# Regra 6 — recast (independente das demais)
# ---------------------------------------------------------------------------
def test_regra6_recast_sai_junto_com_fixed(ingestor, conn):
    """A regra 6 é independente e pode ocorrer junto com qualquer outra — é por
    isso que a implementação usa UNION ALL de blocos e não um CASE único (um
    CASE devolve um evento por linha e perderia o segundo)."""
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", indexed="2026-08-27T10:00:00Z",
        severity_modification_type="NONE")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", state="FIXED", last_fixed="2026-08-28T09:00:00Z",
        indexed="2026-08-28T10:00:00Z",
        severity_modification_type="RECASTED", severity="HIGH")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    tipos = tipos_de_evento(conn, "f1")
    assert "FIXED" in tipos and "RECAST_CHANGED" in tipos


def test_recast_normaliza_caixa_e_nao_dispara_falso_positivo(ingestor, conn):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", indexed="2026-08-27T10:00:00Z",
        severity_modification_type="none")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", indexed="2026-08-28T10:00:00Z",
        severity_modification_type="NONE")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    assert "RECAST_CHANGED" not in tipos_de_evento(conn, "f1")


# ---------------------------------------------------------------------------
# Guarda de ordem (seção 6.4)
# ---------------------------------------------------------------------------
def test_versao_antiga_chegando_depois_nao_faz_o_estado_regredir(ingestor, conn):
    """Durante o backfill, uma versão antiga do finding chega depois da atual.
    Sem a guarda `EXCLUDED.indexed > f.indexed`, o estado anda para trás."""
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", state="FIXED", severity="HIGH",
        last_fixed="2026-08-20T00:00:00Z", indexed="2026-08-27T10:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", state="OPEN", severity="LOW",
        indexed="2020-01-01T00:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    linha = estado(conn, "f1")
    assert linha["state"] == "FIXED"
    assert linha["severity"] == "HIGH"


def test_first_found_nunca_regride(ingestor, conn):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", first_found="2020-01-01T00:00:00Z",
        indexed="2026-08-27T10:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", first_found="2026-08-01T00:00:00Z",
        indexed="2026-08-28T10:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    assert estado(conn, "f1")["first_found"] == dt.datetime(
        2020, 1, 1, tzinfo=dt.timezone.utc
    )


# ---------------------------------------------------------------------------
# Dedup intra-arquivo (seção 6.3)
# ---------------------------------------------------------------------------
def test_abriu_e_fechou_no_mesmo_arquivo_preserva_o_par(ingestor, conn):
    """Descartar o intermediário antes de gerar evento perderia o par
    OPENED+FIXED. Raro em VM, menos raro em WAS (rescan rápido)."""
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [
        finding_vm(finding_id="f1", state="OPEN",
                   first_found="2026-08-27T08:00:00Z", indexed="2026-08-27T10:00:00Z"),
        finding_vm(finding_id="f1", state="FIXED",
                   first_found="2026-08-27T08:00:00Z",
                   last_fixed="2026-08-27T10:10:00Z", indexed="2026-08-27T10:15:00Z"),
    ]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    assert tipos_de_evento(conn, "f1") == ["OPENED", "FIXED"]
    linha = estado(conn, "f1")
    assert linha["state"] == "FIXED", "o upsert aplica só o último"
    assert linha["indexed"].minute == 15


def test_existing_open_fixed_reopened_no_mesmo_payload(ingestor, conn):
    """A timeline precisa comparar cada versão com a anterior no payload.

    Se todas as linhas forem comparadas apenas com o baseline OPEN persistido,
    a transição FIXED -> REOPENED desaparece.
    """
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", state="OPEN", indexed="2026-08-27T10:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    bucket.adicionar("FINDING", envelope("FINDING", [
        finding_vm(
            finding_id="f1", state="FIXED",
            last_fixed="2026-08-28T11:00:00Z",
            indexed="2026-08-28T11:05:00Z",
        ),
        finding_vm(
            finding_id="f1", state="REOPENED",
            resurfaced_date="2026-08-28T12:00:00Z",
            indexed="2026-08-28T12:05:00Z",
        ),
    ]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    assert tipos_de_evento(conn, "f1")[-2:] == ["FIXED", "REOPENED"]
    assert estado(conn, "f1")["state"] == "REOPENED"


def test_payload_antigo_nao_gera_evento_espurio(ingestor, conn):
    """Uma versão anterior ao relógio persistido não pertence à timeline."""
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", state="FIXED",
        last_fixed="2026-08-27T09:00:00Z",
        indexed="2026-08-27T10:00:00Z",
    )]))
    ciclo(ingestor, bucket, "INCREMENTAL")
    antes = eventos(conn, "f1")

    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", state="OPEN", indexed="2020-01-01T10:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    assert eventos(conn, "f1") == antes


def test_delete_antigo_nao_apaga_update_novo(ingestor, conn):
    """A posição no payload não pode fazer um tombstone antigo vencer."""
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", indexed="2026-08-28T12:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    bucket.adicionar(
        "FINDING",
        envelope("FINDING", [], [{"_id": "f1", "deleted_at": "2026-08-20T12:00:00Z"}]),
    )
    ciclo(ingestor, bucket, "INCREMENTAL")

    assert estado(conn, "f1")["deleted_at"] is None
    assert "DELETED" not in tipos_de_evento(conn, "f1")


def test_delete_aceito_avanca_relogio_e_update_empatado_nao_ressuscita(ingestor, conn):
    """Sem avançar indexed, um replay empatado poderia limpar o tombstone."""
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", state="OPEN", indexed="2026-08-27T10:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    bucket.adicionar(
        "FINDING",
        envelope("FINDING", [], [{"_id": "f1", "deleted_at": "2026-08-28T12:00:00Z"}]),
    )
    ciclo(ingestor, bucket, "INCREMENTAL")
    apagado = estado(conn, "f1")
    assert apagado["state"] == "OPEN"
    assert apagado["indexed"] == dt.datetime(
        2026, 8, 28, 12, tzinfo=dt.timezone.utc
    )

    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", state="REOPENED", indexed="2026-08-28T12:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    empatado = estado(conn, "f1")
    assert empatado["deleted_at"] is not None
    assert empatado["state"] == "OPEN"


def test_update_e_delete_empatados_no_mesmo_payload_aplicam_delete(ingestor, conn):
    """O delete vem depois no staging e deve vencer o update de mesmo relógio."""
    bucket = Bucket()
    bucket.adicionar(
        "FINDING",
        envelope(
            "FINDING",
            [finding_vm(
                finding_id="f1", state="OPEN",
                first_found="2026-08-28T08:00:00Z",
                indexed="2026-08-28T12:00:00Z",
            )],
            [{"_id": "f1", "deleted_at": "2026-08-28T12:00:00Z"}],
        ),
    )
    ciclo(ingestor, bucket, "INCREMENTAL")

    assert tipos_de_evento(conn, "f1") == ["OPENED", "DELETED"]
    linha = estado(conn, "f1")
    assert linha["deleted_at"] is not None
    assert linha["state"] == "OPEN"


def test_seed_deduplica_mantendo_o_maior_indexed(ingestor, conn):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [
        finding_vm(finding_id="f1", severity="LOW", indexed="2026-08-27T10:15:00Z"),
        finding_vm(finding_id="f1", severity="HIGH", indexed="2026-08-27T10:00:00Z"),
    ]))
    ciclo(ingestor, bucket, "SEED")

    assert estado(conn, "f1")["severity"] == "LOW"
    assert eventos(conn) == []


# ---------------------------------------------------------------------------
# WAS e enriched
# ---------------------------------------------------------------------------
def test_was_e_vm_convivem_na_mesma_tabela(ingestor, conn):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="vm1")]))
    bucket.adicionar("WAS_FINDING", envelope("WAS_FINDING", [finding_was(finding_id="was1")]))
    ciclo(ingestor, bucket, "INCREMENTAL", tipos=("FINDING", "WAS_FINDING"))

    with conn.cursor() as cur:
        cur.execute("SELECT product, count(*) AS total FROM finding_current GROUP BY 1 ORDER BY 1")
        assert [(l["product"], l["total"]) for l in cur.fetchall()] == [("VM", 1), ("WAS", 1)]


def test_recast_chega_antes_do_finding_e_nao_e_descartado(ingestor, conn):
    """SEM foreign key para finding_current, de propósito: o recast pode chegar
    antes do finding correspondente."""
    bucket = Bucket()
    bucket.adicionar(
        "FINDING_ENRICHED_ATTRIBUTES",
        envelope("FINDING_ENRICHED_ATTRIBUTES", [enriched(finding_id="ainda-nao-existe")]),
    )
    ciclo(ingestor, bucket, "INCREMENTAL", tipos=("FINDING_ENRICHED_ATTRIBUTES",))

    with conn.cursor() as cur:
        cur.execute("SELECT finding_id, modification FROM finding_recast")
        linha = cur.fetchone()
    assert linha["finding_id"] == "ainda-nao-existe"
    assert linha["modification"] == "RECASTED"


def test_plugin_vai_para_tabela_separada_e_nao_repete(ingestor, conn):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [
        finding_vm(finding_id="f1"), finding_vm(finding_id="f2"),
    ]))
    ciclo(ingestor, bucket, "SEED")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS total FROM plugin")
        assert cur.fetchone()["total"] == 1
        cur.execute("SELECT plugin_id, plugin_name FROM finding_current ORDER BY finding_id")
        linhas = cur.fetchall()
    assert {l["plugin_id"] for l in linhas} == {14272}
    assert all(l["plugin_name"] for l in linhas), "plugin_name é denormalizado"


# ---------------------------------------------------------------------------
# Integridade e quarentena (seção 12)
# ---------------------------------------------------------------------------
def test_md5_divergente_manda_o_arquivo_para_falha(ingestor, conn):
    bucket = Bucket()
    bucket.adicionar(
        "FINDING", envelope("FINDING", [finding_vm(finding_id="f1")]),
        md5_forcado="00000000000000000000000000000000",
    )
    resultado = ciclo(ingestor, bucket, "INCREMENTAL")

    assert resultado.payloads_ok == 0
    assert resultado.payloads_falhos == 1
    with conn.cursor() as cur:
        cur.execute("SELECT status, attempt_count, error_message FROM ingest_file")
        linha = cur.fetchone()
    assert linha["status"] == "FAILED"
    assert linha["attempt_count"] == 1
    assert "md5" in linha["error_message"]
    assert estado(conn, "f1") is None, "nada meio aplicado"


def test_arquivo_envenenado_vai_para_quarentena_e_a_fila_segue(ingestor, conn):
    """Um buraco conhecido e alarmado é melhor que uma fila parada em
    silêncio: depois de MAX_ATTEMPTS o arquivo é quarentenado e o pipeline
    continua."""
    bucket = Bucket()
    bucket.adicionar(
        "FINDING", envelope("FINDING", [finding_vm(finding_id="f1")]),
        md5_forcado="00000000000000000000000000000000",
    )
    bucket.fechar_manifest("FINDING")

    for _ in range(3):
        ingestor(bucket.store).executar(modo="INCREMENTAL")

    with conn.cursor() as cur:
        cur.execute("SELECT status, attempt_count FROM ingest_file")
        linha = cur.fetchone()
    assert linha["status"] == "QUARANTINED"
    assert linha["attempt_count"] == 3

    # a fila segue: um payload novo entra normalmente
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="f2")]))
    resultado = ciclo(ingestor, bucket, "INCREMENTAL")
    assert resultado.payloads_ok == 1
    assert estado(conn, "f2") is not None


def test_contagem_divergente_do_manifest_falha(ingestor, conn):
    bucket = Bucket()
    doc = envelope("FINDING", [finding_vm(finding_id="f1")])
    path = bucket.adicionar("FINDING", doc)
    # o manifest já foi gravado com num_updates=1; muda o payload por baixo
    from fixtures import comprimir, md5

    novo = envelope("FINDING", [finding_vm(finding_id="f1"), finding_vm(finding_id="f2")])
    bucket.store[path] = comprimir(novo)
    bucket._entradas["FINDING"][0]["md5"] = md5(bucket.store[path])

    resultado = ciclo(ingestor, bucket, "INCREMENTAL")
    assert resultado.payloads_falhos == 1
    with conn.cursor() as cur:
        cur.execute("SELECT error_message FROM ingest_file")
        assert "update(s)" in cur.fetchone()["error_message"]


def test_versao_de_schema_divergente_nao_interrompe(ingestor, conn, caplog):
    """Seção 12.4: alertar e CONTINUAR — a mudança pode ser aditiva."""
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="f1")], version=99))
    resultado = ciclo(ingestor, bucket, "INCREMENTAL")

    assert resultado.payloads_ok == 1
    assert estado(conn, "f1") is not None
    assert any("ALERTA schema" in registro.message for registro in caplog.records)


def test_payload_ja_processado_e_pulado(ingestor, conn):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="f1")]))
    bucket.fechar_manifest("FINDING")

    primeiro = ingestor(bucket.store).executar(modo="INCREMENTAL")
    segundo = ingestor(bucket.store).executar(modo="INCREMENTAL")

    assert primeiro.payloads_ok == 1
    assert segundo.payloads_ok == 0
    assert segundo.payloads_pulados == 1
