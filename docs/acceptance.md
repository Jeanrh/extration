# Aceite — critérios da SPEC e evidência verificável

Este documento liga cada um dos nove critérios de aceite da seção 18 do
[`docs/spec.md`](spec.md) a um teste ou comando executável e ao resultado
realmente observado.

Nada aqui é marcado como aprovado por leitura de código ou por inspeção de
texto. O que depende de bucket real, console Tenable, e-mail do Data Stream,
HML/produção, EKS ou AWS permanece explicitamente pendente, com o comando que
o operador deve executar no ambiente real.

**Escopo:** este documento cobre apenas a **ingestão**. O motor de risco
(`risk/`) tem critérios próprios e nasceu depois destes nove — o estado
verificável de cada item dele está em [motor.md §8](motor.md), com a mesma
legenda. Os dois são independentes: a ingestão pode ir para produção sem o
motor, e nesse caso as views caem no `severity` nativo do Tenable.

## Legenda

| Estado | Significado |
|---|---|
| `PASS_LOCAL` | Comando executado nesta máquina, com saída observada e registrada abaixo. |
| `NOT_RUN` | Verificação possível localmente, mas não executada nesta rodada. |
| `EXTERNAL_VALIDATION_REQUIRED` | Só é comprovável em sistema externo (S3, Tenable, AWS, EKS, HML/produção). Não pode ser aprovado daqui. |

`PASS_LOCAL` cobre o comportamento do código contra PostgreSQL descartável e
fixtures derivadas dos payloads de exemplo. Ele **não** substitui a validação
no ambiente real: é a condição necessária, não a suficiente.

## Contexto da evidência

| Item | Valor |
|---|---|
| Data da execução | 2026-08-28 |
| Branch | `feat/tenable-ingestion-complete` |
| Estado avaliado | Branch após a reorganização do repositório (exportador legado movido para `legacy/`, fixtures para `samples/`, spec para `docs/spec.md`). Toda a bateria foi reexecutada depois da mudança de caminhos; nenhuma lógica de `ingestion/`, `migrations/` ou `deploy/` foi alterada pela reorganização. |
| Python | 3.14.6 no venv local; o projeto declara suporte a 3.11+ |
| PostgreSQL de teste | 18, cluster descartável em diretório temporário, porta 55489 |
| Sistema | Windows 11, PowerShell |
| Ferramentas ausentes no host | `docker`, `kustomize` standalone |

## Portões executados nesta rodada

| Portão | Comando | Resultado |
|---|---|---|
| Dependências coerentes | `python -m pip check` | `PASS_LOCAL` — `No broken requirements found.` |
| CLI responde e expõe `reconcile` | `python -m ingestion.cli --help` | `PASS_LOCAL` — subcomandos `init-db, run, set-mode, reprocess, status, quarantine, reconcile` |
| Entry point alternativo | `python main.py --help` | `PASS_LOCAL` — mesma saída de `python -m ingestion.cli` |
| Compilação | `python -m compileall -q ingestion migrations tests legacy` | `PASS_LOCAL` — sem saída, sem erro |
| Suíte sem banco | `python -m pytest -q -m 'not banco'` | `PASS_LOCAL` — `109 passed, 77 deselected` |
| Suíte completa com PostgreSQL | `scripts\test-postgres.ps1 -PostgresBin 'C:\Program Files\PostgreSQL\18\bin' -Port 55489` | `PASS_LOCAL` — `186 passed`; cluster descartável criado e removido |
| Linhagem Alembic | `alembic heads` | `PASS_LOCAL` — `0004 (head)`, cabeça única |
| Ida e volta de migração | `upgrade head` → `downgrade 0003` → `upgrade head` em PostgreSQL descartável | `PASS_LOCAL` — `alembic_version` percorreu `0004 → 0003 → 0004` e a coluna `pipeline_control.last_findings_open` sumiu e voltou (`1 → 0 → 1`) |
| Template CloudFormation | `cfn-lint deploy\cloudwatch-alarms.yaml` | `PASS_LOCAL` — código de saída `0` |
| Renderização Kubernetes | `kubectl kustomize deploy\k8s` | `PASS_LOCAL` — exatamente `ServiceAccount`, `ConfigMap` e dois `CronJob`, sem namespace e sem Secret |
| Higiene do diff | `git diff --check` | `PASS_LOCAL` — código de saída `0` |
| Imagem do container | `docker build .` | `EXTERNAL_VALIDATION_REQUIRED` — Docker não está disponível neste host; construir, escanear e publicar por digest é gate do pipeline de build |

A suíte sem banco isolada **não é aceite**. O aceite local exige a suíte
completa com PostgreSQL descartável (`186 passed`).

## Os nove critérios

### 1. Reprocessar todo o bucket duas vezes produz estado e contagem idênticos

- **Estado:** `PASS_LOCAL` (2026-08-28)
- **Evidência:** `tests/test_idempotency.py:95`
  `test_reprocessar_tudo_produz_estado_identico`, parametrizado em `SEED` e
  `INCREMENTAL`; `tests/test_idempotency.py:167`
  `test_terceira_passada_tambem_nao_move_nada`;
  `tests/test_idempotency.py:131`
  `test_instantaneo_detecta_mudanca_na_linha_completa_das_quatro_estruturas`;
  `tests/test_idempotency.py:212`
  `test_reprocess_manual_de_um_payload_e_idempotente`.
- **Comando:** `python -m pytest -q tests/test_idempotency.py`, com
  `TEST_PG_DSN` apontando para um PostgreSQL descartável.
- **O que é comparado:** contagem e hash da linha completa de
  `finding_current`, `finding_event`, `plugin` e `finding_recast`. O teste de
  instantâneo prova que essa comparação detecta mudança de verdade, então a
  igualdade nas passadas seguintes não é um falso positivo.
- **Externo:** repetir o ciclo contra o bucket real e comparar o mesmo
  instantâneo permanece `EXTERNAL_VALIDATION_REQUIRED`.

### 2. Nenhum `finding_id` duplicado em `finding_current`

- **Estado:** `PASS_LOCAL` (2026-08-28)
- **Evidência:** `tests/test_idempotency.py:179`
  `test_nenhum_finding_id_duplicado`, executado depois de duas passadas
  completas sobre o cenário. A chave primária de `finding_current` está em
  `migrations/sql/0001_schema.sql` e é aplicada tanto por `init-db` quanto por
  `alembic upgrade head`.
- **Comando:**
  `python -m pytest -q tests/test_idempotency.py::test_nenhum_finding_id_duplicado`
- **Verificação no ambiente real:**

  ```sql
  SELECT finding_id, count(*)
  FROM finding_current
  GROUP BY finding_id
  HAVING count(*) > 1;
  ```

  Zero linhas é o resultado esperado. Rodar isso em HML/produção é
  `EXTERNAL_VALIDATION_REQUIRED`.

### 3. Todo evento tem `occurred_at` vindo do dado, não do relógio do job

- **Estado:** `PASS_LOCAL` (2026-08-28)
- **Evidência:** `tests/test_idempotency.py:195`
  `test_todo_evento_tem_occurred_at_vindo_do_dado`; regras individuais em
  `tests/test_events.py:341` (`OPENED` datado por `first_found`),
  `tests/test_events.py:358` (inédito já fechado gera `OPENED` retroativo e
  `FIXED`), `tests/test_events.py:427` (`FIXED` datado por `last_fixed`),
  `tests/test_events.py:447` (`REOPENED` depois de `FIXED`) e
  `tests/test_events.py:380` (`OPENED` de 2019 cai na partição DEFAULT — prova
  direta de que a data vem do dado, não da ingestão).
- **Comando:** `python -m pytest -q tests/test_events.py tests/test_idempotency.py`
- **Verificação no ambiente real:**

  ```sql
  SELECT count(*) FROM finding_event WHERE occurred_at > ingested_at;
  ```

  Também vale inspecionar `finding_event_default`: linhas antigas ali são o
  comportamento correto para findings historicamente abertos.

### 4. `deletes[]` gera `DELETED`, nunca `FIXED`

- **Estado:** `PASS_LOCAL` (2026-08-28)
- **Evidência:** `tests/test_events.py:466`
  `test_regra5_delete_gera_deleted_e_nunca_fixed`;
  `tests/test_events.py:488` `test_delete_repetido_nao_duplica_evento`;
  `tests/test_events.py:671` `test_delete_antigo_nao_apaga_update_novo`;
  `tests/test_events.py:757`
  `test_update_e_delete_empatados_no_mesmo_payload_aplicam_delete`;
  `tests/test_events.py:715`
  `test_delete_posterior_avanca_relogio_sem_novo_evento_ou_ressurreicao`.
- **Comando:** `python -m pytest -q tests/test_events.py -k delete`
- **Semântica registrada:** um tombstone repetido mais novo avança o relógio de
  origem em `finding_current` sem emitir um segundo evento `DELETED`. Isso
  impede que um update intermediário atrasado ressuscite um finding já ausente
  e evita inflar a métrica de deleções. Consumidores da timeline veem uma única
  transição lógica por deleção.
- **Externo:** o comportamento do Tenable quando um recast é removido continua
  não verificado (P2) e não é inventado por este código.

### 5. `host_audit_finding` e `tds_test_file` não são ingeridos

- **Estado:** `PASS_LOCAL` (2026-08-28)
- **Evidência:** `tests/test_flatten.py:555`
  `test_whitelist_tem_exatamente_tres_tipos` prova que a whitelist tem
  exatamente `FINDING`, `WAS_FINDING` e `FINDING_ENRICHED_ATTRIBUTES`;
  `tests/test_events.py:322`
  `test_manifest_de_outro_stream_e_rejeitado_sem_processar_payload` prova que
  um manifest fora da whitelist é rejeitado antes de qualquer payload ser
  processado; `tests/test_events.py:132`
  `test_payload_com_type_de_outro_stream_entra_no_ledger_de_falha` prova que um
  payload cujo `type` não bate com o prefixo declarado vira falha de
  integridade em vez de ser aceito silenciosamente.
- **Comando:**
  `python -m pytest -q tests/test_flatten.py::test_whitelist_tem_exatamente_tres_tipos tests/test_events.py`
- **Verificação no ambiente real:**

  ```sql
  SELECT DISTINCT payload_type FROM ingest_file ORDER BY 1;
  ```

  Somente os três tipos da whitelist devem aparecer.

### 6. Payload corrompido vai para quarentena e a fila continua

- **Estado:** `PASS_LOCAL` (2026-08-28)
- **Evidência:** `tests/test_events.py:1063`
  `test_arquivo_envenenado_vai_para_quarentena_e_a_fila_segue` prova as duas
  metades do critério: o arquivo defeituoso chega a `QUARANTINED` depois de
  `MAX_ATTEMPTS` e os payloads seguintes do mesmo manifest continuam sendo
  processados; `tests/test_events.py:1044`
  `test_md5_divergente_manda_o_arquivo_para_falha`;
  `tests/test_events.py:1090` `test_contagem_divergente_do_manifest_falha`;
  `tests/test_streaming.py:306` `test_gzip_corrompido_e_falha_de_conteudo`.
- **Fronteira provada em separado:** falha operacional (S3, banco, ledger) e
  erro de programação **não** viram quarentena — eles abortam o ciclo de forma
  visível, conforme `test_erro_operacional_no_download_do_manifest_aborta_ciclo`,
  `test_erro_ao_persistir_retry_de_conteudo_aborta_job` e
  `test_erro_de_programacao_nao_registra_retry_ou_quarentena`, em
  `tests/test_events.py`.
- **Comando:** `python -m pytest -q tests/test_events.py tests/test_streaming.py`
- **Operação:** `python -m ingestion.cli quarantine` lista o ledger e
  `reprocess --path <path-do-ledger>` reprocessa preservando o modo original
  (`docs/runbook.md`, seção 6).

### 7. Alarme dispara quando o último manifest tem mais de 6 horas

- **Estado local:** `PASS_LOCAL` para a métrica e para a identidade do alarme
  (2026-08-28)
- **Estado do disparo real:** `EXTERNAL_VALIDATION_REQUIRED`
- **Evidência local:** `tests/test_metrics.py:36`
  `test_metricas_tem_nomes_unidades_e_valores_exatos_sem_arredondar` fixa nome,
  unidade e valor de `HoursSinceLastManifest`; `tests/test_metrics.py:55`
  `test_sem_manifest_omite_so_a_metrica_de_staleness_e_registra_erro` prova que
  a ausência total de manifest não vira um número inventado — a métrica é
  omitida e o erro é registrado; `tests/test_deploy_contract.py:335`
  `test_cloudformation_declares_exact_alarm_identities_and_actions` fixa a
  matriz completa dos quatro alarmes; `tests/test_deploy_contract.py:415`
  `test_cloudformation_passes_installed_console_linter` roda o `cfn-lint`
  instalado sobre o template.
- **Identidade versionada em `deploy/cloudwatch-alarms.yaml`:**

  | Alarme | Métrica | Estatística | Período | Períodos | Comparação | Missing data |
  |---|---|---|---|---|---|---|
  | `StaleManifestAlarm` | `HoursSinceLastManifest` | `Maximum` | 86400 | 1 | `> 6` | `breaching` |
  | `QuarantinedFilesAlarm` | `FilesQuarantined` | `Maximum` | 86400 | 1 | `> 0` | `notBreaching` |
  | `MissingJobDurationAlarm` | `JobDurationSeconds` | `SampleCount` | 3600 | 26 (todos) | `< 1` | `breaching` |
  | `FindingsOpenChangeAlarm` | `FindingsOpenChangePercent` | `Maximum` | 86400 | 1 | `> 20` | `notBreaching` |

- **Limite honesto:** o job escolhido roda **uma vez por dia** (P5). O limiar de
  6 horas é, portanto, **observado quando a execução diária mede e publica a
  idade do manifest** — isso não é detecção em até 6 horas e não é um SLA. A
  ausência do próprio job é coberta separadamente pelo alarme de 26 horas, e
  `TreatMissingData: breaching` cobre o caso em que nenhuma métrica chega.
- **Comando externo:**

  ```powershell
  aws cloudformation deploy --template-file deploy/cloudwatch-alarms.yaml --stack-name '<stack>' --parameter-overrides AlarmTopicArn='<arn-do-topico>'
  aws cloudwatch describe-alarms --alarm-names '<nomes-gerados>'
  ```

  Aplicação do template, assinatura e entrega SNS e transição real de estado só
  podem ser comprovadas na conta AWS. Habilitar e testar o e-mail de status do
  Data Stream no próprio Tenable é um item separado e igualmente externo.

### 8. Partições do mês corrente e seguinte existem após cada execução

- **Estado:** `PASS_LOCAL` (2026-08-28)
- **Evidência:** `tests/test_partitions.py:32`
  `test_garante_mes_atual_e_seguinte_com_bounds_utc_e_e_idempotente`;
  `tests/test_partitions.py:86`
  `test_move_linhas_da_default_atomicamente_sem_perder_id_ou_conteudo`;
  `tests/test_partitions.py:67`
  `test_particao_com_bound_errado_falha_visivelmente`;
  `tests/test_partitions.py:52`
  `test_tabela_com_nome_conflitante_falha_visivelmente`.
- **Comando:** `python -m pytest -q tests/test_partitions.py`
- **Por que os testes negativos importam:** `CREATE ... IF NOT EXISTS` pode
  esconder uma tabela comum com o nome esperado ou uma partição com bound
  errado. A manutenção verifica o que criou e falha de forma visível nesses dois
  casos, em vez de reportar sucesso silencioso.
- **Verificação no ambiente real:**

  ```sql
  SELECT inhrelid::regclass AS partition_name
  FROM pg_inherits
  WHERE inhparent = 'finding_event'::regclass
  ORDER BY 1;

  SELECT count(*) AS default_rows FROM finding_event_default;
  ```

### 9. Views retornam dia em `America/Sao_Paulo`

- **Estado:** `PASS_LOCAL` (2026-08-28)
- **Evidência:** `tests/test_views.py:10`
  `test_evento_na_virada_utc_agrega_no_dia_e_mes_de_sao_paulo` executa contra
  PostgreSQL real e cobre a fronteira: um evento gravado em UTC logo depois da
  meia-noite UTC agrega no dia e no mês corretos de São Paulo, tanto na view
  diária quanto na mensal.
- **Comando:** `python -m pytest -q tests/test_views.py`
- **Contrato:** o banco persiste UTC; a conversão para `America/Sao_Paulo`
  acontece na leitura, preservando a referência original.

## Resumo

| # | Critério | Estado local | Pendência externa |
|---|---|---|---|
| 1 | Reprocesso duplo idêntico | `PASS_LOCAL` | Repetir contra o bucket real |
| 2 | Sem `finding_id` duplicado | `PASS_LOCAL` | Consulta em HML/produção |
| 3 | `occurred_at` vem do dado | `PASS_LOCAL` | Consulta em HML/produção |
| 4 | `deletes[]` → `DELETED` | `PASS_LOCAL` | P2 (recast removido) segue não verificado |
| 5 | Tipos fora da whitelist ignorados | `PASS_LOCAL` | Conferir `ingest_file` no ambiente real |
| 6 | Quarentena sem travar a fila | `PASS_LOCAL` | Exercício controlado no ambiente real |
| 7 | Alarme de manifest > 6h | `PASS_LOCAL` (métrica e template) | Aplicação AWS, SNS e transição de estado |
| 8 | Partições do mês corrente e seguinte | `PASS_LOCAL` | Conferir `pg_inherits` no ambiente real |
| 9 | Views em `America/Sao_Paulo` | `PASS_LOCAL` | Conferir com dados reais |

## Gates externos obrigatórios antes do go-live

Nenhum item abaixo pode ser marcado como aprovado a partir deste repositório.

| Gate | Como comprovar | Estado |
|---|---|---|
| Bucket e prefixos do Data Stream autorizados | Listar manifests/payloads com a identidade do job | `EXTERNAL_VALIDATION_REQUIRED` |
| E-mail de status do Data Stream habilitado e testado no Tenable | Configuração e recebimento real | `EXTERNAL_VALIDATION_REQUIRED` |
| PostgreSQL de HML/produção acessível por endpoint privado | Conexão e `alembic current` | `EXTERNAL_VALIDATION_REQUIRED` |
| Migração de produção por mecanismo aprovado antes dos CronJobs | Private CI runner **ou** Job Kubernetes one-shot endurecido, com evidência do `alembic upgrade head` | `EXTERNAL_VALIDATION_REQUIRED` |
| Imagem construída, escaneada e referenciada por digest `sha256:` | Build/scan/push no pipeline; overlay usando o digest | `EXTERNAL_VALIDATION_REQUIRED` (Docker ausente neste host) |
| Secret e workload identity (IRSA ou EKS Pod Identity) | Overlay real do cluster EKS já existente | `EXTERNAL_VALIDATION_REQUIRED` |
| Deploy no cluster EKS existente | `kubectl apply --dry-run=server -k '<overlay-real>'` e apply | `EXTERNAL_VALIDATION_REQUIRED` |
| Template de alarmes aplicado, SNS assinado e entrega testada | `aws cloudformation deploy` e `aws cloudwatch describe-alarms` | `EXTERNAL_VALIDATION_REQUIRED` |
| Métricas visíveis no namespace CloudWatch configurado | `aws cloudwatch list-metrics --namespace TenableIngestion` | `EXTERNAL_VALIDATION_REQUIRED` |
| Lifecycle do S3 versus retenção de 90 dias de `ingest_file` | Comparar a política do bucket com `INGEST_FILE_RETENTION_DAYS` | `EXTERNAL_VALIDATION_REQUIRED` |
| Contagens do banco versus console Tenable (P4) | `reconcile --console-vm-open N --console-was-open N` com números obtidos por canal autorizado | `EXTERNAL_VALIDATION_REQUIRED` |

Enquanto o lifecycle do S3 não for alinhado com os 90 dias do ledger, um payload
que sobreviva à sua linha de `ingest_file` pode ser baixado repetidamente. O
estado continua idempotente por causa dos relógios estritos, mas duração e custo
crescem — precisa ser decisão consciente, não descuido.

## Pendências conhecidas da SPEC

Permanecem visíveis, sem solução inventada.

| ID | Pendência | Estado |
|---|---|---|
| **P1** | Dono da reconciliação semanal | Indefinido. O relatório é gerado de qualquer forma; sem contagens do console ele grava `console_comparison: "NOT_PROVIDED"`. |
| **P2** | Comportamento quando um recast é removido no Tenable | Não verificado. Requer teste empírico no ambiente real. Deleção enriquecida com identidade desconhecida não cria tombstone. |
| **P3** | `indexed` dos findings do backfill vem original ou reindexado | Não verificado. Requer inspeção de um finding antigo no bucket real. |
| **P4** | Divergência de contagem banco versus console | Conhecida. Resolver antes do go-live usando o relatório de reconciliação. |
| **P5** | Frequência ideal do job | 1x/dia por escolha. Reavaliar depois de medir `JobDurationSeconds` real. |

## Como reproduzir esta evidência

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ingestion.cli --help
.\.venv\Scripts\python.exe -m compileall -q ingestion migrations tests legacy
.\.venv\Scripts\python.exe -m pytest -q -m 'not banco'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-postgres.ps1 -PostgresBin 'C:\Program Files\PostgreSQL\18\bin' -Port 55489
.\.venv\Scripts\alembic.exe heads
.\.venv\Scripts\cfn-lint.exe deploy\cloudwatch-alarms.yaml
kubectl kustomize deploy\k8s
git diff --check
```

Use sempre uma porta descartável livre, diferente de `5432`. A ida e volta de
migração (`upgrade head` → `downgrade 0003` → `upgrade head`) exige um
PostgreSQL descartável com `PG_DSN` apontado para ele; downgrade de Alembic não
é rotina de rollback de produção.
