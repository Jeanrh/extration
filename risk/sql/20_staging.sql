-- Staging do veredito. TEMP com PRESERVE ROWS: o cálculo entra em vários
-- lotes com autocommit, e só a gravação final é transacional.

DROP TABLE IF EXISTS stg_risk;

CREATE TEMP TABLE stg_risk (
    finding_id        text PRIMARY KEY,
    product           text,
    py                numeric,
    px                numeric,
    quadrant          text,
    priority_id       smallint,
    priority_name     text,
    sla_status        text,
    aging             integer,
    nota_bia          smallint,
    nota_pci          smallint,
    nota_exposure     smallint,
    nota_arch         smallint,
    nota_cvss         smallint,
    nota_threat       smallint,
    nota_exploit      smallint,
    nota_layer        smallint,
    sigla             text,
    pci               text,
    bia               text,
    criticality_cmdb  text,
    unidade_negocio   text,
    tribo             text,
    equipe_solucionadora text,
    arch_type         text,
    layer             text,
    familia           text,
    engine_version    text,
    context_synced_at timestamptz
) ON COMMIT PRESERVE ROWS;
