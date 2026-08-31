-- Um finding com todo o contexto que o scoring precisa, já resolvido.
--
-- Sem filtro de tempo e sem filtro de estado: OPEN, REOPENED e FIXED entram.
-- O único corte é `deleted_at IS NULL` — o que o Tenable removeu na origem não
-- é backlog de ninguém.
--
-- Paginação por chave (o último finding_id lido, com ORDER BY finding_id) em
-- vez de cursor server-side: a conexão do projeto é autocommit, e keyset
-- mantém a memória constante sem exigir uma transação aberta durante todo o
-- cálculo. Atenção: o psycopg conta os marcadores mesmo dentro de comentário,
-- então nada de escrever o marcador literal aqui.

WITH servidor_por_ip AS (
    -- `ipv4` não é único no CMDB. Escolher determinístico evita que o mesmo
    -- finding troque de sigla entre execuções sem nada ter mudado.
    SELECT DISTINCT ON (ipv4) ipv4, sigla
      FROM   cmdb_server
     WHERE   ipv4 IS NOT NULL AND ipv4 <> ''
     ORDER   BY ipv4, hostname
)
SELECT fc.finding_id,
       fc.product,
       -- No WAS o "asset_name" é a URL: é nela que a arquitetura procura "api"
       -- para decidir entre API (80) e App/Web (100).
       CASE WHEN fc.product = 'WAS' THEN COALESCE(fc.url, '')
            ELSE COALESCE(fc.asset_hostname, '') END          AS asset_name,
       p.cvss3_base_score,
       p.exploitability_ease,
       COALESCE(pl.layer, '')                                 AS layer,
       COALESCE(pl.familia, '')                               AS familia,
       fc.first_found,
       COALESCE(sv.sigla, ip.sigla, u.sigla, '')              AS sigla,
       COALESCE(a.pci, '')                                    AS pci,
       COALESCE(a.bia, '')                                    AS bia,
       COALESCE(a.criticality, '')                            AS criticality_cmdb,
       COALESCE(a.unidade_negocio, '')                        AS unidade_negocio,
       COALESCE(a.tribo, '')                                  AS tribo,
       COALESCE(a.equipe_solucionadora, '')                   AS equipe_solucionadora,
       COALESCE(ar.arquitetura, '')                           AS arquitetura,
       (ti.finding_id IS NOT NULL)                            AS em_threat_intel
  FROM finding_current fc
  LEFT JOIN plugin          p  ON p.plugin_id  = fc.plugin_id
  LEFT JOIN plugin_layer    pl ON pl.plugin_id = fc.plugin_id
  -- VM: hostname primeiro, IPv4 como último recurso (mesma ordem do extraction)
  LEFT JOIN cmdb_server     sv ON fc.product = 'VM'
                              AND sv.hostname = upper(fc.asset_hostname)
  LEFT JOIN servidor_por_ip ip ON fc.product = 'VM'
                              AND sv.hostname IS NULL
                              AND ip.ipv4 = fc.asset_ipv4
  -- WAS: pelo fqdn
  LEFT JOIN cmdb_url        u  ON fc.product = 'WAS'
                              AND u.url = upper(fc.asset_fqdn)
  LEFT JOIN cmdb_acronym    a  ON a.sigla = COALESCE(sv.sigla, ip.sigla, u.sigla)
  LEFT JOIN architecture    ar ON ar.sigla = COALESCE(sv.sigla, ip.sigla, u.sigla)
  LEFT JOIN threat_intel    ti ON ti.finding_id = fc.finding_id
 WHERE fc.deleted_at IS NULL
   AND fc.finding_id > %s
 ORDER BY fc.finding_id
 LIMIT %s
