"""
gui.py
======

Interface desktop em Python (PySide6/Qt) que expõe todas as features:
  - autenticar (fluxo OAuth)
  - compor novo post (texto, arquivo .md, imagem, agendamento)
  - listar / publicar / apagar a fila
  - rodar/parar o agendador em loop

Roda com:
  python gui.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

import auth as auth_module
import composer
import db
import evaluator
import news
from linkedin_client import LinkedInClient, LinkedInError

BASE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Helpers em thread (Qt signals)
# ---------------------------------------------------------------------------

class Worker(QtCore.QObject):
    """Roda uma callable em thread separada e devolve resultado via signals."""

    finished = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    @QtCore.Slot()
    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception:  # noqa: BLE001
            self.failed.emit(traceback.format_exc())
            return
        self.finished.emit(result)


_THREAD_KEEPALIVE: list = []


def _post_to_main(target: QtCore.QObject, callback, value):
    """
    Posta um callable na thread em que `target` vive, via
    `QTimer.singleShot(0, context, slot)`. O `context` define qual event loop
    (qual thread) executa o slot — usamos `target` da main thread.
    """
    if callback is None:
        return
    QtCore.QTimer.singleShot(0, target, lambda: callback(value))


def run_in_thread(parent: QtCore.QObject, fn, on_done=None, on_error=None, on_any=None, *args, **kwargs):
    """
    Roda `fn` em QThread. Callbacks `on_done`/`on_error`/`on_any` rodam SEMPRE
    na thread em que `parent` vive (tipicamente main thread).

    `signal.connect(callable, QueuedConnection)` NÃO basta — PySide trata
    callables sem afinidade de thread como DirectConnection. Por isso
    roteamos via `QMetaObject.invokeMethod(parent, ...)`.
    """
    thread = QtCore.QThread(parent)
    worker = Worker(fn, *args, **kwargs)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)

    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)

    if on_done:
        worker.finished.connect(lambda r: _post_to_main(parent, on_done, r))
    if on_error:
        worker.failed.connect(lambda tb: _post_to_main(parent, on_error, tb))
    if on_any:
        worker.finished.connect(lambda *_a: _post_to_main(parent, lambda _=None: on_any(), None))
        worker.failed.connect(lambda *_a: _post_to_main(parent, lambda _=None: on_any(), None))

    worker.finished.connect(worker.deleteLater)
    worker.failed.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)

    pair = (thread, worker)
    _THREAD_KEEPALIVE.append(pair)

    def _cleanup():
        try:
            _THREAD_KEEPALIVE.remove(pair)
        except ValueError:
            pass

    thread.finished.connect(_cleanup)
    thread.start()
    return thread, worker


# ---------------------------------------------------------------------------
# Modelo: status do token
# ---------------------------------------------------------------------------

def _fmt_sched_br(value: Optional[str]) -> str:
    """Converte ISO (UTC) → 'dd/MM/yyyy HH:mm' em horário local."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return value


def _ran_today_local(last_run_at: Optional[str]) -> bool:
    """True se `last_run_at` (UTC ISO) cai no dia local atual."""
    if not last_run_at:
        return False
    try:
        last_local = datetime.fromisoformat(last_run_at).astimezone().date()
    except (TypeError, ValueError):
        return False
    return last_local == datetime.now().date()


class BRDateTimeEdit(QtWidgets.QDateTimeEdit):
    """QDateTimeEdit que abre o popup do calendário ao clicar em qualquer
    parte do input (não só no ícone). Mantém digitação via teclado
    (segmentos navegáveis com Tab/setas e Escape fecha o popup).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCalendarPopup(True)
        self.setDisplayFormat("dd/MM/yyyy HH:mm")
        self.setLocale(QtCore.QLocale(QtCore.QLocale.Portuguese, QtCore.QLocale.Brazil))

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        super().mousePressEvent(event)
        if event.button() == QtCore.Qt.LeftButton:
            self._open_calendar_popup()

    def _open_calendar_popup(self) -> None:
        # Com calendarPopup=True, o Qt cria um QToolButton interno como
        # gatilho. Clicar nele dispara o popup nativo (com auto-close).
        for btn in self.findChildren(QtWidgets.QToolButton):
            btn.animateClick(0)
            return


def read_env_status() -> dict:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env", override=True)
    token = os.getenv("LINKEDIN_ACCESS_TOKEN", "").strip()
    urn = os.getenv("LINKEDIN_URN", "").strip()
    expires_at = os.getenv("LINKEDIN_TOKEN_EXPIRES_AT", "").strip()
    client_id = os.getenv("LINKEDIN_CLIENT_ID", "").strip()
    expired = False
    if expires_at:
        try:
            expired = datetime.now(timezone.utc) >= datetime.fromisoformat(expires_at)
        except ValueError:
            pass
    return {
        "authenticated": bool(token and urn),
        "expired": expired,
        "urn": urn,
        "expires_at": expires_at or None,
        "has_client_credentials": bool(client_id),
    }


def publish_row(client: LinkedInClient, row) -> dict:
    pid = row["id"]
    print(f"[publish] #{pid} start image={bool(row['image_path'])}", flush=True)
    try:
        if row["image_path"]:
            urn = client.post_with_image(row["text"], row["image_path"])
        else:
            urn = client.post_text(row["text"])
        db.mark_posted(pid, urn)
        print(f"[publish] #{pid} OK -> {urn}", flush=True)
        return {"id": pid, "ok": True, "urn": urn}
    except (LinkedInError, FileNotFoundError) as exc:
        db.mark_error(pid, str(exc))
        print(f"[publish] #{pid} FAIL: {exc}", flush=True)
        return {"id": pid, "ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        db.mark_error(pid, repr(exc))
        print(f"[publish] #{pid} EXC: {exc!r}", flush=True)
        return {"id": pid, "ok": False, "error": repr(exc)}


# ---------------------------------------------------------------------------
# Scheduler em background
# ---------------------------------------------------------------------------

class SchedulerThread:
    """Loop que verifica fila a cada N segundos. Sinaliza Qt via callbacks."""

    # Lock global de classe: serializa _run_job entre o scheduler real
    # e qualquer SchedulerThread "dummy" criada pelo botão manual.
    _run_lock = threading.Lock()

    def __init__(self, interval: int, on_tick):
        self.interval = interval
        self.on_tick = on_tick
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        # cliente do LinkedIn é opcional: sem token o loop ainda processa
        # jobs de pesquisa (só não publica posts).
        try:
            client = LinkedInClient.from_env()
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] sem client LinkedIn: {exc}", flush=True)
            client = None

        while not self._stop.is_set():
            ok = fail = 0
            if client is not None:
                for r in db.fetch_due():
                    res = publish_row(client, r)
                    if res.get("ok"):
                        ok += 1
                    else:
                        fail += 1

            jobs_run, jobs_err = self._process_jobs()
            evals_ok, evals_err = self._evaluate_drafts()

            self.on_tick({
                "ok": ok,
                "fail": fail,
                "jobs_run": jobs_run,
                "jobs_err": jobs_err,
                "evals_ok": evals_ok,
                "evals_err": evals_err,
                "last_tick": datetime.now(timezone.utc).isoformat(),
            })
            slept = 0
            while slept < self.interval and not self._stop.is_set():
                time.sleep(1)
                slept += 1

    def _evaluate_drafts(self) -> tuple[int, int]:
        """Avalia rascunhos pendentes sem nota. Devolve (ok, falhas)."""
        ok = fail = 0
        for row in db.fetch_unevaluated_drafts():
            pid = row["id"]
            try:
                arts = db.get_source_articles(pid)
                keys = row.keys() if hasattr(row, "keys") else []
                topic = row["source_topic"] if "source_topic" in keys else None
                ev = evaluator.evaluate_post(
                    row["text"] or "", topic=topic, articles=arts or None,
                )
                db.set_evaluation(pid, ev.score, ev.comment)
                print(
                    f"[eval] post #{pid} score={ev.score:.1f} "
                    f"comment={ev.comment[:80]!r}",
                    flush=True,
                )
                ok += 1
            except evaluator.EvaluationError as exc:
                print(f"[eval] post #{pid} FAIL: {exc}", flush=True)
                fail += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[eval] post #{pid} ERRO: {exc}", flush=True)
                fail += 1
        return ok, fail

    def _process_jobs(self) -> tuple[int, int]:
        """
        Itera research_jobs habilitados, dispara `news.fetch_news` +
        `composer.compose_post` quando `generate_at` (HH:MM local) já passou
        hoje e o job ainda não rodou hoje. Devolve (executados, falhas).
        """
        executados = falhas = 0
        now_local = datetime.now()

        for job in db.list_jobs(enabled_only=True):
            try:
                if _ran_today_local(job["last_run_at"]):
                    continue  # já rodou hoje (compara em hora local)

                hh, mm = map(int, job["generate_at"].split(":"))
                gen_dt = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if now_local < gen_dt:
                    continue  # ainda não deu o horário

                self._run_job(job, now_local)
                executados += 1
            except Exception as exc:  # noqa: BLE001
                db.mark_job_run(job["id"], error=str(exc))
                print(f"[job {job['id']}] FAIL: {exc}", flush=True)
                falhas += 1
        return executados, falhas

    def _run_job(self, job, now_local: datetime) -> None:
        # Serializa: bloqueia ticks paralelos e cliques duplos do manual.
        with SchedulerThread._run_lock:
            # Re-checa "rodou hoje" agora que temos o lock — evita corrida
            # entre dois caminhos que ambos passaram pelo check inicial.
            fresh = db.get_job(job["id"])
            if fresh and _ran_today_local(fresh["last_run_at"]):
                print(f"[job {job['id']}] skip: já rodou hoje", flush=True)
                return

            topic = job["topic"]
            jid = job["id"]
            print(f"[job {jid}] gerando topic={topic!r}", flush=True)

            arts = news.fetch_news(topic, max_results=job["max_results"] or 5)
            if not arts:
                raise RuntimeError("nenhuma notícia encontrada")

            # Dedup GLOBAL: descarta notícias já usadas por QUALQUER job.
            fresh_arts = db.filter_unused_articles(None, arts)
            if not fresh_arts:
                # Marca o run pra não retentar em loop hoje.
                db.mark_job_run(jid, error="todas as notícias já foram usadas")
                raise RuntimeError("todas as notícias já foram usadas")

            text = composer.compose_post(topic, fresh_arts)
            image_path = news.pick_image(fresh_arts)

            # Jobs só geram rascunho. Publicação fica a cargo da Fila.
            pid = db.add_post(
                text=text, image_path=image_path,
                scheduled_at=None, is_draft=True,
                source_topic=topic, source_articles=fresh_arts,
            )
            # job_id guardado só para auditoria; lookup é por url_hash global.
            db.mark_articles_used(jid, fresh_arts)
            db.mark_job_run(jid, error=None)
            print(
                f"[job {jid}] OK -> draft #{pid} "
                f"(arts={len(fresh_arts)}, image={bool(image_path)})",
                flush=True,
            )


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

DARK_QSS = """
QWidget { background: #0f1419; color: #e6edf3; font-size: 13px; }
QFrame#topbar { background: #161b22; border-bottom: 1px solid #2a313a; }
QLabel#brand { font-size: 16px; font-weight: 600; }
QLabel#logo { background: #0a66c2; color: white; padding: 2px 8px; border-radius: 4px; font-style: italic; font-weight: 700; }
QLabel#pill { padding: 4px 12px; border-radius: 10px; background: #1e252e; border: 1px solid #2a313a; font-size: 12px; }
QLabel#pillOk { padding: 4px 12px; border-radius: 10px; background: rgba(46,160,67,0.15); color: #4ade80; border: 1px solid rgba(46,160,67,0.4); font-size: 12px; }
QLabel#pillWarn { padding: 4px 12px; border-radius: 10px; background: rgba(210,153,34,0.15); color: #fcd34d; border: 1px solid rgba(210,153,34,0.4); font-size: 12px; }
QLabel#pillBad { padding: 4px 12px; border-radius: 10px; background: rgba(197,48,48,0.15); color: #fca5a5; border: 1px solid rgba(197,48,48,0.4); font-size: 12px; }

QTabWidget::pane { border: none; background: #0f1419; }
QTabBar::tab {
    background: #161b22; color: #8b949e; padding: 10px 18px;
    border: none; border-bottom: 2px solid transparent;
}
QTabBar::tab:selected { color: #e6edf3; border-bottom: 2px solid #0a66c2; }
QTabBar::tab:hover { color: #e6edf3; }

QLineEdit, QTextEdit, QPlainTextEdit, QDateTimeEdit, QSpinBox, QComboBox {
    background: #161b22; border: 1px solid #2a313a; border-radius: 6px;
    padding: 6px 8px; color: #e6edf3;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QDateTimeEdit:focus, QSpinBox:focus, QComboBox:focus {
    border: 1px solid #0a66c2;
}

QPushButton {
    background: #1e252e; border: 1px solid #2a313a; border-radius: 6px;
    padding: 6px 14px; color: #e6edf3;
}
QPushButton:hover { background: #262d36; }
QPushButton:disabled { color: #6b7280; }
QPushButton#primary { background: #0a66c2; border-color: #0a66c2; color: white; }
QPushButton#primary:hover { background: #1675d1; }
QPushButton#danger { background: #c53030; border-color: #c53030; color: white; }
QPushButton#danger:hover { background: #e53e3e; }

QTableWidget {
    background: #161b22; border: 1px solid #2a313a; border-radius: 6px;
    gridline-color: #2a313a;
}
QHeaderView::section {
    background: #1e252e; color: #8b949e; padding: 6px; border: none;
    border-bottom: 1px solid #2a313a; font-size: 11px;
}
QTableWidget::item { padding: 6px; }
QTableWidget::item:selected { background: rgba(10,102,194,0.25); }

QLabel#muted { color: #8b949e; font-size: 12px; }
QLabel#section { font-size: 16px; font-weight: 600; margin-top: 6px; }

QFrame#card { background: #161b22; border: 1px solid #2a313a; border-radius: 6px; padding: 12px; }

QLabel#msgOk { color: #4ade80; padding: 6px; }
QLabel#msgErr { color: #fca5a5; padding: 6px; }
QLabel#msgInfo { color: #93c5fd; padding: 6px; }

QCheckBox { color: #e6edf3; }
"""


class StatusPill(QtWidgets.QLabel):
    def __init__(self):
        super().__init__("verificando...")
        self.setObjectName("pill")

    def set_state(self, status: dict):
        if not status.get("has_client_credentials"):
            self.setObjectName("pillBad")
            self.setText("sem credenciais (.env)")
        elif not status.get("authenticated"):
            self.setObjectName("pillWarn")
            self.setText("não autenticado")
        elif status.get("expired"):
            self.setObjectName("pillBad")
            self.setText("token expirado")
        else:
            self.setObjectName("pillOk")
            self.setText("autenticado")
        self.style().unpolish(self)
        self.style().polish(self)


class ComposeTab(QtWidgets.QWidget):
    post_added = QtCore.Signal()

    def __init__(self):
        super().__init__()
        layout = QtWidgets.QHBoxLayout(self)

        # ---- coluna esquerda: texto + arquivo
        left = QtWidgets.QVBoxLayout()
        left.addWidget(self._label("Texto do post"))
        self.text_edit = QtWidgets.QPlainTextEdit()
        self.text_edit.setPlaceholderText("Escreva aqui...")
        left.addWidget(self.text_edit, 1)

        left.addWidget(self._label("Importar de arquivo (posts/*.md)"))
        row = QtWidgets.QHBoxLayout()
        self.file_combo = QtWidgets.QComboBox()
        self.file_combo.addItem("(nenhum)", "")
        self.refresh_file_list()
        btn_load = QtWidgets.QPushButton("Carregar")
        btn_load.clicked.connect(self.load_file)
        row.addWidget(self.file_combo, 1)
        row.addWidget(btn_load)
        left.addLayout(row)

        layout.addLayout(left, 1)

        # ---- coluna direita: imagem + schedule + ação
        right = QtWidgets.QVBoxLayout()
        right.addWidget(self._label("Imagem (opcional)"))
        img_row = QtWidgets.QHBoxLayout()
        self.image_path = QtWidgets.QLineEdit()
        self.image_path.setReadOnly(True)
        btn_pick = QtWidgets.QPushButton("Selecionar...")
        btn_pick.clicked.connect(self.pick_image)
        btn_clr = QtWidgets.QPushButton("Limpar")
        btn_clr.clicked.connect(lambda: (self.image_path.clear(), self.preview.clear()))
        img_row.addWidget(self.image_path, 1)
        img_row.addWidget(btn_pick)
        img_row.addWidget(btn_clr)
        right.addLayout(img_row)

        self.preview = QtWidgets.QLabel()
        self.preview.setAlignment(QtCore.Qt.AlignCenter)
        self.preview.setMinimumHeight(180)
        self.preview.setStyleSheet("border: 1px dashed #2a313a; border-radius: 6px;")
        right.addWidget(self.preview)

        right.addWidget(self._label("Agendamento"))
        sched_box = QtWidgets.QGroupBox()
        sched_box.setObjectName("card")
        sl = QtWidgets.QVBoxLayout(sched_box)

        # modo 1: data específica (ou agora)
        self.mode_one = QtWidgets.QRadioButton("Em uma data/hora específica")
        self.mode_one.setChecked(True)
        sl.addWidget(self.mode_one)
        one_row = QtWidgets.QHBoxLayout()
        one_row.addSpacing(20)
        self.when_now = QtWidgets.QCheckBox("Publicar agora")
        self.when_now.setChecked(True)
        self.when_edit = BRDateTimeEdit()
        self.when_edit.setDateTime(QtCore.QDateTime.currentDateTime())
        self.when_edit.setEnabled(False)
        self.when_now.toggled.connect(lambda c: self.when_edit.setEnabled(not c))
        one_row.addWidget(self.when_now)
        one_row.addWidget(self.when_edit, 1)
        sl.addLayout(one_row)

        # modo 2: diário em HH:MM
        self.mode_daily = QtWidgets.QRadioButton("Diariamente no horário")
        sl.addWidget(self.mode_daily)
        daily_row = QtWidgets.QHBoxLayout()
        daily_row.addSpacing(20)
        self.daily_edit = QtWidgets.QTimeEdit()
        self.daily_edit.setDisplayFormat("HH:mm")
        self.daily_edit.setTime(QtCore.QTime(9, 0))
        daily_row.addWidget(self.daily_edit)
        daily_row.addWidget(QtWidgets.QLabel("(repete a cada 24h após a 1ª publicação)"))
        daily_row.addStretch(1)
        sl.addLayout(daily_row)

        # modo 3: cada N minutos
        self.mode_interval = QtWidgets.QRadioButton("A cada N minutos")
        sl.addWidget(self.mode_interval)
        int_row = QtWidgets.QHBoxLayout()
        int_row.addSpacing(20)
        self.interval_edit = QtWidgets.QSpinBox()
        self.interval_edit.setRange(1, 60 * 24 * 30)
        self.interval_edit.setValue(60)
        self.interval_edit.setSuffix(" min")
        int_row.addWidget(self.interval_edit)
        int_row.addWidget(QtWidgets.QLabel("(1ª publicação agora; depois repete)"))
        int_row.addStretch(1)
        sl.addLayout(int_row)

        # habilita campos só do modo selecionado
        def _update_mode():
            self.when_now.setEnabled(self.mode_one.isChecked())
            self.when_edit.setEnabled(self.mode_one.isChecked() and not self.when_now.isChecked())
            self.daily_edit.setEnabled(self.mode_daily.isChecked())
            self.interval_edit.setEnabled(self.mode_interval.isChecked())
        for b in (self.mode_one, self.mode_daily, self.mode_interval):
            b.toggled.connect(_update_mode)
        _update_mode()

        right.addWidget(sched_box)

        self.add_btn = QtWidgets.QPushButton("Adicionar à fila")
        self.add_btn.setObjectName("primary")
        self.add_btn.clicked.connect(self.add_post)
        right.addWidget(self.add_btn)

        self.msg = QtWidgets.QLabel("")
        self.msg.setWordWrap(True)
        right.addWidget(self.msg)
        right.addStretch(1)

        layout.addLayout(right, 1)

    def _label(self, text):
        l = QtWidgets.QLabel(text)
        l.setObjectName("muted")
        return l

    def refresh_file_list(self):
        self.file_combo.clear()
        self.file_combo.addItem("(nenhum)", "")
        posts_dir = BASE_DIR / "posts"
        if posts_dir.is_dir():
            for p in sorted(posts_dir.glob("*.md")):
                self.file_combo.addItem(p.name, str(p))

    def load_file(self):
        path = self.file_combo.currentData()
        if not path:
            return
        try:
            self.text_edit.setPlainText(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            self._set_msg("err", f"Falha ao ler arquivo: {exc}")

    def pick_image(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Selecionar imagem", "",
            "Imagens (*.png *.jpg *.jpeg *.gif *.webp)"
        )
        if not path:
            return
        self.image_path.setText(path)
        pix = QtGui.QPixmap(path)
        if not pix.isNull():
            self.preview.setPixmap(pix.scaled(
                400, 200, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation
            ))

    def add_post(self):
        text = self.text_edit.toPlainText().strip()
        if not text:
            self._set_msg("err", "Texto vazio.")
            return
        image = self.image_path.text().strip() or None
        if image and not Path(image).is_file():
            self._set_msg("err", f"Imagem não encontrada: {image}")
            return

        when = None
        repeat_minutes = None
        repeat_daily_at = None
        if self.mode_one.isChecked():
            if not self.when_now.isChecked():
                when = self.when_edit.dateTime().toString("yyyy-MM-dd HH:mm")
        elif self.mode_daily.isChecked():
            repeat_daily_at = self.daily_edit.time().toString("HH:mm")
        elif self.mode_interval.isChecked():
            repeat_minutes = self.interval_edit.value()

        try:
            post_id = db.add_post(
                text=text,
                scheduled_at=when,
                image_path=image,
                repeat_minutes=repeat_minutes,
                repeat_daily_at=repeat_daily_at,
            )
        except ValueError as exc:
            self._set_msg("err", str(exc))
            return

        suffix = ""
        if repeat_daily_at:
            suffix = f" (diário às {repeat_daily_at})"
        elif repeat_minutes:
            suffix = f" (a cada {repeat_minutes} min)"
        self._set_msg("ok", f"Post #{post_id} adicionado{suffix}.")
        self.text_edit.clear()
        self.image_path.clear()
        self.preview.clear()
        self.post_added.emit()

    def _set_msg(self, kind, text):
        obj = {"ok": "msgOk", "err": "msgErr", "info": "msgInfo"}[kind]
        self.msg.setObjectName(obj)
        self.msg.setText(text)
        self.msg.style().unpolish(self.msg)
        self.msg.style().polish(self.msg)


class EditPostDialog(QtWidgets.QDialog):
    """Editor de um post da fila: texto, imagem, agendamento, recorrência."""

    def __init__(self, parent, row):
        super().__init__(parent)
        self.setWindowTitle(f"Editar post #{row['id']}")
        self.resize(640, 640)
        self._row = row
        self._post_id = row["id"]

        layout = QtWidgets.QVBoxLayout(self)

        # --- avaliação do agente (se houver) + botão de regerar
        keys = row.keys() if hasattr(row, "keys") else []
        has_score = ("eval_score" in keys) and (row["eval_score"] is not None)
        has_source = ("source_articles" in keys) and bool(row["source_articles"])
        eval_box = QtWidgets.QFrame()
        eval_box.setObjectName("card")
        ev_l = QtWidgets.QVBoxLayout(eval_box)
        header = QtWidgets.QHBoxLayout()
        title_lbl = QtWidgets.QLabel("Avaliação do agente")
        title_lbl.setObjectName("muted")
        header.addWidget(title_lbl)
        header.addStretch(1)
        self.score_lbl = QtWidgets.QLabel("(ainda não avaliado)")
        if has_score:
            sc = float(row["eval_score"])
            self.score_lbl.setText(f"Nota: {sc:.1f} / 10")
            if sc < 5:
                self.score_lbl.setStyleSheet("color:#fca5a5;font-weight:600;")
            elif sc < 7:
                self.score_lbl.setStyleSheet("color:#fcd34d;font-weight:600;")
            else:
                self.score_lbl.setStyleSheet("color:#4ade80;font-weight:600;")
        header.addWidget(self.score_lbl)
        ev_l.addLayout(header)
        self.eval_comment = QtWidgets.QPlainTextEdit()
        self.eval_comment.setReadOnly(True)
        self.eval_comment.setPlainText(
            (row["eval_comment"] if "eval_comment" in keys else "") or ""
        )
        self.eval_comment.setMaximumHeight(80)
        self.eval_comment.setPlaceholderText(
            "(o agente avaliador escreve aqui o que melhorar)"
        )
        ev_l.addWidget(self.eval_comment)
        regen_row = QtWidgets.QHBoxLayout()
        self.regen_btn = QtWidgets.QPushButton(
            "Regerar com feedback do agente"
            + (" + notícias" if has_source else "")
        )
        self.regen_btn.setObjectName("primary")
        self.regen_btn.setEnabled(
            has_score and bool((row["eval_comment"] or "").strip())
        )
        self.regen_btn.clicked.connect(self._regenerate)
        regen_row.addWidget(self.regen_btn)
        self.regen_status = QtWidgets.QLabel("")
        self.regen_status.setObjectName("muted")
        regen_row.addWidget(self.regen_status, 1)
        ev_l.addLayout(regen_row)
        layout.addWidget(eval_box)

        layout.addWidget(QtWidgets.QLabel("Texto:"))
        self.text_edit = QtWidgets.QPlainTextEdit()
        self.text_edit.setPlainText(row["text"] or "")
        layout.addWidget(self.text_edit, 1)

        # imagem
        img_row = QtWidgets.QHBoxLayout()
        img_row.addWidget(QtWidgets.QLabel("Imagem:"))
        self.image_path = QtWidgets.QLineEdit(row["image_path"] or "")
        self.image_path.setReadOnly(True)
        img_row.addWidget(self.image_path, 1)
        b_pick = QtWidgets.QPushButton("Selecionar...")
        b_pick.clicked.connect(self._pick_image)
        img_row.addWidget(b_pick)
        b_clr = QtWidgets.QPushButton("Limpar")
        b_clr.clicked.connect(lambda: self.image_path.clear())
        img_row.addWidget(b_clr)
        layout.addLayout(img_row)

        # scheduled_at
        sched_row = QtWidgets.QHBoxLayout()
        sched_row.addWidget(QtWidgets.QLabel("Agendado para:"))
        self.when_edit = BRDateTimeEdit()
        # tenta parsear scheduled_at
        try:
            dt = datetime.fromisoformat(row["scheduled_at"]).astimezone()
            self.when_edit.setDateTime(QtCore.QDateTime(
                QtCore.QDate(dt.year, dt.month, dt.day),
                QtCore.QTime(dt.hour, dt.minute),
            ))
        except (TypeError, ValueError):
            self.when_edit.setDateTime(QtCore.QDateTime.currentDateTime())
        sched_row.addWidget(self.when_edit, 1)
        layout.addLayout(sched_row)

        # recorrência
        box = QtWidgets.QGroupBox("Recorrência")
        bl = QtWidgets.QVBoxLayout(box)
        self.r_none = QtWidgets.QRadioButton("Sem recorrência")
        self.r_daily = QtWidgets.QRadioButton("Diariamente em HH:MM")
        self.r_interval = QtWidgets.QRadioButton("A cada N minutos")
        bl.addWidget(self.r_none)
        d_row = QtWidgets.QHBoxLayout()
        d_row.addSpacing(20)
        self.daily_edit = QtWidgets.QTimeEdit()
        self.daily_edit.setDisplayFormat("HH:mm")
        self.daily_edit.setTime(QtCore.QTime(9, 0))
        d_row.addWidget(self.r_daily)
        d_row.addWidget(self.daily_edit)
        d_row.addStretch(1)
        bl.addLayout(d_row)
        i_row = QtWidgets.QHBoxLayout()
        i_row.addSpacing(20)
        self.interval_edit = QtWidgets.QSpinBox()
        self.interval_edit.setRange(1, 60 * 24 * 30)
        self.interval_edit.setValue(60)
        self.interval_edit.setSuffix(" min")
        i_row.addWidget(self.r_interval)
        i_row.addWidget(self.interval_edit)
        i_row.addStretch(1)
        bl.addLayout(i_row)
        layout.addWidget(box)

        # popula recorrência atual
        keys = row.keys() if hasattr(row, "keys") else []
        if "repeat_daily_at" in keys and row["repeat_daily_at"]:
            self.r_daily.setChecked(True)
            h, m = row["repeat_daily_at"].split(":")
            self.daily_edit.setTime(QtCore.QTime(int(h), int(m)))
        elif "repeat_minutes" in keys and row["repeat_minutes"]:
            self.r_interval.setChecked(True)
            self.interval_edit.setValue(row["repeat_minutes"])
        else:
            self.r_none.setChecked(True)

        def _upd():
            self.daily_edit.setEnabled(self.r_daily.isChecked())
            self.interval_edit.setEnabled(self.r_interval.isChecked())
        for b in (self.r_none, self.r_daily, self.r_interval):
            b.toggled.connect(_upd)
        _upd()

        # is_draft (só mostrar se já é rascunho ou pendente)
        keys = row.keys() if hasattr(row, "keys") else []
        is_draft = ("is_draft" in keys) and bool(row["is_draft"])
        self.draft_chk = QtWidgets.QCheckBox("Salvar como rascunho (não publica até aprovar)")
        self.draft_chk.setChecked(is_draft)
        layout.addWidget(self.draft_chk)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _pick_image(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Selecionar imagem", "",
            "Imagens (*.png *.jpg *.jpeg *.gif *.webp)"
        )
        if path:
            self.image_path.setText(path)

    def _regenerate(self) -> None:
        """Manda composer reescrever o post com o feedback do avaliador."""
        comment = self.eval_comment.toPlainText().strip()
        if not comment:
            self.regen_status.setText("Sem comentário do avaliador.")
            return
        previous = self.text_edit.toPlainText().strip()
        if not previous:
            self.regen_status.setText("Texto vazio.")
            return

        # carrega tópico + notícias originais do banco (se houver)
        articles = db.get_source_articles(self._post_id)
        row = db.get_post(self._post_id)
        keys = row.keys() if (row and hasattr(row, "keys")) else []
        topic = row["source_topic"] if (row and "source_topic" in keys) else None

        self.regen_btn.setEnabled(False)
        self.regen_status.setText("Regerando...")

        def job():
            return composer.regenerate_post(
                topic=topic,
                articles=articles or None,
                previous_text=previous,
                evaluation_comment=comment,
            )

        def done(new_text: str):
            self.text_edit.setPlainText(new_text)
            # Limpa a avaliação anterior: o agente reavalia no próximo tick.
            db.clear_evaluation(self._post_id)
            self.score_lbl.setText("(reavaliação pendente)")
            self.score_lbl.setStyleSheet("color:#93c5fd;")
            self.eval_comment.clear()
            self.regen_btn.setEnabled(False)
            self.regen_status.setText("Texto regerado. Salve para aplicar.")

        def err(tb: str):
            self.regen_btn.setEnabled(True)
            self.regen_status.setText(tb.splitlines()[-1])
            print(tb, flush=True)

        run_in_thread(self, job, on_done=done, on_error=err)

    def values(self) -> dict:
        out: dict = {
            "text": self.text_edit.toPlainText().strip(),
            "image_path": self.image_path.text().strip() or None,
            "scheduled_at": self.when_edit.dateTime().toString("yyyy-MM-dd HH:mm"),
            "is_draft": self.draft_chk.isChecked(),
        }
        if self.r_daily.isChecked():
            out["repeat_daily_at"] = self.daily_edit.time().toString("HH:mm")
            out["repeat_minutes"] = None
        elif self.r_interval.isChecked():
            out["repeat_minutes"] = self.interval_edit.value()
            out["repeat_daily_at"] = None
        else:
            out["repeat_minutes"] = None
            out["repeat_daily_at"] = None
        return out


class QueueTab(QtWidgets.QWidget):
    queue_changed = QtCore.Signal()

    def __init__(self):
        super().__init__()
        layout = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Fila")
        title.setObjectName("section")
        top.addWidget(title)
        top.addStretch(1)
        self.pending_chk = QtWidgets.QCheckBox("só pendentes")
        self.pending_chk.toggled.connect(self.refresh)
        top.addWidget(self.pending_chk)
        btn_refresh = QtWidgets.QPushButton("Atualizar")
        btn_refresh.clicked.connect(self.refresh)
        top.addWidget(btn_refresh)
        self.btn_due = QtWidgets.QPushButton("Publicar vencidos")
        self.btn_due.setObjectName("primary")
        self.btn_due.clicked.connect(self.publish_due)
        top.addWidget(self.btn_due)
        layout.addLayout(top)

        msg_row = QtWidgets.QHBoxLayout()
        self.msg = QtWidgets.QLabel("")
        self.msg.setWordWrap(True)
        msg_row.addWidget(self.msg, 1)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 0)  # indeterminado
        self.progress.setTextVisible(False)
        self.progress.setMaximumWidth(140)
        self.progress.setMaximumHeight(14)
        self.progress.hide()
        msg_row.addWidget(self.progress)
        layout.addLayout(msg_row)

        self._busy = False
        self.table = QtWidgets.QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            "#", "Status", "Nota", "Agendado", "Repete", "Texto", "Img",
            "Ações", "Erro",
        ])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(
            5, QtWidgets.QHeaderView.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            8, QtWidgets.QHeaderView.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            7, QtWidgets.QHeaderView.Fixed
        )
        self.table.setColumnWidth(7, 280)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setEditTriggers(QtWidgets.QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QTableWidget.SelectRows)
        layout.addWidget(self.table, 1)

    def refresh(self):
        rows = db.list_posts(pending_only=self.pending_chk.isChecked())
        self.table.setRowCount(0)
        keys_all = rows[0].keys() if rows else []
        for r in rows:
            keys = r.keys() if hasattr(r, "keys") else keys_all
            is_draft = ("is_draft" in keys) and bool(r["is_draft"])
            if r["posted_at"]:
                status = "posted"
            elif r["error"]:
                status = "error"
            elif is_draft:
                status = "rascunho"
            else:
                status = "pending"
            i = self.table.rowCount()
            self.table.insertRow(i)
            self.table.setItem(i, 0, QtWidgets.QTableWidgetItem(f"#{r['id']}"))
            self.table.setItem(i, 1, QtWidgets.QTableWidgetItem(status))
            # Nota do agente avaliador (0-10) — vazio enquanto não avaliado
            score_txt = ""
            score_color: Optional[QtGui.QColor] = None
            if "eval_score" in keys and r["eval_score"] is not None:
                try:
                    sc = float(r["eval_score"])
                    score_txt = f"{sc:.1f}"
                    if sc < 5:
                        score_color = QtGui.QColor("#fca5a5")
                    elif sc < 7:
                        score_color = QtGui.QColor("#fcd34d")
                    else:
                        score_color = QtGui.QColor("#4ade80")
                except (TypeError, ValueError):
                    score_txt = ""
            elif status in ("rascunho",):
                score_txt = "…"  # ainda na fila do avaliador
            score_item = QtWidgets.QTableWidgetItem(score_txt)
            if score_color is not None:
                score_item.setForeground(score_color)
            if "eval_comment" in keys and r["eval_comment"]:
                score_item.setToolTip(r["eval_comment"])
            self.table.setItem(i, 2, score_item)
            self.table.setItem(i, 3, QtWidgets.QTableWidgetItem(_fmt_sched_br(r["scheduled_at"])))
            repeat_str = ""
            if "repeat_daily_at" in keys and r["repeat_daily_at"]:
                repeat_str = f"diário {r['repeat_daily_at']}"
            elif "repeat_minutes" in keys and r["repeat_minutes"]:
                repeat_str = f"a cada {r['repeat_minutes']}min"
            self.table.setItem(i, 4, QtWidgets.QTableWidgetItem(repeat_str))
            snippet = (r["text"] or "").replace("\n", " ")[:80]
            text_item = QtWidgets.QTableWidgetItem(snippet)
            text_item.setToolTip(r["text"] or "")
            self.table.setItem(i, 5, text_item)
            self.table.setItem(i, 6, QtWidgets.QTableWidgetItem("✓" if r["image_path"] else ""))
            # ações
            actions = QtWidgets.QWidget()
            ah = QtWidgets.QHBoxLayout(actions)
            ah.setContentsMargins(4, 4, 4, 4)
            ah.setSpacing(4)
            # primário (Aprovar/Publicar/Abrir)
            if status == "rascunho":
                b_main = QtWidgets.QPushButton("Aprovar")
                b_main.setObjectName("primary")
                b_main.clicked.connect(lambda _=False, pid=r["id"]: self.approve_one(pid))
                ah.addWidget(self._size_btn(b_main, 80))
            elif status != "posted":
                b_main = QtWidgets.QPushButton("Publicar")
                b_main.setObjectName("primary")
                b_main.clicked.connect(lambda _=False, pid=r["id"]: self.publish_one(pid))
                ah.addWidget(self._size_btn(b_main, 80))
            else:
                b_main = QtWidgets.QPushButton("Abrir")
                b_main.setObjectName("primary")
                b_main.clicked.connect(lambda _=False, urn=r["post_urn"]: self.open_post(urn))
                ah.addWidget(self._size_btn(b_main, 70))

            # Editar (só não-posted)
            if status != "posted":
                b_edit = QtWidgets.QPushButton("Editar")
                b_edit.clicked.connect(lambda _=False, pid=r["id"]: self.edit_one(pid))
                ah.addWidget(self._size_btn(b_edit, 70))

            # Apagar (sempre)
            b_del = QtWidgets.QPushButton("Apagar")
            b_del.setObjectName("danger")
            b_del.clicked.connect(lambda _=False, pid=r["id"]: self.delete_one(pid))
            ah.addWidget(self._size_btn(b_del, 70))

            self.table.setCellWidget(i, 7, actions)
            err_item = QtWidgets.QTableWidgetItem(r["error"] or "")
            if r["error"]:
                err_item.setForeground(QtGui.QColor("#fca5a5"))
            self.table.setItem(i, 8, err_item)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(1)
        self.table.resizeColumnToContents(2)
        self.table.resizeColumnToContents(3)
        self.table.resizeColumnToContents(4)
        self.table.resizeColumnToContents(6)

    # ---- ações
    def publish_one(self, post_id: int):
        if self._busy:
            return
        row = db.get_post(post_id)
        if row is None:
            return
        self._set_busy(True, f"Publicando #{post_id}...")
        self._with_client(lambda c: publish_row(c, row), self._on_publish_done)

    def publish_due(self):
        if self._busy:
            return
        rows = db.fetch_due()
        if not rows:
            self._set_msg("info", "Nada a publicar agora.")
            return
        self._set_busy(True, f"Publicando {len(rows)} post(s)...")

        def job(client):
            return [publish_row(client, r) for r in rows]

        def done(results):
            ok = sum(1 for r in results if r.get("ok"))
            fail = len(results) - ok
            kind = "err" if fail else "ok"
            self._set_busy(False)
            self._set_msg(kind, f"{ok} publicado(s), {fail} falha(s).")
            self.refresh()
            self.queue_changed.emit()

        self._with_client(job, done, finalize=False)

    @staticmethod
    def _size_btn(btn: QtWidgets.QPushButton, min_w: int) -> QtWidgets.QPushButton:
        btn.setMinimumWidth(min_w)
        return btn

    def edit_one(self, post_id: int):
        if self._busy:
            return
        row = db.get_post(post_id)
        if row is None:
            self._set_msg("err", f"#{post_id} não encontrado.")
            return
        dlg = EditPostDialog(self, row)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        vals = dlg.values()
        if not vals["text"]:
            self._set_msg("err", "Texto vazio — nada alterado.")
            return
        if vals["image_path"] and not Path(vals["image_path"]).is_file():
            self._set_msg("err", f"Imagem não encontrada: {vals['image_path']}")
            return
        try:
            db.update_post(post_id, **vals)
        except ValueError as exc:
            self._set_msg("err", str(exc))
            return
        # Se o texto mudou, joga fora a avaliação antiga — agente reavalia.
        if (vals.get("text") or "") != (row["text"] or ""):
            db.clear_evaluation(post_id)
        self._set_msg("ok", f"Post #{post_id} atualizado.")
        self.refresh()
        self.queue_changed.emit()

    def approve_one(self, post_id: int):
        if self._busy:
            return
        n = db.approve_post(post_id)
        if n == 0:
            self._set_msg("err", f"#{post_id} não encontrado.")
            return
        self._set_msg("ok", f"Rascunho #{post_id} aprovado — publicará no próximo ciclo.")
        self.refresh()
        self.queue_changed.emit()

    def delete_one(self, post_id: int):
        if self._busy:
            return
        if QtWidgets.QMessageBox.question(
            self, "Apagar", f"Apagar post #{post_id}?"
        ) != QtWidgets.QMessageBox.Yes:
            return
        db.delete_post(post_id)
        self.refresh()
        self.queue_changed.emit()

    def open_post(self, urn: str):
        if not urn:
            return
        from urllib.parse import quote
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(
            f"https://www.linkedin.com/feed/update/{quote(urn, safe='')}"
        ))

    def _with_client(self, job, on_done, finalize: bool = True):
        def runner():
            client = LinkedInClient.from_env()
            return job(client)

        def err(tb):
            self._set_busy(False)
            self._set_msg("err", tb.splitlines()[-1])
            print(tb, flush=True)

        run_in_thread(
            self, runner,
            on_done=lambda r: (on_done(r) if callable(on_done) else None),
            on_error=err,
        )
        # `finalize` controla se _on_publish_done chama _set_busy(False);
        # publish_due cuida do busy explicitamente em done().

    def _on_publish_done(self, res):
        self._set_busy(False)
        if res.get("ok"):
            self._set_msg("ok", f"Post #{res['id']} publicado → {res.get('urn')}")
        else:
            self._set_msg("err", f"Falha #{res['id']}: {res.get('error')}")
        self.refresh()
        self.queue_changed.emit()

    def _set_msg(self, kind, text):
        obj = {"ok": "msgOk", "err": "msgErr", "info": "msgInfo"}[kind]
        self.msg.setObjectName(obj)
        self.msg.setText(text)
        self.msg.style().unpolish(self.msg)
        self.msg.style().polish(self.msg)

    def _set_busy(self, busy: bool, text: str = ""):
        self._busy = busy
        if busy:
            self.progress.show()
            self._set_msg("info", text or "Executando...")
            self.btn_due.setEnabled(False)
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
            # desabilita botões em cellWidgets
            for i in range(self.table.rowCount()):
                cw = self.table.cellWidget(i, 6)
                if cw:
                    for b in cw.findChildren(QtWidgets.QPushButton):
                        b.setEnabled(False)
        else:
            self.progress.hide()
            self.btn_due.setEnabled(True)
            QtWidgets.QApplication.restoreOverrideCursor()


class JobDialog(QtWidgets.QDialog):
    """Cria/edita um research_job. Job só gera rascunho — agenda é na Fila."""

    def __init__(self, parent=None, job=None):
        super().__init__(parent)
        self.setWindowTitle("Job de pesquisa")
        self.resize(420, 200)
        form = QtWidgets.QFormLayout(self)

        info = QtWidgets.QLabel(
            "Jobs só geram rascunhos. Defina horário de publicação na Fila."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #94a3b8;")
        form.addRow(info)

        self.topic = QtWidgets.QLineEdit()
        self.topic.setPlaceholderText("ex: inteligência artificial generativa")
        form.addRow("Tópico:", self.topic)

        self.gen_time = QtWidgets.QTimeEdit()
        self.gen_time.setDisplayFormat("HH:mm")
        self.gen_time.setTime(QtCore.QTime(8, 0))
        form.addRow("Gerar às:", self.gen_time)

        self.max_results = QtWidgets.QSpinBox()
        self.max_results.setRange(1, 10)
        self.max_results.setValue(5)
        form.addRow("Máx notícias:", self.max_results)

        self.enabled = QtWidgets.QCheckBox("Habilitado")
        self.enabled.setChecked(True)
        form.addRow(self.enabled)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

        if job is not None:
            self.topic.setText(job["topic"] or "")
            g = (job["generate_at"] or "08:00").split(":")
            self.gen_time.setTime(QtCore.QTime(int(g[0]), int(g[1])))
            self.max_results.setValue(job["max_results"] or 5)
            self.enabled.setChecked(bool(job["enabled"]))

    def values(self) -> dict:
        return {
            "topic": self.topic.text().strip(),
            "generate_at": self.gen_time.time().toString("HH:mm"),
            "max_results": self.max_results.value(),
            "enabled": self.enabled.isChecked(),
        }


class JobsPanel(QtWidgets.QWidget):
    """Painel de jobs de pesquisa recorrente. Sem controles de scheduler."""

    jobs_changed = QtCore.Signal()

    def __init__(self):
        super().__init__()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        jobs_header = QtWidgets.QHBoxLayout()
        jt = QtWidgets.QLabel("Jobs de pesquisa recorrente")
        jt.setObjectName("section")
        jobs_header.addWidget(jt)
        jobs_header.addStretch(1)
        self.btn_new = QtWidgets.QPushButton("Novo job")
        self.btn_new.setObjectName("primary")
        self.btn_new.clicked.connect(self.new_job)
        jobs_header.addWidget(self.btn_new)
        self.btn_refresh_jobs = QtWidgets.QPushButton("Atualizar")
        self.btn_refresh_jobs.clicked.connect(self.refresh_jobs)
        jobs_header.addWidget(self.btn_refresh_jobs)
        layout.addLayout(jobs_header)

        self.jobs_table = QtWidgets.QTableWidget(0, 6)
        self.jobs_table.setHorizontalHeaderLabels([
            "#", "Tópico", "Gerar", "Habilitado", "Última exec", "Ações"
        ])
        self.jobs_table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.Stretch
        )
        self.jobs_table.horizontalHeader().setSectionResizeMode(
            5, QtWidgets.QHeaderView.Fixed
        )
        self.jobs_table.setColumnWidth(5, 280)
        self.jobs_table.verticalHeader().setVisible(False)
        self.jobs_table.verticalHeader().setDefaultSectionSize(44)
        self.jobs_table.setEditTriggers(QtWidgets.QTableWidget.NoEditTriggers)
        self.jobs_table.setSelectionBehavior(QtWidgets.QTableWidget.SelectRows)
        layout.addWidget(self.jobs_table, 1)

        self.refresh_jobs()

    # ---- jobs CRUD
    def refresh_jobs(self):
        jobs = db.list_jobs()
        self.jobs_table.setRowCount(0)
        for j in jobs:
            i = self.jobs_table.rowCount()
            self.jobs_table.insertRow(i)
            self.jobs_table.setItem(i, 0, QtWidgets.QTableWidgetItem(f"#{j['id']}"))
            self.jobs_table.setItem(i, 1, QtWidgets.QTableWidgetItem(j["topic"]))
            self.jobs_table.setItem(i, 2, QtWidgets.QTableWidgetItem(j["generate_at"]))
            self.jobs_table.setItem(i, 3, QtWidgets.QTableWidgetItem("✓" if j["enabled"] else ""))
            ran_today = _ran_today_local(j["last_run_at"])
            last = _fmt_sched_br(j["last_run_at"]) or "—"
            if ran_today:
                last = f"{last} · rodou hoje"
            if j["last_error"]:
                last = f"{last} · ERRO: {j['last_error'][:40]}"
            last_item = QtWidgets.QTableWidgetItem(last)
            if j["last_error"]:
                last_item.setForeground(QtGui.QColor("#fca5a5"))
            elif ran_today:
                last_item.setForeground(QtGui.QColor("#fbbf24"))
            self.jobs_table.setItem(i, 4, last_item)

            actions = QtWidgets.QWidget()
            ah = QtWidgets.QHBoxLayout(actions)
            ah.setContentsMargins(4, 4, 4, 4)
            ah.setSpacing(4)
            b_run = QtWidgets.QPushButton("Rodar agora")
            b_run.setMinimumWidth(90)
            b_run.clicked.connect(lambda _=False, jid=j["id"]: self.run_now(jid))
            ah.addWidget(b_run)
            b_edit = QtWidgets.QPushButton("Editar")
            b_edit.setMinimumWidth(70)
            b_edit.setObjectName("primary")
            b_edit.clicked.connect(lambda _=False, jid=j["id"]: self.edit_job(jid))
            ah.addWidget(b_edit)
            b_del = QtWidgets.QPushButton("Apagar")
            b_del.setMinimumWidth(70)
            b_del.setObjectName("danger")
            b_del.clicked.connect(lambda _=False, jid=j["id"]: self.delete_job(jid))
            ah.addWidget(b_del)
            self.jobs_table.setCellWidget(i, 5, actions)
        self.jobs_table.resizeColumnToContents(0)
        self.jobs_table.resizeColumnToContents(2)
        self.jobs_table.resizeColumnToContents(3)

    def new_job(self):
        dlg = JobDialog(self)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        v = dlg.values()
        try:
            db.add_job(**v)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Erro", str(exc))
            return
        self.refresh_jobs()
        self.jobs_changed.emit()

    def edit_job(self, jid: int):
        job = db.get_job(jid)
        if job is None:
            return
        dlg = JobDialog(self, job=job)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        try:
            db.update_job(jid, **dlg.values())
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Erro", str(exc))
            return
        self.refresh_jobs()
        self.jobs_changed.emit()

    def delete_job(self, jid: int):
        if QtWidgets.QMessageBox.question(
            self, "Apagar", f"Apagar job #{jid}?"
        ) != QtWidgets.QMessageBox.Yes:
            return
        db.delete_job(jid)
        self.refresh_jobs()
        self.jobs_changed.emit()

    def run_now(self, jid: int):
        """Executa um job ignorando o horário (usa now_local como gen_dt)."""
        job = db.get_job(jid)
        if job is None:
            return

        if _ran_today_local(job["last_run_at"]):
            r = QtWidgets.QMessageBox.question(
                self, "Rodar de novo?",
                f"Job #{jid} já rodou hoje "
                f"({_fmt_sched_br(job['last_run_at'])}).\n"
                "Executar novamente? Vai gerar outro rascunho na fila.",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if r != QtWidgets.QMessageBox.Yes:
                return

        def runner():
            # cria um "dummy thread" só para reutilizar _run_job
            dummy = SchedulerThread(interval=60, on_tick=lambda _d: None)
            dummy._run_job(job, datetime.now())
            return True

        def done(_ok):
            self.refresh_jobs()
            self.jobs_changed.emit()
            QtWidgets.QMessageBox.information(
                self, "Job", f"Job #{jid} executado (veja Fila)."
            )

        def err(tb):
            db.mark_job_run(jid, error=tb.splitlines()[-1])
            self.refresh_jobs()
            QtWidgets.QMessageBox.warning(self, "Falha", tb.splitlines()[-1])
            print(tb, flush=True)

        run_in_thread(self, runner, on_done=done, on_error=err)


class ResearchTab(QtWidgets.QWidget):
    """Pesquisa notícias sobre um tópico, gera draft via OpenAI, salva como rascunho.
    Também hospeda o painel de jobs de pesquisa recorrente."""

    draft_created = QtCore.Signal()
    jobs_changed = QtCore.Signal()

    def __init__(self):
        super().__init__()
        layout = QtWidgets.QVBoxLayout(self)

        title = QtWidgets.QLabel("Pesquisar notícias e gerar post")
        title.setObjectName("section")
        layout.addWidget(title)

        desc = QtWidgets.QLabel(
            "Busca notícias recentes (NewsAPI com fallback Google News RSS) "
            "e gera um draft via OpenAI. Sai como rascunho na fila — você "
            "revisa e aprova antes de publicar."
        )
        desc.setObjectName("muted")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # ---- entrada
        form = QtWidgets.QHBoxLayout()
        form.addWidget(QtWidgets.QLabel("Tópico:"))
        self.topic_edit = QtWidgets.QLineEdit()
        self.topic_edit.setPlaceholderText("ex: inteligência artificial generativa")
        self.topic_edit.returnPressed.connect(self.run_search)
        form.addWidget(self.topic_edit, 1)
        form.addWidget(QtWidgets.QLabel("Máx:"))
        self.max_edit = QtWidgets.QSpinBox()
        self.max_edit.setRange(1, 10)
        self.max_edit.setValue(5)
        form.addWidget(self.max_edit)
        self.search_btn = QtWidgets.QPushButton("Buscar")
        self.search_btn.clicked.connect(self.run_search)
        form.addWidget(self.search_btn)
        self.gen_btn = QtWidgets.QPushButton("Gerar post")
        self.gen_btn.setObjectName("primary")
        self.gen_btn.setEnabled(False)
        self.gen_btn.clicked.connect(self.run_generate)
        form.addWidget(self.gen_btn)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setMaximumWidth(140)
        self.progress.setMaximumHeight(14)
        self.progress.hide()
        form.addWidget(self.progress)
        layout.addLayout(form)

        # ---- splitter notícias / draft
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        layout.addWidget(split, 1)

        # notícias
        news_box = QtWidgets.QFrame()
        news_box.setObjectName("card")
        nl = QtWidgets.QVBoxLayout(news_box)
        nl.addWidget(self._label_section("Notícias encontradas"))
        self.news_list = QtWidgets.QTextBrowser()
        self.news_list.setOpenExternalLinks(True)
        self.news_list.setPlaceholderText("(nenhuma busca ainda)")
        nl.addWidget(self.news_list, 1)
        split.addWidget(news_box)

        # draft
        draft_box = QtWidgets.QFrame()
        draft_box.setObjectName("card")
        dl = QtWidgets.QVBoxLayout(draft_box)
        dl.addWidget(self._label_section("Draft gerado (editável)"))
        self.draft_edit = QtWidgets.QPlainTextEdit()
        self.draft_edit.setPlaceholderText("(o texto gerado aparece aqui — pode editar antes de salvar)")
        dl.addWidget(self.draft_edit, 1)

        # imagem da notícia (auto-baixada após Gerar)
        dl.addWidget(self._label_section("Imagem (auto da notícia)"))
        self.draft_image_preview = QtWidgets.QLabel()
        self.draft_image_preview.setAlignment(QtCore.Qt.AlignCenter)
        self.draft_image_preview.setMinimumHeight(120)
        self.draft_image_preview.setStyleSheet(
            "border: 1px dashed #2a313a; border-radius: 6px; color: #8b949e;"
        )
        self.draft_image_preview.setText("(sem imagem)")
        dl.addWidget(self.draft_image_preview)

        img_btns = QtWidgets.QHBoxLayout()
        self.img_path_lbl = QtWidgets.QLabel("")
        self.img_path_lbl.setObjectName("muted")
        self.img_path_lbl.setWordWrap(True)
        img_btns.addWidget(self.img_path_lbl, 1)
        self.btn_img_pick = QtWidgets.QPushButton("Trocar...")
        self.btn_img_pick.clicked.connect(self._pick_image)
        img_btns.addWidget(self.btn_img_pick)
        self.btn_img_clear = QtWidgets.QPushButton("Remover")
        self.btn_img_clear.clicked.connect(self._clear_image)
        img_btns.addWidget(self.btn_img_clear)
        dl.addLayout(img_btns)

        save_row = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton("Salvar como rascunho")
        self.save_btn.setObjectName("primary")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self.save_draft)
        save_row.addWidget(self.save_btn)
        save_row.addStretch(1)
        dl.addLayout(save_row)
        split.addWidget(draft_box)
        split.setSizes([400, 600])

        self.msg = QtWidgets.QLabel("")
        self.msg.setWordWrap(True)
        layout.addWidget(self.msg)

        # ---- jobs recorrentes (mesmo tema)
        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.HLine)
        sep.setStyleSheet("color: #2a313a;")
        layout.addWidget(sep)
        self.jobs = JobsPanel()
        self.jobs.jobs_changed.connect(self.jobs_changed.emit)
        layout.addWidget(self.jobs, 1)

        self._articles: list[dict] = []
        self._draft_image: Optional[str] = None

    def _label_section(self, text):
        l = QtWidgets.QLabel(text)
        l.setObjectName("muted")
        return l

    # ---- ações
    def run_search(self):
        """Busca notícias apenas — não chama LLM."""
        topic = self.topic_edit.text().strip()
        if not topic:
            self._set_msg("err", "Informe um tópico.")
            return
        self._set_busy(True, "Buscando notícias...")

        n = self.max_edit.value()

        def job():
            arts = news.fetch_news(topic, max_results=n)
            return arts

        def done(arts):
            self._articles = arts
            self._render_news(arts)
            self._set_busy(False)
            if not arts:
                self.gen_btn.setEnabled(False)
                self._set_msg("err", "Nenhuma notícia encontrada.")
            else:
                self.gen_btn.setEnabled(True)
                self._set_msg("ok", f"{len(arts)} notícia(s) encontradas. Revise antes de gerar o post.")

        def err(tb):
            self._set_busy(False)
            self._set_msg("err", tb.splitlines()[-1])
            print(tb, flush=True)

        run_in_thread(self, job, on_done=done, on_error=err)

    def run_generate(self):
        """Gera o draft usando as notícias já buscadas + baixa imagem."""
        if not self._articles:
            self._set_msg("err", "Busque notícias antes de gerar.")
            return
        topic = self.topic_edit.text().strip()
        self._set_busy(True, "Gerando texto + baixando imagem...")

        articles = list(self._articles)

        def job():
            text = composer.compose_post(topic, articles)
            image_path = news.pick_image(articles)
            return {"text": text, "image": image_path}

        def done(res):
            self.draft_edit.setPlainText(res["text"])
            if res.get("image"):
                self._set_image(res["image"])
                extra = " (com imagem)"
            else:
                self._clear_image()
                extra = " (sem imagem — fonte não fornece)"
            self.save_btn.setEnabled(True)
            self._set_busy(False)
            self.gen_btn.setEnabled(True)
            self._set_msg("ok", f"Draft gerado{extra}. Revise e salve.")

        def err(tb):
            self._set_busy(False)
            self.gen_btn.setEnabled(True)
            self._set_msg("err", tb.splitlines()[-1])
            print(tb, flush=True)

        run_in_thread(self, job, on_done=done, on_error=err)

    def _set_image(self, path: str):
        self._draft_image = path
        self.img_path_lbl.setText(path)
        pix = QtGui.QPixmap(path)
        if pix.isNull():
            self.draft_image_preview.setText("(falha ao carregar preview)")
        else:
            self.draft_image_preview.setPixmap(pix.scaled(
                400, 200,
                QtCore.Qt.KeepAspectRatio,
                QtCore.Qt.SmoothTransformation,
            ))

    def _clear_image(self):
        self._draft_image = None
        self.img_path_lbl.setText("")
        self.draft_image_preview.clear()
        self.draft_image_preview.setText("(sem imagem)")

    def _pick_image(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Selecionar imagem", "",
            "Imagens (*.png *.jpg *.jpeg *.gif *.webp)"
        )
        if path:
            self._set_image(path)

    def save_draft(self):
        text = self.draft_edit.toPlainText().strip()
        if not text:
            self._set_msg("err", "Draft vazio.")
            return
        image = self._draft_image if self._draft_image and Path(self._draft_image).is_file() else None
        topic = self.topic_edit.text().strip() or None
        arts = list(self._articles) if self._articles else None
        try:
            post_id = db.add_post(
                text=text, image_path=image, is_draft=True,
                source_topic=topic, source_articles=arts,
            )
        except ValueError as exc:
            self._set_msg("err", str(exc))
            return
        suffix = " com imagem" if image else ""
        self._set_msg("ok", f"Rascunho #{post_id} salvo{suffix}. Aprove na aba Fila.")
        self.draft_created.emit()

    def _render_news(self, arts: list[dict]):
        html_parts = []
        for i, a in enumerate(arts, 1):
            title = a.get("title") or "(sem título)"
            url = a.get("url") or ""
            src = a.get("source") or ""
            when = a.get("published_at") or ""
            desc = a.get("description") or ""
            link = f'<a href="{url}" style="color:#93c5fd">{title}</a>' if url else title
            html_parts.append(
                f"<p><b>[{i}]</b> {link}<br>"
                f"<span style='color:#8b949e'>{src} · {when}</span><br>"
                f"{desc}</p>"
            )
        self.news_list.setHtml("\n".join(html_parts) or "<p>(vazio)</p>")

    def _set_msg(self, kind, text):
        obj = {"ok": "msgOk", "err": "msgErr", "info": "msgInfo"}[kind]
        self.msg.setObjectName(obj)
        self.msg.setText(text)
        self.msg.style().unpolish(self.msg)
        self.msg.style().polish(self.msg)

    def _set_busy(self, busy, text=""):
        if busy:
            self.progress.show()
            self.search_btn.setEnabled(False)
            self.gen_btn.setEnabled(False)
            self.save_btn.setEnabled(False)
            self._set_msg("info", text or "Executando...")
        else:
            self.progress.hide()
            self.search_btn.setEnabled(True)
            # gen_btn re-habilitado pelos callers conforme contexto


class SettingsTab(QtWidgets.QWidget):
    """Edita variáveis do .env via UI. Reusa `auth.update_env` (preserva comentários)."""

    saved = QtCore.Signal()

    # (key, label, is_secret)
    SECTIONS = [
        ("LinkedIn API", [
            ("LINKEDIN_CLIENT_ID", "Client ID", False),
            ("LINKEDIN_CLIENT_SECRET", "Client Secret", True),
            ("LINKEDIN_REDIRECT_URI", "Redirect URI", False),
            ("LINKEDIN_API_VERSION", "API Version (YYYYMM)", False),
        ]),
        ("LinkedIn Auth (preenchido por auth.py)", [
            ("LINKEDIN_ACCESS_TOKEN", "Access Token", True),
            ("LINKEDIN_URN", "URN", False),
            ("LINKEDIN_TOKEN_EXPIRES_AT", "Token expira em", False),
        ]),
        ("OpenAI (composer)", [
            ("OPENAI_API_KEY", "API Key", True),
            ("OPENAI_MODEL", "Model", False),
            ("COMPOSER_LANGUAGE", "Idioma", False),
        ]),
        ("NewsAPI (opcional)", [
            ("NEWSAPI_KEY", "API Key", True),
        ]),
    ]

    auth_done = QtCore.Signal()

    def __init__(self):
        super().__init__()
        outer = QtWidgets.QVBoxLayout(self)

        title = QtWidgets.QLabel("Configurações (.env)")
        title.setObjectName("section")
        outer.addWidget(title)

        desc = QtWidgets.QLabel(
            "Edita o arquivo .env do projeto. Comentários e linhas extras são preservados. "
            "Campos sensíveis são mascarados (clique no olho para ver)."
        )
        desc.setObjectName("muted")
        desc.setWordWrap(True)
        outer.addWidget(desc)

        # ---- card de autenticação LinkedIn
        auth_card = QtWidgets.QFrame()
        auth_card.setObjectName("card")
        ac = QtWidgets.QVBoxLayout(auth_card)
        ac_title = QtWidgets.QLabel("Autenticação LinkedIn")
        ac_title.setStyleSheet("font-weight: 600;")
        ac.addWidget(ac_title)
        ac_hint = QtWidgets.QLabel(
            "Preencha CLIENT_ID + CLIENT_SECRET abaixo e clique em Autenticar agora. "
            "O fluxo abre o navegador e captura o callback em http://localhost:8000/callback."
        )
        ac_hint.setObjectName("muted")
        ac_hint.setWordWrap(True)
        ac.addWidget(ac_hint)
        ac_row = QtWidgets.QHBoxLayout()
        self.auth_btn = QtWidgets.QPushButton("Autenticar agora")
        self.auth_btn.setObjectName("primary")
        self.auth_btn.clicked.connect(self.start_auth)
        ac_row.addWidget(self.auth_btn)
        self.auth_progress = QtWidgets.QProgressBar()
        self.auth_progress.setRange(0, 0)
        self.auth_progress.setTextVisible(False)
        self.auth_progress.setMaximumWidth(140)
        self.auth_progress.setMaximumHeight(14)
        self.auth_progress.hide()
        ac_row.addWidget(self.auth_progress)
        ac_row.addStretch(1)
        ac.addLayout(ac_row)
        self.auth_msg = QtWidgets.QLabel("")
        self.auth_msg.setWordWrap(True)
        ac.addWidget(self.auth_msg)
        outer.addWidget(auth_card)

        # área rolável caso a janela seja pequena
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        outer.addWidget(scroll, 1)
        inner = QtWidgets.QWidget()
        scroll.setWidget(inner)
        body = QtWidgets.QVBoxLayout(inner)
        body.setSpacing(14)

        self._fields: dict[str, QtWidgets.QLineEdit] = {}

        for sec_title, keys in self.SECTIONS:
            card = QtWidgets.QFrame()
            card.setObjectName("card")
            cl = QtWidgets.QVBoxLayout(card)
            sec_lbl = QtWidgets.QLabel(sec_title)
            sec_lbl.setStyleSheet("font-weight: 600; margin-bottom: 4px;")
            cl.addWidget(sec_lbl)
            form = QtWidgets.QFormLayout()
            form.setLabelAlignment(QtCore.Qt.AlignLeft)
            cl.addLayout(form)
            for key, label, secret in keys:
                edit = QtWidgets.QLineEdit()
                if secret:
                    edit.setEchoMode(QtWidgets.QLineEdit.Password)
                row = QtWidgets.QHBoxLayout()
                row.setContentsMargins(0, 0, 0, 0)
                row.addWidget(edit, 1)
                if secret:
                    eye = QtWidgets.QPushButton("👁")
                    eye.setCheckable(True)
                    eye.setMaximumWidth(32)
                    eye.toggled.connect(lambda c, e=edit: e.setEchoMode(
                        QtWidgets.QLineEdit.Normal if c else QtWidgets.QLineEdit.Password
                    ))
                    row.addWidget(eye)
                holder = QtWidgets.QWidget()
                holder.setLayout(row)
                form.addRow(f"{label} ({key})", holder)
                self._fields[key] = edit

            body.addWidget(card)

        body.addStretch(1)

        # botões
        btns = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton("Salvar")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self.save)
        btns.addWidget(self.save_btn)
        self.reload_btn = QtWidgets.QPushButton("Recarregar do disco")
        self.reload_btn.clicked.connect(self.reload)
        btns.addWidget(self.reload_btn)
        btns.addStretch(1)
        outer.addLayout(btns)

        self.msg = QtWidgets.QLabel("")
        self.msg.setWordWrap(True)
        outer.addWidget(self.msg)

        self.reload()

    def reload(self):
        from dotenv import load_dotenv
        load_dotenv(BASE_DIR / ".env", override=True)
        for key, edit in self._fields.items():
            edit.setText(os.getenv(key, "") or "")
        self._set_msg("info", "Valores carregados do .env.")

    def save(self):
        updates: dict[str, str] = {}
        for key, edit in self._fields.items():
            updates[key] = edit.text().strip()
        try:
            auth_module.update_env(updates)
        except Exception as exc:  # noqa: BLE001
            self._set_msg("err", f"Falha ao salvar: {exc}")
            return
        # re-load no processo
        from dotenv import load_dotenv
        load_dotenv(BASE_DIR / ".env", override=True)
        self._set_msg("ok", f".env atualizado ({len(updates)} chaves).")
        self.saved.emit()

    def _set_msg(self, kind, text):
        obj = {"ok": "msgOk", "err": "msgErr", "info": "msgInfo"}[kind]
        self.msg.setObjectName(obj)
        self.msg.setText(text)
        self.msg.style().unpolish(self.msg)
        self.msg.style().polish(self.msg)

    def _set_auth_msg(self, kind, text):
        obj = {"ok": "msgOk", "err": "msgErr", "info": "msgInfo"}[kind]
        self.auth_msg.setObjectName(obj)
        self.auth_msg.setText(text)
        self.auth_msg.style().unpolish(self.auth_msg)
        self.auth_msg.style().polish(self.auth_msg)

    def start_auth(self):
        # garante que credenciais atuais estão no .env antes do fluxo
        self.save()
        self.auth_btn.setEnabled(False)
        self.auth_progress.show()
        self._set_auth_msg("info", "Abrindo navegador. Conclua o login...")

        def job():
            return auth_module.main()

        def done(rc):
            self.auth_btn.setEnabled(True)
            self.auth_progress.hide()
            self.reload()  # ACCESS_TOKEN/URN/EXPIRES_AT foram gravados
            if rc == 0:
                self._set_auth_msg("ok", "Autenticado com sucesso.")
            else:
                self._set_auth_msg("err", f"auth retornou código {rc}")
            self.auth_done.emit()

        def err(tb):
            self.auth_btn.setEnabled(True)
            self.auth_progress.hide()
            self._set_auth_msg("err", tb.splitlines()[-1])
            print(tb, flush=True)
            self.auth_done.emit()

        run_in_thread(self, job, on_done=done, on_error=err)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("linkedinAuto")
        self.resize(1100, 760)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # topbar
        top = QtWidgets.QFrame()
        top.setObjectName("topbar")
        top_l = QtWidgets.QHBoxLayout(top)
        top_l.setContentsMargins(20, 12, 20, 12)
        logo = QtWidgets.QLabel("in")
        logo.setObjectName("logo")
        brand = QtWidgets.QLabel("linkedinAuto")
        brand.setObjectName("brand")
        top_l.addWidget(logo)
        top_l.addWidget(brand)
        top_l.addStretch(1)
        self.pill = StatusPill()
        top_l.addWidget(self.pill)
        root.addWidget(top)

        # tabs
        self.tabs = QtWidgets.QTabWidget()
        self.compose = ComposeTab()
        self.research = ResearchTab()
        self.queue = QueueTab()
        self.settings = SettingsTab()
        self.tabs.addTab(self.compose, "Novo post")
        self.tabs.addTab(self.research, "Pesquisar")
        self.tabs.addTab(self.queue, "Fila")
        self.tabs.addTab(self.settings, "Configurações")
        root.addWidget(self.tabs, 1)

        # status bar (estado do scheduler invisível)
        self.statusBar().showMessage("Iniciando scheduler...")
        self.sched_lbl = QtWidgets.QLabel("scheduler: parado")
        self.statusBar().addPermanentWidget(self.sched_lbl)

        # scheduler em background (sempre on enquanto app aberto)
        self.sched_thread: Optional[SchedulerThread] = None
        self._start_scheduler()

        # conexões
        self.compose.post_added.connect(self.queue.refresh)
        self.compose.post_added.connect(self._refresh_pill)
        self.research.draft_created.connect(self.queue.refresh)
        self.research.jobs_changed.connect(self.queue.refresh)
        self.queue.queue_changed.connect(self._refresh_pill)
        self.settings.saved.connect(self._refresh_pill)
        self.settings.auth_done.connect(self._refresh_pill)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.queue.refresh()
        self._refresh_pill()

    def _start_scheduler(self, interval: int = 60):
        if self.sched_thread and self.sched_thread.running:
            return
        self.sched_thread = SchedulerThread(
            interval=interval,
            on_tick=lambda d: QtCore.QTimer.singleShot(
                0, self, lambda: self._on_sched_tick(d)
            ),
        )
        self.sched_thread.start()
        self.sched_lbl.setText(f"scheduler: rodando (a cada {interval}s)")

    def _on_sched_tick(self, d: dict):
        if "error" in d:
            self.statusBar().showMessage(f"scheduler erro: {d['error']}")
            return
        parts = [f"{d.get('ok', 0)} pub", f"{d.get('fail', 0)} falha(s)"]
        if d.get("jobs_run") or d.get("jobs_err"):
            parts.append(f"jobs: {d.get('jobs_run', 0)} ok / {d.get('jobs_err', 0)} falha(s)")
        if d.get("evals_ok") or d.get("evals_err"):
            parts.append(
                f"avaliações: {d.get('evals_ok', 0)} ok / "
                f"{d.get('evals_err', 0)} falha(s)"
            )
        self.statusBar().showMessage(
            f"último ciclo {d.get('last_tick', '')}: " + " · ".join(parts)
        )
        if d.get("jobs_run") or d.get("evals_ok"):
            self.research.jobs.refresh_jobs()
            self.queue.refresh()

    def _refresh_pill(self):
        self.pill.set_state(read_env_status())

    def _on_tab_changed(self, idx: int):
        w = self.tabs.widget(idx)
        if w is self.queue:
            self.queue.refresh()
        elif w is self.compose:
            self.compose.refresh_file_list()
        elif w is self.research:
            self.research.jobs.refresh_jobs()
        elif w is self.settings:
            self.settings.reload()

    def closeEvent(self, event):  # noqa: N802
        if self.sched_thread:
            self.sched_thread.stop()
        super().closeEvent(event)


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet(DARK_QSS)
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
