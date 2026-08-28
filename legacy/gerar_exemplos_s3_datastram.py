"""
Gera 1 exemplo de JSON de cada tipo de stream no S3:
- VM finding
- Finding enriched
- WAS finding
- WAS finding enriched
- WAS asset

Uso:
    python -m legacy.gerar_exemplos_s3_datastram
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError

from legacy.tenable_core import buscar_objeto, carregar_config, criar_cliente_s3


@dataclass(frozen=True)
class TipoStream:
    nome: str
    prefixo_env: str
    prefixos_default: tuple[str, ...]
    arquivo_saida: str


TIPOS_STREAM = [
    TipoStream("vm_finding", "AWS_S3_VM_FINDINGS_PREFIX", ("finding/",), "exemplo_vm_finding.json"),
    TipoStream("vm_asset", "AWS_S3_VM_ASSET_PREFIX", ("asset/",), "exemplo_vm_asset.json"),
    TipoStream(
        "finding_enriched",
        "AWS_S3_FINDING_ENRICHED_PREFIX",
        ("finding_enriched_attributes/",),
        "exemplo_finding_enriched.json",
    ),
    TipoStream("was_finding", "AWS_S3_WAS_FINDINGS_PREFIX", ("was_finding/",), "exemplo_was_finding.json"),
    TipoStream(
        "was_finding_enriched",
        "AWS_S3_WAS_FINDING_ENRICHED_PREFIX",
        (
            "was_finding_enriched_attributes/",
            "web_app_scanning_finding_enriched_attributes/",
            "was_finding_enriched/",
        ),
        "exemplo_was_finding_enriched.json",
    ),
    TipoStream("was_asset", "AWS_S3_WAS_ASSET_PREFIX", ("was_asset/",), "exemplo_was_asset.json"),
]


def listar_chave_mais_recente(s3_client, bucket: str, prefixo: str) -> str | None:
    paginator = s3_client.get_paginator("list_objects_v2")
    melhor_key: str | None = None
    melhor_last_modified = None

    for pagina in paginator.paginate(Bucket=bucket, Prefix=prefixo):
        for obj in pagina.get("Contents", []):
            key = obj.get("Key")
            if not key or key.endswith("/"):
                continue
            if not (key.endswith(".json") or key.endswith(".json.gz")):
                continue

            last_modified = obj.get("LastModified")
            if melhor_last_modified is None or (
                last_modified is not None and last_modified > melhor_last_modified
            ):
                melhor_last_modified = last_modified
                melhor_key = key

    return melhor_key


def _prefixos_por_tipo(tipo: TipoStream) -> list[str]:
    bruto = (os.getenv(tipo.prefixo_env) or "").strip()
    if bruto:
        return [p.strip() for p in bruto.split(",") if p.strip()]
    return list(tipo.prefixos_default)


def salvar_json_pretty(conteudo: bytes, caminho_saida: str) -> None:
    texto = conteudo.decode("utf-8")
    try:
        dados = json.loads(texto)
    except json.JSONDecodeError:
        dados = {"conteudo_bruto": texto}

    with open(caminho_saida, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)


def main() -> None:
    config = carregar_config()
    output_dir = os.getenv("S3_SAMPLES_OUTPUT_DIR", "samples")
    s3_client = criar_cliente_s3(config)
    os.makedirs(output_dir, exist_ok=True)

    print(f"Bucket: {config['bucket']}")
    print(f"Saída: {output_dir}")

    falhas = 0
    for tipo in TIPOS_STREAM:
        prefixos = _prefixos_por_tipo(tipo)
        if not prefixos:
            print(f"[{tipo.nome}] pulado: prefixo vazio em {tipo.prefixo_env}")
            continue

        try:
            key = None
            prefixo_usado = None
            for prefixo in prefixos:
                key = listar_chave_mais_recente(s3_client, config["bucket"], prefixo)
                if key:
                    prefixo_usado = prefixo
                    break
            if not key:
                print(
                    f"[{tipo.nome}] nenhum JSON encontrado em s3://{config['bucket']}/ "
                    f"(tentados: {', '.join(prefixos)})"
                )
                continue

            conteudo = buscar_objeto(s3_client, config["bucket"], key)
            saida = os.path.join(output_dir, tipo.arquivo_saida)
            salvar_json_pretty(conteudo, saida)
            print(f"[{tipo.nome}] OK | prefixo={prefixo_usado} | key={key} | arquivo={saida}")
        except (BotoCoreError, ClientError, OSError, UnicodeDecodeError, json.JSONDecodeError) as erro:
            falhas += 1
            print(f"[{tipo.nome}] erro: {erro}")

    if falhas:
        print(f"Concluído com {falhas} falha(s).")
        sys.exit(1)
    print("Concluído com sucesso.")


if __name__ == "__main__":
    main()
