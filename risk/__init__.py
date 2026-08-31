"""Motor de risco: recalcula a prioridade de todo finding, sem filtro de tempo.

O Tenable não é a fonte de verdade de risco desta empresa. Este pacote lê o
estado já ingerido em `finding_current`/`plugin`, cruza com o contexto de
negócio (CMDB, arquitetura, threat intel, camada) e grava o veredito em
`finding_risk`.

Roda como CronJob próprio, separado da ingestão: as regras de scoring mudam
com frequência, e recalcular depois de mexer num peso não pode exigir
reprocessar o Data Stream inteiro.
"""
