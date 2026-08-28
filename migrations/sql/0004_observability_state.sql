ALTER TABLE pipeline_control
    ADD COLUMN IF NOT EXISTS last_findings_open bigint;
