-- ============================================================================
-- SCHEMA: pipeline de ingestão Tenable Data Stream  (SPEC seção 5.2)
-- PostgreSQL 14+ (requer particionamento declarativo)
--
-- Este arquivo é a ÚNICA fonte de verdade do DDL. A revisão Alembic
-- migrations/versions/0001_initial_schema.py e o comando
-- `python -m ingestion.cli init-db` executam este mesmo arquivo.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- ----------------------------------------------------------------------------
-- 1. CONTROLE DE INGESTÃO
-- ----------------------------------------------------------------------------

-- Um registro por payload processado. É o mecanismo de idempotência de arquivo
-- (camada 1 das quatro da seção 10).
CREATE TABLE IF NOT EXISTS ingest_file (
    path                    text        PRIMARY KEY,
    payload_type            text        NOT NULL,
    manifest_path           text        NOT NULL,
    md5                     text,
    schema_version          integer,
    num_updates             integer,
    num_deletes             integer,
    rows_read               integer,
    events_generated        integer,
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

CREATE INDEX IF NOT EXISTS ix_ingest_file_status    ON ingest_file (status)
    WHERE status <> 'OK';
CREATE INDEX IF NOT EXISTS ix_ingest_file_processed ON ingest_file (processed_at);
CREATE INDEX IF NOT EXISTS ix_ingest_file_type      ON ingest_file (payload_type, processed_at);


-- Estado global do pipeline. Tabela de uma linha só.
CREATE TABLE IF NOT EXISTS pipeline_control (
    id                    integer     PRIMARY KEY DEFAULT 1,
    mode                  text        NOT NULL DEFAULT 'SEED',  -- SEED | INCREMENTAL
    cutoff_at             timestamptz,          -- "T": quando virou incremental
    last_manifest_seen_at timestamptz,          -- último manifest processado
    last_run_at           timestamptz,
    notes                 text,
    CONSTRAINT pipeline_control_single_row CHECK (id = 1),
    CONSTRAINT pipeline_control_mode_ck    CHECK (mode IN ('SEED','INCREMENTAL'))
);

INSERT INTO pipeline_control (id, mode) VALUES (1, 'SEED')
    ON CONFLICT (id) DO NOTHING;


-- ----------------------------------------------------------------------------
-- 2. PLUGIN (dados estáticos por plugin, evita repetir texto 500k vezes)
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS plugin (
    plugin_id               bigint      PRIMARY KEY,
    name                    text,
    family                  text,
    risk_factor             text,           -- normalizado para MAIÚSCULO
    type                    text,
    synopsis                text,
    description             text,
    solution                text,
    see_also                text[],
    cve                     text[],
    cwe                     text[],
    cpe                     text[],
    cvss2_base_score        numeric,
    cvss3_base_score        numeric,
    cvss4_base_score        numeric,
    epss_score              numeric,
    vpr_score               numeric,
    exploit_available       boolean,
    exploited_by_malware    boolean,
    in_the_news             boolean,
    has_patch               boolean,
    unsupported_by_vendor   boolean,
    publication_date        timestamptz,
    patch_publication_date  timestamptz,
    modification_date       timestamptz,
    raw                     jsonb       NOT NULL,   -- objeto plugin completo
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_plugin_cve ON plugin USING gin (cve);


-- ----------------------------------------------------------------------------
-- 3. FINDING_CURRENT (estado atual — uma linha por finding)
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS finding_current (
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
    indexed                 timestamptz NOT NULL,  -- RELÓGIO DE VERSÃO (seção 6.4)

    -- scan
    scan_uuid               text,
    scan_schedule_uuid      text,
    scan_started_at         timestamptz,
    scan_completed_at       timestamptz, -- só WAS
    scan_target             text,
    source                  text,        -- AGENT|NESSUS|NNM (campo do Tenable)

    -- controle interno
    natural_key             text        NOT NULL,  -- ver seção 5.4
    deleted_at              timestamptz,
    raw                     jsonb       NOT NULL,  -- registro completo
    first_ingested_at       timestamptz NOT NULL DEFAULT now(),
    last_ingested_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT finding_current_product_ck CHECK (product IN ('VM','WAS')),
    CONSTRAINT finding_current_state_ck   CHECK (state IN ('OPEN','REOPENED','FIXED'))
);

CREATE INDEX IF NOT EXISTS ix_fc_state        ON finding_current (state)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_fc_product      ON finding_current (product, state);
CREATE INDEX IF NOT EXISTS ix_fc_last_found   ON finding_current (last_found);
CREATE INDEX IF NOT EXISTS ix_fc_plugin       ON finding_current (plugin_id);
CREATE INDEX IF NOT EXISTS ix_fc_asset        ON finding_current (asset_uuid);
CREATE INDEX IF NOT EXISTS ix_fc_hostname     ON finding_current (asset_hostname);
CREATE INDEX IF NOT EXISTS ix_fc_natural_key  ON finding_current (natural_key);
CREATE INDEX IF NOT EXISTS ix_fc_indexed      ON finding_current (indexed);


-- ----------------------------------------------------------------------------
-- 4. FINDING_EVENT (histórico append-only, particionado por mês)
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS finding_event (
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

-- Idempotência de evento (camada 4): reprocessar o mesmo payload não duplica.
CREATE UNIQUE INDEX IF NOT EXISTS ux_finding_event_dedup
    ON finding_event (finding_id, event_type, occurred_at);

CREATE INDEX IF NOT EXISTS ix_fe_lookup  ON finding_event (event_type, occurred_at);
CREATE INDEX IF NOT EXISTS ix_fe_finding ON finding_event (finding_id, occurred_at);

-- Partição DEFAULT: captura eventos com occurred_at fora das faixas criadas
-- (ex.: OPENED retroativo de 2019 gerado pela regra 2 da seção 8.3).
-- Sem ela, o INSERT falha. As partições mensais são criadas pelo job
-- (ingestion/partitions.py, seção 14.3).
CREATE TABLE IF NOT EXISTS finding_event_default PARTITION OF finding_event DEFAULT;


-- ----------------------------------------------------------------------------
-- 5. FINDING_RECAST (detalhe do recast, vindo do stream enriched)
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS finding_recast (
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


-- ----------------------------------------------------------------------------
-- 6. FINDING_RISK (preenchida pelo MOTOR — fora do escopo deste projeto, mas a
--    tabela DEVE existir desde o dia 1 porque o contrato de consumo depende
--    dela; até o motor plugar, as views caem no `severity` nativo)
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS finding_risk (
    finding_id      text        PRIMARY KEY REFERENCES finding_current(finding_id)
                                ON DELETE CASCADE,
    quadrant        text,           -- Q1..Q16
    py              numeric,
    px              numeric,
    engine_version  text NOT NULL,
    computed_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_risk_quadrant ON finding_risk (quadrant);
