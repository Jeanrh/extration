FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --gid 10001 ingestion \
    && useradd --uid 10001 --gid 10001 --no-create-home \
        --shell /usr/sbin/nologin ingestion

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=10001:10001 ingestion/ ingestion/
# O motor de risco roda da mesma imagem, com outro entrypoint. Sem isto o
# CronJob de `python -m risk.cli` falharia — inclusive o arquivo de referência
# de arquitetura, que o motor lê do disco.
COPY --chown=10001:10001 risk/ risk/
COPY --chown=10001:10001 migrations/ migrations/
COPY --chown=10001:10001 alembic.ini ./

USER 10001:10001

ENTRYPOINT ["python", "-m", "ingestion.cli"]
CMD ["run"]
