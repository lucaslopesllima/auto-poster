"""
composer.py
===========

Gera o texto de um post de LinkedIn a partir de um tópico + lista de
notícias.

Usa a Chat Completions da OpenAI (modelo padrão `gpt-4o-mini`). Sem SDK
externo — falamos HTTP direto pra evitar mais uma dependência.

Variáveis lidas do `.env`:
  OPENAI_API_KEY       — obrigatório
  OPENAI_MODEL         — opcional (default: gpt-4o-mini)
  COMPOSER_LANGUAGE    — opcional (default: pt-BR)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
OPENAI_URL = "https://api.openai.com/v1/chat/completions"


class ComposerError(Exception):
    pass


def compose_post(topic: str, articles: Iterable[dict]) -> str:
    """
    Gera o texto do post.

    Levanta `ComposerError` se faltar chave ou a API retornar erro.
    """
    load_dotenv(BASE_DIR / ".env", override=True)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ComposerError("OPENAI_API_KEY ausente no .env")

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    lang = os.getenv("COMPOSER_LANGUAGE", "pt-BR").strip()

    arts = list(articles)
    if not arts:
        raise ComposerError("sem notícias para resumir")

    user_prompt = _build_user_prompt(topic, arts, lang)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT.format(lang=lang)},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.6,
        "max_tokens": 700,
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
        raise ComposerError(f"OpenAI {resp.status_code}: {resp.text}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as exc:
        raise ComposerError(f"resposta inesperada: {data}") from exc


_SYSTEM_PROMPT = (
    "Você é um redator que escreve posts curtos para LinkedIn em {lang}. "
    "Tom profissional, direto, sem clichês motivacionais. "
    "Estrutura: gancho na primeira linha; 2–4 parágrafos curtos; "
    "termine com uma pergunta ou call-to-action curto. "
    "Inclua hashtags pertinentes no final (3–5). "
    "NÃO invente fatos: use somente o que está nas fontes fornecidas. "
    "Sempre liste as fontes (título + URL) numa seção final 'Fontes:'."
)


def _build_user_prompt(topic: str, articles: list[dict], lang: str) -> str:
    lines = [f"Tópico: {topic}", "", "Notícias recentes encontradas:", ""]
    for i, a in enumerate(articles, 1):
        title = a.get("title") or "(sem título)"
        url = a.get("url") or ""
        desc = a.get("description") or ""
        source = a.get("source") or ""
        when = a.get("published_at") or ""
        lines.append(f"[{i}] {title}")
        if source or when:
            lines.append(f"    fonte: {source} | quando: {when}")
        if desc:
            lines.append(f"    resumo: {desc}")
        if url:
            lines.append(f"    url: {url}")
        lines.append("")
    lines.append(
        f"Escreva um post em {lang} para LinkedIn sobre o tópico acima, "
        "sintetizando os pontos comuns/contrastes entre as notícias. "
        "Inclua a seção 'Fontes:' com título + URL ao final."
    )
    return "\n".join(lines)


if __name__ == "__main__":
    import json
    import sys
    import news

    topic = " ".join(sys.argv[1:]) or "inteligência artificial"
    arts = news.fetch_news(topic, max_results=5)
    if not arts:
        sys.exit("sem notícias")
    print("---- artigos ----")
    print(json.dumps(arts, indent=2, ensure_ascii=False))
    print("---- post ----")
    try:
        print(compose_post(topic, arts))
    except ComposerError as exc:
        sys.exit(f"ERRO: {exc}")
