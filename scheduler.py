#!/usr/bin/env python3
"""极简 cron 调度器：解析 5 字段表达式，按任务的 schedule 触发自动推送。

支持的写法（足够覆盖本系统任务，如 "0 9 * * 1-5"）：
  *       任意
  */n     步长（如 */15 表示每 15）
  a-b     区间
  a-b/n   区间内步长
  逗号列表 / 单值
说明：cron 的 dow 0/7=周日；Python weekday() 周一=0..周日=6，内部已归一。
经典规则：dom 与 dow 同时受限时取“或”，否则各自为“与”。

关键设计（历史 bug 修复，勿回退）：
  1. 同一分钟只触发一次。线程间隔 60s，但两次检查可能落在同一分钟内
     （如 09:00:05 与 09:00:55），旧实现会重复推送两次。
  2. cron 表达式非法时只跳过该任务，不能让异常中断整轮循环，
     否则一个写错的任务会把其他所有任务一起停掉。
  3. 停止用 Event 而非裸 sleep，能立即退出，避免 stop 后残留线程。
"""
import datetime as dt
import threading


class CronError(ValueError):
    pass


def _parse_field(field, lo, hi, name):
    """解析单个 cron 字段，返回 None（任意）或 set[int]。"""
    field = (field or "").strip()
    if field == "*":
        return None
    out = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"{name} 字段为空")
        step = 1
        rng = part
        if "/" in part:
            rng, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                raise CronError(f"{name} 步长非法: {part}")
            if step <= 0:
                raise CronError(f"{name} 步长必须为正: {part}")
        if rng == "*":
            vals = range(lo, hi + 1)
        elif "-" in rng.lstrip("-"):
            a_s, b_s = rng.split("-", 1)
            try:
                a, b = int(a_s), int(b_s)
            except ValueError:
                raise CronError(f"{name} 区间非法: {part}")
            if a > b:
                raise CronError(f"{name} 区间起点大于终点: {part}")
            vals = range(a, b + 1)
        else:
            try:
                vals = [int(rng)]
            except ValueError:
                raise CronError(f"{name} 取值非法: {part}")
        for v in vals:
            if not (lo <= v <= hi):
                raise CronError(f"{name} 超出范围 {lo}-{hi}: {v}")
            if (v - lo) % step == 0:
                out.add(v)
    if not out:
        raise CronError(f"{name} 未解析出有效值")
    return out


def validate(expr):
    """校验表达式；非法抛 CronError。供接口保存前校验。"""
    parts = [p for p in (expr or "").strip().split() if p]
    if len(parts) != 5:
        raise CronError("cron 必须是 5 个字段：分 时 日 月 周")
    _parse_field(parts[0], 0, 59, "分")
    _parse_field(parts[1], 0, 23, "时")
    _parse_field(parts[2], 1, 31, "日")
    _parse_field(parts[3], 1, 12, "月")
    _parse_field(parts[4], 0, 7, "周")
    return True


def cron_matches(expr, now=None):
    now = now or dt.datetime.now()
    parts = [p for p in (expr or "").strip().split() if p]
    if len(parts) != 5:
        return False
    try:
        minute = _parse_field(parts[0], 0, 59, "分")
        hour = _parse_field(parts[1], 0, 23, "时")
        dom = _parse_field(parts[2], 1, 31, "日")
        month = _parse_field(parts[3], 1, 12, "月")
        dow = _parse_field(parts[4], 0, 7, "周")
    except CronError:
        return False
    # cron dow: 0/7=周日, 1=周一 … 6=周六 → Python weekday: 周一=0 … 周日=6
    if dow is not None:
        dow = {((d - 1) % 7) for d in dow}

    m, h, d, mo, wd = now.minute, now.hour, now.day, now.month, now.weekday()
    if minute is not None and m not in minute:
        return False
    if hour is not None and h not in hour:
        return False
    if month is not None and mo not in month:
        return False
    dom_ok = dom is None or d in dom
    dow_ok = dow is None or wd in dow
    if dom is not None and dow is not None:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


class Scheduler:
    """后台线程：每隔 interval 秒检查一次，命中 schedule 的任务触发 trigger(task_id)。"""

    def __init__(self, get_tasks, trigger, interval=30):
        self.get_tasks = get_tasks
        self.trigger = trigger
        self.interval = interval
        self._running = False
        self._thread = None
        self._stop = threading.Event()
        self._fired = {}       # task_id -> "YYYY-MM-DD HH:MM"，保证同分钟只触发一次
        self._last_run = {}    # task_id -> ISO 时间，供前端展示
        self._last_result = {} # task_id -> 最近一次触发结果
        self._last_check = None # 调度线程最近一次完成扫描的时间

    def _fire_once(self, task_id, now):
        """同一任务同一分钟只触发一次。"""
        key = now.strftime("%Y-%m-%d %H:%M")
        if self._fired.get(task_id) == key:
            return False
        self._fired[task_id] = key
        # 避免字典无限增长
        if len(self._fired) > 500:
            self._fired = {k: v for k, v in self._fired.items() if v == key}
        return True

    def _loop(self):
        while not self._stop.is_set():
            try:
                now = dt.datetime.now()
                for t in self.get_tasks():
                    # 任务禁用或单独关闭定时，则跳过
                    if t.get("enabled", True) is False or t.get("auto_enabled", True) is False:
                        continue
                    sched = (t.get("schedule") or "").strip()
                    if not sched:
                        continue
                    try:
                        hit = cron_matches(sched, now)
                    except Exception as e:  # 单个任务表达式异常不影响其他任务
                        print(f"scheduler: 任务 {t.get('id')} cron 解析失败: {e}")
                        continue
                    if not hit:
                        continue
                    tid = t.get("id")
                    if not tid or not self._fire_once(tid, now):
                        continue
                    try:
                        result = self.trigger(tid)
                        finished = dt.datetime.now().isoformat(timespec="seconds")
                        self._last_run[tid] = finished
                        self._last_result[tid] = result if isinstance(result, dict) else {
                            "ok": True, "status": "completed"
                        }
                    except Exception as e:  # noqa: BLE001
                        finished = dt.datetime.now().isoformat(timespec="seconds")
                        self._last_run[tid] = finished
                        self._last_result[tid] = {"ok": False, "status": "error", "error": str(e)[:300]}
                        print(f"scheduler: 任务 {tid} 触发失败: {e}")
                self._last_check = dt.datetime.now().isoformat(timespec="seconds")
            except Exception as e:  # noqa: BLE001
                self._last_check = dt.datetime.now().isoformat(timespec="seconds")
                print("scheduler loop error:", e)
            self._stop.wait(self.interval)

    def start(self):
        if self._running:
            return
        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="tender-scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self._stop.set()
        th = self._thread
        if th and th.is_alive():
            th.join(timeout=2)
        self._thread = None

    @property
    def running(self):
        return self._running

    def status(self):
        """返回可观测状态，区分“线程在扫描”和“任务有实际推送”。"""
        return {
            "running": self._running,
            "last_check": self._last_check,
            "last_run": dict(self._last_run),
            "last_result": dict(self._last_result),
        }

    def last_run(self):
        """兼容旧接口：最近一次触发时间。"""
        return dict(self._last_run)
