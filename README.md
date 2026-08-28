# Tenable Data Stream → CSV

Lê os findings do **Tenable Data Stream** (JSON num bucket S3) e gera CSV.

## Como funciona

O Tenable Vulnerability Management publica arquivos JSON no S3, separados por
stream (prefixo da chave):

| prefixo S3 | conteúdo |
|---|---|
| `finding/` | findings de VM |
| `was_finding/` | findings de Web App Scanning |
| `finding_enriched_attributes/` | recast / aceite de risco (cobre VM **e** WAS) |

Cada arquivo é um payload `{ "type": ..., "updates": [ {um finding}, ... ] }` e
cada item de `updates[]` vira **uma linha** no CSV. Campos aninhados são achatados
com ponto (`plugin.cvss3_base_score`); listas de texto viram `a | b`.

Três peças:

- **`tenable_core.py`** — o motor: lê o S3 em streaming, filtra por Last Found,
  deduplica por `finding_id`, junta o stream de recast, escreve o CSV.
- **`reports.py`** — as definições. Cada relatório é um `Relatorio(...)` com um
  **mapa** `{coluna_do_csv: origem_no_payload}`.
- **`exportar.py`** — o CLI.

### Streaming e memória

A base pode chegar a alguns GB, então nada é acumulado em memória:

- Relatório **sem dedupe** (`vm_findings`, `was_findings`): 1 passada pelo
  prefixo, memória ~constante.
- Relatório **com dedupe** (`intranet`, `internet`, `gestao_vuln`): 2 passadas —
  a 1ª monta um índice leve `finding_id → timestamp` (cresce com o nº de findings
  distintos, não com o volume); a 2ª re-lê e emite só o registro mais recente.
  `<NOME>_DEDUPE=false` força 1 passada (aceita linhas duplicadas).
- O stream de recast é carregado uma vez por execução e reusado entre os
  relatórios.

## Instalação

```
python -m venv .venv
.venv\Scripts\activate          # Windows;  no Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
```

## Configuração

Copie `.env.example` para `.env`. Só `AWS_S3_BUCKET` é obrigatório — sem
credenciais no `.env`, o boto3 usa a cadeia padrão (`aws configure`, variáveis de
ambiente, IAM role).

Cada relatório traz seus defaults; dá para sobrepor por relatório via env
(`<NOME>_LAST_FOUND_DAYS`, `<NOME>_DEDUPE`, `<NOME>_MERGE_ENRICHED`,
`<NOME>_MAX_ROWS`, `<NOME>_OUTPUT`) — ver `.env.example`.

## Uso

```
python exportar.py                     # roda todos os relatórios
python exportar.py gestao_vuln         # roda um
python exportar.py intranet internet   # roda vários
python exportar.py --list              # lista os relatórios
```

Os CSVs saem em `csv/` (criada automaticamente), UTF-8 com BOM (abre no Excel),
todos os campos entre aspas.

### Relatórios

| nome | fontes | saída (em `csv/`) | descrição |
|---|---|---|---|
| `vm_findings` | VM | `tenable_vm_findings_completov2.csv` | conjunto enxuto; recast inline (`severity_modification_type`); datas dd/mm/aaaa; filtro 30d |
| `was_findings` | WAS | `tenable_was_findings_completov2.csv` | idem para WAS; filtro 7d |
| `intranet` | VM | `tenable_intranet_full.csv` | colunas com nomes "amigáveis" + merge do stream de recast; dedupe |
| `internet` | WAS | `tenable_internet_full.csv` | idem para WAS |
| `gestao_vuln` | VM + WAS | `tenable_gestao_vuln.csv` | 1 CSV, layout da planilha de gestão; só campos nativos do Tenable; dedupe |

### Criar um relatório novo

Adicione um `Relatorio(...)` em `reports.py` e inclua-o em `RELATORIOS`. Só o mapa
de colunas — o motor não muda.

## Scripts de exemplo

| script | o que faz |
|---|---|
| `gerar_exemplo_s3.py` | baixa 1 objeto (`AWS_S3_KEY`) formatado |
| `gerar_exemplos_s3_datastram.py` | baixa o exemplo mais recente de cada stream para `samples_s3/` |

## Testes

```
pytest tests/                     # se tiver pytest
python tests/test_relatorios.py   # runner embutido, sem dependências
```

Rodam contra os JSON de `samples_s3/` com um S3 falso — não tocam a AWS.

## Observações sobre os dados

- `last_found` = data do último scan que detectou (o "Last Seen"); **não** é
  `indexed` / `indexed_at` (data de ingestão no Tenable).
- `state` = `OPEN` / `REOPENED` / `FIXED`. Na UI: OPEN ≈ Active/New,
  REOPENED ≈ Resurfaced.
- `severity` = `info` / `low` / `medium` / `high` / `critical` (a caixa pode
  variar entre VM e WAS).
- `severity_modification_type` = `NONE` / `RECASTED` / `ACCEPTED` (é o "Risk
  Modified" da UI).
