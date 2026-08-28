-- Relógios monotônicos dos alvos auxiliares.
--
-- Plugin legado fica NULL: `updated_at` é o relógio do nosso banco, não do
-- Tenable, portanto não é evidência segura para ordenar registros da origem.
ALTER TABLE plugin
    ADD COLUMN IF NOT EXISTS source_indexed timestamptz;

ALTER TABLE finding_recast
    ADD COLUMN IF NOT EXISTS source_indexed timestamptz;

-- Para recast, as datas persistidas são da própria origem. Tombstone vence
-- quando existe; depois valem a última atualização e a criação da regra.
UPDATE finding_recast
SET    source_indexed = COALESCE(deleted_at, rule_updated_at, rule_created_at)
WHERE  source_indexed IS NULL;
