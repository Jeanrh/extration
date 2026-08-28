from __future__ import annotations

import datetime as dt
import functools
import hashlib
import json
from pathlib import Path

import pytest

from fixtures import FakeS3, comprimir, envelope
from ingestion import s3 as s3mod
from ingestion.config import Config, TIPOS_PAYLOAD
from ingestion.erros import ErroIntegridade, ErroParse
from ingestion.manifest import EntradaPayload
from ingestion.s3 import ClienteS3


EPOCH_MS = 1_787_826_739_356
INSTANTE = dt.datetime.fromtimestamp(EPOCH_MS / 1000, tz=dt.timezone.utc)


def _entrada(
    path: str,
    dados: bytes,
    *,
    updates: int = 1,
    deletes: int = 0,
    first: dt.datetime | None = INSTANTE,
    last: dt.datetime | None = INSTANTE,
) -> EntradaPayload:
    return EntradaPayload(
        path=path,
        md5=hashlib.md5(dados).hexdigest(),  # noqa: S324 - checksum do fornecedor
        version=1,
        num_updates=updates,
        num_deletes=deletes,
        first_record_timestamp=first,
        last_record_timestamp=last,
        scan_id="scan-stream",
    )


def _arquivo(tmp_path: Path, doc: dict) -> tuple[Path, bytes]:
    dados = comprimir(doc)
    path = tmp_path / "payload.json.gz"
    path.write_bytes(dados)
    return path, dados


def _finding(posicao: int = 0, *, score: float | None = None) -> dict:
    plugin: dict = {"id": 1000 + posicao, "name": f"plugin-{posicao}"}
    if score is not None:
        plugin["cvss3_base_score"] = score
    return {
        "finding_id": f"finding-{posicao}",
        "state": "OPEN",
        "indexed": "2026-08-27T10:00:00Z",
        "plugin": plugin,
        "asset": {"hostname": f"host-{posicao}"},
        "port": {"port": 443, "protocol": "tcp"},
    }


@pytest.fixture
def config_s3() -> Config:
    return Config(bucket="bucket", prefixo="prod", pg_dsn="")


def test_download_usa_chunks_fixos_fecha_body_e_remove_apenas_seu_temporario(
    config_s3, tmp_path
):
    dados = b"x" * (1024 * 1024 + 17)
    fake = FakeS3({"prod/finding/x.json.gz": dados})
    cliente = ClienteS3(config_s3, cliente=fake)
    sentinela = tmp_path / "nao-remover.json.gz"
    sentinela.write_bytes(b"preservar")

    with cliente.baixar_payload(
        "prod/finding/x.json.gz", hashlib.md5(dados).hexdigest()  # noqa: S324
    ) as path:
        criado = path
        assert path.read_bytes() == dados
        assert path.suffixes[-2:] == [".json", ".gz"]

    assert fake.body is not None
    assert fake.body.read_sizes == [1024 * 1024, 1024 * 1024, 1024 * 1024]
    assert fake.body.closed is True
    assert not criado.exists()
    assert sentinela.read_bytes() == b"preservar"


def test_download_remove_temporario_quando_consumidor_falha(config_s3):
    dados = b"payload"
    cliente = ClienteS3(config_s3, cliente=FakeS3({"x.json.gz": dados}))

    with pytest.raises(RuntimeError, match="COPY indisponivel"):
        with cliente.baixar_payload(
            "x.json.gz", hashlib.md5(dados).hexdigest()  # noqa: S324
        ) as path:
            criado = path
            raise RuntimeError("COPY indisponivel")

    assert not criado.exists()


def test_download_rejeita_md5_malformado_sem_tocar_s3(config_s3):
    fake = FakeS3({"x.json.gz": b"payload"})
    cliente = ClienteS3(config_s3, cliente=fake)

    with pytest.raises(ErroIntegridade, match="md5 inv"):  # conteúdo inválido
        with cliente.baixar_payload("x.json.gz", "nao-e-md5"):
            pytest.fail("não deve entregar arquivo")

    assert fake.bodies == []


def test_falha_ao_fechar_body_propaga_sem_deixar_temporario(
    monkeypatch, config_s3, tmp_path
):
    dados = b"payload"
    fake = FakeS3({"x.json.gz": dados})
    cliente = ClienteS3(config_s3, cliente=fake)
    named_temporary_file = s3mod.tempfile.NamedTemporaryFile
    monkeypatch.setattr(
        s3mod.tempfile,
        "NamedTemporaryFile",
        functools.partial(named_temporary_file, dir=tmp_path),
    )

    class FalhaClose:
        def __init__(self, body):
            self.body = body

        def get_object(self, **kwargs):
            resposta = self.body.get_object(**kwargs)

            def close_com_falha():
                raise RuntimeError("falha no close")

            resposta["Body"].close = close_com_falha
            return resposta

    cliente.cliente = FalhaClose(fake)

    with pytest.raises(RuntimeError, match="falha no close"):
        with cliente.baixar_payload(
            "x.json.gz", hashlib.md5(dados).hexdigest()  # noqa: S324
        ):
            pytest.fail("não deve entregar arquivo")

    assert list(tmp_path.iterdir()) == []


def test_payload_stream_nao_usa_json_loads(monkeypatch, tmp_path):
    from ingestion.streaming import PayloadStream

    doc = envelope("FINDING", [_finding(i) for i in range(10_000)])
    path, dados = _arquivo(tmp_path, doc)
    entrada = _entrada(path.name, dados, updates=10_000)
    monkeypatch.setattr(
        json,
        "loads",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("buffer integral")
        ),
    )

    stream = PayloadStream(path, TIPOS_PAYLOAD["FINDING"], entrada)

    assert stream.version == 1
    assert sum(1 for _ in stream.iter_findings()) == 10_000
    stream.validar_contagens()


def test_payload_stream_preserva_decimal_no_raw_serializavel(tmp_path):
    from ingestion.streaming import PayloadStream

    path, dados = _arquivo(
        tmp_path, envelope("FINDING", [_finding(score=7.25)])
    )
    stream = PayloadStream(
        path, TIPOS_PAYLOAD["FINDING"], _entrada(path.name, dados)
    )

    plugin = next(stream.iter_plugins())

    assert plugin.raw["cvss3_base_score"] == 7.25
    assert json.dumps(plugin.raw)


@pytest.mark.parametrize(
    ("mudanca", "mensagem"),
    [
        (lambda doc: doc.update(type="WAS_FINDING"), "type"),
        (lambda doc: doc.update(payload_id=""), "payload_id"),
        (lambda doc: doc.update(version="1"), "version"),
        (lambda doc: doc.update(count_updated=-1), "count_updated"),
        (lambda doc: doc.update(updates={}), "updates"),
        (lambda doc: doc.pop("deletes"), "deletes"),
    ],
)
def test_payload_stream_rejeita_envelope_invalido(tmp_path, mudanca, mensagem):
    from ingestion.streaming import PayloadStream

    doc = envelope("FINDING", [_finding()])
    mudanca(doc)
    path, dados = _arquivo(tmp_path, doc)

    with pytest.raises((ErroIntegridade, ErroParse), match=mensagem):
        PayloadStream(path, TIPOS_PAYLOAD["FINDING"], _entrada(path.name, dados))


def test_payload_stream_valida_contagem_real_envelope_e_manifest(tmp_path):
    from ingestion.streaming import PayloadStream

    doc = envelope("FINDING", [_finding()])
    doc["count_updated"] = 2
    path, dados = _arquivo(tmp_path, doc)
    stream = PayloadStream(
        path,
        TIPOS_PAYLOAD["FINDING"],
        _entrada(path.name, dados, updates=3),
    )
    assert sum(1 for _ in stream.iter_findings()) == 1

    with pytest.raises(
        ErroIntegridade,
        match=r"envelope diz 2 update\(s\).*manifest diz 3.*payload trouxe 1",
    ):
        stream.validar_contagens()


def test_payload_stream_rejeita_versao_divergente_do_manifest(tmp_path):
    from ingestion.streaming import PayloadStream

    path, dados = _arquivo(tmp_path, envelope("FINDING", [_finding()], version=2))

    with pytest.raises(ErroIntegridade, match="version.*manifest"):
        PayloadStream(path, TIPOS_PAYLOAD["FINDING"], _entrada(path.name, dados))


@pytest.mark.parametrize(
    ("first_ts", "last_ts", "mensagem"),
    [
        (-1, EPOCH_MS, "negativo"),
        (EPOCH_MS + 1, EPOCH_MS, "invert"),
        (str(EPOCH_MS), str(EPOCH_MS + 1), "manifest"),
    ],
)
def test_payload_stream_valida_timestamp_do_envelope_contra_manifest(
    tmp_path, first_ts, last_ts, mensagem
):
    from ingestion.streaming import PayloadStream

    doc = envelope("FINDING", [_finding()])
    doc.update(first_ts=first_ts, last_ts=last_ts)
    path, dados = _arquivo(tmp_path, doc)

    with pytest.raises(ErroIntegridade, match=mensagem):
        PayloadStream(path, TIPOS_PAYLOAD["FINDING"], _entrada(path.name, dados))


def test_timestamp_decimal_longo_demais_e_erro_de_integridade(tmp_path):
    from ingestion.streaming import PayloadStream

    doc = envelope("FINDING", [_finding()])
    doc["first_ts"] = "9" * 5_000
    doc["last_ts"] = doc["first_ts"]
    path, dados = _arquivo(tmp_path, doc)

    with pytest.raises(ErroIntegridade, match="first_ts.*string decimal"):
        PayloadStream(path, TIPOS_PAYLOAD["FINDING"], _entrada(path.name, dados))


def test_payload_vazio_pode_omitir_par_de_timestamps_quando_manifest_tambem_omite(
    tmp_path,
):
    from ingestion.streaming import PayloadStream

    doc = envelope("FINDING", [])
    doc.pop("first_ts")
    doc.pop("last_ts")
    path, dados = _arquivo(tmp_path, doc)
    stream = PayloadStream(
        path,
        TIPOS_PAYLOAD["FINDING"],
        _entrada(path.name, dados, updates=0, first=None, last=None),
    )

    assert list(stream.iter_findings()) == []
    stream.validar_contagens()


def test_payload_nao_vazio_exige_par_de_timestamps(tmp_path):
    from ingestion.streaming import PayloadStream

    doc = envelope("FINDING", [_finding()])
    doc.pop("last_ts")
    path, dados = _arquivo(tmp_path, doc)

    with pytest.raises(ErroIntegridade, match="first_ts.*last_ts"):
        PayloadStream(path, TIPOS_PAYLOAD["FINDING"], _entrada(path.name, dados))


def test_gzip_corrompido_e_falha_de_conteudo(tmp_path):
    from ingestion.streaming import PayloadStream

    path = tmp_path / "corrompido.json.gz"
    path.write_bytes(b"nao-e-gzip")
    entrada = _entrada(path.name, b"nao-e-gzip")

    with pytest.raises(ErroParse, match="gzip|JSON"):
        PayloadStream(path, TIPOS_PAYLOAD["FINDING"], entrada)
