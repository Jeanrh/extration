"""Leitura incremental de payloads Tenable comprimidos.

Cada passe abre o gzip novamente: metadados, findings, plugins e recasts nunca
coexistem como coleções em memória. O módulo ``payload`` continua sendo a única
fonte do mapeamento dos registros para as linhas de staging.
"""

from __future__ import annotations

import datetime as dt
import gzip
import re
from pathlib import Path
from typing import Any, Iterator, Mapping

import ijson

from .config import PRODUTO_VM, TipoPayload
from .erros import ErroIntegridade, ErroParse
from .manifest import EntradaPayload
from .payload import (
    LinhaFinding,
    LinhaPlugin,
    LinhaRecast,
    _achatar_delete,
    _achatar_plugin,
    _achatar_vm,
    _achatar_was,
    achatar_registro_enriched,
)

_ESCALARES = {
    "payload_id",
    "version",
    "type",
    "count_updated",
    "count_deleted",
    "first_ts",
    "last_ts",
}
_EVENTOS_ESCALARES = {"null", "boolean", "integer", "double", "number", "string"}
_DECIMAL_INTEIRO = re.compile(r"^[0-9]+$")
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


class PayloadStream:
    """Envelope validado com geradores independentes para cada staging."""

    def __init__(self, path: Path, tipo: TipoPayload, entrada: EntradaPayload):
        self.path = Path(path)
        self.tipo = tipo
        self.entrada = entrada
        self._valores, self._presentes, self._arrays = self._ler_metadados()
        self._validar_envelope()

        self.version: int = self._valores["version"]
        self.count_updated: int = self._valores["count_updated"]
        self.count_deleted: int = self._valores["count_deleted"]
        self.num_updates_lidos = 0
        self.num_deletes_lidos = 0
        self.findings_mapeados = 0
        self.plugins_mapeados = 0
        self.recasts_mapeados = 0
        self._contagem_percorrida = False

    def _ler_metadados(self) -> tuple[dict[str, Any], set[str], set[str]]:
        valores: dict[str, Any] = {}
        presentes: set[str] = set()
        arrays: set[str] = set()
        raiz_iniciada = False
        raiz_encerrada = False
        try:
            with gzip.open(self.path, "rb") as arquivo:
                for prefixo, evento, valor in ijson.parse(arquivo, use_float=True):
                    if prefixo == "" and evento == "start_map":
                        if raiz_iniciada:
                            raise ErroParse(f"{self.entrada.path}: envelope duplicado")
                        raiz_iniciada = True
                    elif prefixo == "" and evento == "end_map":
                        raiz_encerrada = True
                    elif prefixo == "" and not raiz_iniciada:
                        raise ErroParse(
                            f"{self.entrada.path}: payload não é um objeto JSON"
                        )

                    if prefixo in {"updates", "deletes"}:
                        if evento == "start_array":
                            if prefixo in presentes:
                                raise ErroParse(
                                    f"{self.entrada.path}: campo {prefixo!r} duplicado"
                                )
                            presentes.add(prefixo)
                            arrays.add(prefixo)
                        elif evento not in {"end_array", "map_key"}:
                            raise ErroParse(
                                f"{self.entrada.path}: {prefixo!r} deve ser uma lista"
                            )
                    elif prefixo in _ESCALARES and evento in _EVENTOS_ESCALARES:
                        if prefixo in presentes:
                            raise ErroParse(
                                f"{self.entrada.path}: campo {prefixo!r} duplicado"
                            )
                        presentes.add(prefixo)
                        valores[prefixo] = valor
                    elif prefixo in _ESCALARES and evento in {"start_map", "start_array"}:
                        raise ErroParse(
                            f"{self.entrada.path}: {prefixo!r} deve ser escalar"
                        )
        except (OSError, EOFError, UnicodeDecodeError, ijson.JSONError) as erro:
            raise ErroParse(
                f"gzip/JSON inválido em {self.entrada.path}: {erro}"
            ) from erro

        if not raiz_iniciada or not raiz_encerrada:
            raise ErroParse(f"{self.entrada.path}: payload não é um objeto JSON")
        return valores, presentes, arrays

    def _validar_envelope(self) -> None:
        obrigatorios = {
            "payload_id",
            "version",
            "type",
            "count_updated",
            "count_deleted",
            "updates",
            "deletes",
        }
        ausentes = obrigatorios - self._presentes
        if ausentes:
            raise ErroParse(
                f"{self.entrada.path}: envelope sem {', '.join(sorted(ausentes))}"
            )
        if self._arrays != {"updates", "deletes"}:
            faltantes = {"updates", "deletes"} - self._arrays
            raise ErroParse(
                f"{self.entrada.path}: {', '.join(sorted(faltantes))} deve ser lista"
            )

        payload_id = self._valores.get("payload_id")
        if not isinstance(payload_id, str) or not payload_id.strip():
            raise ErroParse(f"{self.entrada.path}: payload_id deve ser string não vazia")

        declarado = self._valores.get("type")
        if declarado != self.tipo.nome:
            raise ErroIntegridade(
                f"{self.entrada.path}: type={declarado!r}, esperado {self.tipo.nome!r}"
            )

        for campo in ("version", "count_updated", "count_deleted"):
            valor = self._valores.get(campo)
            if type(valor) is not int:  # bool não é inteiro válido no envelope
                raise ErroParse(f"{self.entrada.path}: {campo} deve ser inteiro")
            if campo != "version" and valor < 0:
                raise ErroParse(f"{self.entrada.path}: {campo} não pode ser negativo")

        if type(self.entrada.version) is not int:
            raise ErroIntegridade(
                f"{self.entrada.path}: version do manifest deve ser inteiro"
            )
        if self._valores["version"] != self.entrada.version:
            raise ErroIntegridade(
                f"{self.entrada.path}: version={self._valores['version']} diverge do "
                f"manifest={self.entrada.version}"
            )
        for campo, valor in (
            ("num_updates", self.entrada.num_updates),
            ("num_deletes", self.entrada.num_deletes),
        ):
            if type(valor) is not int or valor < 0:
                raise ErroIntegridade(
                    f"{self.entrada.path}: {campo} do manifest deve ser inteiro não negativo"
                )

        self._validar_timestamps()

    def _validar_timestamps(self) -> None:
        tem_first = "first_ts" in self._presentes
        tem_last = "last_ts" in self._presentes
        vazio = self._valores["count_updated"] + self._valores["count_deleted"] == 0
        manifest_first = _datetime_para_epoch_ms(
            self.entrada.first_record_timestamp,
            "first_record_timestamp",
            self.entrada.path,
        )
        manifest_last = _datetime_para_epoch_ms(
            self.entrada.last_record_timestamp,
            "last_record_timestamp",
            self.entrada.path,
        )

        if tem_first != tem_last:
            raise ErroIntegridade(
                f"{self.entrada.path}: first_ts e last_ts devem formar um par"
            )
        if (manifest_first is None) != (manifest_last is None):
            raise ErroIntegridade(
                f"{self.entrada.path}: timestamps do manifest devem formar um par"
            )
        if not tem_first:
            if not vazio or manifest_first is not None:
                raise ErroIntegridade(
                    f"{self.entrada.path}: first_ts/last_ts ausentes fora de payload vazio "
                    "com manifest também vazio"
                )
            return

        payload_first = _epoch_ms(
            self._valores.get("first_ts"), "first_ts", self.entrada.path
        )
        payload_last = _epoch_ms(
            self._valores.get("last_ts"), "last_ts", self.entrada.path
        )
        if payload_first > payload_last:
            raise ErroIntegridade(f"{self.entrada.path}: timestamps do payload invertidos")
        if manifest_first is None or manifest_last is None:
            raise ErroIntegridade(
                f"{self.entrada.path}: timestamps presentes no payload e ausentes no manifest"
            )
        if manifest_first > manifest_last:
            raise ErroIntegridade(f"{self.entrada.path}: timestamps do manifest invertidos")
        if (payload_first, payload_last) != (manifest_first, manifest_last):
            raise ErroIntegridade(
                f"{self.entrada.path}: timestamps do payload divergem do manifest"
            )

    def _itens(self, prefixo: str) -> Iterator[Mapping[str, Any]]:
        try:
            with gzip.open(self.path, "rb") as arquivo:
                for posicao, item in enumerate(
                    ijson.items(arquivo, prefixo, use_float=True)
                ):
                    if not isinstance(item, dict):
                        array = prefixo.split(".", 1)[0]
                        raise ErroParse(
                            f"{self.entrada.path}: {array}[{posicao}] não é um objeto"
                        )
                    yield item
        except (OSError, EOFError, UnicodeDecodeError, ijson.JSONError) as erro:
            raise ErroParse(
                f"gzip/JSON inválido em {self.entrada.path}: {erro}"
            ) from erro

    def iter_findings(self) -> Iterator[LinhaFinding]:
        if self.tipo.produto is None:
            return
        self.num_updates_lidos = 0
        self.num_deletes_lidos = 0
        self.findings_mapeados = 0
        achatar = _achatar_vm if self.tipo.produto == PRODUTO_VM else _achatar_was
        fallback = self.entrada.relogio_fallback
        for seq, registro in enumerate(self._itens("updates.item")):
            self.num_updates_lidos += 1
            linha = achatar(seq, dict(registro), fallback)
            if linha.indexed is None:
                raise ErroParse(
                    f"{self.entrada.path}: finding {linha.finding_id} sem relógio de versão"
                )
            self.findings_mapeados += 1
            yield linha
        for posicao, registro in enumerate(self._itens("deletes.item")):
            self.num_deletes_lidos += 1
            linha = _achatar_delete(
                self.num_updates_lidos + posicao,
                dict(registro),
                self.tipo,
                fallback,
            )
            self.findings_mapeados += 1
            yield linha
        self._contagem_percorrida = True

    def iter_plugins(self) -> Iterator[LinhaPlugin]:
        if self.tipo.produto is None:
            return
        self.plugins_mapeados = 0
        achatar = _achatar_vm if self.tipo.produto == PRODUTO_VM else _achatar_was
        fallback = self.entrada.relogio_fallback
        for seq, registro in enumerate(self._itens("updates.item")):
            linha_finding = achatar(seq, dict(registro), fallback)
            if linha_finding.indexed is None:
                raise ErroParse(
                    f"{self.entrada.path}: finding {linha_finding.finding_id} sem relógio de versão"
                )
            plugin = _achatar_plugin(seq, registro.get("plugin"), linha_finding.indexed)
            if plugin is not None:
                self.plugins_mapeados += 1
                yield plugin

    def iter_recasts(self) -> Iterator[LinhaRecast]:
        if self.tipo.produto is not None:
            return
        self.num_updates_lidos = 0
        self.num_deletes_lidos = 0
        self.recasts_mapeados = 0
        fallback = self.entrada.relogio_fallback
        for seq, registro in enumerate(self._itens("updates.item")):
            self.num_updates_lidos += 1
            linha = achatar_registro_enriched(
                self.tipo,
                registro,
                is_delete=False,
                seq=seq,
                fallback=fallback,
            )
            self.recasts_mapeados += 1
            yield linha
        for posicao, registro in enumerate(self._itens("deletes.item")):
            self.num_deletes_lidos += 1
            linha = achatar_registro_enriched(
                self.tipo,
                registro,
                is_delete=True,
                seq=self.num_updates_lidos + posicao,
                fallback=fallback,
            )
            self.recasts_mapeados += 1
            yield linha
        self._contagem_percorrida = True

    def validar_contagens(self) -> None:
        if not self._contagem_percorrida:
            raise RuntimeError(
                f"{self.entrada.path}: contagens validadas antes de percorrer o payload"
            )
        erros: list[str] = []
        for nome, envelope, manifest, lido in (
            (
                "update",
                self.count_updated,
                self.entrada.num_updates,
                self.num_updates_lidos,
            ),
            (
                "delete",
                self.count_deleted,
                self.entrada.num_deletes,
                self.num_deletes_lidos,
            ),
        ):
            if envelope != manifest or envelope != lido:
                erros.append(
                    f"envelope diz {envelope} {nome}(s), manifest diz {manifest}, "
                    f"payload trouxe {lido}"
                )
        if erros:
            raise ErroIntegridade(f"{self.entrada.path}: {'; '.join(erros)}")

    @property
    def registros_lidos(self) -> int:
        return self.num_updates_lidos + self.num_deletes_lidos


def _epoch_ms(valor: Any, campo: str, path: str) -> int:
    if type(valor) is int:
        convertido = valor
    elif isinstance(valor, str) and _DECIMAL_INTEIRO.fullmatch(valor):
        convertido = int(valor)
    else:
        raise ErroIntegridade(
            f"{path}: {campo} deve ser inteiro ou string decimal"
        )
    if convertido < 0:
        raise ErroIntegridade(f"{path}: {campo} negativo")
    return convertido


def _datetime_para_epoch_ms(
    valor: dt.datetime | None, campo: str, path: str
) -> int | None:
    if valor is None:
        return None
    if not isinstance(valor, dt.datetime):
        raise ErroIntegridade(f"{path}: {campo} do manifest inválido")
    if valor.tzinfo is None:
        raise ErroIntegridade(f"{path}: {campo} do manifest sem timezone")
    utc = valor.astimezone(dt.timezone.utc)
    delta = utc - _EPOCH
    micros = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    if micros < 0:
        raise ErroIntegridade(f"{path}: {campo} do manifest negativo")
    if micros % 1000:
        raise ErroIntegridade(
            f"{path}: {campo} do manifest não está em milissegundos"
        )
    return micros // 1000
