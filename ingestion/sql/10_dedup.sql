-- Dedup intra-arquivo (seção 6.3) — camada 2 das quatro da seção 10.
--
-- O mesmo finding_id pode aparecer várias vezes no mesmo payload. Sobrevive o
-- de maior `indexed`; `seq` desempata quando o relógio empata (acontece: o
-- fallback do manifest é o mesmo para o arquivo inteiro).
--
-- ORDEM IMPORTA: em modo INCREMENTAL este DELETE roda **depois** da geração de
-- eventos. Se um finding abriu e fechou dentro da mesma janela de 15 minutos,
-- descartar o intermediário antes perderia o par OPENED+FIXED. Raro em VM,
-- menos raro em WAS (rescan rápido).
--
-- Só linhas de update entram aqui. As de `deletes[]` são resolvidas em
-- 45_apply_deletes.sql, que já escolhe a última por finding_id — elas não
-- carregam relógio próprio e não podem competir por `indexed` com um update.

DELETE FROM stg_finding a
USING  stg_finding b
WHERE  a.finding_id = b.finding_id
  AND  a.is_delete = false
  AND  b.is_delete = false
  AND  (a.indexed, a.seq) < (b.indexed, b.seq);
