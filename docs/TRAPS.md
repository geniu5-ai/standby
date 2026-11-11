# Armadilhas do Standby

Mesmo padrão do `TRAPS.md` do Genius Next: uma entrada por armadilha, em ordem de descoberta, com
**Data / Impacto**, **Causa**, **Correção** e **Regra**. Armadilha nova entra aqui, na mesma entrega
que a corrige. As do Next que também valem aqui estão citadas pelo número dele (`Next #NN`).

### 1. `401` em todo webhook do Standby
**Data:** 02/nov/2026 | **Impacto:** nenhum webhook de teste entrava; nada era gravado.

**Causa:** o teste apontava para a URL da VPS (`genius-next.geniuslabs.pro`) em vez da do Render, e a tabela `tenant_registry` do Neon estava vazia (a instância não estava cadastrada).

**Correção:** usar a URL do Render e cadastrar a instância no `tenant_registry`.

**Regra:** teste de webhook sempre contra a URL do serviço que se quer testar, e confira o `tenant_registry` antes.

### 2. `503` e nada gravado: `clients_status_check` do Neon diferente do baseline
**Data:** 02/nov/2026 | **Impacto:** todo webhook de cliente novo voltava 503. Ambiente de teste.

**Causa:** o Neon nasceu aceitando `new/off` no status, enquanto o código usa `open/block`. Ver `Next #82`.

**Correção:** constraint igual ao `migrations/000_baseline.sql`.

**Regra:** banco novo nasce do baseline, nunca de cópia parcial; compare as constraints `CHECK`.

### 3. Turno `failed` por `DEEPSEEK_API_KEY`
**Data:** 02/nov/2026 | **Impacto:** nenhuma resposta do agente.

**Causa:** o motor `agent` usava o modelo DeepSeek padrão, sem a chave no Render.

**Correção:** `REPLY_AGENT_MODEL=openai:gpt-pool` e `OPENAI_BASE_URL` / `OPENAI_API_KEY` (OmniRoute).

**Regra:** o modelo do Standby é definido por variável de ambiente; confira-a ao criar um serviço.

### 4. Toda resposta virava "Vou confirmar isso com a equipe"
**Data:** 02/nov/2026 | **Impacto:** o agente nunca respondia de verdade.

**Causa:** a guarda de saída dependia do sidecar `llm-guard`, que não existe no Render. Ver `Next #78`.

**Correção:** guarda local (`app/security/guard.py`) e `OutputGuardrail` do Pydantic AI.

**Regra:** nenhuma guarda pode depender de processo que o Render não roda.

### 5. Motor `legacy` respondia `action: "function"`
**Data:** 02/nov/2026 | **Impacto:** resposta inválida.

**Causa:** o `gpt-oss` não segue o envelope JSON do motor `legacy`.

**Correção:** usar o motor `agent`.

**Regra:** `REPLY_ENGINE=agent` no Standby.

### 6. Workflow `Wake do worker` falhou com `ProgrammingError`
**Data:** 03/nov/2026 | **Impacto:** o wake não rodava (job vermelho).

**Causa:** o segredo `WAKE_DATABASE_URL` foi colado com o prefixo `NOME=`, entre aspas, com espaço ou quebrado em duas linhas (sem o `?` antes de `sslmode`). O psycopg recusa a URL e só informa o tipo do erro.

**Correção:** o script normaliza prefixo, aspas e espaços e a mensagem de erro mostra usuário e host (nunca a senha). A quebra de linha precisa ser corrigida no segredo.

**Regra:** segredo de URL é uma linha só, começando em `postgresql://`. Mensagem de erro que mostra a origem do problema sem vazar o valor.

### 7. `import app.wake` no Actions falhava com `No module named 'app.durable'`
**Data:** 03/nov/2026 | **Impacto:** o script de wake não rodava no Actions.

**Causa:** `app/__init__.py` importa o app inteiro e o Actions só instala o `psycopg`.

**Correção:** `scripts/wake_check.py` carrega `app/wake.py` pelo caminho (`importlib`).

**Regra:** script de operação que roda fora do app não importa pelo pacote `app`.

### 8. Role criada pela API ou pelo console do Neon é `neon_superuser`
**Data:** 03/nov/2026 | **Impacto:** `wake_ro` (documentado como "somente leitura") e `vault_rw` podiam ler **e escrever** em todas as tabelas. Sem dado alterado; descoberto por um teste de permissões negadas.

**Causa:** roles criadas pela API/CLI/console entram em `neon_superuser` (`CREATEROLE`, `CREATEDB`, `pg_read_all_data`, `pg_write_all_data`); nenhum `GRANT` mínimo tira isso. Ver `Next #83`.

**Correção:** `REVOKE`, apagar a role pela API e recriar por SQL (`CREATE ROLE ... NOSUPERUSER NOCREATEDB NOCREATEROLE`).

**Regra:** role de menor privilégio no Neon nasce por SQL. Prove o isolamento com consultas permitidas **e** negadas, confirmando o login antes (`select current_user`), e olhe `pg_has_role(..., 'neon_superuser'|'pg_read_all_data'|'pg_write_all_data', 'member')`.

### 9. `invalid role OID` ao conectar com role recriada pelo pooler do Neon
**Data:** 03/nov/2026 | **Impacto:** a `vault_rw` recriada falhava em toda conexão pelo `-pooler`; um teste "negado" teria passado por engano.

**Causa:** o PgBouncer reaproveita conexões de servidor da role apagada, que seguem com o OID antigo. Ver `Next #84`.

**Correção:** usar o endpoint direto (o mesmo host sem `-pooler`) para essa role.

**Regra:** não recriar role com o mesmo nome sobre o pooler e supor que funciona; teste o login primeiro. O cofre usa o endpoint direto.

### 10. Chave mestra do cofre apagada por engano
**Data:** 03/nov/2026 | **Impacto:** o arquivo `vault-key.local` era a única cópia da chave; ao limpar arquivos temporários ele foi apagado e as 7 entradas do cofre ficaram ilegíveis. Sem perda de segredo, porque todos vinham do `tokens.local` da VPS 002: o cofre foi regravado com chave nova.

**Causa:** limpeza de `*.local` sem checar se o chairman já tinha copiado o conteúdo (ele estava no celular e ainda não tinha).

**Correção:** nova chave, entradas regravadas e conferidas por hash contra a origem; as duas variáveis agora em `vault-env.local`.

**Regra:** **nunca apagar arquivo `*.local` com segredo sem o chairman confirmar que copiou.** Um segredo gerado só existe num lugar até alguém o guardar: não limpe antes. Sempre `cp` para um nome novo em vez de `rm`, e confira o arquivo novo antes.

### 11. Push forçado sobrescreveu um commit do PR
**Data:** 03/nov/2026 | **Impacto:** o commit do `OPERACAO.md` do PR #7 sumiu da branch por alguns minutos; o conteúdo foi restaurado a partir do objeto local.

**Causa:** a branch do PR foi recriada a partir da `main` (`git checkout -B`) num clone raso, e o push forçado trocou o histórico remoto. O clone raso também quebra `cherry-pick` (o pai vira "raiz").

**Correção:** recuperar o commit pelo hash e reaplicar o arquivo (`git checkout <hash> -- arquivo`).

**Regra:** antes de recriar uma branch com PR aberto, anote o SHA do remoto (`git ls-remote`); use `--force-with-lease=<branch>:<sha>`; em clone raso, reaplique arquivo em vez de `cherry-pick`.

### 12. O subdomínio do Render não muda ao renomear o serviço
**Data:** 03/nov/2026 | **Impacto:** o serviço se chama `standby`, mas a URL segue `genius-next-serverless.onrender.com`.

**Causa:** o Render não permite trocar o subdomínio de um serviço existente.

**Correção:** documentar. Para `standby.onrender.com` é preciso criar um serviço novo, copiar as variáveis e apagar o antigo (não feito).

**Regra:** nome de serviço e URL são coisas diferentes; `monitor.yml`, `wake.yml` e `scripts/testar_webhook.sh` usam a URL real.

### 13. `CREATE OR REPLACE FUNCTION` não muda o tipo de retorno
**Data:** 03/nov/2026 | **Impacto:** ao acrescentar `rotate_by` ao retorno de `vault_list()`, reaplicar o esquema falharia ("cannot change return type of existing function").

**Causa:** no Postgres, `CREATE OR REPLACE` só troca o corpo; mudar as colunas de retorno exige `DROP FUNCTION` antes. Depois do `DROP`, o `GRANT EXECUTE` some junto.

**Correção:** `DROP FUNCTION IF EXISTS standby_vault.vault_list();` antes do `CREATE FUNCTION`, e o bloco de `GRANT` no fim do script (idempotente) reaplica a permissão.

**Regra:** esquema que muda assinatura de função tem o `GRANT` no mesmo script, depois dos `CREATE`, para que reaplicar o arquivo inteiro sempre deixe as permissões certas.

### 14. Chave de API fixa como valor padrão de `os.getenv`
**Data:** 03/nov/2026 | **Impacto:** `app/tools/procuracao.py` traz `CPFHUB_API_KEY = os.getenv("CPFHUB_API_KEY", "<chave real>")`. A chave está no código e em todo o histórico do Standby (desde `ca3ada2`, a cópia do Next), logo em cada clone, em cada PR e no backup do R2. Achada pela varredura de segredos feita antes do backup; nenhum uso indevido conhecido.

**Causa:** a ferramenta foi copiada do Next com a chave como padrão para "funcionar sem configurar".

**Correção (pendente):** rotacionar a chave no CPFHub, trocar o padrão por `""` e definir `CPFHUB_API_KEY` no Render se a ferramenta `procuracao` for usada. Limpar o histórico não adianta (a chave já foi copiada): a rotação é o que resolve.

**Regra:** valor real nunca é o segundo argumento de `os.getenv`. Antes de qualquer backup ou cópia para fora, varrer **todo o histórico** (`git log --all -p | grep -E 'sk-|npg_|ghp_|postgres(ql)?://[^:]+:[^@]+@|[0-9a-f]{64}'`) e tratar o que aparecer. O mesmo código existe no Genius Next: conferir lá.

### 15. Backup no R2 "sumiu": gravado em pasta aninhada demais
**Data:** 03/nov/2026 | **Impacto:** o backup do repositório existia (conferido por download e hash), mas estava em `backups/standby/repo/` no bucket `genius`; na raiz só aparecia `backups/`, e o chairman concluiu que o backup não tinha dado certo. Sem perda de dado.

**Causa:** escolhi um caminho que imitava a convenção das pastas vizinhas (`backups/<produto>/`) sem perguntar onde o chairman olharia; no R2 pasta é só prefixo de chave, então ela só aparece se alguém abrir cada nível.

**Correção:** pasta `Genius-Standby/` na raiz do bucket, com `LEIA-ME.txt`; cópia antiga removida (só as duas chaves que eu mesmo criei).

**Regra:** o destino de um backup é combinado com quem vai procurá-lo, e o resultado é mostrado do jeito que essa pessoa o vê (caminho exato, nome do bucket). Confirmar "existe" pela API não basta; diga onde olhar.



### 16. O sandbox do Claude Code não abre a porta do Postgres do Neon
**Data:** 03/nov/2026 | **Impacto:** `vault.py` (e qualquer `psql`/`psycopg`) a partir de uma sessão do Claude Code dá `ConnectionTimeout`; o cofre só funcionava passando por uma VPS. O teste travou 90 s antes de eu perceber.

**Causa:** o proxy da sessão só deixa sair HTTPS; a porta 5432 não sai.

**Correção:** o Neon aceita SQL sobre HTTPS (`POST https://<host>/sql`, cabeçalho `Neon-Connection-String`), que passa pelo proxy. O `vault.py` ganhou esse transporte, escolhido sozinho quando a porta não abre. O rótulo do log vai por `set_config('application_name', ..., true)` no mesmo lote (transação).

**Regra:** teste a porta (`socket.create_connection(..., timeout=4)`) antes de abrir conexão de banco numa ferramenta que roda no sandbox, e dê um caminho por HTTPS. Em modo texto cru o Neon devolve tudo como texto (`t`/`f` no booleano): normalize na borda.

### 17. Cada consulta ao cofre levava 12 s: o teste da porta tentava todos os endereços
**Data:** 03/nov/2026 | **Impacto:** com o transporte automático, todo `vault.py` no sandbox esperava ~12,8 s antes de cair no HTTPS (a consulta em si leva 0,7 s). Percebido ao medir "acessar o cofre rapidamente".

**Causa:** `socket.create_connection(host, timeout=4)` tenta **cada** endereço do DNS com o timeout inteiro; com 3 rotas e a porta bloqueada, 3 x 4 s.

**Correção:** `_sondar` tenta no máximo 2 rotas com prazo **total** de 2,5 s, e a decisão fica em cache por 30 min num arquivo temporário (a 1ª chamada da sessão paga ~3 s; as outras ~0,65 s). Testes cobrem o prazo total, o cache, a expiração e o cache corrompido.

**Regra:** timeout de conexão é por endereço, não total. Em ferramenta que roda a cada comando, meça a latência de ponta a ponta e ponha prazo total e cache na decisão que não muda.

### 18. Prompt e catálogo corrigidos no Next não chegam ao Standby; promover com dump velho desfaz a correção
**Data:** 03/nov/2026 | **Impacto:** nenhum ainda (o Standby está em `record_only` e só tem um tenant de teste).

**Causa:** prompts (`tenant_prompt_profiles`), catálogo (`knowledge`) e ambiente são **dados**, não código. O Standby só os recebe restaurando um dump do Next (backup diário no R2), e o env dele é próprio. Em 03/nov o Next corrigiu prompt e catálogo de um tenant (campanha vencida cotada ao paciente, regra de escassez); um dump anterior a essas mudanças as traria de volta.

**Regra:** (1) **todo código do Next é espelhado aqui na mesma entrega** (ordem do chairman, 03/nov): o mesmo patch vale nos dois enquanto os arquivos forem idênticos (ver `OPERACAO.md`, "Paridade com o Next"); (2) antes de promover, usar um dump **posterior** à última mudança de prompt ou catálogo do Next e conferir as versões ativas em `tenant_prompt_profiles` e as campanhas em `knowledge`; (3) o histórico dessas mudanças está em `Genius-Next/docs/TRAPS.md` #85 a #97 (repositório privado); este repositório é público e não copia prompt, preço nem conversa (`TRAPS` #20).

### 19. O env do Next não serve no Standby: `OPENAI_BASE_URL` aponta para um roteador local da VPS 002
**Data:** 03/nov/2026 | **Impacto:** nenhum ainda. Teria derrubado todo turno do Standby (conexão recusada).

**Causa:** em 03/nov o agente de resposta do Next passou a chamar um roteador de LLM na própria 002 (`OPENAI_BASE_URL=http://127.0.0.1:8090/v1`, `OPENAI_API_KEY` = token do roteador, `REPLY_AGENT_MODEL=openai-chat:gpt-4o-mini`). Esse roteador escuta só em `127.0.0.1` e não existe no Render. O Standby fala com o Omni `gpt-pool` pelo OmniRoute.

**Regra:** ao copiar variáveis do Next para o Render, **não copiar** `OPENAI_BASE_URL`, `OPENAI_API_KEY` nem `REPLY_AGENT_MODEL`; manter o par do Omni. O mesmo vale para qualquer variável que aponte para `127.0.0.1` ou para IP da tailnet. Detalhe do roteador: `Genius-Next/docs/runbooks/RUNBOOK-ROUTER-OPENAI-FREE.md`.

### 20. Este repositório é público: nada de dado de cliente nele
**Data:** 03/nov/2026 | **Impacto:** nenhum (prevenção).

**Causa:** `geniu5-ai/standby` é público, e a regra "tudo o que muda no Next muda aqui" empurra para copiar documentos que citam preço, prompt e conversa de um cliente real (clínica).

**Regra:** espelhar aqui **código, testes que não leem dado real, regras e armadilhas genéricas**. Não copiar: prompts e catálogos de tenant, preços, conversas e telefones de pacientes, nem o harness que lê o banco de produção (`tests/promptfoo/kyrios` do Next). Esses ficam no `Genius-Next` (privado), e o que o Standby precisa saber de cada mudança entra como nota genérica (como as #18 e #19). Varrer o diff atrás de segredo e dado de cliente antes de abrir PR (`TRAPS` #14).

### 21. Chave do CPFHub estava fixa no código deste repositório público
**Data:** 04/nov/2026 | **Impacto:** o valor padrão de `os.getenv("CPFHUB_API_KEY", ...)` em `app/tools/procuracao.py` veio da cópia do Next e ficou no histórico do Standby, que é público: vai em todo clone e backup (era a pendência 10 da seção "Pendente" de `docs/OPERACAO.md`).

**Correção:** valor padrão removido (`os.getenv("CPFHUB_API_KEY", "")`), igual ao Next (`Genius-Next` `TRAPS` #101). Sem a variável no Render, a consulta de CPF falha com 401; o Standby não atende hoje o tenant que a usa.

**Falta (chairman):** rotacionar a chave no painel do CPFHub; tirar do código não apaga o histórico.

**Regra:** chave de API nunca é valor padrão de `os.getenv`. Varredura: `git grep -nE 'getenv\([^)]*, *"[A-Za-z0-9_-]{16,}"'`.

### 22. O snippet do console do QStash passa a URL antes do token, ao contrário do SDK
**Data:** 11/nov/2026 | **Impacto:** `QStash("https://qstash-us-east-1.upstash.io", "<token>")`, como o console mostra, não serve para o `qstash` 3.4: o primeiro argumento posicional é o token, então a URL seria usada como token.

**Causa:** a assinatura real é `QStash(token, *, retry=None, base_url=None)` (conferida com `inspect.signature`).

**Correção:** `QStash(token, base_url=url)` em `scripts/qstash_schedule.py`.

**Regra:** conferir a assinatura do SDK instalado antes de colar um snippet de console. O `url` do `Receiver` é comparado com o `sub` do JWT, que é o **destino do schedule**; configure `QSTASH_WAKE_URL` com a URL pública exata (`https://genius-next-serverless.onrender.com/v1/control/qstash-wake`), não a que o app enxerga atrás do proxy.

### 23. O OpenWA entrega o remetente como LID, não como telefone
**Data:** 11/nov/2026 | **Impacto:** `data.from` e `data.chatId` do webhook `message.received` vêm como `...@lid` (identificador privado do WhatsApp, `isLidSender: true`). O app indexa conversa, cliente e allowlist por telefone (`remoteJid` com `@s.whatsapp.net`); entregar o LID como está criaria um cliente novo por LID e nunca casaria com o cadastro.

**Causa:** o WhatsApp passou a usar LID para parte dos contatos; o OpenWA repassa o que recebe e só traz o `pushName` no evento.

**Correção:** o conector (`connectors/wa-bridge/`) resolve o LID por `GET /api/sessions/{id}/contacts/{lid}/phone` do próprio OpenWA (existe também `GET /contacts/{número}/lid`) e só então monta o `remoteJid`. LID sem resolução é ignorado, nunca entregue.

**Regra:** nenhum adaptador de gateway entrega identificador que não seja telefone ao app. Conferido em teste real (10/11/2026, OpenWA 0.30.0, motor Baileys).
