"""
Baixa 1 objeto específico do S3 (AWS_S3_KEY) e salva como JSON formatado.

Configuração via `.env`:
    AWS_S3_BUCKET   (obrigatório)
    AWS_S3_KEY      (obrigatório) — chave do objeto a baixar
    OUTPUT_FILE     (default: exemplo_consulta.json)
    + credenciais AWS (veja .env.example)

Uso:
    python gerar_exemplo_s3.py
"""

from __future__ import annotations

import json
import os
import sys

from botocore.exceptions import BotoCoreError, ClientError

from tenable_core import buscar_objeto, carregar_config, criar_cliente_s3


def main() -> None:
    config = carregar_config()
    key = os.getenv("AWS_S3_KEY")
    if not key:
        print("Erro: variável obrigatória ausente no .env: AWS_S3_KEY")
        sys.exit(1)
    output_file = os.getenv("OUTPUT_FILE", "exemplo_consulta.json")

    s3_client = criar_cliente_s3(config)
    print(f"Acessando s3://{config['bucket']}/{key} ...")
    try:
        conteudo = buscar_objeto(s3_client, config["bucket"], key)
    except (BotoCoreError, ClientError) as erro:
        print(f"Erro ao acessar o S3: {erro}")
        sys.exit(1)

    texto = conteudo.decode("utf-8")
    try:
        dados = json.loads(texto)
    except json.JSONDecodeError:
        dados = {"conteudo_bruto": texto}
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    print(f"Arquivo de exemplo gerado em: {output_file}")


if __name__ == "__main__":
    main()
