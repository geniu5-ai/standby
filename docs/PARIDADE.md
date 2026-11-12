<!-- Este arquivo é IDÊNTICO nos dois repositórios (Genius-Next e Standby). Edite um e copie para o outro na mesma entrega.
     Conferir: sha1sum docs/PARIDADE.md nos dois clones. Repositório do Standby é PÚBLICO: nada de prompt, preço, conversa ou telefone aqui. -->
# Paridade entre o Genius Next e o Standby

Fonte única sobre **o que é igual** e **o que é separado** entre os dois repositórios. Outros
documentos (README, HANDOFF, OPERACAO) apontam para cá em vez de repetir.

| | Genius Next | Standby |
|---|---|---|
| Repositório | `geniu5-ai/Genius-Next` (**privado**) | `geniu5-ai/standby` (**público**) |
| Papel | agente de WhatsApp "Júlia" **em produção** | **contingência**: pronto para assumir se o Next cair |
| Onde roda | VPS 002: systemd (`genius-next.service`), cloudflared, roteador de LLM local | Render (serviço `standby`) |
| Banco | Postgres na própria VPS | Neon (projeto `Next`, `aws-sa-east-1`) |
| Entrega ao WhatsApp | `live` | `record_only` (grava no `outbox`, não envia) |

## Regra de ouro (chairman, 03/nov/2026)

1. **Igual nos dois:** prompts, regras e alguns docs. **Diferente:** a infraestrutura.
2. **O Standby só existe no container local e no GitHub.** Nunca clonar o Standby numa VPS.
3. **O Standby é público.** Nunca copiar para ele prompt de tenant, catálogo, preço, conversa ou telefone
   (`TRAPS` #20 do Standby). O que o Standby precisa saber de uma mudança de prompt ou catálogo entra como
   **nota genérica**, não como o texto.
4. **Tudo o que muda em código no Next é espelhado no Standby na mesma entrega** (PR espelho, mesmo patch).

## O que é igual, e como chega ao Standby

| O quê | Onde mora | Como chega ao Standby |
|---|---|---|
| **Código de `app/`**: status (`app/status/`), follow-up (`app/durable/followup.py`), guardrails (`app/reply/guardrails.py`, `price_guard.py`, `app/security/`), Manager, estado e fatos (`app/state/`), agente (`app/reply/`, `app/agent/`) | git | **PR espelho** aberto na mesma entrega e mesclado junto. Enquanto o arquivo for idêntico, o mesmo patch vale nos dois |
| **Testes** sem dado real (`tests/test_followup_*.py`, `test_guardrails.py` e helpers) | git | cópia no PR espelho. Testes que leem o banco de produção ou o prompt de um tenant **não** vão |
| **Prompts** de tenant (`reply_normal`, `persuasao`, `followup`, `manager_triage`) | Postgres do Next (`tenant_prompt_profiles`) | **Sincronização em tempo real do banco Next → Neon. LIGADA em 12/nov/2026 (`PG_NEON_SYNC=on`, 6 tabelas de configuração; conversas e pacientes ainda NÃO entram) (`docs/specs/SYNC-PG-NEON.md`; worker só no Next, `app/sync/pg_neon.py`).** Até lá, só por dump **posterior** à última mudança, conferindo as versões ativas (`TRAPS` #18 do Standby). Nunca por arquivo do repositório |
| **Catálogo** (`knowledge`, campanhas, preços) | Postgres do Next | idem prompts |
| **Regras de negócio** (leis do cliente, parada de follow-up por objeção e despedida, teto por contato, status) | código, não prompt | são código de `app/`: seguem o PR espelho |
| **Docs** de regra, doutrina e armadilhas **genéricas** | cada repositório tem os seus `docs/HANDOFF.md` e `docs/TRAPS.md` (numerações próprias, não são cópia um do outro) | entra no Standby como nota genérica depois de revisar dado de cliente. Hoje o único doc idêntico é **este** |
| **Migrations de banco** | `migrations/` | `migrations/091_campaigns.sql` (tabela `campaigns`, do módulo de campanhas) vai **nos dois bancos**, cada um por seu caminho (Next: à mão na 002; Standby: pelo Neon). `migrations/090_procrastinate.sql` é idêntica. O restante do esquema `genius_next` do Neon veio de dump e já divergiu uma vez (`TRAPS` #2 do Standby): conferir o esquema antes de promover |

## O que é separado (infraestrutura)

| Tema | Genius Next (VPS 002) | Standby (Render + Neon) |
|---|---|---|
| Processo e deploy | systemd, `deploy/install.sh`, `deploy/apply_migrations.sh` | deploy automático do Render a cada push na `main` |
| Fila e relógios | Procrastinate no Postgres local | Procrastinate no Neon; o worker **dorme** no Render free e o Actions o acorda (`wake.yml`, `docs/WAKE.md` do Standby) |
| Entrada pública | cloudflared (inventário no runbook do Next) | URL do Render (o subdomínio antigo `genius-next-serverless.onrender.com` não muda, `TRAPS` #12 do Standby) |
| LLM | roteador local em `127.0.0.1` (runbook do roteador no Next) | Omni `gpt-pool`. **O env do Next não serve aqui** (`TRAPS` #19 do Standby) |
| Segredos | `/etc/genius-next.env` e `tokens.local` (só leitura) | variáveis no Render + **cofre cifrado no Neon** (`scripts/vault.py`) |
| CI e vigia | sem GitHub Actions; vigias por systemd (`deploy/ops/`) | `monitor.yml`, `wake.yml`, `neon-preview-deploy.yml` |
| Embeddings | sidecar local (`app/embed/`, `genius-embed.service`) | não existe (lições e status audit rodam sem esse contexto) |
| Curadoria de lições | `app/curator/` | não existe |
| Backup | scripts em `deploy/ops/` | `git bundle` no R2 (`scripts/backup_repo.sh`) + `vault.py export` |
| Tenants | reais (Kyrios em produção) e o de teste `genius` | só um tenant fictício de teste |

**Env nunca é copiado** de um lado para o outro. Variável que aponta para `127.0.0.1` ou IP da tailnet não vale
no Render.

## Como cada coisa chega ao Standby (fluxo)

```
código    Next (PR) ──espelho──▶ Standby (PR)  ── mesclados juntos ──▶ Render redeploya
prompts   Postgres do Next ──worker de sync (construído, não ligado)──▶ Neon       [nunca por arquivo]
catálogo  Postgres do Next ──worker de sync (construído, não ligado)──▶ Neon       [nunca por arquivo]
env       cada lado tem o seu                                                [nunca copiado]
```

## Checar a paridade de código (hash de `app/`)

Os dois clones lado a lado no container (`./Genius-Next` e `./standby`). Não precisa de acesso às VPS.

```bash
for r in Genius-Next standby; do
  (cd "$r" && git ls-files app | grep -v __pycache__ | xargs sha1sum | awk '{print $2" "$1}' | sort > "/tmp/$r.sha")
done
# arquivos diferentes, só de um lado ou só do outro (a lista esperada está na tabela abaixo)
join -a1 -a2 -e AUSENTE -o 0,1.2,2.2 /tmp/Genius-Next.sha /tmp/standby.sha | awk '$2!=$3'
# quantos são idênticos
join /tmp/Genius-Next.sha /tmp/standby.sha | awk '$2==$3' | wc -l
```

Colunas da saída: caminho, hash no Next, hash no Standby (`AUSENTE` = não existe naquele lado). Quem muda
`app/` mede antes de abrir o PR espelho e atualiza a tabela abaixo se a lista esperada mudar. O hash
inclui os `README.md` dentro de `app/`: editar um só de um lado conta como divergência.

**Medição de 03/nov/2026** (clones do container: Next `claude/epic-bell-07rm8q`, Standby
`claude/paridade-next-03nov`): 119 arquivos de `app/` no Next, 111 no Standby, **106 idênticos**.

| Situação | Arquivos | Status |
|---|---|---|
| Diferentes | `app/main.py`, `app/observability.py`, `app/status/audit.py`, `app/webapp/router.py` | **ainda não analisados um a um**: investigar antes de dizer "paridade total" |
| Só no Next | `app/curator/*`, `app/embed/main.py` | diferença deliberada (sem sidecar nem curadoria no Render) |
| Só no Standby | `app/wake.py`, `app/qstash_auth.py`, `app/failover.py`, `app/ingress/connector.py`, `app/durable/connector_relay.py`, `connectors/wa-bridge/` | diferença deliberada (acordam o worker no Render free: QStash; vigia da VPS 002 em modo alerta; conector de gateway) |
| Idênticos e sensíveis a mudança | `app/durable/followup.py`, `app/status/rules.py`, `app/reply/llm.py`, `app/reply/prompt_profiles.py` | o patch do follow-up de 03/nov entrou nos quatro |

Paridade de **comportamento** (não só de arquivo) ainda por comparar, módulo a módulo: classificação de status,
lições (busca e extração), embeddings, o juiz de qualidade e os demais módulos de `app/`.

## Registro de espelhamentos

| Data | O que | PR do Next | Notas |
|---|---|---|---|
| 03/nov/2026 | Follow-up: objeção (distância, custo, tempo) e despedida pausam o ciclo; teto de mensagens por contato (3 em 24 h, 5 em 7 dias, `FOLLOWUP_MAX_PER_PHONE_7D`); sem escassez obrigatória na tentativa 2; `data_atual` no contexto. Arquivos: `app/status/rules.py`, `app/durable/followup.py`, `app/durable/flow.py`, `app/reply/llm.py`, `app/settings.py` | `Genius-Next#74` | testes em `tests/test_followup_objecao.py` (106) + helpers; sem Postgres, 8 testes de `test_followup_guard.py` falham por senha do banco de teste, os mesmos que falham no Next |
| 03/nov/2026 | Manager: a rota densa tenta uma vez o modelo do turno quando o de raciocínio falha (`app/reply/tools/reasoning.py`); ao devolver a conversa da equipe ao bot, aborda na hora quem ficou sem resposta (`app/durable/pendentes.py`, `app/durable/manager_tick.py`, só tenants de `MANAGER_PENDENTES_TENANTS`) | `Genius-Next#74` | testes de `test_pendentes.py`, `test_manager_agenda.py` e `test_manager_agent.py` **não** foram copiados (o Standby não os tem) |

| 03/nov/2026 | **Campanhas como módulo dinâmico:** tabela `genius_next.campaigns` (`vale_de`/`vale_ate`), `app/context/campaigns.py`, bloco `campanhas_vigentes` no contexto do atendimento e do follow-up, catálogo ignora `promo:*` (`app/context/knowledge.py`, `app/reply/llm.py`, `app/reply/agent.py`, `app/durable/followup.py`, `migrations/091_campaigns.sql`) | Genius-Next#74 | `tests/test_campanhas.py`: no Standby 8 testes ficam pulados (dependem de helpers e de docs que só existem no Next). Runbook, seed e prompts v24/v8/v8 ficam só no Next |

| 03/nov/2026 | **Sync de configuração Postgres → Neon** (`app/sync/pg_neon.py`, `migrations/092_sync_outbox.sql`, `deploy/systemd/genius-pg-neon-sync.service`) | Genius-Next#74 | **Só no Next** (roda na 002; infra separada). No Neon entram apenas a role `sync_rw` e o esquema igual (incl. `091_campaigns.sql`); `REVOKE` de escrita do app do Standby nessas tabelas e `setval` das sequences antes de promover (`TRAPS.md` #100 do Next) |

Linha nova a cada espelhamento. O detalhe da armadilha fica no `TRAPS.md` do lado em que ela apareceu.

## Antes de promover o Standby para `live`

Prompt e catálogo no Neon são os de um dump: confira as versões ativas e as campanhas contra o Next (`TRAPS` #18),
o env próprio do Render (`TRAPS` #19) e os critérios do failover (`docs/OPERACAO.md` e `docs/WAKE.md` do Standby).
Ligar o `live` é decisão do chairman.
