"""Lazy, validated runtime settings for Genius-Next."""

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.identity_primitives import normalize_phone


def _legado(nome: str) -> AliasChoices:
    """Aceita o nome antigo da variavel de ambiente (PROACTIVE_*, antes do rename
    para Manager em 20/out/2026): o /etc/genius-next.env de producao ainda usa os
    nomes antigos, e trocar o nome nao pode desligar o recurso no deploy."""
    return AliasChoices(nome, nome.replace("MANAGER_", "PROACTIVE_", 1))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = Field(...)
    # PROMPT_DATABASE_URL: Postgres dedicado para tenant_prompt_profiles
    # (historico: separado quando o Neon estourou cota em 29/ago/2026; o Neon
    # ja foi descomissionado). None = usa DATABASE_URL.
    PROMPT_DATABASE_URL: str | None = None
    HMAC_SECRET: str = Field(...)
    # Separate internal control-plane key.  It deliberately never falls back
    # to HMAC_SECRET, which authenticates Evolution ingress.
    CONTROL_HMAC_SECRET: SecretStr | None = None
    CONTROL_HMAC_MAX_AGE_SECONDS: int = Field(default=300, ge=30, le=900)
    # Wake agendado pelo QStash (Upstash): chaves de assinatura atual/proxima (console do QStash)
    # e a URL do destino do schedule, conferida contra o ``sub`` do JWT. Sem a chave atual a
    # rota /v1/control/qstash-wake responde 503 (fail-closed).
    QSTASH_CURRENT_SIGNING_KEY: SecretStr | None = None
    QSTASH_NEXT_SIGNING_KEY: SecretStr | None = None
    QSTASH_WAKE_URL: str = ""
    # Vigia da VPS 002 em modo alerta (app/failover.py). Todas vazias por padrao: sem
    # FAILOVER_PROBE_URL ou sem a chave do QStash a rota /v1/control/qstash-failover responde 503.
    QSTASH_FAILOVER_URL: str = ""
    FAILOVER_PROBE_URL: str = ""
    FAILOVER_ALERT_URL: SecretStr | None = None
    FAILOVER_ALERT_COOLDOWN_S: int = Field(default=1800, ge=60, le=86400)
    # Segundo gatilho do vigia: webhook de saida do Better Stack (incidente aberto). Autenticado por
    # segredo compartilhado no cabecalho X-Webhook-Secret; BETTERSTACK_WATCHED_MONITORS lista os ids
    # dos monitores da VPS 002 (separados por virgula). Sem o segredo a rota responde 503.
    BETTERSTACK_WEBHOOK_SECRET: SecretStr | None = None
    BETTERSTACK_WATCHED_MONITORS: str = ""
    # Tempo sem ato da equipe ate a marcacao de dono humano (automation_state
    # 'crew', o "Humano" do App, a "equipe" do chairman) voltar sozinha para o
    # bot (open).  Ato da equipe = a marcacao (handoff automatico com prazo ou
    # clique Assumir), a ultima mensagem humana da equipe naquele contato ou
    # um envio pelo App, que renovam o prazo.  Paciente esperando resposta
    # segura a marcacao (regra completa: _CREW_RELEASE_ELIGIBLE em
    # app/durable/persistence.py).  Ordem do chairman (08/out/2026): 4,5 min
    # (4min30s), para a conversa ja estar open quando a 1a tentativa do
    # follow-up (5 min) chegar.  O sweep roda a cada 20 s e o proprio
    # follow-up devolve a marcacao vencida na hora do disparo.  Aceita fracao
    # (4.5, com ponto; "4,5" derruba a subida).  Com 5 ou mais a marcacao ainda
    # vale aos 5:00 e o follow-up depois de um handoff automatico para: o
    # startup loga ERRO (crew_in_sweep.log_crew_release_config).
    CREW_IN_AUTO_RELEASE_MINUTES: float = Field(
        default=4.5, ge=1, le=1440, allow_inf_nan=False
    )
    # Lista de tenant_id (virgula) onde o handoff automatico (modelo, promocao,
    # guarda de saida) cria marcacao COM prazo e agenda o follow-up -- a lei de
    # 08/out/2026 (ordem do chairman para a clínica).  Vazio = desligado: fora
    # da lista o handoff automatico fica com a equipe ate alguem agir e nao
    # agenda follow-up, como antes.  A rota fixa de seguranca (emergencia,
    # medicamento) nunca expira, em nenhum tenant.  O clique Assumir expira em
    # todos (ja expirava antes).  So texto de configuracao, NAO SecretStr.
    HANDOFF_AUTO_RELEASE_TENANTS: str = ""
    # Janela em que o aviso de Assumir ("A partir de agora, ... vai continuar
    # seu atendimento") NAO se repete: se nesse intervalo ja saiu um aviso de
    # Assumir ou a equipe ja mandou mensagem humana ao contato, a equipe ja
    # esta conversando e o aviso seria gafe (08/out/2026: pacientes receberam
    # o aviso 2-3 vezes no meio da conversa). 0 = avisa em todo Assumir.
    CREW_IN_DISCLOSURE_QUIET_MINUTES: int = Field(default=120, ge=0, le=1440)
    EVOLUTION_URL: str | None = None
    EVOLUTION_API_KEY: SecretStr | None = None
    # Zernio (WhatsApp Cloud API via inbox): webhook assinado por HMAC-SHA256 e
    # API de envio. Ambos ausentes = o canal Zernio falha FECHADO (webhook 503,
    # envio recusado); nenhum dos dois e exigido para o processo subir.
    ZERNIO_WEBHOOK_SECRET: SecretStr | None = None
    # Conector externo (OpenWA, waxum; connectors/wa-bridge): segredo PROPRIO da rota
    # /webhook/connector. Nunca cai para HMAC_SECRET (Evolution): sem ele a rota responde 503.
    CONNECTOR_HMAC_SECRET: SecretStr | None = None
    # Saida pelo conector SEM sair do record_only: instancias (separadas por virgula) cujas respostas
    # gravadas o conector pode buscar em /v1/connector/outbox e entregar pelo gateway. Vazia = nada
    # sai. Cada resposta so vale ate CONNECTOR_RELAY_MAX_AGE_SECONDS depois de criada.
    CONNECTOR_RELAY_INSTANCES: str = ""
    CONNECTOR_RELAY_MAX_AGE_SECONDS: int = Field(default=900, ge=60, le=3600)
    ZERNIO_API_KEY: SecretStr | None = None
    ZERNIO_API_URL: str = "https://zernio.com/api"
    # Instancia Evolution existente usada so como contingencia do aviso de
    # handoff ao gestor quando o Zernio recusa (a Cloud API so aceita texto
    # livre ate 24h depois da ultima mensagem DO gestor). Opcional.
    # Quem "assina" o recado ao owner (16/out/2026, chairman): "Oi, aqui é o <nome>." Vazio = "Oi!".
    HANDOFF_NOTICE_SENDER: str | None = Field(default=None, max_length=80)
    HANDOFF_FALLBACK_EVOLUTION_INSTANCE: str | None = Field(
        default=None, min_length=1, max_length=128
    )
    # Exa (web search) — opt-in por tenant via flag `web_search` no
    # tenant_registry (migration-069). Cada busca = ~3k tokens de entrada +
    # ida ao provedor; so para perguntas que exigem dado externo.
    EXA_API_KEY: SecretStr | None = None
    # OmniRoute (omni.geniuslabs.pro, VPS001) fronts the reply/classify/follow-up
    # LLM calls behind a server-side priority fallback chain (combo "gpt-pool",
    # só provedores grátis confiáveis), so a single provider outage never turns
    # into a stranded conversation. 21/out/2026: o antigo 9router.geniuslabs.pro
    # morreu (404) e saiu do default; o valor real vem de /etc/genius-next.env.
    OMNIROUTE_API_KEY: SecretStr | None = None
    OMNIROUTE_BASE_URL: str = "https://omni.geniuslabs.pro/v1"
    OMNIROUTE_MODEL: str = "gpt-pool"
    # Juiz por turno (app/reply/turn_judge.py). Assincrono, amostrado, desligado por padrao.
    TURN_JUDGE_ENABLED: bool = False
    TURN_JUDGE_SAMPLE_RATE: float = Field(default=0.2, ge=0.0, le=1.0)
    TURN_JUDGE_THRESHOLD: float = Field(default=0.7, ge=0.0, le=1.0)
    TURN_JUDGE_MODEL: str = "gpt-pool"
    # Lista explicita (separada por virgula). Vazio = nenhum tenant, mesmo com TURN_JUDGE_ENABLED.
    TURN_JUDGE_TENANTS: str = ""
    # Licoes aprovadas no contexto do agente (app/learning). Vazio = nenhum tenant (nunca "todos").
    LESSONS_IN_AGENT_TENANTS: str = ""
    # Auto-aprendizado (app/learning/). Tudo nasce desligado; ligar e decisao humana.
    # LEARNING_ENABLED: o tick e a rodada so agem com isto ligado.
    # LEARNING_DRY_RUN: com True a rodada reflete e audita, mas nao grava insight.
    LEARNING_ENABLED: bool = False
    LEARNING_DRY_RUN: bool = True
    LEARNING_TENANTS: str = ""  # lista separada por virgula; vazio = nenhum tenant (nunca "todos")
    LEARNING_WINDOW_HOURS: int = Field(default=24, ge=1, le=24 * 14)
    LEARNING_MAX_OUTCOMES: int = Field(default=60, ge=5, le=300)
    LEARNING_MIN_OUTCOMES: int = Field(default=8, ge=1, le=100)
    LEARNING_MODEL: str = "gpt-pool"
    LEARNING_TIMEOUT_SECONDS: float = Field(default=90.0, gt=0, le=300.0)
    LEARNING_MAX_TOKENS: int = Field(default=4096, ge=256, le=16384)
    CONFIDENT_API_KEY: str | None = None
    # Dedicated status-audit LLM endpoint.  When absent, the audit falls back
    # to the OmniRoute settings above (never a hardcoded provider/model).
    # Auditoria de status pos-entrega: uma chamada de modelo por turno, fora do
    # caminho da resposta. Desligada em 26/ago/2026 — estava falhando em loop
    # (parametro NULL sem tipo em jsonb_build_object) e custando uma inferencia
    # por turno sem entregar nada. Codigo fica; volta com uma linha no env.
    STATUS_AUDIT_ENABLED: bool = True
    # 28/ago/2026: turno cujas mensagens do cliente sao 100% triviais
    # (agradecimento, risada, emoji-so, confirmacao curta) nao gera resposta --
    # nem chega ao agente. Desliga com uma linha no env se incomodar em uso real.
    SUPPRESS_TRIVIAL_TURNS_ENABLED: bool = True
    # 20/out/2026 (docs/specs/status-no-agente.md): o agente principal da um
    # voto de status junto da resposta. SHADOW = so grava o voto em
    # status_audits.evidence, sem mudar nada; VOTE = o voto entra na decisao do
    # status_workflow (implica SHADOW). Padrao vazio = desligado; "*" = todos.
    # Ligar exige a migration 081 aplicada (coluna status_audits.evidence).
    STATUS_AGENT_SHADOW_TENANTS: str = ""
    STATUS_AGENT_VOTE_TENANTS: str = ""
    # 23/out/2026: a Julia informa FATOS da conversa (sombra, so grava).
    FATOS_SHADOW_TENANTS: str = ""
    # 24/out/2026: fatos derivados por REGRA a partir da fala, da resposta e da acao (sem LLM, sem tocar a Julia).
    FATOS_REGRA_TENANTS: str = ""
    # 23/out/2026: a Julia enxerga as ferramentas da agenda do Genius (horarios reais e reserva).
    AGENDA_TOOL_TENANTS: str = ""
    # TASKS #3 — rota fixa de segurança (vertical clínica): lista de tenant_id
    # (vírgula) onde a detecção determinística de emergência médica e pergunta
    # de medicamento/dose fica ATIVA. Padrão vazio = desligado para todos —
    # tenants fora da lista mantêm o comportamento atual intacto. É só texto
    # de configuração, NÃO SecretStr (não é segredo).
    SAFETY_ROUTE_TENANTS: str = ""
    # Promocao deterministica send_text -> handoff quando a resposta do modelo
    # declara que a equipe vai confirmar/retornar/assumir (app/reply/
    # handoff_promotion.py). Lista de tenant_id (virgula); vazio = desligado.
    # So texto de configuracao, NAO SecretStr.
    HANDOFF_PROMOTE_TENANTS: str = ""
    # Ordem da venda (acolher, engajar, depois o preco): travas no guardrail, por tenant.
    ORDEM_DA_VENDA_TENANTS: str = ""
    # --- Manager (task #8801, branch feat/agente-proativo) ----------
    # Tudo abaixo e' DESLIGADO por padrao: lista vazia = nenhum tenant roda
    # promessa duravel (A), rota de raciocinio (B), follow-up de assistencia (C)
    # nem captura de indicacao (D). So texto de configuracao, NAO SecretStr.
    # A tabela necessaria nasce na migration 076 (genius_next.manager_items
    # e genius_next.manager_usage); codigo lido sem a migration so funciona
    # com a flag vazia.
    MANAGER_AGENT_TENANTS: str = Field(default="", validation_alias=_legado("MANAGER_AGENT_TENANTS"))
    # Modelo da rota densa (B). Mesmo provedor do turno (OMNIROUTE_BASE_URL),
    # so um degrau acima do flash — decisao reversivel de uma linha de env.
    MANAGER_REASONING_MODEL: str = Field(default="gpt-pool", validation_alias=_legado("MANAGER_REASONING_MODEL"))
    MANAGER_REASONING_TIMEOUT_SECONDS: float = Field(default=20.0, gt=0, le=60.0, validation_alias=_legado("MANAGER_REASONING_TIMEOUT_SECONDS"))
    # Teto de tokens de SAIDA por chamada densa: e' o teto de custo direto.
    MANAGER_REASONING_MAX_TOKENS: int = Field(default=1_200, ge=128, le=8_192, validation_alias=_legado("MANAGER_REASONING_MAX_TOKENS"))
    # Teto de chamadas densas por turno (ReplyDeps.raciocinios) — cada chamada
    # e' uma ida a mais ao provedor com o prompt inteiro.
    MANAGER_REASONING_MAX_PER_TURN: int = Field(default=1, ge=0, le=4, validation_alias=_legado("MANAGER_REASONING_MAX_PER_TURN"))
    # Teto diario por tenant (genius_next.manager_usage, incremento atomico).
    MANAGER_REASONING_DAILY_MAX: int = Field(default=60, ge=0, le=10_000, validation_alias=_legado("MANAGER_REASONING_DAILY_MAX"))
    # Minutos entre o fim do turno e o vencimento da promessa/assistencia.
    MANAGER_PROMISE_DUE_MINUTES: int = Field(default=5, ge=1, le=1_440, validation_alias=_legado("MANAGER_PROMISE_DUE_MINUTES"))
    MANAGER_ASSIST_DUE_MINUTES: int = Field(default=5, ge=1, le=1_440, validation_alias=_legado("MANAGER_ASSIST_DUE_MINUTES"))
    # --- Triagem proativa antes da equipe (branch feat/triagem-proativa) ------
    # Antes de chamar a equipe, o Manager tenta resolver sozinho. Só
    # texto de configuracao, NAO SecretStr; padrao vazio = desligado (fora da
    # lista NADA muda no turno). Independente de MANAGER_AGENT_TENANTS.
    MANAGER_TRIAGE_TENANTS: str = Field(default="", validation_alias=_legado("MANAGER_TRIAGE_TENANTS"))
    # Agente de pendencias (16/out/2026, chairman): a cada 2 min aborda quem escreveu e ficou sem
    # nenhuma resposta (app/durable/pendentes.py). CSV de tenant_id; vazio = desligado.
    MANAGER_PENDENTES_TENANTS: str = Field(default="", validation_alias=_legado("MANAGER_PENDENTES_TENANTS"))
    # Varredura diaria de follow-up atrasado (17/out/2026, chairman): um wake
    # por dia as 10:00 America/Sao_Paulo revisa TODO follow-up do tenant e, se
    # 20/out/2026 (docs/specs/manager-agenda.md): agenda do Manager no Postgres.
    # Tenant na lista: o follow-up que esgota as tentativas do provedor (o Zernio
    # para na 3a, janela de 24 h da Meta) agenda o loss por silencio em
    # manager_items, e o tick unico (app/durable/manager_tick.py) o aplica quando
    # vence. Vazio = desligado: o tick nem e agendado.
    MANAGER_AGENDA_TENANTS: str = ""
    # 30/out/2026: "error trigger" do turno (estilo n8n). Turno que quebra nao deixa
    # o paciente sem nada: sai UMA frase de espera (sem LLM, escolhida pelo assunto,
    # nome e variacao estavel). A equipe da clinica nao e avisada; o aviso vai ao
    # celular do operador (OPS_ALERT_TO). Vazio = desligado: mensagem automatica nova
    # ao paciente so com ordem do chairman. TURN_FALLBACK_TEXT, se preenchido,
    # substitui as frases (app/durable/turn_fallback.py).
    TURN_FALLBACK_TENANTS: str = ""
    TURN_FALLBACK_TEXT: str = ""
    # 30/out/2026: compromissos em SOMBRA (migration 089, app/state/compromissos.py):
    # a fala do paciente abre resposta_devida na mesma transacao do evento. Ninguem
    # age pela tabela ainda; o vigia compara com os orfaos. Vazio = desligado.
    COMPROMISSOS_SHADOW_TENANTS: str = ""
    # Horas de silencio depois da ultima tentativa do provedor ate o loss.
    MANAGER_LOSS_SILENCIO_HORAS: int = Field(default=24, ge=1, le=24 * 14)
    # Espera (em SEGUNDOS) entre o texto de espera enviado ao paciente e a
    # tentativa de resolucao do Manager. 0 = tenta na hora; 600 = 10 min.
    MANAGER_TRIAGE_DELAY_SECONDS: int = Field(default=20, ge=0, le=600, validation_alias=_legado("MANAGER_TRIAGE_DELAY_SECONDS"))
    # Tetos PROPRIOS da triagem: a rota de raciocinio da promessa
    # (MANAGER_REASONING_*) continua com os dela. A triagem devolve a
    # resposta COMPLETA ao paciente (valor, preparo, endereco em 1-3 frases),
    # por isso saida maior, e o paciente ja esperou: por isso mais tempo de
    # provedor. Modelo e teto diario seguem compartilhados.
    MANAGER_TRIAGE_MAX_TOKENS: int = Field(default=4_000, ge=256, le=16_000, validation_alias=_legado("MANAGER_TRIAGE_MAX_TOKENS"))
    MANAGER_TRIAGE_TIMEOUT_SECONDS: float = Field(default=45.0, gt=0, le=90, validation_alias=_legado("MANAGER_TRIAGE_TIMEOUT_SECONDS"))
    # Raciocinio do modelo na triagem. Padrao False (medido 15/out: 42/42 e p95 2,2 s contra
    # 36/42 e p95 45 s ligado). False manda {"thinking": {"type":
    # "disabled"}} (TRAPS #45): resposta mais rapida, decisao medida em
    # deploy/ops/medir_triagem.py antes de desligar em producao.
    MANAGER_TRIAGE_THINKING: bool = Field(default=False, validation_alias=_legado("MANAGER_TRIAGE_THINKING"))
    # Temperatura do reply agent. None = default do provedor (comportamento atual).
    # Envio de MODELO (template) da Meta — 18/out/2026. A única mensagem que o
    # WhatsApp aceita fora da janela de 24 h; texto livre ali devolve 131047.
    # Desligado por padrão: ligar exige a flag E o tenant na lista (mesmo
    # desenho de SAFETY_ROUTE_TENANTS / MANAGER_AGENT_TENANTS). Os dois tetos
    # são por contato, somando disparos: 1 modelo a cada 24 h, 3 em 7 dias.
    TEMPLATE_SEND_ENABLED: bool = False
    TEMPLATE_SEND_TENANTS: str = ""
    TEMPLATE_MAX_PER_PHONE_24H: int = Field(default=1, ge=0, le=20)
    TEMPLATE_MAX_PER_PHONE_7D: int = Field(default=3, ge=0, le=50)
    REPLY_AGENT_TEMPERATURE: float | None = Field(default=None, ge=0, le=2)
    STATUS_AUDIT_API_KEY: SecretStr | None = None
    STATUS_AUDIT_BASE_URL: str | None = None
    STATUS_AUDIT_MODEL: str | None = None
    WEBHOOK_MAX_BODY_BYTES: int = Field(default=262_144, ge=1, le=10_485_760)
    # Genius Shield fase 1 (docs/roadmap/FEAT-SECURITY.md): durable token-bucket
    # rate limiting per (tenant_id, phone) on the Evolution webhook.  Bucket
    # state lives in genius_next.rate_limit_buckets; these three knobs are the
    # only tuning surface.  RATE_LIMIT_ENABLED=False disables the check entirely
    # (fail-open), which is also the behavior when the guard table is absent.
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_CAPACITY: int = Field(default=20, ge=1, le=10_000)
    RATE_LIMIT_REFILL_PER_SECOND: float = Field(default=1.0, gt=0, le=1_000)
    TURN_MAX_EVENTS: int = Field(default=100, ge=1, le=100)
    HISTORY_MAX_MESSAGES: int = Field(default=12, ge=1, le=20)
    # Mensagens da equipe (celular/app do Genius) entram no historico da IA marcadas com [equipe].
    HISTORY_INCLUDE_TEAM_MESSAGES: bool = True
    HISTORY_ITEM_MAX_CHARS: int = Field(default=4_096, ge=1, le=4_096)
    HISTORY_TOTAL_MAX_CHARS: int = Field(default=12_000, ge=1, le=12_000)
    HISTORY_RESPONSE_MAX_BYTES: int = Field(default=524_288, ge=1, le=1_048_576)
    HISTORY_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0, le=8.0)
    # Curation ceiling for the paginated first-contact deep-fetch: each page is
    # 20 messages, so the default 10 pages bound the full-history pull at up to
    # 200 messages before the summary is generated.  Configurable, never a
    # hardcoded literal buried in the flow.
    HISTORY_SUMMARY_MAX_PAGES: int = Field(default=10, ge=1, le=50)
    # Guarda local (app/security/guard.py, sem sidecar desde 02/nov/2026):
    # texto invisivel nos dois estagios e segredo na saida.  GUARD_ENABLED
    # desliga as duas sem apagar codigo; OUTPUT_GUARD_ENABLED desliga so a
    # saida.  GUARD_SIDECAR_URL e GUARD_TIMEOUT_SECONDS sairam: no env sao
    # ignoradas (extra="ignore").
    GUARD_ENABLED: bool = True
    OUTPUT_GUARD_ENABLED: bool = True
    REPLY_INPUT_MAX_CHARS: int = Field(default=12_000, ge=1, le=24_000)
    REPLY_RESPONSE_MAX_BYTES: int = Field(default=65_536, ge=1, le=262_144)
    # The router is allowed one bounded retry/failover window. Eight seconds
    # produced routine safe-handoffs before the provider had answered.
    REPLY_TIMEOUT_SECONDS: float = Field(default=20.0, gt=0, le=20.0)
    DELIVERY_RESPONSE_MAX_BYTES: int = Field(default=65_536, ge=1, le=262_144)
    DELIVERY_TIMEOUT_SECONDS: float = Field(default=8.0, gt=0, le=8.0)
    DELIVERY_MODE: Literal["record_only", "live"] = "record_only"
    DELIVERY_ALLOWLIST: str | tuple[str, ...] = ()
    # Alerta operacional no WhatsApp do chairman (15/out/2026): qualquer erro de
    # flow avisa aqui. Numero (so digitos) + instancia Evolution da casa (em
    # producao, `genius-maps`). Sem os dois o alerta vira so log. O destino NAO
    # passa pela DELIVERY_ALLOWLIST (e operacional) e nenhum outro e aceito.
    OPS_ALERT_TO: str | None = None
    OPS_ALERT_EVOLUTION_INSTANCE: str | None = Field(
        default=None, min_length=1, max_length=128
    )
    # Motor de redacao. "legacy" = envelope JSON montado a mao em
    # app/reply/llm.py; "agent" = Pydantic AI com saida tipada
    # (app/reply/agent.py).  Trocar por variavel de ambiente, sem deploy.
    REPLY_ENGINE: Literal["legacy", "agent"] = "agent"
    # REPLY_LAYERS removido 06/set/2026 (ceo-prompt-unico-colapsa-7-camadas): colapsado em prompt unico v31
    # adicionado aqui pra poder isolar o prompt sozinho e investigar a
    # pergunta clinica do dente que sobrevive a 4 versoes de prompt distintas.
    REPLY_LAYERS: str | None = None  # removido 2026-09-06: colapsado em prompt unico v31
    # Janela de juncao: quanto o turno espera por mais mensagens antes de
    # redigir. Paga-se em latencia direta — o cliente sente cada segundo daqui.
    # Baixar demais parte mensagem picada em dois turnos; subir demais faz o
    # bot parecer lento. Era constante fixa em app/durable/flow.py.
    DEBOUNCE_WAIT_SECONDS: float = Field(default=8.0, ge=0, le=30)
    # Teto do catalogo colado no prompt. Acima disso o tenant cai para a
    # lista de chaves + ferramenta: melhor lento que prompt estourado.
    # Passar do teto e o sinal de que aquele tenant precisa da estrategia
    # "selecao" — e por isso o codigo loga em vez de truncar em silencio.
    KNOWLEDGE_INLINE_MAX_CHARS: int = Field(default=12_000, ge=0, le=60_000)
    # Teto de consultas da ferramenta abrir() por turno. Cada consulta e uma
    # ida a mais ao provedor: no modo inline isso nao acontece.
    KNOWLEDGE_MAX_CONSULTAS: int = Field(default=2, ge=0, le=6)
    # Teto de buscas da ferramenta pesquisar_web() por turno (Exa).
    # Cada busca e uma ida ao provedor + ~3k tokens de entrada: 1 por turno.
    WEB_SEARCH_MAX_CONSULTAS: int = Field(default=1, ge=0, le=6)
    # Model string nativa do pydantic-ai.  String, nao instancia.
    # Mesmo valor que roda em producao (/etc/genius-next.env). Sem DeepSeek: 05/nov/2026.
    REPLY_AGENT_MODEL: str = "openai-chat:gpt-4o-mini"
    OUTPUT_MAX_TOKENS: int = Field(default=1600, ge=1, le=4096)
    # Matches the legacy n8n Spliter's proven values (MAX_PARTS=5,
    # TARGET_CHARS=115) -- the previous 220/3 defaults were a guess that
    # left short-but-multi-sentence replies (e.g. a statement + a question,
    # ~150 chars) as a single WhatsApp bubble instead of natural fragments.
    OUTPUT_SPLIT_MAX_BLOCKS: int = Field(default=5, ge=1, le=8)
    OUTPUT_SPLIT_TARGET_CHARS: int = Field(default=115, ge=40, le=1024)
    # Chairman-tuned pacing (2-4s between fragments); legacy n8n Divider
    # node used 3-6s, judged too slow for how the split step reads today.
    OUTPUT_PACING_MIN_SECONDS: float = Field(default=2.0, ge=0, le=60)
    OUTPUT_PACING_MAX_SECONDS: float = Field(default=4.0, ge=0, le=120)
    OUTPUT_PACING_ENABLED: bool = True
    # WhatsApp typing indicator (Evolution ``sendPresence``).  Cosmetic, sempre
    # fire-and-forget: PRESENCE_ENABLED desliga tudo no deploy.  A resposta
    # real pode chegar e cortar o indicador no meio -- e o esperado, nunca
    # se espera o indicador terminar antes de entregar a mensagem.
    # PRESENCE_TIMEOUT_SECONDS limita a chamada desacoplada para uma
    # thread daemon nunca ficar pendurada por um provedor lento.
    PRESENCE_ENABLED: bool = True
    # Desenho do chairman (27/ago/2026): na entrada da mensagem o fio se divide
    # em dois.  Um leva a conversa para o fluxo; o outro espera
    # PRESENCE_PRE_DELAY_SECONDS em silencio e so entao mostra "digitando..."
    # em blocos -- um por valor em PRESENCE_TYPING_SECONDS, cada um com sua
    # propria duracao, com PRESENCE_BURST_GAP_SECONDS de respiro entre eles.
    # Quem digita de verdade nao digita de um folego so: escreve, para pra
    # pensar, escreve de novo -- e nem sempre pelo mesmo tempo.
    #
    # A espera roda dentro da thread daemon do indicador, nunca no caminho da
    # requisicao: o fio da conversa nao espera por ela em nenhum momento.
    PRESENCE_PRE_DELAY_SECONDS: float = Field(default=5.0, ge=0, le=15)
    PRESENCE_TYPING_SECONDS: str = Field(default="5,4")
    PRESENCE_BURST_GAP_SECONDS: float = Field(default=2.0, ge=0, le=10)
    PRESENCE_TIMEOUT_SECONDS: float = Field(default=6.0, gt=0.2, le=6.0)
    # "digitando..." do Zernio (Cloud API), exige tambem PRESENCE_ENABLED.  Chave
    # propria porque a chamada marca a mensagem do paciente como lida (tique
    # azul): ZERNIO_TYPING_ENABLED=false + restart desliga so o Zernio, sem
    # mexer no indicador da Evolution.
    ZERNIO_TYPING_ENABLED: bool = True

    @property
    def presence_typing_seconds(self) -> list[float]:
        """PRESENCE_TYPING_SECONDS parseado: um valor por bloco de digitacao."""
        return [float(v) for v in self.PRESENCE_TYPING_SECONDS.split(",") if v.strip()]

    ELEVENLABS_API_KEY: SecretStr | None = None
    ELEVENLABS_VOICE_ID: str = "cgSgspJ2msm6clMCkdW9"
    ELEVENLABS_MODEL: str = "eleven_v3"
    TTS_PROVIDER: str | None = None
    TTS_INPUT_MAX_CHARS: int = Field(default=4_096, ge=1, le=12_000)
    TTS_TIMEOUT_SECONDS: float = Field(default=8.0, gt=0, le=8.0)
    TTS_RESPONSE_MAX_BYTES: int = Field(default=262_144, ge=1, le=1_048_576)

    GROQ_API_KEY: SecretStr | None = None
    STT_MODEL: str = "whisper-large-v3-turbo"
    # Endpoint OpenAI-compativel de transcricao; padrao Groq direto. Pelo Omni: https://omni.geniuslabs.pro/v1/audio/transcriptions
    STT_URL: str = "https://api.groq.com/openai/v1/audio/transcriptions"
    STT_LANGUAGE: str = "pt"
    COHERE_API_KEY: SecretStr | None = None
    COHERE_STT_URL: str = "https://api.cohere.com/v2/audio/transcriptions"
    STT_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0, le=15.0)
    STT_RESPONSE_MAX_BYTES: int = Field(default=262_144, ge=1, le=1_048_576)

    # Rota de midia (08/out/2026): imagem, PDF/documento, video e audio que o
    # cliente manda viram texto rotulado ("[imagem] ...") em events.text_content.
    # A chave ja existe no env; o lesson_extractor continua lendo os.environ.
    OPENROUTER_API_KEY: SecretStr | None = None
    # Lista de tenant_id (virgula); "*" = todos; vazio = extracao desligada para
    # todos (a midia recebe o fallback honesto). Mesmo parser de SAFETY_ROUTE_TENANTS.
    MEDIA_ROUTE_TENANTS: str = ""
    MEDIA_PROVIDER_URL: str = "https://openrouter.ai/api/v1/chat/completions"
    MEDIA_MODEL: str = "google/gemini-3.1-flash-lite"
    # Modelo/combo so para audio no caminho de chat (reserva do STT); vazio = usa MEDIA_MODEL.
    MEDIA_MODEL_AUDIO: str = ""
    # Chave dedicada opcional; None = usa OPENROUTER_API_KEY.
    MEDIA_API_KEY: SecretStr | None = None
    # Dado de saude: o provedor so pode ser um que nao retem nem treina (ZDR).
    MEDIA_ZDR_REQUIRED: bool = True
    MEDIA_MAX_BYTES_IMAGE: int = Field(default=5_242_880, ge=1, le=16_777_216)
    MEDIA_MAX_BYTES_AUDIO: int = Field(default=16_777_216, ge=1, le=16_777_216)
    MEDIA_MAX_BYTES_VIDEO: int = Field(default=16_777_216, ge=1, le=16_777_216)
    MEDIA_MAX_BYTES_DOCUMENT: int = Field(default=10_485_760, ge=1, le=20_971_520)
    MEDIA_FETCH_TIMEOUT_SECONDS: float = Field(default=15.0, gt=0, le=30)
    MEDIA_EXTRACT_TIMEOUT_SECONDS: float = Field(default=20.0, gt=0, le=60)
    MEDIA_EXTRACT_LONG_TIMEOUT_SECONDS: float = Field(default=45.0, gt=0, le=90)
    MEDIA_MAX_OUTPUT_TOKENS: int = Field(default=700, ge=64, le=2000)
    MEDIA_TEXT_MAX_CHARS: int = Field(default=2000, ge=1, le=3500)
    MEDIA_RESPONSE_MAX_BYTES: int = Field(default=131_072, ge=1, le=1_048_576)
    # Quanto o turno espera pelo resultado da extracao antes do fallback.
    MEDIA_TURN_WAIT_SECONDS: float = Field(default=50.0, ge=0, le=120)
    # Freio de custo: midias extraidas por tenant por dia (America/Sao_Paulo).
    MEDIA_DAILY_MAX_PER_TENANT: int = Field(default=200, ge=0, le=10_000)
    # Evolution: buscar a midia na API (getBase64FromMediaMessage). Desligado:
    # so o base64 que vier no proprio evento e usado.
    MEDIA_EVOLUTION_FETCH_ENABLED: bool = False

    @property
    def media_route_tenants(self) -> frozenset[str]:
        return frozenset(
            tenant.strip() for tenant in self.MEDIA_ROUTE_TENANTS.split(",") if tenant.strip()
        )

    def media_route_enabled(self, tenant_id: str) -> bool:
        tenants = self.media_route_tenants
        return "*" in tenants or tenant_id in tenants

    @staticmethod
    def _tenant_in(lista: str, tenant_id: str) -> bool:
        tenants = {tenant.strip() for tenant in lista.split(",") if tenant.strip()}
        return "*" in tenants or tenant_id in tenants

    def ordem_da_venda_enabled(self, tenant_id: str) -> bool:
        return self._tenant_in(self.ORDEM_DA_VENDA_TENANTS, tenant_id)

    def manager_agenda_enabled(self, tenant_id: str) -> bool:
        return self._tenant_in(self.MANAGER_AGENDA_TENANTS, tenant_id)

    def turn_fallback_enabled(self, tenant_id: str) -> bool:
        return self._tenant_in(self.TURN_FALLBACK_TENANTS, tenant_id)

    def compromissos_shadow_enabled(self, tenant_id: str) -> bool:
        return self._tenant_in(self.COMPROMISSOS_SHADOW_TENANTS, tenant_id)

    def agenda_tool_enabled(self, tenant_id: str) -> bool:
        return self._tenant_in(self.AGENDA_TOOL_TENANTS, tenant_id)

    def fatos_regra_enabled(self, tenant_id: str) -> bool:
        return self._tenant_in(self.FATOS_REGRA_TENANTS, tenant_id)

    def fatos_enabled(self, tenant_id: str) -> bool:
        return self._tenant_in(self.FATOS_SHADOW_TENANTS, tenant_id)

    def status_agent_vote_enabled(self, tenant_id: str) -> bool:
        return self._tenant_in(self.STATUS_AGENT_VOTE_TENANTS, tenant_id)

    def status_agent_enabled(self, tenant_id: str) -> bool:
        """Agente emite o voto de status (sombra ou voto)."""
        return self.status_agent_vote_enabled(tenant_id) or self._tenant_in(
            self.STATUS_AGENT_SHADOW_TENANTS, tenant_id
        )

    @property
    def template_send_tenants(self) -> frozenset[str]:
        return frozenset(
            tenant.strip()
            for tenant in self.TEMPLATE_SEND_TENANTS.split(",")
            if tenant.strip()
        )

    def template_send_enabled(self, tenant_id: str) -> bool:
        """Flag E tenant: os dois precisam liberar o envio de modelo."""
        if not self.TEMPLATE_SEND_ENABLED:
            return False
        tenants = self.template_send_tenants
        return "*" in tenants or tenant_id in tenants

    @field_validator("MEDIA_PROVIDER_URL")
    @classmethod
    def media_provider_url_is_https(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("https://"):
            raise ValueError("MEDIA_PROVIDER_URL must use https")
        return normalized

    @field_validator("OUTPUT_PACING_MAX_SECONDS")
    @classmethod
    def pacing_range_is_ordered(cls, value: float, info: ValidationInfo) -> float:
        minimum = info.data.get("OUTPUT_PACING_MIN_SECONDS")
        if minimum is not None and value < minimum:
            raise ValueError("OUTPUT_PACING_MAX_SECONDS must be >= OUTPUT_PACING_MIN_SECONDS")
        return value
    @field_validator("EVOLUTION_URL")
    @classmethod
    def evolution_url_is_http(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("EVOLUTION_URL must use http or https")
        return normalized

    @field_validator("ZERNIO_API_URL")
    @classmethod
    def zernio_url_is_http(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("ZERNIO_API_URL must use http or https")
        return normalized

    @field_validator("DELIVERY_ALLOWLIST", mode="before")
    @classmethod
    def normalize_delivery_allowlist(cls, value: object) -> tuple[str, ...]:
        if value is None or value == "":
            return ()
        if isinstance(value, (int, float)):
            # pydantic-settings parses bare env numbers (e.g. 558695407492)
            # into int/float before this validator runs.
            candidates = [str(value).split(".")[0]]
        elif isinstance(value, str):
            candidates = value.split(",")
        elif isinstance(value, (list, tuple, set, frozenset)):
            candidates = list(value)
        else:
            raise ValueError("DELIVERY_ALLOWLIST must be a string or list")

        normalized: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, str):
                raise ValueError("DELIVERY_ALLOWLIST entries must be strings")
            item = candidate.strip()
            if not item:
                continue
            if item == "*":
                # Curinga: entrega liberada para qualquer destino.  Ordem do
                # chairman 25/ago/2026 para destravar o teste real de ponta a
                # ponta. ponytail: trocar por allowlist real antes de atender
                # cliente pagante em producao.
                normalized.append(item)
                continue
            jid = item if "@" in item else f"{item}@s.whatsapp.net"
            if jid.endswith("@lid"):
                normalize_phone(jid)  # validate LID shape, no phone to append
                canonical = jid
            else:
                phone = normalize_phone(jid)
                canonical = f"{phone}@s.whatsapp.net"
                if jid != canonical:
                    raise ValueError(
                        "DELIVERY_ALLOWLIST accepts direct WhatsApp JIDs only"
                    )
            if canonical not in normalized:
                normalized.append(canonical)
        return tuple(normalized)

    @property
    def delivery_allowlist_jids(self) -> frozenset[str]:
        return frozenset(self.DELIVERY_ALLOWLIST)

    def validate_runtime_integrations(self) -> None:
        missing = [
            name
            for name in ("EVOLUTION_URL", "EVOLUTION_API_KEY", "OMNIROUTE_API_KEY")
            if getattr(self, name) is None
        ]
        if missing:
            raise ValueError(f"missing required runtime settings: {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load environment settings on first runtime use, never at import time."""
    return Settings()


class _LazySettings:
    """Compatibility proxy for callers that use ``settings.ATTRIBUTE``."""

    def __getattr__(self, name: str):
        return getattr(get_settings(), name)


settings = _LazySettings()
