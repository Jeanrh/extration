# Exportador CSV legado

Antecede o pipeline PostgreSQL em `ingestion/`. Lê os mesmos objetos do Data
Stream no S3 e escreve CSVs em `csv/`, sem banco e sem estado.

**Não participa** dos CronJobs do EKS, não compartilha código com a ingestão e
não é validado pelos critérios de aceite do pipeline. Está aqui porque ainda há
quem consuma os CSVs, e sai daqui quando não houver mais.

## Uso

```powershell
.\.venv\Scripts\python.exe -m legacy.exportar --list          # lista os relatórios
.\.venv\Scripts\python.exe -m legacy.exportar                 # roda todos
.\.venv\Scripts\python.exe -m legacy.exportar gestao_vuln     # roda um
.\.venv\Scripts\python.exe -m legacy.exportar intranet internet
```

Rode a partir da raiz do repositório — os módulos são importados como
`legacy.*`.

## Relatórios

| Nome | Fonte | Janela e tratamento | Saída |
|---|---|---|---|
| `vm_findings` | VM | 30 dias | `csv/tenable_vm_findings_completov2.csv` |
| `was_findings` | WAS | 7 dias | `csv/tenable_was_findings_completov2.csv` |
| `intranet` | VM | dedupe, merge enriched | `csv/tenable_intranet_full.csv` |
| `internet` | WAS | dedupe, merge enriched | `csv/tenable_internet_full.csv` |
| `gestao_vuln` | VM + WAS | dedupe | `csv/tenable_gestao_vuln.csv` |

As definições ficam em `reports.py`; a mecânica de busca, filtro e escrita fica
em `tenable_core.py`. Os CSVs saem em UTF-8 com BOM e são ignorados pelo Git.

## Configuração

Usa o bloco "Exportador CSV legado" do [`.env.example`](../.env.example) na
raiz — `AWS_S3_BUCKET` e os prefixos `AWS_S3_*_PREFIX` —, reaproveitando
`AWS_REGION`, a cadeia de credenciais do boto3 e os parâmetros `S3_RETRY_*` do
pipeline. Há overrides globais (`DEDUPE_BY_FINDING_ID`, `MERGE_ENRICHED`) e por
relatório (`<NOME>_LAST_FOUND_DAYS`, `<NOME>_MAX_ROWS`, `<NOME>_OUTPUT`, …).

## Scripts de amostra

```powershell
.\.venv\Scripts\python.exe -m legacy.gerar_exemplos_s3_datastram   # 1 exemplo de cada tipo
.\.venv\Scripts\python.exe -m legacy.gerar_exemplo_s3              # 1 objeto específico
```

`gerar_exemplos_s3_datastram` é o que popula `samples/` — os JSON que as
fixtures da suíte de ingestão carregam e deformam para exercitar cada regra. O
destino é configurável por `S3_SAMPLES_OUTPUT_DIR` e o default é `samples`.
Regerar esses arquivos muda as fixtures dos testes: rode a suíte depois.

`gerar_exemplo_s3` baixa um único objeto, definido por `AWS_S3_KEY`, para
`OUTPUT_FILE`. É ferramenta de inspeção pontual.

## Testes

`tests/test_relatorios.py` cobre os relatórios contra os JSON de `samples/` e um
S3 falso. Roda junto com a suíte principal (`pytest -q`) ou sozinho:

```powershell
.\.venv\Scripts\python.exe tests\test_relatorios.py
```
