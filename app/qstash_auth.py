"""Assinatura do QStash (``Upstash-Signature``) para o wake agendado.

O QStash chama o Standby por HTTP em horario marcado (schedule). Cada chamada traz um JWT HS256
assinado com a chave **atual** ou a **proxima** (a Upstash rotaciona sem janela de queda) e com o
hash SHA-256 do corpo. Este modulo so responde "a chamada e mesmo do QStash?": quem acorda o
worker e quem le o estado do banco continuam em ``app/wake.py`` e ``app/main.py``.

Fail-closed: sem cabecalho, sem chave, corpo que nao e UTF-8 ou qualquer erro de verificacao
devolve ``False``. Nao tem segredo nem estado proprio.
"""

from __future__ import annotations

from qstash import Receiver
from qstash.errors import SignatureError

HEADER = "Upstash-Signature"


def assinatura_valida(
    body: bytes,
    signature: str | None,
    *,
    current_key: str,
    next_key: str,
    url: str | None = None,
) -> bool:
    """True so se ``signature`` for um JWT do QStash valido para ``body``.

    ``url`` (o destino do schedule, ``sub`` do JWT) e conferido quando informado: impede que
    uma chamada assinada para outro destino do mesmo projeto QStash acorde este servico.
    """
    if not signature or not current_key or not next_key:
        return False
    try:
        texto = body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    try:
        Receiver(current_signing_key=current_key, next_signing_key=next_key).verify(
            signature=signature, body=texto, url=url or None
        )
    except SignatureError:
        return False
    return True
