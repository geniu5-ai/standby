# Standby: operação

App de **contingência** do Genius Next (FastAPI + fila Procrastinate) rodando no **Render** com
banco no **Neon**. Estado em 03/nov/2026. O app de produção continua sendo o
`geniu5-ai/Genius-Next` na VPS 002; a base de conhecimento (armadilhas, runbooks) vive lá:
`docs/TRAPS.md` (#78 a #82 são deste app) e `docs/HANDOFF.md`, ambos do **Genius-Next** (privado; os do
Standby estão em [`TRAPS.md`](TRAPS.md) e [`HANDOFF.md`](HANDOFF.md)).

Papel: ficar pronto para assumir se o agente principal cair. Por isso roda em `record_only` e só
deve ir a `live` num failover deliberado (ver "Pendente antes de atender cliente real").

## Onde está

| Peça | Valor |
|---|---|
| Serviço Render | `standby` (`srv-dbjvbvtg1s2s738rr2t0`), plano free, deploy automático a cada push na `main`. Renomeado em 03/nov/2026 (antes `genius-next-serverless`) |
| URL | `https://genius-next-serverless.onrender.com` (`/health`, `/webhook/evolution`, `/webhook/zernio`, `/v1/control/wake`) |
| Banco | Neon, projeto `Next` (`noisy-fire-02526952`), região `aws-sa-east-1`, endpoint `ep-spring-brook`, schema `genius_next` + tabelas `procrastinate_*` |
| Observabilidade | Logfire (`LOGFIRE_TOKEN`), projeto `genius` |
| Repositório | `geniu5-ai/standby` (antes `genius-next-serverless`) |

**Sobre a URL:** o Render não permite trocar o subdomínio de um serviço existente, então a URL
continua com o nome antigo mesmo após renomear o serviço. Para ter `standby.onrender.com` seria
preciso criar um serviço novo (copiar variáveis, repontar webhooks) e apagar o antigo. Nada em
produção aponta para esta URL hoje, só os workflows `monitor.yml` e `wake.yml` e o
`scripts/testar_webhook.sh`.

O plano free **dorme após ~15 min sem tráfego de entrada**: a primeira chamada leva até ~60 s (o
"Shutting down" no log é isso, não queda). Detalhes e a solução em [`WAKE.md`](WAKE.md).

## Variáveis de ambiente (nomes; os valores ficam só no Render)

| Variável | Para quê |
|---|---|
| `DATABASE_URL` | Neon |
| `HMAC_SECRET` | assinatura do `/webhook/evolution` (`X-Hub-Signature-256` ou `X-Evolution-Token`) |
| `CONTROL_HMAC_SECRET` | assinatura de `/v1/control/*`, incluindo `/v1/control/wake`; sem ele esses endpoints respondem 503 |
| `EVOLUTION_URL`, `EVOLUTION_API_KEY` | Evolution (histórico e "digitando"); com `record_only` nada é enviado |
| `QSTASH_CURRENT_SIGNING_KEY`, `QSTASH_NEXT_SIGNING_KEY`, `QSTASH_WAKE_URL` | wake pelo QStash: chaves de assinatura (console do QStash) e a URL do destino do schedule; sem a chave atual `/v1/control/qstash-wake` responde 503 |
| `CONNECTOR_HMAC_SECRET` | segredo PROPRIO da rota `/webhook/connector` (conector OpenWA/waxum, `connectors/wa-bridge/`); assinatura `timestamp.corpo`; sem ele a rota responde 503 e ele nunca cai para o `HMAC_SECRET` da Evolution |
| (wa-bridge, na 003) `TYPING_ENABLED`, `TYPING_PRE_DELAY_S`, `TYPING_REFRESH_S`, `TYPING_MAX_S` | digitando para a pessoa: começa em 5 s, como o Next; `TYPING_ENABLED=0` desliga |
| `CONNECTOR_RELAY_INSTANCES`, `CONNECTOR_RELAY_MAX_AGE_SECONDS` | saída pelo conector sem sair do `record_only`: instâncias cujas respostas gravadas o conector pode buscar em `/v1/connector/outbox` (vazia = nada sai) e idade máxima da resposta (padrão 900 s) |
| `FAILOVER_PROBE_URL`, `QSTASH_FAILOVER_URL`, `FAILOVER_ALERT_URL`, `FAILOVER_ALERT_COOLDOWN_S` | vigia da VPS 002 em modo alerta (`WAKE.md`): URL sondada, URL pública da rota `/v1/control/qstash-failover`, webhook de aviso (secreta, opcional) e intervalo mínimo entre avisos (padrão 1800 s); sem `FAILOVER_PROBE_URL` a rota responde 503 |
| `BETTERSTACK_WEBHOOK_SECRET`, `BETTERSTACK_WATCHED_MONITORS` | segundo gatilho do vigia (`WAKE.md`): segredo do cabeçalho `X-Webhook-Secret` do webhook de saída do Better Stack (secreta) e os ids dos monitores da 002 (separados por vírgula); sem o segredo a rota `/v1/control/betterstack-incident` responde 503 |
| `OMNIROUTE_API_KEY` | OmniRoute (resumos, motor legacy) |
| `REPLY_ENGINE=agent`, `REPLY_AGENT_MODEL=openai:gpt-pool` | motor de resposta = Pydantic AI pelo combo `gpt-pool` do OmniRoute (o mesmo da produção, TRAPS #68 do Next) |
| `OPENAI_BASE_URL=https://omni.geniuslabs.pro/v1`, `OPENAI_API_KEY` | como o Pydantic AI alcança o OmniRoute |
| `OUTPUT_GUARD_ENABLED=true` | guarda de saída local (credencial, texto invisível) |
| `LOGFIRE_TOKEN` | envio para o Logfire |

**Não definidas de propósito:** `DELIVERY_MODE` (padrão `record_only`: a resposta é gravada no `outbox` e **nada vai ao WhatsApp**), `DELIVERY_ALLOWLIST` (vazia: o "digitando" também não sai), `OPS_ALERT_TO`, `HANDOFF_AUTO_RELEASE_TENANTS` (vazia: o aviso no boot é esperado enquanto não houver tenant real), `MANAGER_PENDENTES_TENANTS` (vazia: o `pendentes_sweep` não abordará ninguém).

## Segredos e credenciais (onde ficam; nunca os valores)

Regra: **nenhum valor em repositório, doc, Drive, Notion ou chat.** O histórico do git guarda para sempre.

### Hoje

| Segredo | Onde | Observação |
|---|---|---|
| `HMAC_SECRET`, `DATABASE_URL`, `OMNIROUTE_API_KEY`, ... | variáveis do serviço no Render | fonte de verdade em execução; legíveis pela API do Render com a `RENDER_API_KEY` |
| `RENDER_API_KEY`, `NEON_API_KEY`, `OMNIROUTE_*`, `HMAC_SECRET` e ~110 outras | `tokens.local` em `/home/ubuntu/.config/genius/tokens.local` nas VPS 002 e 003 | cofre atual do Genius, texto puro. Fora do escopo do Standby: **o Genius Dev (VPS 003, DBOS) não deve ser alterado**. A `NEON_API_KEY` da 002 funciona (exige `org_id=org-flat-hat-11836365`); a da 003 está desatualizada |
| `WAKE_DATABASE_URL` | segredo do repositório no GitHub | URL do Neon da role `wake_ro` (ver "Roles do banco"); uma linha só, começando em `postgresql://`, com `?sslmode=...` e sem aspas |
| `CONTROL_HMAC_SECRET` | segredo do GitHub **e** variável do Render | os dois com o mesmo valor |
| `TAILSCALE_AUTHKEY` | ambiente das sessões do Claude Code | entra na tailnet `topmidiahd`; usada só quando preciso das VPS |

Segredos do GitHub são **somente escrita**: depois de salvos ninguém lê de volta. Servem ao Actions
(`${{ secrets.NOME }}`, mascarados nos logs, não chegam a PRs de forks); não são um cofre
consultável.

### Cofre do Standby: segredos cifrados no Neon (03/nov/2026)

Schema `standby_vault` no banco do Standby. Cada valor é **cifrado fora do banco** (AES-256-GCM,
autenticada) antes de ser enviado: o servidor nunca vê valor nem chave, então log de statement ou
vazamento do banco não expõem nada. A chave de 256 bits sai da chave mestra por HKDF-SHA256 e o
**nome do segredo entra como dado autenticado (AAD)**: alterar o texto cifrado, ou trocar o
conteúdo de duas linhas de lugar, faz a decifragem falhar (testado). Formatos em `value_enc`:
base64(`0x01` + nonce de 12 bytes + texto cifrado + tag), `fmt = 'aead1'` (uma parte da chave); e
`0x02`, `fmt = 'aead2'`, derivado de **duas partes** da chave. Hoje `RENDER_API_KEY`, `NEON_API_KEY`,
`R2_ACCESS_KEY_ID` e `R2_SECRET_ACCESS_KEY` estão em `aead2`; as demais, em `aead1`. Ideias, decisões e alternativas: [`COFRE-IDEIAS.md`](COFRE-IDEIAS.md).

**Acesso só por funções** (`scripts/vault_schema.sql`, `SECURITY DEFINER`, `search_path` fixo,
`EXECUTE` revogado de `PUBLIC`). A role `vault_rw` **não tem acesso às tabelas**; só executa:

| Função | Faz |
|---|---|
| `vault_get(nome)` | devolve o texto cifrado e **registra o acesso** em `access_log` |
| `vault_list()` | nomes, formato, origem e data (sem valores) |
| `vault_put(...)` | grava e guarda a versão em `secret_versions` |
| `vault_delete(nome)` | apaga, mas guarda a última versão no histórico |
| `vault_history(nome)` / `vault_restore(nome, id)` | versões (quem, quando, ação) e volta a uma delas |
| `vault_export()` | todo o texto cifrado, para backup (registra o acesso) |
| `vault_access_log(n)` | últimos `n` acessos: quem leu o quê e quando |
| `vault_set_rotate_by(nome, dias)` / `vault_due(dias)` | prazo de rotação e lista do que vence |

`secret_versions` e `access_log` são **somente de inserção**: gatilhos recusam `UPDATE`, `DELETE` e
`TRUNCATE`, inclusive do dono do banco (que pode desligar o gatilho, e isso deixa rastro no catálogo,
mas não no log). O rótulo de quem chamou vem de `application_name` (`vault.py:usuário@máquina`, ou
`STANDBY_VAULT_ACTOR`): é informativo, não prova.

| Nome | Para quê |
|---|---|
| `RENDER_API_KEY` | operar o Render (variáveis do serviço, deploys) |
| `NEON_API_KEY`, `NEON_ORG_ID` | administração do Neon (roles, senhas) |
| `OMNIROUTE_API_KEY`, `OPENAI_API_KEY` | modelo usado pelo agente |
| `EVOLUTION_URL`, `EVOLUTION_API_KEY` | Evolution (somente leitura em `record_only`) |
| `LOGFIRE_READ_TOKEN` | ler os logs do projeto `genius` |
| `R2_ENDPOINT`, `R2_BUCKET` | onde fica o backup do repositório (`aead1`) |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | gravar no R2 (`aead2`: precisam das duas partes da chave, porque a chave dá escrita e apagar no bucket inteiro) |

Origem: `tokens.local` da VPS 002, **só lido, nunca alterado**. O que **não** foi copiado, de
propósito: as outras ~110 entradas (token do GitHub, Cloudflare, Supabase `service_role`, login do
Instagram, OCI, Slack, chaves administrativas da OpenAI e tokens de instâncias de clientes).

**Acesso:** a sessão do Claude Code precisa de duas variáveis no ambiente (menu da sessão →
*Edit*); o resto sai do cofre, inclusive a `RENDER_API_KEY`:

| Variável do ambiente | Para quê |
|---|---|
| `STANDBY_VAULT_URL` | URL do Neon da role `vault_rw`, **endpoint direto** (sem `-pooler`, `TRAPS` #9) |
| `STANDBY_VAULT_KEY` | chave mestra (parte 1): 64 hexadecimais aleatórios; sem ela a tabela é ilegível |
| `STANDBY_VAULT_KEY2` | **opcional**, parte 2. Com ela o `put` grava `aead2`, que só abre com as **duas** partes. Guarde-a **fora** do ambiente; defina só quando a sessão precisar de uma entrada `aead2` |

```bash
python scripts/vault.py list                          # nomes, formato, origem e data (nunca valores)
VALOR=$(python scripts/vault.py get NEON_API_KEY)     # valor decifrado, direto para uma variável
printf '%s' "$VALOR" | python scripts/vault.py put NOME origem
python scripts/vault.py run RENDER_API_KEY -- ./meu_script.sh   # segredo só no ambiente do filho
python scripts/vault.py delete NOME                   # recuperável: history NOME / restore NOME ID
python scripts/vault.py log 20                        # últimos 20 acessos
python scripts/vault.py expire NOME 90                # prazo de rotação: hoje + 90 dias
python scripts/vault.py due                           # vencido, vence em 14 dias ou sem prazo há 90+ dias
python scripts/vault.py export backup.json            # só texto cifrado + sha256 (0600); import volta
NOVA=<chave> [NOVA2=<parte 2>] python scripts/vault.py rotate   # recifra tudo, numa transação
```

**Transporte (automático):** o sandbox do Claude Code **não abre a porta 5432** do Neon (o `psycopg`
dá *timeout*), mas abre HTTPS. O `vault.py` testa a porta (4 s) e, se ela não abrir, usa o **SQL
sobre HTTPS do Neon** (`POST https://<host>/sql`, cabeçalho `Neon-Connection-String`): o mesmo
cofre, as mesmas funções, cada chamada numa transação. VPS e máquinas com a porta aberta usam o
Postgres direto. Para forçar: `STANDBY_VAULT_TRANSPORT=pg|http`. Verificado em 03/nov a partir do
sandbox: `list`, `get`, `put`, `expire`, `due`, `history`, `log`, `run` e o erro legível do banco.
Prova de ponta a ponta: a `RENDER_API_KEY` (`aead2`) decifrada do cofre foi aceita pela API do
Render (HTTP 200).

**Velocidade** (medido do sandbox em 03/nov): a 1ª consulta da sessão leva ~3 s (teste da porta, no
máximo 2,5 s no total) e as seguintes **~0,65 s**, porque a decisão `pg`/`http` fica guardada por 30
min em `$TMPDIR/standby-vault-transporte-*`. Chave de duas partes e `run` não custam a mais
(`run RENDER_API_KEY -- curl ...` até a resposta da API: ~0,9 s). Para acesso instantâneo, exporte
`STANDBY_VAULT_TRANSPORT=http` no ambiente da sessão.

Atenção: com `STANDBY_VAULT_KEY2` definida, **todo `put` grava `aead2`**, inclusive um valor que
não é segredo. Para gravar em `aead1`, rode o `put` sem essa variável (`unset STANDBY_VAULT_KEY2`).

Requer `cryptography` e `psycopg` (`pip install cryptography "psycopg[binary]"`). Instalar o
esquema ou atualizá-lo: `psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/vault_schema.sql` (dono
do banco; idempotente). Backup (`export`) fica fora do banco: gerenciador de senhas ou Drive.

Regras: nunca imprima o valor em log, resultado de task ou chat (`run` existe para isso); guarde a
`STANDBY_VAULT_KEY` **só** no ambiente da sessão e num cofre seu (se for perdida, os valores são
irrecuperáveis: regrave a partir do `tokens.local`); **não apague arquivo `*.local` com segredo
antes de o chairman confirmar que copiou** (`TRAPS` #10).

Limites conhecidos:
- A chave mestra é ponto único de falha: quem tem `STANDBY_VAULT_URL` **e** `STANDBY_VAULT_KEY` lê
  tudo. Mantenha as duas em lugares diferentes quando puder.
- O dono do banco (`neondb_owner`, o `DATABASE_URL` do app) é `neon_superuser`: lê e altera tudo,
  inclusive desligar os gatilhos. Vê só texto cifrado e não forja um valor sem a chave; apagar
  continua possível (por isso o backup).
- O log registra o rótulo informado pelo cliente (`application_name`), não a identidade de quem usa.
- Isto contraria a regra do Genius Dev de nunca gravar chave no banco
  (`docs/GENIUS-DEV-AGENTS.md` §4); foi decisão expressa do chairman em 03/nov/2026, para o banco do
  Standby, e por isso só entrou o que o Standby usa.

### Roles do banco (menor privilégio)

| Role | Pode | Não pode |
|---|---|---|
| `wake_ro` (Actions, `WAKE_DATABASE_URL`) | `USAGE` em `public` e `genius_next`; `SELECT` em `procrastinate_jobs`, `procrastinate_workers`, `genius_next.events`, `genius_next.clients`, `genius_next.outbox` | escrever, criar objetos ou roles, ler o cofre ou qualquer outra tabela |
| `vault_rw` (cofre, `STANDBY_VAULT_URL`) | `EXECUTE` nas funções `standby_vault.vault_*` | ler ou escrever as tabelas do cofre diretamente, ler `genius_next`/`procrastinate_*`, criar objetos ou roles |

**Role no Neon nasce por SQL, nunca pela API/console** (`TRAPS` #8): as criadas pela API entram em
`neon_superuser` e enxergam e alteram tudo mesmo sem `GRANT`. Depois de criar, prove o isolamento com
consultas permitidas e negadas, com `select current_user` antes.

### Em estudo: guardar segredos "de reserva" no Render

O Render tem *Environment Groups*: conjuntos de variáveis e arquivos secretos que podem ser
ligados a vários serviços ([docs](https://render.com/docs/configure-environment-variables)). Um grupo
**sem nenhum serviço ligado** não é injetado em processo algum, então serviria de cofre. Em aberto:
confirmar no painel se o plano free oferece grupos e quantos. **Não** coloque segredos de reserva
como variáveis de um serviço em execução: todas elas entram no processo e aparecem no painel e
nos logs de quem tiver acesso.

## Tenant de teste

`mariaeduarda_ficticia` (instância `mariaeduarda-ficticia`, que **não existe** no Evolution):
`manager_jid` nulo (handoff não avisa ninguém), follow-up desligado em `record_only`, 4 perfis de
prompt copiados do tenant `genius` do Next (`reply_normal` v32, `persuasao` v5, `guardrail` v2,
`criticas` v8) e 11 itens de catálogo com **valores fictícios**.

## Testar

```bash
HMAC_SECRET=... ./scripts/testar_webhook.sh mariaeduarda-ficticia 5500000000010 "Quanto custa a prótese flexível?"
```

Resposta `202` com `workflow_id` = entrou. A resposta do agente sai em ~20 s:

```sql
select status, error, decision->>'action', decision->>'draft_text' from genius_next.turns
 where tenant_id = 'mariaeduarda_ficticia' order by created_at desc limit 3;
select delivery_mode, delivery_status, payload->>'text' from genius_next.outbox
 where tenant_id = 'mariaeduarda_ficticia' order by created_at desc limit 6;
```

Tudo deve estar `record_only` / `recorded`. Nunca use número real de WhatsApp como cliente de teste.

Teste de ponta a ponta feito em 02/nov/2026 (Render + Neon + modelo real): mensagem "Oi! Quanto custa a
prótese flexível?" → turno `completed`, resposta com o preço do catálogo (R$ 1.500 em até 10x) e
`outbox` em `record_only` / `recorded`.

Sem rede e sem banco (modelo falso, guardrails e agente): `pytest -q tests`.

## O que já deu errado e por quê

Ver [`TRAPS.md`](TRAPS.md): dezessete armadilhas, cada uma com sintoma, causa, correção e regra. As mais
úteis para quem opera: #6 (segredo de URL colado errado), #8 e #9 (roles do Neon), #10 (não apagar
arquivo de segredo antes do chairman copiar).

## Wake do worker (Actions)

Resumo; o desenho completo, as decisões e as alternativas estão em [`WAKE.md`](WAKE.md).

O Procrastinate roda dentro do serviço web: no plano free ele dorme com o Render e leva os relógios
junto. `.github/workflows/wake.yml` (a cada 20 min, minutos 7/27/47) roda `scripts/wake_check.py`,
que consulta o Neon com a role `wake_ro`, de leitura e, só se houver trabalho vencido, chama
`POST /v1/control/wake` assinado. Sem trabalho, o serviço continua dormindo.

| Onde | Nome | O quê |
|---|---|---|
| Segredo do GitHub | `WAKE_DATABASE_URL` | URL do Neon com a role `wake_ro` |
| Segredo do GitHub | `CONTROL_HMAC_SECRET` | o mesmo valor definido no Render |
| Variável do Render | `CONTROL_HMAC_SECRET` | habilita `/v1/control/wake` (sem ele responde 503) |
| Variável do GitHub (opcional) | `WAKE_TENANTS` | tenants separados por vírgula; vazio = todos |

**Wake pelo QStash (11/nov/2026):** `POST /v1/control/qstash-wake`, assinado pela Upstash, chamado
pelo schedule `standby-wake` (`scripts/qstash_schedule.py`). `QSTASH_TOKEN` e `QSTASH_URL` servem só a
esse script e ficam fora do Render; as chaves de assinatura são variáveis do Render. Desenho,
custo e a conta de horas do Render em [`WAKE.md`](WAKE.md).

Teste manual: *Actions → Wake do worker → Run workflow* na `main`, com `force`. Resultado
esperado: job verde e `{"status": "awake", "jobs_vencidos": 0, ...}`. Verificado em 03/nov/2026.

## Backup do repositório (R2)

Cópia completa do repositório (todas as refs e todo o histórico) num `git bundle`, guardada fora do
GitHub. Primeira cópia feita em 03/nov/2026.

| Item | Valor |
|---|---|
| Onde | bucket `genius` do Cloudflare R2, **pasta `Genius-Standby/` na raiz** (o mesmo bucket guarda `backups/genius_dev/` e `backups/genius-specific/`; há ainda o bucket `genius-clientes`, que o Standby não usa nem toca) |
| Nome | `Genius-Standby/standby-<data UTC>-<sha7 da main>.bundle` + `.sha256` + `LEIA-ME.txt` |
| Primeira tentativa | em 03/nov/2026 foi gravada em `backups/standby/repo/`, dentro de duas pastas, e passou despercebida na raiz; foi refeita em `Genius-Standby/` e a cópia antiga removida (`TRAPS` #15) |
| Credenciais | `R2_ENDPOINT`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`: lidas do `tokens.local` da VPS 002 (só leitura) e guardadas no cofre (`R2_ACCESS_KEY_ID` e `R2_SECRET_ACCESS_KEY` em `aead2`, que pede as duas partes da chave). O R2_SECRET dá escrita e **apagar** no bucket inteiro, inclusive nos backups do Genius Dev: use só para este backup |
| Cifrado? | **Não.** O bundle tem todo o histórico (inclui a chave do `CPFHUB_API_KEY`, `TRAPS` #14). Só em bucket privado |

```bash
scripts/backup_repo.sh                    # cria, verifica e testa a restauração; salva ./standby-*.bundle
scripts/backup_repo.sh --subir            # idem e sobe ao R2 (com as variáveis R2_* no ambiente)
scripts/backup_repo.sh --so-subir ARQ     # sobe um bundle já existente
```

**Do sandbox do Claude Code** (sem VPS): as quatro variáveis do R2 saem do cofre (as duas chaves de
escrita são `aead2`, então a sessão precisa de `STANDBY_VAULT_KEY2`):

```bash
python scripts/vault.py run R2_ENDPOINT R2_BUCKET R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY -- \
  ./scripts/backup_repo.sh --subir
```

Verificado em 03/nov/2026: `Genius-Standby/standby-20261103T155104Z-2987d3b.bundle` (30 commits, depois
do merge do PR #8), sha256 `b0e1ff35…5e7427`, conferido por download. A cópia anterior
(`…T145957Z-41afa5e`, 24 commits) continua na pasta; apague as antigas quando quiser, uma a uma.

O script completa o histórico se o clone for raso, verifica o bundle, **clona o bundle numa pasta
de teste e roda `git fsck`** antes de subir, **nunca sobrescreve** (nome existente → código 4,
testado) e confere o objeto no R2 por download e hash.

**Restaurar:** baixar o `.bundle` e o `.sha256`, conferir com `sha256sum -c`, e
`git clone standby-....bundle standby` (todas as branches ficam em `origin/*`).

O repositório continua no GitHub; o R2 é a segunda cópia. Repita o backup depois de mudanças
grandes. O cofre tem o seu próprio backup cifrado (`vault.py export`), que **não** está no bundle.

## Paridade com o Next

**Canônico: [`PARIDADE.md`](PARIDADE.md)** (o mesmo arquivo existe no Genius-Next). Lá estão a regra
(o que muda no Next muda aqui na mesma entrega; o repositório é público, `TRAPS` #20), a matriz do que é igual e
do que é separado, a medição por hash de `app/` com o comando para refazê-la, as diferenças deliberadas
(`app/wake.py`, `app/embed/`, `app/curator/`) e o registro de espelhamentos (PR `Genius-Next#74`).
Aqui não se repete nada disso. Dados e ambiente não são código: prompts e catálogo chegam por sincronização do
banco (a construir) ou dump posterior (`TRAPS` #18), e o env do Next não vale aqui (`TRAPS` #19).

## Pendente antes de atender cliente real

1. Trocar as chaves que circularam em 02/nov (`HMAC_SECRET`, senha do Neon, OmniRoute, Evolution, `LOGFIRE_TOKEN`). Também as geradas para o wake (`wake_ro`, `CONTROL_HMAC_SECRET`) apareceram numa conversa e valem ser rotacionadas.
2. Embeddings: a busca de lições e o status audit chamam um sidecar de embedding local que não existe no Render (não derruba o turno, só não tem esse contexto).
3. Plano pago (o free dorme), `DELIVERY_MODE=live` + `DELIVERY_ALLOWLIST`, `HANDOFF_AUTO_RELEASE_TENANTS` com o tenant, e cadastro pelo runbook de ativação do Next (`docs/runbooks/RUNBOOK-ATIVACAO-TENANT.md`, **no repositório Genius-Next**).
4. Failover automático: o workflow que vigia a VPS 002 e liga o `live` só quando ela cair ainda **não existe** (ver `WAKE.md`, "Próximos passos").
5. Investigar o job `conversation_turn` em `failed` de 02/nov 05:49 UTC (ainda não analisado).
6. Rotinas do Next que mencionam o nome antigo (`geniu5-ai/genius-next-serverless`) fora deste repositório: `scripts/core/dbos/github_monitor.py` no Genius-Dev (VPS 003) e `docs/HANDOFF.md` / `docs/TRAPS.md` no Genius-Next (VPS 002). Atualizar junto com o rename do repositório.
7. Validar a paridade do Standby com o Next, módulo a módulo. **Primeira comparação por arquivo feita em 03/nov** (`PARIDADE.md`); falta comparar o comportamento de:
   `app/status/` (classificação de status igual à do Next), lessons (`app/agent/lesson*.py`, busca e
   extração), embeds (hoje sem sidecar de embedding no Render), o juiz (avaliação/guardrails de
   qualidade, `deepeval`) e os demais módulos de `app/`. Fazer por comparação com o repositório
   `geniu5-ai/Genius-Next`, sem tocar nas VPS.
8. Rotacionar as credenciais que apareceram em conversa (`wake_ro`, `CONTROL_HMAC_SECRET`) ao criar
   o cofre novo.
9. Criar o ambiente da sessão com `STANDBY_VAULT_URL` e `STANDBY_VAULT_KEY` (os dois valores estão em
   `vault-env.local`, arquivo local ignorado pelo git; copiar e só então apagar) e testar `python scripts/vault.py list` numa sessão nova.
10. ~~**`CPFHUB_API_KEY` fixa no código**~~ **(valor padrão removido em 04/nov, `TRAPS` #21; falta rotacionar a chave no CPFHub)** — (`app/tools/procuracao.py`, como valor padrão de `os.getenv`),
    vinda da cópia do Next: está no histórico do Standby e vai em todo clone e backup. Rotacionar a
    chave no CPFHub, trocar o padrão por vazio e definir `CPFHUB_API_KEY` no Render se a ferramenta
    for usada (`TRAPS` #14). Conferir o mesmo no repositório do Next.
