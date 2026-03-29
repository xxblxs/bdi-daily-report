"""
NAVGreen 干散货市场日报 — 自动化推送脚本
=========================================
功能：
  1. 从 NAVGreen API 拉取最新 BDI / 各船型指数 / 主要航线租金
  2. 从 Hellenic Shipping News RSS 抓取当日市场新闻
  3. 生成完整 HTML 日报文件（可存档/分享）
  4. 推送到企业微信机器人 / 钉钉机器人 / Slack webhook（按需启用）

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
    # NAVGreen API
    NAVGREEN_API = "https://miniapi.navgreen.cn/api/data/baltic_exchange_index"
    API_LIMIT     = 10          # 拉取最近 N 条（取前两条算涨跌幅）

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
# 3. HTML 报告生成
# ══════════════════════════════════════════════════════════════════════════════

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>干散货市场日报 — {{ date }}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
     font-size:13px;color:#1a1a18;background:#f5f5f3;padding:20px;}
.report{max-width:960px;margin:0 auto;}
.header{background:#fff;border:0.5px solid #e0dfd7;border-radius:12px;padding:16px 20px;
        margin-bottom:12px;display:flex;align-items:flex-start;justify-content:space-between;}
.h-date{font-size:11px;color:#6b6b65;margin-bottom:4px;}
.h-title{font-size:20px;font-weight:500;}
.h-sub{font-size:12px;color:#6b6b65;margin-top:3px;}
.h-src{font-size:10px;padding:3px 8px;background:#E1F5EE;color:#085041;border-radius:4px;}
.indices-row{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:12px;}
.idx{background:#fff;border:0.5px solid #e0dfd7;border-radius:12px;padding:12px 14px;}
.idx.highlight{border-color:#1D9E75;border-width:1.5px;}
.idx-name{font-size:10px;color:#6b6b65;margin-bottom:4px;}
.idx-val{font-size:22px;font-weight:500;}
.idx-chg{font-size:12px;margin-top:3px;}
.up{color:#0F6E56;} .dn{color:#A32D2D;} .neu{color:#6b6b65;}
.mini-bar{height:3px;border-radius:2px;margin-top:6px;background:#f0f0ec;overflow:hidden;}
.mb-fill{height:100%;border-radius:2px;}
.two-col{display:grid;grid-template-columns:1.15fr 1fr;gap:12px;margin-bottom:12px;}
.card{background:#fff;border:0.5px solid #e0dfd7;border-radius:12px;overflow:hidden;}
.card-hd{padding:10px 14px;background:#f8f8f5;border-bottom:0.5px solid #e8e8e3;
         display:flex;align-items:center;justify-content:space-between;}
.card-title{font-size:12px;font-weight:500;}
.badge{font-size:10px;padding:2px 7px;border-radius:10px;}
.b-up{background:#E1F5EE;color:#085041;} .b-dn{background:#FCEBEB;color:#A32D2D;}
.b-neu{background:#f0f0ec;color:#6b6b65;}
.card-body{padding:12px 14px;}
.route-row{display:flex;align-items:center;gap:8px;padding:6px 0;
           border-bottom:0.5px solid #f0f0ec;}
.route-row:last-child{border-bottom:none;}
.route-name{font-size:11px;color:#6b6b65;flex:1;line-height:1.3;}
.route-sub{font-size:10px;color:#aaa;}
.route-val{font-size:13px;font-weight:500;color:#1a1a18;min-width:64px;text-align:right;}
.route-chg{font-size:11px;min-width:50px;text-align:right;}
.sec-lbl{font-size:11px;font-weight:500;color:#6b6b65;padding:8px 0 5px;
          border-bottom:0.5px solid #f0f0ec;margin-top:4px;}
.trend-row{display:flex;align-items:center;gap:6px;padding:5px 0;
           border-bottom:0.5px solid #f0f0ec;}
.trend-row:last-child{border-bottom:none;}
.tr-date{font-size:11px;color:#6b6b65;width:80px;flex-shrink:0;}
.tr-bdi{font-size:12px;font-weight:500;width:50px;}
.tr-bci{font-size:12px;color:#185FA5;width:50px;}
.tr-bar-wrap{flex:1;height:5px;background:#f0f0ec;border-radius:3px;overflow:hidden;}
.tr-bar{height:100%;border-radius:3px;background:#378ADD;}
.week-row{display:flex;justify-content:space-between;align-items:center;padding:7px 0;
          border-bottom:0.5px solid #f0f0ec;}
.week-row:last-child{border-bottom:none;}
.week-label{font-size:12px;color:#6b6b65;}
.week-val{font-size:13px;font-weight:500;}
.week-note{font-size:10px;color:#aaa;margin-top:2px;}
.news-item{padding:12px 14px;border-bottom:0.5px solid #f0f0ec;}
.news-item:last-child{border-bottom:none;}
.ni-tag{display:inline-block;font-size:10px;font-weight:500;padding:2px 8px;
        border-radius:4px;margin-bottom:7px;}
.nt-bull{background:#E1F5EE;color:#085041;}
.nt-bear{background:#FCEBEB;color:#A32D2D;}
.nt-neu{background:#FEF3C7;color:#854F0B;}
.ni-text{font-size:12px;color:#1a1a18;line-height:1.75;}
.view-row{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px;}
.view-card{background:#fff;border:0.5px solid #e0dfd7;border-radius:12px;padding:12px 14px;}
.vc-seg{font-size:10px;font-weight:500;color:#6b6b65;text-transform:uppercase;
         letter-spacing:0.05em;margin-bottom:6px;}
.vc-verdict{font-size:13px;font-weight:500;margin-bottom:6px;}
.verdict-bull{color:#0F6E56;} .verdict-bear{color:#A32D2D;} .verdict-neu{color:#854F0B;}
.vc-text{font-size:11px;color:#6b6b65;line-height:1.6;}
.footer{background:#f8f8f5;border-radius:8px;padding:10px 14px;font-size:11px;
        color:#6b6b65;display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;}
.nv{font-size:12px;font-weight:500;color:#0F6E56;}
@media(max-width:700px){
  .indices-row{grid-template-columns:repeat(3,1fr);}
  .two-col{grid-template-columns:1fr;}
  .view-row{grid-template-columns:1fr;}
}
</style>
</head>
<body>
<div class="report">
  <div class="header">
    <div>
      <div class="h-date">{{ date }}</div>
      <div class="h-title"><img src="data:image/png;base64,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" alt="ISOWAY" style="height:28px;vertical-align:middle;margin-right:8px;"> 干散货市场日报</div>
      <div class="h-sub">BDI {{ indices.BDI.val|int }} &nbsp;·&nbsp; {{ headline }}</div>
    </div>
    <span class="h-src">实时数据</span>
  </div>

  <div class="indices-row">
    {% for key, label, color, width_pct in idx_display %}
    <div class="idx {% if key == 'BDI' %}highlight{% endif %}">
      <div class="idx-name">{{ label }}</div>
      <div class="idx-val">{{ indices[key].val|int if indices[key].val else '—' }}</div>
      <div class="idx-chg {{ indices[key].direction }}">
        {% if indices[key].diff %}
          {{ '↑' if indices[key].direction == 'up' else '↓' }}
          {{ indices[key].diff|abs|int }} pts &nbsp;{{ indices[key].pct_str }}
        {% else %}—{% endif %}
      </div>
      <div class="mini-bar">
        <div class="mb-fill" style="width:{{ width_pct }}%;background:{{ color }};"></div>
      </div>
    </div>
    {% endfor %}
  </div>

  <div class="two-col">
    <div class="card">
      <div class="card-hd">
        <span class="card-title">主要航线租金（TCE $/天）</span>
        <span class="badge b-up">实时更新</span>
      </div>
      <div class="card-body">
        <div class="sec-lbl">— 海岬型 Capesize —</div>
        {% for key in ['C2-TCE','C3-TCE','C5-TCE','C17-TCE','C5TC'] %}
        {% set r = routes[key] %}
        <div class="route-row">
          <div class="route-name">{{ r.label }}<br>
            {% if r.sublabel %}<span class="route-sub">{{ r.sublabel }}</span>{% endif %}
          </div>
          <div class="route-val">{% if r.val %}${{ '{:,.0f}'.format(r.val) }}{% else %}—{% endif %}</div>
          <div class="route-chg {{ r.direction }}">{{ r.pct_str }}</div>
        </div>
        {% endfor %}
        <div class="sec-lbl">— 巴拿马型 Panamax —</div>
        {% for key in ['P2A_82','P3A_82','P5TC'] %}
        {% set r = routes[key] %}
        <div class="route-row">
          <div class="route-name">{{ r.label }}</div>
          <div class="route-val">{% if r.val %}${{ '{:,.0f}'.format(r.val) }}{% else %}—{% endif %}</div>
          <div class="route-chg {{ r.direction }}">{{ r.pct_str }}</div>
        </div>
        {% endfor %}
        <div class="sec-lbl">— 灵便型 Supramax —</div>
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

    <div style="display:flex;flex-direction:column;gap:12px;">
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
          <span class="badge b-neu">实际数据</span>
        </div>
        <div class="card-body">
          {% for key, label in [('BDI','BDI 综合'),('BCI','BCI 海岬'),('BPI','BPI 巴拿马'),('BSI','BSI 灵便'),('BHSI','BHSI 小灵便')] %}
          {% set w = week_chg[key] %}
          <div class="week-row">
            <span class="week-label">{{ label }}</span>
            <div style="text-align:right;">
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

  {% if market_analysis %}
  <div class="card" style="margin-bottom:12px;">
    <div class="card-hd">
      <span class="card-title">今日市场驱动因素 — 新闻 & 分析</span>
      <span class="badge b-up">综合解读</span>
    </div>
    <div class="card-body" style="padding:0;">
      {% for item in market_analysis %}
      <div class="news-item">
        <span class="ni-tag nt-{{ item.type }}">{{ item.tag }}</span>
        <div class="ni-text">{{ item.text|safe }}</div>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  <div class="view-row">
    <div class="view-card">
      <div class="vc-seg">海岬型 Capesize</div>
      <div class="vc-verdict verdict-{{ cape_view.direction }}">{{ cape_view.verdict }}</div>
      <div class="vc-text">{{ cape_view.text }}</div>
    </div>
    <div class="view-card">
      <div class="vc-seg">巴拿马型 Panamax</div>
      <div class="vc-verdict verdict-{{ pmx_view.direction }}">{{ pmx_view.verdict }}</div>
      <div class="vc-text">{{ pmx_view.text }}</div>
    </div>
    <div class="view-card">
      <div class="vc-seg">灵便型 Supramax / Handysize</div>
      <div class="vc-verdict verdict-{{ supra_view.direction }}">{{ supra_view.verdict }}</div>
      <div class="vc-text">{{ supra_view.text }}</div>
    </div>
  </div>

  <div class="footer">
    <div>ISOWAY 市场参考资讯 · 仅供参考</div>
    <span>生成时间：{{ generated_at }}</span>
  </div>
</div>
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
    """拉取数据 → 生成报告 → 推送所有渠道。"""
    log.info("=" * 60)
    log.info("NAVGreen 干散货日报 — 开始执行")

    # 1. 拉数据
    try:
        data = fetch_bdi_data()
        log.info(f"✅ 数据获取成功: {data['date']}，BDI={data['indices']['BDI']['val']}")
    except Exception as e:
        log.error(f"❌ 数据获取失败: {e}")
        return False

    # 2. 抓新闻
    news = fetch_news()
    log.info(f"新闻条数: {len(news)}")

    # 3. 生成 HTML
    html, market_analysis, views = render_html(data, news)

    # 4. 保存文件
    Config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"bdi_daily_{data['date']}.html"
    filepath = Config.OUTPUT_DIR / filename
    filepath.write_text(html, encoding="utf-8")
    log.info(f"✅ HTML 报告已保存: {filepath}")

    report_url = ""  # 如果有 CDN/对象存储可以填真实 URL

    # 5. 推送各渠道
    results = {
        "wecom":    push_wecom(data, report_url, html_path=str(filepath.resolve()),
                               market_analysis=market_analysis, views=views),
        "dingtalk": push_dingtalk(data, report_url),
        "feishu":   push_feishu(data, report_url),
        "slack":    push_slack(data, report_url),
        "email":    push_email(html, data["date"]),
    }
    active = {k: v for k, v in results.items() if v}
    skipped = {k for k, v in results.items() if v is False and not any([
        Config.WECOM_WEBHOOK if k == "wecom" else "",
        Config.DINGTALK_WEBHOOK if k == "dingtalk" else "",
        Config.FEISHU_WEBHOOK if k == "feishu" else "",
        Config.SLACK_WEBHOOK if k == "slack" else "",
        Config.SMTP_USER if k == "email" else "",
    ])}

    log.info(f"推送结果: {results}")
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
