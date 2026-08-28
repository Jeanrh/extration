-- Aplicação dos `deletes[]` (seção 6.7).
--
-- Arquivo que não está na lista da seção 17.1, e o motivo está registrado no
-- README (Desvios): o SQL de exemplo da seção 8.5 passa as linhas de delete
-- pelo mesmo INSERT de estado, mas elas trazem só o ID e o `deleted_at`. Sem
-- `state` e sem `indexed` — ambos NOT NULL — o INSERT aborta a transação, e a
-- guarda `EXCLUDED.indexed > f.indexed` nunca deixaria a marca ser gravada de
-- qualquer forma. Então o delete vira um UPDATE dedicado.
--
-- O que este SQL garante, que é o que a spec exige:
--   * `deleted_at` recebe a data do payload;
--   * `state` NÃO é tocado — delete não é remediação, e virar FIXED inflaria a
--     métrica de remediação com trabalho que ninguém fez;
--   * finding desconhecido é no-op (não há dado para criar a linha) — coerente
--     com a regra 5 da seção 8.2, que também exige a linha já existir.
--
-- Roda DEPOIS de 20_events.sql (o evento DELETED precisa ver deleted_at ainda
-- nulo) e DEPOIS de 40_upsert_current.sql.

UPDATE finding_current f
SET    deleted_at       = d.deleted_at,
       last_ingested_at = now()
FROM   (
    -- último delete de cada finding dentro do arquivo
    SELECT DISTINCT ON (finding_id) finding_id, deleted_at, seq
    FROM   stg_finding
    WHERE  is_delete = true
    ORDER  BY finding_id, seq DESC
) d
WHERE  f.finding_id = d.finding_id
  AND  f.deleted_at IS DISTINCT FROM d.deleted_at
  -- se o mesmo arquivo trouxe um update DEPOIS do delete, o finding voltou:
  -- o upsert já limpou a marca e reaplicá-la aqui seria andar para trás.
  AND  NOT EXISTS (
           SELECT 1 FROM stg_finding u
           WHERE  u.finding_id = d.finding_id
             AND  u.is_delete = false
             AND  u.seq > d.seq
       );
