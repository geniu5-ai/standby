# Monitoramento e alertas

Estado em 11/nov/2026. Princípio: **detecção e aviso ficam fora da Oracle**. As VPS 002 e 003 estão no mesmo
provedor e região; vigias dentro delas só servem para falha parcial (reiniciar serviço) e morrem junto na
perda total. O Standby é público: aqui há nomes e URLs públicas, nenhum segredo, token ou id de recurso privado.

## O que fica fora da Oracle (sobrevive à perda dela)

| Peça | Onde roda | Para quê |
|---|---|---|
| Better Stack | nuvem deles | monitores a cada 30 s de 4 regiões, heartbeats, página de status, aviso por e-mail e push |
| QStash (Upstash, `us-east-1`) | AWS | schedule do wake e vigia da 002 (`docs/WAKE.md`) |
| GitHub Actions | GitHub | `monitor.yml` (saúde do Standby), `wake.yml`, backup |
| Render e Neon | AWS (Oregon e `sa-east-1`) | o Standby e o banco dele |
| R2 | Cloudflare | backups do repositório e do cofre |

Dentro da Oracle (caem juntos): Next de produção (002), Genius Dev e crew (003), Evolution (Docker no mesmo
host da 002), túnel `cloudflared`, `systemd` de vigia e o `tokens.local`. A Evolution no mesmo host significa que
o Standby cobre **falha do app**, não **perda do host**, para tenants na Evolution; para tenants no Zernio o
failover é só repontar o webhook (`docs/GATEWAYS-WHATSAPP.md`).

## Better Stack (plano grátis)

Conta limpa em 11/nov/2026: foram apagados os recursos de teste (1 monitor pausado, 5 incidentes, 1 heartbeat,
1 página de status, 6 fontes de telemetria e 1 collector que nunca se conectou). O plantão padrão não pode ser
apagado e foi renomeado para `Genius · plantão`.

**Monitores** (alertam por e-mail e push, sem escalonamento):

| Monitor | Alvo | Frequência | Observação |
|---|---|---|---|
| `Next · /health (produção)` | `https://genius-next.geniuslabs.pro/health` | 30 s, 4 regiões | palavra-chave `ok`, confirmação de 2 min, SSL a 14 dias |
| `Next · porta 443 (TCP)` | mesmo host | 3 min | distingue aplicação de túnel |
| `Next · DNS` | resolução do mesmo host | 5 min | |
| `OmniRoute · porta 443 (TCP)` | roteador de modelos | 3 min | |
| `OmniRoute · HTTP (307)` | raiz do roteador | 3 min | espera o redirecionamento; SSL a 14 dias |
| `Standby · /health` | `https://genius-next-serverless.onrender.com/health` | 10 min | **pausado de propósito**: cada checagem acordaria o Render. Despausar em failover |

**Heartbeats** (ficam `pending` e **não alertam** até o primeiro ping; as URLs de ping são segredo e ficam só no painel):
`Backup · VPS 002 (Next + Evolution)` (24 h), `Crew · VPS 003 (Genius Dev)` (5 min) e
`Backup · repositório Standby → R2` (7 dias). **Ainda não ligados:** falta o `backup.sh` da 002 e o da 003
chamarem a URL ao terminar (PR no Next).

**Organização:** 3 grupos de monitores (`Genius Next · produção`, `Infra e domínios`, `Standby · contingência`) e
3 de heartbeats (`Rotinas · VPS 002`, `Rotinas · VPS 003`, `Rotinas · Standby`).

**Página de status** `genius` (subdomínio `genius.betteruptime.com`), com seções `Atendimento`, `Infraestrutura`
e `Rotinas` e nomes genéricos. **É pública**: despublicar é configuração avançada, bloqueada no plano grátis.

**Limites do plano grátis (confirmados pela API):** escalonamento (políticas) e webhooks de **entrada** exigem
upgrade; configurações avançadas da página de status também. Webhooks de **saída** são permitidos e aceitam
cabeçalhos fixos (sem assinatura do corpo). Não foram ligadas ligação nem SMS. O **AI SRE** exige plano pago e
cobra por token: **desligado**. Se for ligado, liberar só ferramentas de leitura e conferir que logs não levam
dado de paciente.

**Tokens:** três tipos. O global alcança Uptime e Telemetria; os restritos (um de Uptime, um de Logs) são o
preferido. Revogar o global e **guardar os restritos no cofre** (hoje só estão no histórico de uma conversa,
rotacionar). Nunca em repositório.

## Vigia da VPS 002 e wake (QStash)

Detalhes em [`WAKE.md`](WAKE.md). Resumo: o schedule `vps002-watchdog` sonda o `/health` da 002 e, se falhar em
todas as retentativas, chama `/v1/control/qstash-failover` no Standby, que confirma a queda e avisa, **sem ligar
o `live`**. O Better Stack tem um segundo gatilho, o webhook de saída para `/v1/control/betterstack-incident`
(PR #17, ainda não mesclado). As chaves do Upstash estão no `tokens.local` da VPS 002, gravadas a pedido do
chairman (a regra do `AGENTS.md` diz que o arquivo é só leitura: o lugar certo é o cofre).

**Ponto aberto:** `monitor.yml` consulta o `/health` do Standby a cada 5 min e mantém o Render quase sempre
acordado (~744 de 750 h gratuitas). Decisão pendente: ritmo ou alvo.

## Avisos esperados nos logs do Standby

- `crew release: HANDOFF_AUTO_RELEASE_TENANTS vazio …`: **esperado** no boot. A variável fica vazia de propósito
  enquanto o Standby não atende tenant real (`OPERACAO.md`); só vira lista ao promover um tenant, junto com
  `DELIVERY_MODE=live`. Não é falha e não precisa de correção.

## Ferramentas avaliadas

| Ferramenta | Decisão | Motivo |
|---|---|---|
| Better Stack | **adotada** | grátis, externa, tem heartbeats, página de status, API e webhooks |
| restic (+ rclone CLI) | **próxima** | backup cifrado e verificável, destino S3/R2; base do plano de recuperação |
| Aikido | **adotar (grátis)** | segredos (inclusive no histórico), dependências Python, SAST; o caso do `TRAPS` #21 |
| Ansible | com o plano de recuperação | montar VPS de outro provedor de forma repetível |
| Monit | não agora | roda no próprio host; pouco além do `systemd` |
| Keep | não agora | nuvem grátis só pessoal; sem Better Stack nem WhatsApp entre os provedores |
| SigNoz | não agora | 4 GB só para o Docker; não sonda de fora; sobrepõe o Logfire |
| rclone UI | não | app de desktop de terceiros; o rclone em linha de comando basta |

Não verificado: licença do SigNoz e do Keep, cobertura de repositórios públicos do Aikido e preço de usuários extras.
