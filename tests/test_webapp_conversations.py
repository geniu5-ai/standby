"""Parametros novos de GET /v1/webapp/conversations (sem banco): limite e changed_since."""

from datetime import timezone

import pytest
from fastapi import HTTPException

from app.webapp.router import (
    CONVERSATIONS_DEFAULT_LIMIT,
    CONVERSATIONS_MAX_LIMIT,
    clamp_conversations_limit,
    parse_changed_since,
)


def test_limit_padrao_continua_100():
    assert clamp_conversations_limit(None) == CONVERSATIONS_DEFAULT_LIMIT == 100


def test_limit_tem_piso_e_teto():
    assert clamp_conversations_limit(0) == 1
    assert clamp_conversations_limit(-5) == 1
    assert clamp_conversations_limit(250) == 250
    assert clamp_conversations_limit(99_999) == CONVERSATIONS_MAX_LIMIT == 1000


def test_changed_since_vazio_e_nulo():
    assert parse_changed_since(None) is None
    assert parse_changed_since("") is None


def test_changed_since_aceita_z_e_offset_e_sem_fuso():
    z = parse_changed_since("2026-11-08T15:00:00Z")
    assert z is not None and z.utcoffset() == timezone.utc.utcoffset(None)
    assert parse_changed_since("2026-11-08T12:00:00-03:00").utcoffset().total_seconds() == -3 * 3600
    assert parse_changed_since("2026-11-08T15:00:00").tzinfo is not None


@pytest.mark.parametrize("bad", ["ontem", "2026-13-40", "1; DROP TABLE clients"])
def test_changed_since_invalido_vira_422(bad):
    with pytest.raises(HTTPException) as err:
        parse_changed_since(bad)
    assert err.value.status_code == 422
