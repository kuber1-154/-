#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日科技政经早报 —— 抓新闻 + DeepSeek 写稿 + 推送到微信（PushPlus）

设计要点
--------
1. 只用 Python 标准库，不需要 pip install 任何东西。
2. 新闻源全部免费、无需 Key：Google 新闻总榜(最稳) + Bing 新闻 RSS + Google 搜索 RSS + BBC 中文 RSS。
   每个源独立 try，挂了只跳过它，不影响整体。
3. 时效防线：只保留最近 48 小时发布的条目，防止某个源吐旧新闻污染早报。
4. DeepSeek 写稿失败（网络/欠费/限流）时，自动降级为「新闻列表摘要版」，
   保证每天 8:00 手机一定收到东西。
5. 永不静默断更：采集到 0 条会自动重试；仍失败则给你推一条「降级通知」；
   写稿、推送等任何异常都会兜底推微信告警，绝不让你一声不响收不到信息。

用法
----
    python briefing.py              # 正常推送
    python briefing.py --dry-run    # 只打印内容，不推送（调试用）

密钥来源（GitHub Actions 环境变量）：
    PUSHPLUS_TOKEN   微信推送 token（Secrets，已有）
    DEEPSEEK_API_KEY DeepSeek 平台 API Key（Secrets，需新建）
"""

import os
import re
import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

# Windows 控制台默认 GBK，强制切 UTF-8（GitHub Actions 上本来就是 UTF-8，兜底而已）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CST = timezone(timedelta(hours=8))
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


# ---------------------------------------------------------------- 网络基础

def http_get(url, timeout=30, retries=2):
    """GET 一个 URL，返回解码后的文本。失败自动退避重试。"""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as exc:
            last = exc
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
                continue
            raise
    raise last


def http_post_json(url, payload, headers, timeout=180):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def strip_tags(s):
    s = re.sub(r"(?s)<[^>]+>", "", s or "")
    s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = s.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- 新闻抓取

def _child(el, *names):
    for c in el:
        if c.tag.split("}")[-1].lower() in names:
            return c
    return None


def _item_date(el):
    """取 item 的 pubDate / published / updated，返回 UTC datetime 或 None。"""
    d = _child(el, "pubdate", "published", "updated", "date")
    if d is None or not d.text:
        return None
    try:
        return parsedate_to_datetime(d.text.strip())
    except Exception:
        return None


def clean_link(url):
    """Bing 新闻 RSS 的链接是 apiclick.aspx 跳转链，里面包着真实地址，解出来变直链。"""
    try:
        p = urllib.parse.urlparse(url)
        if "bing.com" in (p.netloc or "") and "apiclick" in (p.path or ""):
            real = (urllib.parse.parse_qs(p.query).get("url") or [""])[0]
            if real.startswith("http"):
                return real
    except Exception:
        pass
    return url


def parse_feed(xml_text, max_age_h=48):
    """把 RSS/Atom 解析成 [{title, link, desc}]，只保留最近 max_age_h 小时。"""
    out = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out

    now = datetime.now(timezone.utc)
    for el in root.iter():
        tag = el.tag.split("}")[-1].lower()
        if tag not in ("item", "entry"):
            continue

        dt = _item_date(el)
        if dt is not None:
            try:
                if (now - dt).total_seconds() > max_age_h * 3600:
                    continue
            except TypeError:
                # 有些源的 pubDate 不带时区，解析成 naive datetime，与 UTC 相减会报错；
                # 视为不算过期、直接保留，而不是把整条丢弃。
                pass

        t = _child(el, "title")
        l = _child(el, "link")
        d = _child(el, "description", "summary")
        title = strip_tags(t.text) if t is not None and t.text else ""
        if l is None:
            continue
        link = clean_link((l.get("href") or l.text or "").strip())
        if not title or not link.startswith("http"):
            continue
        desc = strip_tags(d.text) if d is not None and d.text else ""
        out.append({"title": title, "link": link, "desc": desc[:220]})
    return out


def fetch_bing(query):
    url = "https://www.bing.com/news/search?q={}&format=rss".format(
        urllib.parse.quote(query)
    )
    return parse_feed(http_get(url, retries=2))


def fetch_google(query):
    url = (
        "https://news.google.com/rss/search?q={}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    ).format(urllib.parse.quote(query))
    return parse_feed(http_get(url, retries=2))


def fetch_google_top():
    """Google 新闻中文总榜（不带关键词），比搜索 RSS 更稳定，作为兜底源。"""
    url = "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    return parse_feed(http_get(url, retries=3))


def fetch_bbc():
    url = "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml"
    return parse_feed(http_get(url, retries=2))


# 每个源都是「喂一个参数」，独立 try，坏了只跳它。
# 第一个放最稳的 Google 总榜，保证即使其它源全挂也有兜底。
SOURCES = [
    ("google_top", ""),
    ("bing", "人工智能 大模型 半导体 芯片"),
    ("bing", "中东 俄乌 国际政治"),
    ("bing", "央行 利率 通胀 宏观经济"),
    ("bing", "A股 港股 美股 股市"),
    ("bing", "原油 黄金 大宗商品"),
    ("bing", "新能源汽车 锂电池 光伏 机器人"),
    ("bing", "公司财报 业绩 营收"),
    ("bing", "中美 关税 贸易 汇率"),
    ("google", "AI 芯片 科技"),
    ("google", "美联储 加息 降息"),
    ("bbc", ""),
]


def collect_news():
    items, seen = [], set()

    def add(rows, src):
        for it in rows:
            key = re.sub(r"\s+", "", it["title"])[:40]
            if key and key not in seen:
                seen.add(key)
                items.append({"title": it["title"], "link": it["link"],
                              "desc": it["desc"], "src": src})

    for kind, arg in SOURCES:
        try:
            if kind == "bing":
                add(fetch_bing(arg), "Bing新闻")
            elif kind == "google":
                add(fetch_google(arg), "Google新闻")
            elif kind == "google_top":
                add(fetch_google_top(), "Google新闻")
            else:
                add(fetch_bbc(), "BBC中文")
        except Exception as exc:
            print("[warn] 源 {}/{} 失败: {}".format(kind, arg, exc), file=sys.stderr)
    return items


# ---------------------------------------------------------------- DeepSeek 写稿

SYSTEM_PROMPT = (
    "你是「每日科技政治经济情报官」，负责把新闻素材整理成手机早报。"
    "你的输出会直接推送给用户，要求中立、严谨、简洁、不标题党。"
)


def build_user_prompt(items, now):
    lines = []
    for it in items[:90]:
        line = "- 标题：{}".format(it["title"])
        if it["src"]:
            line += "（来源：{}）".format(it["src"])
        lines.append(line)
        if it["desc"]:
            lines.append("  摘要：" + it["desc"])
        lines.append("  链接：" + it["link"])

    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
    return (
        "今天是北京时间{date}（{weekday}）。以下是最近 24 小时左右的新闻素材"
        "（标题+摘要+链接），请据此写一份早报，严格遵守：\n"
        "1. 八段结构（用 Markdown 二级标题）：\n"
        "   一、今日必看（3-5 条：事件 + 为什么重要 + 后续看点）\n"
        "   二、科技线\n"
        "   三、政治线\n"
        "   四、经济线（宏观 / 央行 / 市场 / 贸易 / 公司财报）\n"
        "   五、未来大事日历（7 天 / 30 天，分科技、政治、经济）\n"
        "   六、主线串联（今天发生了什么 → 下一步可能发生什么 → 影响谁，"
        "体现科技-政治-经济联动）\n"
        "   七、关注信号与概率（高 / 中 / 低，各说明依据）\n"
        "   八、来源链接（**最多只列 12 条**，挑今日必看/主线里最关键的，"
        "不要把素材里的链接全部罗列，否则正文超长会被推送平台拒发）\n"
        "2. 只使用素材里出现的事实与数字，重要条目和关键数据必须附素材里的链接；"
        "素材里没有的一律不写，禁止编造新闻、数字、日期、公司名。\n"
        "3. 区分【事实】与【推断/预测】：媒体推测、消息人士、知情人士内容必须"
        "明确标注为推断。\n"
        "4. 日历只写：素材中已确认的日程；或你训练知识中高度确定的例行日程"
        "（如中国 LPR 每月 20 日公布、美国非农每月首个周五、美联储议息按官方日历）；"
        "日期不确定的一律写「待定」或省略，禁止编造日期。\n"
        "5. 风格：中立、简洁、不标题党；总字数严格控制在 2800-3200 字（含链接）；素材极少时相应缩短"
        "并说明；确实没有大事时直说「今天无重大更新」。\n"
        "6. 输出纯 Markdown 正文，不要用代码块包裹，不要写任何解释性前言。\n\n"
        "新闻素材：\n{items}"
    ).format(
        date="{}年{}月{}日".format(now.year, now.month, now.day),
        weekday=weekday,
        items="\n".join(lines),
    )


def call_deepseek(api_key, prompt):
    url = "https://api.deepseek.com/chat/completions"
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 5000,
        "stream": False,
    }
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + api_key}
    headers.update(UA)

    for i in range(3):
        try:
            res = http_post_json(url, payload, headers)
            return res["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            print("[warn] DeepSeek HTTP {}: {}".format(exc.code, body), file=sys.stderr)
        except Exception as exc:
            print("[warn] DeepSeek 调用失败: {}".format(exc), file=sys.stderr)
        if i < 2:
            time.sleep(20 * (i + 1))
    return None


def fallback_digest(items, now):
    L = [
        "# 📰 科技政经早报 · {}月{}日（自动摘要版）".format(now.month, now.day),
        "",
        "> 写稿模型暂时不可用，先给今天的要闻列表；明天会自动恢复正常。",
        "",
    ]
    for it in items[:18]:
        L.append("- [{}]({})".format(it["title"], it["link"]))
    return "\n".join(L)


# ---------------------------------------------------------------- 推送

def push(token, title, content):
    """推送到 PushPlus，返回是否成功（True/False）。任何异常都吃掉并返回 False。"""
    payload = json.dumps(
        {"token": token, "title": title, "content": content, "template": "markdown"},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://www.pushplus.plus/send",
        data=payload,
        headers={**UA, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:
        print("[error] 推送请求失败: {}".format(exc), file=sys.stderr)
        return False
    print(json.dumps(res, ensure_ascii=False))
    return res.get("code") == 200


def _alert(token, title, content):
    """尽力推一条告警消息，绝不抛异常（兜底用）。"""
    token = (token or "").strip()
    if not token:
        return False
    try:
        return push(token, title, content)
    except Exception:
        return False


# ---------------------------------------------------------------- 主流程

def _run(dry, now):
    print("[info] 开始收集新闻…")
    items = collect_news()
    print("[info] 共收集 {} 条新闻".format(len(items)))

    if not items:
        print("[warn] 第一次采集到 0 条，30 秒后重试一次", file=sys.stderr)
        time.sleep(30)
        items = collect_news()
        print("[info] 重试后共 {} 条新闻".format(len(items)))

    if not items:
        msg = (
            "📰 早报降级通知\n\n"
            "⚠️ 今天的新闻源采集失败（重试后仍为 0 条），早报未能生成。\n"
            "大概率是新闻 RSS 对机房 IP 限流，一般明天会自动恢复；\n"
            "也可现在去 GitHub Actions 手动 Rerun 一次。"
        )
        if dry:
            print(msg)
            return 0
        ok = _alert(os.environ.get("PUSHPLUS_TOKEN", ""), "📰 早报降级通知", msg)
        print("[info] 已推送降级通知" if ok else "[fail] 降级通知也推送失败")
        return 0 if ok else 1

    title = "📰 科技政经早报 · {}月{}日".format(now.month, now.day)
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    content = None

    if api_key:
        print("[info] 调用 DeepSeek 写稿…")
        content = call_deepseek(api_key, build_user_prompt(items, now))

    if not content:
        print("[info] 写稿失败，使用摘要版降级")
        content = fallback_digest(items, now)
        title += "（摘要版）"

    # PushPlus 单条正文上限 2 万字，超出会被服务端拒发（code 999「发送内容过大」）。
    # 这里做硬截断兜底，保证任何情况下都能推出去。
    MAX_CONTENT = 18000
    if len(content) > MAX_CONTENT:
        head = content[:MAX_CONTENT]
        cut = head.rfind("\n")
        if cut > MAX_CONTENT * 0.8:
            head = head[:cut]
        content = head.rstrip() + "\n\n…（正文过长，已截断；要点以上方为准）"

    if dry:
        print("=" * 60)
        print("TITLE:", title)
        print("=" * 60)
        print(content)
        print("=" * 60)
        print("[dry-run] 共 {} 字，未推送".format(len(content)))
        return 0

    token = (os.environ.get("PUSHPLUS_TOKEN") or "").strip()
    if not token:
        print("[error] 没找到 PUSHPLUS_TOKEN", file=sys.stderr)
        return 1

    ok = push(token, title, content)
    print("[ok] 推送成功" if ok else "[fail] 推送失败")
    return 0 if ok else 1


def main():
    dry = "--dry-run" in sys.argv
    now = datetime.now(CST)

    # 兜底：任何没被内部吃掉的异常，都推一条微信告警，避免「一声不响」断更。
    try:
        return _run(dry, now)
    except Exception as exc:
        print("[fatal] {}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        if not dry:
            _alert(
                os.environ.get("PUSHPLUS_TOKEN", ""),
                "📰 早报脚本异常",
                "⚠️ 早报脚本运行崩溃：{}\n请查看 GitHub Actions 该次运行日志。".format(exc),
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())