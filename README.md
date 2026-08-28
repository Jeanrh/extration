# Tenable Data Stream → PostgreSQL

Pipeline idempotente que ingere manifests e payloads do Tenable Vulnerability
Management no PostgreSQL. Processa apenas `FINDING`, `WAS_FINDING` e
`FINDING_ENRICHED_ATTRIBUTES`, preserva estado corrente e timeline de eventos,
mantém quarentena e publica métricas operacionais.

O destino de execução é um cluster EKS **já existente**. Criar cluster, VPC ou
node groups não faz parte deste projeto.

## Estrutura

```
ingestion/      pipeline: CLI, S3, manifest, payload, loader, métricas e SQL
migrations/     Alembic + DDL versionada do schema
deploy/         base Kubernetes (CronJobs) e template de alarmes CloudWatch
scripts/        harness de PostgreSQL descartável para os testes
samples/        payloads de exemplo reais — fixtures da suíte
tests/          suíte completa (186 testes)
docs/           spec, runbook operacional e mapa de aceite
legacy/         exportador CSV anterior ao pipeline, mantido em separado
main.py         entry point equivalente a `python -m ingestion.cli`
```

## Documentação

| Documento | Para quê |
|---|---|
| [docs/spec.md](docs/spec.md) | A especificação completa: modelo de dados, regras de ingestão, motor de eventos, decisões arquiteturais. |
| [docs/runbook.md](docs/runbook.md) | Operação do dia a dia: setup, seed, corte, quarentena, reprocesso, reconciliação, deploy, rollback e diagnóstico. |
| [docs/acceptance.md](docs/acceptance.md) | Estado verificável dos nove critérios de aceite, com comando e evidência de cada um. |
| [legacy/README.md](legacy/README.md) | O exportador CSV legado, que não participa do pipeline. |

## Como funciona

O job lê manifests do S3 e preserva a ordem declarada dos payloads. Cada payload
é baixado em streaming, validado por MD5 e contagens, processado em transação
isolada e registrado em `ingest_file`. O modo `SEED` popula o estado sem criar
eventos; o modo `INCREMENTAL` mantém o estado e também gera eventos.

O Data Stream pode publicar com frequência de até 15 minutos ("as often as every
15 minutes"); isso não é garantia rígida nem SLA. Esta implantação executa a
ingestão uma vez por dia e deve reavaliar a frequência depois de medir a duração
real do ciclo.

O loader usa uma whitelist fixa de três tipos:

| Payload | Manifest | Produto |
|---|---|---|
| `finding/` | `manifest_finding/` | VM |
| `was_finding/` | `manifest_was_finding/` | WAS |
| `finding_enriched_attributes/` | `manifest_finding_enriched_attributes/` | VM e WAS |

`host_audit_finding` (compliance) e `tds_test_file` (teste de conectividade) não
são ingeridos.

## Setup local

Requisitos: Python 3.11+, PostgreSQL 14+ descartável para desenvolvimento,
leitura no bucket S3 e escrita no banco.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Preencha `TENABLE_BUCKET` e `PG_DSN` fora do Git. Sem credenciais AWS
explícitas, o boto3 usa a cadeia padrão. Em EKS, use IRSA ou EKS Pod Identity;
não coloque credenciais estáticas no Secret.

Para um banco **local ou de teste**, inicialize o schema:

```powershell
.\.venv\Scripts\python.exe -m ingestion.cli init-db
```

`init-db` usa o ledger local `schema_migration`. HML e produção usam
exclusivamente `python -m alembic upgrade head`, cujo ledger é
`alembic_version`. Não misture os dois mecanismos no mesmo banco.

## Primeira execução

```powershell
.\.venv\Scripts\python.exe -m ingestion.cli run --seed --limit 1
.\.venv\Scripts\python.exe -m ingestion.cli status
```

`run --seed --limit 1` **não é dry-run**: baixa e confirma até um payload,
altera o banco e o ledger. Use apenas um banco/bucket autorizados.

Depois, drene o backfill em `SEED`. A troca para `INCREMENTAL` é manual, após
observar estabilidade de inéditos por três dias, e o cutoff sempre leva offset
explícito — data-only é interpretada como meia-noite UTC:

```powershell
.\.venv\Scripts\python.exe -m ingestion.cli run --seed
.\.venv\Scripts\python.exe -m ingestion.cli set-mode INCREMENTAL --cutoff '2026-09-05T00:00:00-03:00' --notes 'backfill estável por três dias'
.\.venv\Scripts\python.exe -m ingestion.cli run
```

O [runbook](docs/runbook.md) cobre o resto: lock, quarentena, reprocesso,
reconciliação, partições, retenção, métricas, alarmes, deploy e diagnóstico.

## Comandos

```text
python -m ingestion.cli init-db
python -m ingestion.cli run [--seed] [--mode SEED|INCREMENTAL] [--limit N]
python -m ingestion.cli set-mode SEED|INCREMENTAL [--cutoff ISO] [--notes TEXTO]
python -m ingestion.cli status
python -m ingestion.cli quarantine
python -m ingestion.cli reprocess --path <path-do-ledger>
python -m ingestion.cli reconcile [--output ARQUIVO|-] [--console-vm-open N] [--console-was-open N]
```

`reprocess` exige a key exata de um payload já existente em `ingest_file` e
reutiliza o modo original do ledger. A reconciliação sem contagens do console
grava `console_comparison: "NOT_PROVIDED"`; ela não inventa acesso ao Tenable.

## Validação local

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q -m 'not banco'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-postgres.ps1 -PostgresBin 'C:\Program Files\PostgreSQL\18\bin' -Port 55489
.\.venv\Scripts\python.exe -m compileall -q ingestion migrations tests
```

Use uma porta descartável livre, diferente de `5432`. Pytest sem `TEST_PG_DSN`,
ou apenas com `-m 'not banco'`, **não é aceite total**: os testes PostgreSQL são
parte obrigatória da validação. Docker, EKS, AWS, o bucket e o console Tenable
exigem validação externa — o estado de cada gate está em
[docs/acceptance.md](docs/acceptance.md).

## Documentação oficial do Tenable

- [Data Stream properties](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/data-stream-properties.htm)
- [Manifest files](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/manifest-files.htm)
- [Configuração do Tenable Data Stream](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/configure-tenable-data-stream.htm)
- [Payloads de findings VM](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/findings-payload-files.htm)
- [Payloads de findings WAS](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/web-app-scanning-findings-payload-files.htm)
- [Payloads de atributos enriquecidos](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/finding-enriched-attributes-payload-files.htm)
- [Data Stream best practices](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/data-stream-best-practices.htm)
