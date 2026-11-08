"""Relé TCP local para o Postgres de teste da VPS 003 (docs/OPERACAO.md, "Suíte completa").

O Postgres da 003 só escuta em 127.0.0.1. Cada conexão local vira uma sessão `tailscale ssh` que roda
`nc 127.0.0.1 5432` lá: nenhum listener permanente no VPS, e o repositório nunca vai para a VPS.
Uso: python scripts/pg_relay_003.py &   (escuta em 127.0.0.1:15432; PG_RELAY_PORT muda a porta)
Precisa do tailscale conectado (TS_SOCKET, padrão /run/tailscale.sock). Sem segredos aqui.
"""

import asyncio
import os

TS = os.environ.get("TS_BIN", "/root/.local/bin/tailscale")
SOCK = os.environ.get("TS_SOCKET", "/run/tailscale.sock")
PORT = int(os.environ.get("PG_RELAY_PORT", "15432"))
REMOTE = "nc -q1 127.0.0.1 5432"

async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    finally:
        try:
            writer.close()
        except Exception:
            pass

async def handle(client_reader, client_writer):
    proc = await asyncio.create_subprocess_exec(
        TS, f"--socket={SOCK}", "ssh", "root@003", REMOTE,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.gather(
        pipe(client_reader, proc.stdin),
        pipe(proc.stdout, client_writer),
        return_exceptions=True,
    )
    try:
        proc.kill()
    except ProcessLookupError:
        pass

async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", PORT)
    async with server:
        await server.serve_forever()

asyncio.run(main())
