"""
cli.py
======

Interface de linha de comando para gerenciar a fila de posts.

Subcomandos:
  add        adiciona um post à fila (imediato ou agendado)
  list       lista os posts da fila
  delete     remove um post pelo id
  run-now    publica todos os pendentes vencidos (ou um id específico)

Exemplos:

  python cli.py add "Olá LinkedIn"
  python cli.py add "Bom dia" --at "2026-05-17 09:00"
  python cli.py add --file posts/exemplo.md --at "2026-05-18 10:00"
  python cli.py add "Veja a foto" --image ./foto.jpg
  python cli.py list
  python cli.py list --pending
  python cli.py delete 3
  python cli.py run-now
  python cli.py run-now --id 5
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import db
from linkedin_client import LinkedInClient, LinkedInError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_text_source(text: Optional[str], file: Optional[str]) -> str:
    """Resolve a fonte do texto do post: argumento posicional ou --file."""
    if text and file:
        sys.exit("ERRO: use TEXTO ou --file, não os dois.")
    if file:
        path = Path(file)
        if not path.is_file():
            sys.exit(f"ERRO: arquivo não encontrado: {file}")
        return path.read_text(encoding="utf-8").strip()
    if text:
        return text
    sys.exit("ERRO: forneça um TEXTO ou --file.")


def _publish_one(client: LinkedInClient, row) -> bool:
    """Publica um único post. Devolve True em sucesso, False em falha."""
    try:
        if row["image_path"]:
            post_urn = client.post_with_image(row["text"], row["image_path"])
        else:
            post_urn = client.post_text(row["text"])
        db.mark_posted(row["id"], post_urn)
        print(f"  ✓ #{row['id']} publicado → {post_urn}")
        return True
    except (LinkedInError, FileNotFoundError) as exc:
        db.mark_error(row["id"], str(exc))
        print(f"  ✗ #{row['id']} falhou: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Subcomandos
# ---------------------------------------------------------------------------

def cmd_add(args: argparse.Namespace) -> int:
    text = _read_text_source(args.text, args.file)
    image = args.image
    if image and not Path(image).is_file():
        sys.exit(f"ERRO: imagem não encontrada: {image}")

    if args.daily and args.every:
        sys.exit("ERRO: use --daily OU --every, não ambos.")

    try:
        post_id = db.add_post(
            text=text,
            scheduled_at=args.at,
            image_path=image,
            repeat_minutes=args.every,
            repeat_daily_at=args.daily,
        )
    except ValueError as exc:
        sys.exit(f"ERRO: {exc}")

    row = db.get_post(post_id)
    suffix = ""
    if args.daily:
        suffix = f" (diário às {args.daily})"
    elif args.every:
        suffix = f" (a cada {args.every} min)"
    print(f"Post #{post_id} agendado para {row['scheduled_at']}{suffix}.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = db.list_posts(pending_only=args.pending)
    if not rows:
        print("(fila vazia)")
        return 0

    print(f"{'ID':>3}  {'STATUS':<10}  {'SCHEDULED_AT':<27}  {'REPETE':<14}  TEXTO")
    print("-" * 100)
    keys = rows[0].keys() if rows else []
    for r in rows:
        if r["posted_at"]:
            status = "posted"
        elif r["error"]:
            status = "error"
        else:
            status = "pending"
        repeat = ""
        if "repeat_daily_at" in keys and r["repeat_daily_at"]:
            repeat = f"diário {r['repeat_daily_at']}"
        elif "repeat_minutes" in keys and r["repeat_minutes"]:
            repeat = f"a/{r['repeat_minutes']}min"
        snippet = r["text"].replace("\n", " ")[:40]
        print(f"{r['id']:>3}  {status:<10}  {r['scheduled_at']:<27}  {repeat:<14}  {snippet}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    n = db.delete_post(args.id)
    if n == 0:
        sys.exit(f"Nenhum post com id={args.id}.")
    print(f"Post #{args.id} removido.")
    return 0


def cmd_run_now(args: argparse.Namespace) -> int:
    client = LinkedInClient.from_env()

    if args.id is not None:
        row = db.get_post(args.id)
        if row is None:
            sys.exit(f"Post #{args.id} não encontrado.")
        if row["posted_at"]:
            sys.exit(f"Post #{args.id} já foi publicado em {row['posted_at']}.")
        rows = [row]
    else:
        rows = db.fetch_due()

    if not rows:
        print("Nada a publicar agora.")
        return 0

    print(f"Publicando {len(rows)} post(s)...")
    failures = 0
    for row in rows:
        if not _publish_one(client, row):
            failures += 1

    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fila de posts do LinkedIn.")
    sub = p.add_subparsers(dest="command", required=True)

    # add
    p_add = sub.add_parser("add", help="adiciona post à fila")
    p_add.add_argument("text", nargs="?", help="texto do post")
    p_add.add_argument("--file", help="lê o texto deste arquivo")
    p_add.add_argument("--at", help="quando publicar (ex: '2026-05-17 09:00')")
    p_add.add_argument("--image", help="caminho de uma imagem opcional")
    p_add.add_argument("--daily", help="repete diariamente nesse horário 'HH:MM'")
    p_add.add_argument("--every", type=int, help="repete a cada N minutos")
    p_add.set_defaults(func=cmd_add)

    # list
    p_list = sub.add_parser("list", help="lista posts")
    p_list.add_argument("--pending", action="store_true", help="só pendentes")
    p_list.set_defaults(func=cmd_list)

    # delete
    p_del = sub.add_parser("delete", help="remove post pelo id")
    p_del.add_argument("id", type=int)
    p_del.set_defaults(func=cmd_delete)

    # run-now
    p_run = sub.add_parser("run-now", help="publica pendentes vencidos")
    p_run.add_argument("--id", type=int, help="publica apenas este id")
    p_run.set_defaults(func=cmd_run_now)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
