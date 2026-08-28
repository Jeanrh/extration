-- Upsert do stream enriched → finding_recast (seção 7.3).
--
-- Upsert por finding_id, SEM foreign key para finding_current: o recast pode
-- chegar antes do finding correspondente e não deve ser descartado. É o que
-- tira o stream enriched do caminho crítico de ordem.

-- 1) updates: o detalhe da regra de recast.
INSERT INTO finding_recast AS r (
    finding_id, source, rule_id, rule_comment, modification, modification_target,
    recasted_severity, changed_result, rule_created_at, rule_updated_at,
    source_indexed, deleted_at, raw, ingested_at
)
SELECT DISTINCT ON (finding_id)
       finding_id, source, rule_id, rule_comment, modification, modification_target,
       recasted_severity, changed_result, rule_created_at, rule_updated_at,
       source_indexed, NULL, raw, now()
FROM   stg_recast
WHERE  is_delete = false
ORDER  BY finding_id, source_indexed DESC NULLS LAST, seq DESC
ON CONFLICT (finding_id) DO UPDATE SET
    source              = EXCLUDED.source,
    rule_id             = EXCLUDED.rule_id,
    rule_comment        = EXCLUDED.rule_comment,
    modification        = EXCLUDED.modification,
    modification_target = EXCLUDED.modification_target,
    recasted_severity   = EXCLUDED.recasted_severity,
    changed_result      = EXCLUDED.changed_result,
    rule_created_at     = EXCLUDED.rule_created_at,
    rule_updated_at     = EXCLUDED.rule_updated_at,
    source_indexed      = EXCLUDED.source_indexed,
    -- a regra voltou a valer: limpa a marca de exclusão
    deleted_at          = NULL,
    raw                 = EXCLUDED.raw,
    ingested_at         = now()
-- Empate é replay e não move dados nem `ingested_at`.
WHERE  r.source_indexed IS NULL
    OR EXCLUDED.source_indexed > r.source_indexed;

-- 2) deletes: marcam a regra como removida sem apagar o detalhe já conhecido.
UPDATE finding_recast r
SET    deleted_at     = d.deleted_at,
       source_indexed = d.source_indexed,
       ingested_at    = now()
FROM   (
    SELECT DISTINCT ON (finding_id)
           finding_id, deleted_at, source_indexed, seq
    FROM   stg_recast
    WHERE  is_delete = true
    ORDER  BY finding_id, source_indexed DESC NULLS LAST, seq DESC
) d
WHERE  r.finding_id = d.finding_id
  AND  (r.source_indexed IS NULL OR d.source_indexed > r.source_indexed);
