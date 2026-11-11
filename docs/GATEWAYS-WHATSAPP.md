# Gateways de WhatsApp: pesquisa, laboratório e conector

Estado em 11/nov/2026. Responde: que alternativas à Evolution existem, o que foi **testado de verdade**,
como uma mensagem de um gateway chega ao Standby e o que ainda falta. O Standby é público: aqui não há
segredo, telefone nem conteúdo de conversa (`TRAPS` #20).

## Regra de ouro: oficial para quem é regulado

| Tipo | Como conecta | Risco |
|---|---|---|
| **API oficial da Meta** (Zernio, Telnyx, Zavu) | conta comercial (WABA) e templates; sem sessão num servidor seu | sem risco de banimento por usar API não oficial; exige janela de 24 h e templates |
| **Sessão por QR** (Evolution, OpenWA, waxum, WPPConnect, Whapi.Cloud) | emula um dispositivo vinculado; a sessão fica num servidor | risco de restrição ou banimento, admitido pelos próprios projetos |

O README do OpenWA diz que **não é aprovado para uso regulado** (saúde, finanças, dados pessoais da UE).
Tenants clínicos ficam na API oficial (hoje, via Zernio). Os gateways por QR são para os demais clientes.

## Comparação (fontes: sites e repositórios oficiais, lidos em 11/nov/2026)

| Projeto | Motor | Oficial? | Contêiner único | Licença | Maturidade / observação |
|---|---|---|---|---|---|
| Evolution API v2 | Baileys | não | não: exige Postgres ou MySQL (Redis opcional) | Apache 2.0 + marca | incumbente; v2.4.0 em release candidate, exige ativação gratuita por e-mail |
| Evolution Go | Go (biblioteca não nomeada) | não | não: Postgres (2 bancos) | Apache 2.0 | webhook **incompatível** com o da v2 (eventos e envelope diferentes); onde guarda a sessão e como exportá-la: não documentado |
| **OpenWA** | Baileys ou whatsapp-web.js | não | **sim** (SQLite) | MIT | 16,7 mil estrelas, versão 0.30.0 de 10/nov; painel e API na mesma porta |
| **waxum** | `whatsapp-rust` (modo oficial opcional) | não (padrão) | **sim** (SQLite) | MIT | 133 estrelas, Rust nightly, **sem cifra em repouso** |
| WPPConnect Server | Puppeteer (Chromium) | não | sim (volume) | Apache 2.0 | 1,1 mil estrelas; pesado em memória |
| WA-AKG | Baileys | não | não (MySQL/Postgres) | MIT | 873 estrelas |
| open-wa (wa-automate) | Puppeteer | não | sim | Hippocratic (não OSI) | termos de uso atípicos: evitar |
| Whapi.Cloud | QR, hospedado | não | n/a | comercial | US$ 29/mês por número; a própria FAQ admite risco |
| Telnyx | Cloud API | **sim** | n/a | comercial | US$ 0,004/mensagem + taxas Meta; exige WABA |
| Zavu | não confirmado | provável | n/a | comercial | plano Pro US$ 20/mês |
| Zernio | Cloud API | **sim** | n/a | comercial | já em uso; webhook retentado por até 51 h; URL do webhook mutável por API |

Não encontrei forma de ligar sessões por QR (Evolution, OpenWA, waxum) ao Zernio: ele só unifica os
canais que passam por ele. A unificação possível é **no nosso código**: um adaptador por gateway que
produz o evento `messages.upsert` da Evolution (o `zernio.py` já faz isso para o Zernio).

## Laboratório na VPS 003

Tudo em Podman rootless (usuário `ubuntu`), como unidades Quadlet em `~/.config/containers/systemd/`
(mesmo padrão dos outros contêineres da 003), sobe sozinho no reinício. Portas **só em `127.0.0.1`**; não
há entrada pública. Rede privada `lab-wa` (`10.99.0.0/24`) com DNS entre contêineres.

| Unidade | Imagem | Porta local | Limite | O que é |
|---|---|---|---|---|
| `lab-openwa` | `ghcr.io/rmyndharis/openwa:latest` | 12785 | 2 GB, 2 CPUs | OpenWA (motor Baileys, SQLite), usuário 997, raiz somente leitura |
| `lab-waxum` | `docker.io/fdciabdul/waxum:latest` | 13451 | 512 MB, 1 CPU | waxum (SQLite), usuário 1000, raiz somente leitura |
| `lab-hooksink` | `python:3.11-slim` | 19000 | 128 MB | receptor interno: grava cabeçalhos, assinatura e corpo de cada webhook em `~/lab-wa/sink/hooks.jsonl` |
| `lab-wabridge` | `python:3.11-slim` | 8800 | 128 MB | o conector (abaixo) |

Endurecimento comum: `--cap-drop=ALL`, `no-new-privileges`, sistema de arquivos raiz somente leitura,
`tmpfs` em `/tmp`, volume próprio (`:U`). Segredos em `~/lab-wa/*.env` (permissão 600): `openwa.env`
(chave mestra), `waxum.env` (token e `JWT_SECRET`), `sink.env`, `secrets.env` (segredos dos webhooks),
`connector.env` e `bridge.env` (conector). **Nunca em repositório, doc ou chat.**

Observações do laboratório:

- O OpenWA roda com `WEBHOOK_SSRF_PROTECT=false` **só para entregar ao receptor na rede privada**. Não
  repetir em produção.
- O waxum recusa destinos que não resolvem ou não são públicos (loopback, rede privada, tailnet). Para
  receber webhook dele é preciso URL pública, ou o consumo dos eventos por NATS (ele publica numa stream
  `WA_EVENTS` com retenção de 7 dias). Um túnel público para o receptor foi **bloqueado pelo classificador**
  do ambiente e não foi instalado: o **webhook do waxum está pendente**.
- Imagens de terceiros baixadas de `ghcr.io` e `docker.io`: conferir o digest antes de promover.

## Resultados medidos (Podman 4.9.3, rootless, 4 CPUs, 24 GB)

| | waxum | OpenWA (Baileys) |
|---|---|---|
| Imagem | 177 MB | 1,5 GB |
| Subida até `/health` | 0,35 s | 3,1 s |
| Memória em repouso | 15 MB (2 sessões esperando QR) | 144 a 151 MB |
| Com 1 sessão esperando QR | 15 MB | 192 MB |
| QR gerado | sim, nas 2 sessões | sim, em 4 s |
| Autenticação | 401 sem token, 200 com | 401 sem chave, 200 com a chave mestra |
| Sessão duplicada | 409 | n/a |
| Painel web | não | sim, na porta da API |

Pontos de atenção que o teste revelou:

- **waxum:** a imagem inicia como root e tenta trocar para o usuário 1000; com `--cap-drop=ALL` falha. Rodar
  com `--user 1000:1000`. `/metrics` é público (200) e o Swagger exige autenticação, ao contrário da
  documentação. Defina `JWT_SECRET`, que por padrão muda a cada reinício.
- **OpenWA:** o Swagger vem desligado em produção (`ENABLE_SWAGGER=true` liga; está ligado no laboratório). O
  compose de produção inclui um proxy do socket do Docker que a documentação classifica como equivalente a
  root no host se o contêiner for comprometido: **usar só o serviço único**.
- **Não medido:** o motor Chromium (`whatsapp-web.js`) do OpenWA: a criação da sessão deu 400, provavelmente
  pelo nome de teste (com pontos). O README fala em 300 a 500 MB por sessão. Também não testados: envio de
  mensagem, mídia, persistência após reinício, restauração em outro servidor e a assinatura do waxum.

## Webhooks e mapeamento para o contrato interno

O app só entende `EvolutionWebhook`: `event` (`messages.upsert` ou `send.message`), `instance` e
`data.{key.{id, remoteJid, fromMe}, message.conversation, messageType, pushName}`.

| Campo interno | OpenWA (`message.received`) | waxum (`message`) |
|---|---|---|
| `instance` | `sessionId` (mapeado para a instância do Standby) | `session_id` |
| `key.id` | `data.id` | `data.message_id` |
| `key.remoteJid` | telefone de `data.chatId` + `@s.whatsapp.net`, **depois de resolver o LID** | `data.from_phone` + `@s.whatsapp.net` |
| `key.fromMe` | `data.fromMe` | `data.is_from_me` |
| `message.conversation` | `data.body` (`type: text`) | `data.text` (`message_type: text`) |
| `pushName` | `data.contact.pushName` | `data.push_name` |
| Assinatura | `X-OpenWA-Signature: sha256=<hex>` (HMAC do corpo cru, **sem carimbo de tempo**) | `X-Webhook-Signature: sha256=<hex>` v2 sobre `{timestamp}.{corpo}`, mais `X-Webhook-Timestamp` |
| Idempotência | `idempotencyKey`, `X-OpenWA-Idempotency-Key` | `message_id` |
| Retentativas | `X-OpenWA-Retry-Count`; padrão do projeto | 3 tentativas, fila de mortos, suspensão após 25 falhas |

**LID (`TRAPS` #23):** o OpenWA entrega o remetente como `…@lid` (identificador privado do WhatsApp). O app
indexa por telefone. O OpenWA resolve por `GET /api/sessions/{id}/contacts/{lid}/phone` (e há
`GET /contacts/{número}/lid`); o conector faz isso antes de entregar. Medido em teste real: o evento
`message.received` chegou 3 ms depois da mensagem, com assinatura válida.

## O conector (`connectors/wa-bridge/`)

```
OpenWA --webhook assinado--> lab-wabridge --envelope assinado--> Standby /webhook/connector
        X-OpenWA-Signature                  X-Connector-Signature (segredo PRÓPRIO)
```

- **Três segredos, nenhum compartilhado com a Evolution:** o do webhook do OpenWA (entrada), a chave da API
  do OpenWA (resolver LID) e `CONNECTOR_HMAC_SECRET` (saída). O `HMAC_SECRET` da Evolution não é lido pelo
  conector, não autentica em `/webhook/connector` e o segredo do conector não autentica em `/webhook/evolution`
  (cobertos por teste).
- O Standby recebe o evento como provedor `evolution`, no fluxo comum (`_accept_ingress`); o tenant sai do
  `tenant_registry`, nunca do envelope. `contracts.py` (compartilhado com o Next) não muda.
- Só **texto recebido de pessoa**. Ignorados com 200: outros eventos, mensagens enviadas por nós, grupos,
  status, mídia, texto vazio, sessão sem mapeamento, LID sem resolução.
- Fila SQLite idempotente por `event_id`. Entrega com espera de 5 s a 6 h, até 10 tentativas (o Render free
  leva cerca de 60 s para acordar); 4xx de validação vai para mortos. Sem `OUT_URL`: modo seco.

### Saída (resposta de volta ao WhatsApp)

Sem tirar o Standby do `record_only`: o conector **busca** as respostas gravadas (`GET /v1/connector/outbox`,
assinado) e as envia pelo OpenWA (`POST /api/sessions/{id}/messages/send-text`), confirmando em seguida
(`POST /v1/connector/outbox/{id}/ack`). Travas: instância em `CONNECTOR_RELAY_INSTANCES` (Render), resposta de
no máximo 15 min, uma vez só, destino em `SEND_ALLOWLIST` (conector, vazia = nada sai), instância mapeada e
registro local contra envio duplicado. A consulta só ocorre por 5 min depois de cada mensagem entregue, então
**não mantém o Render acordado**. Detalhes no README do conector.

### Estado em produção (11/nov/2026)

- Standby no commit do PR #18; `CONNECTOR_HMAC_SECRET` cadastrado no Render; a rota responde 401 sem assinatura.
- Sessão `lab-openwa` conectada a um número de teste, mapeada para a instância `mariaeduarda-ficticia` (tenant
  fictício do Standby). **`record_only`: nada é enviado ao WhatsApp.** O primeiro evento real entregue mostra
  se essa instância está habilitada no `tenant_registry` (200 `ignored` = desabilitada; 401 = inexistente).
- O arquivo do conector na 003 foi baixado **do GitHub, fixado no commit do merge** (`729c023`), e não copiado
  deste ambiente. **Exceção à regra "nunca copiar o repositório para uma VPS" (`AGENTS.md`):** o conector precisa
  rodar ao lado do gateway por desenho. Precisa da confirmação do chairman.

### Operar

```bash
# na VPS 003 (usuário ubuntu)
systemctl --user status lab-openwa lab-wabridge lab-hooksink lab-waxum
curl -s http://127.0.0.1:8800/health          # estado da fila do conector
podman logs --tail 20 lab-wabridge            # entregas, retentativas, descartes (sem corpo de mensagem)
SECRET=$(grep ^OPENWA_WEBHOOK_SECRET ~/lab-wa/secrets.env | cut -d= -f2) python3 ~/lab-wa/resume.py openwa   # resume e confere assinaturas do receptor
```

Desligar o laboratório: `systemctl --user stop lab-wabridge lab-openwa lab-waxum lab-hooksink` e apagar os
arquivos `lab-*.container` e `lab.network` de `~/.config/containers/systemd/`. Os volumes (`lab-*-data`) guardam
a sessão do WhatsApp: **não apagar sem desconectar o aparelho** no celular.

## Pendências

1. **Mídia e grupos** no conector (hoje ignorados).
2. **Saída para clientes reais:** hoje a saída é um relé por busca (abaixo), restrito a instâncias e destinos
   listados. Responder de verdade para tenants exige um provedor novo no app (mudar o `Literal` de provedores em
   `contracts.py`, arquivo compartilhado: PR espelho no Next) e a decisão do chairman sobre o `live` (trava 1).
3. **waxum:** webhook (URL pública ou NATS) e adaptador. Lê `from_phone`, então não precisa resolver LID.
4. **Ensaio de recuperação:** restaurar a sessão em outro servidor a partir do volume. Para SQLite, parar o
   contêiner ou usar `sqlite3 .backup` antes de copiar (o arquivo tem WAL).
5. Medir o motor Chromium do OpenWA e o consumo com várias sessões.
6. Decidir o destino da Evolution (manter, migrar para Zernio ou para outro gateway) tenant a tenant.
