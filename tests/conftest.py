"""Configuracao minima para os testes rodarem sem banco nem rede."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("HMAC_SECRET", "test-secret")

# Espelho do conftest do Next: os testes de fluxo montam um turno falso e exigem o motor "legacy";
# o Render do Standby define REPLY_ENGINE=agent (producao), e isso nao vale em teste.
os.environ.setdefault("REPLY_ENGINE", "legacy")
