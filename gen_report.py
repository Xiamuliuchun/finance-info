#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_report.py — 用 Tushare 数据生成每日市场复盘 HTML 报告

本地运行:
    pip install -r requirements.txt
    export TUSHARE_TOKEN=<你的 Tushare Token>
    python gen_report.py

GitHub Actions 运行:
    - name: Build market review
      env:
        TUSHARE_TOKEN: ${{ secrets.TUSHARE_TOKEN }}
      run: |
        python -m pip install -r requirements.txt
        python gen_report.py

约定:
    1. Token 只从环境变量 TUSHARE_TOKEN 读取，不硬编码、不写进报告。
    2. 目标交易日动态解析为「最近一个已收盘交易日」，默认排除北京时间今天
       （当天尚未收盘、Tushare 约 19:00 才更新当日数据）：优先用 Tushare 交易日历
       trade_cal，失败则降级用上证指数日线回溯，再失败回退为北京时间昨天。
       输出为当前目录下的 market-review-<YYYYMMDD>.html（文件名随交易日变化），
       同名覆盖。用环境变量 TRADE_DATE=YYYYMMDD 可手动指定，
       既用于补跑历史，也可在收盘后强制生成当天报告。
    3. 每个数据源独立异常捕获；失败打印 [ERROR] 并在报告里标注缺失，
       绝不编造数字。核心行情全部失败时以非零状态退出。
    4. 输出为纯 HTML + 内联 CSS，无 JS / 无 CDN / 无外部字体，可离线打开。
    5. 运行依赖见 requirements.txt（tushare / pandas / six），其余为标准库。
"""

import html as html_lib
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import tushare as ts

# ---------------------------------------------------------------- 配置
# 目标交易日与输出文件名在 main() 中动态解析后写入下面两个全局变量。
# 需要补跑某一天时，可用环境变量 TRADE_DATE=YYYYMMDD 手动指定。
TRADE_DATE = None
OUTPUT_FILE = None
OUTPUT_PATTERN = "market-review-{date}.html"  # 当前目录，同名覆盖
NEWS_LIMIT = 10                               # 报告展示的快讯条数

A_INDEXES = [
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"),
    ("000300.SH", "沪深300"),
    ("000905.SH", "中证500"),
    ("000016.SH", "上证50"),
    ("899050.BJ", "北证50"),
]

GLOBAL_NAMES = {
    "HSI": "港·恒生指数", "HKTECH": "港·恒生科技", "XIN9": "亚·富时中国A50",
    "N225": "亚·日经225", "KS11": "亚·韩国KOSPI", "TWII": "亚·台湾加权",
    "AS51": "亚·澳洲普通股", "SENSEX": "亚·印度SENSEX",
    "FTSE": "欧·英国富时100", "FCHI": "欧·法国CAC40", "GDAXI": "欧·德国DAX",
    "CSX5P": "欧·欧洲斯托克50",
    "DJI": "美·道琼斯", "SPX": "美·标普500", "IXIC": "美·纳斯达克",
    "RUT": "美·罗素2000", "SPTSX": "美洲·加拿大TSX", "IBOVESPA": "美洲·巴西IBOV",
}

ERRORS = []          # [(数据源名称, 错误信息)]
OK_COUNT = [0]       # 成功拉取的数据源数量


# ---------------------------------------------------------------- 工具
def esc(value):
    """HTML 转义，避免资讯文本破坏页面结构。"""
    return html_lib.escape("" if value is None else str(value), quote=True)


def num(value, default=None):
    """安全转数值。"""
    try:
        result = float(value)
        if pd.isna(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def fmt_pct(value, digits=2):
    value = num(value)
    if value is None:
        return "—"
    return format(value, "+.{0}f".format(digits)) + "%"


def fmt_num(value, digits=2):
    value = num(value)
    if value is None:
        return "—"
    return format(value, ",.{0}f".format(digits))


def cls(value):
    """红涨绿跌（中国市场惯例）。"""
    value = num(value)
    if value is None or value == 0:
        return "flat"
    return "up" if value > 0 else "down"


def record_error(label, exc):
    message = "%s: %s" % (type(exc).__name__, exc)
    ERRORS.append((label, message))
    print("[ERROR] %s 拉取失败 -> %s" % (label, message), file=sys.stderr)


def safe_call(label, func):
    """拉取单个数据源：失败打印报错并返回 None，不中断整体流程。"""
    try:
        df = func()
        if df is None or not hasattr(df, "empty") or df.empty:
            raise ValueError("接口返回为空")
        OK_COUNT[0] += 1
        print("[OK] %s: %d 行" % (label, len(df)))
        return df
    except Exception as exc:
        record_error(label, exc)
        return None


def beijing_now():
    """GitHub Actions runner 走 UTC；中国无夏令时，固定 +8 小时即北京时间。"""
    return datetime.utcnow() + timedelta(hours=8)


def resolve_trade_date(pro):
    """解析「最近一个已收盘交易日」：手动指定 > 交易日历 > 指数日线回溯 > 北京时间昨天。

    默认刻意排除北京时间今天：A 股当日尚未收盘、Tushare 约 19:00 才更新当日数据，
    早上跑任务时取今天会拿到空数据或残缺数据。若确实要跑当天（例如收盘后手动触发），
    用 TRADE_DATE=YYYYMMDD 显式指定即可。
    """
    override = os.environ.get("TRADE_DATE", "").strip()
    if override:
        if len(override) == 8 and override.isdigit():
            print("[INFO] 使用环境变量指定的交易日 %s" % override)
            return override
        print("[WARN] TRADE_DATE=%r 格式无效（应为 YYYYMMDD），改用自动解析。"
              % override, file=sys.stderr)

    now = beijing_now()
    today = now.strftime("%Y%m%d")
    start = (now - timedelta(days=20)).strftime("%Y%m%d")

    def closed_only(values):
        """只保留严格早于今天的日期（YYYYMMDD 字典序即时间序）。"""
        return sorted({str(v) for v in values if str(v) < today})

    try:
        cal = pro.trade_cal(exchange="SSE", start_date=start, end_date=today,
                            is_open="1", fields="cal_date")
        if cal is not None and not cal.empty and "cal_date" in cal.columns:
            dates = closed_only(cal["cal_date"].dropna().tolist())
            if dates:
                print("[INFO] 最近已收盘交易日 %s（北京时间今天 %s，已排除今天）"
                      % (dates[-1], today))
                return dates[-1]
        print("[WARN] 交易日历没有早于今天的开市日，尝试指数日线回溯。",
              file=sys.stderr)
    except Exception as exc:
        print("[WARN] 交易日历 trade_cal 不可用（%s: %s），尝试指数日线回溯。"
              % (type(exc).__name__, exc), file=sys.stderr)

    try:
        hist = pro.index_daily(ts_code="000001.SH", start_date=start,
                               end_date=today, fields="trade_date,close")
        if hist is not None and not hist.empty and "trade_date" in hist.columns:
            dates = closed_only(hist["trade_date"].dropna().tolist())
            if dates:
                print("[INFO] 降级回溯得到最近已收盘交易日 %s" % dates[-1])
                return dates[-1]
    except Exception as exc:
        print("[WARN] 指数日线回溯也失败（%s: %s）。"
              % (type(exc).__name__, exc), file=sys.stderr)

    yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
    print("[WARN] 交易日自动解析失败，回退为北京时间昨天 %s（若休市将无数据）。"
          % yesterday, file=sys.stderr)
    return yesterday


def date_span():
    day = datetime.strptime(TRADE_DATE, "%Y%m%d")
    return (day - timedelta(days=14)).strftime("%Y%m%d"), TRADE_DATE


def news_window():
    day = datetime.strptime(TRADE_DATE, "%Y%m%d").strftime("%Y-%m-%d")
    return day + " 00:00:00", day + " 23:59:59"


# ---------------------------------------------------------------- 数据拉取
def fetch_a_indexes(pro):
    """逐个拉主要 A 股指数，单只失败不影响其它。同时保留上证指数近区间序列。"""
    start, end = date_span()
    rows, sh_hist = [], []
    for code, name in A_INDEXES:
        try:
            df = pro.index_daily(ts_code=code, start_date=start, end_date=end)
            if df is None or df.empty:
                raise ValueError("接口返回为空")
            df = df.sort_values("trade_date")
            current = df[df["trade_date"] == TRADE_DATE]
            if current.empty:
                raise ValueError("目标交易日 %s 无数据" % TRADE_DATE)
            row = current.iloc[-1]
            rows.append({
                "name": name,
                "close": num(row.get("close")),
                "pct": num(row.get("pct_chg")),
                "open": num(row.get("open")),
                "high": num(row.get("high")),
                "low": num(row.get("low")),
                "pre_close": num(row.get("pre_close")),
                "amount": num(row.get("amount")),
            })
            OK_COUNT[0] += 1
            print("[OK] 指数 %s (%s)" % (name, code))
            if code == "000001.SH":
                for _, r in df.tail(6).iterrows():
                    sh_hist.append({
                        "date": str(r.get("trade_date")),
                        "close": num(r.get("close")),
                        "pct": num(r.get("pct_chg")),
                        "amount": num(r.get("amount")),
                    })
        except Exception as exc:
            record_error("指数 %s (%s)" % (name, code), exc)
    return rows, sh_hist


def fetch_all(pro):
    """拉取全部数据，返回上下文字典。"""
    start, end = news_window()
    ctx = {"errors": ERRORS, "ok_count": OK_COUNT[0]}

    ctx["a_indexes"], ctx["sh_hist"] = fetch_a_indexes(pro)

    ctx["global"] = safe_call(
        "国际主要指数 index_global",
        lambda: pro.index_global(trade_date=TRADE_DATE),
    )
    ctx["daily_info"] = safe_call(
        "沪深交易统计 daily_info",
        lambda: pro.daily_info(trade_date=TRADE_DATE),
    )
    ctx["daily"] = safe_call(
        "全市场个股日线 daily",
        lambda: pro.daily(trade_date=TRADE_DATE),
    )
    ctx["hsgt"] = safe_call(
        "陆股通资金 moneyflow_hsgt",
        lambda: pro.moneyflow_hsgt(trade_date=TRADE_DATE),
    )
    ctx["limit_list"] = safe_call(
        "涨跌停清单 limit_list_d",
        lambda: pro.limit_list_d(trade_date=TRADE_DATE),
    )
    ctx["news"] = safe_call(
        "当日快讯 news",
        lambda: pro.news(src="sina", start_date=start, end_date=end,
                         fields="datetime,title,content"),
    )
    if ctx["news"] is None:
        ctx["news"] = safe_call(
            "当日长篇资讯 major_news（快讯降级）",
            lambda: pro.major_news(start_date=start, end_date=end,
                                   fields="title,pub_time,src,url"),
        )
    ctx["ok_count"] = OK_COUNT[0]
    return ctx


# ---------------------------------------------------------------- 计算
def market_breadth(ctx):
    """从全市场个股日线统计涨跌家数；涨跌停优先用 limit_list_d。"""
    result = {"up": None, "down": None, "flat": None,
              "limit_up": None, "limit_down": None, "source": None}
    daily = ctx.get("daily")
    if daily is not None and "pct_chg" in daily.columns:
        pct = pd.to_numeric(daily["pct_chg"], errors="coerce").dropna()
        result.update(up=int((pct > 0).sum()), down=int((pct < 0).sum()),
                      flat=int((pct == 0).sum()),
                      limit_up=int((pct >= 9.8).sum()),
                      limit_down=int((pct <= -9.8).sum()),
                      source="pro.daily 全市场个股日线，涨跌停按涨跌幅 ±9.8% 近似统计")
    limits = ctx.get("limit_list")
    if limits is not None and "limit_type" in limits.columns:
        kinds = limits["limit_type"].astype(str)
        result["limit_up"] = int((kinds == "U").sum())
        result["limit_down"] = int((kinds == "D").sum())
        result["source"] = "涨跌家数据 pro.daily；涨停/跌停数据 pro.limit_list_d（U/D）"
    return result


def amount_change(ctx):
    """上证指数 amount 字段同口径环比（比值，不涉及单位换算）。"""
    hist = ctx.get("sh_hist") or []
    if len(hist) < 2:
        return None
    prev, curr = hist[-2]["amount"], hist[-1]["amount"]
    if not prev or not curr:
        return None
    return (curr / prev - 1.0) * 100.0


def total_amount(ctx):
    """沪深两市成交额合计（daily_info，单位亿元，Tushare 口径）。"""
    info = ctx.get("daily_info")
    if info is None or "ts_code" not in info.columns:
        return None
    picked = info[info["ts_code"].astype(str).isin(["SH_MARKET", "SZ_MARKET"])]
    if picked.empty or "amount" not in picked.columns:
        return None
    values = pd.to_numeric(picked["amount"], errors="coerce").dropna()
    return float(values.sum()) if not values.empty else None


def build_headline(ctx, breadth, amount, amount_chg):
    """结论卡文字由数据规则生成，不写死任何行情数字。"""
    indexes = ctx.get("a_indexes") or []
    sh = next((i for i in indexes if i["name"] == "上证指数"), None)
    if sh is None or sh["pct"] is None:
        return "核心指数数据拉取失败，本次报告仅展示可用数据，请检查日志中的 [ERROR] 条目。"
    sh_pct = sh["pct"]
    if sh_pct > 0:
        tone = "上证指数收涨 %s" % fmt_pct(sh_pct)
    elif sh_pct < 0:
        tone = "上证指数收跌 %s" % fmt_pct(sh_pct)
    else:
        tone = "上证指数收平"
    if breadth["up"] is not None and breadth["down"] is not None:
        if sh_pct > 0 and breadth["down"] > breadth["up"]:
            tone += "，但下跌家数多于上涨家数，属权重支撑下的分化行情"
        elif sh_pct < 0 and breadth["up"] > breadth["down"]:
            tone += "，但上涨家数占优，指数与个股背离"
        else:
            tone += "，涨跌家数与指数方向一致"
    else:
        tone += "；市场宽度数据未取到"
    parts = [tone + "。"]
    if amount is not None:
        segment = "沪深两市合计成交 %s 亿元（Tushare daily_info 口径）" % fmt_num(amount, 0)
        if amount_chg is not None:
            segment += "，较上一交易日 %s（按 index_daily amount 同字段比值计算）" % fmt_pct(amount_chg)
        parts.append(segment + "。")
    parts.append("以下所有数值均由 Tushare 接口实时拉取，未取到的项目已明确标注，不做推测补齐。")
    return "".join(parts)


# ---------------------------------------------------------------- HTML 渲染
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--line:#e6e8ec;--ink:#1b2330;--sub:#667085;
--muted:#8b95a5;--accent:#1c4e80;--accent-soft:#eaf1f8;--up:#d9443f;--down:#12996b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);line-height:1.75;
font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:1060px;margin:0 auto;padding:32px 22px 60px}
h1{font-size:28px;margin:0 0 8px;letter-spacing:-.4px}
h2{font-size:19px;margin:0 0 4px}
h3{font-size:15px;margin:0 0 10px}
.masthead{border-bottom:2px solid var(--ink);padding-bottom:18px;margin-bottom:22px}
.kicker{font-size:12px;letter-spacing:2px;color:var(--accent);font-weight:700;margin-bottom:10px}
.meta{font-size:13px;color:var(--sub);margin-top:8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 22px;margin-bottom:18px}
.tldr{border-left:4px solid var(--accent);background:linear-gradient(180deg,#fff,#fbfcfe)}
.lead{font-size:16px;font-weight:600;line-height:1.85;margin-bottom:8px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:18px}
.kpi{border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.kpi .label{font-size:11.5px;color:var(--sub);margin-bottom:4px}
.kpi .val{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.kpi .note{font-size:11px;color:var(--muted);margin-top:3px}
.up{color:var(--up)}.down{color:var(--down)}.flat{color:var(--sub)}
.sec{display:flex;align-items:baseline;gap:12px;margin:30px 0 12px;flex-wrap:wrap}
.sec .no{font-size:12px;font-weight:700;color:#fff;background:var(--accent);border-radius:4px;padding:2px 7px}
.sec .tip{font-size:12.5px;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th{background:#f4f6f9;color:var(--sub);font-weight:600;text-align:left;padding:9px 11px;border-bottom:1px solid var(--line)}
td{padding:9px 11px;border-bottom:1px solid #f0f2f5}
tr:last-child td{border-bottom:none}
td.n,th.n{text-align:right}
.tbl-wrap{overflow-x:auto}
.note{font-size:11.5px;color:var(--muted);line-height:1.7;margin-top:9px;border-top:1px dashed var(--line);padding-top:9px}
.barrow{display:grid;grid-template-columns:150px 1fr 76px;gap:12px;align-items:center;margin:7px 0;font-size:12.5px}
.barrow .nm{color:#3c4655}
.barrow .v{text-align:right;font-weight:600;font-variant-numeric:tabular-nums}
.track{position:relative;height:12px;background:#f0f2f5;border-radius:3px;overflow:hidden}
.zero{position:absolute;left:50%;top:0;bottom:0;width:1px;background:#c2c9d2}
.bar{position:absolute;top:2px;height:8px;border-radius:2px}
.bar.u{left:50%;background:var(--up)}
.bar.d{right:50%;background:var(--down)}
.stack{display:flex;height:14px;border-radius:4px;overflow:hidden;background:#f0f2f5;margin:10px 0 6px}
.stack span{display:block;height:100%}
.legend{font-size:12px;color:var(--sub)}
.legend b{font-variant-numeric:tabular-nums}
.status{font-size:12.5px;line-height:1.9}
.status .bad{color:#b42318}
.status .good{color:#067647}
.news{margin:0;padding-left:20px}
.news li{margin-bottom:9px;font-size:13px;line-height:1.7;color:#3c4655}
.news .t{font-size:11.5px;color:var(--muted);display:block}
.empty{font-size:13px;color:var(--muted);background:#f7f8fa;border-radius:6px;padding:12px 14px}
.foot{font-size:11.5px;color:var(--muted);border-top:1px solid var(--line);padding-top:15px;margin-top:26px}
@media(max-width:720px){.kpis{grid-template-columns:repeat(2,1fr)}.barrow{grid-template-columns:112px 1fr 66px}}
"""


def render_status(ctx):
    if not ERRORS:
        return ('<div class="card"><h3>数据源状态</h3><div class="status good">'
                '全部 %d 个数据项拉取成功。</div></div>' % ctx["ok_count"])
    items = "".join("<li><b>%s</b>：%s</li>" % (esc(k), esc(v)) for k, v in ERRORS)
    return ('<div class="card"><h3>数据源状态</h3>'
            '<div class="status"><span class="good">成功 %d 项</span> · '
            '<span class="bad">失败 %d 项（对应章节已标注缺失，未编造数据）</span></div>'
            '<ul class="status bad">%s</ul></div>' % (ctx["ok_count"], len(ERRORS), items))


def render_index_table(ctx):
    rows = ctx.get("a_indexes") or []
    if not rows:
        return '<div class="empty">主要指数数据拉取失败，详见上方数据源状态与运行日志。</div>'
    body = []
    for r in rows:
        swing = None
        if r["high"] is not None and r["low"] is not None and r["pre_close"]:
            swing = (r["high"] - r["low"]) / r["pre_close"] * 100.0
        body.append(
            "<tr><td>%s</td><td class=\"n\">%s</td><td class=\"n %s\">%s</td>"
            "<td class=\"n\">%s</td></tr>" % (
                esc(r["name"]), fmt_num(r["close"]), cls(r["pct"]),
                fmt_pct(r["pct"]), "—" if swing is None else "%.2f%%" % swing))
    return ('<div class="tbl-wrap"><table><thead><tr><th>指数</th><th class="n">收盘</th>'
            '<th class="n">涨跌幅</th><th class="n">振幅</th></tr></thead><tbody>%s'
            '</tbody></table></div>' % "".join(body))


def render_global_bars(ctx):
    df = ctx.get("global")
    if df is None or "ts_code" not in df.columns:
        return '<div class="empty">国际指数数据拉取失败，本章节无数据。</div>'
    picked = []
    for _, row in df.iterrows():
        code = str(row.get("ts_code"))
        if code not in GLOBAL_NAMES:
            continue
        value = num(row.get("pct_chg"))
        if value is None:
            continue
        picked.append((GLOBAL_NAMES[code], value))
    if not picked:
        return '<div class="empty">国际指数返回数据中未包含可识别的指数代码。</div>'
    picked.sort(key=lambda x: x[1])
    ceiling = max([abs(v) for _, v in picked] + [1.0])
    lines = []
    for name, value in picked:
        width = abs(value) / ceiling * 48.0
        side = "u" if value >= 0 else "d"
        lines.append(
            '<div class="barrow"><span class="nm">%s</span>'
            '<span class="track"><span class="zero"></span>'
            '<span class="bar %s" style="width:%.2f%%"></span></span>'
            '<span class="v %s">%s</span></div>' % (
                esc(name), side, width, cls(value), fmt_pct(value)))
    return "".join(lines)


def render_breadth(ctx, breadth):
    up, down, flat = breadth["up"], breadth["down"], breadth["flat"]
    if up is None:
        return ('<div class="empty">市场宽度数据拉取失败（pro.daily），本章节无数据。</div>', None)
    total = max(up + down + flat, 1)
    wu, wd, wf = up / total * 100, down / total * 100, flat / total * 100
    block = (
        '<div class="stack">'
        '<span class="up" style="width:%.2f%%;background:#d9443f"></span>'
        '<span class="flat" style="width:%.2f%%;background:#c9ced6"></span>'
        '<span class="down" style="width:%.2f%%;background:#12996b"></span>'
        '</div><div class="legend">上涨 <b class="up">%s</b> 家 · '
        '平盘 <b>%s</b> 家 · 下跌 <b class="down">%s</b> 家 · '
        '涨停 <b class="up">%s</b> · 跌停 <b class="down">%s</b></div>' % (
            wu, wf, wd, fmt_num(up, 0), fmt_num(flat, 0), fmt_num(down, 0),
            "—" if breadth["limit_up"] is None else str(breadth["limit_up"]),
            "—" if breadth["limit_down"] is None else str(breadth["limit_down"])))
    return block, breadth["source"]


def render_daily_info(ctx):
    df = ctx.get("daily_info")
    if df is None or "ts_code" not in df.columns:
        return '<div class="empty">沪深交易统计数据拉取失败，本章节无数据。</div>'
    wanted = ["SH_MARKET", "SZ_MARKET", "SH_A", "SH_STAR", "SZ_MAIN", "SZ_GEM"]
    rows = []
    for code in wanted:
        part = df[df["ts_code"].astype(str) == code]
        if part.empty:
            continue
        r = part.iloc[0]
        rows.append("<tr><td>%s</td><td class=\"n\">%s</td><td class=\"n\">%s</td>"
                    "<td class=\"n\">%s</td></tr>" % (
                        esc(r.get("ts_name", code)),
                        fmt_num(r.get("amount"), 2),
                        fmt_num(r.get("pe"), 2),
                        fmt_num(r.get("com_count"), 0)))
    if not rows:
        return '<div class="empty">daily_info 返回数据中未包含预期板块代码。</div>'
    return ('<div class="tbl-wrap"><table><thead><tr><th>板块</th>'
            '<th class="n">成交额（亿元）</th><th class="n">市盈率</th>'
            '<th class="n">公司数</th></tr></thead><tbody>%s</tbody></table></div>'
            % "".join(rows))


def render_hsgt(ctx):
    df = ctx.get("hsgt")
    if df is None:
        return '<div class="empty">陆股通资金数据拉取失败，本章节无数据。</div>'
    r = df.iloc[0]
    keys = [("north_money", "北向资金"), ("south_money", "南向资金"),
            ("hgt", "沪股通"), ("sgt", "深股通"),
            ("ggt_ss", "港股通（沪）"), ("ggt_sz", "港股通（深）")]
    rows = []
    for key, label in keys:
        if key not in df.columns:
            continue
        value = num(r.get(key))
        rows.append("<tr><td>%s</td><td class=\"n\">%s</td></tr>" % (
            esc(label), "—" if value is None else fmt_num(value, 2)))
    if not rows:
        return '<div class="empty">moneyflow_hsgt 返回数据中未包含预期字段。</div>'
    return ('<div class="tbl-wrap"><table><thead><tr><th>项目</th>'
            '<th class="n">原始值</th></tr></thead><tbody>%s</tbody></table></div>'
            '<div class="note">数值为 Tushare moneyflow_hsgt 接口原始值，单位与口径以 '
            'Tushare 官方文档为准，脚本未做换算，避免口径误用。</div>' % "".join(rows))


def render_history(ctx):
    hist = ctx.get("sh_hist") or []
    if not hist:
        return '<div class="empty">上证指数区间数据拉取失败，本章节无数据。</div>'
    rows = []
    for r in reversed(hist):
        d = r["date"]
        label = "%s-%s-%s" % (d[:4], d[4:6], d[6:8]) if len(d) == 8 else d
        rows.append("<tr><td>%s</td><td class=\"n\">%s</td><td class=\"n %s\">%s</td></tr>" % (
            esc(label), fmt_num(r["close"]), cls(r["pct"]), fmt_pct(r["pct"])))
    return ('<div class="tbl-wrap"><table><thead><tr><th>交易日</th>'
            '<th class="n">上证收盘</th><th class="n">涨跌幅</th></tr></thead>'
            '<tbody>%s</tbody></table></div>' % "".join(rows))


def render_news(ctx):
    df = ctx.get("news")
    if df is None:
        return '<div class="empty">当日资讯数据拉取失败（news / major_news），本章节无数据。</div>'
    if "datetime" in df.columns:
        df = df.sort_values("datetime", ascending=False)
    elif "pub_time" in df.columns:
        df = df.sort_values("pub_time", ascending=False)
    items = []
    for _, r in df.head(NEWS_LIMIT).iterrows():
        title = r.get("title") if "title" in df.columns else None
        content = r.get("content") if "content" in df.columns else None
        text = title if title and str(title).strip() else content
        if not text or not str(text).strip():
            continue
        when = r.get("datetime") if "datetime" in df.columns else r.get("pub_time")
        src = r.get("src") if "src" in df.columns else "sina"
        items.append("<li><span class=\"t\">%s · %s</span>%s</li>" % (
            esc(when), esc(src), esc(str(text).strip()[:220])))
    if not items:
        return '<div class="empty">资讯接口返回为空或无有效文本。</div>'
    return ('<ul class="news">%s</ul><div class="note">以上为 Tushare 资讯接口原文摘录，'
            '仅作事实陈列，未做解读或观点加工；标题过长时截断至 220 字。</div>'
            % "".join(items))


def render(ctx):
    breadth = market_breadth(ctx)
    amount = total_amount(ctx)
    amount_chg = amount_change(ctx)
    headline = build_headline(ctx, breadth, amount, amount_chg)
    breadth_html, breadth_src = render_breadth(ctx, breadth)
    indexes = ctx.get("a_indexes") or []
    sh = next((i for i in indexes if i["name"] == "上证指数"), None)

    parts = []
    parts.append('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1.0">')
    parts.append('<title>市场复盘 · %s</title><style>%s</style></head><body><div class="wrap">'
                 % (esc(TRADE_DATE), CSS))
    parts.append('<div class="masthead"><div class="kicker">DAILY MARKET REVIEW · TUSHARE</div>')
    parts.append('<h1>市场复盘 · %s</h1>' % esc(TRADE_DATE))
    parts.append('<div class="meta">数据源：Tushare Pro 接口 ｜ 交易日 %s ｜ '
                 '生成时间 %s ｜ 纯 HTML 输出，无外部依赖</div></div>'
                 % (esc(TRADE_DATE),
                    esc(beijing_now().strftime("%Y-%m-%d %H:%M:%S") + " 北京时间")))

    parts.append('<div class="card tldr"><div class="lead">%s</div>' % esc(headline))
    parts.append('<div class="kpis">')
    parts.append('<div class="kpi"><div class="label">上证指数</div>'
                 '<div class="val %s">%s</div><div class="note %s">%s</div></div>' % (
                     cls(sh["pct"]) if sh else "flat",
                     fmt_num(sh["close"]) if sh else "—",
                     cls(sh["pct"]) if sh else "flat",
                     fmt_pct(sh["pct"]) if sh else "—"))
    parts.append('<div class="kpi"><div class="label">沪深成交额</div>'
                 '<div class="val">%s</div><div class="note">亿元 · daily_info 口径</div></div>'
                 % ("—" if amount is None else fmt_num(amount, 0)))
    parts.append('<div class="kpi"><div class="label">涨 / 跌家数</div>'
                 '<div class="val" style="font-size:17px">%s / %s</div>'
                 '<div class="note">pro.daily 全市场口径</div></div>' % (
                     "—" if breadth["up"] is None else fmt_num(breadth["up"], 0),
                     "—" if breadth["down"] is None else fmt_num(breadth["down"], 0)))
    parts.append('<div class="kpi"><div class="label">成交额环比</div>'
                 '<div class="val %s">%s</div><div class="note">index_daily amount 同字段比值</div></div>' % (
                     cls(amount_chg), "—" if amount_chg is None else fmt_pct(amount_chg)))
    parts.append('</div></div>')

    parts.append(render_status(ctx))

    parts.append('<div class="sec"><span class="no">01</span><h2>A 股主要指数</h2>'
                 '<span class="tip">收盘价与涨跌幅来自 pro.index_daily</span></div>')
    parts.append('<div class="card">%s<div class="note">振幅由当日最高价、最低价与昨收计算。'
                 '数据源：Tushare pro.index_daily，交易日 %s。</div></div>'
                 % (render_index_table(ctx), esc(TRADE_DATE)))

    parts.append('<div class="sec"><span class="no">02</span><h2>全球主要股指</h2>'
                 '<span class="tip">红涨绿跌 · 0 轴居中双向条</span></div>')
    parts.append('<div class="card">%s<div class="note">数据源：Tushare pro.index_global，'
                 '交易日 %s。注意 A/H/亚太为北京当日白天收盘，欧美为当地当日收盘'
                 '（对应北京时间次日凌晨），两者不是同一时点，不可直接做联动归因。</div></div>'
                 % (render_global_bars(ctx), esc(TRADE_DATE)))

    parts.append('<div class="sec"><span class="no">03</span><h2>市场宽度</h2></div>')
    parts.append('<div class="card">%s<div class="note">%s</div></div>' % (
        breadth_html, esc(breadth_src or "市场宽度数据未取到，口径说明不可用。")))

    parts.append('<div class="sec"><span class="no">04</span><h2>沪深市场交易统计</h2></div>')
    parts.append('<div class="card">%s<div class="note">数据源：Tushare pro.daily_info，'
                 '交易日 %s；amount 单位为亿元（Tushare 口径）。</div></div>'
                 % (render_daily_info(ctx), esc(TRADE_DATE)))

    parts.append('<div class="sec"><span class="no">05</span><h2>陆股通 / 港股通资金</h2></div>')
    parts.append('<div class="card">%s</div>' % render_hsgt(ctx))

    parts.append('<div class="sec"><span class="no">06</span><h2>上证指数近期走势</h2>'
                 '<span class="tip">最近 6 个交易日，按时间倒序</span></div>')
    parts.append('<div class="card">%s<div class="note">数据源：Tushare pro.index_daily '
                 '(000001.SH)，用于观察当日涨跌在近期区间中的位置。</div></div>'
                 % render_history(ctx))

    parts.append('<div class="sec"><span class="no">07</span><h2>当日资讯快讯</h2>'
                 '<span class="tip">Tushare 资讯接口原文，不做观点加工</span></div>')
    parts.append('<div class="card">%s</div>' % render_news(ctx))

    parts.append('<div class="sec"><span class="no">08</span><h2>口径说明与免责</h2></div>')
    parts.append('<div class="card"><div class="status">'
                 '· 全部行情、资金与资讯数据均由 Tushare Pro 接口在脚本运行时实时拉取，'
                 'Token 仅来自环境变量 TUSHARE_TOKEN，未写入代码或报告。<br>'
                 '· 任一数据源拉取失败时会打印 [ERROR]，并在对应章节标注缺失，'
                 '不使用记忆值、推测值或占位数字补齐。<br>'
                 '· 不同接口的金额单位、指数口径、时点可能不一致，脚本保留原始值并标注来源，'
                 '未做跨源合并或单位换算（成交额环比除外，使用同一字段比值）。<br>'
                 '· 本报告为公开数据整理，不含投资建议；数据可能存在延迟，请以交易所官方数据为准。'
                 '</div></div>')

    parts.append('<div class="foot">由 gen_report.py 自动生成 · 交易日 %s · '
                 '投资有风险，决策需谨慎。</div>' % esc(TRADE_DATE))
    parts.append('</div></body></html>')
    return "\n".join(parts)


# ---------------------------------------------------------------- 主流程
def main():
    global TRADE_DATE, OUTPUT_FILE
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        print("[FATAL] 环境变量 TUSHARE_TOKEN 未设置。"
              "本地请先 export TUSHARE_TOKEN=<token>；"
              "GitHub Actions 请用 secrets.TUSHARE_TOKEN 注入。", file=sys.stderr)
        return 1
    try:
        ts.set_token(token)
        pro = ts.pro_api()
    except Exception as exc:
        print("[FATAL] Tushare 初始化失败: %s: %s" % (type(exc).__name__, exc),
              file=sys.stderr)
        return 1

    TRADE_DATE = resolve_trade_date(pro)
    OUTPUT_FILE = OUTPUT_PATTERN.format(date=TRADE_DATE)
    print("[INFO] 目标交易日 %s，输出文件 %s，开始拉取数据..."
          % (TRADE_DATE, OUTPUT_FILE))
    ctx = fetch_all(pro)

    if not ctx.get("a_indexes"):
        print("[FATAL] 核心指数数据全部拉取失败，终止生成报告。请检查 Token 权限与接口积分。",
              file=sys.stderr)
        return 1

    try:
        output = render(ctx)
        target = Path.cwd() / OUTPUT_FILE
        target.write_text(output, encoding="utf-8")
    except Exception as exc:
        print("[FATAL] 报告渲染或写入失败: %s: %s" % (type(exc).__name__, exc),
              file=sys.stderr)
        return 1

    print("[DONE] 已覆盖生成 %s（%d 字节）" % (target, target.stat().st_size))
    print("[INFO] 成功 %d 项，失败 %d 项；失败明细见上方 [ERROR] 与报告「数据源状态」章节。"
          % (ctx["ok_count"], len(ERRORS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
