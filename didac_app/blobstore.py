from __future__ import annotations

from urllib.parse import quote

import requests

from didac_app.config import DidacConfig


def _base(config: DidacConfig) -> str:
    if not (config.supabase_url and config.supabase_bucket and config.supabase_secret_key):
        raise RuntimeError("SUPABASE_URL, SUPABASE_BUCKET and SUPABASE_SECRET_KEY must be set")
    return f"{config.supabase_url.rstrip('/')}/storage/v1"


def _headers(config: DidacConfig) -> dict[str, str]:
    return {"apikey": config.supabase_secret_key, "Authorization": f"Bearer {config.supabase_secret_key}"}


def upload(config: DidacConfig, key: str, data: bytes, content_type: str = "") -> None:
    response = requests.post(
        f"{_base(config)}/object/{config.supabase_bucket}/{quote(key)}",
        headers={**_headers(config), "Content-Type": content_type or "application/octet-stream", "x-upsert": "true"},
        data=data,
        timeout=30,
    )
    response.raise_for_status()


def signed_url(config: DidacConfig, key: str, expires_in: int = 3600) -> str:
    response = requests.post(
        f"{_base(config)}/object/sign/{config.supabase_bucket}/{quote(key)}",
        headers=_headers(config),
        json={"expiresIn": expires_in},
        timeout=15,
    )
    response.raise_for_status()
    return f"{_base(config)}{response.json()['signedURL']}"
