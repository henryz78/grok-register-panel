"""iDataRiver Temp Mail API (cbea) 临时邮箱提供商。

API 参考：https://www.idatariver.com/project/temp-mail-api-cbea
认证：apikey 通过查询参数传递。
约定：所有接口 HTTP 状态恒为 200，需按顶层 code 字段判断（0=成功）。
接口：
  GET {base}/generate/v1         生成随机邮箱（域名固定 uselesss.org）
  GET {base}/messages/v1         指定邮箱的邮件列表（id=邮箱id）
  GET {base}/message/detail/v1   单封邮件正文（id=消息id）

本地直连：每个请求显式传 proxies={}，不依赖/不经过任何代理配置。
"""

from __future__ import annotations

import base64
import threading
import time
from typing import Any, Callable, List, Optional, Tuple
from urllib.parse import urlparse

from email_providers.common import extract_verification_code, generate_username

HttpGet = Callable[..., Any]
HttpPost = Callable[..., Any]
HttpDelete = Callable[..., Any]

# iDataRiver 建议超时 > 60s，避免因提前结束请求被扣量
API_TIMEOUT = 65

# 结构字段猜测（文档未提供精确字段名），做多字段兼容解析
_ADDRESS_KEYS = ("email", "emailAddress", "address", "mail")
_ID_KEYS = ("id", "emailId", "email_id", "mailId")
_MESSAGES_KEYS = ("messages", "list", "results", "emails", "data")


def reset_runtime_state() -> None:
    pass


def normalize_base(base_url: str = "") -> str:
    """站点根 URL（不含末尾斜杠）。配置可写 https://apiok.us 或完整接口前缀。

    自动把用户填的 https://host / https://host/api 补成 https://host/api/cbea。
    """
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    path = (parsed.path or "").rstrip("/")
    # 若误填了具体接口路径，剥到站点根
    for endpoint in ("/generate/v1", "/message/detail/v1", "/messages/v1"):
        if path.endswith(endpoint):
            path = path[: -len(endpoint)].rstrip("/")
            break
    if path.endswith("/cbea"):
        return f"{origin}{path}"
    if path.endswith("/api"):
        return f"{origin}{path}/cbea"
    if path:
        return f"{origin}{path}/api/cbea"
    return f"{origin}/api/cbea"


def _api(base: str, path: str) -> str:
    base = normalize_base(base)
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"


def _params(api_key: str, **extra: Any) -> dict:
    params = {"apikey": str(api_key or "").strip()}
    params.update({key: value for key, value in extra.items() if value is not None})
    return params


def _headers() -> dict:
    return {"Accept": "application/json"}


def _parse_json(resp, action: str) -> Any:
    try:
        return resp.json()
    except Exception as exc:
        preview = str(getattr(resp, "text", "") or "")[:300]
        raise Exception(f"iDataRiver {action} 返回非 JSON: {preview}") from exc


def _raise_http(resp, action: str) -> None:
    status = int(getattr(resp, "status_code", 0) or 0)
    if status >= 400:
        detail = str(getattr(resp, "text", "") or "")[:300]
        raise Exception(f"iDataRiver {action} 失败 HTTP {status}: {detail or 'unknown'}")


def _unwrap(data: Any, action: str) -> Any:
    """兼容 {code,data}, {code,result}, 裸对象, {success,data}, 顶层业务字段 等包装。"""
    if not isinstance(data, dict):
        return data
    code = data.get("code")
    success = data.get("success")
    if code is not None:
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None
        if code not in (0, 200, None):
            msg = (
                data.get("message")
                or data.get("msg")
                or data.get("error")
                or data.get("errorMessage")
                or data
            )
            raise Exception(f"iDataRiver {action} 业务失败 code={code}: {msg}")
    if success is False:
        msg = (
            data.get("message")
            or data.get("msg")
            or data.get("error")
            or data
        )
        raise Exception(f"iDataRiver {action} 业务失败: {msg}")
    # 顶层已有业务字段则优先顶层
    if any(k in data for k in ("id", "email", "address", "messages", "message", "messageId", "emails")):
        return data
    # 否则剥常见包装层（generate 接口实际用 result，而非 data）
    for key in ("data", "result", "response", "payload"):
        payload = data.get(key)
        if isinstance(payload, dict) and payload:
            return payload
        if isinstance(payload, list) and payload:
            return payload
    return data


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
    """GET /generate/v1 → (address, email_id)。

    iDataRiver 随机分配邮箱（域名暂只支持 uselesss.org），
    返回的 email id 用于后续 messages 接口。
    """
    del domains, domain, name, expiry_time
    base = normalize_base(base_url)
    key = str(api_key or "").strip()
    if not base:
        raise Exception("iDataRiver API 地址未配置（idatariver_api_base）")
    if not key:
        raise Exception("iDataRiver API Key 未配置（idatariver_api_key）")

    resp = http_get(
        _api(base, "/generate/v1"),
        params=_params(key, type="*"),
        headers=_headers(),
        timeout=API_TIMEOUT,
        proxies={},
    )
    _raise_http(resp, "创建邮箱")
    data = _unwrap(_parse_json(resp, "创建邮箱"), "创建邮箱")
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise Exception(f"iDataRiver 创建邮箱响应异常: {data!r}")

    email_id = ""
    for k in _ID_KEYS:
        if str(data.get(k) or "").strip():
            email_id = str(data.get(k) or "").strip()
            break
    address = ""
    for k in _ADDRESS_KEYS:
        if str(data.get(k) or "").strip():
            address = str(data.get(k) or "").strip()
            break

    # id 常为邮箱地址的 base64；email 字段常被打码（如 il***@domain），据此恢复真实地址
    decoded = _decode_b64(email_id) if email_id else ""
    if "@" in decoded:
        if "*" in address or not address:
            address = decoded
    if not email_id and address:
        email_id = _b64_encode(address)

    if not address:
        raise Exception(f"iDataRiver 返回邮箱地址为空: {data}")
    if not email_id:
        raise Exception(f"iDataRiver 返回邮箱 id 为空: {data}")

    print(f"[iDataRiver] 创建邮箱成功: {address} (id={email_id})")
    return address, email_id


def get_messages(
    http_get: HttpGet,
    base_url: str,
    email_id: str,
    api_key: str,
    cursor: Optional[str] = None,
) -> List[dict]:
    """GET /messages/v1?id=<email_id> → 邮件列表。"""
    if not base_url or not api_key or not email_id:
        return []
    resp = http_get(
        _api(base_url, "/messages/v1"),
        params=_params(api_key, id=email_id),
        headers=_headers(),
        timeout=API_TIMEOUT,
        proxies={},
    )
    _raise_http(resp, "获取邮件列表")
    data = _unwrap(_parse_json(resp, "获取邮件列表"), "获取邮件列表")
    return _pick_messages(data)


def _pick_messages(data: Any) -> List[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in _MESSAGES_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                for inner_key in _MESSAGES_KEYS:
                    inner = value.get(inner_key)
                    if isinstance(inner, list):
                        return [item for item in inner if isinstance(item, dict)]
    return []


def get_message_detail(
    http_get: HttpGet,
    base_url: str,
    email_id: str,
    message_id: str,
    api_key: str,
) -> dict:
    """GET /message/detail/v1?id=<message_id> → message dict。"""
    del email_id
    if not base_url or not api_key or not message_id:
        return {}
    resp = http_get(
        _api(base_url, "/message/detail/v1"),
        params=_params(api_key, id=message_id),
        headers=_headers(),
        timeout=API_TIMEOUT,
        proxies={},
    )
    _raise_http(resp, "获取邮件详情")
    data = _unwrap(_parse_json(resp, "获取邮件详情"), "获取邮件详情")
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return {}
    message = data.get("message")
    if isinstance(message, dict):
        return message
    if any(key in data for key in ("content", "html", "text", "subject", "from_address")):
        return data
    return {}


def delete_mailbox(
    http_delete: HttpDelete,
    base_url: str,
    email_id: str,
    api_key: str,
) -> None:
    """iDataRiver 无删除接口，留空。"""
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


def _decode_b64(text: str) -> str:
    try:
        return base64.b64decode(str(text or "") + "=" * (-len(str(text) or "") % 4)).decode("utf-8", "ignore")
    except Exception:
        return ""


def _b64_encode(text: str) -> str:
    try:
        return base64.b64encode(str(text).encode("utf-8")).decode("ascii").rstrip("=")
    except Exception:
        return ""


def _message_text(detail: dict, subject: str = "") -> Tuple[str, str]:
    import re as _re

    parts: List[str] = []
    subj = str(subject or detail.get("subject") or "")
    for field in ("content", "text", "textContent", "text_content", "body", "snippet", "intro", "messageBody"):
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
    poll_interval: int = 6,
    http_delete: Optional[HttpDelete] = None,
    cleanup: bool = True,
    raise_if_cancelled: Callable[[Optional[Callable[[], bool]]], None],
    sleep_with_cancel: Callable[[float, Optional[Callable[[], bool]]], None],
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
    resend_callback: Optional[Callable[[], None]] = None,
) -> str:
    """轮询 iDataRiver 收件箱 + 详情，提取 xAI 验证码。"""
    base = normalize_base(base_url)
    key = str(api_key or "").strip()
    eid = str(email_id or "").strip()
    if not base:
        raise Exception("iDataRiver API 地址未配置")
    if not key:
        raise Exception("iDataRiver API Key 未配置")
    if not eid:
        raise Exception("iDataRiver email_id 为空，无法收信")

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
                    log_callback(f"[Debug] iDataRiver 拉取邮件失败: {exc}")
                sleep_with_cancel(poll_interval, cancel_callback)
                continue

            if log_callback:
                log_callback(f"[Debug] iDataRiver 本轮邮件数量: {len(messages)}")

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg_id = str(msg.get("id") or msg.get("messageId") or msg.get("message_id") or "").strip()
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
                        log_callback(f"[*] iDataRiver 从主题提取到验证码: {code}")
                    return code

                # 部分列表接口会直接带正文，先就地试一次，避免多请求触发限流
                body = _message_text(msg, list_subject)[1]
                if body and body != list_subject:
                    code = extract_verification_code(body, list_subject)
                    if code:
                        if log_callback:
                            log_callback(f"[iDataRiver] 从邮件列表正文提取到验证码: {code}")
                        return code

                try:
                    detail = get_message_detail(http_get, base, eid, msg_id, key)
                except Exception as exc:
                    if log_callback:
                        log_callback(f"[Debug] iDataRiver 获取邮件详情失败: {exc}")
                    continue

                subject, combined = _message_text(detail, list_subject)
                if log_callback:
                    log_callback(f"[Debug] iDataRiver 收到邮件: {subject or list_subject}")
                code = extract_verification_code(combined, subject or list_subject)
                if code:
                    if log_callback:
                        log_callback(f"[iDataRiver] 从邮件中提取到验证码: {code}")
                    return code
                if log_callback:
                    log_callback(
                        "[Debug] 邮件已解析但未提取到验证码 "
                        f"id={msg_id} attempt={seen_attempts[msg_id]}"
                    )

            sleep_with_cancel(poll_interval, cancel_callback)
        raise Exception(f"iDataRiver 在 {timeout}s 内未收到验证码邮件")
    finally:
        if cleanup:
            cleanup_address(http_delete, base, key, email, email_id=eid)