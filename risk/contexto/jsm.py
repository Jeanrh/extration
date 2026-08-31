"""CMDB via Atlassian Assets (JSM): cliente AQL e mapeamento de atributos.

Porte de `src/cmdb/{client,extractor}.py` do extraction, sem o cache em disco.
Lá o `_load_cache`/`_save` gravava `referencia/*.json` e fazia uma sondagem por
`Updated` para decidir se reaproveitava o arquivo. Aqui isso não faz sentido:
o pod do CronJob é efêmero, o arquivo morreria com ele, e o snapshot já é
materializado no PostgreSQL — que é justamente o cache, com a vantagem de
sobreviver, ser auditável e virar JOIN.

`requests` é importado só na hora de construir uma sessão real, no mesmo
padrão de `ingestion/db.py`: os testes injetam a sessão e não precisam da
biblioteca.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

TIMEOUT = (10, 60)
MAX_TENTATIVAS = 3
STATUS_RETENTAVEIS = frozenset({429, 500, 502, 503, 504})
ESPERA_PADRAO_RATE_LIMIT = 5

CABECALHOS = {
    "X-Atlassian-Token": "no-check",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# Queries AQL e tamanho de página por entidade.
AQL_SIGLAS = 'objectSchemaId = 3 and objectType in ("Siglas")'
AQL_SERVIDORES = 'objectSchemaId = 3 and objectType in ("Servidores")'
AQL_URLS = 'objectSchemaId = 3 and objectType in ("URL")'
AQL_TIMES = 'objectSchemaId = 7 and objectType in ("Times Cockpit")'

PAGINA_SIGLAS = 1000
PAGINA_SERVIDORES = 100
PAGINA_URLS = 1000
PAGINA_TIMES = 100

# Troca de acentos e de ';' — porte literal do `_clear` do extraction. O ';'
# era proteção de CSV e aqui não seria necessário, mas manter a transformação
# idêntica é o que faz os valores baterem byte a byte na rodada de paridade.
_SUBSTITUICOES = [
    ("ã", "a"), ("á", "a"), ("à", "a"), ("â", "a"),
    ("Ã", "A"), ("Á", "A"), ("À", "A"), ("Â", "A"),
    ("ç", "c"), ("Ç", "C"),
    ("í", "i"), ("Í", "I"),
    ("ê", "e"), ("é", "e"), ("É", "E"), ("Ê", "E"),
    ("ó", "o"), ("Ó", "O"), ("ô", "o"), ("Ô", "O"), ("õ", "o"), ("Õ", "O"),
    ("ú", "u"), ("Ú", "U"),
    (";", ","),
]


def limpar(texto: str) -> str:
    for origem, destino in _SUBSTITUICOES:
        texto = texto.replace(origem, destino)
    return texto


def _primeiro_display(atributo: dict) -> str:
    """Primeiro displayValue / value / searchValue de um atributo do JSM."""
    valores = atributo.get("objectAttributeValues") or []
    if not valores:
        return ""
    primeiro = valores[0]
    return str(
        primeiro.get("displayValue")
        or primeiro.get("value")
        or primeiro.get("searchValue")
        or ""
    ).strip()


def _rotulo_e_chave(atributo: dict) -> tuple[str, str]:
    """(label, objectKey) de um atributo do tipo referência."""
    valores = atributo.get("objectAttributeValues") or []
    if not valores:
        return "", ""
    referenciado = valores[0].get("referencedObject") or {}
    rotulo = str(referenciado.get("label", "")).strip() or str(
        valores[0].get("displayValue", "")
    ).strip()
    chave = str(referenciado.get("objectKey", "")).strip() or str(
        valores[0].get("searchValue", "")
    ).strip()
    return rotulo, chave


class ClienteJSM:
    """Cliente HTTP da Atlassian Assets API. Não conhece o schema do CMDB."""

    def __init__(
        self,
        cloud_id: str,
        *,
        email: str = "",
        token: str = "",
        workspace_id: str = "",
        verify_tls: bool = True,
        sessao: Any = None,
        dormir: Callable[[int], None] | None = None,
    ) -> None:
        if not cloud_id:
            raise ValueError("cloud_id é obrigatório")

        import time

        self._cloud_id = cloud_id
        self._verify_tls = verify_tls
        self._dormir = dormir or time.sleep
        self._sessao = sessao if sessao is not None else self._montar_sessao(email, token)
        self._workspace_id = workspace_id or self._descobrir_workspace()

    # ------------------------------------------------------------------ #

    @property
    def _url_aql(self) -> str:
        return (
            f"https://api.atlassian.com/ex/jira/{self._cloud_id}"
            f"/jsm/assets/workspace/{self._workspace_id}/v1/object/aql"
        )

    def consultar_aql(self, consulta: str, inicio: int, maximo: int) -> dict:
        """Uma página de uma query AQL. Respeita `Retry-After` no 429."""
        resposta = self._sessao.post(
            self._url_aql,
            params={"startAt": inicio, "maxResults": maximo, "includeAttributes": "true"},
            json={"qlQuery": consulta},
            verify=self._verify_tls,
            timeout=TIMEOUT,
        )

        if resposta.status_code == 429:
            espera = int(resposta.headers.get("Retry-After", ESPERA_PADRAO_RATE_LIMIT))
            log.warning("jsm | rate limit | espera=%ss", espera)
            self._dormir(espera)
            return self.consultar_aql(consulta, inicio, maximo)

        resposta.raise_for_status()
        return resposta.json()

    def buscar_tudo(self, consulta: str, maximo: int = 100) -> list[dict]:
        """Todas as páginas de uma query AQL.

        Erro de rede encerra a paginação devolvendo o que já veio, em vez de
        propagar: o sync grava o parcial e as outras entidades seguem com o
        snapshot anterior. Ficar sem contexto é pior do que ficar com contexto
        incompleto.
        """
        itens: list[dict] = []
        inicio = 0
        paginas = 0

        while True:
            try:
                dados = self.consultar_aql(consulta, inicio, maximo)
            except Exception as erro:  # noqa: BLE001 — rede é esperada falhar
                log.error("jsm | falha na paginação | %s", erro)
                break

            valores = dados.get("values", [])
            paginas += 1
            itens.extend(valores)

            if not valores or bool(dados.get("isLast", False)):
                break
            inicio += len(valores)

        log.info("jsm | busca concluída | paginas=%s | itens=%s", paginas, len(itens))
        return itens

    # ------------------------------------------------------------------ #

    def _descobrir_workspace(self) -> str:
        url = f"https://api.atlassian.com/ex/jira/{self._cloud_id}/jsm/assets/workspace"
        try:
            resposta = self._sessao.get(url, verify=self._verify_tls, timeout=TIMEOUT)
            resposta.raise_for_status()
            workspaces = resposta.json().get("values", [])
            if not workspaces:
                raise RuntimeError("nenhum workspace encontrado para este cloud_id")
            return workspaces[0].get("workspaceId", "")
        except Exception as erro:
            raise RuntimeError(
                f"falha ao descobrir workspace_id: {erro}. Defina JIRA_WORKSPACE_ID."
            ) from erro

    def _montar_sessao(self, email: str, token: str):
        import requests
        from requests.adapters import HTTPAdapter
        from requests.auth import HTTPBasicAuth
        from urllib3.util.retry import Retry

        if not email or not token:
            raise ValueError("email e token são obrigatórios para a sessão real")

        sessao = requests.Session()
        sessao.auth = HTTPBasicAuth(email, token)
        sessao.headers.update(CABECALHOS)
        adaptador = HTTPAdapter(
            max_retries=Retry(
                total=MAX_TENTATIVAS,
                backoff_factor=2,
                status_forcelist=STATUS_RETENTAVEIS,
                allowed_methods={"POST", "GET"},
                raise_on_status=False,
            )
        )
        sessao.mount("https://", adaptador)
        sessao.mount("http://", adaptador)
        return sessao


class ExtratorCMDB:
    """Traduz objetos do JSM para os dicionários que `sincronizar_cmdb` espera.

    Os números são `objectTypeAttributeId` do schema do JSM. Não há nome
    simbólico para eles do outro lado — mudar um significa que alguém mexeu no
    schema, e é por isso que ficam visíveis aqui em vez de escondidos.
    """

    def __init__(self, cliente: ClienteJSM | None) -> None:
        self._cliente = cliente

    # -- entidades ----------------------------------------------------- #

    def extract_acronyms(self, max_age_hours: float | None = None) -> list[dict]:
        return self._buscar(AQL_SIGLAS, PAGINA_SIGLAS, self._mapear_sigla, "sigla")

    def extract_servers(self, max_age_hours: float | None = None) -> list[dict]:
        return self._buscar(
            AQL_SERVIDORES, PAGINA_SERVIDORES, self._mapear_servidor, "servidor"
        )

    def extract_urls(self, max_age_hours: float | None = None) -> list[dict]:
        return self._buscar(AQL_URLS, PAGINA_URLS, self._mapear_url, "url")

    def extract_cockpits(self, max_age_hours: float | None = None) -> list[dict]:
        return self._buscar(AQL_TIMES, PAGINA_TIMES, self._mapear_time, "time")

    def _buscar(self, consulta: str, pagina: int, mapeador, rotulo: str) -> list[dict]:
        brutos = self._cliente.buscar_tudo(consulta, pagina)
        return self._mapear_todos(brutos, mapeador, rotulo)

    def _mapear_todos(self, itens: Iterable[dict], mapeador, rotulo: str) -> list[dict]:
        """Um objeto malformado não pode custar o contexto de todo o resto.

        Os mapeadores NÃO são defensivos de propósito: `attributes: None`
        estoura aqui e o id vai para o log. Tolerar produziria um registro
        em branco, descartado silenciosamente na montagem das linhas — e
        ninguém ficaria sabendo que o CMDB devolveu lixo."""
        resultado = []
        for item in itens:
            try:
                resultado.append(mapeador(item))
            except Exception as erro:  # noqa: BLE001
                log.error("jsm | %s | erro de parse | id=%s | %s",
                          rotulo, item.get("id", "?"), erro)
        return resultado

    # -- mapeamentos --------------------------------------------------- #

    def _mapear_sigla(self, item: dict) -> dict:
        obj = {
            "id": str(item.get("id", "")), "key": str(item.get("objectKey", "")),
            "acronym": "", "name": "", "status": "", "domain": "", "subdomain": "",
            "BIA": "", "PCI": "", "criticality": "", "created": "", "updated": "",
            "infrastructure": "", "service": "", "squad": "", "squadid": "",
            "team": "", "teamid": "",
        }
        for atributo in item.get("attributes", []):
            aid = str(atributo.get("objectTypeAttributeId", ""))
            valor = _primeiro_display(atributo)
            if aid == "52":
                obj["acronym"] = limpar(valor)
            elif aid == "49":
                obj["name"] = limpar(valor)
            elif aid == "71":
                obj["status"] = limpar(valor)
            elif aid == "457":
                obj["domain"] = limpar(valor)
            elif aid == "458":
                obj["subdomain"] = limpar(valor)
            elif aid == "1189":
                obj["BIA"] = limpar(valor)
            elif aid == "78":
                obj["PCI"] = limpar(valor)
            elif aid == "69":
                obj["criticality"] = limpar(valor)
            elif aid == "50":
                obj["created"] = valor
            elif aid == "79":
                obj["updated"] = valor
            elif aid == "612":
                obj["infrastructure"] = limpar(valor)
            elif aid == "4278":
                obj["service"] = limpar(valor)
            elif aid == "2822":
                rotulo, chave = _rotulo_e_chave(atributo)
                obj["squad"], obj["squadid"] = limpar(rotulo), chave
            elif aid == "4205":
                rotulo, chave = _rotulo_e_chave(atributo)
                obj["team"], obj["teamid"] = limpar(rotulo), chave
        return obj

    def _mapear_servidor(self, item: dict) -> dict:
        por_id = {
            str(a.get("objectTypeAttributeId", "")): _primeiro_display(a)
            for a in item.get("attributes", [])
        }

        def escolher(*ids: str) -> str:
            """O mesmo campo tem ids diferentes conforme o tipo do objeto."""
            return next((por_id[i] for i in ids if por_id.get(i)), "")

        return {
            "id": str(item.get("id", "")),
            "objectKey": str(item.get("objectKey", "")),
            "name": escolher("94") or str(item.get("label", "")).strip(),
            "status": escolher("3484", "4509"),
            "ipv4": escolher("4386", "4715", "2399"),
            "environment": escolher("4387", "4901"),
            "acronym": escolher("4391", "2373", "4596"),
            "os": escolher("4394", "2374", "4529"),
            "accountname": escolher("4396", "4701"),
            "tenableid": escolher("4397", "4702"),
            "infrastructure": escolher("4401", "4512"),
            "criticality": escolher("69"),
            "platform": escolher("4398"),
            "cluster": escolher("4403"),
            "layer": escolher("4399"),
            "created": escolher("50"),
            "updated": escolher("79"),
        }

    def _mapear_url(self, item: dict) -> dict:
        obj = {
            "id": str(item.get("id", "")), "objectKey": str(item.get("objectKey", "")),
            "name": "", "status": "", "environment": "", "domain": "",
            "squad": "", "squadid": "", "acronym": "", "acronymid": "",
            "pci": "", "alliance": "", "allianceid": "", "created": "", "updated": "",
        }
        for atributo in item.get("attributes", []):
            aid = str(atributo.get("objectTypeAttributeId", ""))
            valor = _primeiro_display(atributo)
            brutos = atributo.get("objectAttributeValues") or []
            if aid == "94":
                obj["name"] = limpar(valor)
            elif aid == "3484":
                obj["status"] = limpar(valor)
            elif aid == "4387":
                obj["environment"] = limpar(valor)
            elif aid == "5060":
                obj["domain"] = limpar(valor)
            elif aid == "5027":
                obj["pci"] = limpar(valor)
            elif aid in ("5062", "5063", "5274"):
                campo = {"5062": "squad", "5063": "acronym", "5274": "alliance"}[aid]
                obj[campo] = limpar(valor)
                if brutos:
                    obj[campo + "id"] = limpar(str(brutos[0].get("searchValue", "")))
            elif aid == "95":
                obj["created"] = limpar(valor)
            elif aid == "96":
                obj["updated"] = limpar(valor)
        return obj

    def _mapear_time(self, item: dict) -> dict:
        obj = {
            "id": str(item.get("id", "")), "key": str(item.get("objectKey", "")),
            "name": "", "status": "", "tribo": "", "alianca": "", "vp": "",
            "squad": "", "squadid": "", "team": "", "teamid": "",
            "created": "", "updated": "",
        }
        for atributo in item.get("attributes", []):
            aid = str(atributo.get("objectTypeAttributeId", ""))
            valor = _primeiro_display(atributo)
            if aid == "1071":
                obj["name"] = limpar(valor)
            elif aid in ("71", "1074"):
                obj["status"] = limpar(valor)
            elif aid == "3482":
                obj["vp"] = valor
            elif aid == "1312":
                obj["alianca"] = valor
            elif aid == "1083":
                obj["tribo"] = valor
            elif aid == "1072":
                obj["created"] = valor
            elif aid == "1073":
                obj["updated"] = valor
            elif aid == "2822":
                rotulo, chave = _rotulo_e_chave(atributo)
                obj["squad"], obj["squadid"] = limpar(rotulo), chave
            elif aid == "4205":
                rotulo, chave = _rotulo_e_chave(atributo)
                obj["team"], obj["teamid"] = limpar(rotulo), chave
        return obj


def extrator_de_cmdb(config) -> ExtratorCMDB:
    """Constrói o extrator a partir da configuração do motor."""
    import os

    cloud_id = config.jira_cloud_id or ""
    if not cloud_id:
        raise ValueError("JIRA_CLOUD_ID é obrigatório para sincronizar o CMDB")

    cliente = ClienteJSM(
        cloud_id=cloud_id,
        email=config.jira_email or "",
        token=config.jira_token or "",
        workspace_id=os.getenv("JIRA_WORKSPACE_ID", ""),
        verify_tls=config.verify_tls,
    )
    return ExtratorCMDB(cliente)
