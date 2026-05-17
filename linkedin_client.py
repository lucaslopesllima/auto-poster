"""
linkedin_client.py
==================

Wrapper fino sobre a API REST de Posts do LinkedIn (versão `202401`).

Cobre os casos de uso do projeto:
  - publicar post de texto no perfil pessoal
  - publicar post com uma imagem (upload em 3 passos)
  - verificar se o token está expirado antes de chamar a API

Endpoints usados:
  - POST   /rest/posts                                       (texto/imagem)
  - POST   /rest/images?action=initializeUpload              (passo 1 da imagem)
  - PUT    <uploadUrl devolvido pelo passo 1>                (passo 2 da imagem)

Docs: https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api
"""

from __future__ import annotations

import io
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Constantes da API
# ---------------------------------------------------------------------------

API_BASE = "https://api.linkedin.com"
# LinkedIn mantém ~12 meses de versões ativas (formato YYYYMM).
# Pode ser sobrescrito via env LINKEDIN_API_VERSION sem editar o código.
API_VERSION = os.getenv("LINKEDIN_API_VERSION", "202604")


class LinkedInError(Exception):
    """Erro retornado pela API do LinkedIn."""


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------

class LinkedInClient:
    """
    Uso:
        client = LinkedInClient.from_env()
        client.post_text("Olá mundo")
        client.post_with_image("Veja a foto", "./foto.jpg")
    """

    def __init__(self, access_token: str, urn: str, expires_at: Optional[str] = None):
        if not access_token or not urn:
            raise ValueError("access_token e urn são obrigatórios")
        self.access_token = access_token
        self.urn = urn
        self.expires_at = expires_at  # ISO 8601, opcional

    # ---- factory --------------------------------------------------------

    @classmethod
    def from_env(cls, env_path: Optional[Path] = None) -> "LinkedInClient":
        """Constrói o cliente a partir do `.env`."""
        if env_path is None:
            env_path = Path(__file__).resolve().parent / ".env"
        load_dotenv(env_path)
        return cls(
            access_token=os.getenv("LINKEDIN_ACCESS_TOKEN", "").strip(),
            urn=os.getenv("LINKEDIN_URN", "").strip(),
            expires_at=os.getenv("LINKEDIN_TOKEN_EXPIRES_AT", "").strip() or None,
        )

    # ---- utilidades -----------------------------------------------------

    def _headers(self, extra: Optional[dict] = None) -> dict:
        """Headers padrão exigidos pela API REST do LinkedIn."""
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "LinkedIn-Version": API_VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
        }
        if extra:
            headers.update(extra)
        return headers

    def is_expired(self) -> bool:
        """Verifica se a expiração registrada já passou."""
        if not self.expires_at:
            return False
        try:
            exp = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return False
        return datetime.now(timezone.utc) >= exp

    def assert_ready(self) -> None:
        """Levanta exceção clara se o token estiver expirado."""
        if self.is_expired():
            raise LinkedInError(
                "access_token expirado. Rode `python auth.py` para renovar."
            )

    # ---- post de texto --------------------------------------------------

    def post_text(self, commentary: str) -> str:
        """
        Publica um post somente de texto.

        Retorna o `URN do post` (header x-restli-id da resposta).
        """
        self.assert_ready()
        body = self._build_post_body(commentary)
        resp = requests.post(
            f"{API_BASE}/rest/posts",
            headers=self._headers(),
            json=body,
            timeout=30,
        )
        self._raise_for_status(resp)
        return resp.headers.get("x-restli-id", "")

    # ---- post com imagem -----------------------------------------------

    def post_with_image(
        self, commentary: str, image_path: str, alt_text: str = ""
    ) -> str:
        """
        Publica um post com uma imagem anexa.

        Fluxo (3 passos da API):
          1. initializeUpload  → recebe `uploadUrl` e `image URN`
          2. PUT bytes da imagem em `uploadUrl`
          3. POST /rest/posts com `content.media.id = image URN`
        """
        self.assert_ready()

        image_urn, upload_url = self._initialize_image_upload()
        self._upload_image_bytes(upload_url, image_path)

        body = self._build_post_body(commentary)
        body["content"] = {
            "media": {
                "id": image_urn,
                "title": alt_text or Path(image_path).name,
            }
        }

        resp = requests.post(
            f"{API_BASE}/rest/posts",
            headers=self._headers(),
            json=body,
            timeout=30,
        )
        self._raise_for_status(resp)
        return resp.headers.get("x-restli-id", "")

    # ---- helpers internos ----------------------------------------------

    def _build_post_body(self, commentary: str) -> dict:
        """Esqueleto comum do payload de posts."""
        return {
            "author": self.urn,
            "commentary": commentary,
            "visibility": "PUBLIC",
            "lifecycleState": "PUBLISHED",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
        }

    def _initialize_image_upload(self) -> tuple[str, str]:
        """Passo 1: reserva um URN de imagem e devolve URL para upload."""
        resp = requests.post(
            f"{API_BASE}/rest/images?action=initializeUpload",
            headers=self._headers(),
            json={"initializeUploadRequest": {"owner": self.urn}},
            timeout=15,
        )
        self._raise_for_status(resp)
        value = resp.json()["value"]
        return value["image"], value["uploadUrl"]

    def _upload_image_bytes(self, upload_url: str, image_path: str) -> None:
        """Passo 2: envia os bytes da imagem via PUT.

        LinkedIn aceita JPEG/PNG/GIF mas não WebP. Converte WebP → PNG
        em memória antes de enviar.
        """
        path = Path(image_path)
        if not path.is_file():
            raise FileNotFoundError(f"Imagem não encontrada: {image_path}")
        data = _read_image_bytes_for_linkedin(path)
        resp = requests.put(
            upload_url,
            data=data,
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=60,
        )
        if resp.status_code >= 300:
            raise LinkedInError(
                f"Falha no upload da imagem: {resp.status_code} {resp.text}"
            )

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        """Converte erros HTTP em LinkedInError com mensagem útil."""
        if resp.status_code >= 300:
            raise LinkedInError(
                f"{resp.request.method} {resp.url} → {resp.status_code}: {resp.text}"
            )


_LINKEDIN_NATIVE_EXTS = {".jpg", ".jpeg", ".png", ".gif"}


def _read_image_bytes_for_linkedin(path: Path) -> bytes:
    """Lê os bytes da imagem, convertendo WebP para PNG quando necessário."""
    ext = path.suffix.lower()
    if ext in _LINKEDIN_NATIVE_EXTS:
        return path.read_bytes()
    try:
        from PIL import Image
    except ImportError as exc:
        raise LinkedInError(
            f"Imagem {path.name} requer conversão ({ext}); instale Pillow "
            "(`pip install Pillow`)."
        ) from exc
    with Image.open(path) as im:
        buf = io.BytesIO()
        if im.mode in ("RGBA", "LA"):
            im.save(buf, format="PNG")
        else:
            im.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()
