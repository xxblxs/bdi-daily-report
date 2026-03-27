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
      <div class="h-title">干散货市场日报</div>
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
    <div>新闻来源：Hellenic Shipping News / Splash247</div>
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
