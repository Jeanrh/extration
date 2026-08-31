-- ----------------------------------------------------------------------------
-- 0008 — Unidade de negócio e tribo
--
-- Domínio e subdomínio saem: ninguém prioriza por eles. No lugar entram os dois
-- campos que o negócio usa para endereçar um finding, ao lado da sigla:
--
--     servidor/url -> sigla -> unidade de negócio (aliança) + tribo
--
-- A ligação com o cockpit é resolvida **no sync**, em memória: o `teamid` da
-- sigla ("OR-345014") casa com a `key` do objeto de cockpit. Por isso não há
-- coluna de id nem tabela de cockpit aqui — id de junção é plumbing, e plumbing
-- não vira coluna. O efeito colateral é bom: a leitura dos ~500 mil findings
-- perde um JOIN.
--
-- Unidade de negócio é a **aliança** do cockpit, não a VP.
-- ----------------------------------------------------------------------------

-- 1. CMDB_ACRONYM — a sigla carrega o resultado já resolvido.

ALTER TABLE cmdb_acronym DROP COLUMN IF EXISTS domain;
ALTER TABLE cmdb_acronym DROP COLUMN IF EXISTS subdomain;
ALTER TABLE cmdb_acronym DROP COLUMN IF EXISTS equipe_sol;

ALTER TABLE cmdb_acronym
    ADD COLUMN IF NOT EXISTS unidade_negocio text,
    ADD COLUMN IF NOT EXISTS tribo           text;


-- 2. CMDB_TEAM sai de cena.
--
-- Com a resolução no sync, ninguém mais lê a tabela. Mantê-la seria snapshot
-- sem consumidor: mais uma carga para falhar e mais um TRUNCATE por execução,
-- em troca de nada.

DROP TABLE IF EXISTS cmdb_team;


-- 3. FINDING_RISK — a tribo viaja até o consumidor.
--
-- `unidade_negocio` já existia, mas vinha da VP; passa a vir da aliança. O
-- valor antigo é limpo para não conviverem duas semânticas na mesma coluna até
-- o próximo recálculo — que reescreve todas as linhas de qualquer forma.

ALTER TABLE finding_risk ADD COLUMN IF NOT EXISTS tribo text;

UPDATE finding_risk SET unidade_negocio = NULL WHERE unidade_negocio IS NOT NULL;
