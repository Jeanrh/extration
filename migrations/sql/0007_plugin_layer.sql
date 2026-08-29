-- ----------------------------------------------------------------------------
-- 0007 — Camada tecnológica derivada por plugin
--
-- A `nota_layer` do scoring casa keywords do Vault contra o nome do plugin.
-- Feito por finding, isso é uma busca de substring por linha, meio milhão de
-- vezes. Mas a camada não depende do finding: depende do plugin. Derivar uma
-- vez por plugin (dezenas de milhares) e materializar aqui transforma a nota
-- num JOIN.
--
-- `resolved_by` registra qual regra decidiu — é o que permite medir, depois de
-- uma rodada real, quanto da camada saiu do Vault e quanto caiu no fallback
-- por `plugin.family`.
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS plugin_layer (
    plugin_id       bigint PRIMARY KEY REFERENCES plugin(plugin_id) ON DELETE CASCADE,
    layer           text NOT NULL,
    familia         text NOT NULL,
    resolved_by     text NOT NULL,   -- plugin_name | family | nenhum
    computed_at     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT plugin_layer_resolved_by_ck
        CHECK (resolved_by IN ('plugin_name', 'family', 'nenhum'))
);

CREATE INDEX IF NOT EXISTS ix_plugin_layer_layer ON plugin_layer (layer);
