-- Upsert de plugin (seção 6.6).
--
-- O mesmo plugin aparece muitas vezes no mesmo lote, então o DISTINCT ON
-- deduplica antes de encostar na tabela. Vence o de maior `indexed` — ou seja,
-- a versão que o Tenable indexou por último.
--
-- Quando o Tenable atualiza a `solution` de um plugin, isto atualiza uma linha
-- em vez de dezenas de milhares.

INSERT INTO plugin AS p (
    plugin_id, name, family, risk_factor, type, synopsis, description, solution,
    see_also, cve, cwe, cpe,
    cvss2_base_score, cvss3_base_score, cvss4_base_score, epss_score, vpr_score,
    exploitability_ease, exploit_available, exploited_by_malware, in_the_news, has_patch,
    unsupported_by_vendor,
    publication_date, patch_publication_date, modification_date,
    source_indexed, raw, updated_at
)
SELECT DISTINCT ON (plugin_id)
       plugin_id, name, family, risk_factor, type, synopsis, description, solution,
       see_also, cve, cwe, cpe,
       cvss2_base_score, cvss3_base_score, cvss4_base_score, epss_score, vpr_score,
       exploitability_ease, exploit_available, exploited_by_malware, in_the_news, has_patch,
       unsupported_by_vendor,
       publication_date, patch_publication_date, modification_date,
       indexed, raw, now()
FROM   stg_plugin
WHERE  plugin_id IS NOT NULL
ORDER  BY plugin_id, indexed DESC NULLS LAST, seq DESC
ON CONFLICT (plugin_id) DO UPDATE SET
    name                   = EXCLUDED.name,
    family                 = EXCLUDED.family,
    risk_factor            = EXCLUDED.risk_factor,
    type                   = EXCLUDED.type,
    synopsis               = EXCLUDED.synopsis,
    description            = EXCLUDED.description,
    solution               = EXCLUDED.solution,
    see_also               = EXCLUDED.see_also,
    cve                    = EXCLUDED.cve,
    cwe                    = EXCLUDED.cwe,
    cpe                    = EXCLUDED.cpe,
    cvss2_base_score       = EXCLUDED.cvss2_base_score,
    cvss3_base_score       = EXCLUDED.cvss3_base_score,
    cvss4_base_score       = EXCLUDED.cvss4_base_score,
    epss_score             = EXCLUDED.epss_score,
    vpr_score              = EXCLUDED.vpr_score,
    exploitability_ease    = EXCLUDED.exploitability_ease,
    exploit_available      = EXCLUDED.exploit_available,
    exploited_by_malware   = EXCLUDED.exploited_by_malware,
    in_the_news            = EXCLUDED.in_the_news,
    has_patch              = EXCLUDED.has_patch,
    unsupported_by_vendor  = EXCLUDED.unsupported_by_vendor,
    publication_date       = EXCLUDED.publication_date,
    patch_publication_date = EXCLUDED.patch_publication_date,
    modification_date      = EXCLUDED.modification_date,
    source_indexed         = EXCLUDED.source_indexed,
    raw                    = EXCLUDED.raw,
    updated_at             = now()
-- Plugin legado não tem relógio confiável e aceita o primeiro registro.
-- Depois disso, empate é replay e não move dados nem `updated_at`.
WHERE  p.source_indexed IS NULL
    OR EXCLUDED.source_indexed > p.source_indexed;
