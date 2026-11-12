"""Extrator de fatos DEPOIS da resposta (24/out/2026): fora do caminho do paciente.

Pedir os fatos na mesma chamada da Julia piorou as regras duras da clínica (medicao de 24/out). Aqui um
modelo separado le a fala do paciente e a resposta ja enviada e devolve os fatos. Tolerante: qualquer
defeito (JSON ruim, tipo desconhecido, falha de rede) vira lista vazia; nunca levanta.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.contracts import FATO_TIPOS, FatoConversa
from app.reply.agent import INSTRUCAO_FATOS

logger = logging.getLogger(__name__)

_SISTEMA = (
    "Voce extrai FATOS de uma troca de WhatsApp entre um paciente e a recepcao de uma clinica. "
    + INSTRUCAO_FATOS.split("Os fatos nunca aparecem")[0].replace('Alem da resposta, preencha o campo "fatos" (lista, pode ser vazia) com o que ACONTECEU nesta troca', "Liste o que ACONTECEU nesta troca")
    + ' Responda SOMENTE com um JSON: {"fatos":[{"tipo":"...","detalhe":"..."}]}. Se nada aconteceu, {"fatos":[]}.'
)


def interpretar(bruto: str) -> list[FatoConversa]:
    """Aceita JSON cru, com cerca de codigo ou com texto em volta; descarta tipo desconhecido."""
    try:
        inicio, fim = bruto.index("{"), bruto.rindex("}") + 1
        itens = json.loads(bruto[inicio:fim]).get("fatos") or []
    except (ValueError, json.JSONDecodeError, AttributeError):
        return []
    fatos: list[FatoConversa] = []
    for item in itens[:8]:
        if not isinstance(item, dict):
            continue
        tipo = re.sub(r"\s+", "_", str(item.get("tipo") or "").strip().lower())
        if tipo in FATO_TIPOS:
            fatos.append(FatoConversa(tipo=tipo, detalhe=str(item.get("detalhe") or "")[:240]))  # type: ignore[arg-type]
    return fatos


def extrair_fatos(
    *, historico: str, fala_paciente: str, resposta_julia: str, settings: Any, timeout: float = 40.0
) -> list[FatoConversa]:
    chave = settings.OMNIROUTE_API_KEY.get_secret_value() if settings.OMNIROUTE_API_KEY else ""
    usuario = f"HISTORICO:\n{historico or '(vazio)'}\n\nPACIENTE: {fala_paciente}\nRECEPCAO (resposta enviada): {resposta_julia}"
    corpo = {
        "model": settings.OMNIROUTE_MODEL, "stream": False, "temperature": 0, "max_tokens": 300,
        "messages": [{"role": "system", "content": _SISTEMA}, {"role": "user", "content": usuario}],
    }
    try:
        r = httpx.post(
            f"{settings.OMNIROUTE_BASE_URL}/chat/completions", json=corpo, timeout=timeout,
            headers={"Authorization": f"Bearer {chave}", "User-Agent": "genius-next/extrator"},
        )
        r.raise_for_status()
        return interpretar(r.json()["choices"][0]["message"]["content"])
    except Exception:  # noqa: BLE001 - extrator nunca derruba o status
        logger.warning("extrator de fatos falhou", exc_info=True)
        return []
