-- ----------------------------------------------------------------------------
-- 0009 — Equipe solucionadora e a view de export
--
-- O consumo do motor e um EXPORT: o time filtra a sigla dele e baixa o CSV, ou
-- baixa a base inteira. `vw_finding_export` e esse contrato.
--
-- Tres decisoes que estao na forma da view:
--
-- 1. NAO filtra estado nem tempo. FIXED e finding antigo entram. Filtrar e
--    trabalho da consulta, nao do contrato — uma view que ja vem filtrada so
--    responde uma pergunta, e mudar o recorte viraria deploy de SQL. O unico
--    corte e `deleted_at`: o que o Tenable removeu na origem nao e backlog.
--
-- 2. `plugin_id` e `plugin_output` sao obrigatorios. Sem o primeiro o time nao
--    rastreia a vulnerabilidade de volta ao Tenable; sem o segundo nao le a
--    evidencia que o scan produziu.
--
-- 3. Colunas listadas uma a uma, nunca `SELECT *`. Entre as tres tabelas ha
--    exatamente tres nomes repetidos — `finding_id` e `plugin_id` (que sao as
--    proprias chaves de juncao, mesmo valor dos dois lados) e `raw`, que fica
--    de fora. Listar resolve a colisao e congela o contrato: acrescentar coluna
--    na tabela base nao muda o CSV do time sem alguem decidir.
--
-- Sobre duplicacao: juntar tres tabelas NAO multiplica linha aqui, porque os
-- dois lados direitos sao chave primaria (`finding_risk.finding_id` e
-- `plugin.plugin_id`). LEFT JOIN contra PK casa no maximo uma linha. Isso esta
-- provado em tests/test_export.py, nao assumido.
-- ----------------------------------------------------------------------------

-- 1. EQUIPE SOLUCIONADORA
--
-- Vem do campo `team` da sigla no CMDB ("Plataforma de Deploy"), nao do
-- cockpit — o cockpit da tribo e alianca. Foi removida por engano na 0008,
-- quando o contrato foi enxugado sem checar que o dashboard usava.

ALTER TABLE cmdb_acronym ADD COLUMN IF NOT EXISTS equipe_solucionadora text;
ALTER TABLE finding_risk ADD COLUMN IF NOT EXISTS equipe_solucionadora text;


-- 2. VIEW DE EXPORT

CREATE OR REPLACE VIEW vw_finding_export AS
SELECT fc.finding_id,
       -- rastreabilidade de volta ao Tenable
       fc.plugin_id,
       fc.plugin_name,
       fc.output                              AS plugin_output,
       fc.proof,                              -- evidencia do WAS
       -- estado nativo
       fc.product,
       fc.state,
       fc.severity,
       -- ativo
       COALESCE(fc.asset_hostname, fc.url)    AS asset_name,
       fc.asset_fqdn,
       fc.asset_ipv4,
       fc.asset_uuid,
       -- contexto de negocio, resolvido pelo motor
       r.sigla,
       r.equipe_solucionadora,
       r.tribo,
       r.unidade_negocio,
       r.pci,
       r.bia,
       r.criticality_cmdb,
       -- vulnerabilidade
       p.cvss3_base_score,
       p.exploitability_ease,
       array_to_string(p.cve, ';')            AS cve,
       r.layer,
       r.familia,
       r.arch_type,
       -- as oito notas: a linha explica sozinha por que a prioridade e essa
       r.nota_bia, r.nota_pci, r.nota_exposure, r.nota_arch,
       r.nota_cvss, r.nota_threat, r.nota_exploit, r.nota_layer,
       -- veredito
       r.py, r.px,
       r.priority_id,
       r.priority_name,
       r.quadrant,
       r.sla_status,
       r.aging,
       -- datas
       fc.first_found,
       fc.last_found,
       fc.last_fixed,
       -- procedencia: qual versao do motor gerou esta linha, e quando
       r.engine_version,
       r.computed_at
  FROM finding_current fc
  LEFT JOIN finding_risk r ON r.finding_id = fc.finding_id
  LEFT JOIN plugin       p ON p.plugin_id  = fc.plugin_id
 WHERE fc.deleted_at IS NULL;

COMMENT ON VIEW vw_finding_export IS
    'Contrato de export dos times. Uma linha por finding nao deletado, em '
    'qualquer estado e sem janela de tempo. Filtrar por sigla, estado ou data '
    'e trabalho da consulta.';
