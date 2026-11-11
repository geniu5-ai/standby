# Documentação do Standby

App de **contingência** do Genius Next (FastAPI + Procrastinate, Render + Neon). Mesmo padrão do
`HANDOFF.md` do Next: o que está no ar, o que mudou, e por onde começar uma sessão fria. Onde um
bloco de estado mais novo divergir de um texto mais antigo, vale o bloco.

| Documento | Para quê |
|---|---|
| [`../README.md`](../README.md) | porta de entrada e atalhos |
| [`INDEX.md`](INDEX.md) | índice de toda a documentação (canônico × histórico) |
| [`PARIDADE.md`](PARIDADE.md) | o que é igual e o que é separado entre Next e Standby, e a checagem por hash de `app/` |
| [`../AGENTS.md`](../AGENTS.md) | regras para qualquer agente aqui (CEO técnico, **autonomia total** com três travas). `../CLAUDE.md` é cópia gerada dele (`scripts/sync_agents.py`) |
| [`OPERACAO.md`](OPERACAO.md) | onde está, variáveis, segredos, cofre, roles, como testar, pendências |
| [`WAKE.md`](WAKE.md) | por que o worker dorme no Render free e como o Actions o acorda |
| [`COFRE-IDEIAS.md`](COFRE-IDEIAS.md) | as seis ideias do cofre (todas feitas), decisões e alternativas avaliadas |
| [`TRAPS.md`](TRAPS.md) | armadilhas já encontradas (sintoma, causa, correção, regra) |
| [`GENIUS-DEV-AGENTS.md`](GENIUS-DEV-AGENTS.md) | cópia das regras da casa do Genius Dev (fonte: VPS 003) |

> **🧭 QStash (11/nov/2026):** rota `POST /v1/control/qstash-wake` (assinatura da Upstash), `app/qstash_auth.py` e `scripts/qstash_schedule.py` entraram para acordar o worker no lugar do relógio do Actions; o `wake.yml` fica como reserva. Estado da ativação (variáveis do Render, deploy, schedule) e a conta de horas do Render free: [`WAKE.md`](WAKE.md), seção "Wake pelo QStash".

> **🧭 Conector OpenWA (11/nov/2026):** rota `POST /webhook/connector` (segredo próprio `CONNECTOR_HMAC_SECRET`, não o `HMAC_SECRET` da Evolution) e `connectors/wa-bridge/` (stdlib, roda ao lado do gateway, resolve LID em telefone, fila SQLite). Entrega ao fluxo comum como `evolution`, só ingestão em `record_only`: nada sai ao WhatsApp. Laboratório na VPS 003 (Quadlet `lab-*`); detalhes no README do conector.

> **🧭 Paridade com o Next (03/nov/2026, tarde):** o canônico agora é [`PARIDADE.md`](PARIDADE.md) (regra, matriz igual × separado, medição por hash, registro de espelhamentos). Em resumo:
> - **Regra (ordem do chairman):** tudo o que muda no Next é espelhado aqui na mesma entrega (`AGENTS.md`). Repositório público: só código, testes sem dado real, regras e armadilhas genéricas (`TRAPS` #20). O Standby só existe no container local e no GitHub, nunca numa VPS.
> - **Código:** 106 dos 119 arquivos de `app/` do Next são idênticos aos daqui; quatro diferem (`app/main.py`, `app/observability.py`, `app/status/audit.py`, `app/webapp/router.py`) e ainda não foram analisados. O patch do follow-up e o do Manager já estão espelhados neste PR; merge junto com o PR do Next (`Genius-Next#74`).
> - **Dados e ambiente não vêm do código:** prompt e catálogo chegam por sincronização do banco (a construir) ou dump posterior (`TRAPS` #18); o env do Next não serve aqui (`TRAPS` #19).

> **🧭 Estado em 03/nov/2026 (leia primeiro):**
> - **No ar:** serviço `standby` no Render (plano free, dorme após ~15 min), banco no Neon (projeto `Next`, `aws-sa-east-1`). `record_only`: nada vai ao WhatsApp. URL `https://genius-next-serverless.onrender.com` (o subdomínio não muda, `TRAPS` #12).
> - **Wake:** `wake.yml` a cada 20 min consulta o Neon com a role `wake_ro` e só chama `POST /v1/control/wake` quando há trabalho vencido. Verificado de ponta a ponta em 03/nov (`WAKE.md`).
> - **Cofre de segredos:** schema `standby_vault` no Neon; valores cifrados fora do banco (AES-256-GCM, nome como AAD). Acesso só por funções que registram cada leitura; histórico e log são somente de inserção; backup cifrado com `export`; chave em duas partes (`aead2`) para `RENDER_API_KEY` e `NEON_API_KEY`; prazo de rotação de 90 dias (`due`). `python scripts/vault.py` (`OPERACAO.md`, "Cofre"); funciona do sandbox por HTTPS do Neon (`TRAPS` #16), ~0,65 s por consulta depois da 1ª (`TRAPS` #17). Entrada: `STANDBY_VAULT_URL` e `STANDBY_VAULT_KEY` no ambiente da sessão.
> - **Roles do Neon criadas por SQL**, nunca pela API (`TRAPS` #8): `wake_ro` (lê 5 tabelas) e `vault_rw` (só executa as funções do cofre).
> - **Autonomia total (ordem do chairman, 03/nov):** o agente decide, executa e **faz o merge dos PRs do Standby** sem pedir licença; só três travas: mensagem a paciente/cliente (inclui ligar o `live`), destruir o que não tem backup verificado, e dinheiro (`AGENTS.md`).
> - **Rename:** o app era `genius-next-serverless`, agora é Standby (repositório `geniu5-ai/standby`, serviço `standby`). O Genius Dev (VPS 003, DBOS) e os docs do Next citam o nome antigo; o GitHub mantém o redirecionamento.
> - **Abertos:** (1) cadastrar `STANDBY_VAULT_URL`/`STANDBY_VAULT_KEY` no ambiente da sessão e guardar `STANDBY_VAULT_KEY2` **fora** dele (o chairman estava no celular; os valores estão em `vault-env.local`, ignorado pelo git, **não apague antes de ele copiar**, `TRAPS` #10); (2) rotacionar `wake_ro`, `CONTROL_HMAC_SECRET` e o que apareceu em conversa; (3) paridade com o Next (status, lessons, embeds, juiz); (4) failover automático; (5) job `conversation_turn` com `failed` de 02/nov 05:49 UTC ainda não analisado; (6) **rotacionar a `CPFHUB_API_KEY` que está fixa em `app/tools/procuracao.py`** (`TRAPS` #14).
> - **Backup do repositório:** `git bundle` completo no R2 (`Genius-Standby/`, raiz do bucket `genius`), conferido por download; `scripts/backup_repo.sh`. Repetir depois de mudanças grandes; roda do próprio sandbox com `vault.py run R2_* -- scripts/backup_repo.sh --subir` (`OPERACAO.md`, "Backup do repositório"). Última cópia: 03/nov, 30 commits.

## Sessão fria: por onde começar

```bash
curl -sS https://genius-next-serverless.onrender.com/health          # {"status":"ok"} (pode levar ~60 s)
pytest -q tests                                                      # suíte sem rede e sem banco
python scripts/vault.py list                                         # precisa do ambiente do cofre
python scripts/vault.py due                                          # o que pede rotação
python scripts/wake_check.py --check-only                            # mede, não acorda (WAKE_DATABASE_URL)
```

Antes de mexer: leia o bloco de estado acima e as últimas entradas de [`TRAPS.md`](TRAPS.md). Ao
terminar: o que mudou fica escrito no lugar certo, na mesma entrega.
