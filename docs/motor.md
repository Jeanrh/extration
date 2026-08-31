# Motor de risco — recálculo sem filtro de tempo

Companheiro da [spec.md](spec.md). A ingestão transcreve o Tenable; este
documento descreve o subsistema que decide **o que é crítico para esta
empresa**, que é coisa diferente.

---

## 1. Por que existe

A `severity` que o Tenable manda não é a verdade de risco desta empresa. A
priorização real é uma matriz de quadrantes (Q1–Q16) sobre dois vetores — risco
do ativo e risco da vulnerabilidade —, e até aqui ela vivia no pipeline de CSV
do projeto `extraction`.

Aquele pipeline tem um limite que não é de implementação, é de origem: o export
da API clássica só devolve findings dentro de uma janela de `last_found` de 30
dias (VM) e 7 dias (WAS). Tudo que ficou fora carrega para sempre o score da
última execução que o tocou — **inclusive depois de a regra mudar**. Mexer num
peso não repriorizava o backlog antigo; repriorizava só quem tinha aparecido
naquela janela.

O banco não tem esse limite: `finding_current` guarda o histórico inteiro. O
motor recalcula **todos os findings, em qualquer estado, sem filtro de tempo**.

**Resultado:** qualquer consumidor pergunta "traga as críticas" ao banco e
recebe a prioridade calculada pelo critério da empresa, sobre a base completa —
sem CSV intermediário e sem janela.

---

## 2. Fronteira

O banco fornece **apenas os campos do Tenable**. Toda fonte externa continua
sendo exatamente o que já era:

| Insumo | Origem | Mudou? |
|---|---|---|
| finding, asset, plugin, estado, datas | PostgreSQL (`finding_current`, `plugin`) | **sim** — antes vinha do export/S3 |
| threat intel (`nota_threat`) | API clássica do Tenable, filtro `cve_category` | não |
| família e camada (`nota_layer`) | Vault (keywords) + `plugin.family` | não |
| sigla, PCI, BIA, criticidade | CMDB (Atlassian Assets/JSM) | não |
| unidade de negócio (= aliança) e tribo | CMDB, via cockpit | não |
| arquitetura (`nota_arch`) | `risk/referencia/arquitetura.csv`, versionado | não |

**Fora do escopo:** enriquecimento Jira, marcações de negócio (falso positivo,
impedimento), exportador CSV para o dashboard e métricas novas de CloudWatch. O
motor termina no banco; quem monta o dashboard consome a view.

### 2.1 Por que o threat intel não vem do Data Stream

`cve_category` é um **filtro** do export clássico, não um campo. A resposta
nunca diz a que categoria o finding pertence — devolve só o subconjunto que
passou pelas sete categorias. Não há como "ler o campo" nem na API, nem no
stream.

O stream traz sinais adjacentes em `plugin`, e nenhum substitui o conjunto:

| Categoria usada hoje | Equivalente no stream |
|---|---|
| `in the news` | `in_the_news` — cobertura exata |
| `ransomware` | `exploited_by_malware` — mais amplo: malware ≠ ransomware |
| `recent active exploitation`, `persistently exploited` | `exploit_available` — diz que *existe* exploit, não que está sendo explorado |
| `top 50 vpr` | `vpr.score` — dá para cortar em ≥9.0, mas o "Top 50" é lista curada global |
| `cisa known exploitable` | possivelmente em `xrefs`; **não confirmado** nos payloads de amostra |
| `emerging threats` | **nenhum.** É curadoria da Tenable Research |

Como `nota_threat` é binária (100 ou 10) com peso 1.1 no `px`, trocar a
definição desloca findings de quadrante sem ninguém perceber. Por isso a API
clássica continua.

### 2.2 Por que a camada NÃO depende das tags

`asset_category` vinha da tag "Categoria de Ativos" do Tenable, e o Data Stream
não publica tags no payload de finding. Isso não é uma lacuna: a ordem real de
resolução do `extraction` (alinhada ao DAX) classifica pela **vulnerabilidade**,
não pelo ativo —

1. `plugin.name` × keywords do Vault
2. fallback em `plugin.family`

— e os dois campos existem na tabela `plugin`. Um patch de kernel num servidor
de banco é "Sistema Operacional", não "Banco de Dados".

---

## 3. Decisões

| Decisão | Escolha | Consequência aceita |
|---|---|---|
| Universo | todo finding com `deleted_at IS NULL`, inclusive `FIXED` | ~500 mil linhas por execução |
| Threat intel | snapshot: substitui a cada execução | finding fora da janela de 90 dias/OPEN cai para `nota_threat=10` e pode mudar de quadrante retroativamente |
| `nota_exploit` | `exploitability_ease`, tolerando `null` **e** texto | se o campo vier null em massa, o `px` achata; a rodada de paridade mede isso |
| Contexto externo | materializado em tabelas do PostgreSQL | pod efêmero do EKS não precisa de PVC/EFS |
| Fonte indisponível | calcula com o último snapshot, registrando a idade | contexto até um ciclo defasado, nunca ausente |
| Execução | CronJob **separado** da ingestão | janela em que `finding_current` está fresco e `finding_risk` não — exposta via `computed_at` |
| Saída | `finding_risk` com as oito notas e o contexto resolvido | contrato maior, porém a linha explica sozinha a prioridade |
| Histórico | evento `RISK_CHANGED` só na mudança | zero custo quando nada muda |

### 3.1 Divergência deliberada da spec

A [spec.md §3.3](spec.md) previu o motor como "etapa separada **dentro do mesmo
job**". A implementação usa **CronJob separado**, e o motivo é que a manutenção
deste sistema é quase toda no motor: peso, faixa de CVSS, mapa de camada e o
`arquitetura.csv` mudam com frequência, e cada ajuste precisa de um recálculo
para se ver o efeito. Com job separado isso é um `run`; acoplado, todo teste de
peso arrastaria a ingestão junto.

Some-se: falha no scoring não pode impedir o Tenable de entrar no banco, e a
`engine_version` permite reverter a imagem do motor sem encostar na ingestão. A
própria tabela de decisões da spec já justificou a `finding_risk` separada com
*"cadência independente; `engine_version` auditável; motor intocado"* — separar
o job leva essa decisão até o fim.

---

## 4. Modelo de dados

Quatro migrações. Cada uma acompanha o código que a usa, em vez de criar schema
especulativo.

### 4.1 `0005_risk_engine` — o veredito

Expande `finding_risk`, que nasceu vazia na `0001` justamente para isto:

- **veredito**: `priority_id`, `priority_name`, `sla_status`, `aging`
- **as oito notas**: `nota_bia`, `nota_pci`, `nota_exposure`, `nota_arch`,
  `nota_cvss`, `nota_threat`, `nota_exploit`, `nota_layer`
- **contexto resolvido**: `sigla`, `pci`, `bia`, `criticality_cmdb`,
  `unidade_negocio`, `tribo`, `arch_type`, `layer`, `familia`
  (`tribo` entra na [`0008`](#44-0008_unidade_negocio--unidade-de-negócio-e-tribo))
- **procedência**: `context_synced_at`

As oito notas ficam gravadas de propósito. Sem elas, responder "por que este
finding é Muito Alta?" exige reexecutar o motor — que a essa altura já pode
estar rodando com outros pesos.

Também: `RISK_CHANGED` entra no CHECK de `finding_event.event_type`, e
`plugin.exploitability_ease` é promovido do `raw` (caminho que a
[spec §5.3](spec.md) previu para campo não modelado; barato aqui, porque
`plugin` tem uma linha por plugin, não por finding).

### 4.2 `0006_risk_context` — as fontes externas

`cmdb_acronym`, `cmdb_server`, `cmdb_url`, `architecture`,
`threat_intel` e `context_sync`. Cada tabela é recarregada por inteiro dentro de
**uma** transação, e a busca acontece **antes** dela: se a fonte cair no meio,
as tabelas nunca chegaram a ser tocadas.

`cmdb_server.sigla` já vem resolvida. O CMDB guarda ali o *nome de exibição*
("GTeC - Gestão de Terminais"), não o código ("GTEC"); traduzir na carga faz o
score de 500 mil findings virar JOIN e deixa `acronym_raw` para auditar de qual
nome saiu cada código.

### 4.3 `0007_plugin_layer` — a camada derivada

`plugin_id → (layer, familia, resolved_by)`. A camada não depende do finding,
depende do plugin: derivar uma vez por plugin (dezenas de milhares) em vez de
por finding (centenas de milhares) transforma a `nota_layer` num JOIN.

`resolved_by` (`plugin_name` | `family` | `nenhum`) permite medir, depois de uma
rodada real, quanto da camada saiu do Vault e quanto caiu no fallback.

### 4.4 `0008_unidade_negocio` — unidade de negócio e tribo

Do CMDB saem para o consumidor exatamente três campos — **sigla, unidade de
negócio e tribo** —, resolvidos por esta cadeia:

```
servidor.acronym / url.acronym
   └─> sigla ──┬─> CMDB:  BIA, PCI, criticality
               ├─> CSV:   arquitetura            (arquivo mocado, versionado)
               └─> teamid ──> cockpit (por chave):  aliança + tribo
                                (resolvido no sync, não na leitura)
```

Domínio, subdomínio e equipe solucionadora saíram: ninguém prioriza por eles.

Duas correções de semântica, ambas encontradas confrontando o código com o
payload real do CMDB:

- **unidade de negócio é a `alianca` do cockpit**, não a `vp`.
- **o casamento sigla → cockpit é por id**, via `teamid` ("OR-345014") = `key`
  do cockpit. Por nome não funcionava: o `name` do cockpit é o rótulo da
  **tribo** ("GARAGEM") e o `team` da sigla é o da **equipe** ("Plataforma de
  Deploy"). Casavam só por coincidência, e `unidade_negocio` chegava
  praticamente vazia ao consumidor.

O casamento acontece **no sync**, em memória, e o resultado é gravado direto em
`cmdb_acronym`. Daí não existir coluna de id nem tabela `cmdb_team`: id de
junção é plumbing, e plumbing não vira coluna. Resolver na carga (dezenas de
milhares de siglas) em vez de na leitura (centenas de milhares de findings)
também tira um JOIN do caminho quente — a `0008` derruba a `cmdb_team` criada
pela `0006`.

> Cuidado ao ler o mapeador: `nota_bia` consome `criticality`
> (Crise/Alto/Medio/Baixo), **não** o campo `BIA` do CMDB, que é "Sim"/"Nao".
> O nome veio do extraction; `bia` viaja apenas como contexto.

---

## 5. As regras

```
py = BIA·1.0 + PCI·1.0 + Exposição·1.0 + Arquitetura·1.5
px = CVSS·1.0 + Ameaça·1.1 + Exploit·1.1 + Camada·0.8
```

Bandas em 100 / 200 / 300 nos dois eixos, cruzadas na grade Q1–Q16, com prazo de
SLA por quadrante (30 dias no Q16, 270 no Q1) contado desde `first_found`.
Tudo em [`risk/scoring/`](../risk/scoring/) — `pesos.py` isolado justamente para
que uma mudança de peso caiba em uma linha de diff.

### 5.1 As três divergências entre o código e os testes do `extraction`

**Oito dos 132 testes de scoring do `extraction` falham contra o próprio código
dele.** Os testes descrevem um comportamento que a implementação não tem.

O porte é fiel ao **código**, porque é ele que gera o `tenable_full.csv` que o
negócio usa hoje — e portanto é ele o parâmetro de paridade. As divergências
estão marcadas com `DIVERGÊNCIA` em `risk/scoring/motor.py`:

| # | Ponto | Código (portado) | Testes de lá |
|---|---|---|---|
| 1 | `nota_pci` com "Escopo Estendido" | 10 (regra inativa no Power BI) | 100 |
| 2 | `nota_cvss` sem CVSS3 | 10, sem olhar a severidade | fallback por severity |
| 3 | `nota_camada` vazia/não mapeada | 30 (default do DAX) | 10 |

**A número 2 merece atenção.** No `extraction`, `_cvss_label` **faz** o fallback
por severity e rotula o finding como "Crítico", enquanto `score_cvss` pontua 10.
Ou seja: hoje um finding CRITICAL sem CVSS3 aparece como crítico e pontua como
irrelevante. Isso tem cara de bug, não de escolha.

**Decisão registrada:** manter o comportamento atual e **medir na rodada de
paridade** quantos findings estão nessa situação, para decidir com o número na
mão. Corrigir agora misturaria erro de porte com mudança deliberada e tornaria a
comparação de CSV ilegível.

---

## 6. Como executa

O trabalho se divide por **cardinalidade do domínio**, não por camada. É o que
mantém o custo baixo:

1. **Sync de contexto** (Python, domínios pequenos) — CMDB, `arquitetura.csv` e
   threat intel recarregados em tabelas; Vault em memória durante a execução.
2. **Derivações por plugin** (Python, dezenas de milhares) — `plugin_layer`.
3. **Scoring** (Python, lotes por chave sobre ~500 mil) — oito notas por linha.
4. **Diff, gravação e eventos** (SQL) — upsert que **só escreve o que mudou**.

### 6.1 Por que o cálculo em Python e não em SQL

A [spec §17.2](spec.md) manda "Python orquestra, SQL move dado", e o passo 4
respeita isso: o diff de 500 mil linhas nunca sobe para a memória.

Mas a *regra de scoring é o produto deste subsistema* — ela muda toda semana.
Reescrevê-la em SQL criaria uma segunda cópia das fórmulas, em outra linguagem,
para manter em sincronia com os testes a cada ajuste de peso. O cálculo é
stateless por linha e roda em segundos; o ganho não paga a duplicação.

### 6.2 Por que gravar só o que mudou

Reescrever 500 mil linhas por dia quando quase nada muda geraria 500 mil tuplas
mortas diárias, bloat e pressão de autovacuum. O `IS DISTINCT FROM` no upsert
torna a escrita proporcional à mudança real — e é a mesma comparação que produz
os eventos `RISK_CHANGED`, de graça.

---

## 7. Operação

```powershell
# recarrega CMDB, arquitetura e threat intel
python -m risk.cli sync-context

# deriva camadas e recalcula o risco de TODOS os findings
python -m risk.cli run

# saúde do motor, idade do contexto e distribuição de prioridade
python -m risk.cli status
```

`run` não depende de `sync-context`: recalcula sobre o contexto já no banco. É
deliberado — mexer num peso e ver o efeito não pode exigir esperar JSM, Vault e
a API clássica.

Detalhes de operação (ordem no dia, fonte fora do ar, mudança de peso,
diagnóstico) estão no [runbook §12](runbook.md).

---

## 8. Estado de verificação

Seguindo a convenção destes documentos: o que depende de sistema externo não é
marcado como aprovado a partir do repositório.

| Item | Estado | Como foi verificado |
|---|---|---|
| Migrações 0005–0007 | **verificado** | `alembic upgrade head` em banco novo e `downgrade` da 0005; ledger `.sql` pela suíte |
| Regras de scoring | **verificado** | 100 testes portados do `extraction` + mutação: 8 alterações deliberadas, 8 detectadas |
| Resolução de camada e de sigla | **verificado** | testes com os mesmos formatos de segredo e de nome de exibição reais |
| Recálculo fim a fim | **verificado** | `python -m risk.cli run` sobre 5.000 findings em PostgreSQL descartável |
| Idempotência | **verificado** | segunda execução: 5.000 recalculados, **0 gravados**, 0 eventos, 0,19 s |
| `RISK_CHANGED` | **verificado** | rebaixar uma sigla no CMDB gerou exatamente 2.500 eventos |
| Degradação sem Vault | **verificado** | execução real caiu no fallback por `plugin.family` sem parar |
| Clientes HTTP (JSM, Tenable) | `EXTERNAL_VALIDATION_REQUIRED` | paginação, 429, mapeamento e timeout testados com sessão injetada; **nunca executados contra JSM ou Tenable reais** |
| Rodada de paridade | **pendente** | ver §8.1 |
| CronJob no EKS | **pendente** | só depois da paridade fechar |

### 8.1 A rodada de paridade

Ainda não executada. Ela precisa de credenciais reais e de uma execução do
`extraction` para comparar. Duas condições são obrigatórias:

- comparar contra um run do `extraction` com **`TENABLE_SOURCE=s3`**, nunca
  `api` — com `api` o CSV traz `asset_category` e `tenable_tags` vindos do
  stream `tags/`, que este banco não ingere e nunca vai reproduzir; a
  comparação acusaria diferença onde não há erro;
- restringir à janela que o dashboard usa hoje (30 dias VM / 7 dias WAS), senão
  a diferença de universo domina o relatório — o motor calcula a base inteira,
  o CSV não.

O relatório precisa medir também **quantos findings têm `exploitability_ease`
preenchido**: é o dado que falta para decidir se `nota_exploit` continua nesse
campo ou migra para `plugin.exploit_available`.
