"""Auto-aprendizado do Next (esboço). Módulo separado, assíncrono, só leitura por padrão.

Ver docs/specs/JUIZ-E-SELF-LEARNING.md. Nada daqui é chamado pelo fluxo de resposta nem
agendado: é o esboço do REFLECT do Next, feito para ser testado antes de ganhar job.

Peças:
- ``models``   : contratos (Outcome, Insight) em pydantic, sem dado de paciente além do texto já
                 apagado de telefone pelo chamador.
- ``reflect``  : monta o prompt de REFLECT, chama o LLM (OmniRoute) e faz o parse defensivo.
- ``store``    : gravação. Protocolo + implementação em memória. Gravação em Postgres não existe ainda.
- ``runner``   : orquestra coleta -> reflexão -> gravação. ``dry_run=True`` é o padrão e nunca grava.
"""

from app.learning.models import Insight, Outcome
from app.learning.runner import run_reflection

__all__ = ["Insight", "Outcome", "run_reflection"]
