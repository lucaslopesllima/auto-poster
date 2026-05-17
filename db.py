"""
db.py
=====

Fila de posts em SQLite.

Schema da tabela `posts`:
  id            INTEGER PRIMARY KEY
  text          TEXT      conteúdo do post (commentary)
  image_path    TEXT      caminho opcional para imagem (relativo ou absoluto)
  scheduled_at  TEXT      ISO 8601 (UTC) — quando deve ser publicado
  posted_at     TEXT      ISO 8601 (UTC) — preenchido após publicação
  post_urn      TEXT      URN devolvido pelo LinkedIn (referência ao post real)
  error         TEXT      última mensagem de erro, se houver
  created_at    TEXT      ISO 8601 (UTC) — gerado automaticamente

Estados:
  - pendente  → posted_at IS NULL
  - publicado → posted_at IS NOT NULL
  - com falha → posted_at IS NULL AND error IS NOT NULL
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# Localização do banco
# ---------------------------------------------------------------------------

DB_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DB_DIR / "queue.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    text            TEXT NOT NULL,
    image_path      TEXT,
    scheduled_at    TEXT NOT NULL,
    posted_at       TEXT,
    post_urn        TEXT,
    error           TEXT,
    repeat_minutes  INTEGER,
    repeat_daily_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_posts_due
    ON posts (posted_at, scheduled_at);

CREATE TABLE IF NOT EXISTS research_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    topic           TEXT NOT NULL,
    generate_at     TEXT NOT NULL,    -- HH:MM local: hora de gerar o rascunho
    publish_at      TEXT,             -- LEGADO: ignorado; agenda fica na Fila
    max_results     INTEGER DEFAULT 5,
    auto_approve    INTEGER DEFAULT 0, -- LEGADO: ignorado; jobs sempre geram rascunho
    enabled         INTEGER DEFAULT 1,
    last_run_at     TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS used_articles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER,
    url_hash    TEXT NOT NULL UNIQUE,
    url         TEXT,
    title       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_used_articles_hash
    ON used_articles (url_hash);
"""

# Migrations idempotentes: ADD COLUMN levanta OperationalError quando a coluna
# já existe — ignoramos para suportar bancos criados antes desses campos.
MIGRATIONS = [
    "ALTER TABLE posts ADD COLUMN repeat_minutes INTEGER",
    "ALTER TABLE posts ADD COLUMN repeat_daily_at TEXT",
    "ALTER TABLE posts ADD COLUMN is_draft INTEGER DEFAULT 0",
    # Dedup global de notícias: deduplica registros legados (UNIQUE
    # composto antigo permitia mesma url em jobs distintos) e cria índice
    # único global em url_hash.
    "DELETE FROM used_articles WHERE id NOT IN "
    "(SELECT MIN(id) FROM used_articles GROUP BY url_hash)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uidx_used_articles_hash_global "
    "ON used_articles (url_hash)",
]


# ---------------------------------------------------------------------------
# Conexão
# ---------------------------------------------------------------------------

@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Garante que o diretório existe, abre conexão e aplica schema."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        for sql in MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # coluna já existe
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers de data
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_when(value: Optional[str]) -> str:
    """
    Aceita formatos amigáveis e devolve ISO 8601 (UTC):
      - None / ""              → agora
      - 'YYYY-MM-DD HH:MM'     → local naive, convertido para UTC
      - 'YYYY-MM-DDTHH:MM:SS'  → ISO completo
    """
    if not value:
        return _now_iso()

    candidates = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in candidates:
        try:
            dt = datetime.strptime(value, fmt)
            # interpreta como hora local → converte para UTC
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue

    # tenta ISO direto (já com timezone)
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()
    except ValueError as exc:
        raise ValueError(f"Formato de data inválido: {value!r}") from exc


# ---------------------------------------------------------------------------
# Operações
# ---------------------------------------------------------------------------

def _validate_daily(value: Optional[str]) -> Optional[str]:
    """Valida formato HH:MM. Devolve string normalizada ou None."""
    if not value:
        return None
    try:
        t = datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"repeat_daily_at inválido (HH:MM): {value!r}") from exc
    return t.strftime("%H:%M")


def add_post(
    text: str,
    scheduled_at: Optional[str] = None,
    image_path: Optional[str] = None,
    repeat_minutes: Optional[int] = None,
    repeat_daily_at: Optional[str] = None,
    is_draft: bool = False,
) -> int:
    """
    Insere um post na fila. Devolve o id gerado.

    Recorrência (mutuamente exclusivas):
      - repeat_minutes: re-publica a cada N minutos após cada postagem
      - repeat_daily_at: re-publica diariamente no horário 'HH:MM' (local)
    """
    if repeat_minutes is not None and repeat_daily_at:
        raise ValueError("use repeat_minutes OU repeat_daily_at, não ambos")
    if repeat_minutes is not None and repeat_minutes <= 0:
        raise ValueError("repeat_minutes deve ser > 0")
    daily = _validate_daily(repeat_daily_at)

    if scheduled_at is None and daily:
        # primeiro disparo no próximo HH:MM
        scheduled_at = _next_daily_iso(daily)

    when_iso = parse_when(scheduled_at)
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO posts (text, image_path, scheduled_at, repeat_minutes, repeat_daily_at, is_draft) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (text, image_path, when_iso, repeat_minutes, daily, 1 if is_draft else 0),
        )
        return cur.lastrowid


def approve_post(post_id: int) -> int:
    """Marca um rascunho como pronto para publicar. Retorna nº de linhas."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE posts SET is_draft = 0 WHERE id = ?", (post_id,)
        )
        return cur.rowcount


def update_text(post_id: int, text: str) -> int:
    """Atualiza o texto do post. Retorna nº de linhas."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE posts SET text = ? WHERE id = ?", (text, post_id)
        )
        return cur.rowcount


_POST_EDITABLE = {
    "text", "image_path", "scheduled_at",
    "repeat_minutes", "repeat_daily_at", "is_draft",
}


def update_post(post_id: int, **fields) -> int:
    """Atualiza campos editáveis de um post. Ignora chaves desconhecidas."""
    valid: dict = {}
    for k, v in fields.items():
        if k not in _POST_EDITABLE:
            continue
        valid[k] = v
    if "scheduled_at" in valid:
        valid["scheduled_at"] = parse_when(valid["scheduled_at"])
    if "repeat_daily_at" in valid:
        valid["repeat_daily_at"] = _validate_daily(valid["repeat_daily_at"])
    if "repeat_minutes" in valid and valid["repeat_minutes"] is not None:
        if int(valid["repeat_minutes"]) <= 0:
            valid["repeat_minutes"] = None
    if "is_draft" in valid:
        valid["is_draft"] = 1 if valid["is_draft"] else 0
    if not valid:
        return 0
    set_clause = ", ".join(f"{k} = ?" for k in valid)
    params = list(valid.values()) + [post_id]
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE posts SET {set_clause} WHERE id = ?", params
        )
        return cur.rowcount


def _next_daily_iso(hhmm: str, after: Optional[datetime] = None) -> str:
    """Próxima ocorrência de HH:MM local em ISO UTC."""
    now = after or datetime.now()
    hour, minute = map(int, hhmm.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc).isoformat()


def _next_interval_iso(minutes: int, after: Optional[datetime] = None) -> str:
    """Agora + N minutos em ISO UTC."""
    base = after or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.astimezone(timezone.utc)
    return (base + timedelta(minutes=minutes)).isoformat()


def list_posts(pending_only: bool = False) -> list[sqlite3.Row]:
    """Lista posts. Se `pending_only`, retorna só os ainda não publicados."""
    query = "SELECT * FROM posts"
    if pending_only:
        query += " WHERE posted_at IS NULL"
    query += " ORDER BY scheduled_at ASC"
    with connect() as conn:
        return list(conn.execute(query))


def fetch_due(now_iso: Optional[str] = None) -> list[sqlite3.Row]:
    """
    Devolve posts pendentes (não-rascunho) com `scheduled_at <= now`.

    `now_iso` é parametrizável para facilitar testes.
    """
    now_iso = now_iso or _now_iso()
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM posts
                WHERE posted_at IS NULL
                  AND (is_draft IS NULL OR is_draft = 0)
                  AND scheduled_at <= ?
                ORDER BY scheduled_at ASC
                """,
                (now_iso,),
            )
        )


def get_post(post_id: int) -> Optional[sqlite3.Row]:
    with connect() as conn:
        cur = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        return cur.fetchone()


def mark_posted(post_id: int, post_urn: str) -> Optional[int]:
    """
    Marca um post como publicado e zera erro anterior.

    Se o post tiver recorrência (`repeat_minutes` ou `repeat_daily_at`),
    clona o registro com `scheduled_at` da próxima ocorrência e devolve o id
    da clonagem. Caso contrário devolve None.
    """
    with connect() as conn:
        conn.execute(
            """
            UPDATE posts
               SET posted_at = ?, post_urn = ?, error = NULL
             WHERE id = ?
            """,
            (_now_iso(), post_urn, post_id),
        )
        row = conn.execute(
            "SELECT * FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        if row is None:
            return None

        next_iso: Optional[str] = None
        if row["repeat_daily_at"]:
            next_iso = _next_daily_iso(row["repeat_daily_at"])
        elif row["repeat_minutes"]:
            next_iso = _next_interval_iso(row["repeat_minutes"])

        if next_iso is None:
            return None

        cur = conn.execute(
            "INSERT INTO posts (text, image_path, scheduled_at, repeat_minutes, repeat_daily_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                row["text"],
                row["image_path"],
                next_iso,
                row["repeat_minutes"],
                row["repeat_daily_at"],
            ),
        )
        return cur.lastrowid


def mark_error(post_id: int, message: str) -> None:
    """Registra erro mas mantém posted_at NULL para nova tentativa."""
    with connect() as conn:
        conn.execute(
            "UPDATE posts SET error = ? WHERE id = ?",
            (message, post_id),
        )


def delete_post(post_id: int) -> int:
    """Remove um post da fila. Devolve quantas linhas foram apagadas."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        return cur.rowcount


# ---------------------------------------------------------------------------
# Jobs de pesquisa automática
# ---------------------------------------------------------------------------

def _validate_hhmm(value: Optional[str], field: str) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        t = datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"{field} inválido (HH:MM): {value!r}") from exc
    return t.strftime("%H:%M")


def add_job(
    topic: str,
    generate_at: str,
    max_results: int = 5,
    enabled: bool = True,
) -> int:
    """Cria um job recorrente de geração de rascunho. Devolve o id.

    Jobs só geram rascunhos — o agendamento de publicação é feito depois,
    na Fila, editando o post gerado.
    """
    topic = topic.strip()
    if not topic:
        raise ValueError("topic não pode ser vazio")
    g = _validate_hhmm(generate_at, "generate_at")
    if g is None:
        raise ValueError("generate_at é obrigatório (HH:MM)")
    if max_results < 1:
        raise ValueError("max_results deve ser >= 1")
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO research_jobs "
            "(topic, generate_at, max_results, enabled) "
            "VALUES (?, ?, ?, ?)",
            (topic, g, max_results, 1 if enabled else 0),
        )
        return cur.lastrowid


def list_jobs(enabled_only: bool = False) -> list[sqlite3.Row]:
    q = "SELECT * FROM research_jobs"
    if enabled_only:
        q += " WHERE enabled = 1"
    q += " ORDER BY generate_at ASC, id ASC"
    with connect() as conn:
        return list(conn.execute(q))


def get_job(job_id: int) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM research_jobs WHERE id = ?", (job_id,)
        ).fetchone()


_JOB_FIELDS = {
    "topic", "generate_at", "max_results",
    "enabled", "last_run_at", "last_error",
}


def update_job(job_id: int, **fields) -> int:
    """Atualiza um job. Ignora chaves desconhecidas."""
    valid = {k: v for k, v in fields.items() if k in _JOB_FIELDS}
    if "generate_at" in valid:
        valid["generate_at"] = _validate_hhmm(valid["generate_at"], "generate_at")
    if "enabled" in valid:
        valid["enabled"] = 1 if valid["enabled"] else 0
    if not valid:
        return 0
    set_clause = ", ".join(f"{k} = ?" for k in valid)
    params = list(valid.values()) + [job_id]
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE research_jobs SET {set_clause} WHERE id = ?", params
        )
        return cur.rowcount


def delete_job(job_id: int) -> int:
    with connect() as conn:
        cur = conn.execute("DELETE FROM research_jobs WHERE id = ?", (job_id,))
        return cur.rowcount


def mark_job_run(job_id: int, error: Optional[str] = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE research_jobs SET last_run_at = ?, last_error = ? WHERE id = ?",
            (_now_iso(), error, job_id),
        )


# ---------------------------------------------------------------------------
# Dedup de notícias já usadas
# ---------------------------------------------------------------------------

def _article_hash(art: dict) -> str:
    """Hash estável da notícia. URL é a chave primária; cai para título."""
    import hashlib
    key = (art.get("url") or art.get("title") or "").strip().lower()
    return hashlib.sha1(key.encode("utf-8")).hexdigest() if key else ""


def filter_unused_articles(job_id: Optional[int], articles: list[dict]) -> list[dict]:
    """Retorna apenas artigos cujo hash ainda não foi usado por este job.

    Se `job_id` é None, dedup é global (qualquer job que já tenha usado).
    """
    if not articles:
        return []
    hashes = [_article_hash(a) for a in articles]
    pairs = [(h, a) for h, a in zip(hashes, articles) if h]
    if not pairs:
        return []
    with connect() as conn:
        if job_id is None:
            rows = conn.execute(
                f"SELECT url_hash FROM used_articles WHERE url_hash IN "
                f"({','.join('?' * len(pairs))})",
                [h for h, _ in pairs],
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT url_hash FROM used_articles "
                f"WHERE job_id = ? AND url_hash IN "
                f"({','.join('?' * len(pairs))})",
                [job_id, *(h for h, _ in pairs)],
            ).fetchall()
    used = {r["url_hash"] for r in rows}
    return [a for h, a in pairs if h not in used]


def mark_articles_used(job_id: Optional[int], articles: list[dict]) -> int:
    """Registra artigos como já usados. Devolve nº de inserts efetivos."""
    if not articles:
        return 0
    rows = []
    for a in articles:
        h = _article_hash(a)
        if not h:
            continue
        rows.append((job_id, h, a.get("url"), a.get("title")))
    if not rows:
        return 0
    with connect() as conn:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO used_articles (job_id, url_hash, url, title) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        return cur.rowcount or 0
