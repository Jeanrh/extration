-- Upsert de estado (seção 8.5) — camada 3 das quatro da seção 10.
--
-- Só linhas de update. As de `deletes[]` vão para 45_apply_deletes.sql: elas
-- trazem apenas o ID e o `deleted_at`, então não têm `state` nem `indexed`
-- (ambos NOT NULL aqui) e nunca passariam pela guarda de ordem. Passá-las por
-- este INSERT quebraria a transação inteira num arquivo legítimo.
--
-- A guarda `EXCLUDED.indexed > f.indexed` é ESTRITAMENTE maior, por dois
-- motivos (seção 6.4):
--   1. backfill misturado com fluxo corrente — uma versão antiga do finding
--      pode chegar depois da atual, e sem a guarda o estado anda para trás;
--   2. reprocesso — `>` estrito descarta um registro idêntico já aplicado, que
--      é o que faz o teste de idempotência da seção 10.1 passar.
--
-- `last_found` NÃO serve de relógio: no WAS real ele fica horas atrás do
-- `indexed_at` (10:19 vs 12:19 no payload de exemplo).

-- Materializa a decisão enquanto o baseline anterior ainda está visível.
-- O delete posterior reutiliza esta tabela para distinguir um update realmente
-- aceito de um replay empatado que o ON CONFLICT descartou.
CREATE TEMP TABLE stg_effective_finding_update ON COMMIT DROP AS
SELECT s.*
FROM stg_finding s
LEFT JOIN finding_current c USING (finding_id)
WHERE s.is_delete = false
  AND (c.finding_id IS NULL OR s.indexed > c.indexed);

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
       scan_target, source, natural_key, NULL, raw
FROM   stg_effective_finding_update
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
    asset_uuid                 = EXCLUDED.asset_uuid,
    asset_fqdn                 = EXCLUDED.asset_fqdn,
    asset_hostname             = EXCLUDED.asset_hostname,
    asset_ipv4                 = EXCLUDED.asset_ipv4,
    asset_ipv6                 = EXCLUDED.asset_ipv6,
    asset_mac_address          = EXCLUDED.asset_mac_address,
    asset_operating_system     = EXCLUDED.asset_operating_system,
    asset_device_type          = EXCLUDED.asset_device_type,
    asset_agent_uuid           = EXCLUDED.asset_agent_uuid,
    asset_network_id           = EXCLUDED.asset_network_id,
    asset_tracked              = EXCLUDED.asset_tracked,
    port_number                = EXCLUDED.port_number,
    port_protocol              = EXCLUDED.port_protocol,
    port_service               = EXCLUDED.port_service,
    url                        = EXCLUDED.url,
    input_type                 = EXCLUDED.input_type,
    input_name                 = EXCLUDED.input_name,
    http_method                = EXCLUDED.http_method,
    output                     = EXCLUDED.output,
    proof                      = EXCLUDED.proof,
    payload                    = EXCLUDED.payload,
    -- first_found NUNCA regride: mantém o mais antigo conhecido. LEAST ignora
    -- NULL, então um payload sem first_found não apaga o que já havia.
    first_found                = LEAST(f.first_found, EXCLUDED.first_found),
    last_found                 = EXCLUDED.last_found,
    last_fixed                 = EXCLUDED.last_fixed,
    last_observed              = EXCLUDED.last_observed,
    resurfaced_date            = EXCLUDED.resurfaced_date,
    time_taken_to_fix          = EXCLUDED.time_taken_to_fix,
    indexed                    = EXCLUDED.indexed,
    scan_uuid                  = EXCLUDED.scan_uuid,
    scan_schedule_uuid         = EXCLUDED.scan_schedule_uuid,
    scan_started_at            = EXCLUDED.scan_started_at,
    scan_completed_at          = EXCLUDED.scan_completed_at,
    scan_target                = EXCLUDED.scan_target,
    source                     = EXCLUDED.source,
    natural_key                = EXCLUDED.natural_key,
    -- ressurreição: um update depois de um delete limpa a marca. O sistema é
    -- espelho puro do Tenable — se o finding voltou a ser enviado, ele existe.
    deleted_at                 = NULL,
    raw                        = EXCLUDED.raw,
    last_ingested_at           = now()
WHERE  EXCLUDED.indexed > f.indexed;     -- ← GUARDA DE ORDEM (seção 6.4)
