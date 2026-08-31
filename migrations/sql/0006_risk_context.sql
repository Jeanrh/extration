-- ----------------------------------------------------------------------------
-- 0006 — Contexto externo do motor de risco
--
-- O vetor py não sai do Tenable: BIA, PCI, criticidade e arquitetura vêm do
-- CMDB e de um CSV de referência; a nota de ameaça vem da API clássica. Nada
-- disso pode viver em arquivo no pod: CronJob roda em pod efêmero, então um
-- cache em disco morre com ele e o motor bateria no JSM toda execução.
--
-- Cada tabela é recarregada por inteiro dentro de UMA transação. Se a fonte
-- cair no meio, o snapshot anterior fica de pé e o motor calcula com contexto
-- de um ciclo atrás — nunca com contexto vazio, que produziria score plausível
-- e silenciosamente errado.
-- ----------------------------------------------------------------------------

-- Siglas: a entidade de negócio. Uma sigla carrega BIA, PCI e criticidade.
CREATE TABLE IF NOT EXISTS cmdb_acronym (
    sigla           text PRIMARY KEY,
    nome            text,
    status          text,
    pci             text,       -- "PCI" | "Escopo Estendido" | "Nao"
    bia             text,
    criticality     text,       -- Crise | Alto | Medio | Baixo
    equipe_sol      text,
    domain          text,
    subdomain       text,
    infrastructure  text
);

-- Servidores: resolvem o asset de VM para uma sigla.
--
-- `sigla` já vem resolvida do sync. O CMDB guarda aqui o *nome de exibição*
-- ("GTeC - Gestão de Terminais"), não o código ("GTEC"); traduzir na carga faz
-- o score de 500 mil findings virar JOIN, e deixa `acronym_raw` para auditar
-- de qual nome saiu cada código.
CREATE TABLE IF NOT EXISTS cmdb_server (
    hostname        text PRIMARY KEY,   -- UPPER, como o índice do extraction
    ipv4            text,
    sigla           text,
    acronym_raw     text,
    status          text,
    infrastructure  text,
    environment     text
);

CREATE INDEX IF NOT EXISTS ix_cmdb_server_ipv4 ON cmdb_server (ipv4)
    WHERE ipv4 IS NOT NULL AND ipv4 <> '';

-- URLs: resolvem o asset de WAS para uma sigla.
CREATE TABLE IF NOT EXISTS cmdb_url (
    url             text PRIMARY KEY,   -- UPPER
    sigla           text,
    acronym_raw     text,
    status          text,
    pci             text,
    alliance        text
);

-- Times Cockpit: tribo, aliança e VP a partir do time da sigla.
CREATE TABLE IF NOT EXISTS cmdb_team (
    nome            text PRIMARY KEY,   -- UPPER
    tribo           text,
    alianca         text,
    vp              text
);

-- Arquitetura: hoje um CSV mocado, mantido à mão e versionado no git.
-- Vira tabela para o JOIN, sem deixar de ser um arquivo que alguém edita num PR.
CREATE TABLE IF NOT EXISTS architecture (
    sigla           text PRIMARY KEY,
    arquitetura     text
);

-- Threat intel: o subconjunto que a API clássica devolve para o filtro
-- `cve_category`. É snapshot — substituído a cada execução —, então finding
-- fora da janela de 90 dias/OPEN volta a valer 10 na nota de ameaça.
CREATE TABLE IF NOT EXISTS threat_intel (
    finding_id      text PRIMARY KEY,
    collected_at    timestamptz NOT NULL DEFAULT now()
);

-- Procedência: quando cada fonte sincronizou e como terminou.
CREATE TABLE IF NOT EXISTS context_sync (
    source          text PRIMARY KEY,   -- CMDB | ARCHITECTURE | THREAT_INTEL
    synced_at       timestamptz NOT NULL DEFAULT now(),
    status          text NOT NULL,      -- OK | FAILED
    row_count       integer,
    detail          text,

    CONSTRAINT context_sync_status_ck CHECK (status IN ('OK', 'FAILED'))
);
