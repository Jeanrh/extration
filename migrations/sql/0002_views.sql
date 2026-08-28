-- ============================================================================
-- VIEWS DE CONSUMO  (SPEC seção 16)
--
-- São o contrato de leitura. Consumidores DEVEM ler das views, não das tabelas
-- base, para que mudanças de regra fiquem em um lugar só.
--
-- Regra de fuso (seção 15): o banco guarda UTC; a conversão para
-- America/Sao_Paulo acontece SÓ aqui, na leitura.
-- ============================================================================

-- Findings ativos, com janela de exibição por produto (30 dias para infra,
-- 7 dias para WAS). É regra de EXIBIÇÃO, não de dado: o banco guarda tudo,
-- a janela é filtro de leitura.
CREATE OR REPLACE VIEW vw_finding_ativo AS
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


-- Eventos do dia, em horário de São Paulo.
CREATE OR REPLACE VIEW vw_evento_diario AS
SELECT (occurred_at AT TIME ZONE 'America/Sao_Paulo')::date AS dia,
       product, event_type, count(*) AS total
FROM   finding_event
GROUP  BY 1, 2, 3;


-- Abertura x fechamento por mês (base do gráfico de tendência).
-- NOTA PARA OS CONSUMIDORES: o gráfico NÃO DEVE exibir períodos anteriores a
-- pipeline_control.cutoff_at. Antes do corte existem aberturas retroativas sem
-- os fechamentos correspondentes.
CREATE OR REPLACE VIEW vw_tendencia_mensal AS
SELECT date_trunc('month', occurred_at AT TIME ZONE 'America/Sao_Paulo') AS mes,
       product,
       count(*) FILTER (WHERE event_type IN ('OPENED','REOPENED')) AS abertos,
       count(*) FILTER (WHERE event_type = 'FIXED')                AS fechados,
       count(*) FILTER (WHERE event_type = 'DELETED')              AS excluidos
FROM   finding_event
GROUP  BY 1, 2;


-- Saúde do pipeline.
CREATE OR REPLACE VIEW vw_pipeline_saude AS
SELECT (SELECT mode FROM pipeline_control WHERE id = 1)          AS modo,
       (SELECT cutoff_at FROM pipeline_control WHERE id = 1)     AS corte,
       (SELECT max(processed_at) FROM ingest_file)               AS ultima_ingestao,
       (SELECT count(*) FROM ingest_file WHERE status='QUARANTINED') AS quarentena,
       (SELECT count(*) FROM finding_current
         WHERE state <> 'FIXED' AND deleted_at IS NULL)          AS abertos;


-- Duplicatas por chave natural (seção 5.4). Não é contrato de consumo: é a
-- query da reconciliação semanal (seção 13.3), exposta como view para não
-- ficar solta em documento.
CREATE OR REPLACE VIEW vw_duplicata_natural_key AS
SELECT natural_key,
       count(*)              AS ids_distintos,
       array_agg(finding_id) AS finding_ids
FROM   finding_current
WHERE  deleted_at IS NULL AND state <> 'FIXED'
GROUP  BY natural_key
HAVING count(*) > 1;
