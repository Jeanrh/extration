# Runbook operacional — ingestão Tenable Data Stream

Este runbook cobre o pipeline PostgreSQL (seções 1 a 11) e o motor de risco
(seção 12), que roda em CronJob separado. O exportador CSV legado está descrito
separadamente no README e não participa desta operação.

## 1. Limites e pré-requisitos

O destino é um cluster EKS **já existente**. Este projeto não cria cluster,
VPC, subnets ou node groups. Antes de qualquer execução, confirme:

- bucket e prefixo do Data Stream autorizados para o ambiente;
- PostgreSQL 14+ descartável em local/teste ou endpoint privado em HML/produção;
- Python 3.11+ e dependências instaladas;
- em EKS, namespace existente, imagem ECR por digest, Secret fornecido por canal
  seguro e workload identity via IRSA ou EKS Pod Identity;
- política S3 limitada aos prefixes de manifests/payloads necessários e
  permissão CloudWatch quando `CLOUDWATCH_ENABLED=true`;
- mecanismo de migração de HML/produção escolhido antes de habilitar CronJobs;
- para o motor (seção 12): credenciais de CMDB (JSM), da API clássica do Tenable
  e do Vault. Nenhuma é necessária para a ingestão, e a ausência de cada uma
  degrada o motor de forma previsível em vez de derrubá-lo.

Não registre DSN, credenciais, tokens, External ID, ARNs, Secrets, payloads,
paths de quarentena ou relatórios de reconciliação reais no Git, tickets ou
saídas públicas.

## 2. Setup local/teste

No PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ingestion.cli --help
```

Preencha no `.env` apenas valores do ambiente local autorizado. `TENABLE_BUCKET`
e `PG_DSN` são obrigatórios. Deixe `INGESTION_MODE` vazio para o modo persistido
em `pipeline_control` governar as execuções.

### Migração local/teste

```powershell
.\.venv\Scripts\python.exe -m ingestion.cli init-db
```

`init-db` aplica `migrations/sql/*.sql` e registra versões em
`schema_migration`. Ele existe somente para banco local/teste e fixtures.

### Migração de HML/produção

```powershell
.\.venv\Scripts\python.exe -m alembic heads
.\.venv\Scripts\python.exe -m alembic current
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Alembic registra a revisão em `alembic_version`. Em HML/produção use somente
esse caminho e execute `upgrade head` **antes** dos CronJobs, por um dos
mecanismos explicitamente aprovados:

1. private CI runner com rota ao PostgreSQL e secret efêmero; ou
2. Job Kubernetes one-shot endurecido, com a mesma imagem por digest,
   `restartPolicy: Never`, `backoffLimit` limitado, deadline, non-root,
   filesystem read-only, capabilities removidas, recursos limitados, identidade
   mínima e descarte após evidência do sucesso.

Nunca rode `init-db` e Alembic casualmente no mesmo banco: seus ledgers
(`schema_migration` e `alembic_version`) são independentes. Downgrade Alembic é
um ensaio obrigatório em PostgreSQL descartável, não uma rotina de rollback de
produção.

## 3. Smoke mutável e seed

O smoke abaixo é uma escrita real:

```powershell
.\.venv\Scripts\python.exe -m ingestion.cli run --seed --limit 1
.\.venv\Scripts\python.exe -m ingestion.cli status
```

`run --seed --limit 1` **não é dry-run**. Ele baixa, valida e confirma no máximo
um payload ainda processável; altera `finding_current`, `plugin`,
`finding_recast`, `ingest_file`, `pipeline_control` e manutenção aplicável. Use
somente o banco/bucket autorizados e guarde a saída como evidência privada.

Depois do smoke, drene o backfill em SEED:

```powershell
.\.venv\Scripts\python.exe -m ingestion.cli run --seed
.\.venv\Scripts\python.exe -m ingestion.cli status
```

SEED popula o estado corrente, plugin e recast, grava `ingest_file.mode=SEED` e
gera zero eventos. Repita conforme necessário enquanto o backfill inicial for
entregue. O Tenable pode publicar com frequência de até 15 minutos ("as often
as every 15 minutes"), sem garantia rígida ou SLA dessa cadência.

`status` lê `vw_pipeline_saude`, contagens do ledger por tipo/status e total de
eventos. Investigue `ultimo_manifest`, `ultima_execucao`, quarentena e partição
DEFAULT antes de avançar.

## 4. Corte deliberado e modo incremental

A troca nunca é automática. Durante SEED, acompanhe inéditos por dia:

```sql
SELECT date_trunc('day', first_ingested_at AT TIME ZONE 'America/Sao_Paulo') AS dia,
       count(*) AS ineditos
FROM finding_current
GROUP BY 1
ORDER BY 1;
```

O critério é estabilidade com variação inferior a 20% por três dias
consecutivos, confirmada também pela queda do volume diário no bucket. Registre
decisão, aprovador e evidência fora do repositório. Grave T com timestamp ISO
8601 e offset explícito:

```powershell
.\.venv\Scripts\python.exe -m ingestion.cli set-mode INCREMENTAL --cutoff '2026-09-05T00:00:00-03:00' --notes 'backfill estável por três dias'
.\.venv\Scripts\python.exe -m ingestion.cli status
```

Um cutoff somente com data (`2026-09-05`) é aceito pela CLI, mas representa
`2026-09-05T00:00:00+00:00`: **data-only é UTC**. Prefira sempre o offset
explícito para não deslocar a fronteira operacional.

Execução incremental normal:

```powershell
.\.venv\Scripts\python.exe -m ingestion.cli run
.\.venv\Scripts\python.exe -m ingestion.cli status
```

INCREMENTAL mantém o estado, gera eventos e grava
`ingest_file.mode=INCREMENTAL`. Não fixe `INGESTION_MODE` no ambiente de
produção; isso sobrescreveria a decisão persistida para cada execução.

## 5. Concorrência e lock

Há duas proteções complementares:

- `concurrencyPolicy: Forbid` evita sobreposição no mesmo CronJob;
- `pg_try_advisory_lock(hashtext('tenable_ingestion'))` protege o banco contra
  jobs distintos ou execuções manuais concorrentes.

Se `run` não obtiver o lock, ele registra warning e encerra com código 0 sem
ingerir. `reprocess` e `reconcile` encerram com código 1 quando o lock está
ocupado. Se isso não corresponde a uma execução conhecida, identifique sessão,
Job e duração antes de intervir; encerrar uma sessão libera o advisory lock.
Não remova o lock do código nem mude `Forbid` para contornar um job lento.

## 6. Quarentena e reprocesso

Falha de conteúdo ou integridade aborta apenas a transação do payload. A
primeira e a segunda tentativas ficam `FAILED`; ao atingir `MAX_ATTEMPTS` (3 por
default), o ledger registra `QUARANTINED`, publica a contagem e a fila segue.

Liste o ledger:

```powershell
.\.venv\Scripts\python.exe -m ingestion.cli quarantine
```

Corrija a causa antes de reprocessar. Use a key **exata exibida pelo ledger**:

```powershell
.\.venv\Scripts\python.exe -m ingestion.cli reprocess --path '<path-exato-de-ingest_file>'
.\.venv\Scripts\python.exe -m ingestion.cli quarantine
.\.venv\Scripts\python.exe -m ingestion.cli status
```

`reprocess` exige que o path já exista em `ingest_file`, recupera do ledger o
manifest, MD5, contagens, clocks e `mode`, força uma nova tentativa e preserva
o modo original. Nunca escolha SEED/INCREMENTAL manualmente no reprocesso. Se o
objeto não existir mais no S3, restaure-o por processo autorizado antes de
tentar novamente; não fabrique metadados do manifest.

## 7. Reconciliação semanal

Sem números do console, gere um snapshot local honesto:

```powershell
.\.venv\Scripts\python.exe -m ingestion.cli reconcile --output '<arquivo-privado>.json'
```

O relatório cobre abertos VM/WAS, duplicatas por `natural_key`, findings sem
plugin e quarentena. Sem contagens fornecidas, o campo é exatamente
`console_comparison: "NOT_PROVIDED"`.

Depois de obter contagens do console Tenable por canal autorizado:

```powershell
.\.venv\Scripts\python.exe -m ingestion.cli reconcile --output '<arquivo-privado>.json' --console-vm-open <contagem-vm> --console-was-open <contagem-was>
```

Não versione o JSON nem cole dados reais neste repositório. O CronJob semanal
da base não recebe contagens do console e, portanto, reporta `NOT_PROVIDED` até
existir integração externa. O dono da revisão semanal continua pendente (P1),
e divergência banco versus console deve ser resolvida antes do go-live (P4).

## 8. Partições e retenção

Cada `run`, depois da ingestão:

- garante partições UTC de `finding_event` para o mês corrente e o seguinte;
- move com segurança da partição DEFAULT as linhas que passam a ter partição;
- remove partições mensais inteiramente vencidas por `RETENTION_MONTHS` (24);
- remove ledgers não-quarentenados mais antigos que
  `INGEST_FILE_RETENTION_DAYS` (90);
- preserva indefinidamente ledgers em `QUARANTINED`;
- registra quantas linhas ficaram em `finding_event_default`.

Diagnóstico SQL:

```sql
SELECT inhrelid::regclass AS partition_name
FROM pg_inherits
WHERE inhparent = 'finding_event'::regclass
ORDER BY 1;

SELECT count(*) AS default_rows FROM finding_event_default;

SELECT status, count(*) FROM ingest_file GROUP BY status ORDER BY status;
```

### Gate externo de lifecycle S3

Antes do go-live, o dono do bucket deve comparar o lifecycle dos objetos com os
90 dias de `ingest_file`. Isso não é verificável neste repositório e é um gate
externo. Se um payload permanecer no S3 depois que sua linha de ledger expirar,
um ciclo futuro poderá baixá-lo repetidamente. Os clocks estritos e a dedupe de
eventos mantêm o estado idempotente, mas download, duração e custo podem crescer.
Alinhe lifecycle/ledger ou aceite e monitore explicitamente esse custo.

## 9. Métricas, alarmes e notificações

Com `CLOUDWATCH_ENABLED=true`, cada ciclo tenta publicar no namespace definido:

- `HoursSinceLastManifest`;
- `PayloadsProcessed`;
- `RecordsIngested`;
- `EventsGenerated`;
- `FilesQuarantined`;
- `FindingsOpen`;
- `FindingsOpenChangePercent`;
- `JobDurationSeconds`.

Falha de publicação é registrada, mas não desfaz uma ingestão já confirmada.
Valide permissões, namespace e presença das métricas no ambiente real.

O template `deploy/cloudwatch-alarms.yaml` liga alarmes para manifest com idade
maior que 6 horas, quarentena, ausência de duração por 26 horas e variação de
abertos maior que 20%. Como a ingestão escolhida roda uma vez ao dia, o limiar
de 6 horas só é **observado quando a execução diária mede/publica a idade**; ele
não oferece detecção em até 6 horas nem SLA. O alarme de missing-job é separado
e cobre ausência de `JobDurationSeconds` ao longo de 26 horas.

A aplicação do template, assinatura/entrega SNS e transição real de estados são
gates AWS externos. Também habilite e teste, no próprio Tenable, o e-mail de
status do Data Stream. Nunca marque essas notificações como validadas apenas
porque o template passou no linter.

## 10. Deploy no EKS existente

`deploy/k8s` é uma base sem namespace, sem Secret, sem identidade real e com
imagem placeholder `tenable-ingestion:latest`. **Não execute**
`kubectl apply -k deploy/k8s`.

Para cada ambiente, mantenha um overlay real em repositório/secret store
apropriado que, no mínimo:

1. selecione o namespace já existente;
2. injete o Secret `tenable-ingestion-secret` com `TENABLE_BUCKET` e `PG_DSN`
   por canal seguro, sem material sensível no Git;
3. configure a ServiceAccount para IRSA **ou** registre a associação EKS Pod
   Identity, com permissões mínimas S3/CloudWatch;
4. substitua as duas imagens por um URI ECR com digest `sha256:...` imutável;
5. ajuste parâmetros não secretos necessários sem definir `INGESTION_MODE`;
6. preserve security contexts, recursos, deadlines e `concurrencyPolicy`;
7. mantenha CronJobs suspensos até a migração e os gates do ambiente passarem.

Fluxo de release:

```powershell
kubectl kustomize '<overlay-real>' > '<manifest-renderizado-temporario>.yaml'
kubectl apply --dry-run=server -k '<overlay-real>'
# execute e evidencie `python -m alembic upgrade head` pelo mecanismo aprovado
kubectl apply -k '<overlay-real>'
kubectl -n '<namespace>' get cronjob tenable-ingestion tenable-ingestion-reconciliation
```

O dry-run de servidor, migração, apply, criação de Job de smoke e inspeção de
logs exigem contexto/autorização reais e devem ficar na evidência externa. Não
há criação de infraestrutura EKS neste fluxo.

### Rollback operacional

CronJob não tem rollout revision e `kubectl rollout undo` não se aplica. Em
incidente:

1. suspenda os dois CronJobs no overlay real e aplique a mudança;
2. preserve logs, ledger, alarmes e digest atual para diagnóstico;
3. altere o overlay para o digest imutável anterior conhecido e compatível;
4. renderize, faça dry-run server-side e aplique o overlay;
5. rode um Job controlado, valide `status`, métricas e quarentena;
6. reative os CronJobs somente após aprovação.

Não use tag `latest`. Não faça downgrade Alembic rotineiro em produção: prefira
migração forward compatível/corretiva. Se a restauração de schema for inevitável,
trate como mudança excepcional com backup testado, plano específico e aprovação.

## 11. Diagnóstico rápido

| Sintoma | Verificação | Ação segura |
|---|---|---|
| Job não iniciou | CronJob, schedule/timezone, suspensão, Events, missing-job 26h | corrigir overlay/identidade; não criar cluster |
| Job saiu 0 sem carga | logs de lock e sessão concorrente | aguardar/identificar o holder; não remover proteção |
| Manifest envelhecido | `status`, logs S3, `HoursSinceLastManifest`, role/trust/policy | corrigir acesso/configuração e executar ciclo controlado |
| Quarentena > 0 | `quarantine`, erro, MD5/contagens/schema | corrigir causa e reprocessar pelo path do ledger |
| Duração/custo cresce | volume S3, expurgo de ledger e lifecycle | revisar gate lifecycle versus 90 dias |
| DEFAULT contém linhas | clocks `occurred_at` e partições existentes | investigar datas; não apagar dados às cegas |
| Banco diverge do console | relatório com contagens reais | tratar P4 antes do go-live |
| CloudWatch sem métricas | logs do publisher, identity/policy, namespace/região | corrigir permissão e provar publicação no ambiente |

Pendências empíricas permanecem visíveis: dono da reconciliação (P1), remoção de
recast no Tenable (P2), semântica de `indexed` no backfill (P3), divergência de
contagens (P4) e frequência ideal após medir duração (P5).

## 12. Motor de risco

O motor roda em **CronJob próprio**, agendado com folga depois da ingestão. Não
é etapa do job de ingestão: mudar um peso e recalcular não pode arrastar o Data
Stream junto, e uma falha no scoring não pode barrar a entrada do dado. A
justificativa completa está em [motor.md §3.1](motor.md).

### Ordem no dia

```powershell
python -m risk.cli sync-context   # CMDB, arquitetura, threat intel
python -m risk.cli run            # deriva camadas e recalcula TUDO
python -m risk.cli status         # confere idade do contexto e distribuição
```

`run` **não** depende de `sync-context`: recalcula sobre o contexto já no banco.
É o que permite ajustar um peso e ver o efeito em segundos, sem esperar JSM,
Vault e a API clássica.

### Mudança de regra

Peso, faixa de CVSS, mapa de camada e `arquitetura.csv` mudam com frequência.
O procedimento é sempre o mesmo:

1. edite `risk/scoring/pesos.py` (ou `risk/referencia/arquitetura.csv`);
2. **suba `RISK_ENGINE_VERSION`** — é ela que fica gravada em cada linha e
   permite saber com que versão um score foi produzido;
3. rode `python -m risk.cli run`;
4. confira o deslocamento em `status` e nos eventos `RISK_CHANGED`.

Sem o passo 2, duas rodadas com fórmulas diferentes ficam indistinguíveis no
banco.

### Fonte externa fora do ar

Nenhuma fonte derruba o motor. Cada uma registra o que houve em `context_sync`,
e o snapshot anterior continua valendo — o motor prefere contexto de um ciclo
atrás a contexto vazio, que produziria score plausível e silenciosamente errado.

| Fonte | Comportamento | Efeito no score |
|---|---|---|
| CMDB (JSM) fora | busca falha antes de tocar as tabelas; snapshot anterior intacto | contexto até um ciclo defasado |
| Sem credencial de CMDB | `sync-context` pula a fonte e loga aviso | idem |
| `arquitetura.csv` ausente | `context_sync` marca `FAILED`, tabela zerada | `nota_arch` cai no default 40 |
| Threat intel vazio ou em timeout | **não** zera a tabela | snapshot anterior preservado |
| Vault fora | índice vazio | camada cai no fallback por `plugin.family` |

O caso do threat intel merece atenção: o export clássico devolve lista vazia
quando estoura o timeout de ~10 minutos. Zerar a tabela nessa situação
rebaixaria toda vulnerabilidade de ameaça ativa para nota 10 de uma só vez.

### Diagnóstico

| Sintoma | Verificação | Ação segura |
|---|---|---|
| `run` grava 0 e você esperava mudança | esqueceu de subir `RISK_ENGINE_VERSION`? | o upsert só escreve o que mudou — sem mudança de regra nem de contexto, 0 é o correto |
| Muita sigla vazia em `finding_risk` | `SELECT count(*) FROM finding_risk WHERE sigla = ''` | conferir `cmdb_server`/`cmdb_url` e o `synced_at` em `context_sync`; findings sem sigla caem nos defaults (BIA 50, PCI 10, arquitetura 40) |
| Camada quase toda vazia | `SELECT resolved_by, count(*) FROM plugin_layer GROUP BY 1` | `nenhum` em massa indica Vault fora ou segredo mudado |
| `nota_threat` desabou | `SELECT status, synced_at FROM context_sync WHERE source='THREAT_INTEL'` | export clássico falhou; o snapshot anterior deveria ter sido preservado |
| Prioridade oscila sem ninguém mexer | eventos `RISK_CHANGED` e `context_synced_at` da linha | provável troca de snapshot do CMDB; comparar `synced_at` entre as rodadas |
| `finding_risk` crescendo em disco | `n_dead_tup` em `pg_stat_user_tables` | tuplas mortas devem ser proporcionais à mudança real; se não forem, o `IS DISTINCT FROM` do upsert não está filtrando |

### Ainda pendente

- **Rodada de paridade** contra o `tenable_full.csv` do `extraction` — não
  executada. Condições obrigatórias em [motor.md §8.1](motor.md).
- **CronJob no EKS** — só depois da paridade fechar.
- **Clientes HTTP** (JSM e API clássica) nunca executados contra os serviços
  reais; testados apenas com sessão injetada.

## 13. Referências oficiais

- [Data Stream properties](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/data-stream-properties.htm)
- [Manifest files](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/manifest-files.htm)
- [Configuração do Tenable Data Stream](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/configure-tenable-data-stream.htm)
- [Findings payload files](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/findings-payload-files.htm)
- [Web App Scanning findings payload files](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/web-app-scanning-findings-payload-files.htm)
- [Finding enriched attributes payload files](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/finding-enriched-attributes-payload-files.htm)
- [Data Stream best practices](https://docs.tenable.com/vulnerability-management/Content/Settings/data-stream/data-stream-best-practices.htm)
