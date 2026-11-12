"""Regressao do incidente de 03/nov/2026: objecao ("Vix", "Muito longe") reabriu o follow-up.

O responsavel por um paciente respondeu "Vix" e "Muito longe" sobre uma vacina; o
sistema nao viu recusa, abriu um ciclo completo e a 3a mensagem usou o angulo
``motivo_pessoal`` com frase de culpa. Estes testes travam: o sinal de objecao
(rules), a despedida, o teto por contato (24 h e 7 dias), a guarda de abertura do
ciclo, os angulos pos-objecao, o fim da escassez obrigatoria e a data de hoje no
contexto.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.durable import followup as fu
from app.reply import llm
from app.status.rules import (
    detect_status_by_rule,
    encerra_follow_up,
    is_deferral_or_closing,
    is_farewell,
    is_objection,
    llm_loss_vetado,
)
from tests.test_followup_guard import _OPEN_CLIENT, _FollowupSession

# --------------------------------------------------------------------------- #
# 1. Objecao de localizacao / distancia / custo / tempo
# --------------------------------------------------------------------------- #

OBJECOES = [
    # as duas respostas reais do incidente
    "Vix",
    "Muito longe",
    # variacoes e erros de digitacao
    "vixe",
    "VIXE!",
    "vixiii",
    "muito longe!",
    "mt longe",
    "Mora longe",
    "eu moro longe",
    "moro muito longe",
    "É longe",
    "e longe",
    "tá muito longe",
    "Não dá pra ir",
    "nao da pra ir ai",
    "não tenho como ir",
    "não consigo ir lá",
    "Fica muito longe de mim",
    "fica muito longe de casa",
    "longe demais",
    "Moro em outra cidade",
    "Muito longe, né?",
    "Vixe, muito longe",
    "Tá caro",
    "ta muito caro",
    "achei caro",
    "É caro demais",
    "Não tenho como pagar",
    "não cabe no meu orçamento",
    "Não tenho tempo",
    "to sem tempo",
]

NAO_OBJECOES = [
    # pergunta de endereco nunca e objecao
    "Onde fica",
    "Onde fica a clínica?",
    "onde fica?",
    "Qual o endereço?",
    "Como chego aí?",
    "Fica longe da rodoviária?",
    "Fica muito longe da rodoviária",
    "É longe da rodoviária?",
    "Fica longe do metrô, né?",
    # perguntas sobre custo/tempo
    "Tá caro?",
    "Muito longe?",
    "Quanto custa? Achei caro",
    "Tem tempo amanhã?",
    "Mais caro é qual?",
    # conflito de horario, nao recusa do servico
    "Não posso ir amanhã",
    "Não dá pra ir de manhã",
    "não consigo ir nessa semana",
    # conversa comum
    "Quero marcar",
    "Bom dia",
    "Quero saber mais sobre vacina",
    "Caro Dr, bom dia",
    "Vixe que legal, quero marcar",
    "",
    "   ",
]


@pytest.mark.parametrize("texto", OBJECOES)
def test_objecao_e_deferral_ou_closing(texto: str) -> None:
    assert is_objection(texto) is True
    assert is_deferral_or_closing(texto) is True
    assert encerra_follow_up(texto) is True


@pytest.mark.parametrize("texto", NAO_OBJECOES)
def test_nao_e_objecao(texto: str) -> None:
    assert is_objection(texto) is False
    assert is_deferral_or_closing(texto) is False


@pytest.mark.parametrize("texto", ["Vix", "Muito longe", "Tá caro", "Não tenho tempo"])
def test_objecao_nunca_vira_loss(texto: str) -> None:
    """Pausa o ciclo sem marcar loss definitivo: nem o regex nem o voto do LLM."""
    assert detect_status_by_rule(texto) is None
    assert llm_loss_vetado(texto) is True


# --------------------------------------------------------------------------- #
# 2. Despedida
# --------------------------------------------------------------------------- #

DESPEDIDAS = [
    "Ok obrigada",
    "ok, obrigada!",
    "Valeu",
    "tá bom, obrigado",
    "Muito obrigada pela atenção",
    "obrigadaaa",
    "Obrigado, até mais",
    "vlw",
    "Obg",
    "Ok obrigada 😊",
    "Tudo bem, obrigada",
]
NAO_DESPEDIDAS = [
    "ok",
    "Onde fica",
    "Obrigada, quanto custa?",
    "Obrigada! Quero marcar",
    "obrigada, vou marcar",
    "Obrigada pela informação, vou agendar amanhã",
    "bom dia",
    "",
]


@pytest.mark.parametrize("texto", DESPEDIDAS)
def test_despedida_encerra(texto: str) -> None:
    assert is_farewell(texto) is True
    assert encerra_follow_up(texto) is True


@pytest.mark.parametrize("texto", NAO_DESPEDIDAS)
def test_nao_e_despedida(texto: str) -> None:
    assert is_farewell(texto) is False
    assert encerra_follow_up(texto) is False


# --------------------------------------------------------------------------- #
# 3. Gate de continuacao e guarda de abertura do ciclo
# --------------------------------------------------------------------------- #


def _continua(**kw: Any) -> bool:
    session = _FollowupSession(client=_OPEN_CLIENT, lifecycle_state="scheduled", **kw)
    return fu._should_continue(session, "tenant-a", "5511999999999", 7, "turn-1")


@pytest.mark.parametrize("fala", ["Ok obrigada", "Agora não", "Não tenho interesse", "Não quero", "Não me manda mais"])
def test_ciclo_para_so_com_recusa_firme_ou_despedida(fala: str) -> None:
    assert _continua(last_inbound_text=[fala]) is False


@pytest.mark.parametrize(
    "fala", ["Vix", "Muito longe", "Tá caro", "Vou pensar", "Depois eu vejo", "Sem pressa", "Semana que vem eu vejo"]
)
def test_ciclo_segue_com_indecisao_adiamento_e_objecao(fala: str) -> None:
    """06/nov/2026 (chairman): quem esta indeciso, vai pensar ou objetou recebe insistencia leve."""
    assert _continua(last_inbound_text=[fala]) is True


def test_ciclo_segue_com_objecao_na_fala_anterior_e_para_com_recusa_na_anterior() -> None:
    assert _continua(last_inbound_text=["Ok", "Muito longe"]) is True
    assert _continua(last_inbound_text=["Ok", "Agora não"]) is False


def test_despedida_so_vale_na_ultima_fala() -> None:
    """Quem agradeceu e depois perguntou nao se despediu."""
    # _LAST_INBOUND_TEXT_SQL devolve a fala mais nova primeiro.
    assert _continua(last_inbound_text=["Qual o horário de sábado?", "ok obrigada"]) is True
    assert _continua(last_inbound_text=["ok obrigada", "Qual o horário de sábado?"]) is False


def test_onde_fica_nao_para_o_ciclo() -> None:
    assert _continua(last_inbound_text=["Onde fica"]) is True


def test_guarda_de_abertura_recusa_so_recusa_firme_e_despedida() -> None:
    def motivo(**kw: Any) -> str | None:
        session = _FollowupSession(client=_OPEN_CLIENT, **kw)
        return fu.motivo_para_nao_abrir_ciclo(session, "tenant-a", "5511999999999")

    assert motivo(last_inbound_text=["Agora não"]) == fu.MOTIVO_FALA_ENCERRA
    assert motivo(last_inbound_text=["Não tenho interesse"]) == fu.MOTIVO_FALA_ENCERRA
    assert motivo(last_inbound_text=["Ok obrigada"]) == fu.MOTIVO_FALA_ENCERRA
    # indecisao, adiamento e objecao abrem ciclo (insistencia leve)
    assert motivo(last_inbound_text=["Muito longe"]) is None
    assert motivo(last_inbound_text=["Vix"]) is None
    assert motivo(last_inbound_text=["Depois eu vejo"]) is None
    assert motivo(last_inbound_text=["Vou pensar"]) is None
    # Sem teto de envios por contato (removido em 05/nov/2026): quem perguntou algo abre ciclo.
    assert motivo(last_inbound_text=["Quero saber mais sobre vacina"], sent_24h=3, sent_7d=9) is None
    assert motivo(last_inbound_text=["Onde fica"]) is None
    assert motivo(last_inbound_text=["Quero saber mais sobre vacina"]) is None


class _Exec:
    def __init__(self, rowcount: int = 1) -> None:
        self.rowcount = rowcount


class _LifecycleSession(_FollowupSession):
    """Fake da transacao de abertura: registra o INSERT do lifecycle."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(client=_OPEN_CLIENT, **kw)
        self.inserts: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: Any, params: dict[str, Any]) -> Any:  # type: ignore[override]
        sql = str(statement)
        if "SELECT last_sequence" in sql:
            return _scalar(3)
        if "followup.v2_lifecycle_ensure" in sql:
            self.inserts.append((sql, params))
            return _Exec()
        return super().execute(statement, params)


def _scalar(value: Any) -> Any:
    from tests.test_followup_guard import _Result

    return _Result(scalar=value)


def _abre_ciclo(session: _LifecycleSession) -> bool:
    from app import db

    token = db._SESSION.set(session)
    try:
        return fu.ensure_follow_up_v2_lifecycle_txn(
            "tenant-a", "5511999999999@s.whatsapp.net", "5511999999999", "genius", "turn-1"
        )
    finally:
        db._SESSION.reset(token)


def test_abertura_registra_ciclo_stopped_com_motivo_e_nao_agenda() -> None:
    session = _LifecycleSession(last_inbound_text=["Agora não"])
    assert _abre_ciclo(session) is False
    assert len(session.inserts) == 1
    sql, params = session.inserts[0]
    assert "'stopped'" in sql and "stop_reason" in sql
    assert params["reason"] == fu.MOTIVO_FALA_ENCERRA


def test_abertura_normal_cria_ciclo_scheduled() -> None:
    session = _LifecycleSession(last_inbound_text=["Quero saber mais sobre vacina"])
    assert _abre_ciclo(session) is True
    sql, params = session.inserts[0]
    assert "'scheduled'" in sql and "reason" not in params


def test_start_nao_agenda_nada_quando_a_guarda_recusa(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cadeia inteira: ensure devolve False, nenhum despertar e agendado."""
    from tests.followup_chain import drive_follow_up

    sleeps: list[int] = []
    monkeypatch.setattr(
        fu, "load_follow_up_policy_step", lambda *_: fu.FollowUpPolicy.enabled_default("tenant-a")
    )
    monkeypatch.setattr(fu, "ensure_follow_up_v2_lifecycle_txn", lambda *_: False)
    drive_follow_up(
        monkeypatch, fu,
        "tenant-a", "5511999999999@s.whatsapp.net", "5511999999999", "genius", "turn-1",
        on_sleep=sleeps.append,
    )
    assert sleeps == []


def test_job_antigo_sem_novos_argumentos_continua_valido() -> None:
    """Compatibilidade: assinaturas publicas dos jobs/steps nao ganharam argumento obrigatorio."""
    import inspect

    for fn in (fu.start_follow_up_v2, fu.ensure_follow_up_v2_lifecycle_txn, fu.draft_follow_up_step):
        obrigatorios = [
            p.name for p in inspect.signature(fn).parameters.values()
            if p.default is inspect.Parameter.empty and p.kind is p.POSITIONAL_OR_KEYWORD
        ]
        assert "pos_objecao" not in obrigatorios and "data_atual" not in obrigatorios
    assert inspect.signature(fu.follow_up_context).parameters["data_atual"].default is None


# --------------------------------------------------------------------------- #
# 4. Angulos
# --------------------------------------------------------------------------- #

PRESSAO_OU_COMPARACAO = {
    "horario_agenda",
    "campanha_vigente",
    "motivo_pessoal",
    "autoridade_verdadeira",
}


def test_pos_objecao_exclui_angulos_de_pressao_e_comparacao() -> None:
    assert PRESSAO_OU_COMPARACAO <= fu._FOLLOWUP_ANGULOS_NAO_POS_OBJECAO
    for i in range(400):
        for attempt in (1, 2, 3):
            angulo = fu.angulo_da_vez("t", str(i), attempt, attempt - 1, pos_objecao=True)
            assert angulo[0] not in fu._FOLLOWUP_ANGULOS_NAO_POS_OBJECAO, angulo


def test_sem_objecao_o_sorteio_continua_o_mesmo() -> None:
    """Compatibilidade: sem objecao a escolha e' a de antes (mesma semente)."""
    assert fu.angulo_da_vez("t", "5588", 2, 1) == fu.angulo_da_vez("t", "5588", 2, 1, pos_objecao=False)


def test_contexto_marca_objecao_do_historico_e_filtra_angulo() -> None:
    history = [
        {"role": "customer", "text": "Quero saber da vacina de meningite"},
        {"role": "assistant", "text": "Posso te explicar como funciona a vacina."},
        {"role": "customer", "text": "Vix"},
        {"role": "assistant", "text": "Entendi."},
        {"role": "customer", "text": "Muito longe"},
    ]
    for i in range(200):
        ctx = fu.follow_up_context("t", str(i), 3, history, ["Entendi."], None)
        assert ctx["apos_objecao_do_paciente"] is True
        assert ctx["angulo_da_vez"]["id"] not in fu._FOLLOWUP_ANGULOS_NAO_POS_OBJECAO
    sem = fu.follow_up_context("t", "5588", 2, [{"role": "customer", "text": "Quero saber mais"}], [], None)
    assert "apos_objecao_do_paciente" not in sem


def test_onde_fica_no_historico_nao_marca_objecao() -> None:
    assert fu.historico_tem_objecao([{"role": "customer", "text": "Onde fica"}]) is False
    assert fu.historico_tem_objecao([{"role": "customer", "text": "Muito longe"}]) is True
    # fala do proprio atendente nunca conta como objecao do paciente
    assert fu.historico_tem_objecao([{"role": "assistant", "text": "fica muito longe de mim"}]) is False


def test_motivo_pessoal_proibe_comparar_e_traz_o_caso_real_como_exemplo_negativo() -> None:
    texto = dict(fu.FOLLOWUP_ANGULOS)["motivo_pessoal"]
    assert "PROIBIDO" in texto and "outras famílias/pessoas fazem X mesmo com" in texto
    assert "Exemplo NEGATIVO" in texto and "mesmo morando mais longe" in texto


def test_regras_somam_proibicao_de_comparacao_e_pos_objecao_so_com_sinal() -> None:
    sem = fu.follow_up_extra_rules(2)
    assert fu._REGRA_SEM_COMPARACAO in sem and fu._REGRA_POS_OBJECAO not in sem
    com = fu.follow_up_extra_rules(2, pos_objecao=True, pos_handoff=True)
    assert fu._REGRA_POS_OBJECAO in com
    assert com[-1] is fu._REGRA_POS_HANDOFF  # pos-handoff continua por ultimo


def test_ultima_tentativa_continua_porta_aberta_mesmo_pos_objecao() -> None:
    assert fu.angulo_da_vez("t", "5588", 4, 3, pos_objecao=True)[0] == "porta_aberta"


# --------------------------------------------------------------------------- #
# 5. Sem escassez obrigatoria
# --------------------------------------------------------------------------- #

_FRASES_DE_ESCASSEZ = (
    "horários da semana são poucos",
    "agenda do especialista costuma encher",
    "agenda costuma encher",
    "poucos horários",
    "últimas vagas",
)


@pytest.mark.parametrize("attempt", [1, 2, 3, 4])
def test_regras_por_tentativa_nao_mandam_escassez(attempt: int) -> None:
    regra = fu.FOLLOWUP_RULES_BY_ATTEMPT[attempt].casefold()
    for frase in _FRASES_DE_ESCASSEZ:
        assert frase.casefold() not in regra, (attempt, frase)


def test_tentativa_2_mantem_motivo_novo_e_condiciona_a_escassez_ao_perfil() -> None:
    regra = fu.FOLLOWUP_RULE_ATTEMPT_2
    assert "MOTIVO NOVO" in regra
    assert "perfil do tenant" in regra and "Não invente escassez" in regra


# --------------------------------------------------------------------------- #
# 6. data_atual no contexto
# --------------------------------------------------------------------------- #


def test_contexto_inclui_data_atual_do_atendimento(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fu, "_data_atual_negocio", lambda: "03/11/2026 (terça-feira)")
    ctx = fu.follow_up_context("t", "5588", 1, [], [], None)
    assert ctx["data_atual"] == "03/11/2026 (terça-feira)"
    explicita = fu.follow_up_context("t", "5588", 1, [], [], None, data_atual="04/11/2026 (quarta-feira)")
    assert explicita["data_atual"] == "04/11/2026 (quarta-feira)"


def test_data_atual_usa_o_mesmo_helper_do_atendimento() -> None:
    assert fu._data_atual_negocio is llm._data_atual_negocio
    assert fu.follow_up_context("t", "5588", 1, [], [], None)["data_atual"]


def test_guia_de_entrada_do_followup_explica_data_atual_e_objecao(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm, "active_prompt_for", lambda tenant_id, purpose: "texto do tenant")
    monkeypatch.setattr(llm, "_override_prompt", lambda tenant_id: None)
    guia = json.loads(llm._build_system_prompt("t", purpose="followup"))["input_guide"]
    assert "data_atual" in guia and "apos_objecao_do_paciente" in guia
    ctx = fu.follow_up_context(
        "t", "5588", 2, [{"role": "customer", "text": "Muito longe"}], [], None, pos_handoff=True
    )
    # toda chave do contexto tem explicacao no guia (nenhum campo sem legenda)
    assert set(ctx) - {"client", "informacoes_da_empresa"} <= set(guia)
