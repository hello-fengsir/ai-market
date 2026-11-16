#!/usr/bin/env python3
"""标讯采集层：读取本地缓存，并从一手官网强制刷新每日数据。"""
import datetime as dt
import importlib.util
import json
import os
import threading
from pathlib import Path

FIXTURE = Path(__file__).parent / "data" / "tenders_fixture.json"
UPDATE_STATUS = Path(__file__).parent / "data" / "update_status.json"
_WRITE_LOCK = threading.Lock()


def _budget_number(value):
    """把“123万元 / 1,200,000元”等公告预算归一为元；未披露返回 None。"""
    text = str(value or "").replace(",", "").strip()
    if not text or text in ("公告未披露", "无", "不设置", "/"):
        return None
    import re
    # 优先识别带金额单位的数字，避免“包1-1266300.00元”误取包号 1。
    m = re.search(r"(\d+(?:\.\d+)?)\s*(亿元|万元|万|元)", text)
    if m:
        number = float(m.group(1))
        unit = m.group(2)
    else:
        m = re.search(r"(\d+(?:\.\d+)?)", text)
        if not m:
            return None
        number = float(m.group(1))
        unit = "元"
    if unit == "亿元":
        number *= 100_000_000
    elif unit in ("万元", "万"):
        number *= 10_000
    return number


def normalize_item(item, source="shanghai_tender"):
    """将不同采集器字段归一到规则引擎 schema。"""
    out = dict(item or {})
    if not out.get("region"):
        # 当前一手源是上海公共资源交易平台；平台内公告地域统一记为上海。
        out["region"] = "上海" if source == "shanghai_tender" else ""
    if out.get("budget_num") in (None, ""):
        out["budget_num"] = _budget_number(out.get("budget"))
    # 兼容采集器的 published 字段名。
    if not out.get("published_dt") and out.get("published"):
        out["published_dt"] = out["published"]
    return out


def get_fixture_items():
    """读取离线样例数据。

    仓库出于脱敏考虑不含样例标讯，文件缺失/为空/损坏时返回空列表，
    避免 clone 后首次启动因 FileNotFoundError 导致 /api/config 返回 500。
    """
    try:
        if not FIXTURE.exists():
            return []
        text = FIXTURE.read_text(encoding="utf-8").strip()
        data = json.loads(text) if text else []
        return [normalize_item(x) for x in data] if isinstance(data, list) else []
    except (OSError, ValueError) as e:
        print("fixture unavailable, using empty list:", e)
        return []


def _save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _load_collector(live_module_path):
    path = Path(live_module_path or os.getenv("LIVE_MODULE_PATH", ""))
    if not path.is_file():
        raise RuntimeError("未配置有效的真实采集脚本 LIVE_MODULE_PATH")
    spec = importlib.util.spec_from_file_location("tender_live_collector", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载采集脚本：{path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in ("TERMS", "search", "detail", "ident"):
        if not hasattr(mod, name):
            raise RuntimeError(f"采集脚本缺少 {name}")
    return mod


def update_from_live(live_module_path=None, days=21):
    """强制从一手官网刷新缓存；失败抛异常，绝不把旧缓存冒充今日更新。"""
    started = dt.datetime.now().astimezone()
    status = {"started_at": started.isoformat(timespec="seconds"), "ok": False}
    try:
        mod = _load_collector(live_module_path)
        now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
        start = (now - dt.timedelta(days=max(1, int(days)))).date().isoformat()
        end = now.date().isoformat()
        candidates, health = [], []
        for term in mod.TERMS:
            try:
                rows = mod.search(term, start, end)
                candidates.extend(rows)
                health.append({"term": term, "ok": True, "count": len(rows)})
            except Exception as e:
                health.append({"term": term, "ok": False, "error": str(e)[:200]})
        success_count = sum(1 for x in health if x["ok"])
        minimum = min(10, max(1, len(mod.TERMS)))
        if success_count < minimum:
            raise RuntimeError(f"采集不完整：仅 {success_count}/{len(mod.TERMS)} 个关键词成功")

        unique, errors = {}, []
        for c in {x.get("url"): x for x in candidates if x.get("url")}.values():
            try:
                item = normalize_item(mod.detail(c))
                item["id"] = mod.ident(item)
                # datetime 统一保存为可跨进程读取的 ISO 字符串。
                for key, value in list(item.items()):
                    if isinstance(value, (dt.datetime, dt.date)):
                        item[key] = value.isoformat()
                unique[item.get("url") or item["id"]] = item
            except Exception as e:
                errors.append({"url": c.get("url", ""), "error": str(e)[:200]})
        items = list(unique.values())
        if not items:
            raise RuntimeError("官网采集成功但未解析出任何标讯，拒绝覆盖现有缓存")

        with _WRITE_LOCK:
            _save_json(FIXTURE, items)
            status.update({
                "ok": True,
                "finished_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "source": getattr(mod, "BASE", "live_collector"),
                "period": {"from": start, "to": end},
                "items": len(items),
                "candidates": len(candidates),
                "keywords_ok": success_count,
                "keywords_total": len(mod.TERMS),
                "detail_errors": len(errors),
                "health": health,
            })
            _save_json(UPDATE_STATUS, status)
        return {"items": items, "status": status}
    except Exception as e:
        status.update({
            "finished_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "error": str(e)[:500],
        })
        with _WRITE_LOCK:
            _save_json(UPDATE_STATUS, status)
        raise


def get_update_status():
    try:
        data = json.loads(UPDATE_STATUS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_live_items(live_module_path=None):
    """兼容实时预览：刷新成功返回新数据，失败回退缓存并打印明确错误。"""
    try:
        return update_from_live(live_module_path)["items"]
    except Exception as e:
        print("live fetch failed, fallback to cached fixture:", e)
        return get_fixture_items()


def get_items(source="shanghai_tender", live=False, live_module_path=None):
    if live:
        return get_live_items(live_module_path)
    return get_fixture_items()
