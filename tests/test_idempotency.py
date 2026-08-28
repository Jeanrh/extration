"""Teste de idempotência obrigatório (SPEC seção 10.1).

    1. Rodar ingestão completa sobre um conjunto de payloads
    2. Registrar count + checksum completo de current, event, plugin e recast
    3. Limpar ingest_file (apenas ela)
    4. Rodar a ingestão exatamente igual de novo
    5. Os oito valores DEVEM ser idênticos

Precisa de PostgreSQL (`TEST_PG_DSN`); sem ela é pulado.
"""

from __future__ import annotations

import json

import pytest

from fixtures import Bucket, envelope, enriched, finding_vm, finding_was
from ingestion.config import TIPOS_PAYLOAD
from ingestion.manifest import parse_manifest

pytestmark = pytest.mark.banco


def _cenario() -> Bucket:
    """Um bucket com as quatro situações que mexem em estado: abertura,
    fechamento, reabertura, exclusão — mais WAS e recast."""
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [
        finding_vm(finding_id="abre", indexed="2026-08-20T10:00:00Z"),
        finding_vm(finding_id="fecha", indexed="2026-08-20T10:00:00Z"),
        finding_vm(finding_id="some", indexed="2026-08-20T10:00:00Z"),
        finding_vm(finding_id="reabre", state="FIXED",
                   last_fixed="2026-08-19T00:00:00Z", indexed="2026-08-20T10:00:00Z"),
    ]))
    bucket.adicionar("FINDING", envelope("FINDING", [
        finding_vm(finding_id="fecha", state="FIXED",
                   last_fixed="2026-08-21T09:00:00Z", indexed="2026-08-21T10:00:00Z"),
        finding_vm(finding_id="reabre", state="REOPENED",
                   resurfaced_date="2026-08-21T08:00:00Z", indexed="2026-08-21T10:00:00Z"),
    ], [{"_id": "some", "deleted_at": "2026-08-21T11:00:00Z"}]))
    bucket.adicionar("WAS_FINDING", envelope("WAS_FINDING", [
        finding_was(finding_id="was1", indexed_at="2026-08-21T12:00:00Z"),
    ]))
    bucket.adicionar(
        "FINDING_ENRICHED_ATTRIBUTES",
        envelope("FINDING_ENRICHED_ATTRIBUTES", [enriched(finding_id="abre")]),
    )
    for tipo in ("FINDING", "WAS_FINDING", "FINDING_ENRICHED_ATTRIBUTES"):
        bucket.fechar_manifest(tipo)
    return bucket


def _instantaneo(conn) -> dict:
    """Contagem + checksum canônico da linha completa nas quatro estruturas."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS total, "
            "       md5(coalesce(string_agg(t::text, '|' ORDER BY t.finding_id), '')) AS h "
            "FROM finding_current t"
        )
        current = cur.fetchone()
        cur.execute(
            "SELECT count(*) AS total, "
            "       md5(coalesce(string_agg(e::text, '|' "
            "           ORDER BY e.finding_id, e.event_type, e.occurred_at, e.id), '')) AS h "
            "FROM finding_event e"
        )
        event = cur.fetchone()
        cur.execute(
            "SELECT count(*) AS total, "
            "       md5(coalesce(string_agg(p::text, '|' ORDER BY p.plugin_id), '')) AS h "
            "FROM plugin p"
        )
        plugin = cur.fetchone()
        cur.execute(
            "SELECT count(*) AS total, "
            "       md5(coalesce(string_agg(r::text, '|' ORDER BY r.finding_id), '')) AS h "
            "FROM finding_recast r"
        )
        recast = cur.fetchone()
    return {
        "finding_current_count": current["total"],
        "finding_current_hash": current["h"],
        "finding_event_count": event["total"],
        "finding_event_hash": event["h"],
        "plugin_count": plugin["total"],
        "plugin_hash": plugin["h"],
        "finding_recast_count": recast["total"],
        "finding_recast_hash": recast["h"],
    }


@pytest.mark.parametrize("modo", ["SEED", "INCREMENTAL"])
def test_reprocessar_tudo_produz_estado_identico(ingestor, conn, modo):
    bucket = _cenario()

    ingestor(bucket.store).executar(modo=modo)
    antes = _instantaneo(conn)
    assert antes["finding_current_count"] == 5
    assert antes["finding_event_count"] == (9 if modo == "INCREMENTAL" else 0)
    assert antes["plugin_count"] == 2
    assert antes["finding_recast_count"] == 1
    for chave in (
        "finding_current_hash",
        "finding_event_hash",
        "plugin_hash",
        "finding_recast_hash",
    ):
        assert len(antes[chave]) == 32

    # limpa APENAS ingest_file: some a camada 1 (idempotência de arquivo) e a
    # garantia passa a depender das camadas 3 e 4 — a guarda `>` estrita no
    # upsert e o índice único de evento.
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM ingest_file")

    ingestor(bucket.store).executar(modo=modo)
    depois = _instantaneo(conn)

    assert depois == antes


def test_instantaneo_detecta_mudanca_na_linha_completa_das_quatro_estruturas(
    ingestor, conn
):
    bucket = _cenario()
    ingestor(bucket.store).executar(modo="INCREMENTAL")
    antes = _instantaneo(conn)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE finding_current SET last_ingested_at = last_ingested_at + interval '1 second' "
            "WHERE finding_id = (SELECT min(finding_id) FROM finding_current)"
        )
        cur.execute(
            "UPDATE finding_event SET detected_at = detected_at + interval '1 second' "
            "WHERE id = (SELECT min(id) FROM finding_event)"
        )
        cur.execute(
            "UPDATE plugin SET updated_at = updated_at + interval '1 second' "
            "WHERE plugin_id = (SELECT min(plugin_id) FROM plugin)"
        )
        cur.execute(
            "UPDATE finding_recast SET ingested_at = ingested_at + interval '1 second' "
            "WHERE finding_id = (SELECT min(finding_id) FROM finding_recast)"
        )

    depois = _instantaneo(conn)
    for estrutura in (
        "finding_current",
        "finding_event",
        "plugin",
        "finding_recast",
    ):
        assert depois[f"{estrutura}_count"] == antes[f"{estrutura}_count"]
        assert depois[f"{estrutura}_hash"] != antes[f"{estrutura}_hash"]


def test_terceira_passada_tambem_nao_move_nada(ingestor, conn):
    bucket = _cenario()
    ingestor(bucket.store).executar(modo="INCREMENTAL")
    referencia = _instantaneo(conn)

    for _ in range(2):
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("DELETE FROM ingest_file")
        ingestor(bucket.store).executar(modo="INCREMENTAL")
        assert _instantaneo(conn) == referencia


def test_nenhum_finding_id_duplicado(ingestor, conn):
    """Critério de aceite: nenhum finding_id duplicado em finding_current.

    A PK já garante; o teste existe para o critério ficar verificado por código
    e não por confiança no DDL."""
    bucket = _cenario()
    ingestor(bucket.store).executar(modo="INCREMENTAL")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS total FROM ("
            "  SELECT finding_id FROM finding_current GROUP BY 1 HAVING count(*) > 1"
            ") d"
        )
        assert cur.fetchone()["total"] == 0


def test_todo_evento_tem_occurred_at_vindo_do_dado(ingestor, conn):
    """Critério de aceite: occurred_at nunca é o relógio do job.

    Os payloads do cenário são datados de agosto/2026 e antes; nenhum evento
    pode estar datado com o `now()` da execução."""
    bucket = _cenario()
    ingestor(bucket.store).executar(modo="INCREMENTAL")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS total FROM finding_event "
            "WHERE occurred_at > now() - interval '1 minute'"
        )
        assert cur.fetchone()["total"] == 0
        cur.execute("SELECT count(*) AS total FROM finding_event WHERE detected_at IS NULL")
        assert cur.fetchone()["total"] == 0


def test_reprocess_manual_de_um_payload_e_idempotente(ingestor, conn):
    bucket = _cenario()
    ing = ingestor(bucket.store)
    ing.executar(modo="INCREMENTAL")
    antes = _instantaneo(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT path FROM ingest_file ORDER BY path LIMIT 1")
        caminho = cur.fetchone()["path"]

    resultado = ing.reprocessar(caminho)
    assert resultado.status == "OK"
    assert _instantaneo(conn) == antes


def test_reprocesso_preserva_modo_original_incremental(ingestor, conn):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="f1")]))
    bucket.fechar_manifest("FINDING")
    ing = ingestor(bucket.store)
    ing.executar(modo="INCREMENTAL")

    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT path FROM ingest_file")
        caminho = cur.fetchone()["path"]
        cur.execute("UPDATE pipeline_control SET mode = 'SEED' WHERE id = 1")

    resultado = ing.reprocessar(caminho)

    assert resultado.status == "OK"
    with conn.cursor() as cur:
        cur.execute("SELECT mode FROM ingest_file WHERE path = %s", (caminho,))
        assert cur.fetchone()["mode"] == "INCREMENTAL"


def test_payload_ok_e_pulado_transacionalmente_mas_forcar_reprocessa(ingestor, conn):
    bucket = Bucket()
    bucket.adicionar("FINDING", envelope("FINDING", [finding_vm(finding_id="f1")]))
    manifest_path = bucket.fechar_manifest("FINDING")
    manifest = parse_manifest(manifest_path, json.loads(bucket.store[manifest_path]))
    entrada = manifest.payloads[0]
    ing = ingestor(bucket.store)
    tipo = TIPOS_PAYLOAD["FINDING"]

    assert ing.processar_payload(entrada, manifest, tipo, "SEED").status == "OK"

    original_baixar = ing.cliente.baixar

    def download_indevido(_path):
        raise AssertionError("payload OK não deve ser baixado sem forçar")

    ing.cliente.baixar = download_indevido
    assert ing.processar_payload(entrada, manifest, tipo, "SEED").status == "SKIPPED"
    ing.cliente.baixar = original_baixar

    assert ing.processar_payload(entrada, manifest, tipo, "SEED", forcar=True).status == "OK"
    with conn.cursor() as cur:
        cur.execute("SELECT attempt_count FROM ingest_file WHERE path = %s", (entrada.path,))
        assert cur.fetchone()["attempt_count"] == 2
