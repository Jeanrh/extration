"""Exceções do pipeline.

A distinção importa no tratamento (seção 12): `ErroIntegridade` e
`ErroParse` entram no fluxo de tentativa/quarentena do arquivo;
`ErroConfiguracao` aborta o job inteiro, porque tentar de novo não resolve.
"""

from __future__ import annotations


class ErroIngestao(Exception):
    """Base de tudo que o pipeline levanta."""


class ErroConfiguracao(ErroIngestao):
    """Configuração ausente ou inválida. Não adianta retentar."""


class ErroIntegridade(ErroIngestao):
    """md5 divergente ou contagem de updates/deletes fora do manifest.

    Causa comum é download truncado — a retentativa costuma resolver
    (seção 12.3)."""


class ErroParse(ErroIngestao):
    """Payload ilegível: gzip corrompido, JSON inválido, campo obrigatório
    ausente."""


class ErroVersaoSchema(ErroIngestao):
    """A versão do payload divergiu da esperada.

    NÃO é fatal: a seção 12.4 manda alertar e continuar processando, porque a
    mudança pode ser aditiva. Existe como tipo para o alerta ser explícito."""
