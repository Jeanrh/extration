from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.banco


def _popular(conn):
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO plugin (plugin_id, raw) VALUES (10, '{}'::jsonb)"
        )
        cur.execute(
            "INSERT INTO finding_current "
            "(finding_id, product, state, plugin_id, indexed, natural_key, raw) VALUES "
            "('vm-1', 'VM', 'OPEN', 10, now(), 'duplicada', '{}'::jsonb),"
            "('vm-2', 'VM', 'REOPENED', 99, now(), 'duplicada', '{}'::jsonb),"
            "('was-1', 'WAS', 'OPEN', NULL, now(), 'was-key', '{}'::jsonb),"
            "('fixed', 'VM', 'FIXED', 10, now(), 'fixed-key', '{}'::jsonb)"
        )
        cur.execute(
            "INSERT INTO ingest_file "
            "(path, payload_type, manifest_path, status, attempt_count, "
            " error_message, mode, processed_at) VALUES "
            "('payload/b.json', 'WAS_FINDING', 'manifest/b.json', 'QUARANTINED', 3, "
            " 'schema inválido', 'SEED', '2026-08-27T12:00:00Z'),"
            "('payload/a.json', 'FINDING', 'manifest/a.json', 'QUARANTINED', 4, "
            " 'md5 inválido', 'SEED', '2026-08-28T12:00:00Z')"
        )


def test_relatorio_sem_console_e_honesto_e_deterministico(conn):
    from ingestion.reconcile import gerar_relatorio

    _popular(conn)
    agora = dt.datetime(2026, 8, 28, 15, 30, tzinfo=dt.timezone.utc)

    assert gerar_relatorio(conn, agora=agora) == {
        "generated_at": "2026-08-28T15:30:00+00:00",
        "database": {
            "open_findings": {"VM": 2, "WAS": 1, "total": 3},
            "duplicate_natural_keys": [
                {
                    "natural_key": "duplicada",
                    "ids_distintos": 2,
                    "finding_ids": ["vm-1", "vm-2"],
                }
            ],
            "findings_without_plugin": 2,
            "quarantine": {
                "count": 2,
                "files": [
                    {
                        "path": "payload/a.json",
                        "payload_type": "FINDING",
                        "attempt_count": 4,
                        "error_message": "md5 inválido",
                        "processed_at": "2026-08-28T12:00:00+00:00",
                    },
                    {
                        "path": "payload/b.json",
                        "payload_type": "WAS_FINDING",
                        "attempt_count": 3,
                        "error_message": "schema inválido",
                        "processed_at": "2026-08-27T12:00:00+00:00",
                    },
                ],
            },
        },
        "console_comparison": "NOT_PROVIDED",
    }


def test_comparacao_com_console_completa_usa_delta_sem_ambiguidade(conn):
    from ingestion.reconcile import gerar_relatorio

    _popular(conn)
    relatorio = gerar_relatorio(conn, console_vm_open=5, console_was_open=1)

    assert relatorio["console_comparison"] == {
        "VM": {"database": 2, "console": 5, "database_minus_console": -3},
        "WAS": {"database": 1, "console": 1, "database_minus_console": 0},
    }


def test_comparacao_parcial_marca_produto_ausente(conn):
    from ingestion.reconcile import gerar_relatorio

    _popular(conn)
    relatorio = gerar_relatorio(conn, console_vm_open=1)

    assert relatorio["console_comparison"] == {
        "VM": {"database": 2, "console": 1, "database_minus_console": 1},
        "WAS": "NOT_PROVIDED",
    }


def test_contagem_negativa_e_rejeitada_antes_de_consultar(conn):
    from ingestion.reconcile import gerar_relatorio

    with pytest.raises(ValueError, match="não pode ser negativa"):
        gerar_relatorio(conn, console_was_open=-1)


class _CursorIntercalado:
    def __init__(self, real, inserir):
        self._real = real
        self._inserir = inserir
        self._inseriu = False

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, *args):
        return self._real.__exit__(*args)

    def execute(self, query, params=None):
        resultado = self._real.execute(query, params)
        if not self._inseriu and "count(*) FILTER" in str(query):
            self._inseriu = True
            self._inserir()
        return resultado

    def __getattr__(self, nome):
        return getattr(self._real, nome)


class _ConexaoIntercalada:
    def __init__(self, real, inserir):
        self._real = real
        self._inserir = inserir

    def transaction(self):
        return self._real.transaction()

    def cursor(self):
        return _CursorIntercalado(self._real.cursor(), self._inserir)


def test_todas_as_contagens_usam_um_snapshot_coerente(conn, config_teste):
    from ingestion.db import conectar
    from ingestion.reconcile import gerar_relatorio

    def inserir_concorrente():
        with conectar(config_teste.pg_dsn) as outra:
            with outra.transaction(), outra.cursor() as cur:
                cur.execute(
                    "INSERT INTO finding_current "
                    "(finding_id, product, state, plugin_id, indexed, natural_key, raw) "
                    "VALUES ('depois-snapshot', 'VM', 'OPEN', 777, now(), "
                    "'depois-snapshot', '{}'::jsonb)"
                )

    relatorio = gerar_relatorio(_ConexaoIntercalada(conn, inserir_concorrente))

    assert relatorio["database"]["open_findings"] == {"VM": 0, "WAS": 0, "total": 0}
    assert relatorio["database"]["findings_without_plugin"] == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS total FROM finding_current")
        assert cur.fetchone()["total"] == 1


def test_json_em_arquivo_e_stdout_e_deterministico_e_escrita_e_atomica(
    tmp_path, monkeypatch
):
    from ingestion import reconcile

    relatorio = {"z": 1, "a": {"ç": 2}}
    esperado = '{\n  "a": {\n    "ç": 2\n  },\n  "z": 1\n}\n'
    destino = tmp_path / "reports" / "reconcile.json"

    reconcile.escrever_relatorio(relatorio, destino)
    assert destino.read_text(encoding="utf-8") == esperado

    stdout = io.StringIO()
    reconcile.escrever_relatorio(relatorio, "-", stdout=stdout)
    assert stdout.getvalue() == esperado

    destino.write_text("conteúdo anterior", encoding="utf-8")
    replace_real = reconcile.os.replace

    def falhar_replace(origem, alvo):
        assert Path(origem).parent == destino.parent
        assert Path(alvo) == destino
        raise OSError("falha simulada antes da troca atômica")

    monkeypatch.setattr(reconcile.os, "replace", falhar_replace)
    with pytest.raises(OSError, match="falha simulada"):
        reconcile.escrever_relatorio(relatorio, destino)
    assert destino.read_text(encoding="utf-8") == "conteúdo anterior"
    assert sorted(p.name for p in destino.parent.iterdir()) == [destino.name]
    monkeypatch.setattr(reconcile.os, "replace", replace_real)


def test_cli_rejeita_contagens_negativas():
    from ingestion.cli import montar_parser

    with pytest.raises(SystemExit) as saida:
        montar_parser().parse_args(
            ["reconcile", "--output", "-", "--console-vm-open", "-1"]
        )
    assert saida.value.code == 2
