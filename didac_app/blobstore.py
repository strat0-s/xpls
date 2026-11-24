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


def signed_upload_url(config: DidacConfig, key: str) -> str:
    """A one-time URL the browser can PUT a file to, so a big file goes straight to the bucket and not through the app."""
    response = requests.post(f"{_base(config)}/object/upload/sign/{config.supabase_bucket}/{quote(key)}", headers=_headers(config), json={}, timeout=15)
    response.raise_for_status()
    return f"{_base(config)}{response.json()['url']}"


def signed_url(config: DidacConfig, key: str, expires_in: int = 3600) -> str:
    response = requests.post(
        f"{_base(config)}/object/sign/{config.supabase_bucket}/{quote(key)}",
        headers=_headers(config),
        json={"expiresIn": expires_in},
        timeout=15,
    )
    response.raise_for_status()
    return f"{_base(config)}{response.json()['signedURL']}"


def signed_urls(config: DidacConfig, keys: list[str], expires_in: int) -> dict[str, str]:
    """Links that open without a login, for many files in one call: {key: url}. A file the bucket does not have is left out."""
    links = {}
    for start in range(0, len(keys), 200):
        response = requests.post(f"{_base(config)}/object/sign/{config.supabase_bucket}", headers=_headers(config), json={"expiresIn": expires_in, "paths": keys[start:start + 200]}, timeout=30)
        response.raise_for_status()
        links.update({row["path"]: f"{_base(config)}{row['signedURL']}" for row in response.json() if row.get("signedURL")})
    return links


def download(config: DidacConfig, key: str) -> bytes:
    response = requests.get(f"{_base(config)}/object/{config.supabase_bucket}/{quote(key)}", headers=_headers(config), timeout=30)
    response.raise_for_status()
    return response.content
