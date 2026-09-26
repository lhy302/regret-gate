"""模型列表探测（GUI 启动器用）。

填好「API 地址 + 密钥」后，从 provider 的 `/models` 端点拉取可选模型名，
供用户在下拉框里选，避免手打模型名打错。

- OpenAI 兼容端点：`GET {base_url}/models`
- Anthropic：`GET {base_url}/models`（`x-api-key` + `anthropic-version`）
- **只做 GET，不发送任何对话内容**；密钥不进日志。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class ModelListError(RuntimeError):
    pass


def _request(url: str, headers: dict, timeout: float) -> tuple:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise ModelListError(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise ModelListError(f"网络不可达：{exc.reason}") from exc
    except OSError as exc:
        raise ModelListError(f"请求失败：{exc}") from exc


def _normalize_base_url(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise ModelListError("API 地址为空")
    if not url.startswith(("http://", "https://")):
        raise ModelListError("API 地址必须以 http:// 或 https:// 开头")
    return url


def list_models(
    base_url: str,
    api_key: str,
    provider: str = "openai",
    timeout: float = 20.0,
) -> list:
    """返回模型 id 列表（已排序、去重）。失败抛 `ModelListError`（消息可直接展示）。"""
    url = _normalize_base_url(base_url)
    if not api_key:
        raise ModelListError("API 密钥为空")

    headers = {"Accept": "application/json", "User-Agent": "regret-gate-launcher"}
    if (provider or "openai").lower() == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        endpoint = f"{url}/models"
    else:
        headers["Authorization"] = f"Bearer {api_key}"
        endpoint = f"{url}/models" if url.endswith("/v1") else f"{url}/v1/models"

    status, payload = _request(endpoint, headers, timeout)
    if status != 200:
        raise ModelListError(f"HTTP {status}")
    try:
        data = json.loads(payload.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise ModelListError(f"响应不是合法 JSON：{exc}") from exc

    ids: list = []
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
        for item in items:
            if isinstance(item, dict):
                model_id = item.get("id") or item.get("name") or item.get("model")
                if model_id:
                    ids.append(str(model_id))
            elif isinstance(item, str):
                ids.append(item)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                model_id = item.get("id") or item.get("name")
                if model_id:
                    ids.append(str(model_id))
            elif isinstance(item, str):
                ids.append(item)

    # 去重 + 排序（稳定、可复现）
    unique = sorted({i for i in ids if i})
    if not unique:
        raise ModelListError("端点返回成功，但没解析出任何模型 id（该服务可能不支持 /models）")
    return unique


def provider_defaults(provider: str) -> dict:
    """各 provider 的默认地址，供 GUI 预填。"""
    provider = (provider or "openai").lower()
    if provider == "anthropic":
        return {"base_url": "https://api.anthropic.com/v1", "hint": "Claude 模型"}
    if provider == "fake":
        return {"base_url": "", "hint": "离线桩，不需要地址与密钥"}
    return {"base_url": "https://api.openai.com/v1", "hint": "OpenAI 兼容端点"}
