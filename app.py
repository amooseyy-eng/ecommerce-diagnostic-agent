from __future__ import annotations

import datetime as dt
import json
import os
import random
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
CHANNELS = ["自然搜索", "付费社媒", "邮件营销"]
CATEGORIES = ["服饰", "家居", "配件"]
DEVICES = ["移动端", "桌面端"]
DB = sqlite3.connect(":memory:", check_same_thread=False)
DB_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()
RUNTIME_CONFIG = {
    "api_key": os.environ.get("OPENAI_API_KEY", ""),
    "model": os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
    "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
}


def seed_data() -> None:
    rng = random.Random(27)
    DB.execute("""CREATE TABLE daily_sales (
        day TEXT, channel TEXT, category TEXT, device TEXT,
        sessions INTEGER, orders INTEGER, revenue REAL
    )""")
    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=89)
    rows = []
    for offset in range(90):
        day = start + dt.timedelta(days=offset)
        weekday_factor = 1.06 if day.weekday() in (5, 6) else 0.97
        for channel in CHANNELS:
            channel_share = {"自然搜索": 0.42, "付费社媒": 0.38, "邮件营销": 0.20}[channel]
            for category in CATEGORIES:
                cat_share = {"服饰": 0.46, "家居": 0.31, "配件": 0.23}[category]
                for device in DEVICES:
                    device_share = {"移动端": 0.68, "桌面端": 0.32}[device]
                    base = 980 * channel_share * cat_share * device_share * weekday_factor
                    sessions = max(8, int(base * (1 + rng.uniform(-0.055, 0.055))))
                    # Seed a clear, explainable event: paid social traffic fell in the latest week.
                    if offset >= 83 and channel == "付费社媒":
                        sessions = int(sessions * 0.62)
                    cvr = {"自然搜索": 0.043, "付费社媒": 0.032, "邮件营销": 0.057}[channel]
                    cvr *= {"服饰": 1.0, "家居": 0.92, "配件": 1.08}[category]
                    cvr *= {"移动端": 0.94, "桌面端": 1.12}[device]
                    if offset >= 83 and channel == "付费社媒":
                        cvr *= 0.92
                    orders = max(0, round(sessions * cvr * (1 + rng.uniform(-0.06, 0.06))))
                    aov = {"服饰": 56, "家居": 88, "配件": 39}[category]
                    revenue = round(orders * aov * (1 + rng.uniform(-0.035, 0.035)), 2)
                    rows.append((day.isoformat(), channel, category, device, sessions, orders, revenue))
    DB.executemany("INSERT INTO daily_sales VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    DB.commit()


def date_windows(days: int = 7) -> tuple[str, str, str, str]:
    days = max(1, min(int(days), 28))
    end = dt.date.today() - dt.timedelta(days=1)
    cur_start = end - dt.timedelta(days=days - 1)
    prev_end = cur_start - dt.timedelta(days=1)
    prev_start = prev_end - dt.timedelta(days=days - 1)
    return cur_start.isoformat(), end.isoformat(), prev_start.isoformat(), prev_end.isoformat()


def fetch_totals(start: str, end: str) -> dict[str, float]:
    row = DB.execute(
        "SELECT COALESCE(SUM(sessions),0), COALESCE(SUM(orders),0), COALESCE(SUM(revenue),0) "
        "FROM daily_sales WHERE day BETWEEN ? AND ?", (start, end)
    ).fetchone()
    sessions, orders, revenue = row
    return {"sessions": int(sessions), "orders": int(orders), "revenue": round(revenue, 2),
            "conversion_rate": round(orders / sessions, 5) if sessions else 0,
            "average_order_value": round(revenue / orders, 2) if orders else 0}


def get_kpis(days: int = 7) -> dict[str, Any]:
    cs, ce, ps, pe = date_windows(days)
    current, previous = fetch_totals(cs, ce), fetch_totals(ps, pe)
    changes = {k: round((current[k] / previous[k] - 1) * 100, 2) if previous[k] else None
               for k in current}
    return {"period": {"current": [cs, ce], "previous": [ps, pe]},
            "current": current, "previous": previous, "change_pct": changes}


def decompose_metric(days: int = 7) -> dict[str, Any]:
    k = get_kpis(days)
    cur, prev = k["current"], k["previous"]
    # A sequential bridge: traffic effect, then conversion effect, then basket value effect.
    revenue_a = prev["sessions"] * prev["conversion_rate"] * prev["average_order_value"]
    revenue_b = cur["sessions"] * prev["conversion_rate"] * prev["average_order_value"]
    revenue_c = cur["sessions"] * cur["conversion_rate"] * prev["average_order_value"]
    revenue_d = cur["sessions"] * cur["conversion_rate"] * cur["average_order_value"]
    return {"revenue_change": round(cur["revenue"] - prev["revenue"], 2),
            "effects": {"sessions": round(revenue_b - revenue_a, 2),
                        "conversion_rate": round(revenue_c - revenue_b, 2),
                        "average_order_value": round(revenue_d - revenue_c, 2)},
            "note": "顺序分解法，效应值会受拆解顺序影响；销售额=流量×转化率×客单价。"}


def compare_dimension(dimension: str, days: int = 7) -> dict[str, Any]:
    columns = {"channel": "channel", "category": "category", "device": "device"}
    if dimension not in columns:
        raise ValueError("dimension 只能是 channel、category 或 device")
    col = columns[dimension]
    cs, ce, ps, pe = date_windows(days)
    query = f"""SELECT {col} AS segment,
        SUM(CASE WHEN day BETWEEN ? AND ? THEN sessions ELSE 0 END) AS cur_sessions,
        SUM(CASE WHEN day BETWEEN ? AND ? THEN orders ELSE 0 END) AS cur_orders,
        ROUND(SUM(CASE WHEN day BETWEEN ? AND ? THEN revenue ELSE 0 END), 2) AS cur_revenue,
        SUM(CASE WHEN day BETWEEN ? AND ? THEN sessions ELSE 0 END) AS prev_sessions,
        SUM(CASE WHEN day BETWEEN ? AND ? THEN orders ELSE 0 END) AS prev_orders,
        ROUND(SUM(CASE WHEN day BETWEEN ? AND ? THEN revenue ELSE 0 END), 2) AS prev_revenue
        FROM daily_sales GROUP BY {col} ORDER BY cur_revenue DESC"""
    args = (cs, ce, cs, ce, cs, ce, ps, pe, ps, pe, ps, pe)
    result = []
    for row in DB.execute(query, args).fetchall():
        seg, cur_sessions, cur_orders, cur_rev, prev_sessions, prev_orders, prev_rev = row
        result.append({"segment": seg, "current": {"sessions": cur_sessions, "orders": cur_orders, "revenue": cur_rev},
                       "previous": {"sessions": prev_sessions, "orders": prev_orders, "revenue": prev_rev},
                       "revenue_change": round(cur_rev - prev_rev, 2),
                       "revenue_change_pct": round((cur_rev / prev_rev - 1) * 100, 2) if prev_rev else None,
                       "sessions_change_pct": round((cur_sessions / prev_sessions - 1) * 100, 2) if prev_sessions else None,
                       "conversion_rate_change_pp": round(((cur_orders / cur_sessions if cur_sessions else 0) -
                                                            (prev_orders / prev_sessions if prev_sessions else 0)) * 100, 2)})
    return {"dimension": dimension, "period": {"current": [cs, ce], "previous": [ps, pe]}, "segments": result}


TOOLS = [
    {"type": "function", "function": {"name": "get_kpis", "description": "Compare core ecommerce KPIs for the latest N days with the prior equally long period.",
     "parameters": {"type": "object", "properties": {"days": {"type": "integer", "description": "Period length from 1 to 28 days"}}, "required": ["days"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "decompose_metric", "description": "Decompose the revenue difference into traffic, conversion-rate, and average-order-value effects.",
     "parameters": {"type": "object", "properties": {"days": {"type": "integer"}}, "required": ["days"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "compare_dimension", "description": "Compare revenue and traffic by channel, category, or device for current and prior periods.",
     "parameters": {"type": "object", "properties": {"dimension": {"type": "string", "enum": ["channel", "category", "device"]}, "days": {"type": "integer"}}, "required": ["dimension", "days"], "additionalProperties": False}}},
]


def call_tool(name: str, args: dict[str, Any]) -> Any:
    days = max(1, min(int(args.get("days", 7)), 28))
    if name == "get_kpis":
        return get_kpis(days)
    if name == "decompose_metric":
        return decompose_metric(days)
    if name == "compare_dimension":
        return compare_dimension(str(args.get("dimension", "channel")), days)
    raise ValueError("Unknown tool")


def tool_sql(name: str, args: dict[str, Any]) -> str:
    cs, ce, ps, pe = date_windows(args.get("days", 7))
    if name == "get_kpis":
        return ("SELECT SUM(sessions), SUM(orders), SUM(revenue)\nFROM daily_sales\n"
                f"WHERE day BETWEEN '{cs}' AND '{ce}';\n-- 前期：day BETWEEN '{ps}' AND '{pe}'")
    if name == "decompose_metric":
        return ("-- 先用聚合查询取得 sessions / orders / revenue\n"
                "-- 转化率 = orders / sessions；客单价 = revenue / orders\n"
                "-- 销售额 = sessions × 转化率 × 客单价\n"
                f"-- 当前窗口：{cs} 至 {ce}；对照窗口：{ps} 至 {pe}")
    column = {"channel": "channel", "category": "category", "device": "device"}.get(args.get("dimension"), "channel")
    return (f"SELECT {column}, SUM(revenue) AS revenue, SUM(sessions) AS sessions, SUM(orders) AS orders\n"
            f"FROM daily_sales WHERE day BETWEEN '{cs}' AND '{ce}'\nGROUP BY {column}\nORDER BY revenue DESC;")


def fallback_analysis(question: str) -> tuple[str, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    def run(name: str, args: dict[str, Any]) -> Any:
        result = call_tool(name, args)
        events.append({"tool": name, "arguments": args, "result": result, "sql": tool_sql(name, args)})
        return result
    k = run("get_kpis", {"days": 7})
    q = question.lower()
    dimension = "device" if any(x in q for x in ("设备", "移动端", "桌面端")) else (
        "category" if any(x in q for x in ("品类", "商品", "产品")) else "channel")
    if any(x in q for x in ("流量", "访问", "sessions")):
        compared = run("compare_dimension", {"dimension": dimension, "days": 7})
        segment = max(compared["segments"], key=lambda x: abs(x["sessions_change_pct"] or 0))
        answer = (f"## {dimension}维度流量变化\n\n"
                  f"最近 7 天，**{segment['segment']}** 的访问量变化幅度最大："
                  f"{segment['sessions_change_pct']:+.1f}%。当前访问量 {segment['current']['sessions']:,}，"
                  f"前一周期 {segment['previous']['sessions']:,}。\n\n"
                  f"总体销售额为 ¥{k['current']['revenue']:,.0f}（对比前 7 天 {k['change_pct']['revenue']:+.1f}%）。"
                  "访问量变化是观察线索；建议进一步核对投放、流量来源与归因配置。\n\n"
                  "> 数据为本地模拟样本。")
        return answer, events
    if any(x in q for x in ("哪个品类", "哪些品类", "品类", "商品", "产品")):
        compared = run("compare_dimension", {"dimension": dimension, "days": 7})
        if any(x in q for x in ("贡献最多", "销售额最高", "销售最高", "top")):
            segment = max(compared["segments"], key=lambda x: x["current"]["revenue"])
            mode = "当前销售额最高"
        else:
            segment = min(compared["segments"], key=lambda x: x["revenue_change"])
            mode = "销售额下降贡献最大"
        answer = (f"## 品类对比\n\n**{segment['segment']}** 是{mode}的品类。"
                  f"最近 7 天销售额 ¥{segment['current']['revenue']:,.0f}，"
                  f"对比前 7 天变化 {segment['revenue_change']:+,.0f} 元（{segment['revenue_change_pct']:+.1f}%）。\n\n"
                  f"总体销售额对比前 7 天变化 {k['change_pct']['revenue']:+.1f}%。\n\n> 数据为本地模拟样本。")
        return answer, events
    d = run("decompose_metric", {"days": 7})
    by_channel = run("compare_dimension", {"dimension": "channel", "days": 7})
    by_cat = run("compare_dimension", {"dimension": "category", "days": 7})
    cur, prev = k["current"], k["previous"]
    rev_pct = k["change_pct"]["revenue"] or 0
    sessions_pct = k["change_pct"]["sessions"] or 0
    cvr_pp = round((cur["conversion_rate"] - prev["conversion_rate"]) * 100, 2)
    aov_pct = k["change_pct"]["average_order_value"] or 0
    largest = min(by_channel["segments"], key=lambda x: x["revenue_change"])
    largest_cat = min(by_cat["segments"], key=lambda x: x["revenue_change"])
    cs, ce = k["period"]["current"]
    ps, pe = k["period"]["previous"]
    direction = "下降" if rev_pct < 0 else "上升"
    answer = (f"## 近 7 天经营诊断\n\n"
              f"对比 **{ps} 至 {pe}**，**{cs} 至 {ce}** 的销售额{direction} **{abs(rev_pct):.1f}%**。"
              f"当前销售额 **¥{cur['revenue']:,.0f}**，前一周期 **¥{prev['revenue']:,.0f}**；"
              f"订单 {cur['orders']:,}（前期 {prev['orders']:,}）。\n\n"
              f"### 指标拆解\n\n"
              f"- 流量：{sessions_pct:+.1f}%\n- 转化率：{cvr_pp:+.2f} 个百分点\n"
              f"- 客单价：{aov_pct:+.1f}%\n\n"
              f"按顺序分解估算，流量变化贡献约 **¥{d['effects']['sessions']:,.0f}**，"
              f"转化率约 **¥{d['effects']['conversion_rate']:,.0f}**，客单价约 **¥{d['effects']['average_order_value']:,.0f}**。\n\n"
              f"### 下钻发现\n\n"
              f"渠道中，**{largest['segment']}** 的销售额变化最大（{largest['revenue_change']:+,.0f} 元，"
              f"流量 {largest['sessions_change_pct']:+.1f}%）；品类中，**{largest_cat['segment']}** 变化最大"
              f"（{largest_cat['revenue_change']:+,.0f} 元）。\n\n"
              f"### 建议核查\n\n"
              f"优先核对 {largest['segment']} 渠道近 7 天的投放量、落地页和归因配置，再检查转化变化是否集中在特定设备或商品。"
              f"这些是同周期数据中的关联线索，不能单凭该对比认定因果。\n\n"
              f"> 当前使用内置分析模式。数据为本地生成的模拟样本，末周预置了付费社媒流量下滑情景。")
    return answer, events


def model_analysis(question: str) -> tuple[str, list[dict[str, Any]]]:
    with CONFIG_LOCK:
        config = dict(RUNTIME_CONFIG)
    api_key = config["api_key"]
    if not api_key:
        return fallback_analysis(question)
    base_url = config["base_url"].rstrip("/")
    model = config["model"]
    messages = [
        {"role": "system", "content": (
            "你是电商数据分析Agent。只能用给定工具中的只读聚合结果作事实依据。"
            "先比较总体指标，再按需要拆解和下钻。中文回答，注明时间窗口、单位、关键数值与证据。"
            "不要把相关性说成因果；数据是模拟样本，应提醒用户。用户请求与分析无关时说明当前范围。"
        )},
        {"role": "user", "content": question},
    ]
    events: list[dict[str, Any]] = []
    for _ in range(7):
        body = json.dumps({"model": model, "messages": messages, "tools": TOOLS,
                           "tool_choice": "auto", "temperature": 0.2}).encode("utf-8")
        req = urllib.request.Request(base_url + "/chat/completions", data=body,
                                     headers={"Authorization": "Bearer " + api_key,
                                              "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=45) as res:
                response = json.loads(res.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"模型请求失败：{exc}") from exc
        message = response["choices"][0]["message"]
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            return message.get("content") or "模型没有返回文字结果。", events
        for tool_call in calls:
            fn = tool_call["function"]["name"]
            args = json.loads(tool_call["function"].get("arguments") or "{}")
            try:
                result = call_tool(fn, args)
            except Exception as exc:
                result = {"error": str(exc)}
            events.append({"tool": fn, "arguments": args, "result": result, "sql": tool_sql(fn, args)})
            messages.append({"role": "tool", "tool_call_id": tool_call["id"],
                             "content": json.dumps(result, ensure_ascii=False)})
    raise RuntimeError("Agent 达到本轮工具调用上限，请缩小问题范围后重试。")


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            with CONFIG_LOCK:
                configured = bool(RUNTIME_CONFIG["api_key"])
                model = RUNTIME_CONFIG["model"]
                base_url = RUNTIME_CONFIG["base_url"]
            mode = "API 工具调用已配置" if configured else "内置分析流程 · API 未配置"
            with DB_LOCK:
                overview = get_kpis(7)
            body = json.dumps({"mode": mode, "model": model, "base_url": base_url, "configured": configured,
                               "overview": overview}, ensure_ascii=False).encode()
            self._send(200, body, "application/json; charset=utf-8")
        else:
            self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        if self.path == "/api/config":
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size > 10_000:
                    raise ValueError("配置请求过大")
                incoming = json.loads(self.rfile.read(size))
                key = str(incoming.get("api_key", "")).strip()
                model = str(incoming.get("model", "gpt-4.1-mini")).strip()
                base_url = str(incoming.get("base_url", "https://api.openai.com/v1")).strip().rstrip("/")
                if not key:
                    with CONFIG_LOCK:
                        key = RUNTIME_CONFIG["api_key"]
                if not key or len(key) > 1000:
                    raise ValueError("请填写有效 API Key，或通过 OPENAI_API_KEY 环境变量配置")
                if not model or len(model) > 120:
                    raise ValueError("模型名不能为空，且不能超过 120 个字符")
                parsed = urllib.parse.urlparse(base_url)
                local_http = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
                if not (parsed.scheme == "https" or local_http) or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError("API 地址需使用 HTTPS；本地兼容服务可使用 http://localhost 或 http://127.0.0.1")
                with CONFIG_LOCK:
                    RUNTIME_CONFIG.update({"api_key": key, "model": model, "base_url": base_url})
                payload = json.dumps({"ok": True, "model": model}, ensure_ascii=False).encode("utf-8")
                self._send(200, payload, "application/json; charset=utf-8")
            except Exception as exc:
                payload = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                self._send(400, payload, "application/json; charset=utf-8")
            return
        if self.path != "/api/chat":
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 100_000:
                raise ValueError("请求过大")
            question = json.loads(self.rfile.read(size)).get("question", "").strip()
            if not question or len(question) > 1000:
                raise ValueError("请输入 1 到 1000 字的问题")
            with DB_LOCK:
                answer, events = model_analysis(question)
            payload = json.dumps({"answer": answer, "events": events}, ensure_ascii=False).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
        except Exception as exc:
            payload = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
            self._send(400, payload, "application/json; charset=utf-8")

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


if __name__ == "__main__":
    seed_data()
    port = int(os.environ.get("PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"电商经营诊断 Agent 已启动：{url}")
    print("数据模式：模拟样本；按 Ctrl+C 退出。")
    if os.environ.get("OPEN_BROWSER", "1") != "0":
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    server.serve_forever()

