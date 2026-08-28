from __future__ import annotations

import datetime as dt

import pytest

pytestmark = pytest.mark.banco


def test_evento_na_virada_utc_agrega_no_dia_e_mes_de_sao_paulo(conn):
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO finding_event "
            "(finding_id, product, event_type, occurred_at) "
            "VALUES ('tz-boundary', 'VM', 'OPENED', '2026-09-01 02:30:00+00')"
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT dia, product, event_type, total FROM vw_evento_diario "
            "WHERE product = 'VM' AND event_type = 'OPENED'"
        )
        assert cur.fetchone() == {
            "dia": dt.date(2026, 8, 31),
            "product": "VM",
            "event_type": "OPENED",
            "total": 1,
        }
        cur.execute(
            "SELECT mes, product, abertos, fechados, excluidos "
            "FROM vw_tendencia_mensal WHERE product = 'VM'"
        )
        assert cur.fetchone() == {
            "mes": dt.datetime(2026, 8, 1),
            "product": "VM",
            "abertos": 1,
            "fechados": 0,
            "excluidos": 0,
        }
