"""SQLite 存储。所有时间都是北京时间字符串（YYYY-MM-DD HH:MM[:SS]）。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from . import timeutil

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    oid INTEGER PRIMARY KEY, name TEXT NOT NULL, grade INTEGER, legacy_id INTEGER,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_name ON items(name);

-- 每次采集的列表快照（当前价、涨跌、秒出价）
CREATE TABLE IF NOT EXISTS snapshots (
    oid INTEGER NOT NULL, ts TEXT NOT NULL, price REAL, bl REAL,
    day7_price REAL, day7_bl REAL, day30_price REAL, day30_bl REAL,
    sell_method TEXT, sell_level INTEGER, quick_price REAL,
    PRIMARY KEY (oid, ts)
);

-- 分钟价格：接口只给最近 24 小时，所以每晚拉取后在这里自己攒历史
CREATE TABLE IF NOT EXISTS minute_prices (
    oid INTEGER NOT NULL, ts TEXT NOT NULL, price REAL NOT NULL,
    PRIMARY KEY (oid, ts)
);

CREATE TABLE IF NOT EXISTS hour_prices (
    oid INTEGER NOT NULL, ts TEXT NOT NULL, price REAL, pmin REAL, pmax REAL,
    forecast INTEGER NOT NULL DEFAULT 0, fetched_at TEXT,
    PRIMARY KEY (oid, ts, forecast)
);

CREATE TABLE IF NOT EXISTS day_prices (
    oid INTEGER NOT NULL, d TEXT NOT NULL, price REAL, pmin REAL, pmax REAL,
    min_at TEXT, max_at TEXT,
    PRIMARY KEY (oid, d)
);

-- 小涛查“今日倒子弹低价预测”等分析类接口的原始结论（当作一个信源来打分）
CREATE TABLE IF NOT EXISTS xtc_hints (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
    oid INTEGER, name TEXT, hour INTEGER, price REAL, extra TEXT
);

-- 接口调用记账（按次扣费，必须可追溯）
CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, day TEXT NOT NULL,
    provider TEXT NOT NULL, endpoint TEXT NOT NULL, cost INTEGER NOT NULL,
    ok INTEGER NOT NULL DEFAULT 1, run_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_usage_day ON api_usage(day, provider);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
    mode TEXT, status TEXT, api_cost INTEGER DEFAULT 0, summary TEXT
);

-- 每条建议。variant: rule = 纯规则基线；final = 经模型调整 + 护栏后的最终建议
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, plan_id TEXT NOT NULL,
    variant TEXT NOT NULL, created_at TEXT NOT NULL, oid INTEGER NOT NULL, name TEXT,
    action TEXT NOT NULL, buy_price REAL, sell_price REAL, stop_price REAL,
    qty INTEGER, budget REAL, valid_from TEXT, valid_until TEXT,
    confidence REAL, reason TEXT, model TEXT, prompt_ver TEXT, paper INTEGER DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'open',      -- open | final
    buy_hit INTEGER, buy_hit_at TEXT, outcome TEXT, exit_price REAL, exit_at TEXT,
    net_return REAL, net_profit REAL, scored_at TEXT, score_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_dec_plan ON decisions(plan_id, variant);
CREATE INDEX IF NOT EXISTS idx_dec_status ON decisions(status);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, side TEXT NOT NULL,
    oid INTEGER, name TEXT NOT NULL, qty INTEGER NOT NULL, price REAL NOT NULL,
    source TEXT, message_id TEXT, paper INTEGER DEFAULT 0, note TEXT,
    kind TEXT NOT NULL DEFAULT 'trade',   -- trade = 普通成交；opening = 期初库存（用本程序之前就持有的，不动余额）
    net REAL,                             -- 卖出时实际到手的总金额（可不填；填了就用它算余额和盈亏、并用来校准费率）
    mail_seq INTEGER                      -- 来自邮件回复时：这是那封信里的第几笔（和 message_id 一起保证同一封信不会记两遍）
);

-- 信源观点：UP 主、新闻、群消息、小涛查预测……统一入库，N 天后对照真实价格打分
CREATE TABLE IF NOT EXISTS opinions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source_type TEXT NOT NULL, source_name TEXT NOT NULL,
    published_at TEXT, collected_at TEXT NOT NULL, url TEXT, title TEXT, summary TEXT,
    oid INTEGER, item_name TEXT, direction TEXT, horizon_days INTEGER, ref_price REAL,
    status TEXT NOT NULL DEFAULT 'open', hit INTEGER, move_pct REAL, scored_at TEXT,
    fingerprint TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source_type TEXT NOT NULL, source_name TEXT NOT NULL,
    published_at TEXT, collected_at TEXT NOT NULL, url TEXT, title TEXT, text TEXT,
    digest TEXT, fingerprint TEXT UNIQUE,
    quarantined INTEGER DEFAULT 0, quarantine_reason TEXT   -- 疑似含有操纵模型的指令：隔离，不参与分析
);

CREATE TABLE IF NOT EXISTS mail_log (
    message_id TEXT PRIMARY KEY, direction TEXT NOT NULL, subject TEXT, ts TEXT NOT NULL,
    status TEXT, plan_id TEXT, note TEXT,
    paper INTEGER           -- 发出的邮件对应的计划是模拟(1)还是真实(0)；NULL = 不对应任何计划
);

CREATE TABLE IF NOT EXISTS moligod_alerts (
    name TEXT PRIMARY KEY, low REAL, high REAL, synced_at TEXT, verified INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, period TEXT,
    content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', model TEXT, decided_at TEXT
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
"""


class TransactionAborted(sqlite3.OperationalError):
    """写事务里有一步失败了，却被外层代码吞掉了异常：整个事务已经回滚，什么都没有写进去。"""


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._tx_depth = 0          # write_tx 的嵌套层数；>0 时 execute 等方法不单独提交
        self._tx_failed = False     # 嵌套的某一层已经失败：最外层退出时必须回滚
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """给旧数据库补上后来新增的列（只增不改，可重复执行）。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(mail_log)")}
        if "paper" not in cols:
            self.conn.execute("ALTER TABLE mail_log ADD COLUMN paper INTEGER")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(trades)")}
        for col, decl in (("kind", "TEXT NOT NULL DEFAULT 'trade'"), ("net", "REAL"), ("mail_seq", "INTEGER")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {decl}")
        # 同一封邮件里的同一笔只能记一次（旧数据的 mail_seq 是空的，不受这条约束影响）
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_mail ON trades(message_id, mail_seq) "
                          "WHERE message_id IS NOT NULL AND mail_seq IS NOT NULL")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(documents)")}
        if "quarantined" not in cols:
            self.conn.execute("ALTER TABLE documents ADD COLUMN quarantined INTEGER DEFAULT 0")
            self.conn.execute("ALTER TABLE documents ADD COLUMN quarantine_reason TEXT")

    # ---- 事务 ----
    @contextmanager
    def write_tx(self):
        """写事务：先拿到数据库的写锁（BEGIN IMMEDIATE），再读、检查、写，最后一起提交。

        给“先查、够了才写”的操作用（记账：先查持仓，够了才写卖出）。平常“查”和“写”是分开的两步，
        界面、计划任务、命令行是不同的进程，可以同时查到“够”，然后各写一笔 —— 结果超卖。
        在这里面，别的进程要等我们提交之后才能开始它自己的写事务，那时它查到的已经是我们写完之后的持仓。
        等不到锁（默认最多等 30 秒）会抛 sqlite3.OperationalError，此时什么都没写。

        - 事务里照常用 db.execute / db.query / db.kv_set：它们在事务里不会单独提交。
        - 可以嵌套（内层不单独提交）。任何一层抛出异常，整个事务回滚，数据库里不留任何痕迹；
          即使外层把内层的异常吞掉了，最外层退出时也会回滚并抛 TransactionAborted —— 不允许“半笔账”被提交。
        """
        outermost = self._tx_depth == 0
        if outermost:
            if self.conn.in_transaction:
                self.conn.commit()
            self.conn.execute("BEGIN IMMEDIATE")
            self._tx_failed = False
        self._tx_depth += 1
        try:
            yield self
        except BaseException:
            self._tx_depth -= 1
            self._tx_failed = True
            if outermost:
                self._tx_failed = False
                self.conn.rollback()
            raise
        self._tx_depth -= 1
        if outermost:
            if self._tx_failed:
                self._tx_failed = False
                self.conn.rollback()
                raise TransactionAborted("事务里有一步失败了，整个事务已回滚，没有写入任何内容")
            self.conn.commit()

    @property
    def in_write_tx(self) -> bool:
        return self._tx_depth > 0

    def _commit(self) -> None:
        if self._tx_depth == 0:
            self.conn.commit()

    # ---- 通用 ----
    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, tuple(params))
        self._commit()
        return cur

    def executemany(self, sql: str, rows: Iterable[Iterable]) -> None:
        self.conn.executemany(sql, rows)
        self._commit()

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def close(self) -> None:
        self.conn.close()

    # ---- kv ----
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return row["value"]

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), timeutil.fmt(timeutil.now())),
        )

    # ---- 接口记账 ----
    def add_usage(self, provider: str, endpoint: str, cost: int, ok: bool = True,
                  run_id: int | None = None) -> None:
        n = timeutil.now()
        self.execute(
            "INSERT INTO api_usage(ts,day,provider,endpoint,cost,ok,run_id) VALUES(?,?,?,?,?,?,?)",
            (timeutil.fmt(n), timeutil.day_str(n), provider, endpoint, cost, 1 if ok else 0, run_id),
        )

    def usage_on(self, provider: str, day: str | None = None) -> int:
        day = day or timeutil.day_str(timeutil.now())
        row = self.one("SELECT COALESCE(SUM(cost),0) AS c FROM api_usage WHERE provider=? AND day=?",
                       (provider, day))
        return int(row["c"])

    def usage_total(self, provider: str) -> int:
        row = self.one("SELECT COALESCE(SUM(cost),0) AS c FROM api_usage WHERE provider=?", (provider,))
        return int(row["c"])

    # ---- 行情 ----
    def upsert_item(self, oid: int, name: str, grade: int | None, legacy_id: int | None) -> None:
        self.execute(
            "INSERT INTO items(oid,name,grade,legacy_id,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(oid) DO UPDATE SET name=excluded.name, grade=excluded.grade, "
            "legacy_id=excluded.legacy_id, updated_at=excluded.updated_at",
            (oid, name, grade, legacy_id, timeutil.fmt(timeutil.now())),
        )

    def add_minutes(self, oid: int, points: Iterable[tuple[datetime, float]]) -> int:
        rows = [(oid, timeutil.fmt_min(dt), float(p)) for dt, p in points]
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO minute_prices(oid,ts,price) VALUES(?,?,?)", rows)
        self._commit()
        return self.conn.total_changes - before

    def minutes(self, oid: int, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        rows = self.query(
            "SELECT ts, price FROM minute_prices WHERE oid=? AND ts>=? AND ts<=? ORDER BY ts",
            (oid, timeutil.fmt_min(start), timeutil.fmt_min(end)))
        return [(timeutil.parse_dt(r["ts"]), r["price"]) for r in rows]

    def days(self, oid: int, start: datetime, end: datetime) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM day_prices WHERE oid=? AND d>=? AND d<=? ORDER BY d",
            (oid, timeutil.day_str(start), timeutil.day_str(end)))

    def latest_snapshot(self, oid: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM snapshots WHERE oid=? ORDER BY ts DESC LIMIT 1", (oid,))

    def price_at(self, oid: int, when: datetime, tolerance_hours: float = 6) -> float | None:
        """取离 when 最近的已知价格（先分钟，再快照，再日线）。"""
        lo = timeutil.fmt_min(when - timedelta(hours=tolerance_hours))
        hi = timeutil.fmt_min(when + timedelta(hours=tolerance_hours))
        w = timeutil.fmt_min(when)
        best: tuple[float, float] | None = None
        for table in ("minute_prices", "snapshots"):
            for op, order in ((">=", "ASC"), ("<=", "DESC")):
                r = self.one(
                    f"SELECT ts, price FROM {table} WHERE oid=? AND ts{op}? AND ts>=? AND ts<=? "
                    f"AND price IS NOT NULL ORDER BY ts {order} LIMIT 1", (oid, w, lo, hi))
                if r:
                    gap = abs((timeutil.parse_dt(r["ts"]) - when).total_seconds())
                    if best is None or gap < best[0]:
                        best = (gap, r["price"])
        if best:
            return best[1]
        r = self.one("SELECT price FROM day_prices WHERE oid=? AND d=?", (oid, timeutil.day_str(when)))
        return r["price"] if r and r["price"] is not None else None

    # ---- 品种名解析 ----
    def find_item(self, text: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM items WHERE name=? COLLATE NOCASE", (text,))
