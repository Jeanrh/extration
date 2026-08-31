# SPEC — Pipeline de Ingestão Tenable Data Stream → PostgreSQL

**Versão:** 1.0
**Data:** 2026-08-27
**Status:** aprovada para implementação

---

## 0. Como usar este documento

Este documento é a especificação completa e autossuficiente do sistema. Um
implementador (humano ou IA) deve conseguir construir o sistema inteiro lendo
apenas este arquivo, sem precisar consultar ninguém.

Regras de leitura:

- Onde estiver escrito **DEVE**, é requisito obrigatório.
- Onde estiver escrito **NÃO DEVE**, é proibição explícita — geralmente porque
  já foi tentado ou porque quebra alguma garantia do sistema.
- Onde estiver escrito **PODE**, é opcional e fica a critério da implementação.
- A seção 17 lista o que está **fora de escopo**. Não implemente nada de lá.
- A seção 18 lista **pendências conhecidas**. Não invente solução para elas;
  implemente o comportamento descrito e deixe a pendência registrada.

---

## 1. Contexto

A empresa usa Tenable Vulnerability Management (VM) e Tenable Web App Scanning
(WAS) para detectar vulnerabilidades. Hoje existe uma automação em produção que
extrai dados via API clássica do Tenable e gera um CSV final consumido por
Power BI. Essa automação tem duas limitações que motivam este projeto:

1. Ela extrai apenas findings em estado aberto (OPEN/REOPENED). Não há registro
   de fechamento, o que impede qualquer análise de tendência ou de eficácia de
   remediação.
2. O CSV é um snapshot. Não há memória: o estado de ontem é perdido quando o
   arquivo é regravado.

A Tenable disponibilizou o **Data Stream**, que envia continuamente os dados
para um bucket S3 em JSON. Este projeto substitui a extração via API pela
ingestão do Data Stream e introduz um banco PostgreSQL como fonte de verdade
persistente, com histórico de eventos.

### 1.1 Objetivo

Construir a malha de ingestão que leva os dados do bucket S3 do Tenable Data
Stream até um banco PostgreSQL, garantindo:

- **Sem duplicatas.** Cada finding do Tenable corresponde a exatamente uma linha
  de estado no banco.
- **Todos os campos.** Nenhuma informação relevante do payload é descartada.
- **Pipeline de eventos.** É possível responder, para qualquer dia: o que abriu,
  o que fechou, o que reabriu, o que foi excluído.
- **Ciclo confiável.** Reprocessável, idempotente, com detecção de falha.

### 1.2 Escopo

**Dentro do escopo (o que este projeto entrega):**

- Leitura do bucket S3 (manifests e payloads)
- Parse, normalização e carga no PostgreSQL
- Geração de eventos de ciclo de vida
- Controle de idempotência e reprocesso
- Observabilidade da ingestão (métricas e alarmes)
- Retenção e expurgo de histórico
- Views de consumo (contrato de leitura para os consumidores)

**Fora do escopo (ver seção 17):** API REST, dashboard, crawler de tickets,
controle de acesso dos consumidores.

O **motor de recálculo de risco** continua fora do escopo *desta* spec, mas
não é mais um módulo futuro: vive em `risk/`, como CronJob separado, e tem
documento próprio em [motor.md](motor.md). A regra da seção 3.2 não muda —
a ingestão segue sem nenhum cálculo de risco.

### 1.3 Consumidores do banco

O banco será lido por três consumidores, todos construídos por outras pessoas.
Este projeto entrega o dado e o contrato de leitura, não os consumidores.

| Consumidor | O que lê | Necessidade específica |
|---|---|---|
| Dashboard web (~200 usuários) | views de consumo | backlog atual, abertura/fechamento por mês, filtros por sigla/unidade de negócio |
| Crawler de tickets Jira | `finding_current` | compara estado do banco vs estado do Jira por `finding_id`; abre ticket para finding novo de risco alto, fecha ticket cujo finding está FIXED |
| Aplicações de IA | `finding_current` + `plugin` | precisa de texto: `plugin_name`, `solution`, `description`, `output` |

---

## 2. Glossário

| Termo | Definição |
|---|---|
| **Finding** | Uma vulnerabilidade detectada em um asset específico. É a unidade central do sistema. |
| **finding_id** | Identificador único do finding no Tenable. É um UUID versão 5 (hash determinístico), portanto estável e reproduzível. |
| **Asset** | Máquina (VM) ou aplicação web (WAS) onde a vulnerabilidade foi encontrada. Identificado por UUID versão 4 (aleatório). |
| **Plugin** | Regra de detecção do Nessus. Identificado por `plugin_id` numérico. Carrega descrição, solução, CVSS, CVE. |
| **Payload** | Arquivo `.json.gz` no bucket contendo um lote de registros (`updates[]` e `deletes[]`). |
| **Manifest** | Arquivo JSON que lista os payloads de uma janela de tempo, na ordem em que foram enviados, com metadados de validação. |
| **Estado (state)** | Situação do finding segundo o Tenable: `OPEN`, `REOPENED` ou `FIXED`. |
| **Evento** | Registro imutável de uma transição de estado (abriu, fechou, reabriu, foi excluído). |
| **Backfill** | Carga inicial do histórico. O Tenable envia a base completa em fatias ao longo de vários dias após a ativação do stream. |
| **T (data de corte)** | Momento em que o backfill termina e o stream passa a entregar apenas o fluxo corrente. A partir de T, eventos são confiáveis. |
| **Modo seed** | Modo de operação que popula estado sem gerar eventos. Usado durante o backfill. |
| **Modo incremental** | Modo de operação normal, que gera eventos. Usado a partir de T. |
| **Motor de risco** | Subsistema que recalcula a severidade segundo regras próprias da empresa (matriz de quadrantes Q1–Q16), sobre **todos** os findings e sem filtro de tempo. Vive em `risk/` e roda como CronJob separado — ver [motor.md](motor.md). **O job de ingestão não o executa nem o altera.** |
| **Recast** | Ação humana no Tenable que altera a severidade de um finding (recast) ou aceita o risco (accept). |

---

## 3. Arquitetura

### 3.1 Visão geral

```
┌─────────────────┐
│ Tenable VM/WAS  │
└────────┬────────┘
         │ Data Stream (push contínuo, JSON.gz)
         ▼
┌─────────────────┐
│   Bucket S3     │  manifests + payloads
└────────┬────────┘
         │ leitura (boto3)
         ▼
┌─────────────────────────────────────────┐
│  JOB DE INGESTÃO  (CronJob no EKS)      │
│  ┌───────────────────────────────────┐  │
│  │ Ingestão fiel                     │  │  ◄── ESTE PROJETO
│  │    manifest → payload → COPY →    │  │
│  │    eventos → upsert               │  │
│  └───────────────────────────────────┘  │
└──────────────────┬──────────────────────┘
                   │
┌──────────────────┼──────────────────────┐
│  JOB DO MOTOR    │  (CronJob separado)   │
│  ┌───────────────▼───────────────────┐  │  ◄── risk/ — ver motor.md
│  │ lê finding_current + plugin,      │  │      cadência própria para
│  │ cruza com CMDB/Vault/intel →      │  │      que mudar um peso não
│  │ escreve finding_risk              │  │      arraste a ingestão
│  └───────────────────────────────────┘  │
└──────────────────┬──────────────────────┘
                   ▼
         ┌───────────────────┐
         │    PostgreSQL     │  fonte de verdade
         └─────────┬─────────┘
                   │ views de consumo
        ┌──────────┼──────────┐
        ▼          ▼          ▼
   Dashboard   Crawler     IA
               Jira
```

### 3.2 Princípio arquitetural central

**A ingestão é deliberadamente burra.** Ela transcreve fielmente o que o Tenable
mandou. Não calcula risco, não reclassifica, não enriquece, não faz join com
CMDB.

Motivo: o motor de risco roda depois, em job próprio. Se a ingestão calculasse
risco, uma mudança de fórmula exigiria reingerir todo o histórico do S3. Com a
separação, muda-se a fórmula e recalcula-se apenas a tabela de risco.

**Consequência obrigatória:** o job de ingestão **NÃO DEVE** ter nenhum código
de cálculo de severidade, score, quadrante ou classificação de negócio.

### 3.3 Ordem de execução dentro do job

O job de ingestão roda uma vez por dia e executa, em sequência:

1. **Ingestão** — lê S3, popula `finding_current`, `finding_event`, `plugin`,
   `finding_recast`
2. **Manutenção** — cria partição do mês seguinte se necessário, expurga
   partições vencidas, publica métricas

O **motor de risco** roda em CronJob próprio, agendado com folga depois deste —
a justificativa da separação está em [motor.md §3.1](motor.md). Nenhum dos dois
faz ida-e-volta: não se extrai do banco para processar fora e devolver.

Consequência a assumir: existe uma janela em que `finding_current` já está
fresco e `finding_risk` ainda não. Quem consome enxerga essa idade em
`finding_risk.computed_at`.

---

## 4. Fonte de dados: o bucket S3

### 4.1 Estrutura de diretórios

O bucket tem a seguinte estrutura na raiz (ou sob um prefixo configurável):

```
<prefix>/
├── asset/                                 ← assets VM
├── asset_enriched_attributes/             ← scores ACR/AES de assets
├── finding/                               ← findings VM          ★ USADO
├── finding_enriched_attributes/           ← detalhes de recast   ★ USADO
├── host_audit_finding/                    ← compliance (NÃO é vulnerabilidade)
├── tags/                                  ← tags de assets
├── was_asset/                             ← assets WAS
├── was_finding/                           ← findings WAS         ★ USADO
├── tds_test_file/                         ← arquivo de teste de conectividade
├── manifest_asset/
├── manifest_asset_enriched_attributes/
├── manifest_finding/                                             ★ USADO
├── manifest_finding_enriched_attributes/                         ★ USADO
├── manifest_host_audit_finding/
├── manifest_tags/
├── manifest_was_asset/
└── manifest_was_finding/                                         ★ USADO
```

Dentro de cada pasta, os arquivos são organizados por data:
`<prefix>/finding/2026-08-27/finding-1787826742410-90.json.gz`

### 4.2 Whitelist obrigatória de tipos

O sistema **DEVE** processar exatamente estes três `payload_type`:

| payload_type | Pasta de payload | Pasta de manifest |
|---|---|---|
| `FINDING` | `finding/` | `manifest_finding/` |
| `WAS_FINDING` | `was_finding/` | `manifest_was_finding/` |
| `FINDING_ENRICHED_ATTRIBUTES` | `finding_enriched_attributes/` | `manifest_finding_enriched_attributes/` |

Todos os demais tipos **DEVEM** ser ignorados, registrando em log de nível DEBUG
que foram ignorados.

**Atenção especial a dois tipos:**

- **`tds_test_file/`** — é o arquivo que o Tenable escreve para validar a
  conectividade com o bucket na configuração. Não tem manifest e não é dado.
  Um listador que varra o bucket por pasta vai tropeçar nele.

- **`host_audit_finding/`** — é compliance (audit de configuração), não
  vulnerabilidade. Tem um campo `finding_id` como os outros, mas a identidade
  real é a combinação `asset_uuid` + `compliance_full_id` + `audit_file`, e o
  mesmo check em audit files diferentes gera findings separados. Se for
  ingerido na tabela unificada, mistura compliance com vulnerabilidade e
  corrompe todas as contagens. **NÃO DEVE ser ingerido.**

O sistema **NÃO DEVE** implementar um loop genérico que descubra tipos pela
estrutura do bucket. A lista acima é fixa em configuração; adicionar um tipo é
uma mudança de código consciente.

### 4.3 Formato do manifest

O Tenable gera um manifest a cada 15 minutos, listando os payloads enviados
naquela janela **na ordem em que foram enviados**.

Exemplo real (`manifest_finding/2026-08-27/...json`):

```json
{
  "type": "MANIFEST_FINDING",
  "payload_type": "FINDING",
  "payloads": [
    {
      "path": "path-prefix/finding/2026-08-27/finding-1727096618967-60-5a0ffa7d.json.gz",
      "md5": "e6919aaffa6967e0c6de3908c9a04a78",
      "version": 1,
      "num_updates": 2,
      "num_deletes": 0,
      "first_record_timestamp": 1727096618799,
      "last_record_timestamp": 1727096618820,
      "scan_id": "5a0ffa7d-6b2c-4af6-a0f1-f506fe769dba"
    }
  ]
}
```

| Campo | Tipo | Uso neste sistema |
|---|---|---|
| `type` | string | `MANIFEST_FINDING`, `MANIFEST_WAS_FINDING`, `MANIFEST_FINDING_ENRICHED_ATTRIBUTES`, `MANIFEST_ASSET`, `MANIFEST_ASSET_ENRICHED_ATTRIBUTES`, `MANIFEST_TAGS`, `MANIFEST_HOST_AUDIT_FINDING`, `MANIFEST_WAS_ASSET` |
| `payload_type` | string | Chave da whitelist (seção 4.2) |
| `payloads[].path` | string | Key do objeto no S3. **É a PK de `ingest_file`.** |
| `payloads[].md5` | string | **Validação obrigatória** do arquivo baixado |
| `payloads[].version` | integer | Versão do schema. Se diferir da esperada, alertar (seção 12.4) |
| `payloads[].num_updates` | integer | **Validação obrigatória** da contagem lida |
| `payloads[].num_deletes` | integer | **Validação obrigatória** da contagem lida |
| `payloads[].first_record_timestamp` | integer (epoch ms) | Metadado, gravado em `ingest_file` |
| `payloads[].last_record_timestamp` | integer (epoch ms) | Fallback de relógio (seção 6.4) |
| `payloads[].scan_id` | string | ID de agrupamento; vazio quando não há. Gravado em `finding_event` para rastreabilidade |

### 4.4 Formato do envelope de payload

Todos os payloads compartilham o mesmo envelope:

```json
{
  "payload_id": "finding-1787826742410-90",
  "version": 1,
  "type": "FINDING",
  "count_updated": 1,
  "count_deleted": 0,
  "updates": [ { ... } ],
  "deletes": [ { ... } ],
  "first_ts": "1787826739356",
  "last_ts": "1787826739356"
}
```

**Armadilha crítica — nome do campo de ID nos deletes varia por tipo:**

| Tipo | Campo do ID em `deletes[]` |
|---|---|
| FINDING | `deletes[]._id` (com underscore) |
| WAS_FINDING | `deletes[]._id` (com underscore) |
| ASSET | `deletes[].id` (sem underscore) |

Um parser genérico que assuma `id` vai perder todos os deletes de finding em
silêncio. O parser **DEVE** tratar cada tipo explicitamente.

Ambos têm `deletes[].deleted_at` (ISO timestamp).

### 4.5 Sobre o backfill

O Data Stream, ao ser ativado, envia **a base completa do Tenable**, não apenas
os dados novos. Essa carga inicial é entregue **em fatias ao longo de vários
dias**, misturada com o fluxo corrente.

Isso tem duas consequências de projeto:

1. Enquanto o backfill roda, findings antigos podem chegar **depois** de versões
   mais recentes do mesmo finding. Sem guarda de versão, o estado do banco anda
   para trás. Ver seção 6.4.

2. Gerar eventos durante o backfill produziria eventos falsos em massa: cada
   pedaço de histórico viraria um "abriu hoje". Por isso existem os dois modos
   de operação. Ver seção 9.

---

## 5. Modelo de dados

### 5.1 Diagrama

```
      ┌──────────────────┐
      │  ingest_file     │  controle de idempotência
      │  PK: path        │  (sem FK para as demais)
      └──────────────────┘

      ┌──────────────────┐         ┌──────────────────┐
      │  plugin          │◄────────│  finding_current │
      │  PK: plugin_id   │         │  PK: finding_id  │
      └──────────────────┘         └────────┬─────────┘
                                            │
                          ┌─────────────────┼─────────────────┐
                          ▼                 ▼                 ▼
                ┌──────────────────┐ ┌─────────────┐ ┌──────────────────┐
                │  finding_event   │ │finding_risk │ │  finding_recast  │
                │  particionada    │ │ (motor)     │ │  (enriched)      │
                └──────────────────┘ └─────────────┘ └──────────────────┘
```

### 5.2 DDL completo

```sql
-- ============================================================================
-- SCHEMA: pipeline de ingestão Tenable Data Stream
-- PostgreSQL 14+ (requer particionamento declarativo e gen_random_uuid)
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- para digest() usado na natural_key


-- ----------------------------------------------------------------------------
-- 1. CONTROLE DE INGESTÃO
-- ----------------------------------------------------------------------------

-- Um registro por payload processado. É o mecanismo de idempotência de arquivo.
CREATE TABLE ingest_file (
    path                    text        PRIMARY KEY,
    payload_type            text        NOT NULL,
    manifest_path           text        NOT NULL,
    md5                     text,
    schema_version          integer,
    num_updates             integer,
    num_deletes             integer,
    rows_read               integer,          -- quantos registros o parser leu
    events_generated        integer,          -- quantos eventos gerou
    first_record_timestamp  timestamptz,
    last_record_timestamp   timestamptz,
    scan_id                 text,
    status                  text        NOT NULL DEFAULT 'OK',
                                        -- OK | FAILED | QUARANTINED | SKIPPED
    attempt_count           integer     NOT NULL DEFAULT 1,
    error_message           text,
    mode                    text        NOT NULL,   -- SEED | INCREMENTAL
    processed_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_ingest_file_status    ON ingest_file (status)
    WHERE status <> 'OK';
CREATE INDEX ix_ingest_file_processed ON ingest_file (processed_at);
CREATE INDEX ix_ingest_file_type      ON ingest_file (payload_type, processed_at);


-- Estado global do pipeline. Tabela de uma linha só.
CREATE TABLE pipeline_control (
    id                    integer     PRIMARY KEY DEFAULT 1,
    mode                  text        NOT NULL DEFAULT 'SEED',  -- SEED | INCREMENTAL
    cutoff_at             timestamptz,          -- "T": quando virou incremental
    last_manifest_seen_at timestamptz,          -- último manifest processado
    last_run_at           timestamptz,
    notes                 text,
    CONSTRAINT pipeline_control_single_row CHECK (id = 1)
);

INSERT INTO pipeline_control (id, mode) VALUES (1, 'SEED')
    ON CONFLICT DO NOTHING;


-- ----------------------------------------------------------------------------
-- 2. PLUGIN (dados estáticos por plugin, evita repetir texto 500k vezes)
-- ----------------------------------------------------------------------------

CREATE TABLE plugin (
    plugin_id           bigint      PRIMARY KEY,
    name                text,
    family              text,
    risk_factor         text,           -- normalizado para MAIÚSCULO
    type                text,
    synopsis            text,
    description         text,
    solution            text,
    see_also            text[],
    cve                 text[],
    cwe                 text[],
    cpe                 text[],
    cvss2_base_score    numeric,
    cvss3_base_score    numeric,
    cvss4_base_score    numeric,
    epss_score          numeric,
    vpr_score           numeric,
    exploit_available   boolean,
    exploited_by_malware boolean,
    in_the_news         boolean,
    has_patch           boolean,
    unsupported_by_vendor boolean,
    publication_date        timestamptz,
    patch_publication_date  timestamptz,
    modification_date       timestamptz,
    raw                 jsonb       NOT NULL,   -- objeto plugin completo
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_plugin_cve ON plugin USING gin (cve);


-- ----------------------------------------------------------------------------
-- 3. FINDING_CURRENT (estado atual — uma linha por finding)
-- ----------------------------------------------------------------------------

CREATE TABLE finding_current (
    finding_id              text        PRIMARY KEY,
    product                 text        NOT NULL,   -- 'VM' | 'WAS'

    -- estado e severidade nativa do Tenable
    state                   text        NOT NULL,   -- OPEN | REOPENED | FIXED
    severity                text,                   -- INFO|LOW|MEDIUM|HIGH|CRITICAL
    severity_id             smallint,
    severity_default_id     smallint,
    severity_modification_type text,                -- NONE | RECASTED | ACCEPTED
    recast_reason           text,
    recast_rule_uuid        text,

    -- plugin (denormalizado para query simples; texto longo em `plugin`)
    plugin_id               bigint,
    plugin_name             text,

    -- asset (comum aos dois produtos)
    asset_uuid              text,
    asset_fqdn              text,
    asset_hostname          text,        -- só VM
    asset_ipv4              text,        -- só VM
    asset_ipv6              text,        -- só VM
    asset_mac_address       text,        -- só VM
    asset_operating_system  text[],      -- só VM
    asset_device_type       text,        -- só VM
    asset_agent_uuid        text,        -- só VM
    asset_network_id        text,
    asset_tracked           boolean,     -- só VM

    -- localização da vulnerabilidade
    port_number             integer,     -- só VM
    port_protocol           text,        -- só VM
    port_service            text,        -- só VM
    url                     text,        -- só WAS
    input_type              text,        -- só WAS
    input_name              text,        -- só WAS
    http_method             text,        -- só WAS

    -- evidência
    output                  text,
    proof                   text,        -- só WAS
    payload                 text,        -- só WAS

    -- datas do ciclo de vida (todas em UTC)
    first_found             timestamptz,
    last_found              timestamptz,
    last_fixed              timestamptz,
    last_observed           timestamptz, -- só WAS
    resurfaced_date         timestamptz, -- só VM
    time_taken_to_fix       bigint,      -- só VM, em segundos
    indexed                 timestamptz NOT NULL,  -- RELÓGIO DE VERSÃO

    -- scan
    scan_uuid               text,
    scan_schedule_uuid      text,
    scan_started_at         timestamptz,
    scan_completed_at       timestamptz, -- só WAS
    scan_target             text,
    source                  text,        -- AGENT|NESSUS|NNM (campo do Tenable)

    -- controle interno
    natural_key             text        NOT NULL,  -- ver 5.4
    deleted_at              timestamptz,
    raw                     jsonb       NOT NULL,  -- registro completo
    first_ingested_at       timestamptz NOT NULL DEFAULT now(),
    last_ingested_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT finding_current_product_ck CHECK (product IN ('VM','WAS')),
    CONSTRAINT finding_current_state_ck   CHECK (state IN ('OPEN','REOPENED','FIXED'))
);

CREATE INDEX ix_fc_state        ON finding_current (state)
    WHERE deleted_at IS NULL;
CREATE INDEX ix_fc_product      ON finding_current (product, state);
CREATE INDEX ix_fc_last_found   ON finding_current (last_found);
CREATE INDEX ix_fc_plugin       ON finding_current (plugin_id);
CREATE INDEX ix_fc_asset        ON finding_current (asset_uuid);
CREATE INDEX ix_fc_hostname     ON finding_current (asset_hostname);
CREATE INDEX ix_fc_natural_key  ON finding_current (natural_key);
CREATE INDEX ix_fc_indexed      ON finding_current (indexed);


-- ----------------------------------------------------------------------------
-- 4. FINDING_EVENT (histórico append-only, particionado por mês)
-- ----------------------------------------------------------------------------

CREATE TABLE finding_event (
    id              bigint      GENERATED ALWAYS AS IDENTITY,
    finding_id      text        NOT NULL,
    product         text        NOT NULL,
    event_type      text        NOT NULL,
    occurred_at     timestamptz NOT NULL,   -- data DO DADO (Tenable)
    detected_at     timestamptz NOT NULL DEFAULT now(),  -- quando ingerimos
    old_state       text,
    new_state       text,
    old_value       jsonb,
    new_value       jsonb,
    source_path     text,                   -- payload de origem (rastreabilidade)
    scan_id         text,

    PRIMARY KEY (id, occurred_at),
    CONSTRAINT finding_event_type_ck CHECK (event_type IN (
        'OPENED', 'REOPENED', 'FIXED', 'DELETED', 'RECAST_CHANGED'
    ))
) PARTITION BY RANGE (occurred_at);

-- Idempotência de evento: reprocessar o mesmo payload não duplica.
CREATE UNIQUE INDEX ux_finding_event_dedup
    ON finding_event (finding_id, event_type, occurred_at);

CREATE INDEX ix_fe_lookup ON finding_event (event_type, occurred_at);
CREATE INDEX ix_fe_finding ON finding_event (finding_id, occurred_at);

-- Partições: criar do mês corrente e do seguinte. Ver seção 13.
-- Exemplo:
-- CREATE TABLE finding_event_2026_08 PARTITION OF finding_event
--     FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');

-- Partição DEFAULT: captura eventos com occurred_at fora das faixas criadas
-- (ex.: OPENED retroativo de 2019). Sem ela, o INSERT falha.
CREATE TABLE finding_event_default PARTITION OF finding_event DEFAULT;


-- ----------------------------------------------------------------------------
-- 5. FINDING_RECAST (detalhe do recast, vindo do stream enriched)
-- ----------------------------------------------------------------------------

CREATE TABLE finding_recast (
    finding_id          text        PRIMARY KEY,
    source              text,
    rule_id             text,
    rule_comment        text,
    modification        text,       -- RECASTED|ACCEPTED|RESULT_CHANGED|RESULT_ACCEPTED
    modification_target text,       -- RISK | RESULT
    recasted_severity   text,       -- NONE|LOW|MEDIUM|HIGH|CRITICAL
    changed_result      text,
    rule_created_at     timestamptz,
    rule_updated_at     timestamptz,
    deleted_at          timestamptz,
    raw                 jsonb       NOT NULL,
    ingested_at         timestamptz NOT NULL DEFAULT now()
);
-- SEM foreign key para finding_current: o recast pode chegar antes do finding.
```

```sql
-- ----------------------------------------------------------------------------
-- 6. FINDING_RISK (preenchida pelo MOTOR — fora do escopo deste projeto,
--    mas a tabela DEVE existir desde o dia 1, porque o contrato de consumo
--    depende dela)
-- ----------------------------------------------------------------------------

CREATE TABLE finding_risk (
    finding_id      text        PRIMARY KEY REFERENCES finding_current(finding_id)
                                ON DELETE CASCADE,
    quadrant        text,           -- Q1..Q16
    py              numeric,
    px              numeric,
    engine_version  text NOT NULL,
    computed_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_risk_quadrant ON finding_risk (quadrant);
```

### 5.3 Justificativa das decisões de modelagem

**Por que `finding_id` como PK.** Os `finding_id` do Tenable são UUID versão 5,
ou seja, hash determinístico de uma chave natural. O mesmo finding real sempre
produz o mesmo ID, em qualquer scan e em qualquer reprocessamento. Isso torna
`ON CONFLICT (finding_id)` uma garantia forte de dedup, não uma convenção
interna.

**Por que tabela única VM + WAS.** O conceito de evento (abriu, fechou,
reabriu) é idêntico nos dois produtos, e todos os consumidores precisam ver as
duas fontes juntas. Duas tabelas exigiriam `UNION` em toda query e duas cópias
da lógica de evento. O custo é ter colunas nulas de cada lado, o que é
aceitável.

**Por que a coluna se chama `product` e não `source`.** `source` já é um campo
nativo do Tenable no finding VM (com valores `AGENT`, `NESSUS`, `NNM`).
Reutilizar o nome causaria colisão. `product` é o nome que o próprio Tenable usa
no stream de `asset_enriched_attributes` para distinguir VM de WAS.

**Por que `plugin` em tabela separada.** O objeto `plugin` representa 41% do
tamanho de um finding VM e 66% de um WAS, e é **idêntico para todos os findings
do mesmo `plugin_id`**. Guardá-lo por finding significa gravar a mesma descrição
dezenas de milhares de vezes. Medição real: 500 mil findings com o registro
inteiro ≈ 4,2 GB; sem `plugin` e `output` ≈ 0,6 GB.
Além do espaço, quando o Tenable atualiza a `solution` de um plugin, atualiza-se
uma linha em vez de dezenas de milhares.

**Por que `plugin_id` e `plugin_name` também ficam em `finding_current`.**
`plugin_name` tem ~40 bytes e é usado em praticamente toda query e todo ticket.
Denormalizá-lo evita um join na maioria dos casos. O texto longo continua na
tabela `plugin` para quem precisar (IA, corpo do ticket).

**Por que guardar `output`.** É a evidência específica daquele finding naquele
asset — sem ele, o ticket automático não diz nada útil e a IA não tem contexto.
É por finding (não é redundante) e foi explicitamente exigido.

**Por que `raw jsonb` mesmo tendo colunas promovidas.** Quando um consumidor
pedir um campo não modelado, resolve-se com `ALTER TABLE` + backfill local, sem
reingerir o S3.

**Por que `finding_risk` existe desde o dia 1 mesmo vazia.** O Tenable não é a
fonte de verdade de risco desta empresa. Sem o risco recalculado no banco, não
existe query possível para "traga as críticas" — nem para o dashboard, nem para
o ticket, nem para a IA. A tabela nasceu vazia aqui e passou a ser preenchida
pelo motor (`risk/`), que a expandiu na migração `0005` com as oito notas, a
prioridade, o SLA e o contexto que produziu tudo isso — ver
[motor.md §4.1](motor.md). Enquanto o motor não tiver rodado num banco, as
views continuam caindo no `severity` nativo.

### 5.4 Chave natural (`natural_key`)

O `finding_id` é UUIDv5 derivado, entre outras coisas, do UUID do asset — que é
**versão 4, aleatório**. Se um asset for recriado no Tenable (reinstalação de
agent, re-registro), ele ganha um UUID novo e **todos os findings dele ganham
IDs novos**, gerando duplicatas legítimas do ponto de vista do banco.

A defesa não é impedir — é detectar. A coluna `natural_key` **DEVE** ser
calculada na ingestão como um hash SHA-256 hex dos campos estáveis:

```
VM:  sha256( lower(coalesce(asset_hostname,''))  || '|' ||
             coalesce(plugin_id::text,'')        || '|' ||
             coalesce(port_number::text,'')      || '|' ||
             upper(coalesce(port_protocol,'')) )

WAS: sha256( lower(coalesce(asset_fqdn,''))      || '|' ||
             coalesce(plugin_id::text,'')        || '|' ||
             lower(coalesce(url,''))             || '|' ||
             coalesce(input_name,'') )
```

O `input_name` do WAS pode ter centenas de caracteres — por isso entra no hash,
não como coluna.

Query de detecção de duplicata (executar na reconciliação semanal):

```sql
SELECT natural_key, count(*) AS ids_distintos,
       array_agg(finding_id) AS finding_ids
FROM   finding_current
WHERE  deleted_at IS NULL AND state <> 'FIXED'
GROUP  BY natural_key
HAVING count(*) > 1
ORDER  BY 2 DESC;
```

---

## 6. Regras de ingestão

### 6.1 Algoritmo geral

```
para cada payload_type na whitelist:
    manifests ← listar objetos em <prefix>/manifest_<tipo>/ [ordenado por key]
    para cada manifest:
        se manifest já totalmente processado: pular
        conteúdo ← baixar e parsear manifest
        para cada entrada em conteúdo.payloads:   ← ORDEM DO ARRAY É OBRIGATÓRIA
            processar_payload(entrada, manifest_path)
```

`processar_payload` roda inteiramente dentro de **uma transação**:

```
BEGIN
    se existe ingest_file com path = entrada.path e status='OK':
        ROLLBACK; retornar SKIPPED

    bytes ← baixar objeto do S3

    validar md5(bytes) == entrada.md5
        se falhar → erro de integridade (seção 12.3)

    doc ← json(gunzip(bytes))

    validar doc.version == SCHEMA_VERSION_ESPERADA[payload_type]
        se diferir → alertar (seção 12.4), mas continuar

    registros ← achatar(doc.updates) ∪ achatar(doc.deletes)

    validar len(doc.updates) == entrada.num_updates
    validar len(doc.deletes) == entrada.num_deletes
        se falhar → erro de integridade

    CREATE TEMP TABLE stg_finding (...) ON COMMIT DROP
    COPY registros → stg_finding

    dedup_intra_arquivo(stg_finding)          -- seção 6.3

    se modo == INCREMENTAL:
        gerar_eventos(stg_finding)            -- seção 8

    upsert_plugin(stg_finding)                -- seção 6.6
    upsert_finding_current(stg_finding)       -- seção 6.4

    INSERT INTO ingest_file (...)
COMMIT
```

### 6.2 Ordem de processamento

A ordem **DEVE** ser respeitada em dois níveis:

1. **Entre manifests:** ordenar pela key do S3. Os nomes de arquivo contêm
   timestamp epoch, então ordem alfabética = ordem cronológica.
2. **Dentro do manifest:** a ordem do array `payloads` é a ordem em que o
   Tenable observou os dados. **NÃO DEVE** ser reordenada nem paralelizada.

### 6.3 Dedup intra-arquivo

O mesmo `finding_id` pode aparecer várias vezes no mesmo payload. O tratamento
depende do modo:

**Modo SEED:** manter apenas o registro de maior `indexed`. Eventos não são
gerados, então os intermediários não têm valor.

```sql
DELETE FROM stg_finding a
USING  stg_finding b
WHERE  a.finding_id = b.finding_id
  AND  (a.indexed, a.ctid) < (b.indexed, b.ctid);
```

**Modo INCREMENTAL:** gerar eventos a partir de **todos** os registros, em
ordem, e fazer o upsert apenas do último. Motivo: se um finding abriu e fechou
dentro da mesma janela de 15 minutos, descartar o intermediário perde o par
OPENED+FIXED. Isso é raro em VM e menos raro em WAS (rescan rápido).

Implementação: rodar `gerar_eventos` **antes** do `DELETE` acima.

### 6.4 Relógio de versão e guarda de ordem

**Não existe campo `updated_at` nos findings.** O relógio de versão é:

| Produto | Campo | Semântica |
|---|---|---|
| VM | `indexed` | quando o Tenable adicionou o finding ao banco dele |
| WAS | `indexed_at` | idem |
| fallback | `last_record_timestamp` do manifest | quando os anteriores vierem nulos |

**`last_found` NÃO DEVE ser usado como relógio.** No exemplo real de WAS, o
`last_found` é 10:19 e o `indexed_at` é 12:19 — duas horas de defasagem. Ordenar
por `last_found` faz um registro reindexado depois sobrescrever um estado mais
novo.

A guarda no upsert é **estritamente maior**:

```sql
WHERE EXCLUDED.indexed > f.indexed
```

Dois motivos:

1. **Backfill misturado com fluxo corrente.** Enquanto o histórico está sendo
   drenado, uma versão antiga de um finding pode chegar depois da atual. Sem a
   guarda, o estado anda para trás.
2. **Reprocesso.** `>` estrito descarta um registro idêntico já aplicado.

### 6.5 Normalização obrigatória na escrita

As divergências entre VM e WAS **DEVEM** ser resolvidas no loader, nunca na
query. Se ficarem para a leitura, um dia alguém compara `'INFO'` com `'info'` e
perde metade do resultado.

| Campo | VM (exemplo real) | WAS (exemplo real) | Normalizado para |
|---|---|---|---|
| relógio | `indexed` | `indexed_at` | coluna `indexed` |
| `severity` | `"info"` | `"INFO"` | `UPPER()` |
| `state` | `"OPEN"` | `"OPEN"` | `UPPER()` |
| `severity_modification_type` | `"NONE"` | `"NONE"` | `UPPER()` |
| `plugin.risk_factor` | `"info"` | `"INFO"` | `UPPER()` |
| timestamps | ISO com `Z` | ISO com `Z` | `timestamptz` UTC |

### 6.6 Upsert de plugin

Para cada registro, extrair o objeto `plugin` e fazer upsert em `plugin` por
`plugin_id`. Como o mesmo plugin aparece muitas vezes no lote, deduplique antes:

```sql
INSERT INTO plugin (plugin_id, name, ..., raw, updated_at)
SELECT DISTINCT ON (plugin_id) plugin_id, name, ..., raw, now()
FROM   stg_plugin
ORDER  BY plugin_id, indexed DESC
ON CONFLICT (plugin_id) DO UPDATE SET
    name = EXCLUDED.name, ..., raw = EXCLUDED.raw, updated_at = now();
```

### 6.7 Tratamento de deletes

Registros do array `deletes[]` **NÃO SÃO remediação**. Significam que o finding
sumiu do Tenable (asset removido, purge, licença). Tratamento:

- `finding_current.deleted_at` ← `deleted_at` do payload
- gera evento `DELETED`, nunca `FIXED`
- o `state` **NÃO DEVE** ser alterado para `FIXED`

Se tratados como FIXED, a métrica de remediação infla com trabalho que ninguém
fez.

---

## 7. Mapeamento de campos

### 7.1 FINDING (VM) → finding_current

| Origem no payload | Coluna | Transformação |
|---|---|---|
| `finding_id` | `finding_id` | — |
| *(constante)* | `product` | `'VM'` |
| `state` | `state` | `UPPER()` |
| `severity` | `severity` | `UPPER()` |
| `severity_id` | `severity_id` | — |
| `severity_default_id` | `severity_default_id` | — |
| `severity_modification_type` | `severity_modification_type` | `UPPER()` |
| `recast_reason` | `recast_reason` | — |
| `recast_rule_uuid` | `recast_rule_uuid` | — |
| `plugin.id` | `plugin_id` | — |
| `plugin.name` | `plugin_name` | — |
| `asset.uuid` | `asset_uuid` | — |
| `asset.fqdn` | `asset_fqdn` | `lower()` |
| `asset.hostname` | `asset_hostname` | `lower()` |
| `asset.ipv4` | `asset_ipv4` | — |
| `asset.ipv6` | `asset_ipv6` | — |
| `asset.mac_address` | `asset_mac_address` | — |
| `asset.operating_system` | `asset_operating_system` | array |
| `asset.device_type` | `asset_device_type` | — |
| `asset.agent_uuid` | `asset_agent_uuid` | — |
| `asset.network_id` | `asset_network_id` | — |
| `asset.tracked` | `asset_tracked` | — |
| `port.port` | `port_number` | — |
| `port.protocol` | `port_protocol` | `UPPER()` |
| `port.service` | `port_service` | — |
| `output` | `output` | — |
| `first_found` | `first_found` | ISO → timestamptz |
| `last_found` | `last_found` | ISO → timestamptz |
| `last_fixed` | `last_fixed` | ISO → timestamptz |
| `resurfaced_date` | `resurfaced_date` | ISO → timestamptz |
| `time_taken_to_fix` | `time_taken_to_fix` | — |
| **`indexed`** | **`indexed`** | ISO → timestamptz, **relógio** |
| `scan.uuid` | `scan_uuid` | — |
| `scan.schedule_uuid` | `scan_schedule_uuid` | — |
| `scan.started_at` | `scan_started_at` | ISO → timestamptz |
| `scan.target` | `scan_target` | — |
| `source` | `source` | `UPPER()` |
| *(registro inteiro)* | `raw` | jsonb |
| *(calculado)* | `natural_key` | ver 5.4 |

Colunas WAS ficam `NULL`: `url`, `input_type`, `input_name`, `http_method`,
`proof`, `payload`, `last_observed`, `scan_completed_at`.

### 7.2 WAS_FINDING → finding_current

| Origem no payload | Coluna | Transformação |
|---|---|---|
| `finding_id` | `finding_id` | — |
| *(constante)* | `product` | `'WAS'` |
| `state` | `state` | `UPPER()` |
| `severity` | `severity` | `UPPER()` |
| `severity_id` | `severity_id` | — |
| `severity_default_id` | `severity_default_id` | — |
| `severity_modification_type` | `severity_modification_type` | `UPPER()` |
| `recast_reason` | `recast_reason` | — |
| `recast_rule_uuid` | `recast_rule_uuid` | — |
| `plugin.id` | `plugin_id` | — |
| `plugin.name` | `plugin_name` | — |
| `asset.uuid` | `asset_uuid` | — |
| `asset.fqdn` | `asset_fqdn` | `lower()` |
| `url` | `url` | — |
| `input_type` | `input_type` | — |
| `input_name` | `input_name` | — |
| `http_method` | `http_method` | `UPPER()` |
| `output` | `output` | — |
| `proof` | `proof` | — |
| `payload` | `payload` | — |
| `first_found` | `first_found` | ISO → timestamptz |
| `last_found` | `last_found` | ISO → timestamptz |
| `last_fixed` | `last_fixed` | ISO → timestamptz |
| `last_observed` | `last_observed` | ISO → timestamptz |
| **`indexed_at`** | **`indexed`** | ISO → timestamptz, **relógio** |
| `scan.uuid` | `scan_uuid` | — |
| `scan.completed_at` | `scan_completed_at` | ISO → timestamptz |
| `scan.started_at` | `scan_started_at` | ISO → timestamptz |
| `scan.target` | `scan_target` | — |
| *(registro inteiro)* | `raw` | jsonb |
| *(calculado)* | `natural_key` | ver 5.4 |

Colunas VM ficam `NULL`: `port_*`, `asset_hostname`, `asset_ipv4`, `asset_ipv6`,
`asset_mac_address`, `asset_operating_system`, `asset_device_type`,
`asset_agent_uuid`, `asset_tracked`, `resurfaced_date`, `time_taken_to_fix`,
`source`.

### 7.3 FINDING_ENRICHED_ATTRIBUTES → finding_recast

| Origem | Coluna |
|---|---|
| `recast_properties.finding_id` | `finding_id` |
| `recast_properties.source` | `source` |
| `recast_properties.recast_annotation.rule_id` | `rule_id` |
| `recast_properties.recast_annotation.rule_comment` | `rule_comment` |
| `recast_properties.recast_annotation.modification` | `modification` |
| `recast_properties.recast_annotation.modification_target` | `modification_target` |
| `recast_properties.recast_annotation.recasted_severity` | `recasted_severity` |
| `recast_properties.recast_annotation.changed_result` | `changed_result` |
| `recast_properties.recast_annotation.created_at` | `rule_created_at` |
| `recast_properties.recast_annotation.updated_at` | `rule_updated_at` |
| *(registro inteiro)* | `raw` |

Upsert por `finding_id`, **sem** foreign key: o recast pode chegar antes do
finding correspondente e não deve ser descartado.

### 7.4 Objeto `plugin` → tabela `plugin`

| Origem | Coluna | Observação |
|---|---|---|
| `plugin.id` | `plugin_id` | PK |
| `plugin.name` | `name` | |
| `plugin.family` | `family` | |
| `plugin.risk_factor` | `risk_factor` | `UPPER()` |
| `plugin.type` | `type` | |
| `plugin.synopsis` | `synopsis` | |
| `plugin.description` | `description` | |
| `plugin.solution` | `solution` | |
| `plugin.see_also` | `see_also` | array |
| `plugin.cve` | `cve` | array; pode vir `null` |
| `plugin.cwe` | `cwe` | array; só WAS |
| `plugin.cpe` | `cpe` | array |
| `plugin.cvss_base_score` | `cvss2_base_score` | |
| `plugin.cvss3_base_score` | `cvss3_base_score` | |
| `plugin.cvss4_base_score` | `cvss4_base_score` | |
| `plugin.epss_score` | `epss_score` | |
| `plugin.vpr.score` | `vpr_score` | usar `vpr`, **não** `vpr_v2` |
| `plugin.exploit_available` | `exploit_available` | |
| `plugin.exploited_by_malware` | `exploited_by_malware` | |
| `plugin.in_the_news` | `in_the_news` | |
| `plugin.has_patch` | `has_patch` | |
| `plugin.unsupported_by_vendor` | `unsupported_by_vendor` | |
| `plugin.publication_date` | `publication_date` | |
| `plugin.patch_publication_date` | `patch_publication_date` | |
| `plugin.modification_date` | `modification_date` | |
| *(objeto inteiro)* | `raw` | jsonb |

**Nota sobre `vpr_v2`:** foi deprecado em 01/07/2026 e será removido em
01/10/2026. Este projeto **NÃO DEVE** depender dele. Use `plugin.vpr`.

### 7.5 Armadilhas de parsing observadas nos payloads reais

| Situação | Comportamento exigido |
|---|---|
| `"null"` como **string** (ex.: `plugin.version`, `plugin.vuln_publication_date`, `plugin.patch_publication_date`) | tratar como `NULL` |
| `plugin.cve` vem `null` (não array vazio) | tratar como `NULL` ou array vazio, nunca quebrar |
| `plugin.bid` vem `[14272]` (int) no VM e `["114966"]` (string) no WAS | normalizar para `text[]` |
| `output` do VM contém JSON **serializado como string** | armazenar como texto; **NÃO DEVE** ser parseado |
| `acr_score`/`exposure_score` vêm como string (`"7"`) | converter para numérico |
| `input_name` do WAS tem centenas de caracteres com HTML | armazenar íntegro; usar hash na `natural_key` |
| `deletes[]._id` (finding) vs `deletes[].id` (asset) | tratar por tipo, explicitamente |
| `first_ts` / `last_ts` vêm como **string** de epoch ms | converter |

---

## 8. Motor de eventos

### 8.1 Tipos de evento

| Evento | Significado | Data (`occurred_at`) |
|---|---|---|
| `OPENED` | Vulnerabilidade passou a existir | `first_found` |
| `REOPENED` | Estava fechada, voltou | `resurfaced_date` se houver, senão `last_found` |
| `FIXED` | Foi remediada | `last_fixed` se houver, senão `indexed` |
| `DELETED` | Sumiu do Tenable (não é remediação) | `deleted_at` do payload |
| `RECAST_CHANGED` | Alguém mexeu na severidade | `indexed` |

`detected_at` é sempre `now()`. A distinção importa: `occurred_at` responde
"quando aconteceu de verdade", `detected_at` responde "quando nosso pipeline
soube". Relatórios usam `occurred_at`.

### 8.2 Regras de transição

Seja `c` o estado atual no banco (pode não existir) e `s` o registro que chegou.

| # | Condição | Evento gerado |
|---|---|---|
| 1 | `c` não existe, `s.state` ∈ (OPEN, REOPENED) | `OPENED` |
| 2 | `c` não existe, `s.state` = FIXED | `OPENED` **e** `FIXED` (ver 8.3) |
| 3 | `c.state` = FIXED, `s.state` ∈ (OPEN, REOPENED) | `REOPENED` |
| 4 | `c.state` ∈ (OPEN, REOPENED), `s.state` = FIXED | `FIXED` |
| 5 | `s` vem de `deletes[]`, `c.deleted_at` é nulo | `DELETED` |
| 6 | `c.severity_modification_type` ≠ `s.severity_modification_type` | `RECAST_CHANGED` |

Regras 1–5 são mutuamente exclusivas. A regra 6 é independente e pode ocorrer
junto com qualquer outra — por isso a implementação **DEVE** usar `UNION ALL` de
blocos separados, e **NÃO DEVE** usar um `CASE` único (um `CASE` retorna um
evento por linha e perderia o segundo).

### 8.3 Regra 2 em detalhe (finding inédito já FIXED)

Cenário: uma vulnerabilidade aberta desde 2024 é remediada. Se o banco nunca
viu esse `finding_id`, o registro chega direto como FIXED.

Sem tratamento especial, esse fechamento desaparece: não gera OPENED (chegou
fechado) e não gera FIXED (não havia linha aberta antes). O trabalho de
remediação some da estatística.

Tratamento: gerar **dois eventos**, cada um com sua data real:

- `OPENED` com `occurred_at` = `first_found` (pode ser de anos atrás)
- `FIXED` com `occurred_at` = `last_fixed`

Isso é o que exige a partição `DEFAULT` no `finding_event`: um OPENED de 2019
não cabe nas partições dos meses recentes.

O mesmo vale para inédito chegando `REOPENED`: gera `OPENED` (`first_found`) +
`REOPENED` (`resurfaced_date`).

**Importante:** após o fim do backfill, esse caso é raro — o estoque inteiro já
está no banco. Quando ocorrer, é um finding que nasceu e morreu entre duas
execuções, e o par com datas próprias é o registro correto, não ruído.

### 8.4 SQL de geração de eventos

```sql
INSERT INTO finding_event
    (finding_id, product, event_type, occurred_at, old_state, new_state,
     old_value, new_value, source_path, scan_id)

-- 1. inédito aberto
SELECT s.finding_id, s.product, 'OPENED',
       COALESCE(s.first_found, s.indexed), NULL, s.state,
       NULL, to_jsonb(s.state), :path, :scan_id
FROM   stg_finding s
LEFT   JOIN finding_current c USING (finding_id)
WHERE  c.finding_id IS NULL AND s.is_delete = false AND s.state <> 'FIXED'

UNION ALL
-- 2a. inédito já fechado → OPENED retroativo
SELECT s.finding_id, s.product, 'OPENED',
       COALESCE(s.first_found, s.indexed), NULL, 'OPEN',
       NULL, to_jsonb('OPEN'::text), :path, :scan_id
FROM   stg_finding s
LEFT   JOIN finding_current c USING (finding_id)
WHERE  c.finding_id IS NULL AND s.is_delete = false
  AND  s.state = 'FIXED' AND s.first_found IS NOT NULL

UNION ALL
-- 2b. inédito já fechado → FIXED
SELECT s.finding_id, s.product, 'FIXED',
       COALESCE(s.last_fixed, s.indexed), NULL, s.state,
       NULL, to_jsonb(s.state), :path, :scan_id
FROM   stg_finding s
LEFT   JOIN finding_current c USING (finding_id)
WHERE  c.finding_id IS NULL AND s.is_delete = false AND s.state = 'FIXED'

UNION ALL
-- 3. reaberto
SELECT s.finding_id, s.product, 'REOPENED',
       COALESCE(s.resurfaced_date, s.last_found, s.indexed), c.state, s.state,
       to_jsonb(c.state), to_jsonb(s.state), :path, :scan_id
FROM   stg_finding s
JOIN   finding_current c USING (finding_id)
WHERE  s.is_delete = false AND c.state = 'FIXED' AND s.state <> 'FIXED'

UNION ALL
-- 4. fechado
SELECT s.finding_id, s.product, 'FIXED',
       COALESCE(s.last_fixed, s.indexed), c.state, s.state,
       to_jsonb(c.state), to_jsonb(s.state), :path, :scan_id
FROM   stg_finding s
JOIN   finding_current c USING (finding_id)
WHERE  s.is_delete = false AND c.state <> 'FIXED' AND s.state = 'FIXED'

UNION ALL
-- 5. excluído do Tenable (NÃO é remediação)
SELECT s.finding_id, s.product, 'DELETED',
       s.deleted_at, c.state, NULL,
       to_jsonb(c.state), NULL, :path, :scan_id
FROM   stg_finding s
JOIN   finding_current c USING (finding_id)
WHERE  s.is_delete = true AND c.deleted_at IS NULL

UNION ALL
-- 6. recast alterado (independente das demais)
SELECT s.finding_id, s.product, 'RECAST_CHANGED',
       s.indexed, NULL, NULL,
       to_jsonb(c.severity_modification_type),
       to_jsonb(s.severity_modification_type), :path, :scan_id
FROM   stg_finding s
JOIN   finding_current c USING (finding_id)
WHERE  s.is_delete = false
  AND  c.severity_modification_type IS DISTINCT FROM s.severity_modification_type

ON CONFLICT DO NOTHING;
```

O `ON CONFLICT DO NOTHING` sobre `ux_finding_event_dedup` garante que
reprocessar o mesmo payload não duplica evento.

### 8.5 SQL de upsert de estado

```sql
INSERT INTO finding_current AS f (
    finding_id, product, state, severity, severity_id, severity_default_id,
    severity_modification_type, recast_reason, recast_rule_uuid,
    plugin_id, plugin_name, asset_uuid, asset_fqdn, asset_hostname,
    asset_ipv4, asset_ipv6, asset_mac_address, asset_operating_system,
    asset_device_type, asset_agent_uuid, asset_network_id, asset_tracked,
    port_number, port_protocol, port_service,
    url, input_type, input_name, http_method,
    output, proof, payload,
    first_found, last_found, last_fixed, last_observed, resurfaced_date,
    time_taken_to_fix, indexed,
    scan_uuid, scan_schedule_uuid, scan_started_at, scan_completed_at,
    scan_target, source, natural_key, deleted_at, raw
)
SELECT finding_id, product, state, severity, severity_id, severity_default_id,
       severity_modification_type, recast_reason, recast_rule_uuid,
       plugin_id, plugin_name, asset_uuid, asset_fqdn, asset_hostname,
       asset_ipv4, asset_ipv6, asset_mac_address, asset_operating_system,
       asset_device_type, asset_agent_uuid, asset_network_id, asset_tracked,
       port_number, port_protocol, port_service,
       url, input_type, input_name, http_method,
       output, proof, payload,
       first_found, last_found, last_fixed, last_observed, resurfaced_date,
       time_taken_to_fix, indexed,
       scan_uuid, scan_schedule_uuid, scan_started_at, scan_completed_at,
       scan_target, source, natural_key,
       CASE WHEN is_delete THEN deleted_at END,
       raw
FROM   stg_finding
ON CONFLICT (finding_id) DO UPDATE SET
    state                      = EXCLUDED.state,
    severity                   = EXCLUDED.severity,
    severity_id                = EXCLUDED.severity_id,
    severity_default_id        = EXCLUDED.severity_default_id,
    severity_modification_type = EXCLUDED.severity_modification_type,
    recast_reason              = EXCLUDED.recast_reason,
    recast_rule_uuid           = EXCLUDED.recast_rule_uuid,
    plugin_id                  = EXCLUDED.plugin_id,
    plugin_name                = EXCLUDED.plugin_name,
    -- first_found NUNCA regride: mantém o mais antigo conhecido
    first_found                = LEAST(f.first_found, EXCLUDED.first_found),
    last_found                 = EXCLUDED.last_found,
    last_fixed                 = EXCLUDED.last_fixed,
    last_observed              = EXCLUDED.last_observed,
    resurfaced_date            = EXCLUDED.resurfaced_date,
    time_taken_to_fix          = EXCLUDED.time_taken_to_fix,
    indexed                    = EXCLUDED.indexed,
    output                     = EXCLUDED.output,
    -- deleted_at é "grudento": uma vez marcado, só limpa se voltar como update
    deleted_at                 = CASE WHEN EXCLUDED.deleted_at IS NOT NULL
                                      THEN EXCLUDED.deleted_at ELSE NULL END,
    raw                        = EXCLUDED.raw,
    natural_key                = EXCLUDED.natural_key,
    last_ingested_at           = now()
    -- (demais colunas seguem o mesmo padrão)
WHERE EXCLUDED.indexed > f.indexed;     -- ← GUARDA DE ORDEM (seção 6.4)
```

### 8.6 Sobre recast e o espelhamento do Tenable

O campo `severity_modification_type` vem **dentro do próprio finding** e assume
os valores `none`, `recasted` ou `accepted`. Junto vêm `recast_reason` e
`recast_rule_uuid`. Além disso, `severity_id` (depois do recast) e
`severity_default_id` (antes do recast) permitem detectar o mesmo fato.

Portanto: **o finding é a fonte primária do evento de recast.** O stream
`finding_enriched_attributes` é complemento — traz o detalhe da regra (id,
comentário, severidade recastada, datas). Isso simplifica a ingestão: o stream
enriched deixa de ser bloqueador de ordem.

Requisito de negócio: quando um recast é removido no Tenable, o finding **DEVE**
voltar a refletir o Tenable. Como o sistema é espelho puro, isso acontece
sozinho — desde que o Tenable reenvie o finding quando a regra é deletada. Ver
pendência P2 (seção 18).

---

## 9. Modos de operação

### 9.1 Por que dois modos

O Data Stream, ao ser ativado, entrega **a base completa do Tenable em fatias ao
longo de vários dias**, misturada com o fluxo corrente.

Um snapshot não é uma sequência de mudanças. Gerar evento a partir dele
inventaria história: centenas de milhares de `OPENED` que ninguém observou.
Ligar o modo incremental no meio do backfill produziria exatamente isso.

### 9.2 Modo SEED

- Popula `finding_current`, `plugin`, `finding_recast`
- **Gera zero eventos**
- Dedup intra-arquivo simplificado (só o último por `finding_id`)
- `ingest_file.mode = 'SEED'`

### 9.3 Modo INCREMENTAL

- Tudo do seed, **mais** a geração de eventos
- Dedup preserva os intermediários para geração de evento (seção 6.3)
- `ingest_file.mode = 'INCREMENTAL'`

### 9.4 Como determinar T (a data de corte)

T não é uma data escolhida — é um ponto observável.

Procedimento:

1. Rodar em modo SEED sobre tudo que já caiu no bucket.
2. Contar, por dia de payload, quantos `finding_id` **inéditos** apareceram:

```sql
SELECT date_trunc('day', first_ingested_at AT TIME ZONE 'America/Sao_Paulo') AS dia,
       count(*) AS ineditos
FROM   finding_current
GROUP  BY 1 ORDER BY 1;
```

3. Enquanto o backfill drena, esse número fica alto. Quando cair para o patamar
   de vulnerabilidades realmente novas por dia (ordem de centenas), o dreno
   terminou.
4. Esse ponto de queda é **T**. Gravar em `pipeline_control.cutoff_at` e mudar
   `pipeline_control.mode` para `'INCREMENTAL'`.

Sinal confirmatório: contar arquivos por dia no bucket. Se o volume do backfill
for muito maior que o do regime normal, as duas curvas caem juntas.

**Critério de decisão:** virar para incremental quando a contagem de inéditos
ficar estável (variação < 20%) por 3 dias consecutivos.

### 9.5 A troca de modo

A troca é manual e deliberada — **NÃO DEVE** ser automática. Comando:

```bash
python -m ingestion.cli set-mode INCREMENTAL --cutoff 2026-09-05
```

Registrar em `pipeline_control` com `notes` explicando a decisão.

---

## 10. Idempotência e garantias

O sistema tem **quatro camadas** de proteção contra duplicata. As quatro são
obrigatórias.

| # | Camada | Mecanismo | Protege contra |
|---|---|---|---|
| 1 | Arquivo | `ingest_file.path` como PK | reprocessar o mesmo payload |
| 2 | Registro (intra-arquivo) | dedup por `finding_id` na staging | mesmo finding repetido no lote |
| 3 | Registro (contra o banco) | `WHERE EXCLUDED.indexed > f.indexed` | ordem invertida, backfill vs corrente |
| 4 | Evento | `ux_finding_event_dedup` + `ON CONFLICT DO NOTHING` | evento duplicado em reprocesso |

Além delas, a identidade em si é forte: `finding_id` é UUIDv5 determinístico,
gerado pelo Tenable a partir de uma chave natural. O mesmo finding real sempre
produz o mesmo ID.

**O único vetor de duplicata que resta** é a recriação de asset no Tenable
(o UUID do asset é v4 aleatório; se o asset é recriado, os findings dele ganham
IDs novos). Isso não é impedível na ingestão — é detectável via `natural_key`
(seção 5.4) e monitorado na reconciliação semanal.

### 10.1 Teste de idempotência obrigatório

O sistema **DEVE** passar neste teste antes de ir para produção:

```
1. Rodar ingestão completa sobre um conjunto de payloads
2. Registrar: count(finding_current), count(finding_event), checksum de estado
3. Limpar ingest_file (apenas ela)
4. Rodar a ingestão exatamente igual de novo
5. Os três valores DEVEM ser idênticos
```

---

## 11. Concorrência

Se a carga demorar mais que o intervalo do CronJob, duas execuções podem
processar o mesmo manifest.

Dois mecanismos, ambos obrigatórios:

1. **Kubernetes:** `concurrencyPolicy: Forbid` no CronJob.
2. **Banco:** advisory lock no início do job, como rede de segurança.

```sql
SELECT pg_try_advisory_lock(hashtext('tenable_ingestion'));
-- se retornar false: outra execução está rodando → sair com código 0 e log
```

Liberar no fim (ou deixar a sessão cair, o que libera automaticamente).

---

## 12. Tratamento de erros

### 12.1 Princípio

Uma transação por payload. Ou o arquivo entra inteiro (estado + eventos + marca
em `ingest_file`), ou não entra nada. Nunca sobra estado meio aplicado.

### 12.2 Arquivo envenenado

Um payload malformado aborta a transação. Como a ordem importa, não se pode
simplesmente pular. Sem política, o pipeline fica preso no mesmo arquivo
indefinidamente — e ninguém percebe.

Política obrigatória:

```
tentativa 1 falha → registra erro, incrementa attempt_count, tenta próximo ciclo
tentativa 2 falha → idem
tentativa 3 falha → status = 'QUARANTINED'
                    → ALERTA no CloudWatch
                    → segue a fila (não trava)
```

Arquivos em quarentena ficam visíveis:

```sql
SELECT path, payload_type, attempt_count, error_message, processed_at
FROM   ingest_file WHERE status = 'QUARANTINED' ORDER BY processed_at DESC;
```

Reprocesso manual após correção:

```bash
python -m ingestion.cli reprocess --path <path-do-payload>
```

**Justificativa:** um buraco conhecido e alarmado é melhor que uma fila parada
em silêncio.

### 12.3 Falha de integridade (md5 ou contagem)

Se o md5 não bater ou a contagem de `updates`/`deletes` divergir do manifest:

- abortar a transação
- registrar `status = 'FAILED'` com a mensagem
- entrar no fluxo de tentativas da seção 12.2

Motivo comum: download truncado. A retentativa normalmente resolve.

### 12.4 Mudança de schema

O campo `version` do payload incrementa quando a estrutura do JSON muda. O
loader **DEVE** comparar com a versão esperada em configuração e, se diferir:

- emitir **ALERTA** (é o aviso antecipado de que o parser vai quebrar)
- **continuar processando** (a mudança pode ser aditiva e inofensiva)
- registrar `schema_version` em `ingest_file` para auditoria

### 12.5 Stream parado

A doc do Tenable é explícita: o stream quebra silenciosamente se a role
IAM for deletada, se a trust relationship mudar ou se a policy do bucket for
alterada. Nesse cenário o job roda, não encontra manifest novo, termina com
sucesso, e o dashboard mostra "zero aberturas hoje" — que parece boa notícia.

Ver seção 12 de observabilidade.

---

## 13. Observabilidade

### 13.1 Métricas no CloudWatch

O job **DEVE** publicar, ao final de cada execução, no namespace
`TenableIngestion`:

| Métrica | Unidade | Descrição |
|---|---|---|
| `HoursSinceLastManifest` | Count | horas desde o manifest mais recente no bucket |
| `PayloadsProcessed` | Count | payloads processados no ciclo |
| `RecordsIngested` | Count | registros lidos |
| `EventsGenerated` | Count | eventos gerados |
| `FilesQuarantined` | Count | total em quarentena |
| `JobDurationSeconds` | Seconds | duração do ciclo |
| `FindingsOpen` | Count | `count(*) WHERE state <> 'FIXED' AND deleted_at IS NULL` |

### 13.2 Alarmes obrigatórios

| Alarme | Condição | Severidade |
|---|---|---|
| Stream parado | `HoursSinceLastManifest > 6` | crítico |
| Arquivo em quarentena | `FilesQuarantined > 0` | alto |
| Job não rodou | ausência de `JobDurationSeconds` em 26h | crítico |
| Queda anômala | `FindingsOpen` variou > 20% vs dia anterior | médio |

O alarme de "stream parado" é o mais importante do sistema: é o único que
detecta a falha silenciosa descrita em 12.5.

Complementarmente, **DEVE** ser habilitada a notificação por e-mail do status do
stream na própria configuração do Tenable Data Stream.

### 13.3 Reconciliação semanal

Job semanal que compara o banco com o console do Tenable e reporta:

- total de findings abertos por produto (banco vs console)
- duplicatas por `natural_key` (query da seção 5.4)
- findings sem `plugin` correspondente
- arquivos em quarentena acumulados

O resultado vai para um relatório. **Dono ainda indefinido** — ver pendência P1.

### 13.4 Logging

Nível `INFO` por payload processado: path, registros lidos, eventos gerados,
duração. Nível `WARNING` para skip, versão divergente, quarentena. Nível `ERROR`
para falha de integridade e exceções.

---

## 14. Retenção e particionamento

### 14.1 Política

| Tabela | Retenção | Mecanismo |
|---|---|---|
| `finding_event` | 24 meses | partição mensal + `DROP TABLE` |
| `finding_current` | indefinida | é o estado atual |
| `ingest_file` | 90 dias | `DELETE` (volume baixo) |
| `plugin` | indefinida | volume pequeno |
| `finding_recast` | indefinida | volume pequeno |

### 14.2 Por que particionar desde o dia 1

A retenção de 24 meses significa que um dia alguém vai precisar apagar o mês 25.
`DELETE FROM finding_event WHERE occurred_at < ...` sobre milhões de linhas é
lento, trava a tabela e deixa bloat.

Com partição, o expurgo é `DROP TABLE finding_event_2024_09` — instantâneo, sem
lock, sem bloat. E queries filtradas por período só tocam as partições
relevantes.

**A tabela precisa nascer particionada.** Converter depois, com dados dentro, é
cirurgia. Por isso é requisito da v1, não melhoria futura.

### 14.3 Manutenção automática

O job, na etapa 3 (seção 3.3), **DEVE**:

```
1. garantir que existem partições para o mês corrente e o próximo
2. dropar partições com occurred_at inteiramente anterior a (hoje - 24 meses)
3. verificar se finding_event_default acumulou linhas demais
   (indica evento com data fora de faixa — investigar)
```

Criação de partição:

```sql
CREATE TABLE IF NOT EXISTS finding_event_2026_09
    PARTITION OF finding_event
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
```

---

## 15. Fuso horário

**Regra absoluta:** gravar sempre em UTC (`timestamptz`, como o Tenable manda).
Converter para `America/Sao_Paulo` **apenas na leitura**.

**NÃO DEVE** converter na gravação — perde-se a referência original e o horário
de verão de terceiros vira bagunça.

Todas as views de consumo e todo agrupamento por dia usam:

```sql
(occurred_at AT TIME ZONE 'America/Sao_Paulo')::date AS dia
```

Sem isso, um finding indexado às 23h de 31/08 em Brasília cai em setembro no
relatório.

---

## 16. Views de consumo

São o contrato de leitura. Consumidores **DEVEM** ler das views, não das tabelas
base, para que mudanças de regra fiquem em um lugar só.

```sql
-- Findings ativos, com janela de exibição por produto
-- (30 dias para infra, 7 dias para WAS — é regra de EXIBIÇÃO, não de dado:
--  o banco guarda tudo, a janela é filtro de leitura)
CREATE VIEW vw_finding_ativo AS
SELECT c.*,
       r.quadrant, r.py, r.px, r.engine_version,
       p.solution, p.description, p.cve
FROM   finding_current c
LEFT   JOIN finding_risk r USING (finding_id)
LEFT   JOIN plugin p USING (plugin_id)
WHERE  c.deleted_at IS NULL
  AND  c.state <> 'FIXED'
  AND  ( (c.product = 'VM'  AND c.last_found >= now() - interval '30 days')
      OR (c.product = 'WAS' AND c.last_found >= now() - interval '7 days') );


-- Eventos do dia, em horário de São Paulo
CREATE VIEW vw_evento_diario AS
SELECT (occurred_at AT TIME ZONE 'America/Sao_Paulo')::date AS dia,
       product, event_type, count(*) AS total
FROM   finding_event
GROUP  BY 1, 2, 3;


-- Abertura x fechamento por mês (base do gráfico de tendência)
CREATE VIEW vw_tendencia_mensal AS
SELECT date_trunc('month', occurred_at AT TIME ZONE 'America/Sao_Paulo') AS mes,
       product,
       count(*) FILTER (WHERE event_type IN ('OPENED','REOPENED')) AS abertos,
       count(*) FILTER (WHERE event_type = 'FIXED')                AS fechados,
       count(*) FILTER (WHERE event_type = 'DELETED')              AS excluidos
FROM   finding_event
GROUP  BY 1, 2;


-- Saúde do pipeline
CREATE VIEW vw_pipeline_saude AS
SELECT (SELECT mode FROM pipeline_control WHERE id = 1)          AS modo,
       (SELECT cutoff_at FROM pipeline_control WHERE id = 1)     AS corte,
       (SELECT max(processed_at) FROM ingest_file)               AS ultima_ingestao,
       (SELECT count(*) FROM ingest_file WHERE status='QUARANTINED') AS quarentena,
       (SELECT count(*) FROM finding_current
         WHERE state <> 'FIXED' AND deleted_at IS NULL)          AS abertos;
```

**Nota para os consumidores:** o gráfico de tendência **NÃO DEVE** exibir
períodos anteriores a `pipeline_control.cutoff_at`. Antes do corte, existem
aberturas retroativas sem os fechamentos correspondentes, o que faz parecer que
o backlog explodiu e ninguém remediou.

---

## 17. Estrutura de código

### 17.1 Layout

Este pipeline entra como módulo do pipeline CLI Python já existente
(`main.py run`), reaproveitando config, logging e empacotamento.

```
ingestion/
├── __init__.py
├── cli.py              # comandos: run, set-mode, reprocess, status
├── config.py           # env vars, whitelist de tipos, versões esperadas
├── s3.py               # listagem de manifests, download, validação md5
├── manifest.py         # parse do manifest, ordenação
├── payload.py          # gunzip, parse, achatamento por payload_type
│   ├── flatten_finding()          # VM
│   ├── flatten_was_finding()      # WAS
│   ├── flatten_enriched()         # recast
│   └── flatten_plugin()
├── loader.py           # COPY + transação + orquestração dos SQL
├── partitions.py       # criar/dropar partições
├── metrics.py          # publicação no CloudWatch
└── sql/
    ├── 00_staging.sql
    ├── 10_dedup.sql
    ├── 20_events.sql
    ├── 30_upsert_plugin.sql
    ├── 40_upsert_current.sql
    ├── 50_upsert_recast.sql
    └── 60_mark_file.sql

migrations/             # Alembic
tests/
├── fixtures/           # os payloads de exemplo reais
├── test_flatten.py
├── test_events.py      # todas as transições da seção 8.2
└── test_idempotency.py # o teste da seção 10.1
```

### 17.2 Regras de implementação

- **Python orquestra, SQL move dado.** O diff nunca vive em Python: não carregar
  os 500 mil findings em memória para comparar. O `LEFT JOIN` da staging contra
  `finding_current` faz isso com índice.
- **`psycopg3` com `COPY`** para a carga. SQLAlchemy/Alembic apenas para schema
  e queries leves — **NÃO DEVE** ser usado no caminho quente (ORM linha a linha
  com 500 mil registros leva horas).
- **Leitura em streaming.** `gzip.open` + gerador alimentando o `COPY` sob
  demanda. Memória constante independente do tamanho do payload.
- **SQL em arquivos `.sql`**, não em f-string no meio do Python. As regras de
  evento vão ser ajustadas várias vezes; o diff no git precisa ser legível.
- **Uma função de achatamento por `payload_type`**, mesma assinatura. É o único
  lugar do código que conhece o formato do Tenable.

### 17.3 Configuração

| Variável | Descrição | Exemplo |
|---|---|---|
| `TENABLE_BUCKET` | bucket do Data Stream | `empresa-tenable-ds` |
| `TENABLE_PREFIX` | prefixo dentro do bucket | `prod` |
| `PG_DSN` | conexão PostgreSQL | `host=... dbname=vuln` |
| `INGESTION_MODE` | sobrescreve `pipeline_control` (só para teste) | `SEED` |
| `MAX_ATTEMPTS` | tentativas antes de quarentena | `3` |
| `EXPECTED_SCHEMA_VERSION` | versão esperada por tipo | `{"FINDING": 1, ...}` |
| `CLOUDWATCH_NAMESPACE` | namespace das métricas | `TenableIngestion` |
| `RETENTION_MONTHS` | retenção de eventos | `24` |

---

## 18. Plano de execução

### Fase 0 — Validação local (1 dia)

1. Subir PostgreSQL em Docker local
2. Aplicar o DDL da seção 5.2
3. Implementar `flatten_finding()` e `flatten_was_finding()` usando os payloads
   de exemplo reais como fixture
4. `pytest tests/test_flatten.py` verde

### Fase 1 — Loader (2 dias)

5. Implementar `s3.py`, `manifest.py`, `loader.py`
6. Rodar `--seed --limit 1` contra o bucket real
7. Validar: linha em `ingest_file`, linhas em `finding_current`, zero eventos
8. `pytest tests/test_idempotency.py` verde

### Fase 2 — Seed completo (2–3 dias, majoritariamente espera)

9. Rodar seed sobre tudo que há no bucket
10. Medir a curva de inéditos por dia (seção 9.4)
11. Comparar `count(*) FROM finding_current` com o console do Tenable
12. Rodar a query de duplicata por `natural_key`

### Fase 3 — Eventos (2 dias)

13. Implementar `20_events.sql` completo
14. `pytest tests/test_events.py` cobrindo as 6 regras da seção 8.2
15. Testar em base local com transições sintéticas

### Fase 4 — Corte e produção (1 dia + espera)

16. Quando a curva estabilizar: `set-mode INCREMENTAL`
17. Implementar partições, métricas e alarmes
18. Empacotar como CronJob no EKS com `concurrencyPolicy: Forbid`
19. Pedir acesso ao PostgreSQL de HML

### Critérios de aceite

- [ ] Reprocessar todo o bucket duas vezes produz estado e contagem idênticos
- [ ] Nenhum `finding_id` duplicado em `finding_current`
- [ ] Todo evento tem `occurred_at` vindo do dado, não do relógio do job
- [ ] `deletes[]` gera `DELETED`, nunca `FIXED`
- [ ] `host_audit_finding` e `tds_test_file` não são ingeridos
- [ ] Payload corrompido vai para quarentena e a fila continua
- [ ] Alarme dispara quando o último manifest tem mais de 6 horas
- [ ] Partições do mês corrente e seguinte existem após cada execução
- [ ] Views retornam dia em `America/Sao_Paulo`

---

## 19. Fora de escopo

O sistema **NÃO DEVE** implementar nada desta lista. Se surgir necessidade,
é conversa nova.

| Item | Responsável |
|---|---|
| API REST sobre o banco | outro time |
| Dashboard web | outro time (em andamento em paralelo) |
| **Motor de recálculo de risco** | implementado em `risk/`, como CronJob separado ([motor.md](motor.md)). Fora do escopo **deste job**: a ingestão não o executa, não o importa e não calcula risco |
| Crawler de tickets Jira | outro time; lê `finding_current` e compara com o Jira por `finding_id` |
| Enriquecimento com CMDB (sigla, unidade de negócio, tribo) | feito pelo motor, em tabelas próprias ([motor.md §4.2](motor.md) e [§4.4](motor.md)); a ingestão segue sem join com CMDB |
| Enriquecimento com Jira e marcações de negócio | ainda sem dono |
| Controle de acesso, mascaramento, auditoria de leitura | quem constrói em cima do banco |
| Ingestão de `asset`, `was_asset`, `tags`, `asset_enriched_attributes`, `host_audit_finding` | fase futura, se necessário |
| Geração do CSV do Power BI | script separado, já previsto |

### Nota sobre a fronteira de segurança

O banco vai concentrar dados sensíveis: portas abertas por host, hostnames
internos, IPs, evidências de PII, proofs de exploração. Isso é uma superfície de
exposição maior que o CSV atual.

**A responsabilidade deste projeto termina no `GRANT` do banco.** Controle de
acesso por perfil, o que a IA recebe no contexto e log de consulta são
responsabilidade de quem constrói os consumidores. Esta nota existe para que a
fronteira fique registrada, não para transferir culpa.

---

## 20. Pendências conhecidas

Não invente solução para estas. Implemente o comportamento descrito e mantenha
a pendência visível.

| ID | Pendência | Situação | Ação |
|---|---|---|---|
| **P1** | Dono da reconciliação semanal | indefinido | decisão de gestão; o relatório é gerado de qualquer forma |
| **P2** | Comportamento quando um recast é removido no Tenable | não verificado | teste empírico: deletar uma regra de recast em finding de teste e observar se cai payload novo no bucket nas horas seguintes. Se cair, o espelhamento resolve sozinho. Se não cair, precisa de plano B |
| **P3** | `indexed` dos findings do backfill: vem original ou reindexado? | não verificado | pegar no bucket um finding com `first_found` de 2024 e olhar o `indexed`. Se vier original, a linha do tempo nasce correta; se vier de agosto/2026, depender de `first_found`/`last_found` para histórico |
| **P4** | Divergência de contagem banco vs console | conhecida | a Fase 2 do plano mede isso; resolver antes do go-live |
| **P5** | Frequência ideal do job | 1x/dia por escolha | reavaliar após medir a duração real do ciclo |

---

## 21. Decisões arquiteturais registradas

Resumo do que foi decidido e por quê. Serve para não reabrir discussão.

| Decisão | Alternativa descartada | Motivo |
|---|---|---|
| `finding_id` como PK | chave composta natural | UUIDv5 é determinístico e estável na origem |
| Tabela única VM+WAS com `product` | duas tabelas | evento é o mesmo conceito; consumidores precisam ver junto |
| Coluna `product`, não `source` | `source` | `source` já é campo nativo do Tenable (AGENT/NESSUS/NNM) |
| `plugin` em tabela separada | tudo no finding | 41–66% do tamanho, idêntico por `plugin_id` |
| `plugin_id` + `plugin_name` denormalizados | só o join | evita join na maioria das queries; custo desprezível |
| Guardar `output` | descartar | é a evidência; ticket e IA precisam |
| `raw jsonb` no finding | só colunas | campo novo sem reingerir o S3 |
| `indexed` como relógio | `last_found` | `last_found` tem defasagem de horas vs indexação |
| `>` estrito no upsert | `>=` | protege contra backfill fora de ordem e reprocesso |
| Ingestão pelo manifest | listar o bucket | manifest dá ordem, md5 e contagem |
| Dois modos (seed/incremental) | modo único | snapshot não é sequência de mudanças; evitaria evento falso em massa |
| Evento datado pelo dado | datado pela ingestão | "quando fechou" ≠ "quando meu job viu fechar" |
| `finding_event` particionada | tabela simples | expurgo por `DROP` em vez de `DELETE` de milhões |
| `finding_risk` separada | coluna no finding | cadência independente; `engine_version` auditável; motor intocado |
| Motor em CronJob separado | acoplado no mesmo job | peso muda toda semana e recalcular não pode arrastar a ingestão; falha no scoring não pode barrar o dado; `engine_version` reverte sozinha (revisto — ver [motor.md §3.1](motor.md)) |
| Ingestão sem cálculo de risco | ingestão enriquecida | mudança de fórmula não exige reingerir o S3 |
| Whitelist de 3 tipos | loop genérico | evita `tds_test_file` e mistura de compliance |
| UTC no banco, SP na leitura | converter na escrita | preserva referência original |
