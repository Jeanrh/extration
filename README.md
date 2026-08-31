# Tenable Data Stream → PostgreSQL

Dois subsistemas sobre o mesmo banco, em CronJobs separados.

**Ingestão** (`ingestion/`) — pipeline idempotente que ingere manifests e
payloads do Tenable Vulnerability Management no PostgreSQL. Processa apenas
`FINDING`, `WAS_FINDING` e `FINDING_ENRICHED_ATTRIBUTES`, preserva estado
corrente e timeline de eventos, mantém quarentena e publica métricas.
Transcreve o Tenable fielmente e **não calcula risco**.

**Motor de risco** (`risk/`) — recalcula a prioridade segundo o critério da
empresa (matriz Q1–Q16) sobre **todos** os findings, em qualquer estado e
**sem filtro de tempo**, cruzando o banco com CMDB, Vault, threat intel e um
CSV de arquitetura. É o que o pipeline de CSV anterior não conseguia: lá, o
export só enxerga uma janela de 30 dias (VM) e 7 dias (WAS), e o que fica fora
carrega para sempre o score da última execução que o tocou — inclusive depois
de a regra mudar.

O destino de execução é um cluster EKS **já existente**. Criar cluster, VPC ou
node groups não faz parte deste projeto.

## Estrutura

```
ingestion/      pipeline: CLI, S3, manifest, payload, loader, métricas e SQL
risk/           motor de risco: contexto, derivações, scoring, executor e SQL
migrations/     Alembic + DDL versionada do schema (comum aos dois)
deploy/         base Kubernetes (CronJobs) e template de alarmes CloudWatch
scripts/        harness de PostgreSQL descartável para os testes
samples/        payloads de exemplo reais — fixtures da suíte
tests/          suíte completa (387 testes)
docs/           spec, motor, runbook operacional e mapa de aceite
legacy/         exportador CSV anterior ao pipeline, mantido em separado
main.py         entry point equivalente a `python -m ingestion.cli`
```

O `risk/` se divide por **cardinalidade do domínio**, não por camada: resolver
a camada tecnológica é caro por finding e barato por plugin, então isso roda
uma vez por plugin (`derivacoes/`) e vira JOIN no scoring.

## Documentação

| Documento | Para quê |
|---|---|
| [docs/spec.md](docs/spec.md) | A especificação da ingestão: modelo de dados, regras, motor de eventos, decisões arquiteturais. |
| [docs/motor.md](docs/motor.md) | O motor de risco: fronteira, fontes externas, as regras de scoring, as divergências herdadas e o estado de verificação. |
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

## O motor de risco

Roda em CronJob próprio, depois da ingestão. A separação é deliberada: peso,
faixa de CVSS e mapa de camada mudam com frequência, e recalcular depois de um
ajuste não pode arrastar o Data Stream junto — nem uma falha no scoring pode
barrar a entrada do dado.

```
py = BIA·1.0 + PCI·1.0 + Exposição·1.0 + Arquitetura·1.5
px = CVSS·1.0 + Ameaça·1.1 + Exploit·1.1 + Camada·0.8
```

Bandas em 100/200/300 nos dois eixos, cruzadas na grade Q1–Q16, com prazo de SLA
por quadrante contado desde `first_found`. O resultado vai para `finding_risk`
com **as oito notas gravadas**: sem elas, responder "por que este finding é
Muito Alta?" exigiria reexecutar o motor, que a essa altura já pode estar com
outros pesos.

O banco fornece só os campos do Tenable. As outras quatro fontes continuam onde
sempre estiveram, e **nenhuma delas derruba o motor** quando indisponível — o
snapshot anterior no banco continua valendo:

| Insumo | Origem | Sem ela |
|---|---|---|
| sigla, PCI, BIA, criticidade, unidade de negócio | CMDB (Atlassian Assets/JSM) | contexto de um ciclo atrás |
| threat intel (`nota_threat`) | API clássica do Tenable (`cve_category`) | snapshot anterior preservado |
| camada e família (`nota_layer`) | Vault + `plugin.family` | fallback por `plugin.family` |
| arquitetura (`nota_arch`) | `risk/referencia/arquitetura.csv` | default 40 |

O threat intel é a única que **não** pode migrar para o Data Stream:
`cve_category` é um filtro do export clássico, não um campo — a resposta nunca
diz a que categoria o finding pertence. [docs/motor.md](docs/motor.md) tem o
mapeamento categoria a categoria e o motivo de cada uma não ter substituto.

Gravar é proporcional à mudança real: o upsert usa `IS DISTINCT FROM`, então uma
execução sem mudança de regra nem de contexto recalcula tudo e **escreve zero**.
É o que impede meio milhão de tuplas mortas por dia. Quando o quadrante muda,
sai um evento `RISK_CHANGED` em `finding_event` — a severidade que o negócio
monitora é a do motor, não a do Tenable.

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

python -m risk.cli sync-context
python -m risk.cli run
python -m risk.cli status
```

`reprocess` exige a key exata de um payload já existente em `ingest_file` e
reutiliza o modo original do ledger. A reconciliação sem contagens do console
grava `console_comparison: "NOT_PROVIDED"`; ela não inventa acesso ao Tenable.

No motor, `run` **não** depende de `sync-context`: recalcula sobre o contexto já
no banco. É deliberado — ajustar um peso e ver o efeito não pode exigir esperar
JSM, Vault e a API clássica. Ao mudar uma regra, suba `RISK_ENGINE_VERSION`:
ela fica gravada em cada linha de `finding_risk` e é o que distingue duas
rodadas com fórmulas diferentes.

## Validação local

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q -m 'not banco'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-postgres.ps1 -PostgresBin 'C:\Program Files\PostgreSQL\18\bin' -Port 55489
.\.venv\Scripts\python.exe -m compileall -q ingestion risk migrations tests
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
