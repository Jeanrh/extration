-- ----------------------------------------------------------------------------
-- 0005 — Motor de risco: o veredito e o evento de mudança de prioridade
--
-- O Tenable não é a fonte de verdade de risco desta empresa. A `finding_risk`
-- nasceu vazia na 0001 justamente para receber o cálculo próprio (spec §5.3);
-- esta migração dá a ela o formato que o motor precisa escrever.
-- ----------------------------------------------------------------------------

-- 1. FINDING_RISK — a linha explica sozinha a prioridade
--
-- As oito notas ficam gravadas de propósito. Sem elas, responder "por que este
-- finding é Muito Alta?" exige reexecutar o motor, que a essa altura já pode
-- estar rodando com outros pesos. O contexto resolvido (sigla, PCI, BIA...)
-- entra pelo mesmo motivo: é o insumo que produziu o vetor py.

ALTER TABLE finding_risk
    -- veredito
    ADD COLUMN IF NOT EXISTS priority_id       smallint,
    ADD COLUMN IF NOT EXISTS priority_name     text,
    ADD COLUMN IF NOT EXISTS sla_status        text,
    ADD COLUMN IF NOT EXISTS aging             integer,
    -- vetor py (ativo)
    ADD COLUMN IF NOT EXISTS nota_bia          smallint,
    ADD COLUMN IF NOT EXISTS nota_pci          smallint,
    ADD COLUMN IF NOT EXISTS nota_exposure     smallint,
    ADD COLUMN IF NOT EXISTS nota_arch         smallint,
    -- vetor px (vulnerabilidade)
    ADD COLUMN IF NOT EXISTS nota_cvss         smallint,
    ADD COLUMN IF NOT EXISTS nota_threat       smallint,
    ADD COLUMN IF NOT EXISTS nota_exploit      smallint,
    ADD COLUMN IF NOT EXISTS nota_layer        smallint,
    -- contexto resolvido que produziu as notas
    ADD COLUMN IF NOT EXISTS sigla             text,
    ADD COLUMN IF NOT EXISTS pci               text,
    ADD COLUMN IF NOT EXISTS bia               text,
    ADD COLUMN IF NOT EXISTS criticality_cmdb  text,
    ADD COLUMN IF NOT EXISTS unidade_negocio   text,
    ADD COLUMN IF NOT EXISTS arch_type         text,
    ADD COLUMN IF NOT EXISTS layer             text,
    ADD COLUMN IF NOT EXISTS familia           text,
    -- proveniência: qual snapshot de contexto gerou este score
    ADD COLUMN IF NOT EXISTS context_synced_at timestamptz;

CREATE INDEX IF NOT EXISTS ix_risk_priority ON finding_risk (priority_id);
CREATE INDEX IF NOT EXISTS ix_risk_sla      ON finding_risk (sla_status);
CREATE INDEX IF NOT EXISTS ix_risk_sigla    ON finding_risk (sigla);


-- 2. FINDING_EVENT — RISK_CHANGED
--
-- A severidade que o negócio monitora é a do motor, não a do Tenable. A
-- mudança de prioridade entra como evento de primeira classe para reaproveitar
-- o particionamento e a retenção que a tabela já tem, em vez de abrir uma
-- tabela de histórico paralela.

ALTER TABLE finding_event DROP CONSTRAINT IF EXISTS finding_event_type_ck;
ALTER TABLE finding_event ADD CONSTRAINT finding_event_type_ck CHECK (event_type IN (
    'OPENED', 'REOPENED', 'FIXED', 'DELETED', 'RECAST_CHANGED', 'RISK_CHANGED'
));


-- 3. PLUGIN.EXPLOITABILITY_EASE — promovido do `raw`
--
-- Alimenta a nota_exploit. Fica como TEXTO, não boolean: a regra é "vazio ou
-- 'no known exploit' vale 10, qualquer outro texto vale 100", e o Data Stream
-- manda `null` onde a API clássica manda string. Promover é o caminho que a
-- spec §5.3 previu para campo não modelado — e aqui é barato, porque `plugin`
-- tem uma linha por plugin, não por finding.

ALTER TABLE plugin ADD COLUMN IF NOT EXISTS exploitability_ease text;

UPDATE plugin
   SET exploitability_ease = raw ->> 'exploitability_ease'
 WHERE exploitability_ease IS NULL
   AND raw ? 'exploitability_ease';
