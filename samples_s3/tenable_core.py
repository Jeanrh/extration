"""
Motor de extração do Tenable Data Stream (S3 -> CSV).

Um relatório é um objeto `Relatorio` com um **mapa** `{coluna_do_csv: origem}`.
`executar_relatorio()` lê o(s) stream(s) do S3 em streaming (linha a linha, sem
acumular tudo em memória), aplica filtro/dedupe/enriched e escreve o CSV.

As definições dos relatórios ficam em `reports.py`; o CLI em `exportar.py`.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


# ===========================================================================
# Configuração
# ===========================================================================
def _env_int(nome: str, default: int) -> int:
    valor = os.getenv(nome)
    if valor is None or not valor.strip():
        return default
    try:
        return int(valor)
    except ValueError:
        return default


def _env_float(nome: str, default: float) -> float:
    valor = os.getenv(nome)
    if valor is None or not valor.strip():
        return default
    try:
        return float(valor)
    except ValueError:
        return default


def _env_bool(nome: str, default: bool) -> bool:
    valor = os.getenv(nome)
    if valor is None or not valor.strip():
        return default
    return valor.strip().lower() == "true"


def carregar_config() -> dict[str, Any]:
    """Lê o .env e devolve a config comum a todos os relatórios."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ModuleNotFoundError:
        pass

    config = {
        "aws_access_key_id": os.getenv("AWS_ACCESS_KEY_ID") or None,
        "aws_secret_access_key": os.getenv("AWS_SECRET_ACCESS_KEY") or None,
        "aws_session_token": os.getenv("AWS_SESSION_TOKEN") or None,
        "region_name": os.getenv("AWS_REGION", "us-east-1"),
        "bucket": os.getenv("AWS_S3_BUCKET"),
        "prefix_vm": os.getenv("AWS_S3_VM_FINDINGS_PREFIX", "finding/"),
        "prefix_was": os.getenv("AWS_S3_WAS_FINDINGS_PREFIX", "was_finding/"),
        "prefix_enriched": os.getenv(
            "AWS_S3_FINDING_ENRICHED_PREFIX", "finding_enriched_attributes/"
        ),
        "s3_max_workers": _env_int("S3_MAX_WORKERS", 24),
        "s3_retry_max_attempts": _env_int("S3_RETRY_MAX_ATTEMPTS", 8),
        "s3_retry_base_delay_seconds": _env_float("S3_RETRY_BASE_DELAY_SECONDS", 0.25),
        "s3_retry_max_delay_seconds": _env_float("S3_RETRY_MAX_DELAY_SECONDS", 6.0),
        "progress_every": _env_int("S3_PROGRESS_EVERY", 100),
    }

    if not config["bucket"]:
        print("Erro: variável obrigatória ausente no .env: AWS_S3_BUCKET")
        sys.exit(1)
    if config["s3_max_workers"] < 1:
        print("Erro: S3_MAX_WORKERS deve ser >= 1.")
        sys.exit(1)
    if config["s3_max_workers"] > 128:
        print("Aviso: S3_MAX_WORKERS acima de 128 não é recomendado. Limitando para 128.")
        config["s3_max_workers"] = 128
    if config["s3_retry_max_attempts"] < 1:
        print("Erro: S3_RETRY_MAX_ATTEMPTS deve ser >= 1.")
        sys.exit(1)
    if config["progress_every"] < 1:
        config["progress_every"] = 1

    return config


# ===========================================================================
# Cliente S3 / leitura de objetos
# ===========================================================================
def criar_cliente_s3(config: dict[str, Any], max_pool_connections: int = 10):
    """Cria o client boto3 do S3. Sem credenciais no .env, usa a cadeia padrão
    (aws configure, variáveis de ambiente, IAM role, ...).

    `max_pool_connections` deve acompanhar a concorrência (nº de threads).
    `response_checksum_validation="when_required"` evita falsos erros de
    checksum em objetos .gz. `retries mode="adaptive"` reduz a taxa de
    requisições sozinho quando a AWS responde throttling."""
    kwargs: dict[str, Any] = {"region_name": config["region_name"]}
    if config["aws_access_key_id"] and config["aws_secret_access_key"]:
        kwargs["aws_access_key_id"] = config["aws_access_key_id"]
        kwargs["aws_secret_access_key"] = config["aws_secret_access_key"]
        if config["aws_session_token"]:
            kwargs["aws_session_token"] = config["aws_session_token"]

    kwargs["config"] = Config(
        response_checksum_validation="when_required",
        max_pool_connections=max_pool_connections,
        retries={"max_attempts": 5, "mode": "adaptive"},
    )
    return boto3.client("s3", **kwargs)


def buscar_objeto(s3_client, bucket: str, key: str) -> bytes:
    """Bytes do objeto; descomprime se a chave terminar em .gz."""
    response = s3_client.get_object(Bucket=bucket, Key=key)
    conteudo = response["Body"].read()
    if key.endswith(".gz"):
        conteudo = gzip.decompress(conteudo)
    return conteudo


def listar_chaves_json(s3_client, bucket: str, prefix: str) -> list[str]:
    chaves: list[str] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for pagina in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in pagina.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            if key.endswith(".json") or key.endswith(".json.gz"):
                chaves.append(key)
    return chaves


def _erro_retryavel_s3(erro: Exception) -> tuple[bool, bool]:
    if isinstance(erro, ClientError):
        status = erro.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = (erro.response.get("Error", {}).get("Code") or "").strip()
        throttle_codes = {"SlowDown", "Throttling", "ThrottlingException", "RequestTimeout"}
        eh_throttle = status == 503 or code in throttle_codes
        eh_retryavel = eh_throttle or status in {429, 500, 502, 504}
        return eh_retryavel, eh_throttle
    if isinstance(erro, BotoCoreError):
        return True, False
    return False, False


def _ler_payload_json(
    s3_client,
    bucket: str,
    key: str,
    *,
    retry_max_attempts: int,
    retry_base_delay_seconds: float,
    retry_max_delay_seconds: float,
) -> tuple[dict[str, Any], int]:
    retries_throttle = 0
    tentativa = 1
    while True:
        try:
            conteudo = buscar_objeto(s3_client, bucket, key)
            return json.loads(conteudo.decode("utf-8")), retries_throttle
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise
        except (BotoCoreError, ClientError) as erro:
            retryavel, eh_throttle = _erro_retryavel_s3(erro)
            if not retryavel or tentativa >= retry_max_attempts:
                raise
            if eh_throttle:
                retries_throttle += 1
            atraso = min(
                retry_max_delay_seconds,
                retry_base_delay_seconds * (2 ** (tentativa - 1)),
            )
            atraso *= random.uniform(0.7, 1.3)
            time.sleep(max(0.0, atraso))
            tentativa += 1


# ===========================================================================
# Progresso
# ===========================================================================
def formatar_duracao(segundos: float) -> str:
    total = max(0, int(segundos))
    horas, resto = divmod(total, 3600)
    minutos, segs = divmod(resto, 60)
    if horas:
        return f"{horas:02d}:{minutos:02d}:{segs:02d}"
    return f"{minutos:02d}:{segs:02d}"


def _mostrar_progresso(
    rotulo: str, atual: int, total: int, inicio: float, falhas: int = 0,
    retries_throttle: int = 0,
) -> None:
    if total <= 0:
        return
    percentual = (atual / total) * 100
    decorrido = time.time() - inicio
    taxa = atual / decorrido if decorrido > 0 else 0
    restante = (total - atual) / taxa if taxa > 0 else 0
    print(
        f"[{rotulo}] {atual}/{total} ({percentual:5.1f}%) | "
        f"decorrido {formatar_duracao(decorrido)} | ETA {formatar_duracao(restante)} | "
        f"falhas {falhas} | retries_throttle {retries_throttle}"
    )


def _processar_em_paralelo(
    *,
    s3_client,
    bucket: str,
    chaves: list[str],
    rotulo: str,
    config: dict[str, Any],
    processar_payload_fn,
    on_result_fn=None,
) -> tuple[int, int]:
    """Lê `chaves` do S3 em paralelo. Para cada arquivo chama
    `processar_payload_fn(key, payload)` numa worker thread e passa o retorno
    para `on_result_fn(...)` na thread principal. Não acumula resultados.
    Devolve (falhas, retries_throttle)."""
    total = len(chaves)
    if not chaves:
        return 0, 0

    inicio = time.time()
    falhas = 0
    retries_throttle = 0

    def _worker(key: str):
        payload, retries = _ler_payload_json(
            s3_client,
            bucket,
            key,
            retry_max_attempts=config["s3_retry_max_attempts"],
            retry_base_delay_seconds=config["s3_retry_base_delay_seconds"],
            retry_max_delay_seconds=config["s3_retry_max_delay_seconds"],
        )
        return processar_payload_fn(key, payload), retries

    with ThreadPoolExecutor(max_workers=config["s3_max_workers"]) as executor:
        futuros = {executor.submit(_worker, key): key for key in chaves}
        concluidos = 0
        for futuro in as_completed(futuros):
            key = futuros[futuro]
            concluidos += 1
            try:
                resultado_item, retries_item = futuro.result()
                if on_result_fn is not None:
                    on_result_fn(resultado_item)
                retries_throttle += retries_item
            except (BotoCoreError, ClientError, UnicodeDecodeError, json.JSONDecodeError) as erro:
                falhas += 1
                print(f"Aviso: erro ao ler '{key}': {erro}")
            if concluidos == total or concluidos % config["progress_every"] == 0:
                _mostrar_progresso(rotulo, concluidos, total, inicio, falhas, retries_throttle)

    return falhas, retries_throttle


# ===========================================================================
# Achatar payload / normalizar valores
# ===========================================================================
def _normalizar_lista(valores: list[Any]) -> str:
    if not valores:
        return ""
    if all(not isinstance(v, (dict, list)) for v in valores):
        return " | ".join("" if v is None else str(v) for v in valores)
    return json.dumps(valores, ensure_ascii=False, separators=(",", ":"))


def achatar_objeto(
    valor: Any, prefixo: str = "", destino: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Achata dicts aninhados com notação de ponto. Listas de escalares viram
    'a | b | c'; listas de objetos viram JSON."""
    if destino is None:
        destino = {}
    if isinstance(valor, dict):
        for chave, subvalor in valor.items():
            novo_prefixo = f"{prefixo}.{chave}" if prefixo else chave
            achatar_objeto(subvalor, novo_prefixo, destino)
        return destino
    if isinstance(valor, list):
        if prefixo:
            destino[prefixo] = _normalizar_lista(valor)
        return destino
    if prefixo:
        destino[prefixo] = valor
    return destino


def parse_iso(valor: Any) -> dt.datetime | None:
    if not isinstance(valor, str) or not valor.strip():
        return None
    texto = valor.strip()
    if texto.endswith("Z"):
        texto = f"{texto[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(texto)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def normalizar_csv(valor: Any) -> str:
    if valor is None:
        return ""
    if isinstance(valor, bool):
        return "true" if valor else "false"
    if isinstance(valor, (dict, list)):
        return json.dumps(valor, ensure_ascii=False, separators=(",", ":"))
    texto = str(valor).replace("\x00", "")
    texto = texto.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    if texto.startswith(("=", "+", "-", "@")):
        texto = f"'{texto}"
    return texto


def limpar_valor(valor: Any) -> Any:
    # A Tenable serializa alguns campos ausentes como a string literal "null"
    # (ex.: plugin.patch_publication_date). "NONE" é preservado (valor de vetor CVSS).
    if isinstance(valor, str) and valor.strip() == "null":
        return None
    return valor


def resolver_valor(registro: dict[str, Any], spec: Any) -> Any:
    """spec: str (chave achatada) | tuple[str,...] (fallback) | callable(registro)."""
    if callable(spec):
        return spec(registro)
    chaves = (spec,) if isinstance(spec, str) else tuple(spec)
    primeiro_presente: Any = None
    achou_presente = False
    for chave in chaves:
        if chave not in registro:
            continue
        valor = registro[chave]
        if valor is not None and valor != "":
            return valor
        if not achou_presente:
            primeiro_presente = valor
            achou_presente = True
    return primeiro_presente


def data(chave: str):
    """spec de coluna: resolve `chave` e formata dd/mm/aaaa (valor não-data
    passa reto, igual ao comportamento antigo)."""

    def _fn(registro: dict[str, Any]) -> Any:
        valor = registro.get(chave)
        parsed = parse_iso(valor)
        return parsed.strftime("%d/%m/%Y") if parsed is not None else valor

    return _fn


def dias_entre(inicio: Any, fim: Any) -> int | None:
    i = parse_iso(inicio)
    f = parse_iso(fim)
    if i is None or f is None:
        return None
    return max(0, (f - i).days)


def _inserir_valor_aninhado(destino: dict[str, Any], caminho: str, valor: Any) -> None:
    partes = caminho.split(".")
    atual = destino
    for parte in partes[:-1]:
        prox = atual.get(parte)
        if not isinstance(prox, dict):
            prox = {}
            atual[parte] = prox
        atual = prox
    atual[partes[-1]] = valor


def montar_objeto_por_prefixo(registro: dict[str, Any], prefixo: str) -> dict[str, Any] | None:
    """Remonta um objeto aninhado a partir das chaves achatadas `prefixo.*`."""
    resultado: dict[str, Any] = {}
    chave_prefixo = f"{prefixo}."
    for chave, valor in registro.items():
        if not chave.startswith(chave_prefixo):
            continue
        sufixo = chave[len(chave_prefixo):]
        if sufixo:
            _inserir_valor_aninhado(resultado, sufixo, valor)
    return resultado if resultado else None


# ===========================================================================
# Stream finding_enriched_attributes (recast / aceite) — cobre VM e WAS
# ===========================================================================
def _valor_ts_enriched(registro: dict[str, Any]) -> dt.datetime:
    min_dt = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    updated_at = parse_iso(
        registro.get("enriched.recast_properties.recast_annotation.updated_at")
    )
    if updated_at is not None:
        return updated_at
    created_at = parse_iso(
        registro.get("enriched.recast_properties.recast_annotation.created_at")
    )
    return created_at if created_at is not None else min_dt


def montar_mapa_enriched(
    s3_client, bucket: str, prefixo: str, config: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    chaves = listar_chaves_json(s3_client, bucket, prefixo)
    if not chaves:
        print(f"Aviso: nenhum arquivo enriched em s3://{bucket}/{prefixo}")
        return {}

    print(f"Índice de recast/aceite: {len(chaves)} arquivo(s)...")
    resultado: dict[str, dict[str, Any]] = {}

    def _proc(_key: str, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        parcial: dict[str, dict[str, Any]] = {}
        for update in payload.get("updates") or []:
            recast = (update or {}).get("recast_properties") or {}
            finding_id = recast.get("finding_id")
            if not finding_id:
                continue
            registro = achatar_objeto(update, "enriched")
            atual = parcial.get(str(finding_id))
            if atual is None or _valor_ts_enriched(registro) > _valor_ts_enriched(atual):
                parcial[str(finding_id)] = registro
        return parcial

    def _merge(parcial: dict[str, dict[str, Any]]) -> None:
        for finding_id, registro in parcial.items():
            atual = resultado.get(finding_id)
            if atual is None or _valor_ts_enriched(registro) > _valor_ts_enriched(atual):
                resultado[finding_id] = registro

    falhas, _ = _processar_em_paralelo(
        s3_client=s3_client, bucket=bucket, chaves=chaves, rotulo="ENRICHED",
        config=config, processar_payload_fn=_proc, on_result_fn=_merge,
    )
    if falhas:
        print(f"Aviso: {falhas} arquivo(s) enriched falharam após retries.")
    return resultado


def _obter_cache_enriched(cache: dict, config: dict[str, Any], s3_client) -> dict[str, dict[str, Any]]:
    if "mapa" not in cache:
        cache["mapa"] = montar_mapa_enriched(
            s3_client, config["bucket"], config["prefix_enriched"], config
        )
        print(f"  {len(cache['mapa'])} finding(s) com recast/aceite")
    return cache["mapa"]


# ===========================================================================
# Dedupe por finding_id (passada 1: índice leve)
# ===========================================================================
def _chave_ordenacao(update: dict[str, Any]) -> tuple[str, str]:
    # ISO 8601 ordena lexicalmente = cronologicamente. VM usa `indexed`, WAS `indexed_at`.
    return (
        str(update.get("last_found") or ""),
        str(update.get("indexed_at") or update.get("indexed") or ""),
    )


def construir_indice_dedupe(
    s3_client, bucket: str, chaves: list[str], config: dict[str, Any], rotulo: str
) -> dict[str, tuple[tuple[str, str], str, int]]:
    """finding_id -> (ts, s3_key, idx_do_update) do registro mais recente.
    Memória proporcional ao nº de findings distintos, não ao volume total."""
    indice: dict[str, tuple[tuple[str, str], str, int]] = {}

    def _proc(key: str, payload: dict[str, Any]) -> dict[str, tuple]:
        parcial: dict[str, tuple] = {}
        for idx, update in enumerate(payload.get("updates") or []):
            finding_id = (update or {}).get("finding_id")
            if not finding_id:
                continue
            ts = _chave_ordenacao(update or {})
            atual = parcial.get(str(finding_id))
            if atual is None or ts > atual[0]:
                parcial[str(finding_id)] = (ts, key, idx)
        return parcial

    def _merge(parcial: dict[str, tuple]) -> None:
        for finding_id, entrada in parcial.items():
            atual = indice.get(finding_id)
            if atual is None or entrada[0] > atual[0]:
                indice[finding_id] = entrada

    _processar_em_paralelo(
        s3_client=s3_client, bucket=bucket, chaves=chaves, rotulo=rotulo,
        config=config, processar_payload_fn=_proc, on_result_fn=_merge,
    )
    return indice


# ===========================================================================
# Modelo de relatório + motor
# ===========================================================================
@dataclass(frozen=True)
class Relatorio:
    nome: str
    fontes: tuple[str, ...]            # "vm" e/ou "was"
    colunas: dict[str, Any]            # {nome_coluna: spec}; ordem = ordem no CSV
    saida: str
    dedupe: bool = False
    dias_last_found: int = 0           # 0 = sem filtro
    merge_enriched: bool = False
    record_source: bool = False        # injeta "record_source" (VM/WAS) como 1ª coluna
    max_linhas: int = 0               # 0 = sem limite

    def descricao(self) -> str:
        flags = []
        if self.dedupe:
            flags.append("dedupe")
        if self.merge_enriched:
            flags.append("enriched")
        if self.dias_last_found:
            flags.append(f"{self.dias_last_found}d")
        return f"{'+'.join(self.fontes):8} {', '.join(flags) or '-':20} -> {self.saida}"


_PREFIXO_FONTE = {"vm": "prefix_vm", "was": "prefix_was"}


def _prefixo_da_fonte(config: dict[str, Any], fonte: str) -> str:
    return config[_PREFIXO_FONTE[fonte]]


def _override_int(nome_rel: str, sufixo: str, default: int) -> int:
    return _env_int(f"{nome_rel.upper()}_{sufixo}", default)


def _override_bool(nome_rel: str, sufixo: str, env_global: str, default: bool) -> bool:
    valor = os.getenv(f"{nome_rel.upper()}_{sufixo}")
    if valor is None or not valor.strip():
        valor = os.getenv(env_global)
    if valor is None or not valor.strip():
        return default
    return valor.strip().lower() == "true"


def _montar_linha(registro: dict[str, Any], colunas_map: dict[str, Any], record_source: bool) -> dict[str, str]:
    linha: dict[str, str] = {}
    if record_source:
        linha["record_source"] = normalizar_csv(registro.get("record_source"))
    for coluna, spec in colunas_map.items():
        linha[coluna] = normalizar_csv(limpar_valor(resolver_valor(registro, spec)))
    return linha


def _fazer_processador(*, fonte: str, rel: Relatorio, indice, limite, mapa_enriched):
    origem = fonte.upper()
    colunas_map = rel.colunas
    incluir_source = rel.record_source

    def _processar(key: str, payload: dict[str, Any]) -> dict[str, Any]:
        rows: list[dict[str, str]] = []
        st = {"updates": 0, "descartados": 0}
        for idx, update in enumerate(payload.get("updates") or []):
            st["updates"] += 1
            update = update or {}
            finding_id = update.get("finding_id")
            if indice is not None:
                entrada = indice.get(str(finding_id)) if finding_id is not None else None
                if entrada is None or entrada[1] != key or entrada[2] != idx:
                    continue
            registro = achatar_objeto(update)
            if incluir_source:
                registro["record_source"] = origem
            if limite is not None:
                last_seen = parse_iso(registro.get("last_found"))
                if last_seen is None or last_seen < limite:
                    st["descartados"] += 1
                    continue
            if mapa_enriched and finding_id is not None:
                enriquecido = mapa_enriched.get(str(finding_id))
                if enriquecido:
                    registro.update(enriquecido)
            rows.append(_montar_linha(registro, colunas_map, incluir_source))
        return {"rows": rows, "stats": st}

    return _processar


def executar_relatorio(
    rel: Relatorio, config: dict[str, Any], s3_client, cache_enriched: dict
) -> dict[str, Any]:
    dias = _override_int(rel.nome, "LAST_FOUND_DAYS", rel.dias_last_found)
    dedupe = _override_bool(rel.nome, "DEDUPE", "DEDUPE_BY_FINDING_ID", rel.dedupe)
    merge_enr = _override_bool(rel.nome, "MERGE_ENRICHED", "MERGE_ENRICHED", rel.merge_enriched)
    max_linhas = _override_int(rel.nome, "MAX_ROWS", rel.max_linhas)
    saida = os.getenv(f"{rel.nome.upper()}_OUTPUT") or rel.saida

    colunas = list(rel.colunas)
    if rel.record_source:
        colunas = ["record_source", *colunas]

    limite: dt.datetime | None = None
    if dias and dias > 0:
        limite = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=dias)
        print(f"[{rel.nome}] filtro Last Found: últimos {dias} dia(s)")

    mapa_enriched: dict[str, dict[str, Any]] = {}
    if merge_enr:
        mapa_enriched = _obter_cache_enriched(cache_enriched, config, s3_client)

    stats: dict[str, Any] = {
        "updates": 0, "descartados_filtro": 0, "falhas_arquivo": 0,
        "retries_throttle": 0, "linhas_csv": 0, "saida": saida,
    }

    diretorio = os.path.dirname(saida)
    if diretorio:
        os.makedirs(diretorio, exist_ok=True)
    tmp = f"{saida}.tmp"

    with open(tmp, "w", encoding="utf-8-sig", newline="") as arquivo:
        writer = csv.DictWriter(
            arquivo, fieldnames=colunas, delimiter=",", quoting=csv.QUOTE_ALL
        )
        writer.writeheader()
        estado = {"linhas": 0, "parar": False}

        def _on_result(item: dict[str, Any]) -> None:
            stats["updates"] += item["stats"]["updates"]
            stats["descartados_filtro"] += item["stats"]["descartados"]
            for linha in item["rows"]:
                if max_linhas and estado["linhas"] >= max_linhas:
                    estado["parar"] = True
                    return
                writer.writerow(linha)
                estado["linhas"] += 1

        for fonte in rel.fontes:
            if estado["parar"]:
                break
            prefixo = _prefixo_da_fonte(config, fonte)
            chaves = listar_chaves_json(s3_client, config["bucket"], prefixo)
            if not chaves:
                print(f"[{rel.nome}] nenhum arquivo em s3://{config['bucket']}/{prefixo}")
                continue
            print(f"[{rel.nome}/{fonte}] {len(chaves)} arquivo(s)")

            indice = None
            if dedupe:
                indice = construir_indice_dedupe(
                    s3_client, config["bucket"], chaves, config,
                    rotulo=f"{rel.nome.upper()}/{fonte.upper()} índice",
                )

            processar = _fazer_processador(
                fonte=fonte, rel=rel, indice=indice, limite=limite,
                mapa_enriched=mapa_enriched,
            )
            falhas, retries = _processar_em_paralelo(
                s3_client=s3_client, bucket=config["bucket"], chaves=chaves,
                rotulo=f"{rel.nome.upper()}/{fonte.upper()}", config=config,
                processar_payload_fn=processar, on_result_fn=_on_result,
            )
            stats["falhas_arquivo"] += falhas
            stats["retries_throttle"] += retries

    os.replace(tmp, saida)
    stats["linhas_csv"] = estado["linhas"]
    return stats
