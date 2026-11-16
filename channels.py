#!/usr/bin/env python3
"""多渠道投递适配器：钉钉 / 企微 / 飞书 / 自定义 HTTP。

关键设计（历史 bug 修复，勿回退）：
  三家的机器人在“业务拒绝”时（加签错误、关键字不匹配、被限流、token 失效）
  依然返回 **HTTP 200**，错误只在响应体的 errcode/code 里体现。
  旧实现只判断 HTTP 200 就记 ok=True，会把失败当成功写进去重状态，
  导致这条消息永久不再重推 —— 静默丢消息。因此必须校验业务码。

其他要点：
  - 各渠道消息体有字节上限（企微 4096、钉钉 20000、飞书 30000），超限直接拒收，
    这里按渠道上限做安全截断，避免整条推送失败。
  - 网络类错误自动重试一次（幂等性由业务接受：宁可偶发重复，不要静默丢失）。
  - 密钥统一由外部解析，绝不在返回值里回显。
"""
import base64
import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

ADAPTERS = {}

# 各渠道消息体字节上限（保守取值，留出 JSON 包装余量）
MAX_BYTES = {"wecom": 4000, "dingtalk": 18000, "feishu": 28000, "custom": 60000}

UA = "TenderConsole/1.0 (+ai-market-demo)"


def _register(name):
    def deco(fn):
        ADAPTERS[name] = fn
        return fn

    return deco


def _now_ms():
    return str(round(time.time() * 1000))


def _now_s():
    return str(int(time.time()))


def _truncate_bytes(text, limit):
    """按 UTF-8 字节数安全截断，不切坏多字节字符。"""
    b = text.encode("utf-8")
    if len(b) <= limit:
        return text
    cut = b[: max(0, limit - 20)]
    # 去掉末尾可能被截断的半个字符
    while cut:
        try:
            return cut.decode("utf-8") + "\n\n…（内容过长已截断，完整信息请查看配置台）"
        except UnicodeDecodeError:
            cut = cut[:-1]
    return text[:200]


def _post(url, body, retries=1):
    """发 POST，返回 {ok, status, resp, error}。仅对网络类错误重试。"""
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": UA},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read() or b"{}"
                try:
                    resp = json.loads(raw)
                except Exception:
                    resp = {"_raw": raw.decode("utf-8", "replace")[:300]}
                return {"ok": True, "status": r.status, "resp": resp}
        except urllib.error.HTTPError as e:
            try:
                resp = json.loads(e.read() or b"{}")
            except Exception:
                resp = {}
            # 4xx/5xx 属确定性失败，不重试
            return {"ok": False, "status": e.code, "resp": resp, "error": f"HTTP {e.code}"}
        except Exception as e:  # 超时、DNS、连接重置等
            last = str(e)[:200]
            if attempt < retries:
                time.sleep(0.6)
                continue
            return {"ok": False, "error": f"网络错误: {last}"}
    return {"ok": False, "error": f"网络错误: {last}"}


def _biz_fail(resp, code_keys, msg_keys, code_ok=(0,)):
    """从响应体提取业务码；返回 (失败?, 错误信息)。"""
    if not isinstance(resp, dict):
        return False, ""
    for k in code_keys:
        if k in resp:
            try:
                code = int(resp.get(k))
            except (TypeError, ValueError):
                continue
            if code not in code_ok:
                msg = ""
                for mk in msg_keys:
                    if resp.get(mk):
                        msg = str(resp.get(mk))
                        break
                return True, f"{k}={code} {msg}".strip()
    return False, ""


def _sign(secret):
    """钉钉加签：timestamp+"\\n"+secret 做 HMAC-SHA256，再 base64 + urlencode。"""
    if not secret:
        return None
    s = _now_ms() + "\n" + secret
    return urllib.parse.quote_plus(
        base64.b64encode(hmac.new(secret.encode(), s.encode(), hashlib.sha256).digest()).decode()
    )


def _flatten_markdown(text):
    """飞书 text 消息不渲染 markdown，把常见标记压成可读纯文本。"""
    t = text
    t = re.sub(r"`([^`]+)`", r"\1", t)            # 去掉行内代码反引号
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)         # 去掉加粗
    t = re.sub(r"^#{1,3}\s*", "", t, flags=re.M)   # 去掉标题 #
    t = re.sub(r"^>\s?", "", t, flags=re.M)        # 去掉引用 >
    t = re.sub(r"^-{3,}\s*$", "————————", t, flags=re.M)  # 分隔线
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1：\2", t)  # 链接 [t](u) -> t：u
    return t.strip()


@_register("dingtalk")
def send_dingtalk(webhook, secret, title, text):
    ts = _now_ms()
    sign = _sign(secret)
    url = webhook
    if sign:
        sep = "&" if "?" in webhook else "?"
        url = f"{webhook}{sep}timestamp={ts}&sign={sign}"
    body = json.dumps(
        {"msgtype": "markdown", "markdown": {"title": title, "text": _truncate_bytes(text, MAX_BYTES["dingtalk"])}},
        ensure_ascii=False,
    ).encode("utf-8")
    r = _post(url, body)
    if r.get("ok"):
        bad, msg = _biz_fail(r.get("resp"), ("errcode",), ("errmsg",), code_ok=(0,))
        if bad:
            return {"ok": False, "status": r.get("status"), "resp": r.get("resp"),
                    "error": f"钉钉拒绝: {msg}"}
    return r


@_register("wecom")
def send_wecom(webhook, secret, title, text):
    ts = _now_s()
    url = webhook
    if secret:
        s = ts + "\n" + secret
        sign = base64.b64encode(
            hmac.new(secret.encode(), s.encode(), hashlib.sha256).digest()
        ).decode()
        sep = "&" if "?" in webhook else "?"
        url = f"{webhook}{sep}timestamp={ts}&sign={sign}"
    body = json.dumps(
        {"msgtype": "markdown", "markdown": {"content": _truncate_bytes(text, MAX_BYTES["wecom"])}},
        ensure_ascii=False,
    ).encode("utf-8")
    r = _post(url, body)
    if r.get("ok"):
        bad, msg = _biz_fail(r.get("resp"), ("errcode",), ("errmsg",), code_ok=(0,))
        if bad:
            return {"ok": False, "status": r.get("status"), "resp": r.get("resp"),
                    "error": f"企微拒绝: {msg}"}
    return r


@_register("feishu")
def send_feishu(webhook, secret, title, text):
    body = {"msg_type": "text", "content": {"text": _truncate_bytes(_flatten_markdown(text), MAX_BYTES["feishu"])}}
    if secret:
        ts = _now_s()
        s = ts + "\n" + secret
        sign = base64.b64encode(
            hmac.new(secret.encode(), s.encode(), hashlib.sha256).digest()
        ).decode()
        body["timestamp"] = ts
        body["sign"] = sign
    r = _post(webhook, json.dumps(body, ensure_ascii=False).encode("utf-8"))
    if r.get("ok"):
        # 飞书新旧版本字段不一致：老版 code/msg，新版 StatusCode/StatusMessage
        bad, msg = _biz_fail(r.get("resp"), ("code", "StatusCode"), ("msg", "StatusMessage"), code_ok=(0,))
        if bad:
            return {"ok": False, "status": r.get("status"), "resp": r.get("resp"),
                    "error": f"飞书拒绝: {msg}"}
    return r


@_register("custom")
def send_custom(webhook, secret, title, text):
    payload = {
        "title": title,
        "text": _truncate_bytes(text, MAX_BYTES["custom"]),
        "markdown": _truncate_bytes(text, MAX_BYTES["custom"]),
    }
    if secret:
        payload["secret"] = secret
    r = _post(webhook, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if r.get("ok"):
        bad, msg = _biz_fail(r.get("resp"), ("errcode", "code"), ("errmsg", "message", "msg"), code_ok=(0,))
        if bad:
            return {"ok": False, "status": r.get("status"), "resp": r.get("resp"),
                    "error": f"接口拒绝: {msg}"}
    return r


def dispatch(channel, webhook, secret, title, text):
    fn = ADAPTERS.get(channel)
    if not fn:
        return {"ok": False, "error": f"未知渠道: {channel}"}
    if not webhook:
        return {"ok": False, "error": "webhook 未配置"}
    return fn(webhook, secret, title, text)
