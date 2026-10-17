#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日冷知识推送 —— 从「精选题库」挑 1 条，推送到微信（PushPlus）。

设计要点
--------
1. 只用 Python 标准库，不需要 pip install。
2. 内容全部来自《冷知识_题库.md》，**不靠 AI 现编**，保证准确、可核实、不编造。
3. 查重规则：
   - 标题不重复（永久）
   - 当天两条领域不重复
   - 连续 3 天不重复同一领域
   这些规则靠《冷知识_已推送清单.md》实现：每次推送成功后追加一行，
   由 GitHub workflow 提交回仓库，跨天、跨会话都不丢。
4. 挑选是确定性的（最久未用领域优先、同领域按题库顺序），便于复现与测试。

用法
----
    python cold_knowledge.py              # 正常推送（并追加清单）
    python cold_knowledge.py --dry-run    # 只打印，不推送、不记清单

密钥：环境变量 PUSHPLUS_TOKEN（与早报共用同一个 secret）。
"""

import os
import re
import sys
import json
import urllib.request
from datetime import datetime, timezone, timedelta, date

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
BANK_PATH = os.path.join(HERE, "冷知识_题库.md")
LEDGER_PATH = os.path.join(HERE, "冷知识_已推送清单.md")

DOMAINS = [
    "自然科学", "历史", "地理", "生物", "心理", "语言", "文化",
    "科技", "艺术", "经济", "法律", "生活常识", "天文", "医学健康", "考古",
]

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ---------------------------------------------------------------- 读题库 / 读清单

def load_bank():
    """解析《冷知识_题库.md》，返回 [{领域,标题,正文,为什么,来源,小问题}, ...]"""
    entries, cur = [], {}
    try:
        txt = open(BANK_PATH, encoding="utf-8").read()
    except OSError:
        return entries
    for line in txt.splitlines():
        if line.strip() == "---":
            if cur.get("领域") and cur.get("标题") and cur.get("正文"):
                entries.append(cur)
            cur = {}
            continue
        m = re.match(r"^([^：:]+)[：:](.*)$", line)
        if m:
            key, val = m.group(1).strip(), m.group(2).strip()
            cur[key] = cur.get(key) + "\n" + val if key in cur else val
    if cur.get("领域") and cur.get("标题") and cur.get("正文"):
        entries.append(cur)
    return entries


def load_ledger():
    """解析《冷知识_已推送清单.md》的表格，返回 [{date,time,domain,title}, ...]"""
    rows = []
    if not os.path.exists(LEDGER_PATH):
        return rows
    for line in open(LEDGER_PATH, encoding="utf-8").read().splitlines():
        m = re.match(
            r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(\S+?)\s*\|\s*(\S+?)\s*\|\s*(.+?)\s*\|$",
            line,
        )
        if m:
            rows.append({
                "date": m.group(1),
                "time": m.group(2),
                "domain": m.group(3),
                "title": m.group(4),
            })
    return rows


def append_ledger(entry, now):
    """推送成功后，往清单末尾追加一行。"""
    row = "| {} | {} | {} | {} |".format(
        now.strftime("%Y-%m-%d"), now.strftime("%H:%M"),
        entry["领域"], entry["标题"],
    )
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:
        f.write(row + "\n")


# ---------------------------------------------------------------- 挑选逻辑

def pick(entries, ledger):
    """按规则挑 1 条：标题不重复 + 当天领域不重复 + 连续 3 天不重复领域。"""
    if not entries:
        return None

    today = date.today()
    today_s = today.isoformat()
    used_titles = {r["title"] for r in ledger}
    used_today = {r["domain"] for r in ledger if r["date"] == today_s}

    last = {}  # 每个领域最近一次使用的日期
    for r in ledger:
        d = r["domain"]
        if d not in last or r["date"] > last[d]:
            last[d] = r["date"]

    cut = (today - timedelta(days=2)).isoformat()

    def domain_ok(d):
        if d in used_today:
            return False
        if d in last and last[d] >= cut:
            return False
        return True

    # 候选领域：最久未用优先（轮换均匀）
    order = sorted(DOMAINS, key=lambda d: (last.get(d, "0000-00-00"), DOMAINS.index(d)))

    # 第一遍：严格按规则
    for d in order:
        if not domain_ok(d):
            continue
        for e in entries:
            if e["领域"] == d and e["标题"] not in used_titles:
                return e

    # 第二遍：放宽到「只要当天没重复」就挑（防早期题库不足导致死锁）
    for d in order:
        if d in used_today:
            continue
        for e in entries:
            if e["领域"] == d and e["标题"] not in used_titles:
                return e

    # 最后兜底：随便挑一个没用过的标题（标题不重复是底线）
    for e in entries:
        if e["标题"] not in used_titles:
            return e

    return None


# ---------------------------------------------------------------- 组装 & 推送

def build_message(e):
    return (
        "{}｜{}\n\n{}\n\n"
        "补充：{}\n"
        "为什么有意思：{}\n"
        "来源：{}\n"
        "今日小问题：{}"
    ).format(
        e["领域"], e["标题"], e["正文"], e.get("补充", ""),
        e["为什么"], e["来源"], e["小问题"],
    )


def push(token, title, content):
    payload = json.dumps(
        {"token": token, "title": title, "content": content, "template": "markdown"},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://www.pushplus.plus/send",
        data=payload,
        headers={**UA, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read().decode("utf-8", "replace"))
    print(json.dumps(res, ensure_ascii=False))
    return res.get("code") == 200


def main():
    dry = "--dry-run" in sys.argv
    entries = load_bank()
    if not entries:
        print("[error] 题库为空或无法解析", file=sys.stderr)
        return 1

    ledger = load_ledger()
    e = pick(entries, ledger)
    if not e:
        print("[error] 题库已用完，没有可推送的条目（请补充题库）", file=sys.stderr)
        return 1

    now = datetime.now(CST)
    title = "❄️ 冷知识 · {}月{}日".format(now.month, now.day)
    content = build_message(e)
    print("[info] 选中：{}｜{}".format(e["领域"], e["标题"]))

    if dry:
        print("=" * 60)
        print("TITLE:", title)
        print("=" * 60)
        print(content)
        print("=" * 60)
        print("[dry-run] 共 {} 字，未推送，未记录".format(len(content)))
        return 0

    token = (os.environ.get("PUSHPLUS_TOKEN") or "").strip()
    if not token:
        print("[error] 没找到 PUSHPLUS_TOKEN", file=sys.stderr)
        return 1

    ok = push(token, title, content)
    if ok:
        append_ledger(e, now)
        print("[ok] 推送成功，已记入清单")
        return 0
    print("[fail] 推送失败", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
