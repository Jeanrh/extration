"""Pipeline de ingestão do Tenable Data Stream (S3) para PostgreSQL.

A ingestão é deliberadamente burra: transcreve fielmente o que o Tenable
mandou. Não calcula risco, não reclassifica, não enriquece, não faz join com
CMDB (seção 3.2 da SPEC).

O motivo é econômico. O motor de risco entra depois, como etapa separada do
mesmo job. Se a ingestão calculasse risco, mudar a fórmula exigiria reingerir
todo o histórico do S3; com a separação, muda-se a fórmula e recalcula-se
apenas `finding_risk`.

Módulos:
    config      env vars, whitelist de tipos, versões esperadas
    s3          listagem de manifests, download, validação md5
    manifest    parse do manifest e ordem de processamento
    payload     gunzip, parse e achatamento por payload_type
    db          conexão, advisory lock, carregamento dos .sql
    loader      COPY + transação + orquestração dos SQL
    partitions  criação e expurgo de partições
    metrics     publicação no CloudWatch
    cli         run, set-mode, reprocess, status
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
