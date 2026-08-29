-- RISK_CHANGED: a mudança de prioridade calculada pelo motor.
--
-- A severidade que o negócio monitora é esta, não a nativa do Tenable. O
-- evento entra em `finding_event` para reaproveitar o particionamento mensal e
-- a retenção que a tabela já tem, em vez de abrir histórico paralelo.
--
-- Roda ANTES do upsert: depois, o estado antigo já teria sido sobrescrito.
-- JOIN (não LEFT JOIN) de propósito — a primeira pontuação de um finding não é
-- "mudança de risco", é o risco nascendo.

INSERT INTO finding_event (
    finding_id, product, event_type, occurred_at, old_value, new_value
)
SELECT s.finding_id,
       s.product,
       'RISK_CHANGED',
       now(),
       jsonb_build_object(
           'priority_name', r.priority_name, 'quadrant', r.quadrant,
           'py', r.py, 'px', r.px
       ),
       jsonb_build_object(
           'priority_name', s.priority_name, 'quadrant', s.quadrant,
           'py', s.py, 'px', s.px
       )
  FROM stg_risk s
  JOIN finding_risk r ON r.finding_id = s.finding_id
 WHERE r.priority_id IS DISTINCT FROM s.priority_id
    OR r.quadrant    IS DISTINCT FROM s.quadrant
-- Replay no mesmo instante não duplica (ux_finding_event_dedup).
ON CONFLICT (finding_id, event_type, occurred_at) DO NOTHING
