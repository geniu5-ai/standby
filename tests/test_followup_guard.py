"""Safety boundaries for durable automated follow-ups."""

from __future__ import annotations

from tests.followup_chain import drive_follow_up

from types import SimpleNamespace
from typing import Any

import pytest

from tests.media_stub import passthrough_media


class _Result:
    def __init__(
        self,
        *,
        scalar: Any = None,
        mapping: dict[str, Any] | None = None,
        scalars: list[Any] | None = None,
    ):
        self._scalar = scalar
        self._mapping = mapping
        self._scalars = scalars or []

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return list(self._scalars)

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def mappings(self) -> "_Result":
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self._mapping


class _FollowupSession:
    """Small SQL-aware fake proving the policy, not SQLAlchemy internals."""

    def __init__(
        self,
        *,
        client: dict[str, Any] | None,
        has_newer_inbound: bool = False,
        mark_loss: bool = True,
        lifecycle_state: str | None = None,
        has_team_message: bool = False,
        released: dict[str, Any] | None = None,
        last_inbound_text: str | None = None,
        sent_24h: int = 0,
        sent_7d: int = 0,
    ) -> None:
        self.client = client
        self.last_inbound_text = last_inbound_text
        self.sent_24h = sent_24h
        self.sent_7d = sent_7d
        self.released = released
        self.has_newer_inbound = has_newer_inbound
        self.has_team_message = has_team_message
        self.mark_loss = mark_loss
        self.lifecycle_state = lifecycle_state
        self.calls: list[str] = []

    def execute(self, statement: Any, _params: dict[str, Any]) -> _Result:
        sql = str(statement)
        self.calls.append(sql)
        if "followup.lifecycle_state" in sql:
            return _Result(scalar=self.lifecycle_state)
        if "followup.active_event_after" in sql:
            return _Result(scalar=1 if self.has_newer_inbound else None)
        if "followup.team_message_after_origin" in sql:
            return _Result(scalar=1 if self.has_team_message else None)
        if "followup.client_status" in sql:
            return _Result(mapping=self.client)
        if "followup.last_inbound_text" in sql:
            textos = self.last_inbound_text
            if textos is None:
                textos = []
            elif isinstance(textos, str):
                textos = [textos]
            return _Result(scalars=textos)
        if "followup.confirmed_last_24h" in sql:
            return _Result(scalar=self.sent_24h)
        if "followup.confirmed_last_7d" in sql:
            return _Result(scalar=self.sent_7d)
        if "crew_in.release_one" in sql:
            return _Result(mapping=self.released)
        if "followup.v2_mark_loss_rescue" in sql:
            # V2 fundiu UPDATE do cliente e INSERT do candidate num unico CTE:
            # ou o cliente elegivel foi marcado e o candidate entrou (phone
            # devolvido), ou nada aconteceu (None).
            return _Result(scalar="5511999999999" if self.mark_loss else None)
        raise AssertionError(f"unexpected SQL: {sql}")


@pytest.mark.parametrize(
    ("client", "newer_inbound"),
    [
        (None, False),
        ({"status": "win", "last_intent": "reply"}, False),
        ({"status": "loss", "last_intent": "reply"}, False),
        ({"status": "block", "last_intent": "reply"}, False),
        ({"status": "open", "last_intent": "handoff"}, False),
        ({"status": "open", "last_intent": "reply"}, True),
    ],
)
def test_continuation_is_fail_closed_for_non_active_or_attended_client(
    client: dict[str, Any] | None, newer_inbound: bool
) -> None:
    from app.durable import followup

    session = _FollowupSession(client=client, has_newer_inbound=newer_inbound)

    assert (
        followup._should_continue(session, "tenant-a", "5511999999999", 7, "turn-1")
        is False
    )


def test_continuation_allows_only_open_non_handoff_silent_client() -> None:
    from app.durable import followup

    session = _FollowupSession(
        client={"status": "open", "last_intent": "reply"},
    )

    assert (
        followup._should_continue(session, "tenant-a", "5511999999999", 7, "turn-1")
        is True
    )


def test_continuation_stops_when_a_team_human_wrote_after_the_origin_reply() -> None:
    """08/out/2026: a marcacao automatica expira em 4min30; quem decide e o disparo."""
    from app.durable import followup

    session = _FollowupSession(
        client={"status": "open", "last_intent": "reply", "automation_state": "open"},
        has_team_message=True,
    )

    assert (
        followup._should_continue(session, "tenant-a", "5511999999999", 7, "turn-1")
        is False
    )


@pytest.mark.parametrize(
    "client",
    [
        {"status": "open", "last_intent": "handoff", "automation_state": "crew"},
        {"status": "open", "last_intent": "reply", "automation_state": "crew"},
    ],
)
def test_continuation_stops_while_the_human_owner_marking_stands(client: dict[str, Any]) -> None:
    from app.durable import followup

    session = _FollowupSession(client=client)

    assert (
        followup._should_continue(session, "tenant-a", "5511999999999", 7, "turn-1")
        is False
    )


@pytest.mark.parametrize("apply", [False, True])
def test_continuation_treats_an_expired_marking_as_released(apply: bool) -> None:
    """Sweep atrasado (restart): a marcacao vencida vale como devolvida no disparo."""
    from app.durable import followup

    session = _FollowupSession(
        client={"status": "open", "last_intent": "handoff", "automation_state": "crew"},
        released={"status": "open", "last_intent": "reply", "automation_state": "open"},
    )

    assert (
        followup._should_continue(
            session, "tenant-a", "5511999999999", 7, "turn-1",
            release_window_seconds=270.0, apply_crew_release=apply,
        )
        is True
    )
    marker = "crew_in.release_one */" if apply else "crew_in.release_one_preview"
    assert any(marker in sql for sql in session.calls)


def test_continuation_stops_when_the_marking_has_not_expired() -> None:
    from app.durable import followup

    session = _FollowupSession(
        client={"status": "open", "last_intent": "handoff", "automation_state": "crew"},
        released=None,
    )

    assert (
        followup._should_continue(
            session, "tenant-a", "5511999999999", 7, "turn-1",
            release_window_seconds=270.0, apply_crew_release=True,
        )
        is False
    )


def test_continuation_after_a_released_win_marking_still_stops() -> None:
    """win/crew vencida volta como win/open: cliente win nunca ganha follow-up."""
    from app.durable import followup

    session = _FollowupSession(
        client={"status": "win", "last_intent": "handoff", "automation_state": "crew"},
        released={"status": "win", "last_intent": "reply", "automation_state": "open"},
    )

    assert (
        followup._should_continue(
            session, "tenant-a", "5511999999999", 7, "turn-1",
            release_window_seconds=270.0, apply_crew_release=True,
        )
        is False
    )


def test_continuation_resumes_once_the_marking_was_released() -> None:
    """Sweep devolveu: open/open, last_intent 'reply', ninguem da equipe escreveu."""
    from app.durable import followup

    session = _FollowupSession(
        client={"status": "open", "last_intent": "reply", "automation_state": "open"},
    )

    assert (
        followup._should_continue(session, "tenant-a", "5511999999999", 7, "turn-1")
        is True
    )
    assert any("followup.team_message_after_origin" in sql for sql in session.calls)


@pytest.mark.parametrize("lifecycle_state", ["stopped", "review_required", "rescue_queued"])
def test_terminal_lifecycle_stops_follow_up_even_with_a_healthy_client(
    lifecycle_state: str,
) -> None:
    """O lifecycle V2 cancelado manda, mesmo com clients em open/open."""
    from app.durable import followup

    session = _FollowupSession(
        client={"status": "open", "last_intent": "reply", "automation_state": "open"},
        lifecycle_state=lifecycle_state,
    )

    assert (
        followup._should_continue(session, "tenant-a", "5511999999999", 7, "turn-1")
        is False
    )
    # Fail-fast: nem chega a consultar events/clients.
    assert all("followup.lifecycle_state" in sql for sql in session.calls)


def test_scheduled_lifecycle_keeps_following_up() -> None:
    from app.durable import followup

    session = _FollowupSession(
        client={"status": "open", "last_intent": "reply", "automation_state": "open"},
        lifecycle_state="scheduled",
    )

    assert (
        followup._should_continue(session, "tenant-a", "5511999999999", 7, "turn-1")
        is True
    )


def test_missing_lifecycle_row_keeps_v1_behaviour_untouched() -> None:
    from app.durable import followup

    session = _FollowupSession(
        client={"status": "open", "last_intent": "reply", "automation_state": "open"},
        lifecycle_state=None,
    )

    assert (
        followup._should_continue(session, "tenant-a", "5511999999999", 7, "turn-1")
        is True
    )


def test_mark_loss_only_enqueues_rescue_candidate_when_active_client_was_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.durable import followup

    session = _FollowupSession(
        client={"status": "open", "last_intent": "reply"}, mark_loss=False
    )
    monkeypatch.setattr(followup, "sql_session", lambda: session)

    queued = followup.mark_loss_and_queue_for_rescue_v2_txn(
        "tenant-a", "turn-1"
    )

    assert queued is False


def test_mark_loss_enqueues_rescue_candidate_after_active_client_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.durable import followup

    session = _FollowupSession(
        client={"status": "open", "last_intent": "reply"}, mark_loss=True
    )
    monkeypatch.setattr(followup, "sql_session", lambda: session)

    queued = followup.mark_loss_and_queue_for_rescue_v2_txn(
        "tenant-a", "turn-1"
    )

    assert queued is True
    # O candidate so nasce alimentado pelo UPDATE do cliente elegivel: a V2
    # fundiu os dois comandos da V1 (mark_loss + insert_candidate) num CTE
    # atomico onde o INSERT seleciona FROM queued <- marked <- eligible.
    sql = str(followup._MARK_LOSS_AND_RESCUE_V2_SQL)
    assert "INSERT INTO genius_next.follow_up_candidates" in sql
    assert "FROM queued" in sql


def test_queued_sender_rechecks_before_drafting_or_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.durable import followup

    drafted: list[int] = []
    receipts: list[Any] = []
    monkeypatch.setattr(followup, "should_continue_follow_up_txn", lambda *_: False)
    monkeypatch.setattr(
        followup, "record_follow_up_v2_receipt_txn", lambda *_args: receipts.append(_args[-1])
    )
    monkeypatch.setattr(
        followup,
        "draft_follow_up_step",
        lambda *_: drafted.append(1)
        or (_ for _ in ()).throw(AssertionError("must not draft")),
    )

    receipt = followup.send_follow_up_v2_turn_workflow(
        "tenant-a", "5511999999999@s.whatsapp.net", "5511999999999", "genius", 1, "turn-1"
    )

    assert drafted == []
    assert receipt.status == "stopped"
    assert receipts == [receipt]


def test_scheduler_rechecks_after_wake_before_enqueue_or_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.durable import followup

    waits: list[int] = []
    marked: list[int] = []

    monkeypatch.setattr(
        followup,
        "load_follow_up_policy_step",
        lambda *_: followup.FollowUpPolicy.enabled_default("tenant-a"),
    )
    monkeypatch.setattr(followup, "ensure_follow_up_v2_lifecycle_txn", lambda *_: True)
    monkeypatch.setattr(followup, "record_follow_up_v2_receipt_txn", lambda *_: None)
    monkeypatch.setattr(followup, "should_continue_follow_up", lambda *_: False)
    monkeypatch.setattr(
        followup, "mark_loss_and_queue_for_rescue_v2_txn", lambda *_: marked.append(1)
    )

    drive_follow_up(
        monkeypatch, followup,
        "tenant-a", "5511999999999@s.whatsapp.net", "5511999999999", "genius", "turn-1",
        on_sleep=waits.append,
    )

    assert waits == [followup.FOLLOWUP_V2_WAITS_SECONDS[0]]
    assert marked == []


class _NoopCtx:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> None:
        return None


@pytest.mark.parametrize(
    ("returned_status", "decision_status", "needs_human"),
    [
        ("new", "new", False),
        ("loss", "loss", False),
        ("new", "win", False),
        ("new", "off", False),
        ("new", "new", True),
    ],
)
def test_send_text_turn_starts_status_audit_after_delivery(
    monkeypatch: pytest.MonkeyPatch,
    returned_status: str,
    decision_status: str,
    needs_human: bool,
) -> None:
    from app.contracts import EvolutionWebhook, ReplyDraft, TurnRelease
    from app.security.guard import GuardAssessment
    from app.durable import flow
    from app.identity import build_trusted_ingress

    webhook = EvolutionWebhook.model_validate(
        {
            "event": "messages.upsert",
            "instance": "genius",
            "data": {
                "key": {
                    "id": "followup-guard",
                    "remoteJid": "5511999999999@s.whatsapp.net",
                    "fromMe": False,
                },
                "message": {"conversation": "oi"},
                "messageType": "conversation",
            },
        }
    )
    event = build_trusted_ingress(webhook, tenant_id="tenant-a")
    turn = SimpleNamespace(
        turn_id="turn-1",
        tenant_id="tenant-a",
        remote_jid="5511999999999@s.whatsapp.net",
        instance="genius",
    )
    started: list[tuple[Any, ...]] = []
    decision = SimpleNamespace(
        customer_status=decision_status,
        needs_human=needs_human,
    )

    monkeypatch.setattr(
        flow, "claim_turn_txn", lambda *_: SimpleNamespace(turn=turn, has_more=False)
    )
    monkeypatch.setattr(flow, "automation_allowed_for_turn_txn", lambda *_: True)
    monkeypatch.setattr(flow, "enrich_turn_media", passthrough_media)
    monkeypatch.setattr(flow, "fetch_pg_history_step", lambda *_: [])
    monkeypatch.setattr(
        flow,
        "guard_assess_step",
        lambda stage, *_args, **_kwargs: GuardAssessment(
            stage=stage,
            disposition="allow",
            findings=[],
            event_refs=(),
            deterministic=False,
        ),
    )
    monkeypatch.setattr(
        flow,
        "draft_reply_step",
        lambda *_args, **_kwargs: (
            ReplyDraft(action="send_text", text="oi"),
            returned_status,
        ),
    )
    monkeypatch.setattr(flow, "delivery_mode_step", lambda: "record_only")
    monkeypatch.setattr(
        flow, "build_decision_and_commands", lambda *_args, **_kwargs: (decision, [])
    )
    monkeypatch.setattr(flow, "record_decision_batch_txn", lambda *_: [])
    monkeypatch.setattr(flow, "dispatch_outbox_batch_workflow", lambda *_: None)
    monkeypatch.setattr(
        flow,
        "_start_status",
        lambda *args, **kwargs: started.append((args, kwargs)),
    )
    # Scheduling the follow-up at dispatch must no-op in a queue-less test.

    flow.conversation_turn_workflow(TurnRelease(event=event, through_sequence=1))

    assert len(started) == 1



_OPEN_CLIENT = {"status": "open", "last_intent": "reply", "automation_state": "open"}


def test_continuation_stops_after_patient_deferral() -> None:
    """15/out/2026: "sem pressa / quando o dinheiro entrar" encerra o ciclo."""
    from app.durable import followup

    session = _FollowupSession(
        client=_OPEN_CLIENT,
        lifecycle_state="scheduled",
        last_inbound_text="Tá bem, quando o dinheiro entrar eu vou",
    )
    assert followup._should_continue(session, "tenant-a", "5511999999999", 7, "turn-1") is False


def test_continuation_stops_at_daily_cap_per_contact() -> None:
    """15/out/2026: teto de follow-ups confirmados por contato em 24 h."""
    from app.durable import followup

    session = _FollowupSession(client=_OPEN_CLIENT, lifecycle_state="scheduled", sent_24h=3)
    assert followup._should_continue(session, "tenant-a", "5511999999999", 7, "turn-1") is False
    session = _FollowupSession(client=_OPEN_CLIENT, lifecycle_state="scheduled", sent_24h=2)
    assert followup._should_continue(session, "tenant-a", "5511999999999", 7, "turn-1") is True



def test_continuation_stops_when_deferral_is_followed_by_ok() -> None:
    """16/out: "Depois falo com você" e depois "Ok" continua sendo adiamento."""
    from app.durable import followup

    session = _FollowupSession(
        client=_OPEN_CLIENT,
        lifecycle_state="scheduled",
        last_inbound_text=["Ok", "Depois falo com você"],
    )
    assert followup._should_continue(session, "tenant-a", "5511999999999", 7, "turn-1") is False
