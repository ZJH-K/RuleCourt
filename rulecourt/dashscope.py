"""Alibaba Cloud Model Studio endpoint helpers for experiment providers."""

from __future__ import annotations

from urllib.parse import urlsplit


def dashscope_origin(api_host: str) -> str:
    """Accept the console's API Host and return its HTTPS origin."""
    value = api_host.strip()
    if not value:
        raise ValueError("DASHSCOPE_API_HOST is required for Qwen embedding and rerank")
    parsed = urlsplit(value if "://" in value else "https://" + value)
    hostname = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not hostname.endswith(".maas.aliyuncs.com")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("DASHSCOPE_API_HOST must be an Alibaba Cloud HTTPS API Host")
    return f"https://{hostname}"
