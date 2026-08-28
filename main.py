"""Wrapper de compatibilidade para executar a CLI do pipeline."""

from ingestion.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
