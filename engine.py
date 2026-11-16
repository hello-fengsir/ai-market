#!/usr/bin/env python3
"""多维度规则引擎 + 按任务去重。

规则 schema（task.rule）：
  include: [{field, op, value}, ...]   命中需满足“全部” include 条件（AND）
  exclude: [{field, op, value}, ...]   命中“任一” exclude 即淘汰（OR）

支持的操作 op：
  contains_any  任一子串出现（value 为字符串列表）
  regex         正则匹配（value 为正则串）
  in            精确等于列表中某值
  eq            精确等于 value
  gte/lte       数值 >= / <= value（作用于预算等数值字段）
  between       数值落在 [lo, hi]
  window        时间窗：value={"from":ISO|"now"|null,"to":ISO|"now"|null}

覆盖维度：地区(region)、关键词(title/summary/text)、采购人名单(purchaser)、
金额(budget_num)、发标时间(published_dt)、截止时间(deadline_dt)、
标书获取窗口(file_*_dt)、状态(status)。

设计要点（均为历史 bug 修复，勿随意回退）：
  1. 时间基准必须每次求值取当前时间，绝不能用模块级常量。
     否则服务常驻后 NOW 被冻结在启动时刻，“排除已截止”会整年失效。
  2. window 分支必须独立于数值分支。旧实现先算数值、失败即 return False，
     导致没有 budget_num 字段的条目，“截止时间窗”条件永远不生效。
  3. 去重必须先于 limit 截断。否则真实采集（每天几十上百条）下，
     前 N 条恒为已发送项，fresh 永远为空，定时推送彻底推不出新内容。
"""
import datetime as dt
import json
import re
from pathlib import Path

# 数值型字段：只有这些字段参与 gte/lte/between 比较
NUMERIC_FIELDS = {"budget_num", "budget", "amount", "quantity"}

# 时间型字段：参与 window 比较
TIME_FIELDS = {"published_dt", "deadline_dt", "file_start_dt", "file_end_dt", "bid_open_dt"}

VALID_OPS = {"contains_any", "regex", "in", "eq", "gte", "lte", "between", "window"}


def now():
    """每次调用都返回当前时间（不要改用模块常量）。"""
    return dt.datetime.now()


def parse_dt(v):
    """尽量把各种时间写法解析成 datetime；失败返回 None。"""
    if not v:
        return None
    if isinstance(v, dt.datetime):
        return v
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day)
    s = str(v).strip()
    if not s:
        return None
    # 统一把 2026/11/13、2026年11月13日 归一成 ISO 便于解析
    norm = re.sub(r"[年月/.]", "-", s).replace("日", "")
    try:
        return dt.datetime.fromisoformat(norm.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        pass
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", norm)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            h = int(m.group(4) or 0)
            mi = int(m.group(5) or 0)
            return dt.datetime(y, mo, d, h, mi)
        except Exception:
            return None
    return None


def _as_number(item, field, raw):
    """取数值：优先字段本身，其次 budget_num 兜底；不可转则 None。"""
    for candidate in (raw, item.get("budget_num"), item.get("budget")):
        if candidate is None or candidate == "":
            continue
        try:
            return float(str(candidate).replace(",", "").replace("元", "").strip())
        except Exception:
            continue
    return None


def _match_window(item, field, val):
    """时间窗匹配。value 形如 {"from": "now", "to": null}。"""
    if not isinstance(val, dict):
        return False
    d = parse_dt(item.get(field, ""))
    if d is None:
        # 时间缺失时保守处理：不认为满足“未过期”类条件
        return False
    base = now()
    frm, to = val.get("from"), val.get("to")
    frm_d = base if frm == "now" else (parse_dt(frm) if frm else None)
    to_d = base if to == "now" else (parse_dt(to) if to else None)
    if frm_d and d < frm_d:
        return False
    if to_d and d > to_d:
        return False
    return True


def _text_blob(item):
    """伪字段 text：标题 + 摘要 + 采购人，关键词命中更实用。"""
    return " ".join(str(item.get(k, "") or "") for k in ("title", "summary", "purchaser"))


def _cond_match(item, cond):
    if not isinstance(cond, dict):
        return False
    op = cond.get("op", "contains_any")
    field = cond.get("field")
    val = cond.get("value")
    if not field:
        return False

    # 伪字段：标题+摘要+采购人拼接
    if field == "text":
        blob = _text_blob(item)
        raw = blob
    else:
        raw = item.get(field, "")

    # ---- 字符串/集合类 ----
    if op == "contains_any":
        return any(str(v) in str(raw) for v in (val or []) if str(v) != "")

    if op == "regex":
        try:
            return re.search(str(val), str(raw)) is not None
        except re.error:
            # 非法正则不应让整个请求 500，按不命中处理
            return False

    if op == "in":
        return str(raw) in [str(x) for x in (val or [])]

    if op == "eq":
        return str(raw) == str(val)

    # ---- 时间窗（必须独立于数值分支，见文件头说明 2）----
    if op == "window":
        return _match_window(item, field, val)

    # ---- 数值类 ----
    if op in ("gte", "lte", "between"):
        num = _as_number(item, field, raw)
        if num is None:
            return False
        try:
            if op == "gte":
                return num >= float(val)
            if op == "lte":
                return num <= float(val)
            lo, hi = val if isinstance(val, (list, tuple)) and len(val) == 2 else (val, val)
            return float(lo) <= num <= float(hi)
        except (TypeError, ValueError):
            return False

    return False


def evaluate(item, rule):
    """返回 (passed: bool, reasons: list[str])。"""
    if not rule:
        return (True, [])
    passed, reasons = True, []
    for c in rule.get("include") or []:
        if not _cond_match(item, c):
            passed = False
            reasons.append(f"未满足包含条件 {c.get('field')}/{c.get('op')}")
    for c in rule.get("exclude") or []:
        if _cond_match(item, c):
            passed = False
            reasons.append(f"命中排除条件 {c.get('field')}/{c.get('op')}")
    return (passed, reasons)


def _deadline_key(item):
    """排序键：有截止时间的按升序在前，缺失的排后面。"""
    d = parse_dt(item.get("deadline_dt") or item.get("deadline") or "")
    return (d is None, d or dt.datetime.max)


def load_state(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("sent", [])
            return data
        return {"sent": []}
    except Exception:
        return {"sent": []}


def save_state(path: Path, st):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def run_task(task, items, state_dir: Path, force=False):
    """对采集到的 items 应用规则，并做 per-task 去重。

    关键顺序：先规则筛选 → 排序 → 去重 → 最后才按 limit 截断。
    （旧实现先截断，导致真实数据下新项被老项挤掉，定时推送永远为空。）
    """
    rule = task.get("rule", {})
    matched, excluded = [], []
    for it in items or []:
        ok, reasons = evaluate(it, rule)
        if ok:
            matched.append(it)
        else:
            excluded.append({
                "title": it.get("title"),
                "purchaser": it.get("purchaser"),
                "reasons": reasons,
            })

    # 按截止时间排序，让“快到期”的排前面，预览更有决策价值
    matched.sort(key=_deadline_key)

    st = load_state(state_dir / f"{_safe_task_id(task['id'])}.json")
    sent = set(st.get("sent", []))
    fresh_all = [m for m in matched if m.get("id") not in sent]
    if force:
        fresh_all = list(matched)

    limit = max(1, int(task.get("limit", 8) or 8))
    matched_limited = matched[:limit]
    fresh_limited = fresh_all[:limit]

    return {
        "matched": matched_limited,
        "fresh": fresh_limited,
        "excluded": excluded,
        "sent_count": len(sent),
        "new_count": len(fresh_limited),
        "matched_total": len(matched),
    }


def _safe_task_id(task_id):
    """任务 id 用于拼状态文件名，必须与 server 侧校验保持一致，防路径穿越。"""
    tid = str(task_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tid):
        raise ValueError(f"非法任务 id: {tid!r}")
    return tid


def commit_sent(task, items, state_dir: Path):
    """推送成功后写入去重状态。"""
    path = state_dir / f"{_safe_task_id(task['id'])}.json"
    st = load_state(path)
    ids = st.get("sent", []) + [x.get("id") for x in items if x.get("id")]
    st["sent"] = list(dict.fromkeys(ids))[-1000:]
    st.setdefault("runs", []).append(
        {"at": dt.datetime.now().isoformat(timespec="seconds"), "sent": len(items)}
    )
    st["runs"] = st["runs"][-90:]
    save_state(path, st)


def clear_state(task_id, state_dir: Path):
    """删除任务时清理其去重状态，避免残留文件。"""
    path = state_dir / f"{_safe_task_id(task_id)}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
