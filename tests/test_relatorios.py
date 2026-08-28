"""
Smoke tests dos relatórios legados contra os JSON de `samples/` + um S3 falso.

    pytest tests/                     (se tiver pytest)
    python tests/test_relatorios.py   (runner embutido)
"""

import copy
import csv
import json
import os
import tempfile

from conftest import FakeS3, payload  # noqa: F401  (dispara os stubs)

from legacy.tenable_core import executar_relatorio
from legacy.reports import (
    RELATORIOS,
    INTRANET,
    GESTAO_VULN,
    VM_FINDINGS,
    WAS_FINDINGS,
)

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(RAIZ, "samples")


def _sample(nome):
    with open(os.path.join(SAMPLES, nome), encoding="utf-8") as f:
        return json.load(f)


def _config():
    return {
        "bucket": "b",
        "prefix_vm": "finding/",
        "prefix_was": "was_finding/",
        "prefix_enriched": "finding_enriched_attributes/",
        "s3_max_workers": 4,
        "s3_retry_max_attempts": 2,
        "s3_retry_base_delay_seconds": 0.01,
        "s3_retry_max_delay_seconds": 0.02,
        "progress_every": 1000,
    }


def _colunas_esperadas(rel):
    base = ["record_source"] if rel.record_source else []
    return base + list(rel.colunas)


def _rodar(rel, store, **envs):
    tmp = tempfile.mkdtemp()
    envs = dict(envs)
    envs.setdefault(f"{rel.nome.upper()}_LAST_FOUND_DAYS", "0")  # filtro off = determinístico
    envs[f"{rel.nome.upper()}_OUTPUT"] = os.path.join(tmp, f"{rel.nome}.csv")
    antigos = {k: os.environ.get(k) for k in envs}
    os.environ.update({k: str(v) for k, v in envs.items()})
    try:
        stats = executar_relatorio(rel, _config(), FakeS3(store), {})
    finally:
        for k, v in antigos.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    with open(stats["saida"], encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        linhas = list(reader)
        cabecalho = reader.fieldnames
    return stats, cabecalho, linhas


def _store_vm(updates):
    return {"finding/a.json": payload("FINDING", updates)}


def _store_was(updates):
    return {"was_finding/c.json": payload("WAS_FINDING", updates)}


# --------------------------------------------------------------------------
def test_cabecalhos_e_uma_linha_por_relatorio():
    vm = _sample("exemplo_vm_finding.json")["updates"]
    was = _sample("exemplo_was_finding.json")["updates"]
    store = {**_store_vm(vm), **_store_was(was)}
    for nome, rel in RELATORIOS.items():
        _, cabecalho, linhas = _rodar(rel, store)
        assert cabecalho == _colunas_esperadas(rel), f"{nome}: cabeçalho divergente"
        esperado = len(rel.fontes)  # 1 update por fonte nos samples
        assert len(linhas) == esperado, f"{nome}: {len(linhas)} linhas (esperava {esperado})"


def test_vm_findings_valores():
    vm = _sample("exemplo_vm_finding.json")["updates"]
    _, _, linhas = _rodar(VM_FINDINGS, _store_vm(vm))
    r = linhas[0]
    assert r["record_source"] == "VM"
    assert r["first_found"] == "06/03/2024"          # dd/mm/aaaa
    assert r["last_found"] == "27/08/2026"
    assert r["state"] == "OPEN"
    assert r["plugin.id"] == "14272"


def test_was_findings_valores():
    was = _sample("exemplo_was_finding.json")["updates"]
    _, _, linhas = _rodar(WAS_FINDINGS, _store_was(was))
    r = linhas[0]
    assert r["record_source"] == "WAS"
    assert r["url"].startswith("https://")
    assert r["indexed_at"] == "27/08/2026"
    assert r["asset.ipv4s"] == ""                    # lista vazia


def test_gestao_junta_vm_e_was():
    vm = _sample("exemplo_vm_finding.json")["updates"]
    was = _sample("exemplo_was_finding.json")["updates"]
    _, cabecalho, linhas = _rodar(GESTAO_VULN, {**_store_vm(vm), **_store_was(was)})
    assert cabecalho[0] == "record_source"
    origens = sorted(r["record_source"] for r in linhas)
    assert origens == ["VM", "WAS"]
    vm_row = next(r for r in linhas if r["record_source"] == "VM")
    assert vm_row["Nome Vulnerabilidade"] == "Netstat Portscanner (SSH)"
    assert vm_row["Causa Raiz"] == "Port scanners"   # plugin.family
    assert vm_row["Aging"].isdigit()


def test_intranet_merge_enriched():
    vm = copy.deepcopy(_sample("exemplo_vm_finding.json")["updates"])
    fid = vm[0]["finding_id"]
    enr = [{
        "recast_properties": {
            "finding_id": fid,
            "source": "recast-platform",
            "recast_annotation": {
                "rule_id": "regra-1",
                "rule_comment": "aceito",
                "created_at": "2026-08-27T11:00:00Z",
                "updated_at": "2026-08-27T11:00:00Z",
                "modification": "ACCEPTED",
                "modification_target": "RISK",
                "recasted_severity": "NONE",
            },
        }
    }]
    store = {
        **_store_vm(vm),
        "finding_enriched_attributes/e.json": payload("FINDING_ENRICHED_ATTRIBUTES", enr),
    }
    _, _, linhas = _rodar(INTRANET, store, INTRANET_MERGE_ENRICHED="true")
    r = linhas[0]
    assert r["recast_modification"] == "ACCEPTED"
    assert r["enriched.recast_properties.recast_annotation.recasted_severity"] == "NONE"
    assert r["recast_properties"]  # objeto JSON não vazio


def test_dedupe_mantem_o_mais_recente():
    base = _sample("exemplo_vm_finding.json")["updates"][0]
    antigo = copy.deepcopy(base)
    antigo["last_found"] = "2026-08-01T00:00:00.000Z"
    antigo["state"] = "OPEN"
    novo = copy.deepcopy(base)
    novo["last_found"] = "2026-08-25T00:00:00.000Z"
    novo["state"] = "FIXED"
    store = {
        "finding/a.json": payload("FINDING", [antigo]),
        "finding/b.json": payload("FINDING", [novo]),
    }
    stats, _, linhas = _rodar(INTRANET, store, INTRANET_MERGE_ENRICHED="false")
    assert len(linhas) == 1
    assert linhas[0]["last_found"] == "2026-08-25T00:00:00.000Z"
    assert linhas[0]["state"] == "FIXED"


def test_max_rows():
    updates = [
        {**_sample("exemplo_vm_finding.json")["updates"][0],
         "finding_id": f"fid-{i}", "last_found": "2026-08-27T00:00:00Z"}
        for i in range(20)
    ]
    _, _, linhas = _rodar(VM_FINDINGS, _store_vm(updates), VM_FINDINGS_MAX_ROWS="5")
    assert len(linhas) == 5


def test_volume_sintetico_nao_quebra():
    # 100 arquivos x 50 updates = 5000 linhas, exercita o caminho paralelo
    base = _sample("exemplo_vm_finding.json")["updates"][0]
    store = {}
    for a in range(100):
        ups = []
        for u in range(50):
            up = copy.deepcopy(base)
            up["finding_id"] = f"f-{a}-{u}"
            up["last_found"] = "2026-08-27T00:00:00Z"
            ups.append(up)
        store[f"finding/{a}.json"] = payload("FINDING", ups)
    stats, _, linhas = _rodar(VM_FINDINGS, store)
    assert len(linhas) == 5000
    assert stats["updates"] == 5000


def _main():
    testes = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    falhas = 0
    for t in testes:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            falhas += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            falhas += 1
            print(f"  ERRO {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(testes) - falhas}/{len(testes)} passaram")
    raise SystemExit(1 if falhas else 0)


if __name__ == "__main__":
    _main()
