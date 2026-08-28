-- Motor de eventos (seção 8.4). Roda só em modo INCREMENTAL.
--
-- Cada bloco é uma regra da tabela 8.2. São blocos separados com UNION ALL,
-- e NÃO um CASE único, de propósito: a regra 6 (RECAST_CHANGED) é independente
-- das demais e pode ocorrer junto com qualquer uma. Um CASE devolve um evento
-- por linha e perderia o segundo.
--
-- `occurred_at` sai sempre do dado (quando aconteceu de verdade); `detected_at`
-- é o now() do default da coluna (quando o pipeline soube). Relatório usa
-- occurred_at.
--
-- O ON CONFLICT DO NOTHING cai sobre ux_finding_event_dedup
-- (finding_id, event_type, occurred_at) — camada 4 da seção 10: reprocessar o
-- mesmo payload não duplica evento.
--
-- Este SQL roda ANTES do upsert de estado: os LEFT JOIN/JOIN contra
-- finding_current enxergam o estado anterior ao arquivo. É o diff, e ele vive
-- aqui, no índice, nunca em memória Python.

INSERT INTO finding_event
    (finding_id, product, event_type, occurred_at, old_state, new_state,
     old_value, new_value, source_path, scan_id)

-- 1. inédito aberto
SELECT s.finding_id, s.product, 'OPENED',
       COALESCE(s.first_found, s.indexed), NULL, s.state,
       NULL, to_jsonb(s.state), %(source_path)s, %(scan_id)s
FROM   stg_finding s
LEFT   JOIN finding_current c USING (finding_id)
WHERE  c.finding_id IS NULL AND s.is_delete = false AND s.state <> 'FIXED'

UNION ALL
-- 2a. inédito já fechado → OPENED retroativo (seção 8.3)
--     Sem isto o fechamento some da estatística: não gera OPENED (chegou
--     fechado) nem FIXED (não havia linha aberta antes). É este bloco que
--     exige a partição DEFAULT — um OPENED de 2019 não cabe nos meses recentes.
SELECT s.finding_id, s.product, 'OPENED',
       COALESCE(s.first_found, s.indexed), NULL, 'OPEN',
       NULL, to_jsonb('OPEN'::text), %(source_path)s, %(scan_id)s
FROM   stg_finding s
LEFT   JOIN finding_current c USING (finding_id)
WHERE  c.finding_id IS NULL AND s.is_delete = false
  AND  s.state = 'FIXED' AND s.first_found IS NOT NULL

UNION ALL
-- 2b. inédito já fechado → FIXED, com a data real do fechamento
SELECT s.finding_id, s.product, 'FIXED',
       COALESCE(s.last_fixed, s.indexed), NULL, s.state,
       NULL, to_jsonb(s.state), %(source_path)s, %(scan_id)s
FROM   stg_finding s
LEFT   JOIN finding_current c USING (finding_id)
WHERE  c.finding_id IS NULL AND s.is_delete = false AND s.state = 'FIXED'

UNION ALL
-- 2c. inédito chegando REOPENED → o REOPENED que acompanha o OPENED da
--     regra 1. Está no texto da seção 8.3 ("o mesmo vale para inédito
--     chegando REOPENED"), mas ficou de fora do SQL de exemplo da 8.4.
SELECT s.finding_id, s.product, 'REOPENED',
       COALESCE(s.resurfaced_date, s.last_found, s.indexed), NULL, s.state,
       NULL, to_jsonb(s.state), %(source_path)s, %(scan_id)s
FROM   stg_finding s
LEFT   JOIN finding_current c USING (finding_id)
WHERE  c.finding_id IS NULL AND s.is_delete = false AND s.state = 'REOPENED'

UNION ALL
-- 3. reaberto
SELECT s.finding_id, s.product, 'REOPENED',
       COALESCE(s.resurfaced_date, s.last_found, s.indexed), c.state, s.state,
       to_jsonb(c.state), to_jsonb(s.state), %(source_path)s, %(scan_id)s
FROM   stg_finding s
JOIN   finding_current c USING (finding_id)
WHERE  s.is_delete = false AND c.state = 'FIXED' AND s.state <> 'FIXED'

UNION ALL
-- 4. fechado
SELECT s.finding_id, s.product, 'FIXED',
       COALESCE(s.last_fixed, s.indexed), c.state, s.state,
       to_jsonb(c.state), to_jsonb(s.state), %(source_path)s, %(scan_id)s
FROM   stg_finding s
JOIN   finding_current c USING (finding_id)
WHERE  s.is_delete = false AND c.state <> 'FIXED' AND s.state = 'FIXED'

UNION ALL
-- 5. excluído do Tenable — NÃO é remediação (seção 6.7). Se virasse FIXED, a
--    métrica de remediação inflaria com trabalho que ninguém fez.
SELECT s.finding_id, s.product, 'DELETED',
       COALESCE(s.deleted_at, s.indexed), c.state, NULL,
       to_jsonb(c.state), NULL, %(source_path)s, %(scan_id)s
FROM   stg_finding s
JOIN   finding_current c USING (finding_id)
WHERE  s.is_delete = true AND c.deleted_at IS NULL

UNION ALL
-- 6. recast alterado. Independente das anteriores: pode sair junto com
--    qualquer uma delas. A fonte primária é o próprio finding, que já traz
--    severity_modification_type (seção 8.6).
SELECT s.finding_id, s.product, 'RECAST_CHANGED',
       s.indexed, NULL, NULL,
       to_jsonb(c.severity_modification_type),
       to_jsonb(s.severity_modification_type), %(source_path)s, %(scan_id)s
FROM   stg_finding s
JOIN   finding_current c USING (finding_id)
WHERE  s.is_delete = false
  AND  c.severity_modification_type IS DISTINCT FROM s.severity_modification_type

ON CONFLICT DO NOTHING;
