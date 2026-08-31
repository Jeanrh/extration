"""Vínculo finding → ticket do Jira Service Desk.

Porte de `src/jira/{client,extractor}.py` do extraction, sem o cache em disco.

O elo é frágil e vale entender: o Jira **não sabe** que o finding existe. A
chave do card (`GVUL-123`) não tem relação nenhuma com o `finding_id` (UUID v5
do Tenable). O que liga os dois é a string `Finding ID: <uuid>` escrita dentro
do corpo da descrição — que vem em ADF, uma árvore de nós JSON, não texto.

Se alguém editar a descrição, mudar o template da automação que abre os cards
ou trocar o rótulo, o regex para de casar e o vínculo some **sem erro nenhum**.
Por isso `sincronizar_jira` conta quantos cards da fila ficaram sem finding_id
e grava esse número em `context_sync`: é o que transforma um sumiço silencioso
em sinal visível.

`requests` só é importado ao construir uma sessão real — os testes injetam a
sessão e não precisam da biblioteca.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from typing import Any

from .procedencia import registrar_sync

log = logging.getLogger(__name__)

FONTE = "JIRA"

TIMEOUT = (10, 30)
MAX_TENTATIVAS = 3
STATUS_RETENTAVEIS = frozenset({429, 500, 502, 503, 504})

PAGINA_PADRAO = 50      # a API do Service Desk limita a 50
LOTE_JQL = 100          # cards por requisição de description — a chamada cara

CAMPO_PLANO_DE_ACAO = "customfield_12627"

CABECALHOS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "X-ExperimentalApi": "opt-in",
}

# `Finding ID: <uuid>` no texto achatado da descrição. Tolerante a caixa e a
# espaço, deliberadamente NÃO tolerante a outro rótulo: aceitar variação
# esconderia a mudança de template que a métrica de saúde precisa expor.
_UUID_RE = re.compile(
    r"Finding\s+ID[:\s]+([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
    re.IGNORECASE,
)


def texto_adf(no: Any) -> str:
    """Achata um nó ADF (Atlassian Document Format) para texto puro.

    A descrição real tem lista, negrito e link — o texto vive nas folhas, e
    só descendo a árvore inteira o regex encontra o UUID."""
    if isinstance(no, dict):
        if no.get("type") == "text":
            return no.get("text", "")
        return "".join(texto_adf(filho) for filho in no.get("content", []))
    if isinstance(no, list):
        return "".join(texto_adf(filho) for filho in no)
    return ""


def extrair_finding_id(descricao_adf: Any) -> str:
    """UUID de `Finding ID: <uuid>`, ou vazio quando não há."""
    if not descricao_adf:
        return ""
    encontrado = _UUID_RE.search(texto_adf(descricao_adf))
    return encontrado.group(1) if encontrado else ""


class ClienteJira:
    """HTTP puro da API do Jira Service Desk. Não conhece finding nem risco."""

    def __init__(
        self,
        base_url: str,
        email: str,
        token: str,
        *,
        verify_tls: bool = True,
        sessao: Any = None,
    ) -> None:
        if not all([base_url, email, token]):
            raise ValueError("base_url, email e token são obrigatórios")

        self._base_url = base_url.rstrip("/")
        self._verify_tls = verify_tls
        self._sessao = sessao if sessao is not None else self._montar_sessao(email, token)

    def pagina_da_fila(
        self, service_desk_id: str, queue_id: str, inicio: int, limite: int
    ) -> list[dict]:
        """Uma página da fila. Lista vazia em erro — fila indisponível não pode
        derrubar o sync das outras fontes de contexto."""
        url = (
            f"{self._base_url}/rest/servicedeskapi/servicedesk"
            f"/{service_desk_id}/queue/{queue_id}/issue"
        )
        try:
            resposta = self._sessao.get(
                url,
                params={"start": inicio, "limit": limite},
                timeout=TIMEOUT,
                verify=self._verify_tls,
            )
        except Exception as erro:  # noqa: BLE001 — rede é esperada falhar
            log.warning("jira | falha na fila | inicio=%s | %s", inicio, erro)
            return []

        if resposta.status_code != 200:
            log.warning(
                "jira | fila | status=%s | inicio=%s", resposta.status_code, inicio
            )
            return []
        return resposta.json().get("values", [])

    def campos_em_lote(self, chaves: list[str], campos: list[str]) -> dict[str, dict]:
        """Campos de vários cards numa requisição JQL. Dict vazio em erro."""
        if not chaves:
            return {}
        url = f"{self._base_url}/rest/api/3/search/jql"
        try:
            resposta = self._sessao.post(
                url,
                json={
                    "jql": "key in (" + ",".join(chaves) + ")",
                    "fields": campos,
                    "maxResults": len(chaves),
                },
                timeout=TIMEOUT,
                verify=self._verify_tls,
            )
        except Exception as erro:  # noqa: BLE001
            log.warning("jira | falha no lote | chaves=%s | %s", len(chaves), erro)
            return {}

        if resposta.status_code != 200:
            log.warning(
                "jira | lote | status=%s | chaves=%s", resposta.status_code, len(chaves)
            )
            return {}
        return {
            issue["key"]: issue.get("fields", {})
            for issue in resposta.json().get("issues", [])
        }

    def _montar_sessao(self, email: str, token: str):
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        sessao = requests.Session()
        sessao.auth = (email, token)
        sessao.headers.update(CABECALHOS)
        adaptador = HTTPAdapter(
            max_retries=Retry(
                total=MAX_TENTATIVAS,
                backoff_factor=0.5,
                status_forcelist=list(STATUS_RETENTAVEIS),
                allowed_methods={"GET", "POST"},
                raise_on_status=False,
            )
        )
        sessao.mount("https://", adaptador)
        sessao.mount("http://", adaptador)
        return sessao


class ExtratorJira:
    """Traduz a fila do Service Desk para o que o sync precisa."""

    def __init__(
        self,
        cliente: ClienteJira,
        service_desk_id: str,
        queue_id: str,
        pagina: int = PAGINA_PADRAO,
    ) -> None:
        self._cliente = cliente
        self._sd = service_desk_id
        self._fila = queue_id
        self._pagina = pagina

    def tickets_da_fila(self) -> list[dict]:
        """`ticket_id`, `status` e `updated` de todos os cards da fila.

        Os três vêm na listagem, sem custo extra — é o que permite decidir
        quais cards merecem a busca cara de description."""
        tickets: list[dict] = []
        inicio = 0

        while True:
            valores = self._cliente.pagina_da_fila(
                self._sd, self._fila, inicio, self._pagina
            )
            for issue in valores:
                chave = issue.get("key", "")
                if not chave:
                    continue
                campos = issue.get("fields", {})
                tickets.append({
                    "ticket_id": chave,
                    "status": (campos.get("status") or {}).get("name", ""),
                    "updated": campos.get("updated", ""),
                })
            # Página incompleta é a última. Sem esta parada, laço infinito.
            if len(valores) < self._pagina:
                break
            inicio += len(valores)

        log.info("jira | fila | tickets=%s", len(tickets))
        return tickets

    def descricoes(self, chaves: list[str]) -> dict[str, dict]:
        """Description e plano de ação, em lotes de `LOTE_JQL`."""
        resultado: dict[str, dict] = {}
        for i in range(0, len(chaves), LOTE_JQL):
            lote = chaves[i: i + LOTE_JQL]
            resultado.update(
                self._cliente.campos_em_lote(lote, ["description", CAMPO_PLANO_DE_ACAO])
            )
        return resultado


@dataclass(frozen=True)
class ResultadoJira:
    tickets: int
    sem_finding_id: int
    rebuscados: int


def sincronizar_jira(extrator, conn) -> ResultadoJira:
    """Recarrega `jira_ticket` a partir da fila do Service Desk.

    Snapshot com cache: o card que saiu da fila é apagado, mas o `action_plan`
    dos que ficaram não é rebuscado à toa — o `updated` do Jira decide. Isso
    importa porque a busca de description é 1 requisição JQL por 100 cards.

    Fila vazia preserva o snapshot anterior: Jira fora do ar devolve lista
    vazia, e apagar tudo faria o export perder todos os vínculos de uma vez.
    """
    tickets = extrator.tickets_da_fila()
    if not tickets:
        log.warning("jira | fila vazia — snapshot anterior mantido")
        with conn.transaction(), conn.cursor() as cur:
            registrar_sync(cur, FONTE, "OK", 0, detail="fila vazia; snapshot mantido")
        return ResultadoJira(tickets=0, sem_finding_id=0, rebuscados=0)

    with conn.cursor() as cur:
        cur.execute("SELECT ticket_id, finding_id, action_plan, updated FROM jira_ticket")
        guardado = {l["ticket_id"]: l for l in cur.fetchall()}

    # Só paga a busca cara por card novo ou cujo `updated` mudou.
    a_buscar = [
        t["ticket_id"] for t in tickets
        if not t["updated"] or guardado.get(t["ticket_id"], {}).get("updated") != t["updated"]
    ]
    log.info(
        "jira | cache | reaproveitados=%s | a_buscar=%s",
        len(tickets) - len(a_buscar), len(a_buscar),
    )
    campos = extrator.descricoes(a_buscar) if a_buscar else {}

    linhas: list[tuple] = []
    sem_vinculo = 0
    for t in tickets:
        chave = t["ticket_id"]
        if chave in campos:
            finding_id = extrair_finding_id(campos[chave].get("description"))
            plano = texto_adf(campos[chave].get(CAMPO_PLANO_DE_ACAO))
        else:
            anterior = guardado.get(chave, {})
            finding_id = anterior.get("finding_id", "") or ""
            plano = anterior.get("action_plan", "") or ""
        if not finding_id:
            sem_vinculo += 1
        linhas.append((chave, finding_id, t["status"], plano, t["updated"]))

    agora = dt.datetime.now(dt.timezone.utc)
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO jira_ticket "
            "  (ticket_id, finding_id, status, action_plan, updated, collected_at) "
            "VALUES (%s, %s, %s, %s, %s, now()) "
            "ON CONFLICT (ticket_id) DO UPDATE SET "
            "  finding_id = EXCLUDED.finding_id, status = EXCLUDED.status, "
            "  action_plan = EXCLUDED.action_plan, updated = EXCLUDED.updated, "
            "  collected_at = EXCLUDED.collected_at",
            linhas,
        )
        # Snapshot: card fora da fila perde o vínculo, como decidido.
        cur.execute(
            "DELETE FROM jira_ticket WHERE ticket_id <> ALL(%s)",
            ([t["ticket_id"] for t in tickets],),
        )
        registrar_sync(
            cur, FONTE, "OK", len(tickets),
            detail=f"{sem_vinculo} sem finding_id; {len(a_buscar)} rebuscados",
            sincronizado_em=agora,
        )

    log.info(
        "jira | sync | tickets=%s | sem_finding_id=%s | rebuscados=%s",
        len(tickets), sem_vinculo, len(a_buscar),
    )
    return ResultadoJira(
        tickets=len(tickets), sem_finding_id=sem_vinculo, rebuscados=len(a_buscar)
    )


def extrator_de_jira(config) -> ExtratorJira:
    """Constrói o extrator a partir da configuração do motor."""
    return ExtratorJira(
        ClienteJira(
            base_url=config.jira_base_url or "",
            email=config.jira_email or "",
            token=config.jira_token or "",
            verify_tls=config.verify_tls,
        ),
        service_desk_id=config.jira_sd_service_desk_id or "",
        queue_id=config.jira_sd_queue_id or "",
    )
