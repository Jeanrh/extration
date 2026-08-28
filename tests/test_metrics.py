from __future__ import annotations

import datetime as dt
import logging
from types import SimpleNamespace

import pytest

from ingestion import metrics
from ingestion.config import Config


def _resultado(horas=1.234567):
    return SimpleNamespace(
        payloads_ok=2,
        registros=17,
        eventos=3,
        horas_desde_ultimo_manifest=horas,
    )


@pytest.mark.parametrize(
    ("atual", "anterior", "esperado"),
    [
        (120, 100, 20.0),
        (80, 100, 20.0),
        (0, None, 0.0),
        (0, 0, 0.0),
        (7, 0, 100.0),
    ],
)
def test_variacao_percentual_absoluta(atual, anterior, esperado):
    assert metrics.variacao_percentual(atual, anterior) == esperado


def test_metricas_tem_nomes_unidades_e_valores_exatos_sem_arredondar():
    estado = metrics.EstadoMetricas(
        quarentena=4, findings_open=80, change_percent=20.0
    )

    coletadas = metrics.coletar(_resultado(), estado, duracao_segundos=9.876543)

    assert [(m.nome, m.valor, m.unidade) for m in coletadas] == [
        ("HoursSinceLastManifest", 1.234567, "Count"),
        ("PayloadsProcessed", 2, "Count"),
        ("RecordsIngested", 17, "Count"),
        ("EventsGenerated", 3, "Count"),
        ("FilesQuarantined", 4, "Count"),
        ("FindingsOpen", 80, "Count"),
        ("JobDurationSeconds", 9.876543, "Seconds"),
        ("FindingsOpenChangePercent", 20.0, "Percent"),
    ]


def test_sem_manifest_omite_so_a_metrica_de_staleness_e_registra_erro(caplog):
    estado = metrics.EstadoMetricas(
        quarentena=0, findings_open=0, change_percent=0.0
    )

    with caplog.at_level(logging.ERROR):
        coletadas = metrics.coletar(
            _resultado(horas=None), estado, duracao_segundos=2.0
        )

    assert [m.nome for m in coletadas] == [
        "PayloadsProcessed",
        "RecordsIngested",
        "EventsGenerated",
        "FilesQuarantined",
        "FindingsOpen",
        "JobDurationSeconds",
        "FindingsOpenChangePercent",
    ]
    assert "nenhum manifest" in caplog.text


class _CloudWatch:
    def __init__(self, erro: Exception | None = None):
        self.erro = erro
        self.chamadas = []

    def put_metric_data(self, **kwargs):
        self.chamadas.append(kwargs)
        if self.erro:
            raise self.erro


def _config_cloudwatch():
    return Config(
        bucket="bucket",
        prefixo="",
        pg_dsn="postgresql://unused",
        cloudwatch_habilitado=True,
        cloudwatch_namespace="TenableIngestionTest",
    )


def test_publicacao_nao_adiciona_dimensions_e_serializa_o_baseline():
    cliente = _CloudWatch()
    publicador = metrics.Publicador(_config_cloudwatch(), cliente=cliente)
    estado = metrics.EstadoMetricas(
        quarentena=0, findings_open=80, change_percent=12.345678
    )
    percentual = next(
        metrica
        for metrica in metrics.coletar(
            _resultado(), estado, duracao_segundos=9.876543
        )
        if metrica.nome == "FindingsOpenChangePercent"
    )

    publicador.publicar([percentual])

    assert cliente.chamadas == [
        {
            "Namespace": "TenableIngestionTest",
            "MetricData": [
                {
                    "MetricName": "FindingsOpenChangePercent",
                    "Value": 12.345678,
                    "Unit": "Percent",
                }
            ],
        }
    ]


def test_falha_cloudwatch_e_tolerada_depois_da_serializacao(caplog):
    cliente = _CloudWatch(RuntimeError("cloudwatch indisponivel"))

    with caplog.at_level(logging.ERROR):
        metrics.Publicador(_config_cloudwatch(), cliente=cliente).publicar(
            [metrics.Metrica("FindingsOpen", 9)]
        )

    assert "falha ao publicar" in caplog.text


def test_erro_de_programacao_ao_montar_metric_data_propaga():
    cliente = _CloudWatch()

    with pytest.raises(AttributeError):
        metrics.Publicador(_config_cloudwatch(), cliente=cliente).publicar([object()])

    assert cliente.chamadas == []


pytestmark_banco = pytest.mark.banco


@pytest.mark.banco
def test_estado_le_e_atualiza_baseline_na_mesma_transacao(conn):
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO finding_current "
            "(finding_id, product, state, indexed, natural_key, raw) "
            "VALUES ('vm-1', 'VM', 'OPEN', now(), 'nk-1', '{}'::jsonb)"
        )

    primeiro = metrics.capturar_estado(conn)

    assert primeiro == metrics.EstadoMetricas(
        quarentena=0, findings_open=1, change_percent=0.0
    )
    with conn.cursor() as cur:
        cur.execute("SELECT last_findings_open FROM pipeline_control WHERE id = 1")
        assert cur.fetchone()["last_findings_open"] == 1

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO finding_current "
            "(finding_id, product, state, indexed, natural_key, raw) "
            "VALUES ('vm-2', 'VM', 'OPEN', now(), 'nk-2', '{}'::jsonb)"
        )

    segundo = metrics.capturar_estado(conn)
    assert segundo.change_percent == 100.0


@pytest.mark.banco
def test_falha_cloudwatch_nao_desfaz_baseline_ja_confirmado(conn, config_teste):
    estado = metrics.capturar_estado(conn)
    cliente = _CloudWatch(RuntimeError("AWS fora"))

    metrics.Publicador(
        Config(
            bucket=config_teste.bucket,
            prefixo=config_teste.prefixo,
            pg_dsn=config_teste.pg_dsn,
            cloudwatch_habilitado=True,
        ),
        cliente=cliente,
    ).publicar(metrics.coletar(_resultado(), estado, duracao_segundos=1.0))

    with conn.cursor() as cur:
        cur.execute("SELECT last_findings_open FROM pipeline_control WHERE id = 1")
        assert cur.fetchone()["last_findings_open"] == 0
