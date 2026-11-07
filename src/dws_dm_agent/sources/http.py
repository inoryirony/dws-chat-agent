"""标准库 HTTP 小件（channels/sources 共用）。项目零三方依赖，
urllib + to_thread；不信任系统代理（外部端点直连）。"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from typing import Any


def request_json(
    url: str,
    *,
    params: dict[str, str] | None = None,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = None
    effective_headers = {"Accept": "application/json"}
    if headers:
        effective_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        effective_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=effective_headers, method=method
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def basic_auth_header(username: str, token: str) -> dict[str, str]:
    pair = base64.b64encode(f"{username}:{token}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {pair}"}


__all__ = ["basic_auth_header", "request_json"]
