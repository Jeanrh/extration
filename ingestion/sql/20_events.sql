-- Motor de eventos incremental sobre uma timeline versionada.
--
-- O banco ainda contém o baseline anterior ao payload quando este SQL roda.
-- Cada versão estritamente posterior ao relógio persistido participa da
-- timeline em (indexed, seq); versões antigas ou empatadas são replay e não
-- geram história. A mudança de recast é independente das transições de state.

WITH updates_efetivos AS (
    SELECT s.*,
           c.state AS baseline_state,
           c.severity_modification_type AS baseline_recast,
           (c.finding_id IS NOT NULL) AS baseline_exists,
           row_number() OVER w AS rn,
           lag(s.state) OVER w AS lag_state,
           lag(s.severity_modification_type) OVER w AS lag_recast
    FROM stg_finding s
    LEFT JOIN finding_current c USING (finding_id)
    WHERE s.is_delete = false
      AND (c.finding_id IS NULL OR s.indexed > c.indexed)
    WINDOW w AS (PARTITION BY s.finding_id ORDER BY s.indexed, s.seq)
), timeline AS (
    SELECT *,
           CASE WHEN rn = 1 THEN baseline_state ELSE lag_state END AS old_state,
           CASE WHEN rn = 1 THEN baseline_recast ELSE lag_recast END AS old_recast
    FROM updates_efetivos
), ultimo_update_efetivo AS (
    SELECT DISTINCT ON (finding_id) *
    FROM timeline
    ORDER BY finding_id, indexed DESC, seq DESC
), ultimo_delete AS (
    SELECT DISTINCT ON (finding_id)
           s.*, COALESCE(s.deleted_at, s.indexed) AS delete_clock
    FROM stg_finding s
    WHERE s.is_delete = true
      AND COALESCE(s.deleted_at, s.indexed) IS NOT NULL
    ORDER BY finding_id, COALESCE(s.deleted_at, s.indexed) DESC, seq DESC
), deletes_efetivos AS (
    SELECT d.finding_id,
           COALESCE(u.product, c.product, d.product) AS product,
           d.delete_clock,
           COALESCE(u.state, c.state) AS old_state
    FROM ultimo_delete d
    LEFT JOIN finding_current c USING (finding_id)
    LEFT JOIN ultimo_update_efetivo u USING (finding_id)
    WHERE
        -- Um update efetivo no mesmo staging é o baseline lógico do delete.
        -- Sem ele, o delete precisa ser estritamente posterior ao persistido.
        (
            u.finding_id IS NOT NULL
            AND (
                d.delete_clock > u.indexed
                OR (d.delete_clock = u.indexed AND d.seq > u.seq)
            )
        )
        OR (
            u.finding_id IS NULL
            AND c.finding_id IS NOT NULL
            AND c.deleted_at IS NULL
            AND d.delete_clock > c.indexed
        )
)
INSERT INTO finding_event
    (finding_id, product, event_type, occurred_at, old_state, new_state,
     old_value, new_value, source_path, scan_id)

-- Finding inédito: toda primeira versão cria a abertura histórica.
SELECT t.finding_id, t.product, 'OPENED',
       COALESCE(t.first_found, t.indexed), NULL,
       CASE WHEN t.state = 'FIXED' THEN 'OPEN' ELSE t.state END,
       NULL::jsonb,
       to_jsonb(CASE WHEN t.state = 'FIXED' THEN 'OPEN' ELSE t.state END),
       %(source_path)s, %(scan_id)s
FROM timeline t
WHERE t.rn = 1 AND t.baseline_exists = false

UNION ALL
-- Inédito já FIXED: completa o par OPENED + FIXED.
SELECT t.finding_id, t.product, 'FIXED',
       COALESCE(t.last_fixed, t.indexed), NULL, t.state,
       NULL::jsonb, to_jsonb(t.state), %(source_path)s, %(scan_id)s
FROM timeline t
WHERE t.rn = 1 AND t.baseline_exists = false AND t.state = 'FIXED'

UNION ALL
-- Inédito já REOPENED: completa o par OPENED + REOPENED.
SELECT t.finding_id, t.product, 'REOPENED',
       COALESCE(t.resurfaced_date, t.last_found, t.indexed), NULL, t.state,
       NULL::jsonb, to_jsonb(t.state), %(source_path)s, %(scan_id)s
FROM timeline t
WHERE t.rn = 1 AND t.baseline_exists = false AND t.state = 'REOPENED'

UNION ALL
-- FIXED -> estado não-FIXED, inclusive entre versões do mesmo payload.
SELECT t.finding_id, t.product, 'REOPENED',
       COALESCE(t.resurfaced_date, t.last_found, t.indexed),
       t.old_state, t.state,
       to_jsonb(t.old_state), to_jsonb(t.state),
       %(source_path)s, %(scan_id)s
FROM timeline t
WHERE t.old_state = 'FIXED' AND t.state <> 'FIXED'

UNION ALL
-- Estado não-FIXED -> FIXED, inclusive entre versões do mesmo payload.
SELECT t.finding_id, t.product, 'FIXED',
       COALESCE(t.last_fixed, t.indexed), t.old_state, t.state,
       to_jsonb(t.old_state), to_jsonb(t.state),
       %(source_path)s, %(scan_id)s
FROM timeline t
WHERE t.old_state <> 'FIXED' AND t.state = 'FIXED'

UNION ALL
-- Delete é uma transição própria: nunca converte state em FIXED. Só a passagem
-- de ativo para apagado gera evento; tombstone posterior apenas confirma/avança
-- a versão persistida em 45_apply_deletes.sql.
SELECT d.finding_id, d.product, 'DELETED', d.delete_clock,
       d.old_state, NULL,
       to_jsonb(d.old_state), NULL::jsonb,
       %(source_path)s, %(scan_id)s
FROM deletes_efetivos d

UNION ALL
-- Recast é independente e pode coexistir com uma transição de state.
SELECT t.finding_id, t.product, 'RECAST_CHANGED', t.indexed,
       NULL, NULL,
       to_jsonb(t.old_recast), to_jsonb(t.severity_modification_type),
       %(source_path)s, %(scan_id)s
FROM timeline t
WHERE (t.baseline_exists OR t.rn > 1)
  AND t.old_recast IS DISTINCT FROM t.severity_modification_type

ON CONFLICT DO NOTHING;
