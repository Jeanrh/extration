"""Keywords de camada, lidas do Vault (KV v2, AppRole).

Porte reduzido de `src/vault/client.py` do extraction: o motor só lê um
segredo, então escrita, listagem, metadados e renovação de token ficaram de
fora — código não portado é código que não precisa ser mantido.

O segredo continua sendo a fonte de verdade e é lido em cada execução, sem
virar tabela: é pequeno, é segredo, e materializá-lo no banco só espalharia
credencial de negócio por mais um lugar.

Vault indisponível **não** derruba o motor. Sem o índice, `resolver_camada`
cai no fallback por `plugin.family`, que sozinho já resolve boa parte — e
falhar aqui pararia a priorização de todo o backlog.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..derivacoes.camada import indexar_keywords

log = logging.getLogger(__name__)

TIMEOUT = (5, 30)  # (connect, read)


def _requests():
    """Import tardio: `run` sem Vault configurado não deve exigir a biblioteca."""
    import requests

    return requests


def ler_segredo(
    url: str,
    role_id: str,
    secret_id: str,
    mount: str,
    caminho: str,
    verify_tls: bool = True,
) -> dict[str, Any]:
    """Autentica por AppRole e devolve `data.data` do segredo KV v2."""
    requests = _requests()
    base = url.rstrip("/")

    login = requests.post(
        f"{base}/v1/auth/approle/login",
        json={"role_id": role_id, "secret_id": secret_id},
        verify=verify_tls,
        timeout=TIMEOUT,
    )
    if not login.ok:
        raise RuntimeError(f"login AppRole falhou ({login.status_code})")
    token = login.json()["auth"]["client_token"]

    resposta = requests.get(
        f"{base}/v1/{mount.strip('/')}/data/{caminho.strip('/')}",
        headers={"X-Vault-Token": token},
        verify=verify_tls,
        timeout=TIMEOUT,
    )
    if not resposta.ok:
        raise RuntimeError(f"leitura do segredo falhou ({resposta.status_code})")
    return resposta.json()["data"]["data"]


def keywords_de_camada(config) -> dict[str, list[str]]:
    """Índice {camada: [keyword]} do Vault. Dicionário vazio quando indisponível."""
    try:
        url = os.environ["VAULT_ADDR"]
        role_id = os.environ["VAULT_ROLE_ID"]
        secret_id = os.environ["VAULT_SECRET_ID"]
    except KeyError as ausente:
        log.warning(
            "vault | variável ausente (%s) — camada cai no fallback por plugin.family",
            ausente,
        )
        return {}

    try:
        segredo = ler_segredo(
            url,
            role_id,
            secret_id,
            config.vault_mount,
            config.vault_layer_secret_path,
            verify_tls=config.verify_tls,
        )
    except Exception as erro:  # noqa: BLE001 — qualquer falha degrada, não derruba
        log.warning("vault | indisponível (%s) — camada cai no fallback", erro)
        return {}

    indice = indexar_keywords(segredo)
    log.info("vault | índice de camada | camadas=%s", sorted(indice))
    return indice
