"""Sync do threat intel da API clássica do Tenable para `threat_intel`.

Esta é a única fonte que **não** pode vir do Data Stream. `cve_category` é um
filtro do export clássico, não um campo: a resposta nunca diz a que categoria o
finding pertence, só devolve o subconjunto que passou. O stream tem sinais
adjacentes (`in_the_news`, `exploited_by_malware`, `epss_score`), mas nenhum
cobre "emerging threats" e "ransomware" viraria "qualquer malware" — e como a
nota é binária (100 ou 10) com peso no px, trocar a definição desloca findings
de quadrante sem ninguém notar.

É snapshot, não acumulado: o export filtra `last_found` de 90 dias e estado
OPEN/REOPENED, então finding fora dessa janela volta a valer 10.
"""

from __future__ import annotations

import logging

from .procedencia import registrar_sync

log = logging.getLogger(__name__)

FONTE = "THREAT_INTEL"


def sincronizar_threat_intel(extrator, conn) -> int:
    """Substitui a tabela pelo snapshot atual. Devolve quantos IDs entraram.

    Resultado vazio **não** zera a tabela. O export clássico devolve lista
    vazia quando estoura o timeout de ~10 minutos, e nesse caso zerar
    rebaixaria toda vulnerabilidade de ameaça ativa de uma só vez — o
    extraction se defende disso com `merge=True` ao salvar o CSV.
    """
    ids = sorted(
        {
            str(item.get("finding_id", "")).strip()
            for item in extrator.extract_threat_intel()
            if str(item.get("finding_id", "")).strip()
        }
    )

    if not ids:
        log.warning("intel | export vazio — snapshot anterior preservado")
        with conn.transaction(), conn.cursor() as cur:
            registrar_sync(
                cur, FONTE, "FAILED", 0, "export vazio; snapshot anterior preservado"
            )
        return 0

    with conn.transaction(), conn.cursor() as cur:
        cur.execute("TRUNCATE threat_intel")
        cur.executemany(
            "INSERT INTO threat_intel (finding_id) VALUES (%s)",
            [(i,) for i in ids],
        )
        registrar_sync(cur, FONTE, "OK", len(ids))

    log.info("intel | sync | findings=%s", len(ids))
    return len(ids)
