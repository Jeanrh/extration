# Tenable Data Stream → PostgreSQL

Pipeline idempotente para ingerir manifests e payloads do Tenable Vulnerability
Management no PostgreSQL. O fluxo principal processa apenas `FINDING`,
`WAS_FINDING` e `FINDING_ENRICHED_ATTRIBUTES`, preserva estado corrente e
timeline de eventos, mantém quarentena e publica métricas operacionais.

O destino de execução é um cluster EKS **já existente**. Criar cluster, VPC ou
node groups não faz parte deste projeto. A base em `deploy/k8s` é um contrato
versionado e não é aplicável diretamente: um overlay real precisa fornecer
namespace, Secret, workload identity (IRSA ou EKS Pod Identity), imagem ECR por
digest imutável e uma migração executada antes dos CronJobs.

## Fluxo do pipeline

O job lê manifests S3 e preserva a ordem declarada dos payloads. Cada payload é
baixado em streaming, validado por MD5 e contagens, processado em transação
isolada e registrado em `ingest_file`. O modo `SEED` popula o estado sem criar
eventos; o modo `INCREMENTAL` mantém o estado e também gera eventos.

O Data Stream pode publicar arquivos com frequência de até 15 minutos ("as
often as every 15 minutes"); isso não é uma garantia rígida nem um SLA. Esta
implantação escolhe executar a ingestão uma vez por dia e deve reavaliar a
frequência depois de medir a duração real do ciclo.

O loader usa uma whitelist fixa:

| Payload | Manifest | Produto |
|---|---|---|
| `finding/` | `manifest_finding/` | VM |
| `was_finding/` | `manifest_was_finding/` | WAS |
| `finding_enriched_attributes/` | `manifest_finding_enriched_attributes/` | VM e WAS |

`host_audit_finding` (compliance) e `tds_test_file` (teste de conectividade) não
são ingeridos.

## Requisitos e instalação local

- Python 3.11 ou superior;
- PostgreSQL 14 ou superior, descartável para desenvolvimento/testes;
- acesso de leitura ao bucket S3 e permissão de escrita no banco.

No PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Preencha `TENABLE_BUCKET` e `PG_DSN` fora do Git. Sem credenciais AWS explícitas,
o boto3 usa sua cadeia padrão. Em EKS, use IRSA ou EKS Pod Identity; não coloque
credenciais estáticas no Secret.

Para um banco **local ou de teste**, inicialize o schema com:

```powershell
.\.venv\Scripts\python.exe -m ingestion.cli init-db
```

Esse comando usa o ledger local `schema_migration`. HML e produção usam
exclusivamente `python -m alembic upgrade head`, cujo ledger é
`alembic_version`. Não misture os dois mecanismos no mesmo banco.

## Primeira execução

Confira a configuração e rode um smoke mutável:

```powershell
.\.venv\Scripts\python.exe -m ingestion.cli --help
.\.venv\Scripts\python.exe -m ingestion.cli run --seed --limit 1
.\.venv\Scripts\python.exe -m ingestion.cli status
```

`run --seed --limit 1` **não é dry-run**: baixa e confirma até um payload,
altera o banco e o ledger. Use apenas um banco/bucket autorizados para esse
smoke.

Depois, drene o backfill em `SEED`. A troca para `INCREMENTAL` é manual, depois
de observar estabilidade de inéditos por três dias. Sempre grave o cutoff com
offset explícito:

```powershell
.\.venv\Scripts\python.exe -m ingestion.cli run --seed
.\.venv\Scripts\python.exe -m ingestion.cli set-mode INCREMENTAL --cutoff '2026-09-05T00:00:00-03:00' --notes 'backfill estável por três dias'
.\.venv\Scripts\python.exe -m ingestion.cli run
```

Um cutoff somente com data, como `2026-09-05`, é interpretado como meia-noite
UTC. O offset explícito evita ambiguidade operacional.

Todos os fluxos de setup, seed, corte, incremental, lock, quarentena,
reprocesso, reconciliação, partições, retenção, métricas, alarmes, deploy e
diagnóstico estão em [docs/runbook.md](docs/runbook.md). O estado verificável
dos nove critérios da SPEC está em [docs/acceptance.md](docs/acceptance.md).

## Comandos

```text
python -m ingestion.cli init-db
python -m ingestion.cli run [--seed] [--mode SEED|INCREMENTAL] [--limit N]
python -m ingestion.cli set-mode SEED|INCREMENTAL [--cutoff ISO] [--notes TEXTO]
python -m ingestion.cli status
python -m ingestion.cli quarantine
python -m ingestion.cli reprocess --path <path-do-ledger>
python -m ingestion.cli reconcile [--output ARQUIVO] [--console-vm-open N] [--console-was-open N]
```

`reprocess` exige a key exata de um payload já existente em `ingest_file` e
reutiliza o modo original registrado no ledger. A reconciliação sem contagens
do console grava `console_comparison: "NOT_PROVIDED"`; ela não inventa acesso
ao Tenable.

## Validação local

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q -m 'not banco'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-postgres.ps1 -PostgresBin 'C:\Program Files\PostgreSQL\18\bin' -Port 55489
.\.venv\Scripts\python.exe -m compileall -q ingestion migrations tests
```

Use uma porta descartável livre, diferente de `5432`; o exemplo usa `55489`.
Pytest sem `TEST_PG_DSN`, ou apenas com `-m 'not banco'`, não é aceite total: os
testes PostgreSQL são parte obrigatória da validação. Docker, EKS, AWS, o bucket
e o console Tenable exigem validação externa no ambiente real.

## Documentação oficial do Tenable

- [Data Stream properties](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/data-stream-properties.htm)
- [Manifest files](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/manifest-files.htm)
- [Configuração do Tenable Data Stream](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/configure-tenable-data-stream.htm)
- [Payloads de findings VM](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/findings-payload-files.htm)
- [Payloads de findings WAS](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/web-app-scanning-findings-payload-files.htm)
- [Payloads de atributos enriquecidos](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/finding-enriched-attributes-payload-files.htm)
- [Data Stream best practices](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/data-stream-best-practices.htm)

## Exportador CSV legado

O exportador CSV anterior continua disponível, mas é separado do pipeline
PostgreSQL e não participa dos CronJobs EKS descritos acima.

```powershell
.\.venv\Scripts\python.exe exportar.py --list
.\.venv\Scripts\python.exe exportar.py
.\.venv\Scripts\python.exe exportar.py gestao_vuln
```

Ele usa `AWS_S3_BUCKET` e as variáveis da seção "Exportador CSV legado" em
`.env.example`. As definições ficam em `reports.py`; os CSVs são gravados em
`csv/` em UTF-8 com BOM. Os scripts `gerar_exemplo_s3.py` e
`gerar_exemplos_s3_datastram.py` também pertencem apenas a esse fluxo legado.
