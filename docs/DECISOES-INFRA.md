# Decisões de infraestrutura: serverless, orquestração e continuidade

Estado em 11/nov/2026. Registro da pesquisa (páginas oficiais lidas na data) e do que foi decidido. Onde um fato
**não estava na documentação**, está marcado "não verificado". Histórico: o que muda de verdade vai para
`OPERACAO.md`, `WAKE.md` e `MONITORAMENTO.md`.

## A carga que pesa nas decisões

Premissa de cálculo (ajustável): 1000 clientes, no pico 1 mensagem por minuto cada, cerca de 5 s por resposta de
LLM, o que dá ~17 mensagens/s e **~85 a 100 chamadas de modelo simultâneas**. É carga pequena para qualquer
host; os gargalos reais são o **limite de concorrência do provedor de modelo**, as **conexões do banco** e o
**tempo de resposta do webhook** (o WhatsApp espera confirmação rápida: gravar em fila e responder).

## Onde rodar o Standby

| Opção | Encaixe | Observação |
|---|---|---|
| VPS | alto | sem limite de tempo; você opera tudo; mesmo provedor da produção |
| Render free (hoje) | médio | dorme em 15 min sem tráfego e leva ~60 s para acordar; 750 h/mês por workspace |
| **Cloudflare Containers** | alto para o Python atual | roda qualquer imagem; plano Paid (US$ 5/mês); cobra por 10 ms ativos e **para ao dormir**; cold start de 1 a 3 s; sem autoscaling embutido; agendamento `durable_object` em beta; preço do egress para a América do Sul e região de execução não verificados |
| Cloudflare Python Workers | parcial | roda em Pyodide: **128 MB por isolate, bundle de 64 MiB, sem threads**. Teste local: `fastapi`, `pydantic`, `httpx`, `sqlalchemy`, `psycopg`, `pydantic-ai`, `procrastinate` importam; o conjunto completo deu 65,7 MiB (acima do limite). O Standby usa 13 `asyncio.to_thread` e 2 `threading.Thread` |
| Cloud Run (São Paulo) | alto | com cobrança por requisição a CPU só roda durante a requisição e a instância fica ociosa no máximo 15 min; segurar o worker pede cobrança por instância e instância mínima. Preços não lidos |
| Oracle Container Instances | médio | contêiner sempre ligado (reinício `Always`), disco efêmero (persistência por NFS), **mesmo provedor**. Preço e disponibilidade em São Paulo não verificados |
| Netlify Agent Runners | não serve | é para agentes de código, não hospeda serviço contínuo |

Dentro do Cloudflare: espera de rede não conta como CPU (CPU de 30 s por padrão, até 5 min); Hyperdrive liga a
Postgres externo (inclui Neon) com **pool em modo transação** (sem lock de sessão nem `LISTEN`; locks de
transação funcionam); o uso a partir de Python Workers exige data de compatibilidade `2026-09-08` ou posterior.
Next.js: o adaptador OpenNext (`@opennextjs/cloudflare`) cobre App Router, SSR, ISR e Server Actions (middleware em
Node ainda não); a Cloudflare recomenda o vinext (beta) para apps novos.

**Decisão:** o Standby continua no Render + Neon enquanto é contingência. A direção é Cloudflare: **front Next.js
em Workers (OpenNext)** e **backend Python em Containers**, com Cron Triggers como relógio. Antes, uma prova com
deploy real medindo conexão com o Neon, memória e latência (precisa de conta Workers Paid).

## Fila e orquestração

| Opção | Veredito | Motivo |
|---|---|---|
| **Procrastinate** (atual) | **manter** | no Neon, paridade com o Next, cron embutido; usa `LISTEN/NOTIFY`, então o worker usa o endpoint **direto** do Neon (sem `-pooler`) |
| Absurd (`absurd-sdk` 0.5.1) | só se quiser passos duráveis | alfa ("um experimento"), sem agendador, Neon e pooler não mencionados |
| Inngest | alternativa paga | chama seu endpoint por HTTP (bom para serverless), grátis só até 50 mil execuções/mês, depois US$ 99; SDK de Cloudflare só em TypeScript; servidor SSPL; dados dos passos passam pela nuvem deles |
| Hatchet | não | serverless é Labs e exige plano pago; só adaptador TypeScript (Cloudflare e Vercel); sem suporte Python documentado |
| DBOS | não para serverless | exige processo sempre ligado (agendador, fila e recuperação); Neon só pelo endpoint direto. **O Next saiu do DBOS para o Procrastinate em 31/out**; o Genius Dev segue nele |

**Gatilho** (o app dormindo): nenhum orquestrador Postgres agenda sozinho sem processo ligado. O melhor
gatilho é o da própria plataforma: Cloudflare Queues (entrega a mensagem a um consumidor, entrega "pelo menos
uma vez", atraso de até 24 h) mais Cron Trigger a cada minuto como rede de segurança que drena os jobs vencidos
(os prazos de 24 h e 48 h vivem nas linhas do banco, não na fila). No Render: GitHub Actions (atrasa minutos) ou
QStash; no Cloud Run: Cloud Scheduler. `pg_cron` no Neon só roda com o compute ativo.

## Modelos de linguagem

| Opção | Encaixe para ~100 chamadas simultâneas |
|---|---|
| API de provedor | alto; o limite é o contrato |
| Workers AI | médio a alto (73 modelos; US$ 0,011/mil neurons; 10 mil neurons/dia grátis); português por modelo e uso a partir de Python Workers não verificados |
| Modal | alto para modelo próprio (Python nativo, cobrança por segundo; Starter US$ 30 de crédito/mês; 200 requisições/s por função) |
| Ollama local | baixo (um host); não roda em serverless |
| Ollama Cloud | **baixo: 3 requisições simultâneas no Pro, 10 no Max** |

## Continuidade: falhas correlacionadas

As VPS 002 e 003 são do mesmo provedor e região, e a **Evolution roda em Docker no mesmo host da 002**. Mover a
003 para outra região da Oracle protege só contra falha regional, não de conta ou do provedor. Em ordem de custo:

1. Gatilho e aviso externos (feito: `MONITORAMENTO.md`).
2. Dados e segredos fora da Oracle: backups no R2, cofre no Neon, **sincronização Next → Neon ligada** (hoje
   não confirmada: sem ela o Standby responderia com histórico velho).
3. Standby frio em **outro provedor**: o que ele guarda é o canal do WhatsApp (sessão da Evolution/gateway), não só
   o app. Duas instâncias com a mesma sessão ao mesmo tempo podem derrubar uma delas (não verificado), então a
   segunda cópia fica provisionada, não ligada, e é restaurada do R2 no desastre (Ansible monta, restic restaura).
   Recuperação de minutos a 1 hora; segundos exigem standby quente.
4. Para tenants no **Zernio** o failover é só mudar o webhook (por API); sem sessão para duplicar.

Antes de qualquer failover automático com `live`: trava contra duas respostas ao mesmo cliente e regra de volta,
porta de entrada de webhooks na borda, e critérios escritos no `AGENTS.md` pelo chairman (trava 1).

## Painéis de hospedagem

Coolify e Dokploy são gratuitos autohospedados e rodam Docker Compose, bancos com backup e vários servidores; o
EasyPanel é pago por servidor. Coolify exige Linux com SSH e Docker (mínimo 2 CPUs, 2 GB, 10 GB; dentro de contêiner
não documentado); o Dokploy cita Proxmox LXC. Dividir app e Postgres não exige contêiner único: um **pod do Podman**
(ou vários contêineres numa instância do Oracle Container Instances) compartilha `localhost`. Licenças, templates
da Evolution e migração entre servidores: não verificados.

## Próximos passos

1. Prova do Standby em Cloudflare Containers com cron de wake (medir Neon, memória, latência).
2. Ensaio de recuperação do canal do WhatsApp em outro servidor (`GATEWAYS-WHATSAPP.md`).
3. Ligar a sincronização Next → Neon e medir o atraso.
4. Backups com restic para o R2, com os heartbeats do Better Stack avisando se não rodarem.
5. Aikido no plano grátis nos repositórios; Ansible para o servidor de recuperação.
