# Standby

App de **contingência** do Genius Next: FastAPI + Procrastinate, rodando no **Render** com banco no
**Neon**. É a alternativa pronta para subir se o agente principal (`genius-next.service`, VPS 002) cair.
Hoje roda em `record_only`: a resposta é gravada no `outbox` e **nada vai ao WhatsApp**.

> **Este repositório é público.** Nada de segredo, prompt de tenant, catálogo, preço, conversa ou telefone
> de cliente (`docs/TRAPS.md` #20). Segredos vivem no Render e no cofre cifrado do Neon.

## Onde roda

| Peça | Valor |
|---|---|
| Hospedagem | Render, serviço `standby` (plano free, dorme após ~15 min; o Actions acorda o worker) |
| Banco | Neon, projeto `Next`, `aws-sa-east-1` (esquema `genius_next` + tabelas `procrastinate_*`) |
| CI e vigia | GitHub Actions: `monitor.yml`, `wake.yml`, `neon-preview-deploy.yml` |
| Segredos | variáveis do Render + cofre `standby_vault` no Neon (`scripts/vault.py`) |
| Onde o repositório existe | **só no container local e no GitHub; nunca numa VPS** |

URL de saúde: `https://genius-next-serverless.onrender.com/health`. O repositório e o serviço se chamam
**Standby**; o subdomínio vem do nome antigo e não pode ser alterado num serviço existente (`docs/TRAPS.md` #12).

## Relação com o Genius Next

O Next (`geniu5-ai/Genius-Next`, privado) é a produção. O Standby é uma cópia de **mesmo código e mesmas regras**
com **infraestrutura própria**. Prompts e catálogo são dados do Postgres (chegarão ao Neon por sincronização do
banco, a construir), não arquivos deste repositório. O que é igual, o que é separado e como conferir:
[`docs/PARIDADE.md`](docs/PARIDADE.md).

## Mapa de pastas

| Pasta | O que tem |
|---|---|
| `app/` | o aplicativo. Quase todo idêntico ao do Next (106 de 111 arquivos em 03/nov); `app/wake.py` só existe aqui |
| `app/durable/` | fluxo do turno, follow-up, outbox, Manager e relógios (Procrastinate) |
| `app/reply/`, `app/agent/` | agente de resposta (Pydantic AI), guardrails, lições |
| `app/status/`, `app/state/` | status da conversa, estado e fatos |
| `app/ingress/`, `app/webapp/` | webhooks (Evolution, Zernio) e rotas do painel |
| `migrations/` | só `090_procrastinate.sql`; o restante do esquema veio de dump do Next |
| `scripts/` | `testar_webhook.sh`, `wake_check.py`, `vault.py` (cofre), `backup_repo.sh`, `sync_agents.py` |
| `tests/` | suíte sem rede e sem banco (modelo falso, guardrails, cofre, wake) |
| `docs/` | documentação; índice em [`docs/INDEX.md`](docs/INDEX.md) |
| `.github/workflows/` | `monitor.yml` (saúde, 5 min), `wake.yml` (acorda o worker, 20 min), `neon-preview-deploy.yml` (branch Neon por PR) |

## Como testar

```bash
pytest -q tests                                   # suíte local, sem rede e sem banco
scripts/testar_webhook.sh <instancia> <numero-ficticio> "texto"   # webhook de ponta a ponta (HMAC_SECRET no ambiente)
python scripts/wake_check.py --check-only         # mede se há trabalho vencido (WAKE_DATABASE_URL), não acorda
python scripts/vault.py list                      # cofre (STANDBY_VAULT_URL e STANDBY_VAULT_KEY no ambiente)
```

Sem Postgres de teste, 8 testes de `tests/test_followup_guard.py` falham por senha do banco (os mesmos que
falham no Next). Nunca use número real de WhatsApp como cliente de teste.

## Documentação

| Documento | Para quê |
|---|---|
| [`docs/INDEX.md`](docs/INDEX.md) | índice de toda a documentação, marcando o que é canônico e o que é histórico |
| [`docs/HANDOFF.md`](docs/HANDOFF.md) | estado atual e por onde começar uma sessão fria |
| [`docs/OPERACAO.md`](docs/OPERACAO.md) | onde está, variáveis, credenciais, cofre, como testar, pendências |
| [`docs/WAKE.md`](docs/WAKE.md) | por que o worker dorme no Render free e como o QStash o acorda (o Actions é reserva manual) |
| [`docs/TRAPS.md`](docs/TRAPS.md) | armadilhas já encontradas: sintoma, causa, correção e regra |
| [`docs/PARIDADE.md`](docs/PARIDADE.md) | paridade com o Genius Next (igual × separado), checagem por hash de `app/` |
| [`docs/COFRE-IDEIAS.md`](docs/COFRE-IDEIAS.md) | as seis ideias do cofre, decisões e alternativas avaliadas |
| [`docs/GENIUS-DEV-AGENTS.md`](docs/GENIUS-DEV-AGENTS.md) | cópia de referência das regras da casa do Genius Dev (fonte: VPS 003) |
| [`AGENTS.md`](AGENTS.md) | regras para qualquer agente aqui: CEO técnico, **autonomia total** com três travas, crew, segredos, git (o `CLAUDE.md` é cópia gerada dele) |
