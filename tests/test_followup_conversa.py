"""Follow-up: "o paciente respondeu depois?" vale por conversa, não por telefone (SQL real, Postgres local).

Incidente de 11/nov/2026: o mesmo telefone tinha uma conversa antiga (sequência alta) e uma nova
(sequência 4). A checagem só por sequência fazia todo follow-up da conversa nova parar.
Sem Postgres local (ou com o esquema real já criado), o teste é pulado.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.durable import followup

TENANT = "tenant-teste"
PHONE = "5511900000000"
JID = f"{PHONE}@s.whatsapp.net"

_DDL = """
CREATE SCHEMA genius_next;
CREATE TABLE genius_next.turns (
    id text PRIMARY KEY, tenant_id text, provider text, instance text, remote_jid text,
    last_sequence int, created_at timestamptz
);
CREATE TABLE genius_next.events (
    id bigserial PRIMARY KEY, tenant_id text, phone text, provider text, instance text,
    remote_jid text, direction text, conversation_sequence int, received_at timestamptz
);
"""


@pytest.fixture
def sessao():
    url = os.environ.get("DATABASE_URL", "").replace("postgresql://", "postgresql+psycopg://", 1)
    try:
        engine = create_engine(url)
        conexao = engine.connect()
    except (OperationalError, Exception) as exc:  # sem banco local
        pytest.skip(f"sem Postgres local ({type(exc).__name__})")
    transacao = conexao.begin()
    if conexao.execute(text("SELECT to_regclass('genius_next.events')")).scalar() is not None:
        transacao.rollback()
        conexao.close()
        pytest.skip("esquema genius_next real presente: este teste só roda em banco vazio")
    for comando in filter(None, (c.strip() for c in _DDL.split(";"))):
        conexao.execute(text(comando))
    try:
        yield Session(bind=conexao)
    finally:
        transacao.rollback()  # DDL e dados somem: nada fica no banco
        conexao.close()


def _turno(s: Session, turn_id="turn:novo", instance="inst-nova", last_sequence=4, criado="2026-11-11 20:00:00+00"):
    s.execute(
        text("INSERT INTO genius_next.turns VALUES (:id, :t, 'evolution', :i, :j, :seq, :c)"),
        {"id": turn_id, "t": TENANT, "i": instance, "j": JID, "seq": last_sequence, "c": criado},
    )


def _evento(s: Session, *, instance, seq, quando, direction="inbound", provider="evolution"):
    s.execute(
        text(
            "INSERT INTO genius_next.events (tenant_id, phone, provider, instance, remote_jid, direction,"
            " conversation_sequence, received_at) VALUES (:t, :p, :pr, :i, :j, :d, :seq, :q)"
        ),
        {"t": TENANT, "p": PHONE, "pr": provider, "i": instance, "j": JID, "d": direction, "seq": seq, "q": quando},
    )


def _respondeu(s: Session, turn_id="turn:novo", origin_sequence=4) -> bool:
    linha = s.execute(
        followup._INBOUND_AFTER_ORIGIN_SQL,
        {"tenant_id": TENANT, "phone": PHONE, "origin_sequence": origin_sequence, "origin_turn_id": turn_id},
    ).scalar_one_or_none()
    return linha is not None


def test_conversa_antiga_com_sequencia_alta_nao_para_o_ciclo(sessao):
    """O incidente: 28 mensagens antigas por outra instância, todas antes do turno de origem."""
    _turno(sessao)
    for seq in range(60, 88):
        _evento(sessao, instance="inst-antiga", seq=seq, quando="2026-11-05 12:00:00+00", provider="zernio")
    for seq in range(1, 5):
        _evento(sessao, instance="inst-nova", seq=seq, quando="2026-11-11 19:59:00+00")
    assert _respondeu(sessao) is False


def test_resposta_na_mesma_conversa_para_o_ciclo(sessao):
    _turno(sessao)
    _evento(sessao, instance="inst-nova", seq=5, quando="2026-11-11 20:03:00+00")
    assert _respondeu(sessao) is True


def test_mensagem_por_outra_conversa_depois_da_origem_para_o_ciclo(sessao):
    _turno(sessao)
    _evento(sessao, instance="inst-antiga", seq=2, quando="2026-11-11 20:10:00+00", provider="zernio")
    assert _respondeu(sessao) is True


def test_mensagem_da_equipe_ou_saida_nao_conta_como_resposta(sessao):
    _turno(sessao)
    _evento(sessao, instance="inst-nova", seq=5, quando="2026-11-11 20:03:00+00", direction="outbound")
    assert _respondeu(sessao) is False


def test_sem_turno_de_origem_vale_a_regra_antiga(sessao):
    """Sem a linha do turno, erra só para o lado de não enviar."""
    _evento(sessao, instance="inst-antiga", seq=60, quando="2026-11-05 12:00:00+00", provider="zernio")
    assert _respondeu(sessao, turn_id="turn:inexistente") is True
