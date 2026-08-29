-- Grava o veredito — e SÓ o que mudou.
--
-- Reescrever 500 mil linhas por dia quando quase nada muda geraria 500 mil
-- tuplas mortas diárias, bloat e pressão de autovacuum. O `IS DISTINCT FROM`
-- no WHERE final é o que transforma um recálculo completo numa escrita
-- proporcional à mudança real.

INSERT INTO finding_risk AS r (
    finding_id, py, px, quadrant, priority_id, priority_name, sla_status, aging,
    nota_bia, nota_pci, nota_exposure, nota_arch,
    nota_cvss, nota_threat, nota_exploit, nota_layer,
    sigla, pci, bia, criticality_cmdb, unidade_negocio,
    arch_type, layer, familia,
    engine_version, context_synced_at, computed_at
)
SELECT finding_id, py, px, quadrant, priority_id, priority_name, sla_status, aging,
       nota_bia, nota_pci, nota_exposure, nota_arch,
       nota_cvss, nota_threat, nota_exploit, nota_layer,
       sigla, pci, bia, criticality_cmdb, unidade_negocio,
       arch_type, layer, familia,
       engine_version, context_synced_at, now()
  FROM stg_risk
ON CONFLICT (finding_id) DO UPDATE SET
    py                = EXCLUDED.py,
    px                = EXCLUDED.px,
    quadrant          = EXCLUDED.quadrant,
    priority_id       = EXCLUDED.priority_id,
    priority_name     = EXCLUDED.priority_name,
    sla_status        = EXCLUDED.sla_status,
    aging             = EXCLUDED.aging,
    nota_bia          = EXCLUDED.nota_bia,
    nota_pci          = EXCLUDED.nota_pci,
    nota_exposure     = EXCLUDED.nota_exposure,
    nota_arch         = EXCLUDED.nota_arch,
    nota_cvss         = EXCLUDED.nota_cvss,
    nota_threat       = EXCLUDED.nota_threat,
    nota_exploit      = EXCLUDED.nota_exploit,
    nota_layer        = EXCLUDED.nota_layer,
    sigla             = EXCLUDED.sigla,
    pci               = EXCLUDED.pci,
    bia               = EXCLUDED.bia,
    criticality_cmdb  = EXCLUDED.criticality_cmdb,
    unidade_negocio   = EXCLUDED.unidade_negocio,
    arch_type         = EXCLUDED.arch_type,
    layer             = EXCLUDED.layer,
    familia           = EXCLUDED.familia,
    engine_version    = EXCLUDED.engine_version,
    context_synced_at = EXCLUDED.context_synced_at,
    computed_at       = now()
WHERE  (r.py, r.px, r.quadrant, r.priority_id, r.priority_name, r.sla_status,
        r.aging, r.nota_bia, r.nota_pci, r.nota_exposure, r.nota_arch,
        r.nota_cvss, r.nota_threat, r.nota_exploit, r.nota_layer,
        r.sigla, r.pci, r.bia, r.criticality_cmdb, r.unidade_negocio,
        r.arch_type, r.layer, r.familia, r.engine_version)
   IS DISTINCT FROM
       (EXCLUDED.py, EXCLUDED.px, EXCLUDED.quadrant, EXCLUDED.priority_id,
        EXCLUDED.priority_name, EXCLUDED.sla_status, EXCLUDED.aging,
        EXCLUDED.nota_bia, EXCLUDED.nota_pci, EXCLUDED.nota_exposure,
        EXCLUDED.nota_arch, EXCLUDED.nota_cvss, EXCLUDED.nota_threat,
        EXCLUDED.nota_exploit, EXCLUDED.nota_layer,
        EXCLUDED.sigla, EXCLUDED.pci, EXCLUDED.bia, EXCLUDED.criticality_cmdb,
        EXCLUDED.unidade_negocio, EXCLUDED.arch_type, EXCLUDED.layer,
        EXCLUDED.familia, EXCLUDED.engine_version)
