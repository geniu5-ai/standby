"""Durable Evolution ingress, finite turns, decisions, and outbox dispatch."""

import logging
from typing import Any, Literal

from pydantic import ValidationError

from app.contracts import (
    MEDIA_KINDS,
    AgentDecision,
    OutboxCommand,
    ReplyDraft,
    TrustedIngress,
    TurnRelease,
    media_kind_of,
)
from app.durable.followup import follow_up_start_job
from app.durable.media import (
    AUDIO_FALLBACK_TEXT,  # noqa: F401 — reexportado (tests/test_turn_audio.py)
    MEDIA_HANDOFF_TEXT,
    enrich_turn_media,
    maybe_enqueue_media_on_ingest,
)
from app.durable.outbox import dispatch_outbox_batch_workflow
from app.durable.persistence import (
    CLAIM_ALL_SEQUENCE,
    ClaimedTurn,
    PersistEventResult,
    automation_allowed_for_turn_txn,
    client_has_summary_txn,
    claim_turn_txn,
    persist_event_txn,
    fail_turn_txn,
    record_decision_batch_txn,
    record_guard_decision_batch_txn,
    # Kept as a module-level compatibility hook for output-guard tests and
    # downstream operators; the current safe-fallback path records normally.
    record_output_guard_block_txn,  # noqa: F401
    record_security_event_txn,
    reopen_if_loss_txn,
    reopen_if_win_txn,
    stop_turn_as_trivial_txn,
    stop_turn_for_control_txn,
)
from app.durable.summary_backfill import backfill_one_client_job
from app.status.audit import has_recent_booking_cue
from app.status.conversation_audit import audit_turn
from app.status.workflow import (
    build_status_workflow_input,
    status_audit_job,
    status_workflow_id,
)
from app.durable.tenant_registry import manager_jid_txn
from app.genius_maps.lead_stage import advance_lead_stage_step
from app.agent.history_summary import summarize_history_step
from app.identity_primitives import stable_id
# Historico do turno normal vem do Postgres; a leitura da Evolution
# (app.ingress.evolution.fetch_history) fica reservada ao Genius Rescue.
from app.durable.history import fetch_pg_history_step
from app.durable.turn_fallback import acionar_fallback_do_turno
from app.media.transcription import transcribe_audio_step
from app.media.tts import tts_step
from app.ops_alert import ops_alert_step, ops_alert_text
from app.jobs import (
    TURN_PRIORITY_LIVE,
    TURN_QUEUE,
    defer_once,
    jobs,
)
from app.reply.llm import draft_reply_step
from app.durable.handoff_notice import (
    HandoffCause,
    build_human_notice,
    build_manager_notice,
    contact_name,
)
from app.reply.handoff_promotion import promote_forwarding_to_handoff
from app.reply.safety_routes import EMERGENCY_DRAFT, safety_handoff_draft
from app.reply.split import split_human_reply
from app.status.rules import is_farewell, is_refusal
from app.security.guard import GuardAssessment, GuardDisposition, GuardStage, assess_locally
from app.settings import get_settings

logger = logging.getLogger(__name__)

# Fallback humano portado do node ``Get Audio`` do legado n8n
# (AUDIO_FALLBACK_TEXT): mora em app/durable/media.py (rota de midia) e e
# reexportado por este modulo, de onde os testes o importam.

SAFE_HANDOFF_TEXT = (
    "Vou encaminhar sua conversa para a nossa equipe e ela continua com você por aqui."
)
GUARD_REVIEW_HANDOFF_TEXT = SAFE_HANDOFF_TEXT
OUTPUT_GUARD_FALLBACK_TEXT = "Vou confirmar isso com a equipe e te retorno por aqui."


def _defer_status(turn: ClaimedTurn, payload: Any) -> None:
    """Uma auditoria por turno: o mesmo turno nao entra duas vezes na fila."""
    defer_once(
        status_audit_job,
        queueing_lock=status_workflow_id(turn.tenant_id, turn.turn_id),
        payload=payload.model_dump(mode="json"),
    )


def _start_status(
    turn: ClaimedTurn,
    history: Any,
    draft: ReplyDraft,
    phone: str,
) -> None:
    """Start the post-delivery status workflow with a stable, durable workflow ID."""
    settings = get_settings()
    if not settings.STATUS_AUDIT_ENABLED:
        return
    payload = build_status_workflow_input(
        turn,
        history,
        draft,
        phone=phone,
        audit_model=settings.STATUS_AUDIT_MODEL or settings.OMNIROUTE_MODEL,
    )
    _defer_status(turn, payload)


def _start_status_sem_resposta(
    turn: ClaimedTurn,
    phone: str,
    *,
    requer_pista: bool = False,
) -> None:
    """Audita um turno que terminou SEM resposta do agente (13/out/2026).

    Turnos suprimidos por controle humano (``suppressed_by_control``) e turnos
    triviais nunca chegavam a ``_start_status`` -- e justamente ali que o
    paciente confirma e a equipe assume. O payload vai com ``agent_reply=""`` e
    ``draft_action="sem_resposta"``, com o MESMO workflow ID do caminho normal,
    entao o audit continua idempotente por turno.

    Best-effort por contrato: nunca levanta e nunca muda o retorno do turno --
    a mensagem ja foi tratada (ou suprimida) antes deste ponto, e falhar o
    turno aqui custaria o historico inteiro em troca de um audit.
    ``requer_pista`` e o gate do ramo trivial: sem pista de agendamento nas
    ultimas falas da equipe/IA, "kkkkkk" nao paga uma chamada de LLM.
    """
    try:
        settings = get_settings()
        if not settings.STATUS_AUDIT_ENABLED:
            return
        history = fetch_pg_history_step(turn)
        if requer_pista and not has_recent_booking_cue(
            [
                (message.role, message.text)
                for message in getattr(history, "messages", None) or []
            ]
        ):
            return
        payload = build_status_workflow_input(
            turn,
            history,
            None,
            phone=phone,
            audit_model=settings.STATUS_AUDIT_MODEL or settings.OMNIROUTE_MODEL,
        )
        _defer_status(turn, payload)
    except Exception:
        logger.warning(
            "status sem resposta nao iniciado para o turno %s",
            getattr(turn, "turn_id", "?"),
            exc_info=True,
        )


# Rota fixa de seguranca (emergencia, pergunta de medicamento/dose): a
# marcacao dela nunca expira e nunca agenda follow-up.  Sem a equipe, o LLM
# voltaria a responder (ou o follow-up, redigido por LLM sobre o historico)
# sem a rota fixa nem o guarda de saida -- exatamente o que a rota existe
# para evitar nesse tipo de conversa.
_SAFETY_HANDOFF_CAUSES: frozenset[str] = frozenset({"emergencia", "medicamento"})


def handoff_auto_release_tenants() -> frozenset[str]:
    """Tenants da lei de 08/out/2026 (HANDOFF_AUTO_RELEASE_TENANTS, virgula)."""
    raw = getattr(get_settings(), "HANDOFF_AUTO_RELEASE_TENANTS", "") or ""
    return frozenset(tenant.strip() for tenant in raw.split(",") if tenant.strip())


def handoff_marking_expires(tenant_id: str, handoff_cause: HandoffCause | None) -> bool:
    """O handoff automatico deste turno cria marcacao com prazo (e follow-up)?

    Sim so para tenant em HANDOFF_AUTO_RELEASE_TENANTS e causa fora da rota
    fixa de seguranca.  Nos outros casos a marcacao fica sem prazo
    (crew_in_at NULL) e nao ha follow-up, como antes de 08/out/2026.
    """
    if handoff_cause in _SAFETY_HANDOFF_CAUSES:
        return False
    return tenant_id in handoff_auto_release_tenants()


def _start_follow_up_v2(
    turn: ClaimedTurn,
    phone: str,
    *,
    customer_status: str,
    needs_human: bool,
    handoff_cause: HandoffCause | None = None,
) -> None:
    """Start the tenant-policy V2 follow-up at dispatch, not via the audit.

    Scheduling is intentionally independent of the Status Audit: the audit runs
    after dispatch and may still interrupt the follow-up through
    should_continue, but its customer_status/evidence_source never gate whether
    a follow-up starts.

    08/out/2026 (ordem do chairman): a resposta que marca a conversa para a
    equipe automaticamente (handoff do modelo, promocao, guarda de saida sobre
    um handoff) tambem agenda o follow-up quando a marcacao tem prazo
    (``handoff_marking_expires``).  A marcacao volta para open em 4min30 sem
    ato da equipe e com o paciente calado, e a 1a tentativa (5 min) decide na
    hora do disparo (``_should_continue``): dispara se a conversa estiver open
    (ou a marcacao vencida, que ela mesma devolve), o paciente nao respondeu e
    ninguem da equipe escreveu; senao, stopped.  Cliente win/loss/block nunca
    ganha follow-up, nem a marcacao sem prazo (rota fixa de seguranca, tenant
    fora da lista).
    """
    if customer_status != "open":
        return
    if needs_human and not handoff_marking_expires(turn.tenant_id, handoff_cause):
        return
    # 15/out/2026: se a ultima fala do paciente neste turno foi adiamento ou
    # encerramento ("sem pressa", "quando o dinheiro entrar"), a resposta da
    # Julia fecha o assunto; abrir ciclo novo de follow-up vira cobranca.
    falas = [
        (getattr(e, "text_content", "") or "")
        for e in (getattr(turn, "events", None) or [])
        if getattr(e, "direction", None) == "inbound"
    ]
    # 03/nov/2026: objecao de distancia/custo/tempo ("Muito longe") conta como
    # adiamento (is_deferral_or_closing) e despedida ("ok obrigada") na ULTIMA
    # fala tambem fecha o assunto.  A guarda definitiva, com o teto de 24 h/7
    # dias, fica em ensure_follow_up_v2_lifecycle_txn (app/durable/followup.py).
    if any(is_refusal(t) for t in falas[-2:]) or (falas and is_farewell(falas[-1])):
        return
    defer_once(
        follow_up_start_job,
        queueing_lock=stable_id("followup-v2", turn.tenant_id, turn.remote_jid, turn.turn_id),
        tenant_id=turn.tenant_id,
        remote_jid=turn.remote_jid,
        phone=phone,
        instance=turn.instance,
        origin_turn_id=turn.turn_id,
        provider=turn.provider,
    )


def transcribe_turn_audio(turn: ClaimedTurn) -> tuple[ClaimedTurn, str | None]:
    """Transcribe every inbound audio event in a turn, or set the human fallback.

    Returns ``(turn, None)`` when all audio transcribed (events get the
    transcribed text as ``text_content``) or when the turn has no audio;
    returns ``(turn, fallback_text)`` when any audio is missing/invalid and
    the caller should reply with the human fallback instead of the LLM.

    Fora do fluxo desde a rota de midia (08/out/2026): o turno usa
    ``enrich_turn_media``. Mantida para compatibilidade.
    """
    transcribed_events = []
    for event in turn.events:
        if event.direction != "inbound":
            transcribed_events.append(event)
            continue
        audio_base64 = event.sanitized_payload.audio_base64
        if audio_base64 is None:
            transcribed_events.append(event)
            continue
        result = transcribe_audio_step(audio_base64)
        if result is None:
            # Texto fixo, nunca o que o cliente escreveu: concatenar as falas
            # anteriores do turno ENVIAVA de volta o texto da propria paciente.
            return turn, AUDIO_FALLBACK_TEXT
        transcribed_events.append(
            event.model_copy(update={"text_content": result.text})
        )
    if all(a == b for a, b in zip(transcribed_events, turn.events)):
        return turn, None
    return turn.model_copy(update={"events": transcribed_events}), None


def delivery_mode_step() -> Literal["record_only", "live"]:
    """Checkpoint delivery mode so a workflow replay observes one policy."""
    return get_settings().DELIVERY_MODE


def guard_assess_step(
    stage: Literal["input", "output"],
    texts: list[str],
    event_refs: tuple[str, ...],
) -> GuardAssessment:
    """Avalia um estagio com as checagens locais (texto invisivel; segredo na saida).

    Sem rede desde 02/nov/2026 (o sidecar llm-guard saiu), entao o resultado
    nunca e ``unavailable`` e a guarda de saida nao derruba mais resposta boa
    por indisponibilidade.
    """
    if not texts:
        return GuardAssessment(
            stage=stage,
            disposition=GuardDisposition.ALLOW.value,
            findings=[],
            event_refs=event_refs,
            deterministic=True,
        )
    settings = get_settings()
    desligado = not settings.GUARD_ENABLED or (
        stage == GuardStage.OUTPUT.value and not settings.OUTPUT_GUARD_ENABLED
    )
    if desligado:
        # Desligado por configuracao: libera sem checar.
        return GuardAssessment(
            stage=stage,
            disposition=GuardDisposition.ALLOW.value,
            findings=[],
            event_refs=event_refs,
            deterministic=True,
        )
    return assess_locally(stage, texts, event_refs=event_refs)


def turn_has_audio(turn: ClaimedTurn) -> bool:
    """True when any inbound event carries an audio message (voice turn)."""
    return any(
        event.direction == "inbound"
        and event.sanitized_payload.audio_base64 is not None
        for event in turn.events
    )


def _guard_event_refs(turn: ClaimedTurn) -> tuple[str, ...]:
    return tuple(
        str(event.event_row_id)
        for event in getattr(turn, "events", [])
        if getattr(event, "event_row_id", None) is not None
    )



def _collect_current_turn_texts(turn: ClaimedTurn) -> list[str]:
    texts: list[str] = []
    for event in getattr(turn, "events", []):
        text = getattr(event, "text_content", None)
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return texts


def _collect_historical_guard_texts(history: Any, *, summary: str | None) -> list[str]:
    """Textos do historico que o guarda de injecao examina: so o summary curado.

    As mensagens ``assistant`` (a IA e a equipe) sao saida nossa e nunca entram
    (8fa736c: o classificador marcava "fica a vontade" como injecao). A fala antiga
    do CLIENTE tambem nao entra mais (08/out/2026): ela passa pelo guarda do turno
    em que e respondida, e reescanear a cada turno so tornava o falso positivo
    permanente. Excecao: mensagem de turno parado antes do guarda (automacao
    desligada em crew/human_owned, turno trivial suprimido, suprimido por crew_in)
    nunca e escaneada e so chega ao LLM quando a automacao volta. Como o achado do
    guarda e consultivo desde a9f21a6, perde-se so a linha de auditoria. O classificador da score 1.0 a pedido comum em
    portugues ("veja o valor", "faca o orcamento"); o mesmo texto voltava como
    achado em todo turno seguinte, enchia security_events e o lote de ate 13 textos
    estourava o timeout do sidecar. ``history`` fica na assinatura por
    compatibilidade.
    """
    if summary and summary.strip():
        return [summary.strip()]
    return []


def _collect_customer_texts_for_audit(turn: ClaimedTurn, history: Any) -> list[str]:
    """Fala do cliente em ordem: historico primeiro, turno atual por ultimo.

    So role="customer" -- resposta do proprio agente no historico misturaria
    vocabulario de negocio sempre presente e distorceria o sinal de
    bad_faith_signal (ver app/status/conversation_audit.py).
    """
    texts = [
        message.text.strip()
        for message in getattr(history, "messages", [])
        if getattr(message, "role", None) == "customer" and message.text.strip()
    ]
    texts.extend(_collect_current_turn_texts(turn))
    return texts


def _safe_output_guard_draft(draft: ReplyDraft) -> ReplyDraft:
    # Sem status_signal de proposito: o voto veio junto de uma resposta que a
    # guarda barrou; o status deste turno fica so com o juiz.
    if draft.action == "handoff":
        return ReplyDraft(
            action="handoff",
            text=SAFE_HANDOFF_TEXT,
            llm_model=draft.llm_model,
        )
    # O texto promete que a equipe confirma: tem de ser handoff, senão ninguém
    # é avisado e o paciente fica esperando.
    return ReplyDraft(
        action="handoff",
        text=OUTPUT_GUARD_FALLBACK_TEXT,
        llm_model=draft.llm_model,
    )


def _guard_review_handoff_commands(
    turn: ClaimedTurn,
    delivery_mode: Literal["record_only", "live"],
) -> list[OutboxCommand]:
    """Escalate suspicious historical context for human review without banning.

    Current-turn malicious input is an active customer action and may finalize the
    client. Historical/summary-only findings are different: they can be stale or
    curator-induced context, so the safe outcome is human review with no status
    change.
    """
    manager_jid = manager_jid_txn(turn.tenant_id)
    commands = [
        OutboxCommand(
            tenant_id=turn.tenant_id,
            turn_id=turn.turn_id,
            ordinal=0,
            instance=turn.instance,
            destination_jid=turn.remote_jid,
            action="send_text",
            kind="customer",
            purpose="handoff",
            text=GUARD_REVIEW_HANDOFF_TEXT,
            idempotency_key=stable_id(
                "outbox", turn.tenant_id, turn.turn_id, 0, "send_text"
            ),
            delivery_mode=delivery_mode,
        )
    ]
    if not manager_jid:
        return commands
    customer_lines = [
        event.text_content.strip()
        for event in turn.events
        if event.text_content and event.text_content.strip()
    ]
    # Mesma linha humana do aviso de handoff (sem colchete, sem "rota"/"IA"):
    # o motivo é a revisão do contexto sinalizado.
    notice = build_human_notice(
        remote_jid=turn.remote_jid,
        patient_texts=customer_lines,
        name=contact_name(turn.events),
        motivo="precisa de uma olhada sua na conversa",
    )
    commands.append(
        OutboxCommand(
            tenant_id=turn.tenant_id,
            turn_id=turn.turn_id,
            ordinal=1,
            instance=turn.instance,
            destination_jid=manager_jid,
            action="send_text",
            kind="team",
            text=notice,
            idempotency_key=stable_id(
                "outbox", turn.tenant_id, turn.turn_id, 1, "send_text"
            ),
            delivery_mode=delivery_mode,
        )
    )
    return commands


def build_decision_and_commands(
    turn: ClaimedTurn,
    draft: ReplyDraft,
    delivery_mode: Literal["record_only", "live"],
    *,
    voice: bool | None = None,
    customer_status: str = "open",
    summary: str | None = None,
    handoff_cause: HandoffCause | None = None,
) -> tuple[AgentDecision, list[OutboxCommand]]:
    """Map an untrusted provider draft into canonical domain policy objects.

    ``send_text`` drafts are split into 1..N human-paced messages (ordinals
    0..N-1, each with its own canonical idempotency key); ``handoff`` stays
    a single command. Voice turns (inbound audio, ``voice=True``) get a
    ``send_media`` command when TTS synthesizes; a TTS miss falls back to
    plain text so a voice turn never strands a conversation. When the
    caller already decided a text-only reply (e.g. the transcription
    fallback asking the customer to write), pass ``voice=False``.

    When ``summary`` is provided (curated on this turn's first-contact along
    with ``is_new_client``), it rides in ``decision.memory_updates`` so the
    JSONB merge in ``_CLIENT_APPLY_DECISION_SQL`` persists it as
    ``clients.metadata.summary`` for every subsequent turn — a dedicated
    write is deliberately avoided; the existing pipe is reused.
    """
    needs_human = draft.action == "handoff"
    memory_updates: dict[str, Any] = {}
    if summary:
        memory_updates["summary"] = summary
    decision = AgentDecision(
        intent="handoff" if needs_human else "reply",
        action=draft.action,
        confidence=0.0 if needs_human else 1.0,
        customer_status=customer_status,
        needs_human=needs_human,
        # Prazo da marcacao de equipe (crew_in_at) -- lei de 08/out/2026.
        crew_expires=needs_human
        and handoff_marking_expires(turn.tenant_id, handoff_cause),
        memory_updates=memory_updates,
        draft_text=draft.text,
        llm_model=draft.llm_model,
    )
    if needs_human:
        return decision, _handoff_commands(
            turn, draft, delivery_mode, handoff_cause=handoff_cause
        )

    if (turn_has_audio(turn) if voice is None else voice):
        tts_engine = tts_step(draft.text)
        if tts_engine is not None:
            return decision, [
                OutboxCommand(
                    tenant_id=turn.tenant_id,
                    turn_id=turn.turn_id,
                    ordinal=0,
                    instance=turn.instance,
                    destination_jid=turn.remote_jid,
                    action="send_media",
                    text=draft.text,
                    media_base64=tts_engine.media_base64,
                    mime_type=tts_engine.mime_type,
                    idempotency_key=stable_id(
                        "outbox", turn.tenant_id, turn.turn_id, 0, "send_media"
                    ),
                    delivery_mode=delivery_mode,
                )
            ]

    settings = get_settings()
    texts = split_human_reply(
        draft.text,
        max_blocks=settings.OUTPUT_SPLIT_MAX_BLOCKS,
        target_chars=settings.OUTPUT_SPLIT_TARGET_CHARS,
    )
    if not texts:
        texts = [draft.text]
    commands = [
        OutboxCommand(
            tenant_id=turn.tenant_id,
            turn_id=turn.turn_id,
            ordinal=ordinal,
            instance=turn.instance,
            destination_jid=turn.remote_jid,
            action=draft.action,
            text=text,
            idempotency_key=stable_id(
                "outbox", turn.tenant_id, turn.turn_id, ordinal, draft.action
            ),
            delivery_mode=delivery_mode,
        )
        for ordinal, text in enumerate(texts)
    ]
    return decision, commands


def _handoff_commands(
    turn: ClaimedTurn,
    draft: ReplyDraft,
    delivery_mode: Literal["record_only", "live"],
    *,
    handoff_cause: HandoffCause | None = None,
) -> list[OutboxCommand]:
    """Route a handoff: inform the customer and notify the company manager.

    The first command tells the customer that a person will take over. When
    the tenant has a manager JID registered (team route), a second command
    is added for the manager with a short context of the turn. Without a
    manager JID the handoff still resolves (single customer notice), so a
    missing registration can never strand a conversation.
    """
    manager_jid = manager_jid_txn(turn.tenant_id)
    commands = [
        OutboxCommand(
            tenant_id=turn.tenant_id,
            turn_id=turn.turn_id,
            ordinal=0,
            instance=turn.instance,
            destination_jid=turn.remote_jid,
            action="send_text",
            kind="customer",
            purpose="handoff",
            text=draft.text,
            idempotency_key=stable_id(
                "outbox", turn.tenant_id, turn.turn_id, 0, "send_text"
            ),
            delivery_mode=delivery_mode,
        )
    ]
    if not manager_jid:
        return commands
    # Aviso curto e acionável para o gestor, em tom humano (ver
    # app/durable/handoff_notice.py): telefone discável, nome do contato, motivo
    # em frase natural e últimas falas, sem CPF/e-mail/nascimento. Só o TEXTO
    # mudou; destino, ordinal, kind e idempotência do comando seguem idênticos.
    notice = build_manager_notice(
        remote_jid=turn.remote_jid,
        events=turn.events,
        cause=handoff_cause,
    )
    commands.append(
        OutboxCommand(
            tenant_id=turn.tenant_id,
            turn_id=turn.turn_id,
            ordinal=1,
            instance=turn.instance,
            destination_jid=manager_jid,
            action="send_text",
            kind="team",
            text=notice,
            idempotency_key=stable_id(
                "outbox", turn.tenant_id, turn.turn_id, 1, "send_text"
            ),
            delivery_mode=delivery_mode,
        )
    )
    return commands


def _turno_e_totalmente_trivial(turn: ClaimedTurn) -> bool:
    """True so quando TODA mensagem inbound do turno e' trivial sem sinal.

    Conservador de proposito: mensagem sem texto (audio/imagem ainda nao
    transcrita, por exemplo) nunca conta como trivial -- so suprime quando ha
    certeza textual em cada uma das mensagens do lote que o join juntou.
    """
    from app.status.rules import is_trivial_no_reply

    # getattr: fixtures de teste (SimpleNamespace) nem sempre setam .events/
    # .direction -- em producao ClaimedTurn/ClaimedEvent sempre tem os campos
    # (obrigatorios no schema), entao isto e' so defesa contra objeto de teste
    # incompleto, nunca afeta o caminho real.
    eventos = [
        event
        for event in getattr(turn, "events", None) or []
        if getattr(event, "direction", None) == "inbound"
    ]
    # Midia (imagem, audio, video, documento) nunca e trivial: "ok" + foto do
    # pedido medico era suprimido como agradecimento.
    for event in eventos:
        payload = getattr(event, "sanitized_payload", None)
        if payload is not None and media_kind_of(
            payload, getattr(turn, "provider", "evolution")
        ) in MEDIA_KINDS:
            return False
    textos = [getattr(event, "text_content", None) for event in eventos]
    if not textos:
        return False
    return all(
        texto is not None and texto.strip() and is_trivial_no_reply(texto)
        for texto in textos
    )


def _advance_lead_stage(
    turn: ClaimedTurn,
    history: Any,
    phone: str,
) -> None:
    """Best-effort: sincroniza a etapa do lead em genius_maps.leads com a
    classificacao deste turno. Nunca levanta -- a resposta ao cliente ja foi
    despachada antes desse ponto e nao depende disso."""
    customer_text = "\n".join(_collect_current_turn_texts(turn))
    has_history = bool(getattr(history, "messages", None))
    try:
        advance_lead_stage_step(
            turn.tenant_id, phone, customer_text, has_history=has_history
        )
    except Exception:
        logger.debug("advance_lead_stage best-effort indisponivel", exc_info=True)


# O que o alerta ao celular do operador diz sobre o paciente (so quando o fallback agiu).
_RESUMO_FALLBACK = {
    "espera_enviada": "Paciente recebeu mensagem de espera e segue aguardando resposta. ",
    "conversa_com_a_equipe": "Conversa esta com a equipe. ",
    "ja_respondido": "Paciente ja tinha resposta. ",
    "falhou": "Mensagem de espera tambem falhou. ",
}


def conversation_turn_workflow(release: TurnRelease) -> ClaimedTurn | None:
    """Process one finite batch through bounded reply and guarded delivery."""
    result = claim_turn_txn(release.event, CLAIM_ALL_SEQUENCE)
    if result.turn is None:
        return None
    turn = result.turn
    try:
        # An operator control is authoritative: never draft or dispatch while the
        # conversation belongs to a human or is already finalized.
        if not automation_allowed_for_turn_txn(turn.tenant_id, release.event.phone):
            stop_turn_for_control_txn(turn.turn_id, turn.tenant_id)
            # 13/out/2026: o turno da equipe (mao humana no comando) e um dos
            # momentos em que o paciente confirma -- auditado sem resposta.
            _start_status_sem_resposta(turn, release.event.phone)
            return turn
        # Todo turno cujas mensagens do cliente sao 100% triviais (agradecimento,
        # risada, emoji-so, confirmacao curta) nao gera resposta nenhuma -- nem
        # chega ao agente, zero token gasto. Reversivel via settings, sem
        # redeploy (mesmo padrao de STATUS_AUDIT_ENABLED/GUARD_ENABLED).
        if get_settings().SUPPRESS_TRIVIAL_TURNS_ENABLED and _turno_e_totalmente_trivial(
            turn
        ):
            stop_turn_as_trivial_txn(turn.turn_id, turn.tenant_id)
            # "obrigada" logo apos a equipe confirmar a consulta e aceite, nao
            # ruido -- por isso o gate de pista de agendamento (has_recent_
            # booking_cue) em vez de auditar todo turno trivial.
            _start_status_sem_resposta(turn, release.event.phone, requer_pista=True)
            return turn
        # Rota de midia: imagem, PDF, video e audio viram texto rotulado
        # ("[imagem] ...") antes do historico e do guard, que avalia tambem o
        # texto extraido (consultivo, como o digitado).
        media = enrich_turn_media(turn, phone=release.event.phone)
        turn = media.turn
        if media.outcome == "silent":
            # Figurinha ou reacao sozinhas: nada a responder, sem erro.
            stop_turn_as_trivial_txn(turn.turn_id, turn.tenant_id)
            return turn
        # TASKS #3 — rota fixa de segurança (vertical clínica): detecção
        # determinística (regex, sem LLM) sobre as falas do cliente neste
        # turno, só para tenants em SAFETY_ROUTE_TENANTS. Calculada DEPOIS da
        # rota de midia: a transcricao de audio e a legenda entram; OCR, PDF e
        # video nao (um pedido impresso com "urgência" seria falso positivo).
        # Vence o fallback de midia. Só mensagens inbound.
        safety_draft = safety_handoff_draft(turn.tenant_id, media.safety_texts)
        # Causa do handoff para o aviso ao gestor (motivo em uma linha).
        handoff_cause: HandoffCause | None = None
        if safety_draft is not None:
            handoff_cause = (
                "emergencia" if safety_draft == EMERGENCY_DRAFT else "medicamento"
            )
        history = fetch_pg_history_step(turn)
        # First-contact curation: a customer new to genius_next.clients (but with
        # pre-existing WhatsApp/Evolution history) gets a deep-fetch + summary this
        # turn, so neither this reply nor any later one greets them blind.  It must
        # run before draft_reply_step so the summary is already in context (see
        # summary_override in draft_reply/_reply_context).  Best-effort: a None
        # summary just means no curation on this turn — never blocks the reply.
        summary: str | None = None
        if release.is_new_client and release.event.identity.provider == "evolution":
            summary = summarize_history_step(
                release.event.tenant_id,
                release.event.phone,
                turn.instance,
                turn.remote_jid,
            )
        current_input_assessment = guard_assess_step(
            GuardStage.INPUT.value,
            _collect_current_turn_texts(turn),
            _guard_event_refs(turn),
        )
        if current_input_assessment.disposition == GuardDisposition.BLOCK.value:
            # A statistical prompt-injection finding on the live customer
            # message is advisory. The message remains untrusted user input
            # in the prompt, while the security event is audited separately;
            # it must not become a business ``off`` transition or silently
            # suppress an ordinary reply after a false positive.
            record_security_event_txn(
                tenant_id=turn.tenant_id,
                phone=release.event.phone,
                conversation_id=turn.conversation_id,
                turn_id=turn.turn_id,
                assessment=current_input_assessment,
            )
            logger.warning(
                "current input guard finding treated as advisory for turn %s",
                turn.turn_id,
            )
        if current_input_assessment.disposition == GuardDisposition.UNAVAILABLE.value:
            logger.warning("guard input unavailable for turn %s", turn.turn_id)

        historical_input_texts = _collect_historical_guard_texts(history, summary=summary)
        historical_input_assessment = guard_assess_step(
            GuardStage.INPUT.value,
            historical_input_texts,
            _guard_event_refs(turn),
        )
        if historical_input_assessment.disposition == GuardDisposition.BLOCK.value:
            # Desde 08/out/2026 o "historico" aqui e so o summary curado do primeiro
            # contato (ver _collect_historical_guard_texts).
            # 08/out/2026 (ordem do chairman): achado so no HISTORICO e consultivo,
            # como o do turno atual. O classificador de injecao marcava falas normais
            # e a rota de revisao mandava paciente comum para a equipe, com o
            # telefone do gestor. Fica auditado em security_events; a resposta segue
            # o caminho normal (a guarda de saida continua valendo).
            record_security_event_txn(
                tenant_id=turn.tenant_id,
                phone=release.event.phone,
                conversation_id=turn.conversation_id,
                turn_id=turn.turn_id,
                assessment=historical_input_assessment,
            )
            logger.warning(
                "historical input guard finding treated as advisory for turn %s",
                turn.turn_id,
            )
        if historical_input_assessment.disposition == GuardDisposition.UNAVAILABLE.value:
            logger.warning("guard historical input unavailable for turn %s", turn.turn_id)

        if safety_draft is not None:
            # Curto-circuito da rota fixa: nenhum texto gerado por LLM. O draft
            # de handoff segue pelo caminho normal (build_decision_and_commands
            # -> _handoff_commands -> manager_jid), sem caminho paralelo novo.
            draft = safety_draft
            customer_status = "open"
        elif media.outcome == "handoff":
            # Segunda midia que nao abriu em 24 h: a equipe assume.
            draft = ReplyDraft(action="handoff", text=MEDIA_HANDOFF_TEXT)
            customer_status = "open"
            handoff_cause = "midia"
        elif media.outcome == "fallback":
            # Texto fixo, sem LLM e sem eco do que o cliente escreveu.
            draft = ReplyDraft(
                action="send_text", text=media.fallback_text or AUDIO_FALLBACK_TEXT
            )
            customer_status = "open"
        else:
            manager_draft = None
            try:
                from app.reply.manager import (
                    manager_contact_draft,
                    manager_enabled,
                )
                if manager_enabled(turn.tenant_id):
                    manager_draft = manager_contact_draft(turn)
            except Exception:
                logger.warning("manager_contact_draft falhou", exc_info=True)
            if manager_draft is not None:
                draft = manager_draft
                customer_status = "open"
                try:
                    from app.durable.manager import register_turn_items
                    register_turn_items(turn, draft, phone=release.event.phone)
                except Exception:
                    logger.warning("manager: falha ao registrar", exc_info=True)
            else:
                try:
                    draft, customer_status = _draft_reply(turn, history, summary=summary)
                except ValidationError:
                    # 05/set/2026: rede so para ValidationError -- a classe exata
                    # do incidente de 27/ago (enum morto em customer_status, corri-
                    # gido pontualmente, mas o padrao "resposta do modelo ja paga
                    # e um Pydantic model depois nao valida" continua estrutural).
                    # Deliberadamente NAO um `except Exception`: RuntimeError e
                    # outras falhas de infra (provider fora, timeout) precisam
                    # continuar subindo para o except Exception deste job, que
                    # persiste via fail_turn_txn e deixa a fila decidir o retry --
                    # ver test_conversation_turn_persists_domain_failure_before_
                    # reraising e test_conversation_turn_reraises_original_when_
                    # turn_already_completed, que fixam esse contrato. ValidationError
                    # e' diferente: o modelo ja respondeu certo, o problema e' o
                    # codigo Python validando o resultado, e reexecutar o job do
                    # zero rechamaria o modelo (draft_reply_with_agent nao e' um job
                    # separado) so para tropecar no mesmo bug de novo. Handoff e'
                    # o fallback seguro porque ja e' testado e ja tem workflow
                    # humano por tras; inventar texto novo aqui dobraria a
                    # superficie de erro em vez de reduzi-la.
                    logger.exception(
                        "draft_reply devolveu ValidationError apos possivel chamada "
                        "ao modelo; turno %s cai para handoff",
                        turn.turn_id,
                    )
                    draft = ReplyDraft(action="handoff", text=SAFE_HANDOFF_TEXT)
                    customer_status = "open"
                else:
                    # Resposta do modelo que promete "a equipe vai confirmar" com
                    # send_text nao avisa ninguem: promove a handoff (so tenants em
                    # HANDOFF_PROMOTE_TENANTS). Fora do fallback de midia e do
                    # safety_draft, que ja tem seu caminho proprio.
                    promoted_draft = promote_forwarding_to_handoff(turn.tenant_id, draft)
                    if promoted_draft is not draft:
                        draft = promoted_draft
                        handoff_cause = "promovido"
                # Triagem proativa (branch feat/triagem-proativa): antes de
                # acionar a equipe, o Manager tenta resolver sozinho.
                # Quando assumir, troca o handoff por um texto de espera — a
                # conversa NAO e marcada como da equipe e segue como resposta
                # normal (as mesmas guardas de saida abaixo). Fora da flag nada
                # muda. Roda aqui, um unico ponto, logo apos o draft do ramo
                # normal (e a promocao); seguranca/midia nao chegam a este ramo.
                try:
                    from app.durable.manager import register_turn_triagem
                    espera = register_turn_triagem(
                        turn, draft, handoff_cause, phone=release.event.phone
                    )
                    if espera is not None:
                        draft = espera
                except Exception:
                    logger.warning("manager: falha ao registrar triagem", exc_info=True)
                try:
                    from app.durable.manager import register_turn_items
                    register_turn_items(turn, draft, phone=release.event.phone)
                except Exception:
                    logger.warning("manager: falha ao registrar", exc_info=True)
        decision, commands = build_decision_and_commands(
            turn,
            draft,
            delivery_mode_step(),
            voice=media.outcome == "proceed",
            customer_status=customer_status,
            summary=summary,
            handoff_cause=handoff_cause,
        )
        output_assessment = guard_assess_step(
            GuardStage.OUTPUT.value,
            [draft.text],
            _guard_event_refs(turn),
        )
        if output_assessment.disposition in (
            GuardDisposition.BLOCK.value,
            GuardDisposition.UNAVAILABLE.value,
        ):
            if output_assessment.disposition == GuardDisposition.UNAVAILABLE.value:
                logger.warning("guard output unavailable for turn %s", turn.turn_id)
            safe_draft = _safe_output_guard_draft(draft)
            safe_decision, safe_commands = build_decision_and_commands(
                turn,
                safe_draft,
                delivery_mode_step(),
                voice=False,
                customer_status=decision.customer_status,
                summary=summary,
                handoff_cause=handoff_cause,
            )
            recorded_commands = record_guard_decision_batch_txn(
                turn,
                release.event.phone,
                safe_decision,
                safe_commands,
                output_assessment,
            )
            dispatch_outbox_batch_workflow(recorded_commands)
            _advance_lead_stage(turn, history, release.event.phone)
            # 13/out/2026: handoff audita tambem -- e o turno em que a equipe
            # assume e o paciente responde a ela.
            if safe_draft.action in ("send_text", "handoff"):
                _start_status(
                    turn,
                    history,
                    safe_draft,
                    release.event.phone,
                )
            # Handoff seguro tambem agenda quando a marcacao tem prazo (4min30);
            # rota fixa de seguranca e tenant fora da lista, nao.
            _start_follow_up_v2(
                turn,
                release.event.phone,
                customer_status=safe_decision.customer_status,
                needs_human=safe_decision.needs_human,
                handoff_cause=handoff_cause,
            )
            if result.has_more:
                _defer_turn(release)
            return turn

        # Ponto unico de auditoria pos-hoc -- ver app/status/conversation_audit.py.
        # So roda aqui (draft ja passou pelo guard de saida, e o texto real
        # que vai pro cliente): auditar o safe_draft do ramo acima nao faz
        # sentido, e fallback canonico, nao geracao real.
        audit_turn(
            turn_id=turn.turn_id,
            tenant_id=turn.tenant_id,
            customer_texts=_collect_customer_texts_for_audit(turn, history),
            agent_reply=draft.text,
            customer_status=decision.customer_status,
        )

        recorded_commands = record_decision_batch_txn(
            turn,
            release.event.phone,
            decision,
            commands,
        )
        dispatch_outbox_batch_workflow(recorded_commands)
        _advance_lead_stage(turn, history, release.event.phone)
        if draft.action in ("send_text", "handoff"):
            _start_status(
                turn,
                history,
                draft,
                release.event.phone,
            )
        # Handoff automatico (modelo, promocao) tambem agenda o follow-up quando
        # a marcacao tem prazo (handoff_marking_expires); a rota fixa de
        # seguranca e o tenant fora de HANDOFF_AUTO_RELEASE_TENANTS, nao.
        _start_follow_up_v2(
            turn,
            release.event.phone,
            customer_status=decision.customer_status,
            needs_human=decision.needs_human,
            handoff_cause=handoff_cause,
        )
        if result.has_more:
            _defer_turn(release)
        return turn
    except Exception as exc:
        # Best-effort domain visibility: persist the failure so the turn is not
        # silently lost (turns.error + terminal status).  Never let this mask
        # the root cause: if the turn is already terminal (e.g.
        # record_decision_batch_txn completed it in the same commit), log the
        # persistence conflict and still re-raise the ORIGINAL exception.
        try:
            fail_turn_txn(turn, f"{type(exc).__name__}: {exc}")
        except Exception as persist_exc:
            logger.exception(
                'turn %s: failed to persist failure (%s); re-raising original %r',
                turn.turn_id,
                persist_exc,
                exc,
            )
        # 30/out/2026: ramo de erro (estilo n8n). Com o tenant em
        # TURN_FALLBACK_TENANTS, o paciente recebe uma frase de espera; a equipe da
        # clinica nao e avisada, o operador e, pelo alerta abaixo.
        # acionar_fallback_do_turno nunca levanta.
        desfecho = "falhou"
        try:
            desfecho = acionar_fallback_do_turno(
                turn, release.event.phone, delivery_mode_step()
            )
        except Exception:
            logger.warning("turn %s: fallback do turno falhou", turn.turn_id, exc_info=True)
        logger.info("turn %s: fallback do turno = %s", turn.turn_id, desfecho)
        # Ordem do chairman 15/out/2026: erro de flow avisa no WhatsApp dele.
        # Nunca pode trocar a excecao original por uma falha do alerta.
        try:
            ops_alert_step(
                f"turno:{turn.turn_id}",
                ops_alert_text(
                    "turno nao concluido",
                    tenant_id=turn.tenant_id,
                    contact=release.event.phone,
                    error=f"{_RESUMO_FALLBACK.get(desfecho, '')}{type(exc).__name__}: {exc}",
                ),
            )
        except Exception:
            logger.warning(
                "turn %s: alerta operacional nao enviado", turn.turn_id, exc_info=True
            )
        raise


@jobs.task(name="conversation_turn", queue=TURN_QUEUE)
def conversation_turn_job(release: dict[str, Any]) -> None:
    conversation_turn_workflow(TurnRelease.model_validate(release))


def _defer_turn(release: TurnRelease, *, wait_seconds: float | None = None) -> bool:
    """Agenda o turno da conversa: um por vez (lock), ao vivo na frente do follow-up.

    Com ``wait_seconds`` e o debounce: ``queueing_lock`` junta a rajada num unico turno (os que
    chegam enquanto ele espera sao absorvidos, e o claim le tudo o que ja foi gravado).
    """
    conversation = release.event.conversation_id
    return defer_once(
        conversation_turn_job,
        lock=conversation,
        priority=TURN_PRIORITY_LIVE,
        queueing_lock=stable_id("turn-debounce", conversation) if wait_seconds is not None else None,
        schedule_in=wait_seconds,
        release=release.model_dump(mode="json"),
    )


def _draft_reply(
    turn: ClaimedTurn, history: Any, *, summary: str | None = None
) -> tuple[ReplyDraft, str]:
    """Escolhe o motor de redacao pela chave REPLY_ENGINE.

    A bifurcacao mora AQUI, no job do turno, e nao dentro de draft_reply_step
    de proposito: o caminho do agente nao pode rodar dentro de outro job. O
    caminho legado continua sendo a chamada direta a ``draft_reply_step``.
    """
    if get_settings().REPLY_ENGINE == "agent":
        from app.reply.agent import draft_reply_with_agent

        return draft_reply_with_agent(turn, history, summary=summary)
    return draft_reply_step(turn, history, summary=summary)


def _eligible_for_turn(event: TrustedIngress) -> bool:
    """Only direct inbound messages can release an automated turn.

    Accepts both traditional phone JIDs (``@s.whatsapp.net``) and WhatsApp
    LinkedID JIDs (``@lid``) — a LinkedID is a direct JID, not a group, and
    the dominant addressing format for real tenants (mariaeduarda).
    """
    return event.direction == "inbound" and event.identity.remote_jid.endswith(
        ("@s.whatsapp.net", "@lid")
    )


DEBOUNCE_WAIT_SECONDS = 8.0


def _pick_debounce_wait_seconds() -> float:
    """Fixed join window (chairman, 22/ago/2026); desde 31/out/2026 e fixa a partir da 1a mensagem.

    Was a 7-12s random draw "to feel less robotic"; the variance bought
    nothing measurable and made turn latency impossible to reason about.
    Still a step, not an inline constant: dropping the decorator would
    shift the checkpoint sequence of workflows already in flight.

    26/ago/2026: virou chave (``DEBOUNCE_WAIT_SECONDS``). Medido em producao, esta
    janela era 8 dos ~21s que o cliente esperava pela primeira bolha — o maior
    pedaco que nao e o modelo. A constante fica como piso de compatibilidade.
    """
    return get_settings().DEBOUNCE_WAIT_SECONDS


def ingest_event_workflow(event: TrustedIngress) -> PersistEventResult:
    """Persist first, then signal the bounded conversation debouncer once."""
    result = persist_event_txn(event)
    if _eligible_for_turn(event) and not result.inserted:
        # Reentrega do provedor: o evento ja esta gravado, mas o turno pode nunca ter sido
        # agendado (queda entre o persist e o agendamento). Agendar de novo e inofensivo:
        # o claim nao encontra nada ja reivindicado.
        _defer_turn(
            TurnRelease(event=event, through_sequence=result.sequence),
            wait_seconds=_pick_debounce_wait_seconds(),
        )
    if result.inserted and _eligible_for_turn(event):
        # Rota de midia: baixa e extrai ja na chegada, dentro da janela de
        # debounce (e tambem com a conversa na equipe, para o inbox ver o
        # rotulo e a midia nao expirar). Best-effort: o turno reinicia pelo
        # mesmo ID deterministico se este disparo falhar.
        maybe_enqueue_media_on_ingest(event)
        # 27/ago/2026: cliente loss que fala de novo reabre sozinho -- e ele
        # quem decide voltar, nao a empresa cutucando. Nunca reabre
        # crew (operador) nem block (spam/abuso, so humano libera).
        # Best-effort: uma falha aqui nao pode derrubar a ingestao do evento.
        try:
            reopen_if_loss_txn(event.tenant_id, event.phone)
        except Exception:
            logger.warning(
                "reopen-if-loss failed for %s/%s",
                event.tenant_id,
                event.phone,
                exc_info=True,
            )
        # 21/set/2026: cliente win que volta a falar reabre como open --
        # pode querer comprar outro produto. Mesma logica do reopen de loss:
        # e o cliente quem reabre, nunca a empresa. Se precisar de humano,
        # o handoff normal da conversa cuida disso (needs_human na LLM).
        try:
            reopen_if_win_txn(event.tenant_id, event.phone)
        except Exception:
            logger.warning(
                "reopen-if-win failed for %s/%s",
                event.tenant_id,
                event.phone,
                exc_info=True,
            )
        # O "digitando..." saiu daqui para app.main._fire_ingress_presence, na
        # entrada do webhook: aqui ele só aparecia depois de persistir o evento
        # e ainda esperava 5s antes de disparar, então o cliente ficava ~5s sem
        # ver sinal nenhum enquanto a janela de debounce corria.
        release = TurnRelease(
            event=event,
            through_sequence=result.sequence,
            is_new_client=result.is_new_client,
        )
        if not result.is_new_client and event.identity.provider == "evolution":
            try:
                if not client_has_summary_txn(event.tenant_id, event.phone):
                    defer_once(
                        backfill_one_client_job,
                        queueing_lock=stable_id("summary-backfill", event.tenant_id, event.phone),
                        tenant_id=event.tenant_id,
                        phone=event.phone,
                        instance=event.identity.instance,
                        remote_jid=event.identity.remote_jid,
                    )
            except Exception:
                logger.warning(
                    "summary backfill shortcut failed for %s/%s",
                    event.tenant_id,
                    event.phone,
                    exc_info=True,
                )
        _defer_turn(release, wait_seconds=_pick_debounce_wait_seconds())
    return result
