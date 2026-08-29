"""Os dois clientes HTTP: CMDB (JSM/Assets) e threat intel (API clássica).

Nenhum teste toca a rede — a sessão é injetada. Isso também mantém a suíte
rodando numa máquina sem `requests`, porque os módulos importam a biblioteca
só na hora de construir uma sessão de verdade.
"""

from __future__ import annotations

import pytest

from conftest import RAIZ  # noqa: F401  (garante sys.path)

from risk.contexto.jsm import ClienteJSM, ExtratorCMDB, limpar
from risk.contexto.tenable import CATEGORIAS_AMEACA, ClienteTenable, ExtratorIntel


# ---------------------------------------------------------------------------
# Sessão falsa
# ---------------------------------------------------------------------------


class RespostaFalsa:
    def __init__(self, corpo=None, status=200, headers=None):
        self._corpo = corpo if corpo is not None else {}
        self.status_code = status
        self.headers = headers or {}
        self.ok = status < 400

    def json(self):
        return self._corpo

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=None):
        import json

        yield json.dumps(self._corpo).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class SessaoFalsa:
    """Devolve as respostas na ordem em que foram programadas, por método."""

    def __init__(self, post=None, get=None):
        self._post = list(post or [])
        self._get = list(get or [])
        self.chamadas: list[tuple] = []

    def post(self, url, **kwargs):
        self.chamadas.append(("POST", url, kwargs))
        return self._post.pop(0)

    def get(self, url, **kwargs):
        self.chamadas.append(("GET", url, kwargs))
        return self._get.pop(0)

    def delete(self, url, **kwargs):
        self.chamadas.append(("DELETE", url, kwargs))
        return RespostaFalsa()


def _cliente_jsm(sessao) -> ClienteJSM:
    return ClienteJSM(cloud_id="cloud-1", workspace_id="ws-1", sessao=sessao)


def _pagina(valores, is_last=False):
    return RespostaFalsa({"values": valores, "isLast": is_last, "total": len(valores)})


# ---------------------------------------------------------------------------
# Paginação do JSM
# ---------------------------------------------------------------------------


def test_busca_percorre_todas_as_paginas_ate_is_last():
    sessao = SessaoFalsa(post=[
        _pagina([{"id": 1}, {"id": 2}]),
        _pagina([{"id": 3}], is_last=True),
    ])
    assert _cliente_jsm(sessao).buscar_tudo("objectType in (\"Siglas\")", maximo=2) == [
        {"id": 1}, {"id": 2}, {"id": 3}
    ]


def test_pagina_vazia_encerra_a_busca():
    """Sem isto, um `isLast` que nunca vem viraria laço infinito contra a API."""
    sessao = SessaoFalsa(post=[_pagina([{"id": 1}]), _pagina([])])
    assert _cliente_jsm(sessao).buscar_tudo("q", maximo=1) == [{"id": 1}]


def test_rate_limit_respeita_o_retry_after():
    sessao = SessaoFalsa(post=[
        RespostaFalsa({}, status=429, headers={"Retry-After": "7"}),
        _pagina([{"id": 1}], is_last=True),
    ])
    esperas: list[int] = []
    cliente = ClienteJSM(
        cloud_id="c", workspace_id="w", sessao=sessao, dormir=esperas.append
    )

    assert cliente.buscar_tudo("q") == [{"id": 1}]
    assert esperas == [7]


def test_erro_de_rede_interrompe_sem_perder_o_que_ja_veio():
    """Meia página é melhor que exceção: o sync grava o que conseguiu ler e o
    snapshot anterior das outras entidades continua de pé."""
    class SessaoQueFalha(SessaoFalsa):
        def post(self, url, **kwargs):
            if self._post:
                return super().post(url, **kwargs)
            raise ConnectionError("conexão caiu")

    sessao = SessaoQueFalha(post=[_pagina([{"id": 1}])])
    assert _cliente_jsm(sessao).buscar_tudo("q", maximo=1) == [{"id": 1}]


# ---------------------------------------------------------------------------
# Mapeamento de atributos
# ---------------------------------------------------------------------------


def _atributo(attr_id, display):
    return {
        "objectTypeAttributeId": attr_id,
        "objectAttributeValues": [{"displayValue": display}],
    }


def test_sigla_mapeia_os_atributos_que_movem_o_vetor_py():
    item = {
        "id": 1,
        "objectKey": "SIG-1",
        "attributes": [
            _atributo("52", "GTEC"),
            _atributo("49", "GTeC - Gestão de Terminais"),
            _atributo("1189", "Alto"),      # BIA
            _atributo("78", "PCI"),
            _atributo("69", "Crise"),       # criticidade
            _atributo("457", "Tecnologia"),
        ],
    }
    sigla = ExtratorCMDB(None)._mapear_sigla(item)

    assert sigla["acronym"] == "GTEC"
    assert sigla["BIA"] == "Alto"
    assert sigla["PCI"] == "PCI"
    assert sigla["criticality"] == "Crise"
    assert sigla["domain"] == "Tecnologia"


def test_servidor_usa_o_primeiro_id_preenchido_da_lista_de_fallback():
    """O mesmo campo tem IDs diferentes conforme o tipo de objeto no JSM."""
    item = {
        "id": 2,
        "attributes": [
            _atributo("94", "srv-app-01"),
            _atributo("4715", "10.0.0.7"),   # ipv4 pelo segundo id da lista
            _atributo("2373", "GTEC"),        # acronym pelo segundo id
        ],
    }
    servidor = ExtratorCMDB(None)._mapear_servidor(item)

    assert servidor["name"] == "srv-app-01"
    assert servidor["ipv4"] == "10.0.0.7"
    assert servidor["acronym"] == "GTEC"


def test_item_malformado_nao_derruba_o_lote():
    """Um objeto estranho no CMDB não pode custar o contexto de todo o resto."""
    extrator = ExtratorCMDB(None)
    itens = [{"id": 1, "attributes": [_atributo("52", "GTEC")]}, {"attributes": None}]
    assert len(extrator._mapear_todos(itens, extrator._mapear_sigla, "sigla")) == 1


def test_limpar_remove_acentos_e_ponto_e_virgula():
    """Porte literal do extraction, inclusive a troca de ';' — os valores
    precisam bater byte a byte com o CSV na rodada de paridade."""
    assert limpar("Gestão; Operação") == "Gestao, Operacao"


# ---------------------------------------------------------------------------
# Threat intel — API clássica
# ---------------------------------------------------------------------------


def _cliente_tenable(sessao, dormir=None):
    return ClienteTenable(
        access_key="ak", secret_key="sk", sessao=sessao, dormir=dormir or (lambda _: None)
    )


def test_export_inicia_espera_e_baixa_os_chunks():
    sessao = SessaoFalsa(
        post=[RespostaFalsa({"export_uuid": "u-1"})],
        get=[
            RespostaFalsa({"status": "PROCESSING", "chunks_available": []}),
            RespostaFalsa({"status": "FINISHED", "chunks_available": [1]}),
            RespostaFalsa([{"finding_id": "f-1"}]),
        ],
    )
    intel = ExtratorIntel(_cliente_tenable(sessao)).extract_threat_intel()
    assert intel == [{"finding_id": "f-1"}]


def test_o_filtro_carrega_as_sete_categorias_de_ameaca():
    """`cve_category` é filtro, não campo: se ele sair do payload, o export
    devolve a base inteira e todo finding vira "ameaça ativa"."""
    sessao = SessaoFalsa(
        post=[RespostaFalsa({"export_uuid": "u-1"})],
        get=[RespostaFalsa({"status": "FINISHED", "chunks_available": []})],
    )
    ExtratorIntel(_cliente_tenable(sessao)).extract_threat_intel()

    _metodo, _url, kwargs = sessao.chamadas[0]
    filtros = kwargs["json"]["filters"]
    assert filtros["cve_category"] == CATEGORIAS_AMEACA
    assert len(CATEGORIAS_AMEACA) == 7
    assert filtros["state"] == ["OPEN", "REOPENED"]


def test_timeout_do_export_devolve_lista_vazia_e_cancela():
    """Lista vazia é o contrato com `sincronizar_threat_intel`, que nesse caso
    preserva o snapshot anterior em vez de rebaixar todo mundo para nota 10."""
    sessao = SessaoFalsa(
        post=[RespostaFalsa({"export_uuid": "u-1"})],
        get=[RespostaFalsa({"status": "PROCESSING", "chunks_available": []})] * 3,
    )
    extrator = ExtratorIntel(_cliente_tenable(sessao), max_tentativas=3)

    assert extrator.extract_threat_intel() == []
    assert any(chamada[0] == "DELETE" for chamada in sessao.chamadas), "cancela o export"


def test_export_que_falha_tambem_devolve_lista_vazia():
    sessao = SessaoFalsa(
        post=[RespostaFalsa({"export_uuid": "u-1"})],
        get=[RespostaFalsa({"status": "FAILED", "chunks_available": []})],
    )
    assert ExtratorIntel(_cliente_tenable(sessao)).extract_threat_intel() == []


def test_credenciais_obrigatorias():
    with pytest.raises(ValueError):
        ClienteTenable(access_key="", secret_key="sk", sessao=SessaoFalsa())
