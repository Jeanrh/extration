from __future__ import annotations

import argparse
from contextlib import contextmanager
from types import SimpleNamespace

from ingestion import cli, metrics
from ingestion.config import Config


class _Contexto:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_run_publica_duracao_do_job_inteiro(monkeypatch):
    config = Config(bucket="b", prefixo="", pg_dsn="dsn")
    resultado = SimpleNamespace(
        payloads_falhos=0,
        erros_manifest=[],
        modo="SEED",
        manifests_lidos=0,
        payloads_ok=0,
        payloads_pulados=0,
        payloads_quarentena=0,
        registros=0,
        eventos=0,
        duracao_segundos=1.0,
        horas_desde_ultimo_manifest=2.0,
    )
    publicado = []

    class _Ingestor:
        def __init__(self, *args):
            pass

        def executar(self, **kwargs):
            return resultado

    class _Publicador:
        def __init__(self, config):
            pass

        def publicar(self, metricas_recebidas):
            publicado.extend(metricas_recebidas)

    @contextmanager
    def _lock(_conn):
        yield True

    monkeypatch.setattr(cli, "conectar", lambda _dsn: _Contexto())
    monkeypatch.setattr(cli, "travar_pipeline", _lock)
    monkeypatch.setattr(cli, "ClienteS3", lambda _config: object())
    monkeypatch.setattr(cli, "Ingestor", _Ingestor)
    monkeypatch.setattr(cli.partitions, "garantir_particoes", lambda conn: [])
    monkeypatch.setattr(cli.partitions, "expurgar_particoes", lambda conn, meses: [])
    monkeypatch.setattr(cli.partitions, "expurgar_ingest_file", lambda conn, dias: 0)
    monkeypatch.setattr(cli.partitions, "contar_default", lambda conn: 0)
    monkeypatch.setattr(
        cli.metrics,
        "capturar_estado",
        lambda conn: metrics.EstadoMetricas(0, 0, 0.0),
    )
    monkeypatch.setattr(cli.metrics, "Publicador", _Publicador)
    coletar_real = cli.metrics.coletar
    metricas_preparadas = False

    def _coletar(*args, **kwargs):
        nonlocal metricas_preparadas
        metricas_preparadas = True
        return coletar_real(*args, **kwargs)

    monkeypatch.setattr(cli.metrics, "coletar", _coletar)
    instantes = iter((100.0, 115.5))

    def _monotonic():
        instante = next(instantes)
        if instante == 115.5:
            assert metricas_preparadas
        return instante

    monkeypatch.setattr(cli.time, "monotonic", _monotonic)

    saida = cli.cmd_run(config, argparse.Namespace(seed=False, mode=None, limit=None))

    assert saida == 0
    assert next(m.valor for m in publicado if m.nome == "JobDurationSeconds") == 15.5


def test_reconcile_ocupado_falha_visivelmente(monkeypatch, caplog):
    config = Config(bucket="b", prefixo="", pg_dsn="dsn")

    @contextmanager
    def _lock(_conn):
        yield False

    monkeypatch.setattr(cli, "conectar", lambda _dsn: _Contexto())
    monkeypatch.setattr(cli, "travar_pipeline", _lock)

    saida = cli.cmd_reconcile(
        config,
        argparse.Namespace(output="-", console_vm_open=None, console_was_open=None),
    )

    assert saida != 0
    assert "lock" in caplog.text


def test_config_rejeita_retencao_de_arquivo_invalida(monkeypatch):
    from ingestion.config import carregar_config
    from ingestion.erros import ErroConfiguracao

    monkeypatch.setenv("TENABLE_BUCKET", "bucket")
    monkeypatch.setenv("PG_DSN", "dsn")
    monkeypatch.setenv("INGEST_FILE_RETENTION_DAYS", "0")

    try:
        carregar_config()
    except ErroConfiguracao as erro:
        assert "INGEST_FILE_RETENTION_DAYS deve ser >= 1" in str(erro)
    else:
        raise AssertionError("retenção inválida foi aceita")
