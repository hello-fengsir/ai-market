#!/usr/bin/env python3
"""标讯多任务配置台（标准库 http.server，零第三方依赖）。

功能：
  - 任务 / 渠道 / 规则 的读写（配置落盘 data/tasks.json、data/channels.json）
  - 每个任务可绑定多个 webhook（渠道），密钥不在接口回显
  - 预览：应用规则返回命中项与淘汰原因，不发送（dry-run）
  - 手动推送：对选中项渲染消息并发送到该任务绑定的各渠道，落 outbox 记录
  - 定时推送：按任务 schedule（cron）自动扫描并推送新命中项，全局开关可持久化
  - 真实采集为可选项（默认离线样例），由请求参数 live=1 触发

安全与可靠性约定（历史问题修复，勿回退）：
  - 所有写接口做字段白名单 + 类型校验；任务/渠道 id 只允许 [A-Za-z0-9_-]，
    因为它会拼进状态文件路径，不校验可被路径穿越写任意文件。
  - webhook 只允许 http/https，防止 file:// 与非常规协议被当作投递目标。
  - 配置文件用「临时文件 + replace」原子写，并用一把全局锁串行化写入，
    避免并发请求把 JSON 写坏（半截文件会让服务启动后配置全丢）。
  - 推送加锁，双击/并发请求不会重复向同一个群发两遍。
  - 请求体限制 1MB；非法 JSON 返回 400 而不是 500。
  - 所有路由统一异常兜底，避免连接被重置（客户端收到空响应难以排查）。
"""
import argparse
import datetime as dt
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import channels as channels_mod
import engine
import fetcher
import render
import scheduler as scheduler_mod

ROOT = Path(__file__).parent
DATA = ROOT / "data"
STATE_DIR = DATA / "state"
OUTBOX_DIR = DATA / "outbox"
SECRETS_FILE = DATA / "secrets.json"
SETTINGS_FILE = DATA / "settings.json"
UI_FILE = ROOT / "ui.html"

STATE_DIR.mkdir(parents=True, exist_ok=True)
OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
OUTBOX_LOG = OUTBOX_DIR / "deliveries.jsonl"

MAX_BODY = 1024 * 1024          # 1MB，足够配置类请求
OUTBOX_ROTATE_BYTES = 2 * 1024 * 1024
OUTBOX_KEEP = 200               # 接口返回的最大条数

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CFG_LOCK = threading.RLock()     # 保护配置文件读写
PUSH_LOCK = threading.Lock()     # 串行化推送，防重复发送
UPDATE_LOCK = threading.Lock()   # 真实采集一次可能较久，禁止并发刷新官网


# ---------------------------------------------------------------- 基础设施

class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    """原子写：先写 .tmp 再 replace，避免半截文件导致配置损坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_secrets():
    d = load_json(SECRETS_FILE, {})
    return d if isinstance(d, dict) else {}


def save_secrets(d):
    save_json(SECRETS_FILE, d)


def resolve_secret(ref):
    if not ref:
        return None
    sec = load_secrets()
    if ref in sec and sec[ref]:
        return sec[ref]
    # 允许用环境变量提供密钥：ref 形如 sec:dingtalk_sales → 读 DINGTALK_SALES
    key = ref.split(":", 1)[-1] if ":" in ref else ref
    return os.getenv(key) or os.getenv(key.upper()) or None


def load_settings():
    d = load_json(SETTINGS_FILE, {})
    return d if isinstance(d, dict) else {}


def save_settings(d):
    save_json(SETTINGS_FILE, d)


def channel_public(ch):
    return {
        "id": ch.get("id"),
        "name": ch.get("name", ch.get("id")),
        "channel": ch.get("channel"),
        "webhook": ch.get("webhook", ""),
        "secret_ref": ch.get("secret_ref"),
        "has_secret": bool(resolve_secret(ch.get("secret_ref"))),
    }


def append_outbox(entry):
    with CFG_LOCK:
        try:
            if OUTBOX_LOG.exists() and OUTBOX_LOG.stat().st_size > OUTBOX_ROTATE_BYTES:
                old = OUTBOX_LOG.with_name(OUTBOX_LOG.name + ".1")
                if old.exists():
                    old.unlink()
                OUTBOX_LOG.replace(old)
        except Exception:
            pass
        with OUTBOX_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_outbox(n=OUTBOX_KEEP):
    if not OUTBOX_LOG.exists():
        return []
    try:
        lines = OUTBOX_LOG.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- 校验

def _req_str(value, field, max_len=200, required=True, default=""):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError(400, f"{field} 必须是字符串")
    v = value.strip()
    if required and not v:
        raise ApiError(400, f"{field} 不能为空")
    if len(v) > max_len:
        raise ApiError(400, f"{field} 过长（最多 {max_len} 字符）")
    return v or default


def _valid_id(value, field="id"):
    v = _req_str(value, field, 64)
    if not ID_RE.fullmatch(v):
        raise ApiError(400, f"{field} 只能包含字母、数字、下划线和短横线（最长 64）")
    return v


def _valid_webhook(value, field="webhook"):
    v = _req_str(value, field, 2000)
    if not re.match(r"^https?://", v, re.I):
        raise ApiError(400, f"{field} 必须是 http:// 或 https:// 开头的地址")
    return v


def _valid_rule(rule):
    if rule is None:
        return {"include": [], "exclude": []}
    if not isinstance(rule, dict):
        raise ApiError(400, "rule 必须是对象")
    out = {}
    for key in ("include", "exclude"):
        conds = rule.get(key) or []
        if not isinstance(conds, list):
            raise ApiError(400, f"rule.{key} 必须是数组")
        cleaned = []
        for c in conds:
            if not isinstance(c, dict):
                raise ApiError(400, f"rule.{key} 的每一项必须是对象")
            field = c.get("field")
            op = c.get("op", "contains_any")
            if not isinstance(field, str) or not field.strip():
                raise ApiError(400, f"rule.{key} 缺少 field")
            if op not in engine.VALID_OPS:
                raise ApiError(400, f"不支持的 op: {op}")
            value = c.get("value")
            if op in ("gte", "lte", "between"):
                try:
                    if op == "between":
                        if not (isinstance(value, (list, tuple)) and len(value) == 2):
                            raise ValueError
                        float(value[0]); float(value[1])
                    else:
                        float(value)
                except (TypeError, ValueError):
                    raise ApiError(400, f"{field}/{op} 的 value 必须是数字"
                                        + ("（between 需要两个数字）" if op == "between" else ""))
            elif op == "window":
                if not isinstance(value, dict):
                    raise ApiError(400, f"{field}/window 的 value 必须是对象")
            elif op == "eq":
                if value is None:
                    raise ApiError(400, f"{field}/eq 的 value 不能为空")
            else:
                if not isinstance(value, list):
                    raise ApiError(400, f"{field}/{op} 的 value 必须是数组")
                value = [str(v) for v in value if str(v).strip() != ""]
                if not value:
                    raise ApiError(400, f"{field}/{op} 的关键词不能为空")
            cleaned.append({"field": field.strip(), "op": op, "value": value})
        out[key] = cleaned
    return out


def _valid_task(body, existing_ids):
    tid = body.get("id")
    if tid in (None, ""):
        tid = "t-" + dt.datetime.now().strftime("%Y%m%d%H%M%S")
    tid = _valid_id(tid, "任务 id")

    name = _req_str(body.get("name"), "任务名称", 100)
    source = _req_str(body.get("source"), "数据源", 100, required=False, default="shanghai_tender") or "shanghai_tender"

    raw_limit = body.get("limit", 8)
    if raw_limit is None or raw_limit == "":
        raw_limit = 8
    # 注意：不能用 `or 8`，否则 limit=0 会被静默改成 8，掩盖前端传错值的问题
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        raise ApiError(400, "limit 必须是整数")
    if not (1 <= limit <= 100):
        raise ApiError(400, "limit 必须在 1-100 之间")

    pred = next((t for t in existing_ids if t.get("id") == tid), None)
    # 更新任务时，未提交定时字段表示“保持原值”。旧实现把缺失字段当 False/空串，
    # 任何旧客户端或局部更新都会静默关闭定时任务。
    if "auto_enabled" in body:
        auto_enabled = bool(body.get("auto_enabled"))
    else:
        auto_enabled = bool((pred or {}).get("auto_enabled", False))
    if "schedule" in body:
        raw_schedule = body.get("schedule")
        if raw_schedule is not None and not isinstance(raw_schedule, str):
            raise ApiError(400, "schedule 必须是字符串")
        schedule = (raw_schedule or "").strip()
    else:
        schedule = str((pred or {}).get("schedule") or "").strip()
    if auto_enabled:
        if not schedule:
            raise ApiError(400, "开启定时推送时必须填写执行周期")
        try:
            scheduler_mod.validate(schedule)
        except scheduler_mod.CronError as e:
            raise ApiError(400, f"cron 表达式非法：{e}")

    channel_ids = body.get("channel_ids") or []
    if not isinstance(channel_ids, list):
        raise ApiError(400, "channel_ids 必须是数组")
    channels = CHANNELS_CACHE["list"]
    known = {c.get("id") for c in channels}
    clean_ids = []
    for cid in channel_ids:
        cid = str(cid)
        if cid not in known:
            raise ApiError(400, f"渠道不存在：{cid}")
        if cid not in clean_ids:
            clean_ids.append(cid)

    return {
        "id": tid,
        "name": name,
        "source": source,
        "enabled": bool(body.get("enabled", True)) if "enabled" in body else (pred or {}).get("enabled", True),
        "auto_enabled": auto_enabled,
        "schedule": schedule if auto_enabled else "",
        "mode": "manual",
        "dedupe": "per_task",
        "limit": limit,
        "channel_ids": clean_ids,
        "template": "tender_md",
        "rule": _valid_rule(body.get("rule")),
    }


def _valid_channel(body):
    cid = body.get("id")
    if cid in (None, ""):
        cid = "wh-" + dt.datetime.now().strftime("%Y%m%d%H%M%S")
    cid = _valid_id(cid, "渠道 id")
    name = _req_str(body.get("name"), "渠道名称", 100)
    ch_type = _req_str(body.get("channel"), "渠道类型", 20)
    if ch_type not in channels_mod.ADAPTERS:
        raise ApiError(400, f"不支持的渠道类型：{ch_type}")
    webhook = _valid_webhook(body.get("webhook"), "Webhook 地址")
    secret_ref = body.get("secret_ref")
    if secret_ref is not None and not isinstance(secret_ref, str):
        raise ApiError(400, "secret_ref 必须是字符串")
    secret_ref = (secret_ref or "").strip()
    return {
        "id": cid,
        "name": name,
        "channel": ch_type,
        "webhook": webhook,
        "secret_ref": secret_ref or None,
    }


# 渠道列表缓存（校验任务引用时避免重复读盘；写入后会刷新）
CHANNELS_CACHE = {"list": []}


def refresh_channels_cache():
    CHANNELS_CACHE["list"] = load_json(DATA / "channels.json", [])


# ---------------------------------------------------------------- 推送

def do_push(task, item_ids, force=False):
    """把选中项渲染后发到任务绑定的全部渠道，并落投递记录。"""
    if not PUSH_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "上一次推送尚未完成，请稍后重试"}
    try:
        items = fetcher.get_items(
            task.get("source", "shanghai_tender"),
            live=bool(task.get("_live")),
            live_module_path=os.getenv("LIVE_MODULE_PATH") or None,
        )
        res = engine.run_task(task, items, STATE_DIR, force=force)
        idset = {str(i) for i in (item_ids or [])}
        selected = [m for m in res["matched"] if str(m.get("id")) in idset]
        if not selected:
            return {"ok": False, "error": "没有可推送项（可能已被规则过滤或不在本次预览结果内）"}

        title = task.get("name", "标讯命中")
        text = render.render_tender(selected, title=title)

        channels = load_json(DATA / "channels.json", [])
        results = []
        for cid in task.get("channel_ids", []):
            ch = next((c for c in channels if c.get("id") == cid), None)
            if not ch:
                results.append({"channel_id": cid, "ok": False, "error": "渠道不存在（可能已被删除）"})
                continue
            secret = resolve_secret(ch.get("secret_ref"))
            r = channels_mod.dispatch(ch.get("channel"), ch.get("webhook"), secret, title, text)
            results.append({
                "channel_id": cid,
                "channel": ch.get("channel"),
                "name": ch.get("name"),
                **r,
            })

        ok_all = bool(results) and all(x.get("ok") for x in results)
        # 只有全部渠道成功才写入去重状态：宁可失败后重推（可能偶发重复），
        # 也不要因为部分成功就标记已发送，导致失败渠道永久漏消息。
        if ok_all:
            engine.commit_sent(task, selected, STATE_DIR)

        append_outbox({
            "at": dt.datetime.now().isoformat(timespec="seconds"),
            "task_id": task.get("id"),
            "task_name": title,
            "item_ids": [s.get("id") for s in selected],
            "results": results,
            "ok": ok_all,
        })
        failed = [r for r in results if not r.get("ok")]
        return {
            "ok": ok_all,
            "results": results,
            "selected": len(selected),
            "failed": len(failed),
            "error": None if results else "该任务未绑定任何渠道",
        }
    finally:
        PUSH_LOCK.release()


def auto_trigger(task_id):
    """调度器回调：只推「本任务尚未发送过」的新命中项，并返回可观测结果。"""
    tasks = load_json(DATA / "tasks.json", [])
    task = next((t for t in tasks if t.get("id") == task_id), None)
    if not task:
        return {"ok": False, "status": "task_missing", "error": "任务不存在"}
    if task.get("enabled", True) is False or task.get("auto_enabled", False) is False:
        return {"ok": True, "status": "disabled", "pushed": 0}
    if not task.get("channel_ids"):
        return {"ok": False, "status": "no_channels", "error": "任务未绑定推送渠道", "pushed": 0}
    items = fetcher.get_items(
        task.get("source", "shanghai_tender"),
        live=bool(task.get("_live")),
        live_module_path=os.getenv("LIVE_MODULE_PATH") or None,
    )
    res = engine.run_task(task, items, STATE_DIR, force=False)
    fresh_ids = [m["id"] for m in res["fresh"] if m.get("id")]
    if not fresh_ids:
        return {
            "ok": True,
            "status": "no_new_items",
            "matched": res.get("matched_total", len(res.get("matched", []))),
            "pushed": 0,
        }
    result = do_push(task, fresh_ids, force=False)
    return {
        **result,
        "status": "pushed" if result.get("ok") else "push_failed",
        "pushed": result.get("selected", 0) if result.get("ok") else 0,
    }


def refresh_live_data():
    """强制更新共享标讯缓存；成功后所有任务使用同一批最新数据。"""
    if not UPDATE_LOCK.acquire(blocking=False):
        return {"ok": False, "status": "busy", "error": "数据更新正在进行"}
    try:
        result = fetcher.update_from_live(os.getenv("LIVE_MODULE_PATH") or None)
        return {"ok": True, "status": "updated", **result["status"]}
    except Exception as e:
        return {"ok": False, "status": "update_failed", "error": str(e)[:500],
                **fetcher.get_update_status()}
    finally:
        UPDATE_LOCK.release()


# 自动推送与每日数据更新使用独立调度器：数据先在 08:30 强制刷新，任务再按各自时间推送。
AUTO_PUSH = load_settings().get("auto_push", os.getenv("AUTO_PUSH", "0") == "1")
AUTO_UPDATE = load_settings().get("auto_update", os.getenv("AUTO_UPDATE", "1") != "0")
UPDATE_SCHEDULE = os.getenv("UPDATE_SCHEDULE", "30 8 * * *")
sched = scheduler_mod.Scheduler(lambda: load_json(DATA / "tasks.json", []), auto_trigger)
update_sched = scheduler_mod.Scheduler(
    lambda: [{"id": "daily-live-update", "name": "每日真实标讯更新", "enabled": True,
              "auto_enabled": True, "schedule": UPDATE_SCHEDULE}] if AUTO_UPDATE else [],
    lambda _task_id: refresh_live_data(),
)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "TenderConsole/1.0"

    # ---- 认证 ----
    def _check_auth(self):
        auth = load_json(DATA / "auth.json", {})
        # 未配置密码则放行（兼容旧部署）
        if not auth.get("password_sha256"):
            return True
        # 健康检查免认证，供容器 healthcheck 使用
        if self.path.split("?")[0] == "/api/health":
            return True
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return self._deny()
        try:
            import base64
            import hashlib

            dec = base64.b64decode(hdr[6:]).decode("utf-8", "replace")
            user, _, pw = dec.partition(":")
        except Exception:
            return self._deny()
        if user != auth.get("username", "admin"):
            return self._deny()
        if hashlib.sha256(pw.encode("utf-8")).hexdigest() != auth["password_sha256"]:
            return self._deny()
        return True

    def _deny(self):
        body = json.dumps({"error": "需要登录"}, ensure_ascii=False).encode("utf-8")
        self.send_response(401)
        # HTTP 头必须是 ASCII，realm 不能含中文
        self.send_header("WWW-Authenticate", 'Basic realm="TenderConsole"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    # ---- 基础响应 ----
    def _send(self, code, obj=None, body=None, ctype="application/json; charset=utf-8"):
        payload = body if body is not None else json.dumps(obj, ensure_ascii=False)
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 配置台页面必须实时反映改动，禁止浏览器缓存
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            raise ApiError(400, "Content-Length 非法")
        if length > MAX_BODY:
            raise ApiError(413, f"请求体过大（上限 {MAX_BODY // 1024}KB）")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError(400, "请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return data

    def _safe(self, fn):
        try:
            if not self._check_auth():
                return
            fn()
        except ApiError as e:
            self._send(e.code, {"error": e.msg})
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            try:
                self._send(500, {"error": f"服务器内部错误: {str(e)[:200]}"})
            except Exception:
                pass

    def log_message(self, fmt, *args):
        # 降低日志噪音，只保留非 2xx/3xx
        try:
            code = str(args[1]) if len(args) > 1 else ""
            if code.startswith(("2", "3")):
                return
        except Exception:
            pass
        super().log_message(fmt, *args)

    # ---- 路由 ----
    def do_GET(self):
        self._safe(self._get)

    def do_POST(self):
        self._safe(self._post)

    def do_DELETE(self):
        self._safe(self._delete)

    def _get(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/api/health":
            self._send(200, {"ok": True})
            return
        if p in ("/", "/index.html"):
            if UI_FILE.exists():
                self._send(200, body=UI_FILE.read_text(encoding="utf-8"),
                           ctype="text/html; charset=utf-8")
            else:
                self._send(404, {"error": "ui.html 缺失"})
            return
        if p == "/api/config":
            self._send(200, {
                "tasks": load_json(DATA / "tasks.json", []),
                "channels": [channel_public(c) for c in load_json(DATA / "channels.json", [])],
                "facets": self._facets(),
                "scheduler": self._sched_state(),
                "updater": self._update_state(),
            })
            return
        if p == "/api/tasks":
            self._send(200, load_json(DATA / "tasks.json", []))
            return
        if p == "/api/channels":
            self._send(200, [channel_public(c) for c in load_json(DATA / "channels.json", [])])
            return
        if p == "/api/outbox":
            self._send(200, read_outbox())
            return
        if p == "/api/scheduler":
            self._send(200, self._sched_state())
            return
        if p == "/api/updater":
            self._send(200, self._update_state())
            return
        self._send(404, {"error": "not found"})

    def _facets(self):
        items = fetcher.get_items("shanghai_tender", live=False)
        preset_regions = [
            "北京", "上海", "广州", "深圳", "成都", "重庆", "杭州", "武汉",
            "西安", "南京", "苏州", "天津", "长沙", "郑州", "东莞", "青岛",
            "合肥", "佛山", "宁波", "无锡", "厦门", "济南", "福州", "大连",
            "沈阳", "昆明", "南昌", "南宁", "贵阳", "太原", "石家庄", "哈尔滨",
            "长春", "常州", "惠州", "珠海", "中山",
        ]
        data_regions = {i.get("region") for i in items if i.get("region")}
        task_regions = {
            str(v)
            for task in load_json(DATA / "tasks.json", [])
            for cond in (task.get("rule", {}).get("include", []) or [])
            if cond.get("field") == "region" and cond.get("op") == "in"
            for v in (cond.get("value") or [])
        }
        regions = preset_regions + sorted((data_regions | task_regions) - set(preset_regions))
        return {
            "regions": regions,
            "statuses": sorted({i.get("status") for i in items if i.get("status")}),
        }

    @staticmethod
    def _sched_state():
        status = sched.status()
        tasks = load_json(DATA / "tasks.json", [])
        active = [
            {
                "id": t.get("id"),
                "name": t.get("name") or t.get("id"),
                "schedule": t.get("schedule", ""),
                "channels": len(t.get("channel_ids") or []),
            }
            for t in tasks
            if t.get("enabled", True) is not False and t.get("auto_enabled", False) is True
        ]
        return {
            "enabled": AUTO_PUSH,
            **status,
            "active_tasks": active,
            "active_count": len(active),
        }

    @staticmethod
    def _update_state():
        return {
            "enabled": AUTO_UPDATE,
            "running": update_sched.running,
            "schedule": UPDATE_SCHEDULE,
            "collector_configured": bool(os.getenv("LIVE_MODULE_PATH")),
            "scheduler": update_sched.status(),
            "last_update": fetcher.get_update_status(),
            # 预留可选大模型配置状态；每日采集本身不依赖大模型。
            "llm": {
                "enabled": os.getenv("LLM_ENABLED", "0") == "1",
                "base_url_configured": bool(os.getenv("LLM_BASE_URL")),
                "api_key_configured": bool(os.getenv("LLM_API_KEY")),
                "model": os.getenv("LLM_MODEL", ""),
            },
        }

    def _post(self):
        p = urlparse(self.path).path

        if p == "/api/tasks":
            body = self._json_body()
            with CFG_LOCK:
                tasks = load_json(DATA / "tasks.json", [])
                refresh_channels_cache()
                task = _valid_task(body, tasks)
                idx = next((i for i, t in enumerate(tasks) if t.get("id") == task["id"]), None)
                if idx is None:
                    tasks.append(task)
                else:
                    tasks[idx] = task
                save_json(DATA / "tasks.json", tasks)
            self._send(200, {"ok": True, "task": task})
            return

        if p == "/api/channels":
            body = self._json_body()
            secret = body.pop("secret", None)
            with CFG_LOCK:
                channels = load_json(DATA / "channels.json", [])
                ch = _valid_channel(body)
                if secret:
                    if not ch.get("secret_ref"):
                        ch["secret_ref"] = "sec:" + ch["id"]
                    sec = load_secrets()
                    sec[ch["secret_ref"]] = secret
                    save_secrets(sec)
                idx = next((i for i, c in enumerate(channels) if c.get("id") == ch["id"]), None)
                if idx is None:
                    channels.append(ch)
                else:
                    channels[idx] = ch
                save_json(DATA / "channels.json", channels)
                refresh_channels_cache()
            self._send(200, {"ok": True, "channel": channel_public(ch)})
            return

        if p == "/api/channels/test":
            body = self._json_body()
            cid = _valid_id(body.get("id"), "渠道 id")
            channels = load_json(DATA / "channels.json", [])
            ch = next((c for c in channels if c.get("id") == cid), None)
            if not ch:
                raise ApiError(404, "渠道不存在")
            secret = resolve_secret(ch.get("secret_ref"))
            msg = (
                "# 渠道连通性测试\n"
                f"> 渠道：{ch.get('name')}（{ch.get('channel')}）\n\n"
                f"- 发送时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                "- 说明：收到本条即表示 webhook 与加签配置正确。"
            )
            r = channels_mod.dispatch(ch.get("channel"), ch.get("webhook"), secret,
                                      "渠道连通性测试", msg)
            self._send(200, {"ok": bool(r.get("ok")), "result": r})
            return

        if p == "/api/preview":
            body = self._json_body()
            tid = _valid_id(body.get("task_id"), "task_id")
            tasks = load_json(DATA / "tasks.json", [])
            task = next((t for t in tasks if t.get("id") == tid), None)
            if not task:
                raise ApiError(404, "任务不存在")
            live = bool(body.get("live", False))
            items = fetcher.get_items(task.get("source", "shanghai_tender"), live=live,
                                      live_module_path=os.getenv("LIVE_MODULE_PATH") or None)
            res = engine.run_task(task, items, STATE_DIR, force=False)
            fresh_ids = {m.get("id") for m in res["fresh"]}
            matched = [
                {
                    "id": m.get("id"),
                    "title": m.get("title"),
                    "purchaser": m.get("purchaser"),
                    "budget": m.get("budget"),
                    "deadline": m.get("deadline"),
                    "region": m.get("region"),
                    "status": m.get("status"),
                    "url": m.get("url", ""),
                    "purchaser_contact": m.get("purchaser_contact", ""),
                    "agent_contact": m.get("agent_contact", ""),
                    "is_new": m.get("id") in fresh_ids,
                }
                for m in res["matched"]
            ]
            self._send(200, {
                "matched": matched,
                "new": [m["id"] for m in matched if m["is_new"]],
                "excluded": res["excluded"],
                "sent_count": res["sent_count"],
                "new_count": res["new_count"],
                "matched_total": res.get("matched_total", len(matched)),
                "channel_ids": task.get("channel_ids", []),
            })
            return

        if p == "/api/push":
            body = self._json_body()
            tid = _valid_id(body.get("task_id"), "task_id")
            ids = body.get("ids") or []
            if not isinstance(ids, list) or not ids:
                raise ApiError(400, "请至少选择一条标讯")
            if len(ids) > 200:
                raise ApiError(400, "单次推送最多 200 条")
            tasks = load_json(DATA / "tasks.json", [])
            task = next((t for t in tasks if t.get("id") == tid), None)
            if not task:
                raise ApiError(404, "任务不存在")
            if not task.get("channel_ids"):
                raise ApiError(400, "该任务还没有绑定任何推送渠道")
            result = do_push(task, [str(i) for i in ids], force=True)
            code = 200 if result.get("results") else 400
            self._send(code, result)
            return

        if p == "/api/updater/run":
            result = refresh_live_data()
            self._send(200 if result.get("ok") else 502, result)
            return

        if p == "/api/scheduler":
            global AUTO_PUSH
            body = self._json_body()
            action = (body.get("action") or "").lower()
            if action == "on":
                active = [t for t in load_json(DATA / "tasks.json", [])
                          if t.get("enabled", True) is not False
                          and t.get("auto_enabled", False) is True
                          and (t.get("schedule") or "").strip()]
                if not active:
                    raise ApiError(400, "没有已启用定时设置的任务，请先在任务编辑页开启并保存定时推送")
                AUTO_PUSH = True
                sched.start()
            elif action == "off":
                AUTO_PUSH = False
                sched.stop()
            else:
                raise ApiError(400, "action 仅支持 on/off")
            # 开关状态持久化，容器重启后仍然生效
            st = load_settings()
            st["auto_push"] = AUTO_PUSH
            save_settings(st)
            self._send(200, {"ok": True, **self._sched_state()})
            return

        self._send(404, {"error": "not found"})

    def _delete(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)

        if u.path == "/api/tasks":
            tid = _valid_id((qs.get("id") or [None])[0], "任务 id")
            with CFG_LOCK:
                tasks = load_json(DATA / "tasks.json", [])
                if not any(t.get("id") == tid for t in tasks):
                    raise ApiError(404, "任务不存在")
                save_json(DATA / "tasks.json", [t for t in tasks if t.get("id") != tid])
            try:
                engine.clear_state(tid, STATE_DIR)
            except Exception:
                pass
            self._send(200, {"ok": True})
            return

        if u.path == "/api/channels":
            cid = _valid_id((qs.get("id") or [None])[0], "渠道 id")
            force = (qs.get("force") or ["0"])[0] in ("1", "true", "yes")
            tasks = load_json(DATA / "tasks.json", [])
            used_by = [t.get("name") or t.get("id") for t in tasks
                       if cid in (t.get("channel_ids") or [])]
            # 被任务引用时默认拒绝，避免任务渠道悬空导致静默推送失败
            if used_by and not force:
                raise ApiError(409, "该渠道正被以下任务使用，请先解除绑定：" + "、".join(used_by))
            with CFG_LOCK:
                channels = load_json(DATA / "channels.json", [])
                if not any(c.get("id") == cid for c in channels):
                    raise ApiError(404, "渠道不存在")
                save_json(DATA / "channels.json", [c for c in channels if c.get("id") != cid])
                if force:
                    for t in tasks:
                        if cid in (t.get("channel_ids") or []):
                            t["channel_ids"] = [x for x in t["channel_ids"] if x != cid]
                    save_json(DATA / "tasks.json", tasks)
                refresh_channels_cache()
            self._send(200, {"ok": True, "unbound_from": used_by if force else []})
            return

        self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()

    refresh_channels_cache()
    if AUTO_PUSH:
        sched.start()
    if AUTO_UPDATE:
        update_sched.start()

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"标讯配置台已启动: http://{a.host}:{a.port}/  自动推送={'开' if AUTO_PUSH else '关'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sched.stop()
        update_sched.stop()
        srv.shutdown()


if __name__ == "__main__":
    main()
