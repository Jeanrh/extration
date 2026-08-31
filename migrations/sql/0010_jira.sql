-- ----------------------------------------------------------------------------
-- 0010 — Vínculo finding → ticket do Jira
--
-- O Jira não sabe que o finding existe. A chave do card ("GVUL-123") não tem
-- relação com o `finding_id` (UUID v5 do Tenable); o que liga os dois é a
-- string `Finding ID: <uuid>` escrita dentro da descrição do card.
--
-- A tabela é chaveada por TICKET, não por finding. Parece invertido, mas é o
-- que faz o cache funcionar: guardando o `updated` de cada card, o sync só
-- rebusca a description (1 requisição JQL por 100 cards) do que mudou. Se a
-- chave fosse `finding_id`, os cards SEM carimbo ficariam fora da tabela e
-- seriam rebuscados em toda execução, para sempre — justamente o desperdício
-- que o cache existe para evitar.
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS jira_ticket (
    ticket_id    text PRIMARY KEY,          -- "GVUL-123"
    finding_id   text,                      -- UUID extraído da descrição; '' se não há
    status       text,
    action_plan  text,
    updated      text,                      -- carimbo do Jira; é o cache
    collected_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_jira_finding ON jira_ticket (finding_id)
    WHERE finding_id IS NOT NULL AND finding_id <> '';


-- ----------------------------------------------------------------------------
-- A view de export ganha as três colunas do ticket.
--
-- `DISTINCT ON` porque nada no Jira impede dois cards de citarem o mesmo
-- finding — acontece quando um card é fechado sem resolver e outro é aberto
-- para a mesma vulnerabilidade. Sem isso o finding DUPLICARIA no export e o
-- time trabalharia a mesma linha duas vezes.
--
-- Vence o alterado mais recentemente, não o de chave maior: `ticket_id` é
-- texto, e em ordem de texto "GVUL-100" vem antes de "GVUL-99". Ordenar por
-- `updated` é determinístico e escolhe o card onde alguém mexeu por último —
-- que é onde o plano de ação está atualizado.
-- ----------------------------------------------------------------------------

-- DROP + CREATE, nao CREATE OR REPLACE: o Postgres so deixa ACRESCENTAR
-- coluna no fim de uma view existente, e as tres do Jira entram no meio,
-- junto do resto do atendimento. Dentro da transacao da migracao nao ha
-- janela em que a view falte para quem le.
DROP VIEW IF EXISTS vw_finding_export;

CREATE VIEW vw_finding_export AS
WITH ticket_por_finding AS (
    SELECT DISTINCT ON (finding_id)
           finding_id, ticket_id, status, action_plan
      FROM jira_ticket
     WHERE finding_id IS NOT NULL AND finding_id <> ''
     ORDER BY finding_id, updated DESC NULLS LAST, ticket_id
)
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
       -- atendimento
       j.ticket_id                            AS jira_ticket_id,
       j.status                               AS jira_status,
       j.action_plan                          AS jira_action_plan,
       -- datas
       fc.first_found,
       fc.last_found,
       fc.last_fixed,
       -- procedencia: qual versao do motor gerou esta linha, e quando
       r.engine_version,
       r.computed_at
  FROM finding_current fc
  LEFT JOIN finding_risk       r ON r.finding_id = fc.finding_id
  LEFT JOIN plugin             p ON p.plugin_id  = fc.plugin_id
  LEFT JOIN ticket_por_finding j ON j.finding_id = fc.finding_id
 WHERE fc.deleted_at IS NULL;

COMMENT ON VIEW vw_finding_export IS
    'Contrato de export dos times. Uma linha por finding nao deletado, em '
    'qualquer estado e sem janela de tempo. Filtrar por sigla, estado ou data '
    'e trabalho da consulta.';
