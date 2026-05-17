"""
auth.py
=======

Executa o fluxo OAuth 2.0 (3-legged) com o LinkedIn:

  1) abre o navegador na tela de autorização do LinkedIn;
  2) sobe um servidor HTTP local em http://localhost:8000/callback;
  3) captura o `code` que o LinkedIn devolve via redirect;
  4) troca o `code` por um `access_token`;
  5) consulta /v2/userinfo para obter o URN do perfil pessoal;
  6) salva access_token, URN e expiração no arquivo `.env`.

Rode este script:
  - na primeira vez (para autenticar)
  - sempre que o token expirar (a cada ~60 dias)

Pré-requisitos:
  - LINKEDIN_CLIENT_ID e LINKEDIN_CLIENT_SECRET preenchidos em `.env`
  - URI `http://localhost:8000/callback` cadastrado no app em
    https://developer.linkedin.com/ → seu app → Auth → Authorized redirect URLs
"""

from __future__ import annotations

import os
import secrets
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# Escopos necessários:
#   openid + profile → para descobrir o URN via /v2/userinfo
#   w_member_social  → para publicar posts no feed
SCOPES = "openid profile w_member_social"

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"


# ---------------------------------------------------------------------------
# Captura do `code` via servidor HTTP local
# ---------------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    """Recebe a requisição GET /callback?code=...&state=... do LinkedIn."""

    # variável de classe para devolver o resultado ao chamador
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 (método imposto pela stdlib)
        params = parse_qs(urlparse(self.path).query)
        _CallbackHandler.result = {k: v[0] for k, v in params.items()}

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if "code" in _CallbackHandler.result:
            html = "<h2>OK — pode fechar esta aba.</h2>"
        else:
            html = f"<h2>Erro:</h2><pre>{_CallbackHandler.result}</pre>"
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, *args, **kwargs) -> None:  # silencia logs HTTP
        pass


def _capture_code(redirect_uri: str, expected_state: str) -> str:
    """Abre o servidor local, espera UMA requisição e devolve o `code`."""
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8000

    print(f"Aguardando callback em {redirect_uri} ...")
    server = HTTPServer((host, port), _CallbackHandler)
    server.handle_request()  # bloqueia até uma requisição chegar

    result = _CallbackHandler.result
    if "error" in result:
        raise RuntimeError(f"LinkedIn retornou erro: {result}")
    if result.get("state") != expected_state:
        raise RuntimeError(
            f"Mismatch de state — possível CSRF. Esperado={expected_state} "
            f"recebido={result.get('state')}"
        )
    if "code" not in result:
        raise RuntimeError(f"Resposta sem `code`: {result}")
    return result["code"]


# ---------------------------------------------------------------------------
# Trocas com a API do LinkedIn
# ---------------------------------------------------------------------------

def _exchange_code_for_token(
    code: str, client_id: str, client_secret: str, redirect_uri: str
) -> dict:
    """Troca o `authorization_code` por `access_token`."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_urn(access_token: str) -> str:
    """Consulta /userinfo (OIDC) e devolve o URN no formato urn:li:person:<sub>."""
    resp = requests.get(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    resp.raise_for_status()
    sub = resp.json()["sub"]
    return f"urn:li:person:{sub}"


# ---------------------------------------------------------------------------
# Gravação do `.env`
# ---------------------------------------------------------------------------

def update_env(updates: dict[str, str]) -> None:
    """
    Atualiza chaves no arquivo `.env` preservando demais linhas e comentários.
    Cria o arquivo se não existir.
    """
    if not ENV_PATH.exists():
        ENV_PATH.write_text("", encoding="utf-8")

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    new_lines: list[str] = []

    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        new_lines.append(line)

    # adiciona chaves que ainda não existiam
    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    load_dotenv(ENV_PATH)

    client_id = os.getenv("LINKEDIN_CLIENT_ID", "").strip()
    client_secret = os.getenv("LINKEDIN_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv(
        "LINKEDIN_REDIRECT_URI", "http://localhost:8000/callback"
    ).strip()

    if not client_id or not client_secret:
        print(
            "ERRO: preencha LINKEDIN_CLIENT_ID e LINKEDIN_CLIENT_SECRET em .env",
            file=sys.stderr,
        )
        return 1

    # `state` aleatório protege contra CSRF: o LinkedIn devolve o mesmo valor.
    state = secrets.token_urlsafe(16)

    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
        }
    )
    full_auth_url = f"{AUTH_URL}?{query}"

    print("Abrindo navegador para autorização ...")
    print(f"  Se não abrir, cole esta URL manualmente:\n  {full_auth_url}\n")
    webbrowser.open(full_auth_url)

    code = _capture_code(redirect_uri, state)
    print("Code capturado. Trocando por access_token ...")

    token_data = _exchange_code_for_token(code, client_id, client_secret, redirect_uri)
    access_token = token_data["access_token"]
    expires_in = int(token_data.get("expires_in", 5184000))  # default 60d
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    print("Token obtido. Buscando URN do perfil ...")
    urn = _fetch_urn(access_token)

    update_env(
        {
            "LINKEDIN_ACCESS_TOKEN": access_token,
            "LINKEDIN_URN": urn,
            "LINKEDIN_TOKEN_EXPIRES_AT": expires_at.isoformat(),
        }
    )

    print("\n✓ Autenticação concluída.")
    print(f"  URN salvo: {urn}")
    print(f"  Token expira em: {expires_at.isoformat()}")
    print(f"  (~{expires_in // 86400} dias)")
    return 0


# alias retrocompatível
_update_env = update_env


if __name__ == "__main__":
    raise SystemExit(main())
