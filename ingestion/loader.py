"""Orquestração da ingestão: manifest → payload → COPY → eventos → upsert.

Uma transação por payload. Ou o arquivo entra inteiro (estado + eventos +
marca em `ingest_file`), ou não entra nada (seção 12.1).

A ordem é respeitada em dois níveis (seção 6.2): manifests ordenados pela key
do S3 (o nome traz o epoch, então alfabético = cronológico) e, dentro do
manifest, a ordem literal do array `payloads` — que é a ordem em que o Tenable
observou os dados. Nada aqui é paralelizado, e isso é decisão, não limitação.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import s3 as s3mod
from .config import MODO_INCREMENTAL, MODO_SEED, Config, TipoPayload, TIPOS_PAYLOAD
from .db import carregar_sql, jsonb
from .erros import ErroIngestao, ErroIntegridade, ErroParse
from .manifest import EntradaPayload, Manifest, parse_manifest
from .payload import (
    LinhaFinding,
    LinhaPlugin,
    LinhaRecast,
    colunas,
)
from .streaming import PayloadStream

log = logging.getLogger(__name__)

STATUS_OK = "OK"
STATUS_FALHOU = "FAILED"
STATUS_QUARENTENA = "QUARANTINED"
STATUS_PULADO = "SKIPPED"


@dataclass
class ResultadoPayload:
    path: str
    status: str
    registros: int = 0
    eventos: int = 0
    erro: str | None = None


@dataclass
class ResultadoCiclo:
    manifests_lidos: int = 0
    payloads_ok: int = 0
    payloads_pulados: int = 0
    payloads_falhos: int = 0
    payloads_quarentena: int = 0
    registros: int = 0
    eventos: int = 0
    manifest_mais_recente: dt.datetime | None = None
    duracao_segundos: float = 0.0
    modo: str = MODO_SEED
    erros_manifest: list[str] = field(default_factory=list)

    @property
    def horas_desde_ultimo_manifest(self) -> float | None:
        if self.manifest_mais_recente is None:
            return None
        agora = dt.datetime.now(dt.timezone.utc)
        return (agora - self.manifest_mais_recente).total_seconds() / 3600


class Ingestor:
    def __init__(self, config: Config, cliente: s3mod.ClienteS3, conn):
        self.config = config
        self.cliente = cliente
        self.conn = conn

    # ------------------------------------------------------------------
    # Modo de operação (seção 9)
    # ------------------------------------------------------------------
    def modo_atual(self, override: str | None = None) -> str:
        """A troca SEED → INCREMENTAL é manual e deliberada (seção 9.5). Aqui
        só se lê o que foi decidido; nada promove o modo sozinho."""
        if override:
            return override
        if self.config.modo_forcado:
            log.warning(
                "INGESTION_MODE=%s sobrescreve pipeline_control (use só em teste)",
                self.config.modo_forcado,
            )
            return self.config.modo_forcado
        with self.conn.cursor() as cur:
            cur.execute("SELECT mode FROM pipeline_control WHERE id = 1")
            linha = cur.fetchone()
        return linha["mode"] if linha else MODO_SEED

    # ------------------------------------------------------------------
    # Ciclo completo
    # ------------------------------------------------------------------
    def executar(
        self, limite: int | None = None, modo: str | None = None
    ) -> ResultadoCiclo:
        inicio = time.monotonic()
        resultado = ResultadoCiclo(modo=self.modo_atual(modo))
        log.info("modo de operação: %s", resultado.modo)

        restante = limite
        for tipo in TIPOS_PAYLOAD.values():
            recente = self.cliente.ultimo_manifest_modificado_em(tipo)
            if recente is not None and (
                resultado.manifest_mais_recente is None
                or recente > resultado.manifest_mais_recente
            ):
                resultado.manifest_mais_recente = recente

            for key in self.cliente.listar_manifests(tipo):
                if restante is not None and restante <= 0:
                    break
                restante = self._processar_manifest(
                    key, tipo, resultado, restante
                )
            if restante is not None and restante <= 0:
                log.info("limite de %d payload(s) atingido", limite)
                break

        self._registrar_execucao(resultado)
        resultado.duracao_segundos = time.monotonic() - inicio
        return resultado

    def _processar_manifest(
        self,
        key: str,
        tipo: TipoPayload,
        resultado: ResultadoCiclo,
        restante: int | None,
    ) -> int | None:
        try:
            doc = s3mod.ler_documento(self.cliente.baixar(key), key)
            manifest = parse_manifest(key, doc)
        except Exception:  # noqa: BLE001 - ordem exige abortar no manifest falho
            log.exception("manifest ilegível %s", key)
            raise

        resultado.manifests_lidos += 1
        if manifest.tipo != tipo.tipo_manifest:
            raise ErroIntegridade(
                f"manifest {key}: type={manifest.tipo!r}, esperado "
                f"{tipo.tipo_manifest!r}"
            )
        if manifest.payload_type != tipo.nome:
            raise ErroIntegridade(
                f"manifest {key}: payload_type={manifest.payload_type!r}, esperado "
                f"{tipo.nome!r}"
            )

        estados = self._estados_conhecidos([e.path for e in manifest.payloads])
        if manifest.payloads and all(
            estados.get(e.path) == STATUS_OK for e in manifest.payloads
        ):
            log.warning("manifest %s já totalmente processado", key)
            resultado.payloads_pulados += len(manifest.payloads)
            return restante

        # ORDEM DO ARRAY É OBRIGATÓRIA — não reordenar, não paralelizar.
        for entrada in manifest.payloads:
            if restante is not None and restante <= 0:
                break
            situacao = estados.get(entrada.path)
            if situacao == STATUS_OK:
                log.warning("payload já processado, pulando: %s", entrada.path)
                resultado.payloads_pulados += 1
                continue
            if situacao == STATUS_QUARENTENA:
                log.warning(
                    "payload em quarentena, pulando: %s (reprocesse com "
                    "`ingestion.cli reprocess --path`)", entrada.path,
                )
                resultado.payloads_pulados += 1
                continue

            saida = self.processar_payload(entrada, manifest, tipo, resultado.modo)
            self._contabilizar(resultado, saida)
            if restante is not None:
                restante -= 1
        return restante

    def _contabilizar(self, resultado: ResultadoCiclo, saida: ResultadoPayload) -> None:
        if saida.status == STATUS_OK:
            resultado.payloads_ok += 1
            resultado.registros += saida.registros
            resultado.eventos += saida.eventos
        elif saida.status == STATUS_QUARENTENA:
            resultado.payloads_quarentena += 1
        elif saida.status == STATUS_FALHOU:
            resultado.payloads_falhos += 1
        else:
            resultado.payloads_pulados += 1

    def _estados_conhecidos(self, caminhos: Sequence[str]) -> dict[str, str]:
        """Uma consulta por manifest em vez de uma por payload."""
        if not caminhos:
            return {}
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT path, status FROM ingest_file WHERE path = ANY(%s)",
                (list(caminhos),),
            )
            return {linha["path"]: linha["status"] for linha in cur.fetchall()}

    # ------------------------------------------------------------------
    # Um payload
    # ------------------------------------------------------------------
    def processar_payload(
        self,
        entrada: EntradaPayload,
        manifest: Manifest,
        tipo: TipoPayload,
        modo: str,
        forcar: bool = False,
    ) -> ResultadoPayload:
        inicio = time.monotonic()
        try:
            with self.conn.transaction():
                with self.conn.cursor() as cur:
                    cur.execute(
                        "SELECT status FROM ingest_file WHERE path = %s FOR UPDATE",
                        (entrada.path,),
                    )
                    registro = cur.fetchone()
                if registro is not None and registro["status"] == STATUS_OK and not forcar:
                    log.warning("payload já processado, pulando: %s", entrada.path)
                    return ResultadoPayload(entrada.path, STATUS_PULADO)
                with self.cliente.baixar_payload(entrada.path, entrada.md5) as path:
                    stream = PayloadStream(path, tipo, entrada)
                    eventos = self._aplicar(stream, entrada, manifest, tipo, modo)
                    self._conferir_versao(entrada, tipo, stream.version)
                    self._marcar_ok(entrada, manifest, tipo, stream, eventos, modo)
        except (ErroIntegridade, ErroParse) as erro:
            return self._marcar_falha(entrada, manifest, tipo, modo, erro)
        except Exception:  # noqa: BLE001 - falha operacional deve abortar o job
            log.exception("falha inesperada em %s", entrada.path)
            raise

        registros = stream.registros_lidos
        log.info(
            "%s | %d registro(s), %d evento(s), %.2fs",
            entrada.path, registros, eventos, time.monotonic() - inicio,
        )
        return ResultadoPayload(entrada.path, STATUS_OK, registros, eventos)

    def _conferir_versao(
        self, entrada: EntradaPayload, tipo: TipoPayload, versao: int
    ) -> None:
        """Seção 12.4: versão divergente ALERTA mas NÃO interrompe.

        É o aviso antecipado de que o parser vai quebrar; a mudança pode ser
        aditiva e inofensiva, então parar a fila seria pior."""
        esperada = self.config.versao_esperada(tipo.nome)
        recebida = versao
        if esperada is not None and recebida is not None and recebida != esperada:
            log.error(
                "ALERTA schema: %s veio na versão %s, esperada %s (%s) — seguindo",
                tipo.nome, recebida, esperada, entrada.path,
            )

    def _aplicar(
        self,
        stream: PayloadStream,
        entrada: EntradaPayload,
        manifest: Manifest,
        tipo: TipoPayload,
        modo: str,
    ) -> int:
        eventos = 0
        with self.conn.cursor() as cur:
            cur.execute(carregar_sql("00_staging"))

            findings_copiados = _copiar(
                cur, "stg_finding", LinhaFinding, stream.iter_findings()
            )
            plugins_copiados = _copiar(
                cur, "stg_plugin", LinhaPlugin, stream.iter_plugins()
            )
            recasts_copiados = _copiar(
                cur, "stg_recast", LinhaRecast, stream.iter_recasts()
            )

            stream.validar_contagens()
            copiados = (findings_copiados, plugins_copiados, recasts_copiados)
            mapeados = (
                stream.findings_mapeados,
                stream.plugins_mapeados,
                stream.recasts_mapeados,
            )
            if copiados != mapeados:
                raise RuntimeError(
                    f"{entrada.path}: invariante de COPY violada: "
                    f"copiados={copiados}, mapeados={mapeados}"
                )

            if findings_copiados:
                if modo == MODO_INCREMENTAL:
                    # Antes do dedup, de propósito (seção 6.3): se um finding
                    # abriu e fechou dentro da mesma janela de 15 minutos,
                    # descartar o intermediário perderia o par OPENED+FIXED.
                    cur.execute(
                        carregar_sql("20_events"),
                        {"source_path": entrada.path, "scan_id": entrada.scan_id},
                    )
                    eventos = max(cur.rowcount, 0)
                cur.execute(carregar_sql("10_dedup"))
                if plugins_copiados:
                    cur.execute(carregar_sql("30_upsert_plugin"))
                cur.execute(carregar_sql("40_upsert_current"))
                cur.execute(carregar_sql("45_apply_deletes"))

            if recasts_copiados:
                cur.execute(carregar_sql("50_upsert_recast"))
        return eventos

    def _marcar_ok(
        self,
        entrada: EntradaPayload,
        manifest: Manifest,
        tipo: TipoPayload,
        stream: PayloadStream,
        eventos: int,
        modo: str,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                carregar_sql("60_mark_file"),
                {
                    "path": entrada.path,
                    "payload_type": tipo.nome,
                    "manifest_path": manifest.path,
                    "md5": entrada.md5,
                    "schema_version": stream.version,
                    "num_updates": entrada.num_updates,
                    "num_deletes": entrada.num_deletes,
                    "rows_read": stream.registros_lidos,
                    "events_generated": eventos,
                    "first_record_timestamp": entrada.first_record_timestamp,
                    "last_record_timestamp": entrada.last_record_timestamp,
                    "scan_id": entrada.scan_id,
                    "mode": modo,
                },
            )

    def _marcar_falha(
        self,
        entrada: EntradaPayload,
        manifest: Manifest,
        tipo: TipoPayload,
        modo: str,
        erro: Exception,
    ) -> ResultadoPayload:
        """Transação própria — a do arquivo já foi desfeita.

        Sem isto, a marca de falha morreria junto com o rollback e o pipeline
        ficaria preso no mesmo arquivo para sempre, em silêncio."""
        mensagem = f"{type(erro).__name__}: {erro}"
        nivel = log.error if isinstance(erro, (ErroIntegridade, ErroParse)) else log.exception
        nivel("falha em %s: %s", entrada.path, mensagem)

        try:
            with self.conn.transaction(), self.conn.cursor() as cur:
                cur.execute(
                    carregar_sql("61_mark_failure"),
                    {
                        "path": entrada.path,
                        "payload_type": tipo.nome,
                        "manifest_path": manifest.path,
                        "md5": entrada.md5,
                        "schema_version": entrada.version,
                        "num_updates": entrada.num_updates,
                        "num_deletes": entrada.num_deletes,
                        "first_record_timestamp": entrada.first_record_timestamp,
                        "last_record_timestamp": entrada.last_record_timestamp,
                        "scan_id": entrada.scan_id,
                        "error_message": mensagem[:4000],
                        "mode": modo,
                        "max_attempts": self.config.max_attempts,
                    },
                )
                linha = cur.fetchone()
        except Exception:
            log.exception("não foi possível registrar a falha de %s", entrada.path)
            raise

        status = linha["status"] if linha else STATUS_FALHOU
        if status == STATUS_QUARENTENA:
            log.warning(
                "QUARENTENA após %s tentativa(s): %s — a fila segue",
                linha["attempt_count"], entrada.path,
            )
        return ResultadoPayload(entrada.path, status, erro=mensagem)

    # ------------------------------------------------------------------
    # Reprocesso manual (seção 12.2)
    # ------------------------------------------------------------------
    def reprocessar(self, path: str) -> ResultadoPayload:
        """Reprocessa um payload específico depois de corrigido o problema.

        O manifest de origem é lido de `ingest_file` para recuperar md5 e
        contagens — sem eles não há validação de integridade."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT payload_type, manifest_path, md5, schema_version, "
                "       num_updates, num_deletes, first_record_timestamp, "
                "       last_record_timestamp, scan_id, mode "
                "FROM ingest_file WHERE path = %s",
                (path,),
            )
            registro = cur.fetchone()
        if registro is None:
            raise ErroIngestao(
                f"{path} não está em ingest_file; rode o ciclo normal para ingeri-lo"
            )

        tipo = TIPOS_PAYLOAD.get(registro["payload_type"])
        if tipo is None:
            raise ErroIngestao(
                f"{path} é do tipo {registro['payload_type']}, fora da whitelist"
            )

        entrada = EntradaPayload(
            path=path,
            md5=registro["md5"],
            version=registro["schema_version"],
            num_updates=registro["num_updates"],
            num_deletes=registro["num_deletes"],
            first_record_timestamp=registro["first_record_timestamp"],
            last_record_timestamp=registro["last_record_timestamp"],
            scan_id=registro["scan_id"],
        )
        manifest = Manifest(
            path=registro["manifest_path"],
            tipo=tipo.tipo_manifest,
            payload_type=tipo.nome,
            payloads=(entrada,),
        )
        return self.processar_payload(
            entrada, manifest, tipo, registro["mode"], forcar=True
        )

    # ------------------------------------------------------------------
    def _registrar_execucao(self, resultado: ResultadoCiclo) -> None:
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute(
                "UPDATE pipeline_control "
                "SET last_run_at = now(), "
                "    last_manifest_seen_at = GREATEST(last_manifest_seen_at, %s) "
                "WHERE id = 1",
                (resultado.manifest_mais_recente,),
            )


# ===========================================================================
# COPY
# ===========================================================================
def _valor_copy(valor: Any) -> Any:
    return jsonb(valor) if isinstance(valor, dict) else valor


def _copiar(cur, tabela: str, classe: type, linhas: Iterable[Any]) -> int:
    """Alimenta o COPY linha a linha.

    As colunas saem da ordem dos campos do dataclass, então Python e SQL não
    conseguem sair de sincronia sem o teste acusar."""
    nomes = colunas(classe)
    total = 0
    comando = f"COPY {tabela} ({', '.join(nomes)}) FROM STDIN"
    with cur.copy(comando) as copia:
        for linha in linhas:
            copia.write_row(tuple(_valor_copy(getattr(linha, nome)) for nome in nomes))
            total += 1
    return total
