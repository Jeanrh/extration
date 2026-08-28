"""Motor de eventos — as 6 regras de transição da SPEC seção 8.2.

Precisa de PostgreSQL: o diff vive no SQL, não em Python, então testá-lo sem
banco testaria outra coisa. Defina `TEST_PG_DSN` apontando para um banco
descartável; sem ela os testes são pulados.
"""

from __future__ import annotations

import datetime as dt
import json

import psycopg
import pytest

from conftest import RAIZ
from fixtures import Bucket, envelope, enriched, finding_vm, finding_was
from ingestion import loader as loader_mod
from ingestion.config import TIPOS_PAYLOAD
from ingestion.erros import ErroIntegridade, ErroParse
from ingestion.manifest import parse_manifest

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


def plugin(conn, plugin_id=14272):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM plugin WHERE plugin_id = %s", (plugin_id,))
        return cur.fetchone()


def recast(conn, finding_id):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM finding_recast WHERE finding_id = %s", (finding_id,))
        return cur.fetchone()


def adicionar_plugin(bucket, *, indexed, solution):
    registro = finding_vm(finding_id="plugin-clock", indexed=indexed)
    registro["plugin"]["solution"] = solution
    bucket.adicionar("FINDING", envelope("FINDING", [registro]))


def adicionar_recast(bucket, *, finding_id="recast-clock", updated_at, rule_comment):
    bucket.adicionar(
        "FINDING_ENRICHED_ATTRIBUTES",
        envelope(
            "FINDING_ENRICHED_ATTRIBUTES",
            [enriched(
                finding_id=finding_id,
                updated_at=updated_at,
                rule_comment=rule_comment,
            )],
        ),
    )


def adicionar_delete_recast(bucket, *, finding_id="recast-clock", deleted_at):
    bucket.adicionar(
        "FINDING_ENRICHED_ATTRIBUTES",
        envelope(
            "FINDING_ENRICHED_ATTRIBUTES",
            [],
            [{"id": finding_id, "deleted_at": deleted_at}],
        ),
    )


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


def test_payload_com_type_de_outro_stream_entra_no_ledger_de_falha(ingestor, conn):
    bucket = Bucket()
    doc = envelope("WAS_FINDING", [finding_was(finding_id="was-misrouted")])
    bucket.adicionar("FINDING", doc, scan_id="scan-misrouted")

    resultado = ciclo(ingestor, bucket, "INCREMENTAL")

    assert resultado.payloads_falhos == 1
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, first_record_timestamp, last_record_timestamp, scan_id "
            "FROM ingest_file"
        )
        linha = cur.fetchone()
    assert linha["status"] == "FAILED"
    assert linha["first_record_timestamp"] == dt.datetime(
        2026, 8, 27, 10, 32, 19, 356000, tzinfo=dt.timezone.utc
    )
    assert linha["last_record_timestamp"] == linha["first_record_timestamp"]
    assert linha["scan_id"] == "scan-misrouted"


def test_timestamp_decimal_longo_retenta_e_quarentena_como_conteudo(
    ingestor, conn
):
    bucket = Bucket()
    doc = envelope("FINDING", [finding_vm(finding_id="timestamp-invalido")])
    doc["first_ts"] = "9" * 5_000
    doc["last_ts"] = doc["first_ts"]
    bucket.adicionar("FINDING", doc)
    bucket.fechar_manifest("FINDING")
    ing = ingestor(bucket.store)

    resultados = [ing.executar(modo="INCREMENTAL") for _ in range(3)]

    assert [r.payloads_falhos for r in resultados] == [1, 1, 0]
    assert [r.payloads_quarentena for r in resultados] == [0, 0, 1]
    with conn.cursor() as cur:
        cur.execute("SELECT status, attempt_count, error_message FROM ingest_file")
        linha = cur.fetchone()
    assert linha["status"] == "QUARANTINED"
    assert linha["attempt_count"] == 3
    assert linha["error_message"].startswith("ErroIntegridade:")
    assert estado(conn, "timestamp-invalido") is None


def test_processar_payload_nao_materializa_json(monkeypatch, ingestor, conn):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="f1")]))
    manifest_path = bucket.fechar_manifest("FINDING")
    manifest = parse_manifest(manifest_path, json.loads(bucket.store[manifest_path]))
    entrada = manifest.payloads[0]
    ing = ingestor(bucket.store)
    monkeypatch.setattr(
        json,
        "loads",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("payload materializado")
        ),
    )

    resultado = ing.processar_payload(
        entrada, manifest, TIPOS_PAYLOAD["FINDING"], "SEED"
    )

    assert resultado.status == "OK"
    assert estado(conn, "f1")["state"] == "OPEN"


def test_invariante_de_copy_e_falha_operacional_sem_ledger(
    monkeypatch, ingestor, conn
):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="f1")]))
    manifest_path = bucket.fechar_manifest("FINDING")
    manifest = parse_manifest(manifest_path, json.loads(bucket.store[manifest_path]))
    entrada = manifest.payloads[0]
    copiar_real = loader_mod._copiar

    def copiar_com_contagem_corrompida(cur, tabela, classe, linhas):
        total = copiar_real(cur, tabela, classe, linhas)
        return total - 1 if tabela == "stg_finding" else total

    monkeypatch.setattr(loader_mod, "_copiar", copiar_com_contagem_corrompida)

    with pytest.raises(RuntimeError, match="invariante.*COPY"):
        ingestor(bucket.store).processar_payload(
            entrada, manifest, TIPOS_PAYLOAD["FINDING"], "SEED"
        )

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM ingest_file WHERE path = %s", (entrada.path,))
        assert cur.fetchone() is None


def test_erro_de_programacao_nao_registra_retry_ou_quarentena(ingestor, conn, monkeypatch):
    """Só conteúdo inválido pode avançar o ledger de tentativas do arquivo."""
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="f1")]))
    manifest_path = bucket.fechar_manifest("FINDING")
    manifest = parse_manifest(manifest_path, json.loads(bucket.store[manifest_path]))
    entrada = manifest.payloads[0]
    ing = ingestor(bucket.store)

    def falhar_no_banco(*_args):
        raise RuntimeError("sql bug")

    monkeypatch.setattr(ing, "_aplicar", falhar_no_banco)

    with pytest.raises(RuntimeError, match="sql bug"):
        ing.processar_payload(entrada, manifest, TIPOS_PAYLOAD["FINDING"], "SEED")

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM ingest_file WHERE path = %s", (entrada.path,))
        assert cur.fetchone() is None


def test_erro_ao_persistir_retry_de_conteudo_aborta_job(ingestor, conn, monkeypatch, caplog):
    """Falha do banco ao marcar retry não pode ser convertida em FAILED."""
    bucket = Bucket()
    bucket.adicionar(
        "FINDING",
        envelope("FINDING", [finding_vm(finding_id="f1")]),
        md5_forcado="md5-invalido",
    )
    manifest_path = bucket.fechar_manifest("FINDING")
    manifest = parse_manifest(manifest_path, json.loads(bucket.store[manifest_path]))
    entrada = manifest.payloads[0]
    sql_original = loader_mod.carregar_sql

    def sql_com_falha(nome):
        return "SELECT 1 / 0" if nome == "61_mark_failure" else sql_original(nome)

    monkeypatch.setattr(loader_mod, "carregar_sql", sql_com_falha)

    with pytest.raises(psycopg.errors.DivisionByZero):
        ingestor(bucket.store).processar_payload(
            entrada, manifest, TIPOS_PAYLOAD["FINDING"], "SEED"
        )

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM ingest_file WHERE path = %s", (entrada.path,))
        assert cur.fetchone() is None
    assert any("não foi possível registrar a falha" in registro.message for registro in caplog.records)


def test_erro_operacional_no_download_do_manifest_aborta_ciclo(ingestor, monkeypatch, caplog):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="f1")]))
    bucket.fechar_manifest("FINDING")
    ing = ingestor(bucket.store)

    def indisponivel(_key):
        raise RuntimeError("permissão negada")

    monkeypatch.setattr(ing.cliente, "baixar", indisponivel)

    with pytest.raises(RuntimeError, match="permissão negada"):
        ing.executar(modo="INCREMENTAL")

    assert any("manifest ilegível" in registro.message for registro in caplog.records)


def test_manifest_malformado_interrompe_antes_do_manifest_posterior(ingestor, conn, caplog):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="primeiro")]))
    manifest_malformado = bucket.fechar_manifest("FINDING")
    path_posterior = bucket.adicionar(
        "FINDING", envelope("FINDING", [finding_vm(finding_id="posterior")])
    )
    bucket.fechar_manifest("FINDING")
    bucket.store[manifest_malformado] = b"nao-e-json"

    with pytest.raises(ErroParse, match="JSON inválido"):
        ingestor(bucket.store).executar(modo="INCREMENTAL")

    assert estado(conn, "posterior") is None
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM ingest_file WHERE path = %s", (path_posterior,))
        assert cur.fetchone() is None
    assert any("manifest ilegível" in registro.message for registro in caplog.records)


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("type", "MANIFEST_WAS_FINDING"),
        ("payload_type", "WAS_FINDING"),
    ],
)
def test_manifest_de_outro_stream_e_rejeitado_sem_processar_payload(
    ingestor, conn, campo, valor
):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="f1")]))
    manifest_path = bucket.fechar_manifest("FINDING")
    doc = json.loads(bucket.store[manifest_path])
    doc[campo] = valor
    bucket.store[manifest_path] = json.dumps(doc).encode("utf-8")

    with pytest.raises(ErroIntegridade, match=campo):
        ingestor(bucket.store).executar(modo="INCREMENTAL")

    assert estado(conn, "f1") is None


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
    a transição FIXED -> REOPENED desaparece. Com indexed empatado, seq precisa
    preservar a ordem observada.
    """
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", state="OPEN", indexed="2026-08-27T10:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    bucket.adicionar("FINDING", envelope("FINDING", [
        finding_vm(
            finding_id="f1", state="FIXED",
            last_fixed="2026-08-28T11:00:00Z",
            indexed="2026-08-28T12:05:00Z",
        ),
        finding_vm(
            finding_id="f1", state="REOPENED",
            resurfaced_date="2026-08-28T12:00:00Z",
            indexed="2026-08-28T12:05:00Z",
        ),
    ]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    registros = eventos(conn, "f1")
    assert [e["event_type"] for e in registros][-2:] == ["FIXED", "REOPENED"]
    assert [(e["old_state"], e["new_state"]) for e in registros][-2:] == [
        ("OPEN", "FIXED"),
        ("FIXED", "REOPENED"),
    ]
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


def test_delete_posterior_avanca_relogio_sem_novo_evento_ou_ressurreicao(ingestor, conn):
    """Novo tombstone confirma a versão, mas não repete a transição lógica."""
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", state="OPEN", indexed="2026-08-20T10:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    primeiro_delete = "2026-08-21T12:00:00Z"
    bucket.adicionar(
        "FINDING",
        envelope("FINDING", [], [{"_id": "f1", "deleted_at": primeiro_delete}]),
    )
    ciclo(ingestor, bucket, "INCREMENTAL")

    segundo_delete = dt.datetime(2026, 8, 23, 12, tzinfo=dt.timezone.utc)
    bucket.adicionar(
        "FINDING",
        envelope(
            "FINDING",
            [],
            [{"_id": "f1", "deleted_at": segundo_delete.isoformat()}],
        ),
    )
    ciclo(ingestor, bucket, "INCREMENTAL")

    confirmado = estado(conn, "f1")
    assert tipos_de_evento(conn, "f1").count("DELETED") == 1
    assert confirmado["state"] == "OPEN"
    assert confirmado["deleted_at"] == segundo_delete
    assert confirmado["indexed"] == segundo_delete

    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(
        finding_id="f1", state="REOPENED", indexed="2026-08-22T12:00:00Z")]))
    ciclo(ingestor, bucket, "INCREMENTAL")

    depois_do_atrasado = estado(conn, "f1")
    assert tipos_de_evento(conn, "f1").count("DELETED") == 1
    assert depois_do_atrasado["state"] == "OPEN"
    assert depois_do_atrasado["deleted_at"] == segundo_delete
    assert depois_do_atrasado["indexed"] == segundo_delete


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
# Relogios monotônicos de plugin e recast
# ---------------------------------------------------------------------------
def test_plugin_rejeita_antigo_e_empate_mas_aceita_estritamente_novo(ingestor, conn):
    bucket = Bucket()
    adicionar_plugin(
        bucket, indexed="2026-08-27T12:00:00Z", solution="solucao atual"
    )
    ciclo(ingestor, bucket, "SEED")
    inicial = plugin(conn)

    adicionar_plugin(
        bucket, indexed="2026-08-27T11:00:00Z", solution="solucao antiga"
    )
    ciclo(ingestor, bucket, "SEED")
    apos_antigo = plugin(conn)

    adicionar_plugin(
        bucket, indexed="2026-08-27T12:00:00Z", solution="solucao empatada"
    )
    ciclo(ingestor, bucket, "SEED")
    apos_empate = plugin(conn)

    assert apos_antigo["solution"] == "solucao atual"
    assert apos_empate["solution"] == "solucao atual"
    assert apos_empate["source_indexed"] == dt.datetime(
        2026, 8, 27, 12, 0, tzinfo=dt.timezone.utc
    )
    assert apos_antigo["updated_at"] == inicial["updated_at"]
    assert apos_empate["updated_at"] == inicial["updated_at"]

    adicionar_plugin(
        bucket, indexed="2026-08-27T13:00:00Z", solution="solucao nova"
    )
    ciclo(ingestor, bucket, "SEED")
    apos_novo = plugin(conn)

    assert apos_novo["solution"] == "solucao nova"
    assert apos_novo["source_indexed"] == dt.datetime(
        2026, 8, 27, 13, 0, tzinfo=dt.timezone.utc
    )
    assert apos_novo["updated_at"] != inicial["updated_at"]


def test_recast_rejeita_update_antigo_e_empate_sem_mover_ingested_at(ingestor, conn):
    bucket = Bucket()
    adicionar_recast(
        bucket, updated_at="2026-08-27T12:00:00Z", rule_comment="regra atual"
    )
    ciclo(ingestor, bucket, "SEED", tipos=("FINDING_ENRICHED_ATTRIBUTES",))
    inicial = recast(conn, "recast-clock")

    adicionar_recast(
        bucket, updated_at="2026-08-27T11:00:00Z", rule_comment="regra antiga"
    )
    ciclo(ingestor, bucket, "SEED", tipos=("FINDING_ENRICHED_ATTRIBUTES",))
    adicionar_recast(
        bucket, updated_at="2026-08-27T12:00:00Z", rule_comment="regra empatada"
    )
    ciclo(ingestor, bucket, "SEED", tipos=("FINDING_ENRICHED_ATTRIBUTES",))
    final = recast(conn, "recast-clock")

    assert final["rule_comment"] == "regra atual"
    assert final["source_indexed"] == dt.datetime(
        2026, 8, 27, 12, 0, tzinfo=dt.timezone.utc
    )
    assert final["ingested_at"] == inicial["ingested_at"]


def test_recast_delete_antigo_nao_apaga_mas_delete_novo_apaga(ingestor, conn):
    bucket = Bucket()
    adicionar_recast(
        bucket, updated_at="2026-08-27T12:00:00Z", rule_comment="regra atual"
    )
    ciclo(ingestor, bucket, "SEED", tipos=("FINDING_ENRICHED_ATTRIBUTES",))

    adicionar_delete_recast(bucket, deleted_at="2026-08-27T11:00:00Z")
    ciclo(ingestor, bucket, "SEED", tipos=("FINDING_ENRICHED_ATTRIBUTES",))
    apos_antigo = recast(conn, "recast-clock")

    assert apos_antigo["deleted_at"] is None
    assert apos_antigo["rule_comment"] == "regra atual"

    adicionar_delete_recast(bucket, deleted_at="2026-08-27T13:00:00Z")
    ciclo(ingestor, bucket, "SEED", tipos=("FINDING_ENRICHED_ATTRIBUTES",))
    apos_novo = recast(conn, "recast-clock")

    esperado = dt.datetime(2026, 8, 27, 13, 0, tzinfo=dt.timezone.utc)
    assert apos_novo["deleted_at"] == esperado
    assert apos_novo["source_indexed"] == esperado
    assert apos_novo["rule_comment"] == "regra atual"


def test_recast_tombstone_so_ressuscita_com_update_estritamente_novo(ingestor, conn):
    bucket = Bucket()
    adicionar_recast(
        bucket, updated_at="2026-08-27T12:00:00Z", rule_comment="regra inicial"
    )
    ciclo(ingestor, bucket, "SEED", tipos=("FINDING_ENRICHED_ATTRIBUTES",))
    adicionar_delete_recast(bucket, deleted_at="2026-08-27T13:00:00Z")
    ciclo(ingestor, bucket, "SEED", tipos=("FINDING_ENRICHED_ATTRIBUTES",))
    tombstone = recast(conn, "recast-clock")

    adicionar_recast(
        bucket, updated_at="2026-08-27T13:00:00Z", rule_comment="empate"
    )
    ciclo(ingestor, bucket, "SEED", tipos=("FINDING_ENRICHED_ATTRIBUTES",))
    empatado = recast(conn, "recast-clock")

    assert empatado["deleted_at"] == tombstone["deleted_at"]
    assert empatado["rule_comment"] == "regra inicial"
    assert empatado["ingested_at"] == tombstone["ingested_at"]

    adicionar_recast(
        bucket, updated_at="2026-08-27T14:00:00Z", rule_comment="ressuscitada"
    )
    ciclo(ingestor, bucket, "SEED", tipos=("FINDING_ENRICHED_ATTRIBUTES",))
    ressuscitado = recast(conn, "recast-clock")

    assert ressuscitado["deleted_at"] is None
    assert ressuscitado["rule_comment"] == "ressuscitada"
    assert ressuscitado["source_indexed"] == dt.datetime(
        2026, 8, 27, 14, 0, tzinfo=dt.timezone.utc
    )


def test_replay_do_mesmo_delete_recast_preserva_tombstone_e_ingested_at(ingestor, conn):
    bucket = Bucket()
    adicionar_recast(
        bucket, updated_at="2026-08-27T12:00:00Z", rule_comment="regra inicial"
    )
    ciclo(ingestor, bucket, "SEED", tipos=("FINDING_ENRICHED_ATTRIBUTES",))
    adicionar_delete_recast(bucket, deleted_at="2026-08-27T13:00:00Z")
    ciclo(ingestor, bucket, "SEED", tipos=("FINDING_ENRICHED_ATTRIBUTES",))
    primeiro = recast(conn, "recast-clock")

    adicionar_delete_recast(bucket, deleted_at="2026-08-27T13:00:00Z")
    ciclo(ingestor, bucket, "SEED", tipos=("FINDING_ENRICHED_ATTRIBUTES",))
    replay = recast(conn, "recast-clock")

    esperado = dt.datetime(2026, 8, 27, 13, 0, tzinfo=dt.timezone.utc)
    assert replay["deleted_at"] == esperado
    assert replay["source_indexed"] == esperado
    assert replay["ingested_at"] == primeiro["ingested_at"]


def test_delete_enriched_desconhecido_nao_inventa_tombstone(ingestor, conn):
    bucket = Bucket()
    adicionar_delete_recast(
        bucket, finding_id="atributo-sem-vinculo", deleted_at="2026-08-27T13:00:00Z"
    )
    resultado = ciclo(
        ingestor, bucket, "SEED", tipos=("FINDING_ENRICHED_ATTRIBUTES",)
    )

    assert resultado.payloads_ok == 1
    assert recast(conn, "atributo-sem-vinculo") is None


class _RollbackMigrationTest(Exception):
    pass


def test_migracao_backfill_recast_sem_inventar_relogio_de_plugin(conn):
    """Executa a migracao sobre linhas no formato legado e desfaz ao final."""
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("ALTER TABLE plugin DROP COLUMN IF EXISTS source_indexed")
            cur.execute("ALTER TABLE finding_recast DROP COLUMN IF EXISTS source_indexed")
            cur.execute("INSERT INTO plugin (plugin_id, raw) VALUES (999, '{}'::jsonb)")
            cur.execute(
                "INSERT INTO finding_recast "
                "(finding_id, rule_created_at, rule_updated_at, deleted_at, raw) VALUES "
                "('por-delete', '2026-08-27T10:00:00Z', '2026-08-27T11:00:00Z', "
                " '2026-08-27T12:00:00Z', '{}'::jsonb), "
                "('por-update', '2026-08-27T10:00:00Z', '2026-08-27T11:00:00Z', "
                " NULL, '{}'::jsonb), "
                "('por-create', '2026-08-27T10:00:00Z', NULL, NULL, '{}'::jsonb), "
                "('sem-relogio', NULL, NULL, NULL, '{}'::jsonb)"
            )
            sql = (
                RAIZ / "migrations" / "sql" / "0003_source_clocks.sql"
            ).read_text(encoding="utf-8")
            cur.execute(sql)

            cur.execute("SELECT source_indexed FROM plugin WHERE plugin_id = 999")
            assert cur.fetchone()["source_indexed"] is None
            cur.execute(
                "SELECT finding_id, source_indexed FROM finding_recast ORDER BY finding_id"
            )
            relogios = {row["finding_id"]: row["source_indexed"] for row in cur.fetchall()}
            assert relogios == {
                "por-create": dt.datetime(2026, 8, 27, 10, 0, tzinfo=dt.timezone.utc),
                "por-delete": dt.datetime(2026, 8, 27, 12, 0, tzinfo=dt.timezone.utc),
                "por-update": dt.datetime(2026, 8, 27, 11, 0, tzinfo=dt.timezone.utc),
                "sem-relogio": None,
            }
            raise _RollbackMigrationTest
    except _RollbackMigrationTest:
        pass


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


def test_manifest_totalmente_ok_soma_skips_e_mantem_limite_para_posterior(ingestor, conn, caplog):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="f1")]))
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="f2")]))
    bucket.fechar_manifest("FINDING")
    ing = ingestor(bucket.store)

    primeiro = ing.executar(modo="INCREMENTAL")
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="f3")]))
    bucket.fechar_manifest("FINDING")
    caplog.set_level("WARNING", logger="ingestion.loader")
    segundo = ing.executar(modo="INCREMENTAL", limite=1)

    assert primeiro.payloads_ok == 2
    assert segundo.payloads_ok == 1
    assert segundo.payloads_pulados == 2
    assert estado(conn, "f3") is not None
    assert any("já totalmente processado" in registro.message for registro in caplog.records)
