-- Tabelas de staging de um payload. Vivem dentro da transação do arquivo:
-- ON COMMIT DROP garante que nada sobra se a transação abortar.
--
-- `seq` é a posição do registro dentro do payload. Existe para dar desempate
-- determinístico no dedup e na ordem de aplicação dos deletes — `ctid` também
-- funcionaria, mas é detalhe físico do Postgres e não sobrevive a um
-- reescrita da tabela.
--
-- A ordem das colunas é a ordem dos campos dos dataclasses em payload.py.
-- `tests/test_flatten.py::test_staging_bate_com_dataclass` guarda esse acordo.

CREATE TEMP TABLE stg_finding (
    seq                         integer,
    finding_id                  text,
    product                     text,
    is_delete                   boolean,
    state                       text,
    severity                    text,
    severity_id                 smallint,
    severity_default_id         smallint,
    severity_modification_type  text,
    recast_reason               text,
    recast_rule_uuid            text,
    plugin_id                   bigint,
    plugin_name                 text,
    asset_uuid                  text,
    asset_fqdn                  text,
    asset_hostname              text,
    asset_ipv4                  text,
    asset_ipv6                  text,
    asset_mac_address           text,
    asset_operating_system      text[],
    asset_device_type           text,
    asset_agent_uuid            text,
    asset_network_id            text,
    asset_tracked               boolean,
    port_number                 integer,
    port_protocol               text,
    port_service                text,
    url                         text,
    input_type                  text,
    input_name                  text,
    http_method                 text,
    output                      text,
    proof                       text,
    payload                     text,
    first_found                 timestamptz,
    last_found                  timestamptz,
    last_fixed                  timestamptz,
    last_observed               timestamptz,
    resurfaced_date             timestamptz,
    time_taken_to_fix           bigint,
    indexed                     timestamptz,
    scan_uuid                   text,
    scan_schedule_uuid          text,
    scan_started_at             timestamptz,
    scan_completed_at           timestamptz,
    scan_target                 text,
    source                      text,
    natural_key                 text,
    deleted_at                  timestamptz,
    raw                         jsonb
) ON COMMIT DROP;

CREATE TEMP TABLE stg_plugin (
    seq                     integer,
    plugin_id               bigint,
    indexed                 timestamptz,
    name                    text,
    family                  text,
    risk_factor             text,
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
    raw                     jsonb
) ON COMMIT DROP;

CREATE TEMP TABLE stg_recast (
    seq                 integer,
    finding_id          text,
    is_delete           boolean,
    source              text,
    rule_id             text,
    rule_comment        text,
    modification        text,
    modification_target text,
    recasted_severity   text,
    changed_result      text,
    rule_created_at     timestamptz,
    rule_updated_at     timestamptz,
    source_indexed      timestamptz,
    deleted_at          timestamptz,
    raw                 jsonb
) ON COMMIT DROP;
