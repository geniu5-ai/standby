# Índice da documentação do Standby

Uma linha por documento. **Canônico** = vale hoje e deve ser mantido junto com o código. **Histórico** =
registra uma decisão ou um estado passado e não é atualizado. O Standby é **público**: nada de prompt de tenant,
catálogo, preço, conversa ou telefone em nenhum destes arquivos (`TRAPS` #20).

## Comece por aqui

| Documento | Para quê | Situação |
|---|---|---|
| [`../README.md`](../README.md) | porta de entrada: o que é, onde roda, mapa de pastas, como testar | canônico |
| [`HANDOFF.md`](HANDOFF.md) | estado atual e por onde começar uma sessão fria; blocos de estado mais novos valem sobre os antigos | canônico (vivo) |
| [`../AGENTS.md`](../AGENTS.md) | regras para qualquer agente aqui (CEO técnico, autonomia total com três travas). `../CLAUDE.md` é cópia gerada: edite só o `AGENTS.md` e rode `python scripts/sync_agents.py` | canônico |

## Operação

| Documento | Para quê | Situação |
|---|---|---|
| [`OPERACAO.md`](OPERACAO.md) | onde está (Render, Neon), variáveis, segredos, cofre, roles, como testar, wake, backup, pendências | canônico |
| [`WAKE.md`](WAKE.md) | por que o worker dorme no Render free, como o Actions o acorda, alternativas avaliadas, próximos passos (failover) | canônico |
| [`MONITORAMENTO.md`](MONITORAMENTO.md) | o que vigia o quê fora da Oracle: Better Stack (monitores, heartbeats, página de status, limites do plano grátis), QStash, ferramentas avaliadas | canônico |
| [`GATEWAYS-WHATSAPP.md`](GATEWAYS-WHATSAPP.md) | gateways de WhatsApp: comparação, laboratório na VPS 003, resultados medidos, formato dos webhooks, o conector `wa-bridge` e como operar | canônico |
| [`TRAPS.md`](TRAPS.md) | armadilhas já encontradas (sintoma, causa, correção, regra); numeração própria, citada em todo lugar: **não renumerar** | canônico |

## Relação com o Genius Next

| Documento | Para quê | Situação |
|---|---|---|
| [`PARIDADE.md`](PARIDADE.md) | o que é igual e o que é separado entre Next e Standby, como cada coisa chega, checagem de paridade por hash de `app/`. **Idêntico nos dois repositórios** | canônico |

## Decisões e referência

| Documento | Para quê | Situação |
|---|---|---|
| [`DECISOES-INFRA.md`](DECISOES-INFRA.md) | serverless × VPS × contêineres, Cloudflare, orquestração (Procrastinate, Inngest, Hatchet, DBOS, Absurd), modelos de linguagem, continuidade e falhas correlacionadas, com o que não foi verificado | histórico (decisão registrada em 11/nov/2026) |
| [`COFRE-IDEIAS.md`](COFRE-IDEIAS.md) | as seis ideias do cofre de segredos (todas feitas), decisões e alternativas avaliadas | histórico (decisão registrada; o uso atual está em `OPERACAO.md`, "Cofre") |
| [`GENIUS-DEV-AGENTS.md`](GENIUS-DEV-AGENTS.md) | cópia das regras da casa do Genius Dev (fonte: `Genius-Dev/AGENTS.md`, VPS 003). Não editar; onde divergir do `AGENTS.md` daqui, vale o daqui | referência (cópia) |

## Fora de `docs/` que também é documentação

| Arquivo | Para quê |
|---|---|
| [`../app/status/README.md`](../app/status/README.md) | módulo de status; **idêntico ao do Next** (entra no hash de paridade, não editar só aqui) |
| [`../.github/workflows/`](../.github/workflows/) | `monitor.yml` (saúde a cada 5 min), `wake.yml` (acorda o worker a cada 20 min), `neon-preview-deploy.yml` (branch Neon por PR) |
| [`../scripts/`](../scripts/) | `testar_webhook.sh`, `wake_check.py`, `vault.py`, `backup_repo.sh`, `sync_agents.py` |

## Documentos que vivem só no Genius Next (privado)

O Standby aponta para eles pelo nome, não os copia: runbook de ativação de tenant
(`docs/runbooks/RUNBOOK-ATIVACAO-TENANT.md`), runbook do roteador de LLM, doutrina (`docs/core/`), specs, e os
`docs/TRAPS.md` e `docs/HANDOFF.md` do Next (as armadilhas #78 a #82 do Next são deste app).

## Arquivo morto

Nada arquivado ainda. Quando um documento deixar de ser verdade, ele ganha uma nota no topo e passa a constar
aqui como histórico (mover para `docs/archive/` só depois de atualizar os links que o citam).
