"""Acesso ao bucket do Data Stream: listagem de manifests, download e md5.

O sistema entra pelo manifest, nunca listando as pastas de payload. É o
manifest que dá a **ordem**, o **md5** e as **contagens** — e é ele que evita
tropeçar no `tds_test_file/`, que não tem manifest e não é dado.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import logging
import random
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from .config import Config, TipoPayload
from .erros import ErroIntegridade, ErroParse

log = logging.getLogger(__name__)

CHUNK_PAYLOAD = 1024 * 1024
_MD5_HEX = re.compile(r"^[0-9a-fA-F]{32}$")


def criar_cliente_s3(config: Config, max_pool_connections: int = 10):
    """Client boto3. Sem credenciais explícitas usa a cadeia padrão
    (variáveis de ambiente, perfil, IAM role do pod).

    `response_checksum_validation="when_required"` evita falso erro de checksum
    em objetos .gz; `retries mode="adaptive"` reduz a taxa sozinho quando a AWS
    responde throttling."""
    kwargs: dict[str, Any] = {"region_name": config.region_name}
    if config.aws_access_key_id and config.aws_secret_access_key:
        kwargs["aws_access_key_id"] = config.aws_access_key_id
        kwargs["aws_secret_access_key"] = config.aws_secret_access_key
        if config.aws_session_token:
            kwargs["aws_session_token"] = config.aws_session_token

    kwargs["config"] = BotoConfig(
        response_checksum_validation="when_required",
        max_pool_connections=max_pool_connections,
        retries={"max_attempts": 5, "mode": "adaptive"},
    )
    return boto3.client("s3", **kwargs)


def _classificar_erro(erro: Exception) -> tuple[bool, bool]:
    """(retryável, é_throttle)."""
    if isinstance(erro, ClientError):
        status = erro.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        codigo = (erro.response.get("Error", {}).get("Code") or "").strip()
        throttles = {"SlowDown", "Throttling", "ThrottlingException", "RequestTimeout"}
        eh_throttle = status == 503 or codigo in throttles
        return eh_throttle or status in {429, 500, 502, 504}, eh_throttle
    if isinstance(erro, BotoCoreError):
        return True, False
    return False, False


class ClienteS3:
    """Fachada fina sobre o boto3 com o retry que o Data Stream exige."""

    def __init__(self, config: Config, cliente=None):
        self.config = config
        self.cliente = cliente if cliente is not None else criar_cliente_s3(config)
        self.retries_throttle = 0

    # -- listagem ----------------------------------------------------------
    def listar_manifests(self, tipo: TipoPayload) -> list[str]:
        """Keys dos manifests do tipo, **ordenadas pela key**.

        Os nomes contêm o timestamp epoch, então ordem alfabética = ordem
        cronológica (seção 6.2)."""
        prefixo = self.config.prefixo_manifest(tipo)
        chaves: list[str] = []
        paginator = self.cliente.get_paginator("list_objects_v2")
        for pagina in paginator.paginate(Bucket=self.config.bucket, Prefix=prefixo):
            for obj in pagina.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/") or not key.endswith(".json"):
                    continue
                chaves.append(key)
        chaves.sort()
        log.info("manifests em %s: %d", prefixo, len(chaves))
        return chaves

    def ultimo_manifest_modificado_em(self, tipo: TipoPayload) -> dt.datetime | None:
        """LastModified do manifest mais recente do tipo.

        Base da métrica `HoursSinceLastManifest` — o alarme mais importante do
        sistema, porque é o único que detecta o stream parado em silêncio
        (seção 12.5)."""
        prefixo = self.config.prefixo_manifest(tipo)
        recente: dt.datetime | None = None
        paginator = self.cliente.get_paginator("list_objects_v2")
        for pagina in paginator.paginate(Bucket=self.config.bucket, Prefix=prefixo):
            for obj in pagina.get("Contents", []):
                modificado = obj.get("LastModified")
                if modificado is not None and (recente is None or modificado > recente):
                    recente = modificado
        return recente

    # -- download ----------------------------------------------------------
    def baixar(self, key: str) -> bytes:
        """Bytes crus do objeto, **sem descomprimir**.

        O md5 do manifest é calculado sobre o arquivo como está no bucket, então
        a validação tem que acontecer antes do gunzip."""
        tentativa = 1
        while True:
            try:
                resposta = self.cliente.get_object(Bucket=self.config.bucket, Key=key)
                return resposta["Body"].read()
            except (BotoCoreError, ClientError) as erro:
                retryavel, eh_throttle = _classificar_erro(erro)
                if not retryavel or tentativa >= self.config.s3_retry_max_attempts:
                    raise
                if eh_throttle:
                    self.retries_throttle += 1
                atraso = min(
                    self.config.s3_retry_max_delay_seconds,
                    self.config.s3_retry_base_delay_seconds * (2 ** (tentativa - 1)),
                ) * random.uniform(0.7, 1.3)
                log.warning(
                    "retry %d/%d em %s (%s), aguardando %.2fs",
                    tentativa, self.config.s3_retry_max_attempts, key,
                    type(erro).__name__, atraso,
                )
                time.sleep(max(0.0, atraso))
                tentativa += 1

    @contextmanager
    def baixar_payload(self, key: str, md5_esperado: str | None):
        """Baixa um payload comprimido para um temporário de memória limitada.

        Cada tentativa S3 começa em um arquivo novo. O arquivo validado existe
        somente durante o ``with`` e apenas esse caminho é removido na saída.
        """
        if not isinstance(md5_esperado, str) or not _MD5_HEX.fullmatch(
            md5_esperado.strip()
        ):
            raise ErroIntegridade(f"md5 inválido no manifest para {key}")

        esperado = md5_esperado.strip().lower()
        tentativa = 1
        path: Path | None = None
        while True:
            try:
                path, calculado = self._baixar_payload_uma_vez(key)
            except (BotoCoreError, ClientError) as erro:
                retryavel, eh_throttle = _classificar_erro(erro)
                if not retryavel or tentativa >= self.config.s3_retry_max_attempts:
                    raise
                if eh_throttle:
                    self.retries_throttle += 1
                atraso = min(
                    self.config.s3_retry_max_delay_seconds,
                    self.config.s3_retry_base_delay_seconds * (2 ** (tentativa - 1)),
                ) * random.uniform(0.7, 1.3)
                log.warning(
                    "retry %d/%d em %s (%s), aguardando %.2fs",
                    tentativa,
                    self.config.s3_retry_max_attempts,
                    key,
                    type(erro).__name__,
                    atraso,
                )
                time.sleep(max(0.0, atraso))
                tentativa += 1
                continue
            break

        try:
            if calculado.lower() != esperado:
                raise ErroIntegridade(
                    f"md5 divergente em {key}: manifest={esperado} "
                    f"calculado={calculado}"
                )
            yield path
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    def _baixar_payload_uma_vez(self, key: str) -> tuple[Path, str]:
        path: Path | None = None
        body = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".json.gz", delete=False
            ) as temporario:
                path = Path(temporario.name)
                resposta = self.cliente.get_object(
                    Bucket=self.config.bucket, Key=key
                )
                body = resposta["Body"]
                digest = hashlib.md5()  # noqa: S324 - checksum oficial do Tenable
                for bloco in body.iter_chunks(chunk_size=CHUNK_PAYLOAD):
                    if not bloco:
                        continue
                    temporario.write(bloco)
                    digest.update(bloco)
            return path, digest.hexdigest()
        except Exception:
            if path is not None:
                path.unlink(missing_ok=True)
            raise
        finally:
            if body is not None and hasattr(body, "close"):
                try:
                    body.close()
                except Exception:
                    if path is not None:
                        path.unlink(missing_ok=True)
                    raise


# ===========================================================================
# Validação e desempacotamento
# ===========================================================================
def validar_md5(dados: bytes, esperado: str | None, key: str) -> None:
    """Seção 12.3: md5 divergente aborta a transação e entra no fluxo de
    tentativas. Causa comum é download truncado."""
    if not esperado:
        log.warning("manifest não trouxe md5 para %s; validação pulada", key)
        return
    calculado = hashlib.md5(dados).hexdigest()  # noqa: S324 - checksum do Tenable, não é uso criptográfico
    if calculado.lower() != esperado.strip().lower():
        raise ErroIntegridade(
            f"md5 divergente em {key}: manifest={esperado} calculado={calculado}"
        )


def ler_documento(dados: bytes, key: str) -> dict[str, Any]:
    """gunzip (quando .gz) + json.loads.

    Não há streaming real aqui, e é deliberado: o md5 obrigatório exige o
    arquivo inteiro em memória antes de qualquer coisa, e o payload é um único
    objeto JSON, que `json.loads` precisa ler inteiro de qualquer forma. O
    streaming que importa acontece adiante — as linhas alimentam o COPY uma a
    uma, então o pico de memória é um payload, não o lote."""
    bruto = dados
    if key.endswith(".gz"):
        try:
            bruto = gzip.decompress(dados)
        except (OSError, EOFError) as erro:
            raise ErroParse(f"gzip corrompido em {key}: {erro}") from erro
    try:
        doc = json.loads(bruto.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as erro:
        raise ErroParse(f"JSON inválido em {key}: {erro}") from erro
    if not isinstance(doc, dict):
        raise ErroParse(f"payload de {key} não é um objeto JSON")
    return doc
