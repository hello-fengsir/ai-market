#!/usr/bin/env python3
"""消息模板：把命中项渲染成各渠道通用的 markdown 文本。"""
import datetime as dt
import re

# 视为「未披露联系方式」的占位值
UNDISCLOSED = ("", "公告未披露", "未披露", "暂无", "无", "—", "-", "暂无联系方式", "详见公告")


def _disclosed(val):
    return bool(val) and str(val).strip() not in UNDISCLOSED


def _unique(values):
    result = []
    for value in values:
        value = re.sub(r"\s+", " ", str(value or "")).strip(" ，,；;、")
        if value and value not in result:
            result.append(value)
    return result


def _format_contact(value):
    """把官网抓取的扁平联系信息整理为地址、联系人、电话和邮箱。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not _disclosed(text):
        return []

    # 清除网页跳转提示和公告尾部等采集噪声。
    text = re.split(r"您访问的链接|八、公告发布媒介|公告发布媒介", text, maxsplit=1)[0]
    address_match = re.search(
        r"(?:招标人地址|联系地址|地\s*址|地址)\s*[:：]\s*(.*?)"
        r"(?=\s*(?:联\s*系\s*人|联系人|联系方式|联系电话|电话|电\s*子\s*邮\s*件|电子邮件|邮箱)\s*[:：]|$)",
        text,
    )
    address = address_match.group(1).strip(" ，,；;") if address_match else ""

    name_match = re.search(
        r"(?:获取招标文件联系人|联\s*系\s*人|联系人)\s*[:：]\s*(.*?)"
        r"(?=\s*(?:联系电话|联系方式|电话|电\s*子\s*邮\s*件|电子邮件|邮箱)\s*[:：]|$)",
        text,
    )
    names = []
    if name_match:
        names = _unique(re.split(r"[、,，/；;]+", name_match.group(1)))

    phone_pattern = r"(?<!\d)(?:1[3-9]\d{9}|(?:\+?86[- ]?)?(?:0\d{2,3}[- ]?)?\d{7,8}(?:[-*转]\d{1,5})?)(?!\d)"
    phones = _unique(re.findall(phone_pattern, text))
    emails = _unique(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text))

    # 部分政府采购公告使用“联系方式：姓名 电话”，没有单独的联系人标签。
    if not names:
        generic = re.search(r"联系方式\s*[:：]\s*([^，,；;]+)", text)
        if generic:
            candidate = generic.group(1)
            candidate = re.sub(phone_pattern, "", candidate)
            names = _unique(re.split(r"[、/]+", candidate))
        elif len(text) <= 40 and phones:
            # 简短的“张老师 138...”格式。
            candidate = re.sub(phone_pattern, "", text)
            names = _unique([candidate])

    rows = []
    if address:
        rows.append(f"地址：{address}")
    if names:
        for index, name in enumerate(names, 1):
            details = []
            if index <= len(phones):
                details.append(phones[index - 1])
            if index <= len(emails):
                details.append(emails[index - 1])
            rows.append(f"联系人{index}：{name}" + (f"  {'  '.join(details)}" if details else ""))
        for phone in phones[len(names):]:
            rows.append(f"联系电话：{phone}")
        for email in emails[len(names):]:
            rows.append(f"电子邮箱：{email}")
    else:
        rows.extend(f"联系电话：{phone}" for phone in phones)
        rows.extend(f"电子邮箱：{email}" for email in emails)

    # 无法结构化时保留干净原文，避免丢失有效联系方式。
    return rows or [f"联系信息：{text.strip(' ，,；;')}"]


def _append_contact(lines, heading, value):
    rows = _format_contact(value)
    if not rows:
        return
    lines.append(f"- **{heading}：**")
    lines.extend(f"  - {row}" for row in rows)


def render_tender(items, title="标讯命中预览"):
    today = dt.datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# {title}",
        "",
        f"> **日期：** {today}　　**命中：** {len(items)} 项",
        "",
        "---",
        "",
    ]
    for i, x in enumerate(items, 1):
        lines += [
            f"## {i:02d}｜{x.get('title', '')}",
            "",
            f"- **采购人：** {x.get('purchaser', '')}",
            f"- **项目编号：** `{x.get('project_no', '')}`",
            f"- **预算 / 最高限价：** {x.get('budget', '')}",
            f"- **投标 / 响应截止：** **{x.get('deadline', '')}**",
            f"- **代理机构：** {x.get('agent', '')}",
        ]
        _append_contact(lines, "采购人联系方式", x.get("purchaser_contact", ""))
        _append_contact(lines, "代理联系方式", x.get("agent_contact", ""))
        lines += [
            "",
            f"> {x.get('summary', '')}",
            "",
            f"[查看公告 →]({x.get('url', '')})",
            "",
            "---",
            "",
        ]
    if not items:
        lines += ["> 当前规则下没有命中项。", ""]
    return "\n".join(lines)


TITLE_DEFAULT = "标讯命中预览"
