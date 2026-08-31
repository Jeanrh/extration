"""Threat intel pela API clássica do Tenable.

A única fonte que o Data Stream não substitui. `cve_category` é um **filtro** do
export, não um campo: a resposta nunca diz a que categoria o finding pertence,
só devolve o subconjunto que passou pelas sete categorias abaixo. Por isso o
motor guarda apenas os `finding_id` — é literalmente toda a informação que a
API oferece.

Porte de `src/tenable/client.py` e de `extract_threat_intel`, reduzido ao que o
motor usa: iniciar o export, esperar, baixar e cancelar. Export de VM/WAS não
entra aqui porque o Data Stream já cobre isso no banco.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import tempfile
import uuid as _uuid
from typing import Any, Callable

log = logging.getLogger(__name__)

URL_BASE = "https://cloud.tenable.com"
CAMINHO_EXPORT_VM = "/vulns/export"

TIMEOUT_EXPORT = (10, 30)
TIMEOUT_STATUS = (10, 20)
TIMEOUT_CHUNK = (10, 120)

ESPERA_ENFILEIRADO = 10
ESPERA_PROCESSANDO = 20
# ~10 minutos. O extraction usa o mesmo teto reduzido para o intel: se não
# finalizar, é melhor seguir com o snapshot anterior do que segurar o job.
MAX_TENTATIVAS = 30

# Janela do export. Ameaça ativa é um sinal do presente — e é por isso que o
# snapshot não é acumulado: finding fora desta janela volta a valer 10.
DIAS_JANELA = 90

CATEGORIAS_AMEACA = [
    "cisa known exploitable",
    "emerging threats",
    "in the news",
    "persistently exploited",
    "ransomware",
    "recent active exploitation",
    "top 50 vpr",
]


class ClienteTenable:
    """Cliente da API clássica, reduzido ao ciclo de export."""

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        *,
        verify_tls: bool = True,
        sessao: Any = None,
        dormir: Callable[[int], None] | None = None,
    ) -> None:
        if not access_key or not secret_key:
            raise ValueError("access_key e secret_key são obrigatórios")

        import time

        self._access_key = access_key
        self._secret_key = secret_key
        self._verify_tls = verify_tls
        self._dormir = dormir or time.sleep
        self._sessao = sessao if sessao is not None else self._montar_sessao()

    def _cabecalhos(self, chave_idempotencia: str | None = None) -> dict:
        cabecalhos = {
            "accept": "application/json",
            "content-type": "application/json",
            "X-ApiKeys": f"accessKey={self._access_key};secretKey={self._secret_key}",
        }
        if chave_idempotencia:
            cabecalhos["Idempotency-Key"] = chave_idempotencia
        return cabecalhos

    def iniciar_export(self, caminho: str, payload: dict) -> str:
        resposta = self._sessao.post(
            f"{URL_BASE}{caminho}",
            json=payload,
            headers=self._cabecalhos(str(_uuid.uuid4())),
            verify=self._verify_tls,
            timeout=TIMEOUT_EXPORT,
        )
        resposta.raise_for_status()
        export_uuid = resposta.json()["export_uuid"]
        log.info("tenable | export iniciado | uuid=%s", export_uuid)
        return export_uuid

    def status(self, caminho: str, export_uuid: str) -> dict:
        resposta = self._sessao.get(
            f"{URL_BASE}{caminho}/{export_uuid}/status",
            headers=self._cabecalhos(),
            verify=self._verify_tls,
            timeout=TIMEOUT_STATUS,
        )
        resposta.raise_for_status()
        return resposta.json()

    def baixar_chunk(self, caminho: str, export_uuid: str, chunk_id: int) -> list:
        """Streaming para disco antes de parsear.

        Um chunk pode trazer ~128 mil findings; carregar bytes, string e
        objetos ao mesmo tempo estoura a memória do pod."""
        resposta = self._sessao.get(
            f"{URL_BASE}{caminho}/{export_uuid}/chunks/{chunk_id}",
            headers=self._cabecalhos(),
            verify=self._verify_tls,
            timeout=TIMEOUT_CHUNK,
            stream=True,
        )
        resposta.raise_for_status()
        with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024) as temporario:
            for bloco in resposta.iter_content(chunk_size=1024 * 1024):
                temporario.write(bloco)
            temporario.seek(0)
            return json.load(temporario)

    def cancelar_export(self, caminho: str, export_uuid: str) -> None:
        """Nunca levanta: cancelar é higiene, não pode mascarar o erro real."""
        try:
            self._sessao.delete(
                f"{URL_BASE}{caminho}/{export_uuid}/cancel",
                headers=self._cabecalhos(),
                verify=self._verify_tls,
                timeout=TIMEOUT_STATUS,
            )
        except Exception as erro:  # noqa: BLE001
            log.warning("tenable | falha ao cancelar export | %s", erro)

    def aguardar_e_baixar(
        self, caminho: str, export_uuid: str, max_tentativas: int = MAX_TENTATIVAS
    ) -> list[dict]:
        """Espera FINISHED e baixa os chunks. Cancela o export em qualquer saída ruim."""
        tentativa = 0
        try:
            while tentativa < max_tentativas:
                dados = self.status(caminho, export_uuid)
                situacao = dados.get("status", "")
                chunks = dados.get("chunks_available", [])

                if situacao == "FINISHED":
                    achados: list[dict] = []
                    for chunk_id in chunks:
                        achados.extend(self.baixar_chunk(caminho, export_uuid, chunk_id))
                    return achados

                if situacao in {"FAILED", "CANCELLED"}:
                    raise RuntimeError(f"export {export_uuid} terminou como {situacao}")

                self._dormir(
                    ESPERA_ENFILEIRADO if situacao == "QUEUED" else ESPERA_PROCESSANDO
                )
                tentativa += 1

            raise TimeoutError(
                f"export {export_uuid} não finalizou em {max_tentativas} tentativas"
            )
        except Exception:
            self.cancelar_export(caminho, export_uuid)
            raise

    def _montar_sessao(self):
        import requests

        return requests.Session()


class ExtratorIntel:
    """Devolve os `finding_id` classificados como ameaça ativa pelo Tenable."""

    def __init__(
        self,
        cliente: ClienteTenable,
        max_tentativas: int = MAX_TENTATIVAS,
        dias_janela: int = DIAS_JANELA,
    ) -> None:
        self._cliente = cliente
        self._max_tentativas = max_tentativas
        self._dias_janela = dias_janela

    def _payload(self) -> dict:
        desde = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self._dias_janela)
        return {
            "num_assets": 300,
            "filters": {
                "severity": ["low", "medium", "high", "critical"],
                "cve_category": CATEGORIAS_AMEACA,
                "state": ["OPEN", "REOPENED"],
                "last_found": int(desde.timestamp()),
            },
        }

    def extract_threat_intel(self) -> list[dict]:
        """Lista vazia quando o export falha ou estoura o tempo.

        É contrato com `sincronizar_threat_intel`: lista vazia faz o sync
        preservar o snapshot anterior. Levantar aqui rebaixaria toda
        vulnerabilidade de ameaça ativa para nota 10 de uma só vez.
        """
        try:
            export_uuid = self._cliente.iniciar_export(CAMINHO_EXPORT_VM, self._payload())
            brutos = self._cliente.aguardar_e_baixar(
                CAMINHO_EXPORT_VM, export_uuid, self._max_tentativas
            )
        except Exception as erro:  # noqa: BLE001
            log.warning("intel | export não concluído (%s) — snapshot mantido", erro)
            return []

        achados = [
            {"finding_id": item.get("finding_id", "")}
            for item in brutos
            if item.get("finding_id")
        ]
        log.info("intel | export concluído | findings=%s", len(achados))
        return achados


def extrator_de_intel(config) -> ExtratorIntel:
    return ExtratorIntel(
        ClienteTenable(
            access_key=config.tenable_access_key or "",
            secret_key=config.tenable_secret_key or "",
            verify_tls=config.verify_tls,
        )
    )
