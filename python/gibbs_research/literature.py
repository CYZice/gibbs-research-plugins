"""Client for the existing Literature KB v1 API; no database duplication."""

from __future__ import annotations

from urllib.parse import quote, urlsplit

import httpx


class LiteratureClient:
    def __init__(self, base_url, token="", transport=None):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Literature URL must be an HTTP(S) service URL without embedded credentials")
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/", headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=90, transport=transport, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def request(self, method, path, **kwargs):
        response = await self.client.request(method, "api/v1/" + path, **kwargs)
        if response.is_error:
            # Never include upstream bodies/URLs: they can contain provider credentials.
            raise ValueError(f"Literature service returned HTTP {response.status_code}")
        return response.json()

    async def evidence(self, evidence_id):
        key = quote(evidence_id, safe="")
        content = await self.request("GET", f"evidence/{key}/content")
        source = await self.request("GET", f"evidence/{key}/source")
        return {"content": content, "source": source}

    async def upload(self, filename, content):
        return await self.request("POST", "papers", files={"file": (filename, content, "application/pdf")})
