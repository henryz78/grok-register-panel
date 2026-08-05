"""CatchThis Temp Email 临时邮箱提供商。

API 参考：https://catchthis.email/api
BASE URL：https://catchthis.email/api/v1
认证：Authorization: Bearer <api_key>
收件框地址即唯一标识（无独立 id），消息按 地址/消息id 读取。

本地直连：每个请求显式传 proxies={}，不经过任何代理配置。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, List, Optional, Tuple
from urllib.parse import quote, urlparse

from email_providers.common import extract_verification_code, generate_username

HttpGet = Callable[..., Any]
HttpPost = Callable[..., Any]
HttpDelete = Callable[..., Any]

# 官方 BASE URL /api/v1；配置可写到站点根（会自动拼 /api/v1）
API_BASE_DEFAULT = "https://catchthis.email/api/v1"

_accounts_lock = threading.Lock()


def reset_runtime_state() -> None:
    pass


def normalize_base(base_url: str = "") -> str:
    """站点根 URL 的归一化：任意写法都补成 https://host/api/v1。"""
    raw = str(base_url or "").strip()
    if not raw:
        raw = API_BASE_DEFAULT
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    path = (parsed.path or "").rstrip("/")
    # 剥掉用户误填的具体接口路径
    for endpoint in ("/inboxes/create", "/inboxes/list"):
        if path.endswith(endpoint):
            path = path[: -len(endpoint)].rstrip("/")
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


def _api(base: str, path: str) -> str:
    base = normalize_base(base)
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


def _parse_json(resp, action: str) -> Any:
    try:
        return resp.json()
    except Exception as exc:
        preview = str(getattr(resp, "text", "") or "")[:300]
        raise Exception(f"CatchThis {action} 返回非 JSON: {preview}") from exc


def _raise_http(resp, action: str) -> None:
    status = int(getattr(resp, "status_code", 0) or 0)
    if status < 400:
        return
    try:
        data = resp.json()
        if isinstance(data, dict):
            detail = str(data.get("message") or data.get("error") or data)[:300]
        else:
            detail = str(data)[:300]
    except Exception:
        detail = str(getattr(resp, "text", "") or "")[:300]
    raise Exception(f"CatchThis {action} 失败 HTTP {status}: {detail or 'unknown'}")


def _payload_list(data: Any) -> List[dict]:
    """从 {address, data:[...]} 或纯数组里取邮件列表。"""
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if isinstance(data, dict):
        for key in ("data", "emails", "messages", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [m for m in value if isinstance(m, dict)]
    return []


def create_mailbox(
    http_get: HttpGet,
    http_post: HttpPost,
    base_url: str,
    api_key: str,
    *,
    domains: Optional[List[str]] = None,
    domain: str = "",
    name: str = "",
    expiry_time: Any = None,
) -> Tuple[str, str]:
    """POST /api/v1/inboxes/create → (address, inbox_id)。

    CatchThis 用邮箱地址本身作为收件框标识，故 (address, address)。
    """
    del http_get, domains, expiry_time
    base = normalize_base(base_url)
    key = str(api_key or "").strip()
    if not base:
        raise Exception("CatchThis API 地址未配置（catchthis_api_base）")
    if not key:
        raise Exception("CatchThis API Key 未配置（catchthis_api_key）")

    payload: dict = {}
    username = (name or "").strip()
    if not username:
        username = generate_username(10)
    payload["username"] = username
    managed = str(domain or "").strip().lstrip("@")
    if managed:
        payload["domain"] = managed

    resp = http_post(
        _api(base, "/inboxes/create"),
        json=payload,
        headers=_headers(key, content_type=True),
        timeout=30,
        proxies={},
    )
    _raise_http(resp, "创建邮箱")
    data = _parse_json(resp, "创建邮箱")
    if not isinstance(data, dict):
        raise Exception(f"CatchThis 创建邮箱响应异常: {data!r}")

    address = str(
        data.get("address")
        or data.get("email")
        or data.get("emailAddress")
        or ""
    ).strip()
    # 若只返回 username 可拼接补全
    if not address and username:
        domain_part = str(data.get("domain") or managed or "ilovemyemail.net")
        address = f"{username}@{domain_part}"
    if not address:
        raise Exception(f"CatchThis 返回邮箱地址为空: {data}")

    print(f"[CatchThis] 创建邮箱成功: {address}")
    return address, address


def get_messages(
    http_get: HttpGet,
    base_url: str,
    email_id: str,
    api_key: str,
    cursor: Optional[str] = None,
) -> List[dict]:
    """GET /alert{address}/emails?format=metadata&order=desc → 邮件元数据列表。"""
    if not base_url or not api_key or not email_id:
        return []
    params = {"format": "metadata", "order": "desc"}
    if cursor:
        params["cursor"] = cursor
    resp = http_get(
        _api(base_url, f"/inboxes/{quote(email_id, safe='@')}/emails"),
        params=params,
        headers=_headers(api_key),
        timeout=30,
        proxies={},
    )
    _raise_http(resp, "获取邮件列表")
    return _payload_list(_parse_json(resp, "获取邮件列表"))


def get_message_detail(
    http_get: HttpGet,
    base_url: str,
    email_id: str,
    message_id: str,
    api_key: str,
) -> dict:
    """GET /alert/{address}/emails/{id} → 单封邮件（含 text/html/headers）。"""
    if not base_url or not api_key or not email_id or not message_id:
        return {}
    resp = http_get(
        _api(
            base_url,
            f"/inboxes/{quote(email_id, safe='@')}/emails/{quote(str(message_id), safe='@')}",
        ),
        headers=_headers(api_key),
        timeout=20,
        proxies={},
    )
    _raise_http(resp, "获取邮件详情")
    data = _parse_json(resp, "获取邮件详情")
    if isinstance(data, dict):
        msg = data.get("message")
        if isinstance(msg, dict):
            return msg
        return data
    return {}


def delete_mailbox(
    http_delete: HttpDelete,
    base_url: str,
    email_id: str,
    api_key: str,
) -> None:
    """CatchThis 无删除收件框接口（只可删邮件），留空。"""
    del http_delete, base_url, email_id, api_key
    return


def cleanup_address(
    http_delete: Optional[HttpDelete],
    base_url: str,
    api_key: str,
    email: str,
    email_id: str = "",
) -> None:
    del http_delete, base_url, api_key, email, email_id
    return


def _message_text(detail: dict, subject: str = "") -> Tuple[str, str]:
    import re as _re

    parts: List[str] = []
    subj = str(subject or detail.get("subject") or "")
    for field in ("text", "textContent", "text_content", "body", "snippet", "intro"):
        value = detail.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    html_value = detail.get("html") or detail.get("htmlContent") or detail.get("html_content")
    if isinstance(html_value, str) and html_value.strip():
        parts.append(_re.sub(r"<[^>]+>", " ", html_value))
    elif isinstance(html_value, list):
        parts.extend(
            _re.sub(r"<[^>]+>", " ", item)
            for item in html_value
            if isinstance(item, str)
        )
    return subj, "\n".join(parts)


def wait_for_code(
    http_get: HttpGet,
    base_url: str,
    api_key: str,
    email_id: str,
    email: str = "",
    *,
    timeout: int = 180,
    poll_interval: int = 5,
    http_delete: Optional[HttpDelete] = None,
    cleanup: bool = True,
    raise_if_cancelled: Callable[[Optional[Callable[[], bool]]], None],
    sleep_with_cancel: Callable[[float, Optional[Callable[[], bool]]], None],
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
    resend_callback: Optional[Callable[[], None]] = None,
) -> str:
    """轮询收件框 + 详情，提取 xAI 验证码。"""
    del http_delete
    base = normalize_base(base_url)
    key = str(api_key or "").strip()
    eid = str(email_id or "").strip()
    if not base:
        raise Exception("CatchThis API 地址未配置")
    if not key:
        raise Exception("CatchThis API Key 未配置")
    if not eid:
        raise Exception("CatchThis email_id 为空，无法收信")

    deadline = time.time() + timeout
    seen_attempts: dict[str, int] = {}
    next_resend_at = time.time() + 35

    try:
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
                messages = get_messages(http_get, base, eid, key)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] CatchThis 拉取邮件失败: {exc}")
                sleep_with_cancel(poll_interval, cancel_callback)
                continue

            if log_callback:
                log_callback(f"[Debug] CatchThis 本轮邮件数量: {len(messages)}")

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg_id = str(
                    msg.get("id") or msg.get("messageId") or msg.get("message_id") or ""
                ).strip()
                if not msg_id:
                    continue
                attempt = int(seen_attempts.get(msg_id, 0))
                if attempt >= 5:
                    continue
                seen_attempts[msg_id] = attempt + 1

                list_subject = str(msg.get("subject") or "")
                code = extract_verification_code(list_subject, list_subject)
                if code:
                    if log_callback:
                        log_callback(f"[*] CatchThis 从主题提取到验证码: {code}")
                    return code

                body = _message_text(msg, list_subject)[1]
                if body and body != list_subject:
                    code = extract_verification_code(body, list_subject)
                    if code:
                        if log_callback:
                            log_callback(f"[CatchThis] 从邮件列表正文提取到验证码: {code}")
                        return code

                try:
                    detail = get_message_detail(http_get, base, eid, msg_id, key)
                except Exception as exc:
                    if log_callback:
                        log_callback(f"[Debug] CatchThis 获取邮件详情失败: {exc}")
                    continue

                subject, combined = _message_text(detail, list_subject)
                if log_callback:
                    log_callback(f"[Debug] CatchThis 收到邮件: {subject or list_subject}")
                code = extract_verification_code(combined, subject or list_subject)
                if code:
                    if log_callback:
                        log_callback(f"[CatchThis] 从邮件中提取到验证码: {code}")
                    return code
                if log_callback:
                    log_callback(
                        "[Debug] 邮件已解析但未提取到验证码 "
                        f"id={msg_id} attempt={seen_attempts[msg_id]}"
                    )

            sleep_with_cancel(poll_interval, cancel_callback)
        raise Exception(f"CatchThis 在 {timeout}s 内未收到验证码邮件")
    finally:
        if cleanup:
            cleanup_address(None, base, key, email, email_id=eid)