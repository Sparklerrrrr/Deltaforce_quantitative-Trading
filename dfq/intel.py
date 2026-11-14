"""资讯与观点：采集 → 入库 → 提炼成可打分的观点。

信源及现状：
- 手动粘贴（B 站视频文字稿、QQ 群消息、任何你想喂给系统的文字）：已实现，推荐。
    方式一：把 .txt/.md 文件放进 manual_notes/ 目录
    方式二：python -m dfq note add --source bili --name moligod --title "标题" --file 文稿.txt
- 新闻搜索（Tavily，可替换）：已实现，默认关闭，需要 DFQ_TAVILY_API_KEY。
- B 站自动获取：**没有实现**。api.bilibili.com 的 robots.txt 对所有客户端 Disallow: /，
  获取 UP 主投稿列表还需要伪造浏览器签名（WBI），不合规，所以只保留手动入口。
- 小涛查的“低价预测”：采集时已入库（xtc_hints），这里转成观点，和别的信源一样打分。

所有信源一视同仁地打分——包括既是数据站站长又是主播的 moligod。
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from . import llm as L
from . import timeutil
from .http import HttpError, request_json
from .ledger import _norm
from .log import event, get_logger

log = get_logger("intel")

SOURCE_LABELS = {"bili": "B站UP主", "qq": "QQ群", "news": "新闻", "platform": "数据平台", "other": "其他"}

_BULL = ("看涨", "要涨", "会涨", "上涨", "涨价", "抄底", "可以买", "建议买", "买入", "囤", "低估", "反弹", "回升", "起飞")
_BEAR = ("看跌", "要跌", "会跌", "下跌", "跌价", "降价", "别买", "不要买", "卖出", "出货", "抛", "高估", "回调", "削弱", "砍")
_NEG = ("不", "别", "没", "未必", "不会", "难")


def _fp(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def add_document(db, source_type: str, source_name: str, text: str, *, title: str | None = None,
                 url: str | None = None, published_at: str | None = None) -> int | None:
    """入库一篇原文。内容重复（同信源同文本）返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    fp = _fp(source_type, source_name, text[:5000])
    if db.one("SELECT id FROM documents WHERE fingerprint=?", (fp,)):
        return None
    cur = db.execute(
        "INSERT INTO documents(source_type,source_name,published_at,collected_at,url,title,text,fingerprint)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (source_type, source_name, published_at, timeutil.fmt(timeutil.now()), url, title,
         text[:60000], fp))
    return cur.lastrowid


_HEADER = re.compile(r"^\s*(source|来源|name|信源|作者|up|title|标题|date|日期|url|链接)\s*[:：]\s*(.+?)\s*$", re.I)
_KEYMAP = {"来源": "source", "信源": "name", "作者": "name", "up": "name", "标题": "title",
           "日期": "date", "链接": "url"}


def parse_note_file(path: Path) -> dict:
    """解析手动粘贴的文稿。文件开头可以写几行元信息（都可省略）：

        source: bili          # bili / qq / news / other
        name: moligod         # UP 主名、群名……
        title: 本周子弹怎么倒
        date: 2026-11-12

        （空一行后是正文）

    不写元信息时，从文件名猜：  bili_moligod_本周子弹.txt  →  source=bili, name=moligod
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    meta: dict[str, str] = {}
    lines = raw.splitlines()
    i = 0
    while i < len(lines) and i < 8:
        m = _HEADER.match(lines[i])
        if not m:
            break
        key = m.group(1).lower()
        meta[_KEYMAP.get(key, key)] = m.group(2)
        i += 1
    body = "\n".join(lines[i:]).strip() if meta else raw.strip()
    parts = path.stem.split("_", 2)
    if "source" not in meta and parts[0].lower() in SOURCE_LABELS:
        meta["source"] = parts[0].lower()
        if len(parts) > 1 and "name" not in meta:
            meta["name"] = parts[1]
        if len(parts) > 2 and "title" not in meta:
            meta["title"] = parts[2]
    src = meta.get("source", "other").lower()
    return {"source_type": src if src in SOURCE_LABELS else "other",
            "source_name": meta.get("name") or "未署名",
            "title": meta.get("title") or path.stem, "published_at": meta.get("date"),
            "url": meta.get("url"), "text": body}


def ingest_manual_dir(cfg, db) -> int:
    d = Path(cfg.get("intel.manual_dir", "manual_notes"))
    if not d.is_absolute() and cfg.path is not None:
        d = cfg.path.parent / d
    if not d.exists():
        return 0
    n = 0
    for p in sorted(d.glob("*")):
        if p.suffix.lower() not in (".txt", ".md") or p.name.lower() == "readme.md":
            continue
        try:
            note = parse_note_file(p)
        except OSError as e:
            event(log, logging.WARNING, f"读取文稿失败 {p.name}: {e}")
            continue
        if add_document(db, note["source_type"], note["source_name"], note["text"], title=note["title"],
                        url=note["url"], published_at=note["published_at"]):
            n += 1
    return n


def fetch_news(cfg, db, api_key: str | None, transport=None) -> int:
    """新闻搜索（Tavily）。未用真实 key 实测；接口不通只会跳过，不影响主流程。"""
    if not cfg.get("intel.news.enabled"):
        return 0
    provider = cfg.get("intel.news.provider", "tavily")
    if provider != "tavily":
        raise ValueError(f"未实现的新闻搜索提供方: {provider}（可在 intel.py 的 fetch_news 里扩展）")
    if not api_key:
        raise ValueError("已开启新闻搜索，但未设置环境变量 DFQ_TAVILY_API_KEY")
    n = 0
    for q in cfg.get("intel.news.queries") or []:
        try:
            resp = request_json(
                "POST", "https://api.tavily.com/search",
                json_body={"query": q, "max_results": int(cfg.get("intel.news.max_results", 5)),
                           "topic": "news", "days": int(cfg.get("intel.news.days", 3))},
                headers={"Authorization": f"Bearer {api_key}"}, timeout=30, retries=1,
                transport=transport)
        except HttpError as e:
            event(log, logging.WARNING, f"新闻搜索失败: {e}", node="collect_intel")
            continue
        for r in (resp.get("results") or []) if isinstance(resp, dict) else []:
            if not isinstance(r, dict) or not r.get("content"):
                continue
            host = urlparse(str(r.get("url", ""))).netloc or "未知站点"
            if add_document(db, "news", host, str(r["content"]), title=str(r.get("title") or "")[:200],
                            url=str(r.get("url") or ""), published_at=str(r.get("published_date") or "")[:25] or None):
                n += 1
    return n


# ---------- 观点提取 ----------
def _alias_table(cfg, db) -> list[tuple[str, dict]]:
    table = []
    for w in cfg.watchlist:
        row = db.find_item(w["name"])
        item = {"name": w["name"], "oid": row["oid"] if row else w["oid"]}
        for k in {w["name"], *w["alias"], w["name"].split()[-1]}:
            nk = _norm(k)
            if len(nk) >= 2:
                table.append((nk, item))
    return sorted(table, key=lambda x: -len(x[0]))


def heuristic_opinions(text: str, aliases: list[tuple[str, dict]]) -> list[dict]:
    """不用模型时的兜底：逐句找“提到了某个品种 + 明确的涨/跌用词”。宁缺毋滥。"""
    out, seen = [], set()
    for sent in re.split(r"[。！？!?\n；;]+", text):
        ns = _norm(sent)
        if len(ns) < 4:
            continue
        item = next((it for key, it in aliases if key in ns), None)
        if item is None:
            continue
        bull = [w for w in _BULL if w in sent]
        bear = [w for w in _BEAR if w in sent]
        # “不会涨”“别抄底”这类否定句：直接跳过，交给模型处理
        if any(neg + w in sent for w in bull + bear for neg in _NEG):
            continue
        if bool(bull) == bool(bear):
            continue
        direction = "up" if bull else "down"
        if (item["name"], direction) in seen:
            continue
        seen.add((item["name"], direction))
        out.append({"item": item["name"], "oid": item["oid"], "direction": direction,
                    "claim": sent.strip()[:60]})
    return out


def process_documents(cfg, db, client: L.LLMClient | None = None, limit: int = 12) -> dict:
    """把还没提炼过的原文变成摘要 + 观点。模型不可用时退回关键词规则。"""
    aliases = _alias_table(cfg, db)
    names = {it["name"]: it for _, it in aliases}
    horizon = int(cfg.get("intel.opinion_horizon_days", 3))
    stats = {"docs": 0, "opinions": 0, "by_llm": 0}
    for doc in db.query("SELECT * FROM documents WHERE digest IS NULL ORDER BY id LIMIT ?", (limit,)):
        text = doc["text"] or ""
        digest, ops = None, None
        if client is not None and client.ready:
            try:
                obj = client.chat_json(
                    L.EXTRACT_PROMPT.format(items="\n".join(f"- {n}" for n in names)),
                    f"信源：{doc['source_name']}\n标题：{doc['title'] or ''}\n\n{text[:12000]}")
                digest = str(obj.get("digest") or "")[:400]
                ops = []
                for o in obj.get("opinions") or []:
                    if not isinstance(o, dict) or o.get("item") not in names:
                        continue
                    if o.get("direction") not in ("up", "down"):
                        continue
                    try:
                        h = min(max(int(o.get("horizon_days") or horizon), 1), 14)
                    except (TypeError, ValueError):
                        h = horizon
                    ops.append({"item": o["item"], "oid": names[o["item"]]["oid"],
                                "direction": o["direction"], "horizon_days": h,
                                "claim": str(o.get("claim") or "")[:80]})
                stats["by_llm"] += 1
            except L.LLMError as e:
                event(log, logging.WARNING, f"观点提取改用关键词规则: {e}", node="collect_intel")
        if ops is None:
            ops = heuristic_opinions(text, aliases)
            digest = re.sub(r"\s+", " ", text)[:200]
        db.execute("UPDATE documents SET digest=? WHERE id=?", (digest or "（无摘要）", doc["id"]))
        stats["docs"] += 1
        for o in ops:
            if o.get("oid") is None:
                continue
            snap = db.latest_snapshot(o["oid"])
            ref = snap["price"] if snap is not None else None
            fp = _fp("op", str(doc["id"]), o["item"], o["direction"])
            try:
                db.execute(
                    "INSERT INTO opinions(source_type,source_name,published_at,collected_at,url,title,"
                    "summary,oid,item_name,direction,horizon_days,ref_price,fingerprint) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (doc["source_type"], doc["source_name"], doc["published_at"],
                     timeutil.fmt(timeutil.now()), doc["url"], doc["title"], o.get("claim"),
                     o["oid"], o["item"], o["direction"], o.get("horizon_days", horizon), ref, fp))
                stats["opinions"] += 1
            except Exception:  # 指纹重复
                continue
    return stats


def hints_to_opinions(db, now: datetime | None = None) -> int:
    """把小涛查“今日低价预测”转成可打分的观点：预测今天的最低价约为 P。"""
    now = now or timeutil.now()
    n = 0
    since = timeutil.fmt(now - timedelta(hours=30))
    for h in db.query("SELECT * FROM xtc_hints WHERE kind='ammo_day' AND ts>=? AND price IS NOT NULL", (since,)):
        fp = _fp("hint", h["ts"][:10], str(h["oid"]))
        if db.one("SELECT id FROM opinions WHERE fingerprint=?", (fp,)):
            continue
        db.execute(
            "INSERT INTO opinions(source_type,source_name,collected_at,title,summary,oid,item_name,"
            "direction,horizon_days,ref_price,fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("platform", "小涛查·低价预测", h["ts"], "今日倒子弹低价预测",
             f"预测 {h['hour']} 点前后出现低价 {h['price']:.0f}", h["oid"], h["name"],
             "low", 1, h["price"], fp))
        n += 1
    return n


def recent_digests(db, days: int = 3, limit: int = 12) -> list[dict]:
    since = timeutil.fmt(timeutil.now() - timedelta(days=days))
    rows = db.query("SELECT * FROM documents WHERE collected_at>=? AND digest IS NOT NULL "
                    "ORDER BY id DESC LIMIT ?", (since, limit))
    return [{"source": f"{SOURCE_LABELS.get(r['source_type'], r['source_type'])}/{r['source_name']}",
             "title": r["title"], "date": r["published_at"] or r["collected_at"][:10],
             "digest": r["digest"]} for r in rows]


def recent_hints(db, hours: int = 30) -> list[dict]:
    since = timeutil.fmt(timeutil.now() - timedelta(hours=hours))
    rows = db.query("SELECT kind,name,hour,price FROM xtc_hints WHERE ts>=? ORDER BY id DESC LIMIT 40", (since,))
    return [dict(r) for r in rows]


def transcribe(path: str, model_size: str = "small") -> str:
    """本地语音转写（可选依赖 faster-whisper；未实测）。音频文件需由你自己提供。"""
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        raise RuntimeError("需要先安装：pip install faster-whisper") from None
    model = WhisperModel(model_size, compute_type="int8")
    segments, _ = model.transcribe(path, language="zh")
    return "\n".join(s.text.strip() for s in segments)
