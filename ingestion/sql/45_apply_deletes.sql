-- Aplica somente o tombstone mais novo de cada finding.
--
-- O relógio do delete é deleted_at, com indexed (fallback do manifest) como
-- reserva. Um delete aceito avança finding_current.indexed, mas nunca altera
-- state. Assim, somente um update estritamente posterior pode ressuscitar a
-- linha. O empate só é aceito quando o update efetivo veio antes no mesmo
-- staging, conforme registrado por 40_upsert_current.sql.
-- Tombstone posterior a um finding já apagado também avança o relógio, sem novo
-- DELETED: 20_events.sql registra só a transição lógica deleted_at NULL -> valor.

WITH ultimo_delete AS (
    SELECT DISTINCT ON (finding_id)
           finding_id,
           seq,
           COALESCE(deleted_at, indexed) AS delete_clock
    FROM stg_finding
    WHERE is_delete = true
      AND COALESCE(deleted_at, indexed) IS NOT NULL
    ORDER BY finding_id, COALESCE(deleted_at, indexed) DESC, seq DESC
)
UPDATE finding_current f
SET    deleted_at       = d.delete_clock,
       indexed          = d.delete_clock,
       last_ingested_at = now()
FROM ultimo_delete d
WHERE f.finding_id = d.finding_id
  AND (
      d.delete_clock > f.indexed
      OR (
          d.delete_clock = f.indexed
          AND EXISTS (
              SELECT 1
              FROM stg_effective_finding_update u
              WHERE u.finding_id = d.finding_id
                AND u.indexed = d.delete_clock
                AND u.seq < d.seq
          )
      )
  );
