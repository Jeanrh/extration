# Tenable Data Stream Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Entregar o pipeline Tenable Data Stream → PostgreSQL fiel à spec, com eventos ordenados, idempotência, memória limitada, recuperação segura, observabilidade e artefatos de deploy.

**Architecture:** O S3 continua sendo lido somente por manifests e cada payload continua isolado em uma transação PostgreSQL. O caminho quente baixa o `.json.gz` em chunks para arquivo temporário, valida MD5, percorre o JSON com `ijson` e alimenta staging via `psycopg3 COPY`; SQL ordena a timeline efetiva por `(indexed, seq)`, gera eventos e faz upserts protegidos por relógios estritos. O job termina com manutenção, reconciliação local, métricas CloudWatch e execução como CronJob EKS.

**Tech Stack:** Python 3.11+, boto3, ijson, psycopg 3, PostgreSQL 14+, Alembic/SQL, pytest, Docker, Kubernetes e AWS CloudFormation.

**Spec:** `SPEC-tenable-datastream-ingestion.md`

## Global Constraints

- Processar exatamente `FINDING`, `WAS_FINDING` e `FINDING_ENRICHED_ATTRIBUTES`; nunca descobrir tipos dinamicamente.
- Nunca ingerir `host_audit_finding` ou `tds_test_file`.
- Preservar a ordem das keys de manifest e a ordem literal de `payloads[]`; não paralelizar payloads.
- Usar uma transação por payload e registrar `ingest_file` na mesma transação dos dados.
- `SEED` popula estado sem eventos; `INCREMENTAL` gera os cinco tipos de evento definidos na spec.
- O relógio de finding é `indexed`/`indexed_at`, com fallback em `last_record_timestamp`; a guarda persistente é estritamente `>`.
- `occurred_at` sempre vem do dado; persistência em UTC e conversão para `America/Sao_Paulo` somente nas views.
- Python orquestra; SQL move e compara dados. Usar `psycopg3 COPY`, sem ORM no caminho quente.
- O uso de memória deve ser limitado e independente do tamanho descomprimido do payload.
- Nenhum código de risco, score, quadrante ou classificação de negócio entra na ingestão.
- Após `MAX_ATTEMPTS=3`, somente falhas atribuíveis ao payload viram `QUARANTINED`; falhas de banco, permissão, configuração ou programação abortam o job.
- `finding_event` permanece particionada mensalmente, com retenção de 24 meses; `ingest_file`, 90 dias.
- As pendências P1–P5 permanecem visíveis; nenhuma evidência de bucket, console ou HML deve ser simulada.

---

### Task 1: Baseline reproduzível e proteção do repositório

**Files:**
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `requirements-dev.txt`
- Create: `pytest.ini`
- Create: `main.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: código Python e testes existentes.
- Produces: instalação `pip install -r requirements-dev.txt`, comando `pytest` determinístico, `python main.py run` e entry point `tenable-ingestion = ingestion.cli:main`.

- [ ] **Step 1: Confirmar a falha comportamental inicial**

Run: `python -m pytest --collect-only -q`

Expected: FAIL com colisão entre `tests/` e `samples_s3/tests/`, demonstrando que o comando padrão ainda não é determinístico.

- [ ] **Step 2: Criar metadados e dependências concretos**

`requirements.txt` deve conter os floors `boto3>=1.36`, `python-dotenv>=1.0`, `psycopg[binary]>=3.2`, `alembic>=1.14`, `SQLAlchemy>=2.0` e `ijson>=3.3`. `requirements-dev.txt` inclui `-r requirements.txt`, `pytest>=8.3`, `PyYAML>=6.0` e `cfn-lint>=1.20`. `pytest.ini` define `testpaths = tests`, `norecursedirs = samples_s3 .git .venv` e registra o marker `banco`. `pyproject.toml` declara Python `>=3.11` e o script `tenable-ingestion`; `main.py` é um wrapper fino que termina com `raise SystemExit(main())`.

- [ ] **Step 3: Proteger artefatos locais**

`.gitignore` deve ignorar `.venv/`, `__pycache__/`, `.pytest_cache/`, `.env`, `*.py[cod]`, cobertura, CSVs gerados e diretórios temporários de PostgreSQL, sem ignorar fixtures JSON.

- [ ] **Step 4: Verificar os contratos pelo comportamento real**

Run: `python -m pytest --collect-only -q`

Expected: coleta somente a suíte principal, sem colisão.

Run: `python main.py --help`

Run: `python -m ingestion.cli --help`

Expected: ambos retornam código 0 e expõem os mesmos comandos.

Run: `python -m pytest -q`

Expected: 36+ testes passando, testes marcados `banco` pulados quando `TEST_PG_DSN` não estiver definido, e nenhuma colisão com `samples_s3/tests`.

- [ ] **Step 5: Commit do baseline**

```powershell
git add .
git commit -m "chore: establish ingestion project baseline"
```

---

### Task 2: PostgreSQL descartável e primeira execução real da suíte

**Files:**
- Create: `scripts/test-postgres.ps1`
- Create: `compose.yaml`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `TEST_PG_DSN` e executáveis PostgreSQL 14+.
- Produces: `scripts/test-postgres.ps1 -PostgresBin <dir> -Port 55432`, que cria cluster temporário com auth `trust`, executa pytest e sempre para/remove apenas o diretório temporário validado.

- [ ] **Step 1: Confirmar a falha inicial do comando desejado**

Run: `.\scripts\test-postgres.ps1 -PostgresBin 'C:\Program Files\PostgreSQL\18\bin' -Port 55432`

Expected: FAIL porque o harness ainda não existe.

- [ ] **Step 2: Implementar o harness seguro**

O script recebe `PostgresBin` e `Port`, cria um filho com GUID sob `[IO.Path]::GetTempPath()`, valida com `GetFullPath()` que o alvo permanece sob esse temp root, executa `initdb --auth=trust --encoding=UTF8`, inicia somente em `127.0.0.1`, cria o database `tenable_ingestion_test`, define `TEST_PG_DSN`, roda `python -m pytest -q` e, em `finally`, usa `pg_ctl stop` antes de remover o diretório exato. `compose.yaml` fornece alternativa PostgreSQL 16 com healthcheck e porta `55432`.

- [ ] **Step 3: Instalar dependências em `.venv` e executar a suíte PostgreSQL**

Run: `.venv\Scripts\python -m pip install -r requirements-dev.txt`

Run: `.\scripts\test-postgres.ps1 -PostgresBin 'C:\Program Files\PostgreSQL\18\bin' -Port 55432`

Expected: os testes atualmente corretos passam e as falhas reais do motor/eventos ficam registradas como baseline para Task 3.

- [ ] **Step 4: Verificar cleanup e alternativa Compose**

Após a execução, confirmar que o PID do servidor temporário terminou e que o diretório com GUID foi removido. Quando Docker estiver disponível, executar `docker compose config` e `docker compose up --wait postgres`; ausência local do Docker deve ser registrada como limitação ambiental.

- [ ] **Step 5: Commit do harness**

```powershell
git add scripts/test-postgres.ps1 compose.yaml tests/conftest.py
git commit -m "test: add disposable PostgreSQL harness"
```

---

### Task 3: Timeline SQL ordenada e deletes protegidos por versão

**Files:**
- Modify: `tests/test_events.py`
- Modify: `tests/test_idempotency.py`
- Modify: `ingestion/sql/20_events.sql`
- Modify: `ingestion/sql/40_upsert_current.sql`
- Modify: `ingestion/sql/45_apply_deletes.sql`

**Interfaces:**
- Consumes: `stg_finding(seq, finding_id, state, indexed, is_delete, deleted_at, severity_modification_type)` e baseline `finding_current`.
- Produces: eventos calculados sobre registros efetivos em `(indexed, seq)`, ignorando toda versão `indexed <= finding_current.indexed`; delete aceito avança `finding_current.indexed` e nunca altera `state`.

- [ ] **Step 1: Adicionar testes vermelhos de ordem e replay**

```python
def test_existing_open_fixed_reopened_no_mesmo_payload(ingestor, conn):
    # baseline OPEN; payload posterior contém FIXED e REOPENED.
    # Resultado obrigatório: eventos FIXED, REOPENED nesta ordem e estado REOPENED.
    assert tipos_de_evento(conn, "f1")[-2:] == ["FIXED", "REOPENED"]


def test_payload_antigo_nao_gera_evento_espurio(ingestor, conn):
    # baseline FIXED em 2026; replay OPEN de 2020.
    antes = eventos(conn, "f1")
    assert eventos(conn, "f1") == antes


def test_delete_antigo_nao_apaga_update_novo(ingestor, conn):
    # update em 2026-08-28 seguido por delete observado em 2026-08-20.
    assert estado(conn, "f1")["deleted_at"] is None
    assert "DELETED" not in tipos_de_evento(conn, "f1")
```

- [ ] **Step 2: Rodar os testes e observar falhas**

Run: `.\scripts\test-postgres.ps1 -PostgresBin 'C:\Program Files\PostgreSQL\18\bin' -Port 55432`

Expected: FAIL nos casos novos e nos reprocessamentos incrementais que hoje geram transições espúrias.

- [ ] **Step 3: Reescrever `20_events.sql` como timeline efetiva**

Usar CTEs com esta estrutura obrigatória:

```sql
WITH updates_efetivos AS (
    SELECT s.*, c.state AS baseline_state,
           c.severity_modification_type AS baseline_recast,
           row_number() OVER w AS rn,
           lag(s.state) OVER w AS lag_state,
           lag(s.severity_modification_type) OVER w AS lag_recast
    FROM stg_finding s
    LEFT JOIN finding_current c USING (finding_id)
    WHERE s.is_delete = false
      AND (c.finding_id IS NULL OR s.indexed > c.indexed)
    WINDOW w AS (PARTITION BY s.finding_id ORDER BY s.indexed, s.seq)
), timeline AS (
    SELECT *,
           CASE WHEN rn = 1 THEN baseline_state ELSE lag_state END AS old_state,
           CASE WHEN rn = 1 THEN baseline_recast ELSE lag_recast END AS old_recast
    FROM updates_efetivos
)
```

As uniões geram: primeiro inédito não-FIXED → `OPENED`; primeiro inédito FIXED → `OPENED` retroativo + `FIXED`; primeiro inédito REOPENED → `OPENED` + `REOPENED`; `old_state='FIXED'` para estado não-FIXED → `REOPENED`; estado não-FIXED para FIXED → `FIXED`; mudança de recast em finding existente → `RECAST_CHANGED`. O bloco de delete usa o último update efetivo do mesmo finding como baseline quando ele existir e rejeita relógio anterior ao persistido.

- [ ] **Step 4: Proteger tombstones no upsert**

`45_apply_deletes.sql` deve escolher o último delete, aplicar somente quando seu relógio for posterior ao persistido (ou quando vier depois de update do mesmo staging com relógio igual), definir `deleted_at`, avançar `indexed` e manter `state`. `40_upsert_current.sql` continua limpando `deleted_at` somente para update estritamente mais novo.

- [ ] **Step 5: Fortalecer checksum e rodar todos os testes de banco**

Incluir `finding_recast` no snapshot de idempotência e verificar que a contagem/hash de eventos não muda em replay incremental.

Run: `.\scripts\test-postgres.ps1 -PostgresBin 'C:\Program Files\PostgreSQL\18\bin' -Port 55432`

Expected: PASS.

- [ ] **Step 6: Commit do motor de eventos**

```powershell
git add tests/test_events.py tests/test_idempotency.py ingestion/sql/20_events.sql ingestion/sql/40_upsert_current.sql ingestion/sql/45_apply_deletes.sql
git commit -m "fix: enforce ordered versioned finding events"
```

---

### Task 4: Fronteiras de falha, idempotência transacional e reprocesso fiel

**Files:**
- Modify: `ingestion/loader.py`
- Modify: `ingestion/cli.py`
- Modify: `tests/test_events.py`
- Modify: `tests/test_idempotency.py`

**Interfaces:**
- Consumes: `ingest_file.status`, `ingest_file.mode`, exceções `ErroParse` e `ErroIntegridade`.
- Produces: `processar_payload(..., forcar: bool = False)`; apenas falhas de conteúdo entram no contador/quarentena; reprocesso usa o modo gravado no arquivo.

- [ ] **Step 1: Escrever testes vermelhos**

```python
def test_reprocesso_preserva_modo_original_incremental(ingestor, conn):
    # Arquivo originalmente INCREMENTAL permanece INCREMENTAL mesmo com controle em SEED.
    assert resultado.status == "OK"
    assert modo_gravado == "INCREMENTAL"


def test_erro_de_banco_nao_quarentena_payload(ingestor, conn, monkeypatch):
    monkeypatch.setattr(ingestor, "_aplicar", lambda *args: (_ for _ in ()).throw(RuntimeError("sql bug")))
    with pytest.raises(RuntimeError, match="sql bug"):
        ingestor.processar_payload(entrada, manifest, tipo, "SEED")
    assert status_ingest_file(conn, entrada.path) is None
```

- [ ] **Step 2: Confirmar as falhas**

Run: `.\scripts\test-postgres.ps1 -PostgresBin 'C:\Program Files\PostgreSQL\18\bin' -Port 55432`

Expected: FAIL em modo de reprocesso, propagação de erro e contabilidade de payloads pulados.

- [ ] **Step 3: Implementar fronteiras explícitas**

Dentro da transação, consultar `ingest_file FOR UPDATE`; retornar `SKIPPED` quando `status='OK'` e `forcar=False`. Capturar somente `(ErroIntegridade, ErroParse)` para `_marcar_falha`; qualquer outra exceção deve ser logada e relançada. `reprocessar()` deve selecionar também `mode` e chamar `processar_payload(..., modo=registro['mode'], forcar=True)`.

- [ ] **Step 4: Corrigir a contabilidade de manifests totalmente processados**

Ao detectar que todos os paths estão `OK`, incrementar `payloads_pulados` pela quantidade de entradas, sem consumir `--limit`.

- [ ] **Step 5: Rodar suítes focal e completa**

Run: `.venv\Scripts\python -m pytest tests/test_events.py tests/test_idempotency.py -q`

Run: `.\scripts\test-postgres.ps1 -PostgresBin 'C:\Program Files\PostgreSQL\18\bin' -Port 55432`

Expected: PASS.

- [ ] **Step 6: Commit das fronteiras transacionais**

```powershell
git add ingestion/loader.py ingestion/cli.py tests/test_events.py tests/test_idempotency.py
git commit -m "fix: isolate payload failures and preserve replay mode"
```

---

### Task 5: Relógios persistentes para plugin e recast

**Files:**
- Create: `migrations/sql/0003_source_clocks.sql`
- Create: `migrations/versions/0003_source_clocks.py`
- Modify: `ingestion/payload.py`
- Modify: `ingestion/sql/00_staging.sql`
- Modify: `ingestion/sql/30_upsert_plugin.sql`
- Modify: `ingestion/sql/50_upsert_recast.sql`
- Modify: `tests/test_flatten.py`
- Modify: `tests/test_events.py`
- Modify: `tests/test_idempotency.py`

**Interfaces:**
- Consumes: relógio do finding para plugin; `rule_updated_at`, `rule_created_at`, `deleted_at` e fallback do manifest para recast.
- Produces: colunas `plugin.source_indexed timestamptz` e `finding_recast.source_indexed timestamptz`, ambas usadas com guarda estrita contra regressão.

- [ ] **Step 1: Escrever testes de regressão temporal**

```python
def test_plugin_antigo_nao_sobrescreve_plugin_novo(ingestor, conn):
    assert plugin(conn, 19506)["solution"] == "solucao nova"


def test_recast_antigo_e_delete_antigo_nao_regredem_estado(ingestor, conn):
    linha = recast(conn, "f1")
    assert linha["rule_comment"] == "regra nova"
    assert linha["deleted_at"] is None
```

- [ ] **Step 2: Confirmar falhas no PostgreSQL**

Run: `.\scripts\test-postgres.ps1 -PostgresBin 'C:\Program Files\PostgreSQL\18\bin' -Port 55432`

Expected: FAIL porque os targets ainda não guardam relógio da origem.

- [ ] **Step 3: Criar migração aditiva e atualizar staging**

Adicionar `source_indexed timestamptz` nullable nas duas tabelas. `LinhaPlugin.indexed` passa a copiar para `source_indexed`; `LinhaRecast` ganha `source_indexed`, definido por `rule_updated_at`, depois `rule_created_at`, depois fallback; delete usa `deleted_at`, depois fallback.

- [ ] **Step 4: Aplicar guardas estritas**

Plugin atualiza se `p.source_indexed IS NULL OR EXCLUDED.source_indexed > p.source_indexed`. Recast update/delete usa a mesma regra; empate é replay e não muda `ingested_at`.

- [ ] **Step 5: Verificar migrações e idempotência**

Run: `.venv\Scripts\alembic upgrade head`

Run: `.\scripts\test-postgres.ps1 -PostgresBin 'C:\Program Files\PostgreSQL\18\bin' -Port 55432`

Expected: PASS e checksum incluindo plugin/recast estável.

- [ ] **Step 6: Commit dos relógios auxiliares**

```powershell
git add migrations ingestion tests
git commit -m "fix: prevent plugin and recast version regression"
```

---

### Task 6: Download e parsing realmente streaming

**Files:**
- Create: `ingestion/streaming.py`
- Modify: `ingestion/s3.py`
- Modify: `ingestion/payload.py`
- Modify: `ingestion/loader.py`
- Modify: `tests/fixtures/__init__.py`
- Create: `tests/test_streaming.py`
- Modify: `tests/test_events.py`

**Interfaces:**
- Consumes: `StreamingBody.iter_chunks`, arquivo `.json.gz`, `TipoPayload` e `EntradaPayload`.
- Produces: `ClienteS3.baixar_payload(key, md5_esperado)` como context manager que entrega `Path`; `PayloadStream(path, tipo, entrada)` com `version`, `iter_findings()`, `iter_plugins()` e `iter_recasts()`.

- [ ] **Step 1: Escrever testes vermelhos de I/O limitado**

```python
def test_download_nunca_faz_read_sem_limite(cliente, tmp_path):
    with cliente.baixar_payload("finding/x.json.gz", MD5) as path:
        assert path.stat().st_size > 0
    assert cliente.cliente.body.read_sizes
    assert all(size == 1024 * 1024 for size in cliente.cliente.body.read_sizes)


def test_payload_stream_nao_usa_json_loads(monkeypatch, arquivo_gz, tipo, entrada):
    monkeypatch.setattr(json, "loads", lambda *_: (_ for _ in ()).throw(AssertionError("buffer integral")))
    stream = PayloadStream(arquivo_gz, tipo, entrada)
    assert sum(1 for _ in stream.iter_findings()) == 10000
```

- [ ] **Step 2: Confirmar as falhas**

Run: `.venv\Scripts\python -m pytest tests/test_streaming.py -q`

Expected: FAIL porque `baixar_payload` e `PayloadStream` ainda não existem.

- [ ] **Step 3: Implementar download com MD5 incremental e cleanup**

Ler o body em blocos fixos de 1 MiB, atualizar `hashlib.md5`, escrever em `NamedTemporaryFile(delete=False)` e comparar digest antes de entregar o path. Fechar `StreamingBody` quando suportado. O context manager remove somente o arquivo criado, inclusive em exceção.

- [ ] **Step 4: Implementar passes streaming sobre gzip**

`PayloadStream` abre `gzip.open(path, 'rb')` a cada passe e usa `ijson.items(..., 'updates.item')`/`'deletes.item'`. As funções existentes `_achatar_vm`, `_achatar_was`, `_achatar_plugin`, `_achatar_delete` e `_achatar_enriched` continuam sendo a única fonte de mapeamento. Metadados escalares são obtidos por `ijson.parse` sem materializar arrays.

- [ ] **Step 5: Alimentar COPY diretamente e validar contagens dentro da transação**

Criar staging, consumir os geradores com `_copiar`, contar updates/deletes efetivamente percorridos, validar manifest e schema, depois executar eventos/upserts/mark file. Nenhuma `list` proporcional ao payload pode existir no caminho de `Ingestor.processar_payload`.

- [ ] **Step 6: Rodar testes de streaming e banco**

Run: `.venv\Scripts\python -m pytest tests/test_streaming.py tests/test_flatten.py -q`

Run: `.\scripts\test-postgres.ps1 -PostgresBin 'C:\Program Files\PostgreSQL\18\bin' -Port 55432`

Expected: PASS.

- [ ] **Step 7: Commit do caminho de memória limitada**

```powershell
git add ingestion tests
git commit -m "refactor: stream payloads into PostgreSQL COPY"
```

---

### Task 7: Observabilidade, reconciliação e manutenção verificáveis

**Files:**
- Create: `migrations/sql/0004_observability_state.sql`
- Create: `migrations/versions/0004_observability_state.py`
- Create: `ingestion/reconcile.py`
- Modify: `ingestion/metrics.py`
- Modify: `ingestion/partitions.py`
- Modify: `ingestion/cli.py`
- Create: `tests/test_metrics.py`
- Create: `tests/test_partitions.py`
- Create: `tests/test_reconcile.py`

**Interfaces:**
- Consumes: resultado do ciclo, estado anterior `pipeline_control.last_findings_open` e consultas locais de saúde.
- Produces: sete métricas obrigatórias mais `FindingsOpenChangePercent`; comando `reconcile --output <json>` com contagens locais e campos opcionais `--console-vm-open`/`--console-was-open`.

- [ ] **Step 1: Escrever testes vermelhos para métricas e partições**

```python
def test_variacao_percentual_absoluta():
    assert variacao_percentual(120, 100) == 20.0
    assert variacao_percentual(80, 100) == 20.0
    assert variacao_percentual(1, 0) == 100.0


def test_garante_mes_atual_e_seguinte(conn):
    assert garantir_particoes(conn, hoje=date(2026, 8, 27)) == [
        "finding_event_2026_08", "finding_event_2026_09"
    ]
```

- [ ] **Step 2: Confirmar as falhas**

Run: `.venv\Scripts\python -m pytest tests/test_metrics.py tests/test_partitions.py tests/test_reconcile.py -q`

Expected: FAIL nos contratos novos.

- [ ] **Step 3: Persistir baseline de contagem e publicar variação**

Adicionar `pipeline_control.last_findings_open bigint`. Calcular variação absoluta antes de atualizar o baseline; publicar `FindingsOpenChangePercent` junto das sete métricas obrigatórias. Falha de publicação continua registrada sem desfazer ingestão já confirmada.

- [ ] **Step 4: Implementar relatório semanal local honesto**

`reconcile.py` consulta abertos por produto, duplicatas por `natural_key`, findings sem plugin e quarentenas. Quando contagens do console forem fornecidas, calcula deltas; quando não forem, grava `console_comparison: "NOT_PROVIDED"`. Isso mantém P1/P4 visíveis sem inventar acesso ao console.

- [ ] **Step 5: Validar retenção e DEFAULT**

Testar criação do mês atual/seguinte, expurgo somente de partições inteiramente vencidas, preservação da DEFAULT e de quarentenas em `ingest_file`.

- [ ] **Step 6: Rodar suíte completa**

Run: `.\scripts\test-postgres.ps1 -PostgresBin 'C:\Program Files\PostgreSQL\18\bin' -Port 55432`

Expected: PASS.

- [ ] **Step 7: Commit de observabilidade**

```powershell
git add migrations ingestion tests
git commit -m "feat: add ingestion observability and reconciliation"
```

---

### Task 8: Imagem, CronJobs EKS e alarmes CloudWatch

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `deploy/k8s/service-account.yaml`
- Create: `deploy/k8s/configmap.yaml`
- Create: `deploy/k8s/cronjob.yaml`
- Create: `deploy/k8s/reconciliation-cronjob.yaml`
- Create: `deploy/k8s/kustomization.yaml`
- Create: `deploy/cloudwatch-alarms.yaml`
- Create: `tests/test_deploy_contract.py`

**Interfaces:**
- Consumes: imagem `tenable-ingestion:latest`, Secret Kubernetes `tenable-ingestion-secret` com `TENABLE_BUCKET` e `PG_DSN`, credenciais AWS por workload identity.
- Produces: CronJob diário com `concurrencyPolicy: Forbid`, reconciliação semanal e template CloudFormation parametrizado por namespace/SNS topic.

- [ ] **Step 1: Escrever testes vermelhos dos artefatos**

```python
import subprocess
import sys

import yaml


def test_cronjob_proibe_concorrencia():
    doc = yaml.safe_load((ROOT / "deploy/k8s/cronjob.yaml").read_text())
    assert doc["spec"]["concurrencyPolicy"] == "Forbid"


def test_template_declara_quatro_alarmes():
    subprocess.run(
        [sys.executable, "-m", "cfnlint", "deploy/cloudwatch-alarms.yaml"],
        cwd=ROOT,
        check=True,
    )
```

- [ ] **Step 2: Confirmar falhas**

Run: `.venv\Scripts\python -m pytest tests/test_deploy_contract.py -q`

Expected: FAIL porque os artefatos ainda não existem.

- [ ] **Step 3: Criar imagem não-root**

Usar `python:3.13-slim`, instalar `requirements.txt`, copiar somente código/migrações, criar usuário sem shell e definir `ENTRYPOINT ["python", "-m", "ingestion.cli"]` com `CMD ["run"]`.

- [ ] **Step 4: Criar manifests Kubernetes concretos**

ConfigMap define namespace CloudWatch, retenção, tentativas e versões esperadas. Secret não é versionado; os CronJobs referenciam o nome fixo `tenable-ingestion-secret`. Job diário usa schedule `0 3 * * *`, timezone `America/Sao_Paulo`, `concurrencyPolicy: Forbid`, deadline, backoff limitado, recursos e securityContext não-root. Job semanal executa `reconcile` e grava JSON em stdout/CloudWatch Logs.

- [ ] **Step 5: Criar os quatro alarmes obrigatórios**

CloudFormation parametriza `Namespace` e `AlarmTopicArn`: manifesto >6h; quarentena >0; 26 períodos horários sem `JobDurationSeconds`, com missing como breaching; `FindingsOpenChangePercent >20`. Todos publicam no SNS configurado.

- [ ] **Step 6: Validar sintaxe e contratos**

Run: `.venv\Scripts\python -m pytest tests/test_deploy_contract.py -q`

Run: `kubectl kustomize deploy/k8s`

Expected: testes PASS e YAML renderizado sem erro quando `kubectl` estiver disponível; ausência local de `kubectl` deve ser registrada como validação externa, não convertida em sucesso.

- [ ] **Step 7: Commit de deploy**

```powershell
git add Dockerfile .dockerignore deploy tests/test_deploy_contract.py
git commit -m "feat: add EKS jobs and CloudWatch alarms"
```

---

### Task 9: Configuração, documentação operacional e aceite

**Files:**
- Modify: `.env.example`
- Rewrite: `README.md`
- Create: `docs/runbook.md`
- Create: `docs/acceptance.md`

**Interfaces:**
- Consumes: CLI, variáveis, migrações, deploy e alarmes implementados nas tarefas anteriores.
- Produces: caminho reproduzível para setup, seed, corte manual, incremental, quarentena, reprocesso, reconciliação, deploy e diagnóstico.

- [ ] **Step 1: Confirmar o comportamento atual que a documentação precisa representar**

Run: `.venv\Scripts\python main.py --help`

Run: `.venv\Scripts\python -m ingestion.cli status --help`

Run: `.venv\Scripts\alembic current`

Expected: CLI responde; Alembic consulta o banco descartável quando `PG_DSN` estiver definido. A documentação é validada contra esses comandos reais, não por busca de texto.

- [ ] **Step 2: Documentar operação sem esconder limites externos**

README apresenta o pipeline PostgreSQL como fluxo principal e mantém o exportador legado em seção separada. Runbook inclui comandos exatos para `init-db`, `run --seed --limit 1`, `status`, `set-mode INCREMENTAL --cutoff`, `quarantine`, `reprocess`, `reconcile`, migrações, deploy e rollback operacional.

- [ ] **Step 3: Mapear os nove critérios de aceite**

`docs/acceptance.md` liga cada critério a teste/comando/evidência. Critérios locais aprovados recebem resultado e data; bucket real, console Tenable, e-mail do Data Stream, HML e aplicação do template AWS permanecem `EXTERNAL_VALIDATION_REQUIRED` com comando de verificação, nunca marcados como aprovados sem acesso.

- [ ] **Step 4: Executar verificação final**

Run: `.venv\Scripts\python -m pytest -q`

Run: `.\scripts\test-postgres.ps1 -PostgresBin 'C:\Program Files\PostgreSQL\18\bin' -Port 55432`

Run: `.venv\Scripts\python -m compileall -q ingestion migrations tests`

Expected: todas as suítes locais PASS; somente validações que exigem sistemas externos permanecem explicitamente pendentes.

- [ ] **Step 5: Commit da documentação e aceite**

```powershell
git add .env.example README.md docs
git commit -m "docs: complete ingestion operations and acceptance guide"
```

- [ ] **Step 6: Revisão final do branch**

Gerar diff desde o commit baseline, revisar aderência integral à spec, corrigir apenas achados comprovados, repetir as verificações e usar `superpowers:verification-before-completion` antes de declarar conclusão.
