"""Pydantic contracts at the Evolution and durable-workflow boundaries."""

import hashlib
import json
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.identity_primitives import normalize_phone, stable_id

_EXTERNAL_CONFIG = ConfigDict(
    extra="ignore", populate_by_name=True, str_strip_whitespace=True
)
_INTERNAL_CONFIG = ConfigDict(extra="forbid", str_strip_whitespace=True)
TURN_ID_PATTERN = r"^[a-z][a-z0-9-]{1,31}:[0-9a-f]{64}$"
# Meta template names are lower_snake_case; a language is a BCP-47-lite code
# like ``pt_BR`` or ``en_US``.
TEMPLATE_NAME_PATTERN = r"^[a-z][a-z0-9_]{1,511}$"
TEMPLATE_LANGUAGE_PATTERN = r"^[a-z]{2}(_[A-Z]{2})?$"


class _ExternalModel(BaseModel):
    """Evolution may add fields; ignore them until explicitly modelled."""

    model_config = _EXTERNAL_CONFIG


class _InternalModel(BaseModel):
    """Internal boundaries must reject shape drift rather than guess."""

    model_config = _INTERNAL_CONFIG


class EvolutionMessageKey(_ExternalModel):
    """Provider identity required to deduplicate one Evolution message."""

    id: str = Field(min_length=1, max_length=512)
    remote_jid: str = Field(alias="remoteJid", min_length=1, max_length=512)
    from_me: bool = Field(alias="fromMe")


class EvolutionTextMessage(_ExternalModel):
    text: str | None = Field(default=None, max_length=4_096)


def _str_or_none(value: Any) -> Any:
    """Evolution manda alguns campos como Buffer/objeto: o que nao e texto vira None."""
    return value if isinstance(value, str) or value is None else None


def _int_or_none(value: Any) -> Any:
    """Duracao chega como int, texto ou objeto Long; so inteiro valido e mantido."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


_LooseStr = Annotated[str | None, BeforeValidator(_str_or_none)]
_LooseInt = Annotated[int | None, BeforeValidator(_int_or_none)]

MEDIA_ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"
MediaKind = Literal["image", "audio", "video", "document"]
MediaKindOrSilent = Literal["image", "audio", "video", "document", "silent"]
MEDIA_KINDS: frozenset[str] = frozenset({"image", "audio", "video", "document"})


class EvolutionCaptionMessage(_ExternalModel):
    caption: str | None = Field(default=None, max_length=4_096)
    mimetype: _LooseStr = None


class EvolutionAudioMessage(_ExternalModel):
    """Áudio recebido.

    Tolerante de proposito: ``fileSha256`` vindo como Buffer/objeto e uma
    ``duration`` fora do esperado davam 422 e o audio inteiro se perdia. O base64
    pode vir aqui ou como irmao (``data.message.base64``).
    """

    base64: str | None = Field(default=None, max_length=30_000_000)
    mimetype: _LooseStr = None
    duration: _LooseInt = None
    file_sha256: _LooseStr = Field(default=None, alias="fileSha256")
    ptt: bool | None = None


class EvolutionDocumentMessage(_ExternalModel):
    caption: str | None = Field(default=None, max_length=4_096)
    mimetype: _LooseStr = None
    file_name: _LooseStr = Field(default=None, alias="fileName")
    title: _LooseStr = None


class EvolutionDocumentInner(_ExternalModel):
    document_message: EvolutionDocumentMessage | None = Field(
        default=None, alias="documentMessage"
    )


class EvolutionDocumentWithCaption(_ExternalModel):
    message: EvolutionDocumentInner | None = None


class EvolutionStickerMessage(_ExternalModel):
    mimetype: _LooseStr = None


class EvolutionReactionMessage(_ExternalModel):
    text: _LooseStr = None


class EvolutionContactMessage(_ExternalModel):
    """Cartao de contato: so o nome entra; o vcard (com telefone) nao e modelado."""

    display_name: _LooseStr = Field(default=None, alias="displayName")


class EvolutionContactsArrayMessage(_ExternalModel):
    display_name: _LooseStr = Field(default=None, alias="displayName")
    contacts: list[EvolutionContactMessage] | None = None

    @field_validator("contacts", mode="before")
    @classmethod
    def _at_most_five(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [item for item in value[:5] if isinstance(item, dict)]
        return None


class EvolutionLocationMessage(_ExternalModel):
    degrees_latitude: float | None = Field(
        default=None, alias="degreesLatitude", ge=-90, le=90
    )
    degrees_longitude: float | None = Field(
        default=None, alias="degreesLongitude", ge=-180, le=180
    )
    name: _LooseStr = None
    address: _LooseStr = None


class InboundMediaRef(_ExternalModel):
    """Referencia de midia inbound (Zernio), gravada em ``data.geniusMedia``.

    So e honrada quando o provider confiavel do evento e ``zernio``; a rota da
    Evolution zera o campo. Nunca guarda URL nem bytes.
    """

    media_id: str | None = Field(
        default=None, alias="mediaId", pattern=MEDIA_ID_PATTERN
    )
    kind: MediaKind
    mimetype: str | None = Field(default=None, max_length=128)
    sha256: str | None = Field(default=None, max_length=128)
    voice: bool | None = None
    file_name: str | None = Field(default=None, alias="fileName", max_length=256)


_CONTACT_TEXT_MAX = 120
_LOCATION_TEXT_MAX = 300


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class EvolutionMessageBody(_ExternalModel):
    conversation: str | None = Field(default=None, max_length=4_096)
    extended_text_message: EvolutionTextMessage | None = Field(
        default=None, alias="extendedTextMessage"
    )
    image_message: EvolutionCaptionMessage | None = Field(
        default=None, alias="imageMessage"
    )
    video_message: EvolutionCaptionMessage | None = Field(
        default=None, alias="videoMessage"
    )
    audio_message: EvolutionAudioMessage | None = Field(
        default=None, alias="audioMessage"
    )
    document_message: EvolutionDocumentMessage | None = Field(
        default=None, alias="documentMessage"
    )
    document_with_caption_message: EvolutionDocumentWithCaption | None = Field(
        default=None, alias="documentWithCaptionMessage"
    )
    sticker_message: EvolutionStickerMessage | None = Field(
        default=None, alias="stickerMessage"
    )
    reaction_message: EvolutionReactionMessage | None = Field(
        default=None, alias="reactionMessage"
    )
    contact_message: EvolutionContactMessage | None = Field(
        default=None, alias="contactMessage"
    )
    contacts_array_message: EvolutionContactsArrayMessage | None = Field(
        default=None, alias="contactsArrayMessage"
    )
    location_message: EvolutionLocationMessage | None = Field(
        default=None, alias="locationMessage"
    )
    live_location_message: EvolutionLocationMessage | None = Field(
        default=None, alias="liveLocationMessage"
    )
    # So a presenca importa: o conteudo nao e modelado nem persistido.
    view_once_message: dict[str, Any] | None = Field(
        default=None, alias="viewOnceMessage"
    )
    view_once_message_v2: dict[str, Any] | None = Field(
        default=None, alias="viewOnceMessageV2"
    )
    ptv_message: dict[str, Any] | None = Field(default=None, alias="ptvMessage")
    # Base64 irmao: com webhookBase64=true a Evolution manda a midia aqui.
    base64: str | None = Field(default=None, max_length=30_000_000)

    @field_validator(
        "view_once_message", "view_once_message_v2", "ptv_message", mode="before"
    )
    @classmethod
    def _presence_only(cls, value: Any) -> Any:
        return {} if value is not None else None

    @property
    def document(self) -> EvolutionDocumentMessage | None:
        if self.document_message is not None:
            return self.document_message
        wrapper = self.document_with_caption_message
        if wrapper is not None and wrapper.message is not None:
            return wrapper.message.document_message
        return None

    @property
    def has_document(self) -> bool:
        return (
            self.document_message is not None
            or self.document_with_caption_message is not None
        )

    def _contact_text(self) -> str | None:
        if self.contact_message is None and self.contacts_array_message is None:
            return None
        names: list[str] = []
        if self.contact_message is not None and self.contact_message.display_name:
            names.append(self.contact_message.display_name.strip())
        if self.contacts_array_message is not None:
            for contact in self.contacts_array_message.contacts or []:
                if contact.display_name and contact.display_name.strip():
                    names.append(contact.display_name.strip())
            if not names and self.contacts_array_message.display_name:
                names.append(self.contacts_array_message.display_name.strip())
        names = [name for name in names if name]
        label = ", ".join(names) if names else "sem nome"
        return _clip(f"[contato] {label}", _CONTACT_TEXT_MAX)

    def _location_text(self) -> str | None:
        location = self.location_message or self.live_location_message
        if location is None:
            return None
        name = (location.name or "").strip()
        address = (location.address or "").strip()
        if name or address:
            label = " — ".join(part for part in (name, address) if part)
            return _clip(f"[localização] {label}", _LOCATION_TEXT_MAX)
        if location.degrees_latitude is not None and location.degrees_longitude is not None:
            return (
                f"[localização] {location.degrees_latitude:.5f}, "
                f"{location.degrees_longitude:.5f}"
            )
        return "[localização]"

    @property
    def text_content(self) -> str | None:
        """Texto suportado, na ordem de precedencia da Evolution.

        Digitado, depois legenda (imagem, video, documento) e, por fim, o texto
        derivado sem rede de contato e de localizacao.
        """
        document = self.document
        candidates = (
            self.conversation,
            self.extended_text_message.text if self.extended_text_message else None,
            self.image_message.caption if self.image_message else None,
            self.video_message.caption if self.video_message else None,
            document.caption if document else None,
        )
        typed = next((value for value in candidates if value), None)
        if typed:
            return typed
        return self._contact_text() or self._location_text()

    @property
    def inline_base64(self) -> str | None:
        """Base64 da midia que veio no proprio evento (Evolution), se houver."""
        for value in (
            self.audio_message.base64 if self.audio_message else None,
            self.base64,
        ):
            if value is not None and value.strip():
                return value
        return None

    @property
    def audio_base64(self) -> str | None:
        """Base64 do áudio recebido (``None`` quando não há mídia de voz).

        Le ``audioMessage.base64`` e, sem ele, o base64 irmao da mensagem.
        """
        if self.audio_message is None:
            return None
        return self.inline_base64


class EvolutionMessageData(_ExternalModel):
    key: EvolutionMessageKey
    message: EvolutionMessageBody
    message_type: str = Field(alias="messageType", min_length=1, max_length=128)
    push_name: str | None = Field(default=None, alias="pushName", max_length=256)
    # Referencia de midia do Zernio. Honrada so quando o provider confiavel e
    # "zernio"; a rota /webhook/evolution zera o campo antes do ingresso.
    genius_media: InboundMediaRef | None = Field(default=None, alias="geniusMedia")


class EvolutionWebhook(_ExternalModel):
    """A supported Evolution v2.3.7 message envelope.

    Tenant identity is deliberately absent: it comes only from the trusted
    provider/instance registry after this untrusted payload has been parsed.
    Evolution v2 sends event names in caps (MESSAGES_UPSERT); older docs use
    dotted lowercase (messages.upsert). Only message-carrying events are
    accepted here — CONNECTION_UPDATE has no ``data.key``/``data.message``
    and must never reach this webhook (see Evolution instance config).
    """

    event: Literal["messages.upsert", "send.message"]
    instance: str = Field(min_length=1, max_length=128)
    data: EvolutionMessageData

    @field_validator("event", mode="before")
    @classmethod
    def _normalize_event(cls, value: Any) -> Any:
        if isinstance(value, str) and value == "MESSAGES_UPSERT":
            return "messages.upsert"
        return value

    @property
    def text_content(self) -> str | None:
        return self.data.message.text_content

    @property
    def audio_base64(self) -> str | None:
        return self.data.message.audio_base64


def media_kind_of(payload: EvolutionWebhook, provider: str) -> MediaKindOrSilent | None:
    """Classifica a midia de um evento, sem rede.

    ``None``: o evento e texto (digitado, contato ou localizacao). ``"silent"``:
    nada a responder (figurinha, reacao ou tipo desconhecido sem texto). Os
    demais: midia que a rota de midia tenta abrir.
    """
    data = getattr(payload, "data", None)
    if data is None:
        return None
    body = data.message
    if provider == "zernio" and data.genius_media is not None:
        return data.genius_media.kind
    if body.image_message is not None:
        return "image"
    if body.video_message is not None or body.ptv_message is not None:
        return "video"
    if body.audio_message is not None:
        return "audio"
    if body.has_document:
        return "document"
    if body.view_once_message is not None or body.view_once_message_v2 is not None:
        return "image"
    if body.sticker_message is not None or body.reaction_message is not None:
        return "silent"
    if body.text_content:
        return None
    return "silent"


class InboundEvent(_InternalModel):
    """Legacy internal envelope retained until durable ingress replaces its caller."""

    event_id: str = Field(min_length=1, max_length=512)
    instance: str = Field(min_length=1, max_length=128)
    payload: EvolutionWebhook

    @property
    def sanitized_payload(self) -> dict[str, Any]:
        """Return only modelled Evolution fields, using provider aliases."""
        return self.payload.model_dump(by_alias=True, exclude_none=True)


class EventIdentity(_InternalModel):
    """Trusted tenant scope paired with one validated provider event."""

    tenant_id: str = Field(min_length=1, max_length=128)
    provider: Literal["evolution", "zernio"]
    instance: str = Field(min_length=1, max_length=128)
    event_type: Literal["messages.upsert", "send.message"]
    message_id: str = Field(min_length=1, max_length=512)
    remote_jid: str = Field(min_length=1, max_length=512)

    @property
    def deduplication_scope(self) -> tuple[str, str, str]:
        """The tenant-scoped provider identity used for event uniqueness."""
        return (self.tenant_id, self.provider, self.message_id)


class TrustedIngress(_InternalModel):
    """Sanitized provider event paired with server-trusted tenant identity."""

    tenant_id: str = Field(min_length=1, max_length=128)
    identity: EventIdentity
    conversation_id: str = Field(pattern=TURN_ID_PATTERN)
    ingest_workflow_id: str = Field(pattern=TURN_ID_PATTERN)
    phone: str = Field(min_length=5, max_length=64)
    direction: Literal["inbound", "outbound"]
    text_content: str | None = Field(default=None, max_length=4_096)
    event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    sanitized_payload: EvolutionWebhook

    @model_validator(mode="after")
    def trusted_envelope_is_self_consistent(self) -> Self:
        if self.tenant_id != self.identity.tenant_id:
            raise ValueError("trusted tenant must match event identity")
        payload = self.sanitized_payload
        key = payload.data.key
        expected_identity = (
            payload.instance,
            payload.event,
            key.id,
            key.remote_jid,
        )
        actual_identity = (
            self.identity.instance,
            self.identity.event_type,
            self.identity.message_id,
            self.identity.remote_jid,
        )
        if actual_identity != expected_identity:
            raise ValueError("trusted identity must match allowlisted payload")
        if self.phone != normalize_phone(key.remote_jid):
            raise ValueError("normalized phone must match payload remote JID")
        expected_direction = "outbound" if key.from_me else "inbound"
        if self.direction != expected_direction:
            raise ValueError("direction must match payload fromMe")
        if self.text_content != payload.text_content:
            raise ValueError("text content must match allowlisted payload")
        expected_conversation_id = stable_id(
            "conversation",
            self.tenant_id,
            self.identity.provider,
            self.identity.instance,
            self.identity.remote_jid,
        )
        if self.conversation_id != expected_conversation_id:
            raise ValueError("conversation ID is not canonical")
        canonical_payload = json.dumps(
            self.sanitized_payload.model_dump(
                by_alias=True, exclude_none=True, mode="json"
            ),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        expected_event_hash = hashlib.sha256(canonical_payload).hexdigest()
        if expected_event_hash != self.event_hash:
            raise ValueError("event hash must match the allowlisted payload")
        expected_ingest_id = stable_id(
            "ingest",
            self.tenant_id,
            self.identity.provider,
            self.identity.event_type,
            self.identity.message_id,
            expected_event_hash,
        )
        if self.ingest_workflow_id != expected_ingest_id:
            raise ValueError("ingest workflow ID is not canonical")
        return self


class TurnRelease(_InternalModel):
    """A debounced conversation boundary with an immutable sequence watermark."""

    event: TrustedIngress
    through_sequence: int = Field(ge=1)
    # True when this release's conversation triggered the client's very first
    # row in genius_next.clients (persist's ``xmax = 0`` signal).  On that turn
    # only, conversation_turn_workflow deep-fetches the pre-existing Evolution
    # history and curates a summary so a returning customer is never greeted
    # blind.  False for every later release of the same conversation.
    is_new_client: bool = False


class AgentStatusSignal(_InternalModel):
    """Voto de status do agente principal (20/out/2026, specs/status-no-agente.md).

    Nunca aplicado direto: vai para o ``status_workflow``, que verifica as
    citacoes contra a transcricao e so usa o voto nos tenants das flags
    ``STATUS_AGENT_*``.
    """

    status: Literal["open", "win", "loss"] = "open"
    citacao_cliente: str = Field(default="", max_length=1_024)
    citacao_horario: str = Field(default="", max_length=1_024)


FATO_TIPOS = (
    "pergunta_aberta",
    "promessa",
    "agendamento",
    "remarcacao",
    "atraso",
    "recusa",
    "adiamento",
    "pedido_retorno",
    "insistencia_feita",
    "encerramento",
)


class FatoConversa(_InternalModel):
    """Um fato que a Julia informa junto da resposta (23/out/2026).

    Nao e status: o estado da conversa e uma projecao dos fatos (``app.state``).
    """

    tipo: Literal[
        "pergunta_aberta",
        "promessa",
        "agendamento",
        "remarcacao",
        "atraso",
        "recusa",
        "adiamento",
        "pedido_retorno",
        "insistencia_feita",
        "encerramento",
    ]
    detalhe: str = Field(default="", max_length=240)


class ReplyDraft(_InternalModel):
    """A bounded model output; policy decides whether it becomes a command."""

    action: Literal["send_text", "handoff"]
    text: str = Field(min_length=1, max_length=2_048)
    llm_model: str | None = Field(default=None, max_length=128)
    # So preenchido para tenant em STATUS_AGENT_SHADOW/VOTE_TENANTS.
    status_signal: AgentStatusSignal | None = None
    # So preenchido para tenant em FATOS_SHADOW_TENANTS (23/out/2026).
    fatos: list[FatoConversa] = Field(default_factory=list, max_length=8)


class ConversationControlRequest(_InternalModel):
    """Authenticated operator instruction; tenant scope is never inferred."""

    tenant_id: str = Field(min_length=1, max_length=128)
    phone: str = Field(min_length=5, max_length=64)
    actor_id: str = Field(min_length=1, max_length=256)
    control_id: str = Field(min_length=1, max_length=128)
    action: Literal["crew_in", "finalize", "release", "reopen"]
    terminal_status: Literal["win", "loss", "block"] | None = None

    @model_validator(mode="after")
    def terminal_status_matches_action(self) -> Self:
        if self.action == "finalize" and self.terminal_status is None:
            raise ValueError("finalize requires a terminal_status")
        if self.action != "finalize" and self.terminal_status is not None:
            raise ValueError(f"{self.action} cannot set terminal_status")
        return self


class ConversationControlApplyResult(_InternalModel):
    """Idempotent durable result for the internal commercial-control endpoint."""

    outcome: Literal["applied", "noop", "conflict"]
    automation_state: Literal["open", "crew", "loss"]


class TenantRegistryUpsertRequest(_InternalModel):
    """Authenticated self-serve registration of a provider instance for a tenant.

    Always lands with ``enabled=false`` — this endpoint links an instance to a
    tenant so the webhook stops 401ing once connected; it never turns traffic
    on for a tenant. Turning a tenant live is a separate, human-authorized step
    (``docs/runbooks/RUNBOOK-ATIVACAO-TENANT.md`` passo 7), unchanged by this.
    """

    provider: str = Field(min_length=1, max_length=64)
    instance: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    manager_jid: str | None = Field(default=None, pattern=r"^[0-9]{10,15}@s\.whatsapp\.net$")


class TenantRegistryUpsertResult(_InternalModel):
    """Idempotent durable result for the tenant-registry self-serve endpoint."""

    outcome: Literal["created", "unchanged", "instance_owned_by_other_tenant"]
    enabled: bool


class TenantRegistrySetEnabledRequest(_InternalModel):
    """Authenticated enable/disable of one existing (provider, instance) mapping.

    ``tenant_id`` is the organization of the app session that signed the call
    (never copied from a provider webhook body); it must match the row's owner.
    """

    provider: str = Field(min_length=1, max_length=64)
    instance: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:@-]+$")
    tenant_id: str = Field(min_length=1, max_length=128)


class TenantRegistrySetEnabledResult(_InternalModel):
    """Durable result of an enable/disable; ``unchanged`` when already in that state."""

    outcome: Literal[
        "enabled", "disabled", "unchanged", "not_found", "instance_owned_by_other_tenant"
    ]
    enabled: bool



class FollowUpDeliveryContext(_InternalModel):
    """Trusted origin required to re-check a synthetic follow-up at send time."""

    phone: str = Field(min_length=5, max_length=64)
    origin_turn_id: str = Field(pattern=TURN_ID_PATTERN)


class TemplateCommand(_InternalModel):
    """Reference to one Meta-approved WhatsApp template and its body variables.

    Sent only by the ``send_template`` outbox action, never as free text. Every
    field is closed (``extra="forbid"``): a shape drift must be refused, not
    guessed. ``category`` is what the caller DECLARES; the dispatch cross-checks
    it against the approved catalogue so a MARKETING template can never be
    presented as UTILITY (Meta already knows the real category from the template
    name -- this check only stops us from lying to ourselves).
    """

    name: str = Field(pattern=TEMPLATE_NAME_PATTERN)
    language: str = Field(pattern=TEMPLATE_LANGUAGE_PATTERN)
    category: Literal["UTILITY", "MARKETING"] = "UTILITY"
    # Positional values for {{1}}, {{2}}, ... in the template body. Empty when the
    # template has no body placeholders.
    variables: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("variables")
    @classmethod
    def _variables_are_single_line_text(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item.strip():
                raise ValueError("template variable cannot be blank")
            if len(item) > 1_024:
                raise ValueError("template variable is too long")
            if "\n" in item or "\r" in item:
                raise ValueError("template variable must be a single line")
        return value


class OutboxCommand(_InternalModel):
    """Complete, auditable command awaiting the explicit delivery policy."""

    tenant_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(pattern=TURN_ID_PATTERN)
    ordinal: int = Field(ge=0, le=99)
    instance: str = Field(min_length=1, max_length=128)
    destination_jid: str = Field(min_length=1, max_length=512)
    action: Literal["send_text", "send_media", "handoff", "send_template"]
    kind: Literal["customer", "team"] = "customer"
    # Rendered text: for ``send_template`` it is the template body with the
    # variables substituted, so history and the audit event show what the
    # patient actually read.
    text: str = Field(min_length=1, max_length=2_048)
    media_base64: str | None = Field(default=None, max_length=2_000_000)
    mime_type: str | None = Field(default=None, max_length=64)
    idempotency_key: str = Field(pattern=TURN_ID_PATTERN)
    delivery_mode: Literal["record_only", "live"] = "record_only"
    # "handoff" e proposito proprio: e a ultima fala legitima do bot, dita
    # justamente porque a automacao esta saindo de cena. Marcar como "reply"
    # fazia o guarda de entrega descartar a mensagem que avisa o cliente.
    purpose: Literal["reply", "follow_up", "handoff"] = "reply"
    follow_up: FollowUpDeliveryContext | None = None
    # Only the ``send_template`` action carries this; every other action must
    # leave it None (the outbox dispatch would otherwise have two sources of
    # truth for the same message).
    template: TemplateCommand | None = None

    @model_validator(mode="after")
    def template_matches_action(self) -> Self:
        if self.action == "send_template" and self.template is None:
            raise ValueError("send_template requires a template reference")
        if self.action != "send_template" and self.template is not None:
            raise ValueError("only send_template may carry a template reference")
        return self

    @model_validator(mode="after")
    def follow_up_context_matches_purpose(self) -> Self:
        if self.purpose == "follow_up" and self.follow_up is None:
            raise ValueError("follow-up delivery requires trusted origin context")
        if (
            self.purpose == "follow_up"
            and self.follow_up is not None
            and self.follow_up.phone != normalize_phone(self.destination_jid)
        ):
            raise ValueError("follow-up phone must match command destination")
        if self.purpose != "follow_up" and self.follow_up is not None:
            raise ValueError("normal delivery must not carry follow-up context")
        return self


class AgentDecision(_InternalModel):
    """Structured output expected from the legacy agent adapter."""

    intent: str = Field(min_length=1)
    action: str = Field(min_length=1)
    tool: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    customer_status: Literal["open", "win", "loss", "block"]
    needs_human: bool
    # So vale com needs_human: a marcacao de equipe criada por esta decisao
    # expira (crew_in_at=now(); o crew_in_sweep a devolve ao bot depois de
    # CREW_IN_AUTO_RELEASE_MINUTES sem ato da equipe).  False = marcacao sem
    # prazo (crew_in_at NULL), que so sai por clique humano.  08/out/2026.
    crew_expires: bool = False
    memory_updates: dict[str, Any] = Field(default_factory=dict)
    draft_text: str | None = Field(default=None, max_length=2_048)
    llm_model: str | None = Field(default=None, max_length=128)


class TriageResolution(_InternalModel):
    """Resposta do Manager de triagem (JSON do modelo forte).

    Contrato fechado (``extra="forbid"``): qualquer campo a mais ou JSON
    inválido reprova e a triagem cai no caminho fail-closed (equipe). Só
    ``resolvido`` é obrigatório; ``resposta_paciente`` vazia com
    ``resolvido=true`` não resolve (nada a enviar) e também escala.
    """

    resolvido: bool
    resposta_paciente: str = Field(default="", max_length=2_048)
    resumo_para_equipe: str = Field(default="", max_length=200)
