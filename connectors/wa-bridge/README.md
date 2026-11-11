# wa-bridge: conector WhatsApp (OpenWA) para o Standby

Um arquivo, só biblioteca padrão do Python (3.11+). Fica **ao lado do gateway** (na mesma rede de
contêineres), não no Render, e entrega ao Standby por HTTPS de saída. Não há entrada pública.

```
OpenWA --webhook assinado--> wa-bridge --envelope assinado--> Standby /webhook/connector
        X-OpenWA-Signature             X-Connector-Signature
```

## Por que um conector separado

- O app só entende o formato `messages.upsert` da Evolution e indexa conversas por **telefone**.
- O OpenWA entrega o remetente como **LID** (`...@lid`, identificador privado do WhatsApp). O
  conector resolve para telefone pela API do próprio OpenWA antes de entregar (`TRAPS` #23).
- O gateway só alcança o que está na rede dele; o app roda no Render. O conector faz a ponte e
  guarda numa fila SQLite, porque o Render free leva ~60 s para acordar.
- **Três segredos, nenhum compartilhado com a Evolution:** o do webhook do OpenWA (entrada), a
  chave da API do OpenWA (resolver LID) e `CONNECTOR_HMAC_SECRET` (saída). O `HMAC_SECRET` da
  Evolution não é usado nem lido pelo conector, e a rota `/webhook/connector` do app não aceita ele.

## Configuração (só ambiente)

| Variável | Para quê |
|---|---|
| `OPENWA_WEBHOOK_SECRET` | segredo cadastrado no webhook do OpenWA (obrigatório) |
| `OPENWA_API_URL`, `OPENWA_API_KEY` | API do OpenWA, para resolver LID em telefone |
| `OPENWA_SESSION_MAP` | `sessionId:instancia,...`: qual instância do Standby cada sessão representa |
| `OUT_URL`, `OUT_SECRET` | `https://<standby>/webhook/connector` e o `CONNECTOR_HMAC_SECRET`. Sem `OUT_URL`: **modo seco** (grava em `out.jsonl`, não envia) |
| `OUT_TIMEOUT` | segundos por entrega (padrão 90, cobre o cold start do Render) |
| `BRIDGE_LISTEN`, `DATA_DIR`, `LOG_LEVEL` | `0.0.0.0:8800`, `/data`, `INFO` |

Rodar: `python wa_bridge.py`. Saúde: `GET /health` (estado da fila). Webhook do gateway:
`POST /in/openwa`.

## O que entra e o que é ignorado

Só **texto recebido de uma pessoa**. Ignorados (200, sem erro): eventos que não são
`message.received`, mensagens enviadas por nós, grupos, status, mídia, texto vazio, sessão sem
mapeamento e LID que não resolve. Mídia e grupos ficam para uma próxima versão.

## Entrega

Fila SQLite idempotente por `event_id` (a reentrega do gateway não duplica). Respostas 2xx
concluem; 5xx, 408/425/429 e 401/403 (segredo ainda errado) tentam de novo com espera crescente
(5 s a 6 h, até 10 tentativas); os demais 4xx vão para mortos. O Standby responde 200 `ignored` para
tenant desabilitado, o que também conclui.

## Testes

`pytest tests/test_wa_bridge.py tests/test_connector_webhook.py`. O envelope gerado é validado
contra o `ConnectorEnvelope` e o `EvolutionWebhook` reais do app, e a assinatura de saída contra o
verificador do Standby.
