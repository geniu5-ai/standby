"""Acesso a dados do auto-aprendizado. Tudo síncrono com ``@transaction`` (convenção do Next);
o código assíncrono chama via ``asyncio.to_thread``.

Leitura de conversa é só aqui e só em memória: o texto é redigido (``privacy.redigir``) antes de sair
desta camada e nenhuma tabela nova guarda texto de cliente. Ver migrations/093_learning.sql.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from app.db import sql_session, transaction
from app.learning.models import Desfecho, Insight, Outcome
from app.learning.privacy import redigir

logger = logging.getLogger(__name__)

METRICA_PADRAO = "TurnRelevancy"

_ACAO_PARA_DESFECHO: dict[str, Desfecho] = {
    "send_text": "enviado",
    "handoff": "handoff",
    "suppressed_trivial": "trivial",
}
_STATUS_PARA_DESFECHO: dict[str, Desfecho] = {"win": "ganho", "loss": "perdido", "block": "bloqueado"}

_TABELA_EXISTE_SQL = text("SELECT to_regclass(:nome) IS NOT NULL")

# Notas baixas primeiro (onde ha mais a aprender), depois turnos recentes sem nota.
_OUTCOMES_TMPL = """
    /* learning.outcomes */
    SELECT t.id AS turn_id, t.tenant_id, t.decision,
           (SELECT string_agg(e.text_content, E'\\n' ORDER BY e.conversation_sequence)
              FROM genius_next.events AS e
             WHERE e.claimed_turn_id = t.id AND e.direction = 'inbound' AND e.text_content IS NOT NULL) AS cliente,
           (SELECT o.payload->>'text'
              FROM genius_next.outbox AS o
             WHERE o.turn_id = t.id AND o.kind = 'customer'
             ORDER BY o.ordinal LIMIT 1) AS resposta,
           {nota} AS nota
    FROM genius_next.turns AS t
    {join}
    WHERE t.tenant_id = :tenant AND t.status = 'completed'
      AND t.completed_at >= now() - make_interval(hours => :horas)
      AND t.decision IS NOT NULL
    ORDER BY {ordem} t.completed_at DESC
    LIMIT :limite
    """
_JOIN_NOTA = "LEFT JOIN genius_next.turn_judgments AS j ON j.turn_id = t.id AND j.metric = :metrica"


def _desfecho(decision: dict[str, Any]) -> Desfecho | None:
    """None = turno que nao ensina nada (equipe no comando, acao desconhecida)."""
    acao = decision.get("action")
    if acao == "suppressed_by_control":
        return None
    status = decision.get("customer_status")
    if status in _STATUS_PARA_DESFECHO:
        return _STATUS_PARA_DESFECHO[status]
    return _ACAO_PARA_DESFECHO.get(str(acao))


@transaction(isolation_level="READ COMMITTED")
def tabela_existe(nome: str) -> bool:
    return bool(sql_session().execute(_TABELA_EXISTE_SQL, {"nome": f"genius_next.{nome}"}).scalar())


@transaction(isolation_level="READ COMMITTED")
def tenants_com_movimento(horas: int, minimo: int) -> list[str]:
    rows = sql_session().execute(
        text(
            """
            /* learning.tenants */
            SELECT tenant_id FROM genius_next.turns
            WHERE status = 'completed' AND completed_at >= now() - make_interval(hours => :horas)
            GROUP BY tenant_id HAVING count(*) >= :minimo ORDER BY count(*) DESC
            """
        ),
        {"horas": horas, "minimo": minimo},
    )
    return [r[0] for r in rows]


@transaction(isolation_level="READ COMMITTED")
def coletar_outcomes(tenant_id: str, horas: int, limite: int) -> list[Outcome]:
    session = sql_session()
    com_nota = bool(session.execute(_TABELA_EXISTE_SQL, {"nome": "genius_next.turn_judgments"}).scalar())
    sql = text(
        _OUTCOMES_TMPL.format(
            nota="j.score" if com_nota else "NULL::numeric",
            join=_JOIN_NOTA if com_nota else "",
            ordem="j.score ASC NULLS LAST," if com_nota else "",
        )
    )
    params: dict[str, Any] = {"tenant": tenant_id, "horas": horas, "limite": limite}
    if com_nota:
        params["metrica"] = METRICA_PADRAO
    out: list[Outcome] = []
    for row in session.execute(sql, params).mappings():
        desfecho = _desfecho(row["decision"] or {})
        cliente, resposta = redigir(row["cliente"]), redigir(row["resposta"])
        if desfecho is None or not cliente or not resposta:
            continue
        out.append(
            Outcome(
                turno_id=row["turn_id"],
                tenant_id=row["tenant_id"],
                desfecho=desfecho,
                nota_juiz=float(row["nota"]) if row["nota"] is not None else None,
                texto_cliente=cliente[:2000],
                resposta=resposta[:2048],
            )
        )
    return out


@transaction(isolation_level="READ COMMITTED")
def gravar_nota(
    tenant_id: str, turn_id: str, metrica: str, nota: float, limiar: float, modelo: str
) -> None:
    """Nota do juiz por turno. Sem conteudo. Reentrega do job nao duplica (UNIQUE turn_id+metrica)."""
    sql_session().execute(
        text(
            """
            /* learning.nota */
            INSERT INTO genius_next.turn_judgments (tenant_id, turn_id, metric, score, threshold, passed, judge_model)
            VALUES (:t, :turn, :m, :s, :l, :p, :mod)
            ON CONFLICT (turn_id, metric) DO NOTHING
            """
        ),
        {"t": tenant_id, "turn": turn_id, "m": metrica, "s": round(nota, 3), "l": round(limiar, 3), "p": nota >= limiar, "mod": modelo},
    )


@transaction(isolation_level="READ COMMITTED")
def iniciar_run(tenant_id: str, job_id: int | None, horas: int, dry_run: bool, modelo: str) -> int:
    return int(
        sql_session()
        .execute(
            text(
                """
                /* learning.run_inicio */
                INSERT INTO genius_next.learning_runs (tenant_id, job_id, window_hours, dry_run, llm_model)
                VALUES (:t, :j, :h, :d, :m) RETURNING id
                """
            ),
            {"t": tenant_id, "j": job_id, "h": horas, "d": dry_run, "m": modelo},
        )
        .scalar_one()
    )


@transaction(isolation_level="READ COMMITTED")
def finalizar_run(
    run_id: int, status: str, outcomes: int = 0, insights: int = 0, salvos: int = 0, erro: str | None = None
) -> None:
    sql_session().execute(
        text(
            """
            /* learning.run_fim */
            UPDATE genius_next.learning_runs
               SET status = :s, outcomes = :o, insights = :i, saved = :v, error_kind = :e, finished_at = now()
             WHERE id = :id
            """
        ),
        {"s": status, "o": outcomes, "i": insights, "v": salvos, "e": erro, "id": run_id},
    )


@transaction(isolation_level="READ COMMITTED")
def gravar_insights(tenant_id: str, run_id: int, insights: list[Insight]) -> int:
    """Proposta, nunca ativa: nasce ``proposed``. Titulo repetido no tenant e ignorado."""
    session = sql_session()
    novos = 0
    for i in insights:
        r = session.execute(
            text(
                """
                /* learning.insight */
                INSERT INTO genius_next.learning_insights (tenant_id, run_id, title, body, layer, tags, evidence_turn_ids)
                VALUES (:t, :run, :ti, :b, :l, :tags, :ev)
                ON CONFLICT (tenant_id, title) DO NOTHING RETURNING id
                """
            ),
            {"t": tenant_id, "run": run_id, "ti": i.titulo, "b": i.corpo, "l": i.camada, "tags": i.tags, "ev": i.evidencias},
        )
        novos += 1 if r.first() else 0
    return novos


@transaction(isolation_level="READ COMMITTED")
def listar_insights(tenant_id: str | None, status: str | None, limite: int = 50) -> list[dict[str, Any]]:
    rows = sql_session().execute(
        text(
            """
            /* learning.listar */
            SELECT id, tenant_id, title, body, layer, tags, evidence_turn_ids, status, decided_by, created_at
            FROM genius_next.learning_insights
            WHERE (CAST(:t AS text) IS NULL OR tenant_id = :t) AND (CAST(:s AS text) IS NULL OR status = :s)
            ORDER BY created_at DESC LIMIT :n
            """
        ),
        {"t": tenant_id, "s": status, "n": limite},
    )
    return [dict(r) for r in rows.mappings()]


@transaction(isolation_level="READ COMMITTED")
def decidir_insight(insight_id: int, status: str, por: str) -> bool:
    """So humano chama isto (CLI). ``proposed`` -> ``approved`` ou ``rejected``."""
    if status not in ("approved", "rejected"):
        raise ValueError("status invalido")
    r = sql_session().execute(
        text(
            """
            /* learning.decidir */
            UPDATE genius_next.learning_insights
               SET status = :s, decided_by = :por, decided_at = now()
             WHERE id = :id AND status = 'proposed' RETURNING id
            """
        ),
        {"s": status, "por": por, "id": insight_id},
    )
    return r.first() is not None


@transaction(isolation_level="READ COMMITTED")
def promover_aprovados(limite: int = 20) -> int:
    """Insight aprovado vira licao (dedup e embedding do ``lessons``). So o que um humano aprovou."""
    from app.agent.lessons import save_lesson

    session = sql_session()
    rows = session.execute(
        text(
            """
            /* learning.promover_busca */
            SELECT id, tenant_id, title, body FROM genius_next.learning_insights
            WHERE status = 'approved' ORDER BY decided_at LIMIT :n FOR UPDATE SKIP LOCKED
            """
        ),
        {"n": limite},
    ).mappings().all()
    promovidos = 0
    for r in rows:
        licao = save_lesson(
            session, tenant_id=r["tenant_id"], segmento=None, content=f"{r['title']}: {r['body']}", confidence=0.8
        )
        session.execute(
            text("UPDATE genius_next.learning_insights SET status = 'promoted', lesson_id = :l WHERE id = :id"),
            {"l": licao.id if licao else None, "id": r["id"]},
        )
        promovidos += 1
    return promovidos


@transaction(isolation_level="READ COMMITTED")
def resumo(dias: int = 7) -> dict[str, Any]:
    """Visao geral para o operador e para o Prompter: rodadas, insights e notas (so contagens)."""
    session = sql_session()
    out: dict[str, Any] = {"dias": dias}
    existe = {t: bool(session.execute(_TABELA_EXISTE_SQL, {"nome": f"genius_next.{t}"}).scalar())
              for t in ("learning_runs", "learning_insights", "turn_judgments")}
    out["tabelas"] = existe
    if existe["learning_runs"]:
        rows = session.execute(
            text("SELECT status, dry_run, count(*) FROM genius_next.learning_runs "
                 "WHERE started_at >= now() - make_interval(days => :d) GROUP BY 1, 2 ORDER BY 3 DESC"),
            {"d": dias},
        )
        out["rodadas"] = [{"status": r[0], "dry_run": r[1], "n": r[2]} for r in rows]
    if existe["learning_insights"]:
        out["insights"] = {r[0]: r[1] for r in session.execute(
            text("SELECT status, count(*) FROM genius_next.learning_insights GROUP BY 1"))}
    if existe["turn_judgments"]:
        r = session.execute(
            text("SELECT count(*), count(*) FILTER (WHERE NOT passed), round(avg(score), 3) "
                 "FROM genius_next.turn_judgments WHERE created_at >= now() - make_interval(days => :d)"),
            {"d": dias},
        ).one()
        out["notas"] = {"total": r[0], "reprovadas": r[1], "media": float(r[2]) if r[2] is not None else None}
    return out
