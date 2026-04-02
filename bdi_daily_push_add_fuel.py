"""
NAVGreen 干散货市场日报 — 自动化推送脚本
=========================================
功能：
  1. 从 NAVGreen API 拉取最新 BDI / 各船型指数 / 主要航线租金
  2. 从 NAVGreen Fuel API 拉取全球 383 港口燃油价格（IFO380/VLSFO/LSMGO）
  3. 从 Hellenic Shipping News RSS 抓取当日市场新闻
  4. 生成两份 HTML 日报文件（BDI 日报 + 燃油日报，可存档/分享）
  5. 推送到企业微信（两条消息：BDI 日报 + 燃油日报）/ 钉钉 / Slack webhook

依赖安装：
  pip install requests feedparser jinja2 python-dotenv

配置方式：
  复制 .env.example → .env，填写 webhook 地址即可运行

运行：
  python bdi_daily_push.py            # 立即执行一次
  python bdi_daily_push.py --schedule  # 每天定时执行（需安装 schedule: pip install schedule）
"""

import os
import sys
import json
import logging
import datetime
import argparse
import requests
from pathlib import Path
from typing import Optional

# ── 可选依赖，按需导入 ──────────────────────────────────────────────────────
try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False
    print("⚠️  feedparser 未安装，跳过新闻抓取。运行: pip install feedparser")

try:
    from jinja2 import Template
    HAS_JINJA2 = True
except ImportError:
    HAS_JINJA2 = False
    print("⚠️  jinja2 未安装，使用内置模板引擎。运行: pip install jinja2")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── 日志配置 ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bdi_report")

# ══════════════════════════════════════════════════════════════════════════════
# 1. 配置区（优先读取环境变量 / .env 文件）
# ══════════════════════════════════════════════════════════════════════════════

class Config:
    # NAVGreen API — BDI 指数
    NAVGREEN_API = "https://miniapi.navgreen.cn/api/data/baltic_exchange_index"
    API_LIMIT    = 10           # 拉取最近 N 条（取前两条算涨跌幅）

    # NAVGreen API — 燃油价格（需要登录 token）
    # token 获取：登录 vip.navgreen.cn → DevTools → Application → LocalStorage → 复制 "token" 字段
    # 存入 GitHub Secrets: NAVGREEN_TOKEN；有效期约 30 天，过期后重新登录复制
    FUEL_API       = "https://miniapi.navgreen.cn/api/vessel/bunkers/prices"
    FUEL_LIMIT     = 200        # 单批请求上限（total≈1097，自动分批）
    NAVGREEN_TOKEN = os.getenv("NAVGREEN_TOKEN", "")

    # 推送渠道 webhook（留空则跳过对应渠道）
    WECOM_WEBHOOK   = os.getenv("WECOM_WEBHOOK", "")     # 企业微信机器人
    DINGTALK_WEBHOOK = os.getenv("DINGTALK_WEBHOOK", "") # 钉钉机器人
    SLACK_WEBHOOK   = os.getenv("SLACK_WEBHOOK", "")     # Slack incoming webhook
    FEISHU_WEBHOOK  = os.getenv("FEISHU_WEBHOOK", "")    # 飞书机器人

    # 邮件推送（可选）
    SMTP_HOST   = os.getenv("SMTP_HOST", "smtp.qq.com")
    SMTP_PORT   = int(os.getenv("SMTP_PORT") or "465")
    SMTP_USER   = os.getenv("SMTP_USER", "")
    SMTP_PASS   = os.getenv("SMTP_PASS", "")
    EMAIL_FROM  = os.getenv("EMAIL_FROM", "")
    EMAIL_TO    = os.getenv("EMAIL_TO", "")  # 逗号分隔多个收件人

    # 报告输出目录
    OUTPUT_DIR  = Path(os.getenv("OUTPUT_DIR", "./reports"))

    # 新闻源 RSS（用于抓取市场动态标题）
    NEWS_RSS_URLS = [
        "https://www.hellenicshippingnews.com/category/dry-bulk-market/feed/",
        "https://splash247.com/category/sector/dry-bulk/feed/",
    ]
    MAX_NEWS = 4  # 最多展示几条新闻


# ══════════════════════════════════════════════════════════════════════════════
# 2. 数据获取
# ══════════════════════════════════════════════════════════════════════════════

def fetch_bdi_data() -> dict:
    """从 NAVGreen API 拉取最新指数数据，返回 today / yesterday 两条记录。"""
    log.info(f"拉取 NAVGreen API: {Config.NAVGREEN_API}")
    resp = requests.get(
        Config.NAVGREEN_API,
        params={"limit": Config.API_LIMIT},
        timeout=15,
    )
    resp.raise_for_status()
    raw = resp.json()

    # 过滤掉空白记录（周末 / 节假日）
    rows = [r for r in raw.get("data", []) if r.get("BDI") not in ("", None)]
    if len(rows) < 2:
        raise ValueError("API 返回有效数据不足 2 条，请检查接口状态")

    today_raw = rows[0]
    prev_raw  = rows[1]

    def safe(val):
        """将空字符串转为 None，数字原样返回。"""
        return None if val in ("", None) else float(val)

    def chg_pct(cur, pre):
        """计算涨跌幅，返回 (float | None, str)。"""
        if cur is None or pre is None or pre == 0:
            return None, "n/a"
        pct = (cur - pre) / pre * 100
        sign = "+" if pct >= 0 else ""
        return pct, f"{sign}{pct:.1f}%"

    # 主要指数
    indices = {}
    for key in ["BDI", "BCI", "BPI", "BSI", "BHSI"]:
        cur = safe(today_raw.get(key))
        pre = safe(prev_raw.get(key))
        diff = (cur - pre) if (cur is not None and pre is not None) else None
        pct_val, pct_str = chg_pct(cur, pre)
        indices[key] = {
            "val": cur,
            "prev": pre,
            "diff": diff,
            "pct": pct_val,
            "pct_str": pct_str,
            "direction": ("up" if (pct_val or 0) >= 0 else "dn") if pct_val is not None else "neu",
        }

    # 航线 TCE
    route_map = {
        "C2-TCE":  ("C2 图巴朗→鹿特丹", "铁矿石 大西洋线"),
        "C3-TCE":  ("C3 图巴朗→青岛",   "铁矿石 巴西→中国"),
        "C5-TCE":  ("C5 西澳→青岛",     "铁矿石 太平洋线"),
        "C17-TCE": ("C17 萨尔达尼亚→青岛", "铁矿石 南非→中国"),
        "C5TC":    ("5TC 平均 (Cape综合)", ""),
        "P2A_82":  ("P2A 欧洲大陆→远东", ""),
        "P3A_82":  ("P3A 日本太平洋轮回", ""),
        "P5TC":    ("P5TC Panamax综合",   ""),
        "S2":      ("S2 欧洲大陆→远东",  ""),
        "S3TC_63": ("S3TC 63K综合",      ""),
    }
    routes = {}
    for api_key, (label, sublabel) in route_map.items():
        cur = safe(today_raw.get(api_key))
        pre = safe(prev_raw.get(api_key))
        pct_val, pct_str = chg_pct(cur, pre)
        routes[api_key] = {
            "label": label,
            "sublabel": sublabel,
            "val": cur,
            "pct": pct_val,
            "pct_str": pct_str,
            "direction": ("up" if (pct_val or 0) >= 0 else "dn") if pct_val is not None else "neu",
        }

    # ── 注意：API 中的 FFADV_* 字段含义未经官方确认 ──────────────────────────
    # 经过交叉验证（MSI/Splash247 2026-03-23 数据），真实 Cape Q2 FFA 约 $30,000/天，
    # 而 FFADVCape_T = 7,994，差距约 4 倍，说明该字段极可能是成交量(Volume)而非价格。
    # 因此不抓取、不展示任何 FFADV_* 字段，避免误导用户。
    # 如后续 NAVGreen 后端确认字段含义，再考虑加回。

    # 最近 5 个交易日走势（用于迷你趋势图）
    trend = []
    for r in rows[:5]:
        if r.get("BDI") not in ("", None):
            trend.append({
                "date": r["date"],
                "BDI":  safe(r.get("BDI")),
                "BCI":  safe(r.get("BCI")),
                "BPI":  safe(r.get("BPI")),
            })

    # 周环比数据（取 5 个交易日前对比，约一周）
    week_ago_raw = rows[4] if len(rows) >= 5 else None
    week_chg = {}
    for key in ["BDI", "BCI", "BPI", "BSI", "BHSI"]:
        cur = safe(today_raw.get(key))
        pre = safe(week_ago_raw.get(key)) if week_ago_raw else None
        pct_val, pct_str = chg_pct(cur, pre)
        week_chg[key] = {"pct": pct_val, "pct_str": pct_str}

    return {
        "date":     today_raw["date"],
        "indices":  indices,
        "routes":   routes,
        "trend":    trend,
        "week_chg": week_chg,
    }


def fetch_news() -> list[dict]:
    """从 RSS 源抓取最新干散货新闻标题。"""
    if not HAS_FEEDPARSER:
        return []

    headers = {"User-Agent": "Mozilla/5.0 (compatible; NAVGreen-BDI-Bot/1.0)"}
    news = []
    for url in Config.NEWS_RSS_URLS:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:Config.MAX_NEWS]:
                news.append({
                    "title":   entry.get("title", ""),
                    "link":    entry.get("link", ""),
                    "summary": entry.get("summary", "")[:200].strip(),
                    "source":  feed.feed.get("title", ""),
                })
            if news:
                break  # 第一个源有结果就不再请求第二个
        except Exception as e:
            log.warning(f"RSS 抓取失败 ({url}): {e}")

    return news[:Config.MAX_NEWS]




# ══════════════════════════════════════════════════════════════════════════════
# 2b. 燃油价格数据获取与分析
# ══════════════════════════════════════════════════════════════════════════════

# 干散货船东最关注的港口（按地区）
_FUEL_KEY_PORTS = [
    # 亚太
    "SINGAPORE", "SHANGHAI", "TIANJIN", "QINGDAO", "HONG KONG",
    "BUSAN", "TOKYO", "KAOHSIUNG", "MUMBAI", "COLOMBO",
    # 中东
    "FUJAIRAH", "ABU DHABI",
    # 欧洲
    "ROTTERDAM", "ANTWERP", "HAMBURG", "PIRAEUS", "ISTANBUL",
    # 美洲 / 非洲
    "HOUSTON", "SANTOS", "DURBAN",
]

_FUEL_FLAGS = {
    "SINGAPORE":"🇸🇬","SHANGHAI":"🇨🇳","TIANJIN":"🇨🇳","QINGDAO":"🇨🇳",
    "HONG KONG":"🇭🇰","BUSAN":"🇰🇷","TOKYO":"🇯🇵","KAOHSIUNG":"🇹🇼",
    "MUMBAI":"🇮🇳","COLOMBO":"🇱🇰","FUJAIRAH":"🇦🇪","ABU DHABI":"🇦🇪",
    "ROTTERDAM":"🇳🇱","ANTWERP":"🇧🇪","HAMBURG":"🇩🇪","PIRAEUS":"🇬🇷",
    "ISTANBUL":"🇹🇷","HOUSTON":"🇺🇸","SANTOS":"🇧🇷","DURBAN":"🇿🇦",
}


def fetch_fuel_data() -> dict | None:
    """分批拉取全量燃油价格，返回分析数据字典。token 未配置时返回 None。

    API: GET /api/vessel/bunkers/prices?limit=N&skip=N
    Header: token = NAVGREEN_TOKEN
    每条记录: {portName, grade(ifo380/vlsfo/lsmgo), price, portDistrict, lastUpdated}
    """
    if not Config.NAVGREEN_TOKEN:
        log.warning("⚠️  NAVGREEN_TOKEN 未配置，跳过燃油数据。请将 token 加入环境变量。")
        return None

    headers  = {"token": Config.NAVGREEN_TOKEN}
    all_recs = []

    try:
        # 首批：获取 total
        r0 = requests.get(Config.FUEL_API,
                          params={"limit": Config.FUEL_LIMIT, "skip": 0},
                          headers=headers, timeout=15)
        r0.raise_for_status()
        j0 = r0.json()
        if j0.get("code") != 200:
            log.error(f"燃油 API 错误: {j0.get('msg')}")
            return None
        total = j0.get("total", 0)
        all_recs.extend(j0.get("data", []))

        # 分批拉余量
        skip = Config.FUEL_LIMIT
        while skip < total:
            rn = requests.get(Config.FUEL_API,
                              params={"limit": Config.FUEL_LIMIT, "skip": skip},
                              headers=headers, timeout=15)
            rn.raise_for_status()
            all_recs.extend(rn.json().get("data", []))
            skip += Config.FUEL_LIMIT

        log.info(f"✅ 燃油数据: {len(all_recs)} 条 / total={total}")
    except Exception as e:
        log.error(f"燃油数据拉取失败: {e}")
        return None

    return _analyze_fuel(all_recs)


def _safe_price(val) -> float | None:
    try:
        v = float(val)
        return v if 50 < v < 5000 else None
    except (TypeError, ValueError):
        return None


def _analyze_fuel(records: list) -> dict:
    """聚合原始记录 → 返回报告所需分析结构。"""
    now_ts = int(datetime.datetime.now().timestamp())

    # ── 按港口聚合三种油品 ──────────────────────────────────────────────────
    pm: dict[str, dict] = {}
    for d in records:
        name  = (d.get("portName") or "").strip().upper()
        grade = (d.get("grade")    or "").lower()
        price = _safe_price(d.get("price"))
        ts    = d.get("lastUpdated")
        if not name or grade not in ("ifo380", "vlsfo", "lsmgo") or not price:
            continue
        if name not in pm:
            pm[name] = {"portName": name,
                        "district": (d.get("portDistrict") or "").strip().upper()}
        pm[name][grade]         = price
        pm[name][f"{grade}_ts"] = ts

    ports = list(pm.values())

    # ── 近 30 天有效港口 ────────────────────────────────────────────────────
    fresh = [p for p in ports if any(
        p.get(f"{g}_ts") and (now_ts - p[f"{g}_ts"]) < 30 * 86400
        for g in ("ifo380", "vlsfo", "lsmgo")
    )]

    def stats(grade: str) -> dict:
        vals = sorted(p[grade] for p in fresh if p.get(grade))
        if not vals:
            return {}
        n = len(vals)
        return {"count": n,
                "min":   round(vals[0], 1),
                "max":   round(vals[-1], 1),
                "avg":   round(sum(vals) / n, 1),
                "p25":   round(vals[int(n * 0.25)], 1),
                "p75":   round(vals[int(n * 0.75)], 1)}

    global_stats = {g: stats(g) for g in ("ifo380", "vlsfo", "lsmgo")}

    # ── 关键港口详情 ─────────────────────────────────────────────────────────
    def ts_fmt(ts) -> str:
        if not ts:
            return "—"
        return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")

    key_ports = []
    for name in _FUEL_KEY_PORTS:
        p = pm.get(name, {})
        ifo   = p.get("ifo380")
        vlsfo = p.get("vlsfo")
        lsmgo = p.get("lsmgo")
        spread_vh = round(vlsfo - ifo, 1) if vlsfo and ifo else None
        spread_lv = round(lsmgo - vlsfo, 1) if lsmgo and vlsfo else None
        key_ports.append({
            "port":       name,
            "flag":       _FUEL_FLAGS.get(name, ""),
            "district":   p.get("district", "—"),
            "ifo380":     ifo,
            "vlsfo":      vlsfo,
            "lsmgo":      lsmgo,
            "spread_vh":  spread_vh,    # VLSFO - IFO380（高低硫价差）
            "spread_lv":  spread_lv,    # LSMGO - VLSFO
            "vlsfo_upd":  ts_fmt(p.get("vlsfo_ts")),
            "ifo380_upd": ts_fmt(p.get("ifo380_ts")),
        })

    # ── 中国港口 ─────────────────────────────────────────────────────────────
    china_ports = sorted(
        [p for p in fresh
         if p.get("district") in ("CHINA", "CHN") and p.get("ifo380")],
        key=lambda x: x["portName"]
    )

    # ── Scrubber 价差排行（VLSFO - IFO380）─────────────────────────────────
    spread_list = [
        {"port": p["portName"],
         "district": p.get("district", ""),
         "spread": round(p["vlsfo"] - p["ifo380"], 1)}
        for p in fresh if p.get("vlsfo") and p.get("ifo380")
    ]
    spread_list.sort(key=lambda x: x["spread"], reverse=True)
    avg_spread = round(sum(s["spread"] for s in spread_list) / len(spread_list), 1)                  if spread_list else 0

    # ── 纯数据驱动的文字观点 ─────────────────────────────────────────────────
    ifo_avg  = global_stats.get("ifo380", {}).get("avg", 0)
    vlsfo_avg = global_stats.get("vlsfo", {}).get("avg", 0)
    lsmgo_avg = global_stats.get("lsmgo", {}).get("avg", 0)

    sg  = pm.get("SINGAPORE", {})
    rtm = pm.get("ROTTERDAM", {})
    fuj = pm.get("FUJAIRAH",  {})

    price_level = "中性" if 650 <= ifo_avg <= 780 else ("偏低" if ifo_avg < 650 else "偏高")
    scrubber_eco = "经济性较好" if avg_spread >= 150 else "经济性一般"

    views = {
        "price_level": {
            "verdict": f"全球均价{price_level}",
            "text": (
                f"IFO380 全球均价 ${ifo_avg}/吨，VLSFO ${vlsfo_avg}/吨，LSMGO ${lsmgo_avg}/吨。"
                f"新加坡 IFO380 ${sg.get('ifo380','—')}，"
                f"鹿特丹 ${rtm.get('ifo380','—')}，"
                f"富查伊拉 ${fuj.get('ifo380','—')}。"
            ),
        },
        "scrubber": {
            "verdict": scrubber_eco,
            "text": (
                f"全球 VLSFO−IFO380 平均价差 ${avg_spread}/吨"
                f"（盈亏平衡参考约 $150/吨）。"
                + (f"价差最大：{spread_list[0]['port']} ${spread_list[0]['spread']}，"
                   f"{spread_list[1]['port']} ${spread_list[1]['spread']}。"
                   if len(spread_list) >= 2 else "")
                + (f"价差最小：{spread_list[-1]['port']} ${spread_list[-1]['spread']}。"
                   if spread_list else "")
            ),
        },
        "regional": {
            "verdict": "亚太价格整体高于欧洲",
            "text": (
                f"富查伊拉（${fuj.get('ifo380','—')}）溢价最高，"
                f"欧洲港口偏低（鹿特丹 ${rtm.get('ifo380','—')}，安特卫普 "
                f"${pm.get('ANTWERP',{}).get('ifo380','—')}）。"
                "印度/巴西港口通常是全球价格洼地，适合途经时补油。"
            ),
        },
    }

    return {
        "date":         (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d"),
        "total":        len(records),
        "fresh_count":  len(fresh),
        "global_stats": global_stats,
        "key_ports":    key_ports,
        "china_ports":  china_ports,
        "spread_list":  spread_list[:10],
        "avg_spread":   avg_spread,
        "views":        views,
    }

# ══════════════════════════════════════════════════════════════════════════════
# 3. HTML 报告生成
# ══════════════════════════════════════════════════════════════════════════════

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>干散货市场日报 — {{ date }}</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Mono:wght@400;500&family=Outfit:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing:border-box; margin:0; padding:0; }
:root {
  --ink:#0f1217; --ink-2:#4a5261; --ink-m:#8a93a3;
  --surface:#ffffff; --sf-soft:#f5f6f8; --sf-mid:#eceef2;
  --accent:#1a3a5c; --accent-l:#e8eef5; --accent-mid:#2d5f96;
  --red:#c0392b; --red-l:#fdf0ee; --amber:#b86c0a; --amber-l:#fdf5e8;
  --green:#1e7f5a; --green-l:#e8f5f0; --border:#dde1e8; --border-s:#c4c9d4;
}
body {
  font-family:'Outfit','PingFang SC','Microsoft YaHei',sans-serif;
  background:#eef0f4; color:var(--ink);
  padding:2.5rem 1.5rem;
  -webkit-print-color-adjust:exact; print-color-adjust:exact;
}
.page { max-width:1000px; margin:0 auto; background:var(--surface);
        border:1px solid var(--border); box-shadow:0 8px 40px rgba(0,0,0,0.08); }
/* Header */
.header { background:var(--accent); padding:2rem 2.8rem 1.6rem;
          position:relative; overflow:hidden; }
.header::before { content:''; position:absolute; top:-70px; right:-70px;
  width:300px; height:300px; border-radius:50%; background:rgba(255,255,255,0.04); }
.header::after  { content:''; position:absolute; bottom:-90px; left:200px;
  width:220px; height:220px; border-radius:50%; background:rgba(255,255,255,0.03); }
.header-top { display:flex; justify-content:space-between;
              align-items:flex-start; margin-bottom:1.2rem; }
.doc-label { font-family:'DM Mono',monospace; font-size:10px; letter-spacing:0.15em;
             color:rgba(255,255,255,0.40); text-transform:uppercase; margin-bottom:0.4rem; }
.doc-title { font-family:'DM Serif Display',serif; font-size:26px; color:#fff; line-height:1.25; }
.doc-title em { font-style:italic; color:rgba(255,255,255,0.65); }
.doc-meta { text-align:right; }
.doc-meta .meta-date { font-family:'DM Mono',monospace; font-size:11px;
                       color:rgba(255,255,255,0.48); letter-spacing:0.05em; }
.doc-meta .meta-ref  { font-size:11px; color:rgba(255,255,255,0.30); margin-top:3px; }
.headline-strip { font-size:12px; color:rgba(255,255,255,0.55); font-style:italic; }
/* Body */
.body { padding:2rem 2.8rem; }
.section-head { display:flex; align-items:center; gap:10px;
                margin-bottom:1rem; margin-top:1.8rem; }
.section-head:first-child { margin-top:0; }
.section-num   { font-family:'DM Mono',monospace; font-size:10px;
                 color:var(--ink-m); letter-spacing:0.1em; min-width:22px; }
.section-label { font-size:10px; font-weight:600; letter-spacing:0.12em;
                 text-transform:uppercase; color:var(--ink-m); white-space:nowrap; }
.section-line  { flex:1; height:1px; background:var(--border); }
/* KPI 指数行 */
.kpi-row { display:grid; grid-template-columns:repeat(5,1fr);
           gap:1px; background:var(--border); border:1px solid var(--border);
           margin-bottom:1.8rem; }
.kpi { background:var(--surface); padding:1rem 1.2rem; }
.kpi.highlight { background:var(--accent-l); }
.kpi .k-label  { font-size:9.5px; font-weight:600; letter-spacing:0.08em;
                 text-transform:uppercase; color:var(--ink-m); margin-bottom:5px; }
.kpi .k-val    { font-family:'DM Mono',monospace; font-size:20px;
                 font-weight:500; color:var(--ink); line-height:1; }
.kpi .k-chg    { font-size:11px; margin-top:4px; }
.kpi .k-bar    { height:3px; border-radius:2px; margin-top:8px;
                 background:var(--sf-mid); overflow:hidden; }
.kpi .k-bar-fill { height:100%; border-radius:2px; }
.up { color:var(--green); } .dn { color:var(--red); } .neu { color:var(--ink-m); }
/* Two-col layout */
.two-col { display:grid; grid-template-columns:1.15fr 1fr; gap:14px; margin-bottom:1.8rem; }
.col-stack { display:flex; flex-direction:column; gap:14px; }
/* Cards */
.card { border:1px solid var(--border); overflow:hidden; margin-bottom:0; }
.card-hd { padding:9px 14px; background:var(--sf-soft);
           border-bottom:1.5px solid var(--border-s);
           display:flex; align-items:center; justify-content:space-between; }
.card-title { font-size:10px; font-weight:600; letter-spacing:0.12em;
              text-transform:uppercase; color:var(--ink-m); }
.card-body  { padding:0; }
/* Route rows */
.sec-lbl { font-size:9px; font-weight:700; letter-spacing:0.10em;
           text-transform:uppercase; color:var(--ink-m);
           padding:8px 14px 5px; border-bottom:1px solid var(--border);
           background:var(--sf-soft); }
.route-row { display:flex; align-items:center; gap:8px; padding:8px 14px;
             border-bottom:1px solid var(--border); }
.route-row:last-child { border-bottom:none; }
.route-name { font-size:11.5px; color:var(--ink-2); flex:1; line-height:1.35; }
.route-sub  { font-size:10px; color:var(--ink-m); display:block; }
.route-val  { font-family:'DM Mono',monospace; font-size:13px;
              font-weight:500; color:var(--ink); min-width:72px; text-align:right; }
.route-chg  { font-size:11px; min-width:54px; text-align:right; }
/* Trend */
.trend-row { display:flex; align-items:center; gap:8px; padding:7px 14px;
             border-bottom:1px solid var(--border); }
.trend-row:last-child { border-bottom:none; }
.tr-date { font-family:'DM Mono',monospace; font-size:10.5px; color:var(--ink-m); width:72px; }
.tr-bdi  { font-family:'DM Mono',monospace; font-size:12px; font-weight:500; width:52px; }
.tr-bci  { font-family:'DM Mono',monospace; font-size:12px; color:var(--accent-mid); width:52px; }
.tr-bar-wrap { flex:1; height:4px; background:var(--sf-mid); border-radius:2px; overflow:hidden; }
.tr-bar { height:100%; border-radius:2px; background:var(--accent-mid); }
/* Week chg */
.week-row { display:flex; justify-content:space-between; align-items:center;
            padding:8px 14px; border-bottom:1px solid var(--border); }
.week-row:last-child { border-bottom:none; }
.week-label { font-size:12px; color:var(--ink-2); }
.week-val   { font-family:'DM Mono',monospace; font-size:13px; font-weight:500; text-align:right; }
.week-note  { font-size:10px; color:var(--ink-m); text-align:right; margin-top:1px; }
/* News */
.news-item { padding:11px 14px; border-bottom:1px solid var(--border); }
.news-item:last-child { border-bottom:none; }
.ni-tag { display:inline-block; font-size:9px; font-weight:700; padding:2px 7px;
          border-radius:2px; margin-bottom:6px; letter-spacing:0.05em; text-transform:uppercase; }
.nt-bull { background:var(--green-l); color:var(--green); border:1px solid #a0d4bc; }
.nt-bear { background:var(--red-l);   color:var(--red);   border:1px solid #e8b4b0; }
.nt-neu  { background:var(--amber-l); color:var(--amber); border:1px solid #e8c990; }
.ni-text { font-size:12px; color:var(--ink-2); line-height:1.75; }
/* Views */
.views-grid { display:grid; grid-template-columns:repeat(3,1fr);
              gap:1px; background:var(--border); border:1px solid var(--border);
              margin-bottom:1.8rem; }
.vc { background:var(--surface); padding:1.1rem 1.2rem; }
.vc-seg { font-size:9.5px; font-weight:600; letter-spacing:0.10em;
          text-transform:uppercase; color:var(--ink-m); margin-bottom:5px; }
.vc-verdict { font-family:'DM Serif Display',serif; font-size:15px; margin-bottom:6px; }
.vc-verdict.bull { color:var(--green); }
.vc-verdict.bear { color:var(--red); }
.vc-verdict.neu  { color:var(--amber); }
.vc-text { font-size:11px; color:var(--ink-2); line-height:1.65; }
/* Footer */
.footer { border-top:1.5px solid var(--border-s); padding:1.1rem 2.8rem;
          display:flex; justify-content:space-between; align-items:center;
          background:var(--sf-soft); }
.footer .f-left  { font-size:10.5px; color:var(--ink-m); line-height:1.6; }
.footer .f-right { font-family:'DM Mono',monospace; font-size:10px;
                   color:var(--ink-m); text-align:right; letter-spacing:0.05em; }
@media(max-width:720px){
  .two-col{grid-template-columns:1fr;}
  .kpi-row{grid-template-columns:repeat(3,1fr);}
  .views-grid{grid-template-columns:1fr;}
  .body{padding:1.5rem;}
}
</style>
</head>
<body>
<div class="page">

<div class="header">
  <div class="header-top">
    <div>
      <div class="doc-label">Dry Bulk Market Intelligence · Daily Report</div>
      <div class="doc-title">干散货市场日报<br><em>Baltic Exchange Daily Briefing</em></div>
    </div>
    <div class="doc-meta">
      <div class="meta-date">{{ date }}</div>
      <div class="meta-ref">BDI {{ indices.BDI.val|int if indices.BDI.val else '—' }}</div>
    </div>
  </div>
  <div class="headline-strip">{{ headline }}</div>
</div>

<div class="body">

<!-- 01 市场指数 -->
<div class="section-head">
  <span class="section-num">01</span>
  <span class="section-label">市场指数概览</span>
  <span class="section-line"></span>
</div>

<div class="kpi-row">
  {% for key, label, color, width_pct in idx_display %}
  <div class="kpi {% if key == 'BDI' %}highlight{% endif %}">
    <div class="k-label">{{ label }}</div>
    <div class="k-val">{{ indices[key].val|int if indices[key].val else '—' }}</div>
    <div class="k-chg {{ indices[key].direction }}">
      {% if indices[key].diff %}
        {{ '↑' if indices[key].direction == 'up' else ('↓' if indices[key].direction == 'dn' else '→') }}
        {{ indices[key].diff|abs|int }} pts &nbsp; {{ indices[key].pct_str }}
      {% else %}—{% endif %}
    </div>
    <div class="k-bar"><div class="k-bar-fill" style="width:{{ width_pct }}%;background:{{ color }};"></div></div>
  </div>
  {% endfor %}
</div>

<!-- 02 航线租金 & 走势 -->
<div class="section-head">
  <span class="section-num">02</span>
  <span class="section-label">主要航线租金 &amp; 近期走势</span>
  <span class="section-line"></span>
</div>

<div class="two-col">
  <div class="card">
    <div class="card-hd">
      <span class="card-title">主要航线租金（TCE $/天）</span>
      <span class="badge b-info" style="margin-left:0;">实时更新</span>
    </div>
    <div class="card-body">
      <div class="sec-lbl">海岬型 Capesize</div>
      {% for key in ['C2-TCE','C3-TCE','C5-TCE','C17-TCE','C5TC'] %}
      {% set r = routes[key] %}
      <div class="route-row">
        <div class="route-name">{{ r.label }}
          {% if r.sublabel %}<span class="route-sub">{{ r.sublabel }}</span>{% endif %}
        </div>
        <div class="route-val">{% if r.val %}${{ '{:,.0f}'.format(r.val) }}{% else %}—{% endif %}</div>
        <div class="route-chg {{ r.direction }}">{{ r.pct_str }}</div>
      </div>
      {% endfor %}
      <div class="sec-lbl">巴拿马型 Panamax</div>
      {% for key in ['P2A_82','P3A_82','P5TC'] %}
      {% set r = routes[key] %}
      <div class="route-row">
        <div class="route-name">{{ r.label }}</div>
        <div class="route-val">{% if r.val %}${{ '{:,.0f}'.format(r.val) }}{% else %}—{% endif %}</div>
        <div class="route-chg {{ r.direction }}">{{ r.pct_str }}</div>
      </div>
      {% endfor %}
      <div class="sec-lbl">灵便型 Supramax</div>
      {% for key in ['S2','S3TC_63'] %}
      {% set r = routes[key] %}
      <div class="route-row">
        <div class="route-name">{{ r.label }}</div>
        <div class="route-val">{% if r.val %}${{ '{:,.0f}'.format(r.val) }}{% else %}—{% endif %}</div>
        <div class="route-chg {{ r.direction }}">{{ r.pct_str }}</div>
      </div>
      {% endfor %}
    </div>
  </div>

  <div class="col-stack">
    <div class="card">
      <div class="card-hd"><span class="card-title">近 5 日走势（BDI / BCI / BPI）</span></div>
      <div class="card-body">
        {% set max_bdi = trend|map(attribute='BDI')|select('ne', None)|list|max %}
        {% for t in trend %}
        <div class="trend-row">
          <span class="tr-date">{{ t.date[5:] }}</span>
          <span class="tr-bdi">{{ t.BDI|int if t.BDI else '—' }}</span>
          <span class="tr-bci">{{ t.BCI|int if t.BCI else '—' }}</span>
          <div class="tr-bar-wrap">
            <div class="tr-bar" style="width:{{ ((t.BDI or 0) / max_bdi * 100)|round }}%;"></div>
          </div>
        </div>
        {% endfor %}
      </div>
    </div>
    <div class="card">
      <div class="card-hd">
        <span class="card-title">周环比（近 5 交易日对比）</span>
        <span class="badge b-neu" style="margin-left:0;">实际数据</span>
      </div>
      <div class="card-body">
        {% for key, label in [('BDI','BDI 综合'),('BCI','BCI 海岬'),('BPI','BPI 巴拿马'),('BSI','BSI 灵便'),('BHSI','BHSI 小灵便')] %}
        {% set w = week_chg[key] %}
        <div class="week-row">
          <span class="week-label">{{ label }}</span>
          <div>
            <div class="week-val {{ 'up' if (w.pct or 0) >= 0 else 'dn' }}">
              {{ ('↑' if (w.pct or 0) >= 0 else '↓') + ' ' + w.pct_str if w.pct_str != 'n/a' else '—' }}
            </div>
            <div class="week-note">vs 5 个交易日前</div>
          </div>
        </div>
        {% endfor %}
      </div>
    </div>
  </div>
</div>

<!-- 03 市场驱动因素 -->
{% if market_analysis %}
<div class="section-head">
  <span class="section-num">03</span>
  <span class="section-label">今日市场驱动因素</span>
  <span class="section-line"></span>
</div>

<div class="card" style="margin-bottom:1.8rem;">
  <div class="card-hd">
    <span class="card-title">新闻 &amp; 市场分析</span>
    <span class="badge b-info" style="margin-left:0;">综合解读</span>
  </div>
  <div class="card-body">
    {% for item in market_analysis %}
    <div class="news-item">
      <span class="ni-tag nt-{{ item.type }}">{{ item.tag }}</span>
      <div class="ni-text">{{ item.text|safe }}</div>
    </div>
    {% endfor %}
  </div>
</div>
{% endif %}

<!-- 04 分船型观点 -->
<div class="section-head">
  <span class="section-num">{{ '04' if market_analysis else '03' }}</span>
  <span class="section-label">分船型市场观点</span>
  <span class="section-line"></span>
</div>

<div class="views-grid">
  <div class="vc">
    <div class="vc-seg">海岬型 Capesize</div>
    <div class="vc-verdict {{ cape_view.direction }}">{{ cape_view.verdict }}</div>
    <div class="vc-text">{{ cape_view.text }}</div>
  </div>
  <div class="vc">
    <div class="vc-seg">巴拿马型 Panamax</div>
    <div class="vc-verdict {{ pmx_view.direction }}">{{ pmx_view.verdict }}</div>
    <div class="vc-text">{{ pmx_view.text }}</div>
  </div>
  <div class="vc">
    <div class="vc-seg">灵便型 Supramax / Handysize</div>
    <div class="vc-verdict {{ supra_view.direction }}">{{ supra_view.verdict }}</div>
    <div class="vc-text">{{ supra_view.text }}</div>
  </div>
</div>

</div><!-- /body -->

<div class="footer">
  <div class="f-left">
    数据来源：NAVGreen Baltic Exchange API &nbsp;·&nbsp;
    新闻：Hellenic Shipping News / Splash247<br>
    <strong>本报告仅供参考，不构成投资建议</strong>
  </div>
  <div class="f-right">
    DRY BULK DAILY<br>
    {{ generated_at }}
  </div>
</div>

</div><!-- /page -->
</body>
</html>"""


def _try_translate(text: str) -> str:
    """尝试将英文翻译为中文，失败则返回原文。"""
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source="auto", target="zh-CN").translate(text[:500]) or text
    except Exception:
        return text


def generate_market_analysis(data: dict, news: list) -> list:
    """生成"今日市场驱动因素"分析条目，结合指数数据与新闻。"""
    idx      = data["indices"]
    routes   = data["routes"]
    week_chg = data.get("week_chg", {})

    bdi_pct  = idx["BDI"]["pct"]  or 0
    bci_pct  = idx["BCI"]["pct"]  or 0
    bpi_pct  = idx["BPI"]["pct"]  or 0
    bsi_pct  = idx["BSI"]["pct"]  or 0
    bhsi_pct = idx["BHSI"]["pct"] or 0
    bci_week = week_chg.get("BCI", {}).get("pct") or 0
    bpi_week = week_chg.get("BPI", {}).get("pct") or 0

    def s(v): return "+" if v >= 0 else ""

    items = []

    # ── 1. Capesize 主驱动：找最大涨跌航线 ───────────────────────────────
    cape_keys = ["C3-TCE", "C5-TCE", "C2-TCE", "C17-TCE", "C5TC"]
    cape_movers = [(k, routes[k]) for k in cape_keys if routes.get(k, {}).get("pct") is not None]
    if cape_movers:
        bk, br = max(cape_movers, key=lambda x: abs(x[1]["pct"]))
        c3_pct = routes.get("C3-TCE", {}).get("pct") or 0
        c5_pct = routes.get("C5-TCE", {}).get("pct") or 0
        if c3_pct > c5_pct + 3:
            direction_note = f"大西洋线显著强于太平洋线（C3 {s(c3_pct)}{c3_pct:.1f}% vs C5 {s(c5_pct)}{c5_pct:.1f}%），关注巴西发货量及中国补库需求。"
        elif c5_pct > c3_pct + 3:
            direction_note = f"太平洋线显著强于大西洋线（C5 {s(c5_pct)}{c5_pct:.1f}% vs C3 {s(c3_pct)}{c3_pct:.1f}%），澳矿发运节奏主导当前市场。"
        else:
            direction_note = f"大西洋与太平洋航线走势较为一致（C3 {s(c3_pct)}{c3_pct:.1f}%，C5 {s(c5_pct)}{c5_pct:.1f}%）。"
        trend_note = f"BCI 近 5 日累计 {s(bci_week)}{bci_week:.1f}%，" + (
            "短期上行趋势延续。" if bci_week > 2 else ("短期弱势持续。" if bci_week < -2 else "周线横盘震荡。")
        )
        item_type = "bull" if bci_pct >= 0 else "bear"
        tag_word  = "利多" if bci_pct >= 0 else "利空"
        items.append({
            "type": item_type,
            "tag":  f"{tag_word} · Capesize",
            "text": (
                f"<strong>{br['label']} 今日 {s(br['pct'])}{br['pct']:.1f}%</strong>，"
                f"海岬型市场{'走强' if bci_pct >= 0 else '走弱'}（BCI {s(bci_pct)}{bci_pct:.1f}%，"
                f"收于 {int(idx['BCI']['val'] or 0)} 点）。"
                f"{direction_note} {trend_note}"
            ),
        })

    # ── 2. 大小船分化 ─────────────────────────────────────────────────────
    avg_small  = (bsi_pct + bhsi_pct) / 2
    divergence = bci_pct - avg_small
    if abs(divergence) > 1.5:
        if divergence > 0:
            div_text = (
                f"<strong>大船强、小船弱——船型分化明显。</strong>"
                f"Capesize {s(bci_pct)}{bci_pct:.1f}%，"
                f"Supramax {s(bsi_pct)}{bsi_pct:.1f}% / Handysize {s(bhsi_pct)}{bhsi_pct:.1f}%。"
                "大宗散货（铁矿石、煤炭）需求拉动大船，次要散货（化肥、粮食、钢材）受贸易政策不确定性影响，小吨位船市况相对疲软。"
            )
        else:
            div_text = (
                f"<strong>小船强、大船弱——船型走势出现分化。</strong>"
                f"Capesize {s(bci_pct)}{bci_pct:.1f}%，"
                f"Supramax {s(bsi_pct)}{bsi_pct:.1f}% / Handysize {s(bhsi_pct)}{bhsi_pct:.1f}%。"
                "次要散货需求回暖带动小型船运费上行，大宗货物需求相对平稳。"
            )
        items.append({"type": "neu", "tag": "分歧 · 大小船分化", "text": div_text})

    # ── 3. Panamax 供需结构 ───────────────────────────────────────────────
    pmx_type = "bull" if bpi_pct > 0 else ("bear" if bpi_pct < 0 else "neu")
    pmx_word = "利多" if bpi_pct > 0 else ("利空" if bpi_pct < 0 else "中性")
    items.append({
        "type": pmx_type,
        "tag":  f"{pmx_word} · Panamax",
        "text": (
            f"<strong>BPI 今日 {s(bpi_pct)}{bpi_pct:.1f}%，近 5 日 {s(bpi_week)}{bpi_week:.1f}%。</strong>"
            "2026 年新船交付压力显著高于往年（约 1,500–1,600 万 DWT，较往年翻近 50%），供给端持续偏松；"
            "需求侧重点关注煤炭与粮食贸易量变化，以及太平洋地区租家询盘动向。"
        ),
    })

    # ── 4. 来自新闻的补充资讯（翻译标题 + 摘要） ──────────────────────────
    for n in news[:2]:
        zh_title   = _try_translate(n["title"])
        zh_summary = _try_translate(n["summary"]) if n.get("summary") else ""
        body = f"<strong>{zh_title}</strong>"
        if zh_summary:
            body += f"。{zh_summary}"
        items.append({
            "type": "neu",
            "tag":  "市场资讯",
            "text": body,
        })

    return items


def build_headline(data: dict) -> str:
    """自动生成日报标题副标题。"""
    idx = data["indices"]
    parts = []

    bdi_chg = idx["BDI"]["pct"]
    if bdi_chg is not None:
        sign = "↑" if bdi_chg >= 0 else "↓"
        parts.append(f"BDI {sign}{abs(bdi_chg):.1f}%")

    # 找出涨跌幅最大的航线
    routes = data["routes"]
    extremes = [(k, v) for k, v in routes.items() if v["pct"] is not None]
    if extremes:
        biggest = max(extremes, key=lambda x: abs(x[1]["pct"]))
        r = biggest[1]
        sign = "+" if r["pct"] >= 0 else ""
        if abs(r["pct"]) > 5:
            parts.append(f"{r['label']} {sign}{r['pct']:.1f}%")

    return " · ".join(parts) if parts else "市场数据已更新"


def generate_views(data: dict) -> tuple:
    """根据实际指数数据自动生成分船型观点。
    所有判断只依赖 API 中含义明确的字段（BDI/BCI/BPI/BSI/BHSI 和各航线 TCE）。
    不引用任何含义未经确认的字段（如 FFADV_*）。
    """
    idx = data["indices"]
    routes = data["routes"]
    week_chg = data.get("week_chg", {})

    bci_pct  = idx["BCI"]["pct"] or 0
    bpi_pct  = idx["BPI"]["pct"] or 0
    bsi_pct  = idx["BSI"]["pct"] or 0
    bhsi_pct = idx["BHSI"]["pct"] or 0

    # 周环比辅助判断（趋势方向）
    bci_week = week_chg.get("BCI", {}).get("pct") or 0
    bpi_week = week_chg.get("BPI", {}).get("pct") or 0

    # ── Cape 观点（仅基于 BCI 日涨跌 + 周涨跌 + C3/C5 方向）──────────────
    c3_pct = routes.get("C3-TCE", {}).get("pct") or 0
    c5_pct = routes.get("C5-TCE", {}).get("pct") or 0

    if bci_pct > 2 or (bci_pct > 0 and bci_week > 3):
        cape_dir, cape_v = "bull", "短期偏多"
    elif bci_pct < -2 or (bci_pct < 0 and bci_week < -3):
        cape_dir, cape_v = "bear", "短期偏弱"
    else:
        cape_dir, cape_v = "neu", "震荡整理"

    # 大西洋与太平洋方向分化提示
    atl_pac_note = ""
    if c3_pct > 5 and c5_pct < 1:
        atl_pac_note = "大西洋航线强于太平洋，关注巴西装港货量。"
    elif c5_pct > 5 and c3_pct < 1:
        atl_pac_note = "太平洋航线偏强，关注澳矿发运节奏。"
    else:
        atl_pac_note = "关注中国铁矿石到港量及钢厂开工率变化。"

    cape_text = (
        f"BCI 今日 {'+' if bci_pct >= 0 else ''}{bci_pct:.1f}%，"
        f"近 5 日 {'+' if bci_week >= 0 else ''}{bci_week:.1f}%。{atl_pac_note}"
    )

    # ── Panamax 观点 ───────────────────────────────────────────────────────
    if bpi_pct > 1.5 or (bpi_pct > 0 and bpi_week > 2):
        pmx_dir, pmx_v = "bull", "温和回升"
    elif bpi_pct < -1.5 or (bpi_pct < 0 and bpi_week < -2):
        pmx_dir, pmx_v = "bear", "承压下行"
    else:
        pmx_dir, pmx_v = "neu", "区间震荡"

    pmx_text = (
        f"BPI 今日 {'+' if bpi_pct >= 0 else ''}{bpi_pct:.1f}%，"
        f"近 5 日 {'+' if bpi_week >= 0 else ''}{bpi_week:.1f}%。"
        "2026 年新船交付压力显著高于往年，供给端偏松；关注煤炭和粮食贸易量变化。"
    )

    # ── Supra/Handy 观点 ───────────────────────────────────────────────────
    avg_small = (bsi_pct + bhsi_pct) / 2
    bsi_week  = week_chg.get("BSI",  {}).get("pct") or 0

    if avg_small > 0.5 or bsi_week > 2:
        supra_dir, supra_v = "bull", "小幅回暖"
    elif avg_small < -0.5 or bsi_week < -2:
        supra_dir, supra_v = "bear", "持续偏弱"
    else:
        supra_dir, supra_v = "neu", "横盘整理"

    supra_text = (
        f"BSI {'+' if bsi_pct >= 0 else ''}{bsi_pct:.1f}%，"
        f"BHSI {'+' if bhsi_pct >= 0 else ''}{bhsi_pct:.1f}%。"
        "次要散货需求受贸易政策不确定性压制，转机在于 Q2 南半球粮食出口旺季启动。"
    )

    return (
        {"direction": cape_dir,  "verdict": cape_v,  "text": cape_text},
        {"direction": pmx_dir,   "verdict": pmx_v,   "text": pmx_text},
        {"direction": supra_dir, "verdict": supra_v, "text": supra_text},
    )


def render_html(data: dict, news: list) -> tuple[str, list, tuple]:
    """渲染完整 HTML 报告，同时返回 market_analysis 和 views 供推送使用。"""
    views = generate_views(data)
    cape_view, pmx_view, supra_view = views
    headline = build_headline(data)

    idx_display = [
        ("BDI",  "BDI 综合",   "#1D9E75", 62),
        ("BCI",  "BCI 海岬型", "#185FA5", 75),
        ("BPI",  "BPI 巴拿马型","#378ADD", 47),
        ("BSI",  "BSI 灵便型", "#BA7517", 32),
        ("BHSI", "BHSI 小灵便","#888",    20),
    ]

    market_analysis = generate_market_analysis(data, news)

    ctx = {
        "date":            data["date"],
        "headline":        headline,
        "indices":         data["indices"],
        "routes":          data["routes"],
        "week_chg":        data["week_chg"],
        "trend":           data["trend"],
        "market_analysis": market_analysis,
        "idx_display":     idx_display,
        "cape_view":       cape_view,
        "pmx_view":        pmx_view,
        "supra_view":      supra_view,
        "generated_at":    datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    if HAS_JINJA2:
        html = Template(HTML_TEMPLATE).render(**ctx)
    else:
        bdi = data["indices"]["BDI"]
        html = f"""<html><body>
<h2>干散货日报 {data['date']}</h2>
<p>BDI: {bdi['val']} ({bdi['pct_str']})</p>
<p>BCI: {data['indices']['BCI']['val']} ({data['indices']['BCI']['pct_str']})</p>
<p>请安装 jinja2 获得完整报告: pip install jinja2</p>
</body></html>"""
    return html, market_analysis, views




FUEL_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>燃油价格日报 — {{ date }}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
     font-size:13px;color:#1a1a18;background:#f5f5f3;padding:20px;}
.wrap{max-width:980px;margin:0 auto;}
.header{background:#fff;border:0.5px solid #e0dfd7;border-radius:12px;padding:16px 20px;
        margin-bottom:12px;display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:8px;}
.h-date{font-size:11px;color:#6b6b65;margin-bottom:4px;}
.h-title{font-size:20px;font-weight:500;}
.h-sub{font-size:12px;color:#6b6b65;margin-top:3px;}
.h-badges{display:flex;gap:6px;flex-wrap:wrap;}
.badge{font-size:10px;padding:3px 8px;border-radius:4px;}
.bg-blue{background:#E6F1FB;color:#0C447C;}
.bg-green{background:#E1F5EE;color:#085041;}
.bg-amber{background:#FAEEDA;color:#633806;}
.bg-gray{background:#f0f0ec;color:#6b6b65;}
.stats-row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:12px;}
.sc{background:#fff;border:0.5px solid #e0dfd7;border-radius:12px;padding:13px 16px;}
.sc-label{font-size:10px;color:#6b6b65;font-weight:500;text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px;}
.sc-val{font-size:26px;font-weight:500;}
.sc-meta{font-size:11px;color:#6b6b65;margin-top:6px;}
.sc-range{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap;}
.r-low{font-size:10px;padding:2px 7px;border-radius:8px;background:#E1F5EE;color:#085041;}
.r-high{font-size:10px;padding:2px 7px;border-radius:8px;background:#FCEBEB;color:#A32D2D;}
.r-mid{font-size:10px;padding:2px 7px;border-radius:8px;background:#f0f0ec;color:#6b6b65;}
.note-box{background:#FAEEDA;border:0.5px solid #f0d090;border-radius:8px;
          padding:9px 13px;font-size:11px;color:#633806;margin-bottom:12px;line-height:1.6;}
.two-col{display:grid;grid-template-columns:1.5fr 1fr;gap:10px;margin-bottom:12px;}
.card{background:#fff;border:0.5px solid #e0dfd7;border-radius:12px;overflow:hidden;}
.card-hd{padding:10px 14px;background:#f8f8f5;border-bottom:0.5px solid #e8e8e3;
         display:flex;align-items:center;justify-content:space-between;}
.card-title{font-size:12px;font-weight:500;}
table{width:100%;border-collapse:collapse;font-size:12px;}
thead th{font-size:10px;font-weight:500;color:#6b6b65;padding:7px 10px;
         border-bottom:0.5px solid #e8e8e3;text-align:right;white-space:nowrap;}
thead th:first-child{text-align:left;}
tbody tr{border-bottom:0.5px solid #f0f0ec;}
tbody tr:last-child{border-bottom:none;}
tbody tr:hover{background:#fafaf8;}
td{padding:6px 10px;vertical-align:middle;text-align:right;}
td:first-child{text-align:left;}
.rg-hd{padding:5px 10px;font-size:10px;font-weight:500;color:#6b6b65;
        background:#f8f8f5;border-bottom:0.5px solid #e8e8e3;}
.port-name{font-weight:500;}
.port-dist{font-size:10px;color:#aaa;}
.ifo-val{color:#185FA5;font-weight:500;}
.vls-val{color:#0F6E56;font-weight:500;}
.lsm-val{color:#854F0B;font-weight:500;}
.spd-val{font-size:11px;}
.spd-high{color:#A32D2D;font-weight:500;}
.spd-mid{color:#854F0B;}
.spd-low{color:#0F6E56;}
.upd-val{font-size:10px;color:#aaa;}
.spread-bar-wrap{padding:12px 14px;}
.si{display:flex;align-items:center;gap:8px;margin-bottom:8px;}
.si:last-child{margin-bottom:0;}
.si-label{font-size:11px;color:#6b6b65;width:140px;flex-shrink:0;}
.si-bar-outer{flex:1;height:8px;background:#f0f0ec;border-radius:4px;overflow:hidden;}
.si-bar-inner{height:100%;border-radius:4px;}
.si-val{font-size:11px;font-weight:500;min-width:44px;text-align:right;}
.views-row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:12px;}
.vc{background:#fff;border:0.5px solid #e0dfd7;border-radius:12px;padding:12px 14px;}
.vc-seg{font-size:10px;font-weight:500;color:#6b6b65;text-transform:uppercase;letter-spacing:.05em;margin-bottom:5px;}
.vc-verdict{font-size:13px;font-weight:500;color:#854F0B;margin-bottom:5px;}
.vc-text{font-size:11px;color:#6b6b65;line-height:1.6;}
.footer{background:#f8f8f5;border-radius:8px;padding:10px 14px;font-size:11px;
        color:#6b6b65;display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;}
.nv{font-weight:500;color:#0F6E56;}
@media(max-width:700px){.stats-row,.two-col,.views-row{grid-template-columns:1fr;}}
</style>
</head>
<body>
<div class="wrap">

<div class="header">
  <div>
    <div class="h-date">{{ date }} · {{ fresh_count }} 港口实时数据</div>
    <div class="h-title">全球船用燃油价格日报</div>
    <div class="h-sub">IFO 380 · VLSFO · LSMGO · 覆盖 {{ total }} 条报价</div>
  </div>
  <div class="h-badges">
    <span class="badge bg-blue">IFO 380 均价 ${{ gs.ifo380.avg }}/吨</span>
    <span class="badge bg-green">VLSFO 均价 ${{ gs.vlsfo.avg }}/吨</span>
    <span class="badge bg-amber">LSMGO 均价 ${{ gs.lsmgo.avg }}/吨</span>
  </div>
</div>

<div class="stats-row">
  <div class="sc" style="border-color:#185FA5;border-width:1.5px;">
    <div class="sc-label">IFO 380 · 高硫重油（HSFO）</div>
    <div class="sc-val" style="color:#185FA5;">${{ gs.ifo380.avg }}<span style="font-size:14px;font-weight:400;color:#6b6b65;"> /吨</span></div>
    <div class="sc-meta">{{ gs.ifo380.count }} 港口均价</div>
    <div class="sc-range">
      <span class="r-low">最低 ${{ gs.ifo380.min }}</span>
      <span class="r-mid">P25–P75: ${{ gs.ifo380.p25 }}–${{ gs.ifo380.p75 }}</span>
      <span class="r-high">最高 ${{ gs.ifo380.max }}</span>
    </div>
  </div>
  <div class="sc" style="border-color:#1D9E75;border-width:1.5px;">
    <div class="sc-label">VLSFO · 极低硫燃油（IMO2020）</div>
    <div class="sc-val" style="color:#0F6E56;">${{ gs.vlsfo.avg }}<span style="font-size:14px;font-weight:400;color:#6b6b65;"> /吨</span></div>
    <div class="sc-meta">{{ gs.vlsfo.count }} 港口均价</div>
    <div class="sc-range">
      <span class="r-low">最低 ${{ gs.vlsfo.min }}</span>
      <span class="r-mid">P25–P75: ${{ gs.vlsfo.p25 }}–${{ gs.vlsfo.p75 }}</span>
      <span class="r-high">最高 ${{ gs.vlsfo.max }}</span>
    </div>
  </div>
  <div class="sc" style="border-color:#BA7517;border-width:1.5px;">
    <div class="sc-label">LSMGO · 低硫船用柴油（ECA）</div>
    <div class="sc-val" style="color:#854F0B;">${{ gs.lsmgo.avg }}<span style="font-size:14px;font-weight:400;color:#6b6b65;"> /吨</span></div>
    <div class="sc-meta">{{ gs.lsmgo.count }} 港口均价</div>
    <div class="sc-range">
      <span class="r-low">最低 ${{ gs.lsmgo.min }}</span>
      <span class="r-mid">P25–P75: ${{ gs.lsmgo.p25 }}–${{ gs.lsmgo.p75 }}</span>
      <span class="r-high">最高 ${{ gs.lsmgo.max }}</span>
    </div>
  </div>
</div>

<div class="note-box">
  ⚠️ <strong>高低硫价差（VLSFO − IFO380）</strong>说明：全球均价差约 <strong>${{ avg_spread }}/吨</strong>。
  价差 ≥ $150 时安装脱硫塔（Scrubber）经济性合理；价差 &lt; $100 时 Scrubber 优势很小，直接加 VLSFO 更划算。
</div>

<div class="two-col">
  <div class="card">
    <div class="card-hd">
      <span class="card-title">主要干散货港口燃油报价（$/吨）</span>
      <span class="badge bg-gray">V-H差=VLSFO−IFO380</span>
    </div>
    <table>
      <thead><tr>
        <th style="text-align:left;">港口</th>
        <th>IFO 380</th><th>VLSFO</th><th>LSMGO</th>
        <th>V-H 价差</th><th>更新</th>
      </tr></thead>
      <tbody>
        {% set regions = [
          ('── 亚太 ──',      ['SINGAPORE','SHANGHAI','TIANJIN','QINGDAO','HONG KONG','BUSAN','TOKYO','MUMBAI','COLOMBO']),
          ('── 中东 ──',      ['FUJAIRAH','ABU DHABI']),
          ('── 欧洲 ──',      ['ROTTERDAM','ANTWERP','HAMBURG','PIRAEUS','ISTANBUL']),
          ('── 美洲/非洲 ──', ['HOUSTON','SANTOS','DURBAN']),
        ] %}
        {% for region_label, port_list in regions %}
        <tr><td colspan="6" class="rg-hd">{{ region_label }}</td></tr>
        {% for name in port_list %}
        {% set p = key_ports_map[name] if name in key_ports_map else {} %}
        {% if p and (p.ifo380 or p.vlsfo) %}
        <tr>
          <td><div class="port-name">{{ p.flag }} {{ p.port|title }}</div>
              <div class="port-dist">{{ p.district }}</div></td>
          <td class="ifo-val">{% if p.ifo380 %}${{ '{:,.0f}'.format(p.ifo380) }}{% else %}—{% endif %}</td>
          <td class="vls-val">{% if p.vlsfo %}${{ '{:,.0f}'.format(p.vlsfo) }}{% else %}—{% endif %}</td>
          <td class="lsm-val">{% if p.lsmgo %}${{ '{:,.0f}'.format(p.lsmgo) }}{% else %}—{% endif %}</td>
          <td class="spd-val {% if p.spread_vh and p.spread_vh >= 180 %}spd-high{% elif p.spread_vh and p.spread_vh >= 120 %}spd-mid{% else %}spd-low{% endif %}">
            {% if p.spread_vh %}+${{ '{:,.0f}'.format(p.spread_vh) }}{% else %}—{% endif %}
          </td>
          <td class="upd-val">{{ p.vlsfo_upd }}</td>
        </tr>
        {% endif %}
        {% endfor %}
        {% endfor %}
      </tbody>
    </table>
  </div>

  <div style="display:flex;flex-direction:column;gap:10px;">
    <div class="card">
      <div class="card-hd">
        <span class="card-title">VLSFO − IFO380 价差排行</span>
        <span class="badge bg-amber">Scrubber 经济性</span>
      </div>
      <div class="spread-bar-wrap">
        <div style="font-size:10px;color:#6b6b65;margin-bottom:8px;">🔴≥$180 🟡$120–$180 🟢&lt;$120</div>
        {% set max_sp = spread_list[0].spread if spread_list else 1 %}
        {% for s in spread_list[:8] %}
        {% set pct = (s.spread / max_sp * 100)|round %}
        {% set color = '#A32D2D' if s.spread >= 180 else ('#BA7517' if s.spread >= 120 else '#1D9E75') %}
        <div class="si">
          <div class="si-label">{{ s.port|title }}</div>
          <div class="si-bar-outer"><div class="si-bar-inner" style="width:{{ pct }}%;background:{{ color }};"></div></div>
          <div class="si-val" style="color:{{ color }};">${{ '{:,.0f}'.format(s.spread) }}</div>
        </div>
        {% endfor %}
        <div style="font-size:10px;color:#aaa;margin-top:8px;">全球均价差：${{ avg_spread }}/吨</div>
      </div>
    </div>

    {% if china_ports %}
    <div class="card">
      <div class="card-hd">
        <span class="card-title">🇨🇳 中国主要港口</span>
        <span class="badge bg-blue">IFO380 $/吨</span>
      </div>
      <table>
        <thead><tr><th style="text-align:left;">港口</th><th>IFO380</th><th>VLSFO</th><th>V-H差</th></tr></thead>
        <tbody>
        {% for p in china_ports[:8] %}
        <tr>
          <td class="port-name">{{ p.portName|title }}</td>
          <td class="ifo-val">{% if p.ifo380 %}${{ '{:,.0f}'.format(p.ifo380) }}{% else %}—{% endif %}</td>
          <td class="vls-val">{% if p.vlsfo %}${{ '{:,.0f}'.format(p.vlsfo) }}{% else %}—{% endif %}</td>
          <td class="spd-val {% if p.get('spread_vh') and p.spread_vh >= 150 %}spd-high{% else %}spd-mid{% endif %}">
            {% if p.get('spread_vh') %}+${{ '{:,.0f}'.format(p.spread_vh) }}{% elif p.vlsfo and p.ifo380 %}+${{ '{:,.0f}'.format(p.vlsfo - p.ifo380) }}{% else %}—{% endif %}
          </td>
        </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
    {% endif %}
  </div>
</div>

<div class="views-row">
  <div class="vc">
    <div class="vc-seg">价格水平</div>
    <div class="vc-verdict">{{ views.price_level.verdict }}</div>
    <div class="vc-text">{{ views.price_level.text }}</div>
  </div>
  <div class="vc">
    <div class="vc-seg">Scrubber 经济性</div>
    <div class="vc-verdict">{{ views.scrubber.verdict }}</div>
    <div class="vc-text">{{ views.scrubber.text }}</div>
  </div>
  <div class="vc">
    <div class="vc-seg">地区价差</div>
    <div class="vc-verdict">{{ views.regional.verdict }}</div>
    <div class="vc-text">{{ views.regional.text }}</div>
  </div>
</div>

<div class="footer">
  <div>燃油价格数据 · 仅供参考</div>
  <span>生成时间：{{ generated_at }}</span>
</div>
</div>
</body>
</html>"""


def render_fuel_html(fuel: dict) -> str:
    """渲染燃油日报 HTML，与 BDI 日报完全独立。"""
    # 建立港口名 → 数据的快速查找字典
    key_ports_map = {p["port"]: p for p in fuel["key_ports"]}

    # china_ports 中注入 spread_vh（模板可能用 p.get('spread_vh')）
    china = []
    for p in fuel.get("china_ports", []):
        row = dict(p)
        if row.get("vlsfo") and row.get("ifo380"):
            row["spread_vh"] = round(row["vlsfo"] - row["ifo380"], 1)
        china.append(row)

    ctx = {
        "date":          fuel["date"],
        "total":         fuel["total"],
        "fresh_count":   fuel["fresh_count"],
        "gs":            fuel["global_stats"],
        "key_ports":     fuel["key_ports"],
        "key_ports_map": key_ports_map,
        "china_ports":   china,
        "spread_list":   fuel["spread_list"],
        "avg_spread":    fuel["avg_spread"],
        "views":         fuel["views"],
        "generated_at":  datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    if HAS_JINJA2:
        return Template(FUEL_HTML_TEMPLATE).render(**ctx)
    else:
        # 极简后备
        gs = fuel["global_stats"]
        return (f"<html><body><h2>燃油日报 {fuel['date']}</h2>"
                f"<p>IFO380 均价: ${gs.get('ifo380',{}).get('avg','—')}/吨</p>"
                f"<p>VLSFO 均价: ${gs.get('vlsfo',{}).get('avg','—')}/吨</p>"
                f"<p>LSMGO 均价: ${gs.get('lsmgo',{}).get('avg','—')}/吨</p>"
                f"<p>请安装 jinja2 获得完整报告</p></body></html>")

# ══════════════════════════════════════════════════════════════════════════════
# 4. 推送渠道
# ══════════════════════════════════════════════════════════════════════════════

def _direction_emoji(direction: str) -> str:
    return {"up": "📈", "dn": "📉", "neu": "➡️"}.get(direction, "")


def html_to_image(html_path: str) -> bytes:
    """用 Playwright 将 HTML 报告截全图，返回 PNG 字节。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 980, "height": 800})
        page.goto(f"file://{html_path}")
        page.wait_for_load_state("networkidle")
        img = page.screenshot(full_page=True)
        browser.close()
    return img


def build_wecom_card(data: dict, report_url: str = "",
                     market_analysis: list = None, views: tuple = None) -> dict:
    """构造企业微信完整版 markdown 消息体。"""
    idx    = data["indices"]
    routes = data["routes"]
    week_chg = data.get("week_chg", {})
    trend  = data.get("trend", [])

    def fmt(v):
        return f"${v:,.0f}" if v else "—"

    def chg(r):
        if not r or r.get("pct") is None:
            return "n/a"
        e = "🔺" if r["direction"] == "up" else ("🔻" if r["direction"] == "dn" else "➡")
        return f"{e} {r['pct_str']}"

    def idx_row(key, label):
        d = idx[key]
        color = "info" if d["direction"] == "up" else ("warning" if d["direction"] == "dn" else "comment")
        val = int(d["val"] or 0)
        return f"| {label} | <font color=\"{color}\">{val}</font> | {d['pct_str']} | {week_chg.get(key,{}).get('pct_str','—')} |"

    bdi = idx["BDI"]
    bdi_color = "info" if bdi["direction"] == "up" else "warning"

    lines = [
        f"# 🚢 干散货市场日报 {data['date']}",
        "",
        f"> **BDI <font color=\"{bdi_color}\">{int(bdi['val'] or 0)}</font>**"
        f"　{_direction_emoji(bdi['direction'])} {bdi['pct_str']}　·　近5日 {week_chg.get('BDI',{}).get('pct_str','—')}",
        "",
        "## 📊 指数总览",
        "| 指数 | 点位 | 日涨跌 | 周涨跌 |",
        "|:---|---:|---:|---:|",
        idx_row("BCI",  "BCI 海岬型"),
        idx_row("BPI",  "BPI 巴拿马型"),
        idx_row("BSI",  "BSI 灵便型"),
        idx_row("BHSI", "BHSI 小灵便"),
        "",
        "## 🛳 主要航线 TCE（$/天）",
        "**— 海岬型 Capesize —**",
        f"- C2 图巴朗→鹿特丹　{fmt(routes.get('C2-TCE',{}).get('val'))}　{chg(routes.get('C2-TCE'))}",
        f"- C3 图巴朗→青岛　　{fmt(routes.get('C3-TCE',{}).get('val'))}　{chg(routes.get('C3-TCE'))}",
        f"- C5 西澳→青岛　　　{fmt(routes.get('C5-TCE',{}).get('val'))}　{chg(routes.get('C5-TCE'))}",
        f"- C17 南非→青岛　　 {fmt(routes.get('C17-TCE',{}).get('val'))}　{chg(routes.get('C17-TCE'))}",
        f"- 5TC 综合平均　　　{fmt(routes.get('C5TC',{}).get('val'))}　{chg(routes.get('C5TC'))}",
        "",
        "**— 巴拿马型 Panamax —**",
        f"- P2A 欧洲→远东　　{fmt(routes.get('P2A_82',{}).get('val'))}　{chg(routes.get('P2A_82'))}",
        f"- P3A 日本太平洋轮回　{fmt(routes.get('P3A_82',{}).get('val'))}　{chg(routes.get('P3A_82'))}",
        f"- P5TC 综合　　　　 {fmt(routes.get('P5TC',{}).get('val'))}　{chg(routes.get('P5TC'))}",
        "",
        "**— 灵便型 Supramax —**",
        f"- S2 欧洲→远东　　 {fmt(routes.get('S2',{}).get('val'))}　{chg(routes.get('S2'))}",
        f"- S3TC 63K综合　　 {fmt(routes.get('S3TC_63',{}).get('val'))}　{chg(routes.get('S3TC_63'))}",
    ]

    # 近5日走势
    if trend:
        lines += ["", "## 📈 近5日走势（BDI / BCI）"]
        for t in trend:
            lines.append(f"- {t['date'][5:]}　BDI {int(t['BDI'] or 0)}　BCI {int(t['BCI'] or 0)}")

    # 市场驱动因素
    if market_analysis:
        lines += ["", "## 📰 今日市场驱动因素"]
        tag_map = {"bull": "利多", "bear": "利空", "neu": "中性"}
        import re
        for item in market_analysis:
            tag  = item["tag"]
            # 去除 HTML 标签
            text = re.sub(r"<[^>]+>", "", item["text"]).strip()
            lines.append(f"**[{tag}]** {text}")
            lines.append("")

    # 三船型观点
    if views:
        cape_view, pmx_view, supra_view = views
        lines += ["## 🔍 分船型观点"]
        for label, v in [("海岬型", cape_view), ("巴拿马型", pmx_view), ("灵便型", supra_view)]:
            color = "info" if v["direction"] == "bull" else ("warning" if v["direction"] == "bear" else "comment")
            lines.append(f"**{label}** <font color=\"{color}\">{v['verdict']}</font>　{v['text']}")

    if report_url:
        lines += ["", f"[查看完整日报]({report_url})"]

    return {
        "msgtype": "markdown",
        "markdown": {"content": "\n".join(lines)},
    }


def push_wecom(data: dict, report_url: str = "", html_path: str = "",
               market_analysis: list = None, views: tuple = None) -> bool:
    """推送到企业微信群机器人：先发报告截图，再发完整文字版。"""
    if not Config.WECOM_WEBHOOK:
        return False

    def _post(payload):
        r = requests.post(Config.WECOM_WEBHOOK, json=payload, timeout=30)
        return r.json()

    img_ok = False

    # ── 1. 图片消息 ────────────────────────────────────────────────────────
    if html_path:
        try:
            import base64, hashlib
            img_bytes = html_to_image(html_path)
            b64 = base64.b64encode(img_bytes).decode()
            md5 = hashlib.md5(img_bytes).hexdigest()
            result = _post({"msgtype": "image", "image": {"base64": b64, "md5": md5}})
            if result.get("errcode") == 0:
                log.info("✅ 企业微信图片推送成功")
                img_ok = True
            else:
                log.warning(f"企业微信图片推送失败: {result}")
        except Exception as e:
            log.warning(f"截图失败，跳过图片消息: {e}")

    # ── 2. 完整文字消息（无论图片是否成功，都发送） ───────────────────────
    try:
        result = _post(build_wecom_card(data, report_url,
                                        market_analysis=market_analysis, views=views))
        if result.get("errcode") == 0:
            log.info("✅ 企业微信文字推送成功")
            return True
        else:
            log.error(f"企业微信文字推送失败: {result}")
            return img_ok  # 图片成功也算部分成功
    except Exception as e:
        log.error(f"企业微信推送异常: {e}")
        return img_ok




def build_fuel_wecom_card(fuel: dict) -> dict:
    """构造企业微信燃油日报 markdown（与 BDI 日报分开发送）。"""
    gs = fuel["global_stats"]
    kp = {p["port"]: p for p in fuel["key_ports"]}

    def fv(v, unit=""):  # format value
        if v is None: return "—"
        return f"${v:,.0f}{unit}"

    def spread_color(v):
        if v is None: return "comment"
        if v >= 180: return "warning"
        if v >= 120: return "comment"
        return "info"

    def spread_tag(v):
        if v is None: return ""
        if v >= 180: return " 🔴"
        if v >= 120: return " 🟡"
        return " 🟢"

    ifo_avg  = gs.get("ifo380", {}).get("avg", "—")
    vls_avg  = gs.get("vlsfo",  {}).get("avg", "—")
    lsm_avg  = gs.get("lsmgo",  {}).get("avg", "—")

    lines = [
        f"# ⛽ 全球燃油日报 · {fuel['date']}",
        f"<font color=\"comment\">覆盖 {fuel['fresh_count']} 港口 · 实时数据</font>",
        "",
        "**全球均价（$/吨）**",
        "| 油品 | 均价 | P25 | P75 |",
        "|:---|---:|---:|---:|",
        f"| IFO 380 | {fv(ifo_avg)} | {fv(gs.get('ifo380',{}).get('p25'))} | {fv(gs.get('ifo380',{}).get('p75'))} |",
        f"| VLSFO   | {fv(vls_avg)} | {fv(gs.get('vlsfo',{}).get('p25'))} | {fv(gs.get('vlsfo',{}).get('p75'))} |",
        f"| LSMGO   | {fv(lsm_avg)} | {fv(gs.get('lsmgo',{}).get('p25'))} | {fv(gs.get('lsmgo',{}).get('p75'))} |",
        "",
        "**主要港口（IFO380 / VLSFO / LSMGO | V-H价差）**",
        "<font color=\"comment\">🔴价差≥$180 🟡$120–$180 🟢<$120</font>",
    ]

    port_display = [
        ("🇸🇬", "SINGAPORE"), ("🇨🇳", "SHANGHAI"), ("🇨🇳", "TIANJIN"),
        ("🇭🇰", "HONG KONG"), ("🇰🇷", "BUSAN"),     ("🇯🇵", "TOKYO"),
        ("🇦🇪", "FUJAIRAH"),  ("🇳🇱", "ROTTERDAM"), ("🇩🇪", "HAMBURG"),
        ("🇬🇷", "PIRAEUS"),   ("🇺🇸", "HOUSTON"),   ("🇧🇷", "SANTOS"),
        ("🇮🇳", "MUMBAI"),    ("🇿🇦", "DURBAN"),
    ]
    for flag, name in port_display:
        p = kp.get(name, {})
        if not p.get("ifo380") and not p.get("vlsfo"):
            continue
        sp = p.get("spread_vh")
        lines.append(
            f"> {flag} **{name.title()}**　"
            f"IFO {fv(p.get('ifo380'))} / VLSFO {fv(p.get('vlsfo'))} / LSMGO {fv(p.get('lsmgo'))}"
            f"　<font color=\"{spread_color(sp)}\">价差 {fv(sp)}{spread_tag(sp)}</font>"
        )

    # Scrubber 经济性观点
    v_sc = fuel.get("views", {}).get("scrubber", {})
    lines += [
        "",
        "**Scrubber 经济性**",
        f"> {v_sc.get('text', '')}",
        "",
        "<font color=\"comment\">燃油价格数据 · 仅供参考</font>",
    ]

    return {"msgtype": "markdown", "markdown": {"content": "\n".join(lines)}}


def push_fuel_wecom(fuel: dict, html_path: str = "") -> bool:
    """推送燃油日报到企业微信（图片 + 文字，与 BDI 日报逻辑保持一致）。"""
    if not Config.WECOM_WEBHOOK or not fuel:
        return False

    def _post(payload):
        r = requests.post(Config.WECOM_WEBHOOK, json=payload, timeout=30)
        return r.json()

    img_ok = False

    # 1. 图片（如果能截图）
    if html_path:
        try:
            import base64, hashlib
            img_bytes = html_to_image(html_path)
            b64 = base64.b64encode(img_bytes).decode()
            md5 = hashlib.md5(img_bytes).hexdigest()
            result = _post({"msgtype": "image", "image": {"base64": b64, "md5": md5}})
            if result.get("errcode") == 0:
                log.info("✅ 燃油日报图片推送成功")
                img_ok = True
            else:
                log.warning(f"燃油日报图片推送失败: {result}")
        except Exception as e:
            log.warning(f"燃油日报截图失败，跳过图片: {e}")

    # 2. 文字 markdown（始终发送）
    try:
        result = _post(build_fuel_wecom_card(fuel))
        if result.get("errcode") == 0:
            log.info("✅ 燃油日报文字推送成功")
            return True
        else:
            log.error(f"燃油日报文字推送失败: {result}")
            return img_ok
    except Exception as e:
        log.error(f"燃油日报推送异常: {e}")
        return img_ok

def push_dingtalk(data: dict, report_url: str = "") -> bool:
    """推送到钉钉群机器人（text 类型）。"""
    if not Config.DINGTALK_WEBHOOK:
        return False

    idx = data["indices"]
    bdi = idx["BDI"]
    bci = idx["BCI"]

    def fmt(v):
        return f"${v:,.0f}" if v else "—"

    c3 = data["routes"].get("C3-TCE", {})
    c5 = data["routes"].get("C5-TCE", {})

    text = (
        f"【干散货日报 {data['date']}】\n"
        f"BDI: {int(bdi['val'] or 0)} ({bdi['pct_str']})\n"
        f"BCI: {int(bci['val'] or 0)} ({bci['pct_str']})\n"
        f"C3 巴西→青岛: {fmt(c3.get('val'))} ({c3.get('pct_str','')})\n"
        f"C5 西澳→青岛: {fmt(c5.get('val'))} ({c5.get('pct_str','')})\n"
    )
    if report_url:
        text += f"完整报告: {report_url}"

    payload = {
        "msgtype": "text",
        "text": {"content": text},
        "at": {"isAtAll": False},
    }
    try:
        r = requests.post(Config.DINGTALK_WEBHOOK, json=payload, timeout=10)
        result = r.json()
        if result.get("errcode") == 0:
            log.info("✅ 钉钉推送成功")
            return True
        else:
            log.error(f"钉钉推送失败: {result}")
            return False
    except Exception as e:
        log.error(f"钉钉推送异常: {e}")
        return False


def push_feishu(data: dict, report_url: str = "") -> bool:
    """推送到飞书机器人（interactive card）。"""
    if not Config.FEISHU_WEBHOOK:
        return False

    idx = data["indices"]
    bdi = idx["BDI"]
    bci = idx["BCI"]
    c3 = data["routes"].get("C3-TCE", {})

    color = "green" if (bdi["pct"] or 0) >= 0 else "red"

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"干散货日报 {data['date']}"},
                "template": color,
            },
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"tag": "lark_md",
                            "content": f"**BDI**\n{int(bdi['val'] or 0)} ({bdi['pct_str']})"}},
                        {"is_short": True, "text": {"tag": "lark_md",
                            "content": f"**BCI 海岬**\n{int(bci['val'] or 0)} ({bci['pct_str']})"}},
                        {"is_short": True, "text": {"tag": "lark_md",
                            "content": f"**C3 巴西→青岛**\n${c3.get('val') or 0:,.0f} ({c3.get('pct_str','')})"}},
                    ],
                },
            ],
        },
    }
    if report_url:
        payload["card"]["elements"].append({
            "tag": "action",
            "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "查看完整日报"},
                         "url": report_url, "type": "primary"}],
        })

    try:
        r = requests.post(Config.FEISHU_WEBHOOK, json=payload, timeout=10)
        result = r.json()
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            log.info("✅ 飞书推送成功")
            return True
        else:
            log.error(f"飞书推送失败: {result}")
            return False
    except Exception as e:
        log.error(f"飞书推送异常: {e}")
        return False


def push_slack(data: dict, report_url: str = "") -> bool:
    """推送到 Slack channel（Block Kit）。"""
    if not Config.SLACK_WEBHOOK:
        return False

    idx = data["indices"]
    bdi = idx["BDI"]

    emoji = ":chart_with_upwards_trend:" if (bdi["pct"] or 0) >= 0 else ":chart_with_downwards_trend:"

    routes_txt = ""
    for key, label in [("C3-TCE", "C3 Brazil→Qingdao"), ("C5-TCE", "C5 WAust→Qingdao"),
                        ("P2A_82", "P2A Cont→FE")]:
        r = data["routes"].get(key, {})
        if r.get("val"):
            e = "🔺" if r["direction"] == "up" else "🔻"
            routes_txt += f"• {label}: *${r['val']:,.0f}* {e}{r['pct_str']}\n"

    blocks = [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"{emoji} Dry Bulk Daily — {data['date']}"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*BDI*\n{int(bdi['val'] or 0)} ({bdi['pct_str']})"},
            {"type": "mrkdwn", "text": f"*BCI*\n{int(idx['BCI']['val'] or 0)} ({idx['BCI']['pct_str']})"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": routes_txt}},
    ]
    if report_url:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"<{report_url}|View Full Report>"}})

    try:
        r = requests.post(Config.SLACK_WEBHOOK, json={"blocks": blocks}, timeout=10)
        if r.status_code == 200:
            log.info("✅ Slack 推送成功")
            return True
        else:
            log.error(f"Slack 推送失败: {r.status_code} {r.text}")
            return False
    except Exception as e:
        log.error(f"Slack 推送异常: {e}")
        return False


def push_email(html_content: str, date_str: str) -> bool:
    """通过 SMTP 发送 HTML 邮件。"""
    if not all([Config.SMTP_USER, Config.SMTP_PASS, Config.EMAIL_FROM, Config.EMAIL_TO]):
        return False

    import smtplib
    import ssl
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"【干散货市场日报】{date_str}"
    msg["From"]    = Config.EMAIL_FROM
    msg["To"]      = Config.EMAIL_TO

    msg.attach(MIMEText(html_content, "html", "utf-8"))

    to_addrs = [a.strip() for a in Config.EMAIL_TO.split(",")]

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(Config.SMTP_HOST, Config.SMTP_PORT, context=ctx) as server:
            server.login(Config.SMTP_USER, Config.SMTP_PASS)
            server.sendmail(Config.EMAIL_FROM, to_addrs, msg.as_string())
        log.info(f"✅ 邮件发送成功 → {Config.EMAIL_TO}")
        return True
    except Exception as e:
        log.error(f"邮件发送失败: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# 5. 主流程
# ══════════════════════════════════════════════════════════════════════════════

def run_once() -> bool:
    """拉取数据 → 生成两份报告 → 推送到各渠道（BDI + 燃油，各一条微信消息）。"""
    log.info("=" * 60)
    log.info("NAVGreen 日报 — 开始执行")

    # ── 1. 拉 BDI 指数数据 ──────────────────────────────────────────────────
    try:
        data = fetch_bdi_data()
        log.info(f"✅ BDI 数据: {data['date']}，BDI={data['indices']['BDI']['val']}")
    except Exception as e:
        log.error(f"❌ BDI 数据获取失败: {e}")
        return False

    # ── 2. 拉燃油价格数据（独立模块，失败不影响 BDI 推送）────────────────────
    fuel_data = None
    try:
        fuel_data = fetch_fuel_data()
        if fuel_data:
            ifo_avg = fuel_data["global_stats"].get("ifo380", {}).get("avg", "—")
            log.info(f"✅ 燃油数据: {fuel_data['fresh_count']} 港口，IFO380 均价 ${ifo_avg}/吨")
        else:
            log.info("燃油数据未获取（NAVGREEN_TOKEN 未配置或接口异常）")
    except Exception as e:
        log.warning(f"燃油数据获取异常（不影响 BDI 推送）: {e}")

    # ── 3. 抓新闻 ─────────────────────────────────────────────────────────────
    news = fetch_news()
    log.info(f"新闻条数: {len(news)}")

    # ── 4. 生成 BDI HTML 并保存 ───────────────────────────────────────────────
    html, market_analysis, views = render_html(data, news)
    Config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    bdi_filename = f"bdi_daily_{data['date']}.html"
    bdi_filepath = Config.OUTPUT_DIR / bdi_filename
    bdi_filepath.write_text(html, encoding="utf-8")
    log.info(f"✅ BDI HTML 报告: {bdi_filepath}")

    # ── 5. 生成燃油 HTML 并保存（如有数据）───────────────────────────────────
    fuel_filepath = None
    if fuel_data:
        try:
            fuel_html = render_fuel_html(fuel_data)
            fuel_filename = f"fuel_daily_{data['date']}.html"
            fuel_filepath = Config.OUTPUT_DIR / fuel_filename
            fuel_filepath.write_text(fuel_html, encoding="utf-8")
            log.info(f"✅ 燃油 HTML 报告: {fuel_filepath}")
        except Exception as e:
            log.warning(f"燃油 HTML 生成失败: {e}")
            fuel_filepath = None

    report_url = os.getenv("REPORT_URL", "")

    # ── 6a. 推送 BDI 日报（企业微信 + 其他渠道）──────────────────────────────
    bdi_results = {
        "wecom":    push_wecom(data, report_url, html_path=str(bdi_filepath.resolve()),
                               market_analysis=market_analysis, views=views),
        "dingtalk": push_dingtalk(data, report_url),
        "feishu":   push_feishu(data, report_url),
        "slack":    push_slack(data, report_url),
        "email":    push_email(html, data["date"]),
    }
    log.info(f"BDI 推送结果: {bdi_results}")

    # ── 6b. 推送燃油日报（仅企业微信，间隔 5 秒避免频率限制）─────────────────
    if fuel_data:
        import time as _time
        _time.sleep(5)
        fuel_path_str = str(fuel_filepath.resolve()) if fuel_filepath else ""
        fuel_ok = push_fuel_wecom(fuel_data, html_path=fuel_path_str)
        log.info(f"燃油推送结果: {'✅ 成功' if fuel_ok else '❌ 失败或未配置'}")

    log.info("=" * 60)
    return True


def run_scheduled():
    """每天定时运行（需要 pip install schedule）。"""
    try:
        import schedule
        import time
    except ImportError:
        log.error("请先安装 schedule: pip install schedule")
        sys.exit(1)

    # 每天北京时间 18:30 执行（波罗的海交易所约 17:00 UTC 发布，北京 01:00，
    # 下午盘 18:30 可以抓到当日最新数据）
    RUN_TIME = os.getenv("REPORT_TIME", "18:30")
    log.info(f"定时任务已启动，每天 {RUN_TIME} 执行")

    schedule.every().day.at(RUN_TIME).do(run_once)

    while True:
        schedule.run_pending()
        import time as t
        t.sleep(60)


# ══════════════════════════════════════════════════════════════════════════════
# 6. CLI 入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NAVGreen BDI 日报自动化推送")
    parser.add_argument("--schedule", action="store_true",
                        help="启动定时模式（每天定时执行）")
    parser.add_argument("--output-dir", default="./reports",
                        help="HTML 报告保存目录（默认 ./reports）")
    parser.add_argument("--time", default="18:30",
                        help="定时执行时间，格式 HH:MM（默认 18:30）")
    args = parser.parse_args()

    Config.OUTPUT_DIR = Path(args.output_dir)
    os.environ["REPORT_TIME"] = args.time

    if args.schedule:
        run_scheduled()
    else:
        success = run_once()
        sys.exit(0 if success else 1)
