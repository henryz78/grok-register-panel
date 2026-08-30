"""CatchThis Temp Email 临时邮箱提供商。

API 文档参考：https://catchthis.email/api
基础接口：https://catchthis.email/api/v1
认证方式：Authorization: Bearer <api_key>
核心逻辑：
  - POST/GET {base}/inboxes/create  创建收件箱（返回 address）
  - GET {base}/inboxes/{address}/messages 获取邮件列表
  - GET {base}/messages/{msg_id} 获取单封邮件内容

直连约定：请求时显式传递 proxies={}，确保不走注册代理。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, List, Optional, Tuple
from urllib.parse import quote, urlparse

from email_providers.common import extract_verification_code, generate_username

HttpGet = Callable[..., Any]
HttpPost = Callable[..., Any]
HttpDelete = Callable[..., Any]

API_BASE_DEFAULT = "https://catchthis.email/api/v1"
API_TIMEOUT = 25


def reset_runtime_state() -> None:
    """重置运行时状态。"""
    pass


def normalize_base(base_url: str = "") -> str:
    """规范化 CatchThis API 地址至 https://host/api/v1。"""
    raw = str(base_url or "").strip()
    if not raw:
        raw = API_BASE_DEFAULT
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    path = (parsed.path or "").rstrip("/")

    for ep in ("/inboxes/create", "/inboxes/list", "/inboxes"):
        if path.endswith(ep):
            path = path[: -len(ep)].rstrip("/")
            break

    while path.endswith("/api/v1"):
        path = path[: -len("/api/v1")].rstrip("/")
    if path.endswith("/api"):
        return f"{origin}{path}/v1"
    if path.endswith("/v1"):
        return f"{origin}{path}"
    if path:
        return f"{origin}{path}/api/v1"
    return f"{origin}/api/v1"


def _api(base_url: str, path: str) -> str:
    base = normalize_base(base_url)
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"


def _headers(api_key: str, content_type: bool = False) -> dict:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {str(api_key or '').strip()}",
    }
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def _raise_http(resp, action: str) -> None:
    status = int(getattr(resp, "status_code", 0) or 0)
    if status < 400:
        return
    detail = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            detail = str(data.get("message") or data.get("error") or data)[:300]
        else:
            detail = str(data)[:300]
    except Exception:
        detail = str(getattr(resp, "text", "") or "")[:300]
    raise Exception(f"CatchThis {action} 失败 HTTP {status}: {detail or 'unknown'}")


def _parse_json(resp, action: str) -> Any:
    try:
        return resp.json()
    except Exception as exc:
        preview = str(getattr(resp, "text", "") or "")[:300]
        raise Exception(f"CatchThis {action} 返回非 JSON: {preview}") from exc


def _unwrap_payload(data: Any, action: str = "请求") -> Any:
    """解包数据并检查业务状态。"""
    if not isinstance(data, dict):
        return data

    if data.get("success") is False:
        msg = data.get("message") or data.get("error") or str(data)
        raise Exception(f"CatchThis {action} 业务失败: {msg}")

    code = data.get("code")
    if code is not None:
        try:
            code_num = int(code)
            if code_num not in (0, 200):
                msg = data.get("message") or data.get("error") or str(data)
                raise Exception(f"CatchThis {action} 业务失败 code={code_num}: {msg}")
        except ValueError:
            pass

    if any(k in data for k in ("address", "email", "messages", "inbox")):
        return data

    for k in ("data", "result", "payload"):
        payload = data.get(k)
        if isinstance(payload, (dict, list)) and payload:
            return payload

    return data


def create_mailbox(
    http_get: HttpGet,
    http_post: HttpPost,
    base_url: str = "",
    api_key: str = "",
    *,
    domain: str = "",
    username: str = "",
    **kwargs: Any,
) -> Tuple[str, str]:
    """创建或分配 CatchThis 邮箱地址 → (address, address)。"""
    del domain, username, kwargs
    base = normalize_base(base_url)
    key = str(api_key or "").strip()
    if not base:
        raise Exception("CatchThis API 地址未配置（catchthis_api_base）")
    if not key:
        raise Exception("CatchThis API Key 未配置（catchthis_api_key）")

    # 尝试 POST /inboxes/create，如果不支持则尝试 GET
    last_err = None
    data = None
    try:
        resp = http_post(
            _api(base, "/inboxes/create"),
            headers=_headers(key, content_type=True),
            json={},
            timeout=API_TIMEOUT,
            proxies={},
        )
        _raise_http(resp, "创建邮箱")
        data = _unwrap_payload(_parse_json(resp, "创建邮箱"), "创建邮箱")
    except Exception as exc:
        last_err = exc

    if not data or not isinstance(data, dict):
        try:
            resp = http_get(
                _api(base, "/inboxes/create"),
                headers=_headers(key),
                timeout=API_TIMEOUT,
                proxies={},
            )
            _raise_http(resp, "创建邮箱(GET)")
            data = _unwrap_payload(_parse_json(resp, "创建邮箱"), "创建邮箱")
        except Exception as exc2:
            if last_err:
                raise last_err
            raise exc2

    address = ""
    for k in ("address", "email", "inbox", "mail"):
        val = str(data.get(k) or "").strip()
        if val and "@" in val:
            address = val
            break

    if not address and isinstance(data.get("data"), dict):
        for k in ("address", "email", "inbox"):
            val = str(data["data"].get(k) or "").strip()
            if val and "@" in val:
                address = val
                break

    if not address:
        raise Exception(f"CatchThis 创建邮箱响应中未找到有效地址: {data!r}")

    address = address.lower().strip()
    return address, address


def list_messages(
    http_get: HttpGet,
    base_url: str,
    address: str,
    api_key: str,
) -> List[dict]:
    """获取指定收件箱的邮件列表（优先 /inboxes/{addr}/emails，兼容 /inboxes/{addr}/messages）。"""
    base = normalize_base(base_url)
    key = str(api_key or "").strip()
    addr = str(address or "").strip().lower()
    if not base or not key or not addr:
        return []

    encoded_addr = quote(addr, safe="")
    last_exc = None
    # 优先使用官方标准 /inboxes/{addr}/emails 接口
    for ep in (f"/inboxes/{encoded_addr}/emails", f"/inboxes/{encoded_addr}/messages"):
        try:
            resp = http_get(
                _api(base, ep),
                params={"format": "metadata", "order": "desc"} if ep.endswith("/emails") else {},
                headers=_headers(key),
                timeout=API_TIMEOUT,
                proxies={},
            )
            _raise_http(resp, "获取邮件列表")
            data = _unwrap_payload(_parse_json(resp, "获取邮件列表"), "获取邮件列表")
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
            if isinstance(data, dict):
                for k in ("messages", "list", "emails", "data", "results"):
                    val = data.get(k)
                    if isinstance(val, list):
                        return [item for item in val if isinstance(item, dict)]
            return []
        except Exception as exc:
            last_exc = exc
            continue
    if last_exc:
        raise last_exc
    return []


def get_message_detail(
    http_get: HttpGet,
    base_url: str,
    message_id: str,
    api_key: str,
    address: str = "",
) -> dict:
    """获取邮件详情（优先 /inboxes/{addr}/emails/{id}，兼容 /messages/{id}）。"""
    base = normalize_base(base_url)
    key = str(api_key or "").strip()
    mid = str(message_id or "").strip()
    addr = str(address or "").strip().lower()
    if not base or not key or not mid:
        return {}

    encoded_id = quote(mid, safe="")
    encoded_addr = quote(addr, safe="") if addr else ""
    endpoints = []
    if encoded_addr:
        endpoints.append(f"/inboxes/{encoded_addr}/emails/{encoded_id}")
    endpoints.append(f"/messages/{encoded_id}")

    last_exc = None
    for ep in endpoints:
        try:
            resp = http_get(
                _api(base, ep),
                headers=_headers(key),
                timeout=API_TIMEOUT,
                proxies={},
            )
            _raise_http(resp, "获取邮件详情")
            data = _unwrap_payload(_parse_json(resp, "获取邮件详情"), "获取邮件详情")
            if isinstance(data, list):
                data = data[0] if data else {}
            if isinstance(data, dict):
                msg = data.get("message")
                if isinstance(msg, dict):
                    return msg
                return data
        except Exception as exc:
            last_exc = exc
            continue
    if last_exc:
        raise last_exc
    return {}


def _message_text(detail: dict, subject: str = "") -> Tuple[str, str]:
    parts: List[str] = []
    subj = str(subject or detail.get("subject") or "")
    for field in ("text", "textContent", "text_content", "body", "content", "raw", "message", "snippet", "intro"):
        value = detail.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(re.sub(r"<[^>]+>", " ", value))
        elif isinstance(value, dict):
            for subfield in ("text", "html", "content"):
                subval = value.get(subfield)
                if isinstance(subval, str) and subval.strip():
                    parts.append(re.sub(r"<[^>]+>", " ", subval))
    html_val = detail.get("html") or detail.get("htmlContent") or detail.get("html_content")
    if isinstance(html_val, str) and html_val.strip():
        parts.append(re.sub(r"<[^>]+>", " ", html_val))
    return subj, "\n".join(parts)


def wait_for_code(
    http_get: HttpGet,
    base_url: str,
    api_key: str,
    address: str,
    *,
    timeout: int = 180,
    poll_interval: int = 4,
    http_delete: Optional[HttpDelete] = None,
    cleanup: bool = True,
    raise_if_cancelled: Callable[[Optional[Callable[[], bool]]], None],
    sleep_with_cancel: Callable[[float, Optional[Callable[[], bool]]], None],
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
    resend_callback: Optional[Callable[[], None]] = None,
) -> str:
    """轮询 CatchThis 收件箱提取验证码。"""
    del cleanup, http_delete
    base = normalize_base(base_url)
    key = str(api_key or "").strip()
    addr = str(address or "").strip().lower()
    if not base:
        raise Exception("CatchThis API 地址未配置（catchthis_api_base）")
    if not key:
        raise Exception("CatchThis API Key 未配置（catchthis_api_key）")
    if not addr:
        raise Exception("CatchThis 邮箱地址为空")

    deadline = time.time() + timeout
    seen_attempts: dict[str, int] = {}
    next_resend_at = time.time() + 35

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 35

        try:
            messages = list_messages(http_get, base, addr, key)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] CatchThis 获取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue

        if log_callback:
            log_callback(f"[Debug] CatchThis 收到邮件数: {len(messages)}")

        for msg in messages:
            msg_id = ""
            for k in ("id", "message_id", "messageId", "uid"):
                val = str(msg.get(k) or "").strip()
                if val:
                    msg_id = val
                    break
            if not msg_id:
                msg_id = str(msg.get("subject") or "")
            if not msg_id:
                continue

            attempt = seen_attempts.get(msg_id, 0)
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1

            list_subject = str(msg.get("subject") or "")
            code = extract_verification_code(list_subject, list_subject)
            if code:
                if log_callback:
                    log_callback(f"[*] CatchThis 从主题提取到验证码: {code}")
                return code

            detail = msg
            if "body" not in msg and "text" not in msg and "html" not in msg and msg_id:
                try:
                    detail = get_message_detail(http_get, base, msg_id, key, address=addr)
                except Exception as exc:
                    if log_callback:
                        log_callback(f"[Debug] CatchThis 获取邮件详情失败: {exc}")
                    continue

            subject, combined = _message_text(detail, list_subject)
            code = extract_verification_code(combined, subject or list_subject)
            if code:
                if log_callback:
                    log_callback(f"[*] CatchThis 从正文提取到验证码: {code}")
                return code

        sleep_with_cancel(poll_interval, cancel_callback)

    raise Exception(f"CatchThis 在 {timeout}s 内未收到验证码邮件（{addr}）")
