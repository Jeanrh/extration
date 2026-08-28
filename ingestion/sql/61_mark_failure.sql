-- Registro de falha de um payload (seção 12.2).
--
-- Roda numa transação PRÓPRIA, depois do ROLLBACK do arquivo — se rodasse
-- dentro da transação abortada, sumiria junto com ela e o pipeline ficaria
-- preso no mesmo arquivo indefinidamente, sem ninguém perceber.
--
-- Política de tentativas:
--   tentativa 1 falha → FAILED, tenta de novo no próximo ciclo
--   tentativa 2 falha → FAILED
--   tentativa N = MAX_ATTEMPTS → QUARANTINED, alerta, e a fila SEGUE
--
-- Um buraco conhecido e alarmado é melhor que uma fila parada em silêncio.

INSERT INTO ingest_file (
    path, payload_type, manifest_path, md5, schema_version,
    num_updates, num_deletes, first_record_timestamp,
    last_record_timestamp, scan_id, status, attempt_count, error_message,
    mode, processed_at
)
VALUES (
    %(path)s, %(payload_type)s, %(manifest_path)s, %(md5)s, %(schema_version)s,
    %(num_updates)s, %(num_deletes)s, %(first_record_timestamp)s,
    %(last_record_timestamp)s, %(scan_id)s,
    CASE WHEN 1 >= %(max_attempts)s THEN 'QUARANTINED' ELSE 'FAILED' END,
    1, %(error_message)s, %(mode)s, now()
)
ON CONFLICT (path) DO UPDATE SET
    status = CASE
                 WHEN ingest_file.attempt_count + 1 >= %(max_attempts)s
                 THEN 'QUARANTINED'
                 ELSE 'FAILED'
             END,
    attempt_count = ingest_file.attempt_count + 1,
    error_message = EXCLUDED.error_message,
    first_record_timestamp = EXCLUDED.first_record_timestamp,
    last_record_timestamp  = EXCLUDED.last_record_timestamp,
    scan_id                = EXCLUDED.scan_id,
    mode          = EXCLUDED.mode,
    processed_at  = now()
RETURNING status, attempt_count;
