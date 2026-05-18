"""
scheduler.py
============

Loop simples que verifica a fila a cada N segundos e publica os posts
cujo `scheduled_at` já passou.

Equivalente a rodar `python cli.py run-now` periodicamente, mas em um único
processo de longa duração. Útil quando você não quer configurar cron/systemd.

Uso:
  python scheduler.py                    # intervalo padrão de 60s
  python scheduler.py --interval 300     # checa a cada 5 minutos
  python scheduler.py --once             # roda uma vez e sai (igual a run-now)

Encerre com Ctrl+C.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime

import db
import evaluator
from linkedin_client import LinkedInClient, LinkedInError


# Flag global usada pelo handler de SIGINT/SIGTERM para sair limpo
_stop = False


def _handle_signal(signum, frame) -> None:  # noqa: ARG001
    global _stop
    _stop = True
    print("\nEncerrando após o ciclo atual...")


def _evaluate_tick() -> tuple[int, int]:
    """Avalia rascunhos pendentes sem nota. Devolve (avaliados, falhas)."""
    drafts = db.fetch_unevaluated_drafts()
    if not drafts:
        return 0, 0
    ok = fail = 0
    for row in drafts:
        pid = row["id"]
        try:
            arts = db.get_source_articles(pid)
            keys = row.keys() if hasattr(row, "keys") else []
            topic = row["source_topic"] if "source_topic" in keys else None
            ev = evaluator.evaluate_post(
                row["text"] or "", topic=topic, articles=arts or None,
            )
            db.set_evaluation(pid, ev.score, ev.comment)
            ok += 1
            print(f"  ⓘ #{pid} avaliado → {ev.score:.1f}")
        except evaluator.EvaluationError as exc:
            fail += 1
            print(f"  ✗ #{pid} avaliação falhou: {exc}", file=sys.stderr)
    return ok, fail


def _tick(client: LinkedInClient) -> tuple[int, int]:
    """Executa um ciclo. Devolve (publicados, falhas)."""
    _evaluate_tick()
    rows = db.fetch_due()
    if not rows:
        return 0, 0

    ok = 0
    fail = 0
    for row in rows:
        try:
            if row["image_path"]:
                post_urn = client.post_with_image(row["text"], row["image_path"])
            else:
                post_urn = client.post_text(row["text"])
            db.mark_posted(row["id"], post_urn)
            ok += 1
            print(f"  ✓ #{row['id']} publicado → {post_urn}")
        except (LinkedInError, FileNotFoundError) as exc:
            db.mark_error(row["id"], str(exc))
            fail += 1
            print(f"  ✗ #{row['id']} falhou: {exc}", file=sys.stderr)
    return ok, fail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agendador da fila do LinkedIn.")
    parser.add_argument(
        "--interval", type=int, default=60, help="segundos entre verificações"
    )
    parser.add_argument(
        "--once", action="store_true", help="executa um único ciclo e sai"
    )
    args = parser.parse_args(argv)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    client = LinkedInClient.from_env()
    if client.is_expired():
        sys.exit("Token expirado. Rode `python auth.py` para renovar.")

    if args.once:
        ok, fail = _tick(client)
        print(f"Concluído: {ok} publicado(s), {fail} falha(s).")
        return 1 if fail else 0

    print(f"Scheduler iniciado (intervalo={args.interval}s). Ctrl+C para sair.")
    while not _stop:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ok, fail = _tick(client)
        if ok or fail:
            print(f"[{now}] {ok} publicado(s), {fail} falha(s).")

        # dorme em pedaços pequenos para responder rápido ao SIGINT
        slept = 0
        while slept < args.interval and not _stop:
            time.sleep(1)
            slept += 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
