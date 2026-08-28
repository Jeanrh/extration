-- Marca o payload como processado — camada 1 das quatro da seção 10.
--
-- Roda dentro da MESMA transação do arquivo: ou o payload entra inteiro
-- (estado + eventos + esta marca), ou não entra nada. Nunca sobra estado meio
-- aplicado (seção 12.1).
--
-- O ON CONFLICT cobre o reprocesso manual e a retentativa de um arquivo que
-- havia falhado: a linha existe com status FAILED e passa a OK.

INSERT INTO ingest_file (
    path, payload_type, manifest_path, md5, schema_version,
    num_updates, num_deletes, rows_read, events_generated,
    first_record_timestamp, last_record_timestamp, scan_id,
    status, attempt_count, error_message, mode, processed_at
)
VALUES (
    %(path)s, %(payload_type)s, %(manifest_path)s, %(md5)s, %(schema_version)s,
    %(num_updates)s, %(num_deletes)s, %(rows_read)s, %(events_generated)s,
    %(first_record_timestamp)s, %(last_record_timestamp)s, %(scan_id)s,
    'OK', 1, NULL, %(mode)s, now()
)
ON CONFLICT (path) DO UPDATE SET
    payload_type           = EXCLUDED.payload_type,
    manifest_path          = EXCLUDED.manifest_path,
    md5                    = EXCLUDED.md5,
    schema_version         = EXCLUDED.schema_version,
    num_updates            = EXCLUDED.num_updates,
    num_deletes            = EXCLUDED.num_deletes,
    rows_read              = EXCLUDED.rows_read,
    events_generated       = EXCLUDED.events_generated,
    first_record_timestamp = EXCLUDED.first_record_timestamp,
    last_record_timestamp  = EXCLUDED.last_record_timestamp,
    scan_id                = EXCLUDED.scan_id,
    status                 = 'OK',
    attempt_count          = ingest_file.attempt_count + 1,
    error_message          = NULL,
    mode                   = EXCLUDED.mode,
    processed_at           = now();
