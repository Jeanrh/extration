# Documentação

## Documentos vivos

| Documento | O que responde | Quem usa |
|---|---|---|
| [spec.md](spec.md) | O que o sistema deve fazer e por quê: modelo de dados, regras de ingestão, motor de eventos, modos, idempotência, observabilidade, retenção, views, fora de escopo e decisões arquiteturais registradas. | Quem vai mexer no código ou questionar uma decisão. |
| [runbook.md](runbook.md) | Como operar: setup, migração local versus produção, smoke, seed, corte para incremental, lock, quarentena, reprocesso, reconciliação, partições, métricas, alarmes, deploy no EKS, rollback e diagnóstico. | Quem opera o pipeline. |
| [acceptance.md](acceptance.md) | Em que estado está cada um dos nove critérios de aceite, com comando executável e evidência datada, e quais gates externos continuam pendentes. | Quem precisa decidir sobre go-live. |

Comece pelo [README da raiz](../README.md) se você ainda não rodou nada.

## Ordem de leitura sugerida

1. **README da raiz** — o que é, como instalar, os comandos.
2. **spec.md** seções 3 a 8 — arquitetura, modelo de dados e motor de eventos.
   São a parte que explica por que o código é do jeito que é.
3. **runbook.md** — quando for de fato executar contra um bucket.
4. **acceptance.md** — antes de qualquer conversa sobre produção.

## Registro histórico

`superpowers/plans/2026-08-27-tenable-datastream-ingestion.md` é o plano de
implementação usado para construir o pipeline, em nove tarefas. É um **registro
datado**, não documentação viva: ele descreve caminhos de arquivo anteriores à
reorganização do repositório e não é atualizado quando a estrutura muda. Serve
para entender a sequência de decisões, não para localizar arquivos.

## Convenções

- Os documentos são escritos para serem verificados, não acreditados. Quando um
  documento afirma um resultado, ele nomeia o comando que o produz.
- O que depende de sistema externo — bucket real, console Tenable, AWS, EKS,
  HML/produção — nunca é marcado como aprovado a partir do repositório. Fica
  como `EXTERNAL_VALIDATION_REQUIRED` em `acceptance.md`, com o comando que o
  operador precisa executar.
- Pendências da spec (P1 a P5) permanecem visíveis em vez de receberem solução
  inventada.
- Nenhum documento carrega DSN, credencial, token, External ID, ARN, Secret,
  payload real, path de quarentena ou relatório de reconciliação.
