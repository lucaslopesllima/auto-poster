"""
evaluator.py
============

Agente crítico que avalia posts de LinkedIn antes da aprovação.

Recebe o texto do post (e, opcionalmente, o tópico + notícias usadas) e
devolve uma nota de 0 a 10 + um comentário curto explicando o que
melhorar. Usa a Chat Completions da OpenAI (mesmo modelo do composer).

API:
  EvaluationError                 — exceção
  Evaluation(score, comment)      — dataclass
  evaluate_post(text, topic=None, articles=None) -> Evaluation
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
OPENAI_URL = "https://api.openai.com/v1/chat/completions"


class EvaluationError(Exception):
    pass


@dataclass
class Evaluation:
    score: float
    comment: str


_SYSTEM_PROMPT = (
    "Você é um editor sênior de LinkedIn em {lang}. Avalia posts pessoais "
    "e dá nota objetiva de 0 a 10. Critérios: especificidade (evita "
    "frases genéricas), gancho forte na 1ª linha, clareza, presença de "
    "ângulo/opinião própria, uso real das fontes citadas (quando há), "
    "call-to-action ou pergunta no final, hashtags pertinentes. "
    "Posts genéricos, motivacionais vazios, ou que apenas resumem "
    "notícias sem ângulo recebem nota baixa (≤ 5). "
    "Responda EXCLUSIVAMENTE em JSON válido no formato: "
    '{{"score": <0-10>, "comment": "<comentário curto em {lang}, '
    'até 2 frases, dizendo o principal a melhorar>"}}. '
    "Sem markdown, sem texto fora do JSON."
)


def evaluate_post(
    text: str,
    topic: Optional[str] = None,
    articles: Optional[Iterable[dict]] = None,
) -> Evaluation:
    """Avalia um post. Levanta `EvaluationError` em falha."""
    load_dotenv(BASE_DIR / ".env", override=True)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise EvaluationError("OPENAI_API_KEY ausente no .env")
    if not (text or "").strip():
        raise EvaluationError("texto vazio")

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    lang = os.getenv("COMPOSER_LANGUAGE", "pt-BR").strip()

    user_prompt = _build_user_prompt(text, topic, list(articles or []))
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT.format(lang=lang)},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 300,
        "response_format": {"type": "json_object"},
    }
    resp = requests.post(
        OPENAI_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    if resp.status_code >= 300:
        raise EvaluationError(f"OpenAI {resp.status_code}: {resp.text}")
    data = resp.json()
    try:
        raw = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as exc:
        raise EvaluationError(f"resposta inesperada: {data}") from exc

    return _parse_evaluation(raw)


def _parse_evaluation(raw: str) -> Evaluation:
    """Aceita JSON puro ou JSON envolto em markdown. Clampa score em [0, 10]."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            raise EvaluationError(f"JSON inválido na resposta: {raw!r}")
        obj = json.loads(m.group(0))
    try:
        score = float(obj["score"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EvaluationError(f"score ausente/inválido: {obj!r}") from exc
    score = max(0.0, min(10.0, score))
    comment = (obj.get("comment") or "").strip()
    return Evaluation(score=score, comment=comment)


def _build_user_prompt(
    text: str, topic: Optional[str], articles: list[dict]
) -> str:
    lines: list[str] = []
    if topic:
        lines.append(f"Tópico: {topic}")
        lines.append("")
    if articles:
        lines.append("Fontes que alimentaram o post (use para checar se "
                     "o texto realmente aproveita os ângulos das notícias):")
        for i, a in enumerate(articles, 1):
            title = a.get("title") or "(sem título)"
            url = a.get("url") or ""
            desc = a.get("description") or ""
            lines.append(f"[{i}] {title}")
            if desc:
                lines.append(f"    resumo: {desc}")
            if url:
                lines.append(f"    url: {url}")
        lines.append("")
    lines.append("Post a avaliar:")
    lines.append("---")
    lines.append(text)
    lines.append("---")
    lines.append("Responda em JSON conforme especificado no system.")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sample = " ".join(sys.argv[1:]) or "Hoje quero falar sobre IA. IA é o futuro. #ia"
    try:
        ev = evaluate_post(sample)
    except EvaluationError as exc:
        sys.exit(f"ERRO: {exc}")
    print(f"score={ev.score}\ncomment={ev.comment}")
