"""Exportador CSV legado do Tenable Data Stream.

Antecede o pipeline PostgreSQL em `ingestion/` e é mantido apenas para quem
ainda consome os CSVs. Não participa dos CronJobs do EKS nem compartilha
estado com a ingestão: lê o mesmo bucket e escreve arquivos.
"""
