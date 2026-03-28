"""
ISOWAY 港口拥堵日报 — Port Congestion Monitor（船视宝版）
=========================================================
数据来源：船视宝 SDC API（svc.data.myvessel.cn）
  - /v1/ports/analytics/dynamic/current/count  按港口返回锚泊/靠泊/预抵数量
  - 认证方式：auth_key = timestamp-rand-appId-md5(timestamp-rand-appId-appSecret)
  - 一次 API 调用，秒级返回，无需长时间采集！

覆盖港口（干散货核心装卸港）：
  澳大利亚：Port Hedland（铁矿石）、Dampier（铁矿石）、Newcastle（煤炭）、Hay Point（焦煤）
  巴西：Tubarao（铁矿石）、Ponta da Madeira（铁矿石）
  南非：Richards Bay（动力煤）
  中国：连云港（铁矿石卸货）

运行：
  python port_congestion_myvessel.py           # 正常模式
  python port_congestion_myvessel.py --demo    # 演示模式（无需API）
  python port_congestion_myvessel.py --gen-workflow  # 生成 workflow
"""

import os, sys, json, time, random, string, hashlib, base64
import logging, datetime, argparse
from pathlib import Path
import requests

try:
    from jinja2 import Template
    HAS_JINJA2 = True
except ImportError:
    HAS_JINJA2 = False

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("port_congestion")


# ══════════════════════════════════════════════════════════════════════════════
# 1. 配置
# ══════════════════════════════════════════════════════════════════════════════
class Config:
    BRAND      = "ISOWAY"

    # 船视宝 SDC API
    MV_BASE    = "https://svc.data.myvessel.cn/sdc"
    MV_APP_ID  = os.getenv("MYVESSEL_APP_ID",     "wxjfkjyxgs_api01")
    MV_SECRET  = os.getenv("MYVESSEL_APP_SECRET",  "0d37b356a792a3ad4d5a6e1f0d961bf9")

    WECOM_WEBHOOK = os.getenv("WECOM_WEBHOOK", "")
    OUTPUT_DIR    = Path(os.getenv("OUTPUT_DIR", "./reports"))
    STATE_FILE    = Path(os.getenv("STATE_FILE", "./reports/.port_state.json"))
    TIMEOUT       = 30

    # 拥堵阈值（与7日均值对比）
    CONGESTION_HIGH = 0.40   # +40%
    CONGESTION_MOD  = 0.15   # +15%
    CONGESTION_LOW  = -0.15  # -15%

    # 港口配置 — portCode 是船视宝的 UN/LOCODE
    PORTS = [
        {
            "id":        "port_hedland",
            "port_code": "AUPHE",
            "name":      "Port Hedland",
            "name_cn":   "黑德兰港",
            "country":   "🇦🇺 澳大利亚",
            "cargo":     "铁矿石",
            "cargo_en":  "Iron Ore",
            "relevance": "西澳→中国/日韩 Cape主力装港",
            "bdi_route": "C5TC",
        },
        {
            "id":        "dampier",
            "port_code": "AUDAM",
            "name":      "Dampier / Cape Lambert",
            "name_cn":   "丹皮尔港",
            "country":   "🇦🇺 澳大利亚",
            "cargo":     "铁矿石",
            "cargo_en":  "Iron Ore",
            "relevance": "西澳 Rio Tinto 主力装港",
            "bdi_route": "C5TC",
        },
        {
            "id":        "newcastle",
            "port_code": "AUNTL",
            "name":      "Newcastle",
            "name_cn":   "纽卡斯尔",
            "country":   "🇦🇺 澳大利亚",
            "cargo":     "动力煤",
            "cargo_en":  "Thermal Coal",
            "relevance": "全球最大煤港 Panamax/Cape主力",
            "bdi_route": "P2A_82",
        },
        {
            "id":        "hay_point",
            "port_code": "AUHPT",
            "name":      "Hay Point / Dalrymple Bay",
            "name_cn":   "黑点港",
            "country":   "🇦🇺 澳大利亚",
            "cargo":     "焦煤",
            "cargo_en":  "Coking Coal",
            "relevance": "昆士兰焦煤主力出口港",
            "bdi_route": "P3A_82",
        },
        {
            "id":        "tubarao",
            "port_code": "BRSSZ",
            "name":      "Tubarao / Praia Mole",
            "name_cn":   "图巴朗港",
            "country":   "🇧🇷 巴西",
            "cargo":     "铁矿石",
            "cargo_en":  "Iron Ore",
            "relevance": "C3 巴西→青岛 Cape标准航线起点",
            "bdi_route": "C3",
        },
        {
            "id":        "ponta_madeira",
            "port_code": "BRPDM",
            "name":      "Ponta da Madeira",
            "name_cn":   "马德拉角港",
            "country":   "🇧🇷 巴西",
            "cargo":     "铁矿石",
            "cargo_en":  "Iron Ore",
            "relevance": "Vale S11D 出口港",
            "bdi_route": "C3",
        },
        {
            "id":        "richards_bay",
            "port_code": "ZARCB",
            "name":      "Richards Bay",
            "name_cn":   "理查兹湾",
            "country":   "🇿🇦 南非",
            "cargo":     "动力煤",
            "cargo_en":  "Thermal Coal",
            "relevance": "南非煤炭主力出口港",
            "bdi_route": "P2A_82",
        },
    ]


# ══════════════════════════════════════════════════════════════════════════════
# 2. 船视宝 API 认证
# ══════════════════════════════════════════════════════════════════════════════
def make_auth_key(app_id: str = None, app_secret: str = None) -> str:
    """
    生成船视宝 auth_key（来自官方技术群确认的算法）：
      timestamp = 当前小时末尾 unix 时间戳（endOf("hour").unix()）
      rand      = 随机字符串（不含 -）
      md5Hash   = md5(f"{timestamp}-{rand}-{appId}-{appSecret}")
      auth_key  = f"{timestamp}-{rand}-{appId}-{md5Hash}"
    """
    app_id     = app_id     or Config.MV_APP_ID
    app_secret = app_secret or Config.MV_SECRET

    # endOf("hour") = 当前小时末尾（:59:59）
    now        = int(time.time())
    hour_start = (now // 3600) * 3600
    timestamp  = hour_start + 3599

    # 随机字符串，不含 -
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

    # MD5 签名
    raw_str  = f"{timestamp}-{rand}-{app_id}-{app_secret}"
    md5_hash = hashlib.md5(raw_str.encode()).hexdigest()

    return f"{timestamp}-{rand}-{app_id}-{md5_hash}"


def mv_post(endpoint: str, payload: dict) -> dict:
    """
    调用船视宝 SDC API（POST form-urlencoded）。
    auth_key 通过 URL query 参数传递。
    """
    auth_key = make_auth_key()
    url = f"{Config.MV_BASE}{endpoint}?auth_key={auth_key}"

    # 把 list 字段 JSON 序列化
    form_data = {}
    for k, v in payload.items():
        form_data[k] = json.dumps(v) if isinstance(v, list) else str(v)

    try:
        r = requests.post(
            url,
            data=form_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=Config.TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"API 请求失败 {endpoint}: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# 3. 数据采集
# ══════════════════════════════════════════════════════════════════════════════
def fetch_port_dynamics() -> list:
    """
    调用 /v1/ports/analytics/dynamic/current/count 获取所有港口实时动态数量。
    返回字段：moor（锚泊-港口）、moorHalfway（锚泊-途中）、berth（靠泊）、
              estimate（预抵）、repair（修理）、portCode、nameCn、nameEn
    """
    port_codes = [p["port_code"] for p in Config.PORTS]
    log.info(f"调用船视宝 API，查询 {len(port_codes)} 个港口动态...")

    payload = {
        "portCodes":    port_codes,
        "vesselType":   ["BULK_CARRIER"],   # 散货船
        "cascadeType":  1,                   # 三级状态全满足时返回
        "dwt":          0,
        "dwt2":         500000,
        "includeFish":  False,
    }

    resp = mv_post("/v1/ports/analytics/dynamic/current/count", payload)

    if resp.get("code") not in (None, 200, "200") or not resp.get("data"):
        log.warning(f"API 返回异常: {resp}")
        return []

    data = resp["data"]
    log.info(f"✅ 船视宝 API 返回 {len(data)} 个港口数据")
    for item in data:
        pc = item.get("portCode", "?")
        log.info(f"  {pc}: 锚泊={item.get('moor',0)} 靠泊={item.get('berth',0)} "
                 f"预抵={item.get('estimate',0)} 途中锚={item.get('moorHalfway',0)}")
    return data


# ══════════════════════════════════════════════════════════════════════════════
# 4. 分析
# ══════════════════════════════════════════════════════════════════════════════
def load_state() -> dict:
    try:
        if Config.STATE_FILE.exists():
            return json.loads(Config.STATE_FILE.read_text())
    except Exception:
        pass
    return {"port_history": {}, "last_push": None}


def save_state(state: dict):
    Config.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    Config.STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def analyze_ports(api_data: list) -> list:
    """
    把 API 返回数据对应到港口配置，计算拥堵指数。
    """
    # 建立 portCode → API数据 的索引
    api_index = {item.get("portCode", "").upper(): item for item in api_data}

    state   = load_state()
    history = state.get("port_history", {})
    results = []

    for port_cfg in Config.PORTS:
        pc   = port_cfg["port_code"].upper()
        item = api_index.get(pc, {})

        # 从 API 取数 — 锚泊 = 港口锚泊 + 途中锚泊
        n_moor_port = item.get("moor",         0) or 0
        n_moor_half = item.get("moorHalfway",  0) or 0
        n_anchored  = n_moor_port + n_moor_half
        n_moored    = item.get("berth",         0) or 0
        n_estimate  = item.get("estimate",      0) or 0
        n_repair    = item.get("repair",        0) or 0
        data_fresh  = bool(item)

        # 7日均值
        hist  = history.get(port_cfg["id"], [])
        avg7  = round(sum(h["anchored"] for h in hist[-7:]) / len(hist[-7:]), 1) if hist else None

        # 拥堵等级
        if avg7 is not None and avg7 > 0:
            ratio = (n_anchored - avg7) / avg7
            congestion = ("high"   if ratio >= Config.CONGESTION_HIGH  else
                          "mod"    if ratio >= Config.CONGESTION_MOD   else
                          "low"    if ratio <= Config.CONGESTION_LOW   else "normal")
        elif n_anchored >= 15:
            congestion = "mod"
        else:
            congestion = "normal"

        # 等待时间估算（锚泊/靠泊轮转）
        est_wait = round(n_anchored * 1.5 / max(n_moored, 1), 1) if n_anchored > 0 else 0

        results.append({
            **port_cfg,
            "n_anchored":    n_anchored,
            "n_moor_port":   n_moor_port,
            "n_moor_half":   n_moor_half,
            "n_moored":      n_moored,
            "n_estimate":    n_estimate,
            "n_repair":      n_repair,
            "avg7":          avg7,
            "congestion":    congestion,
            "est_wait_days": est_wait,
            "data_fresh":    data_fresh,
            # API 返回的港口名（用于核对）
            "api_name_cn":   item.get("nameCn", ""),
            "api_name_en":   item.get("nameEn", ""),
        })

    # 保存今日数据到历史
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    for r in results:
        pid = r["id"]
        if pid not in history: history[pid] = []
        history[pid].append({
            "date": today, "anchored": r["n_anchored"], "moored": r["n_moored"]
        })
        if len(history[pid]) > 30: history[pid] = history[pid][-30:]

    state["port_history"] = history
    save_state(state)

    # 补充趋势数据
    for r in results:
        r["history_7d"] = history.get(r["id"], [])[-7:]

    return results


def _demo_data() -> list:
    """演示模式：按船视宝返回格式模拟数据。"""
    import random as rnd; rnd.seed(42)
    demo_vals = {
        "AUPHE": (14, 3, 6, 8),   # moor, moorHalfway, berth, estimate
        "AUDAM": (4,  1, 4, 5),
        "AUNTL": (28, 6, 8, 4),   # Newcastle 高拥堵
        "AUHPT": (8,  2, 5, 3),
        "BRSSZ": (7,  1, 5, 4),
        "BRPDM": (3,  1, 4, 2),
        "ZARCB": (5,  1, 4, 3),
    }
    result = []
    for port in Config.PORTS:
        pc = port["port_code"].upper()
        m, mh, b, e = demo_vals.get(pc, (5,1,3,2))
        result.append({
            "portCode":    pc,
            "nameCn":      port["name_cn"],
            "nameEn":      port["name"],
            "moor":        m,
            "moorHalfway": mh,
            "berth":       b,
            "estimate":    e,
            "repair":      0,
        })
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 5. HTML 模板
# ══════════════════════════════════════════════════════════════════════════════
PORT_HTML = """<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="UTF-8">
<title>港口拥堵日报 — {{ date }} | {{ brand }}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;
     font-size:13px;color:#1a1a18;background:#f0f4f8;padding:20px;}
.wrap{max-width:1060px;margin:0 auto;}

/* Header */
.header{background:linear-gradient(135deg,#0d2137 0%,#0f3a5c 100%);
        border-radius:14px;padding:18px 24px;margin-bottom:14px;
        display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px;}
.h-date{font-size:11px;color:rgba(255,255,255,.45);margin-bottom:5px;}
.h-title{font-size:22px;font-weight:700;color:#fff;letter-spacing:.01em;}
.h-sub{font-size:12px;color:rgba(255,255,255,.55);margin-top:4px;}
.h-right{display:flex;flex-direction:column;align-items:flex-end;gap:8px;}
.brand-tag{font-size:13px;font-weight:700;color:#fff;padding:4px 14px;border-radius:6px;
           border:1px solid rgba(255,255,255,.2);background:rgba(255,255,255,.08);}
.h-badges{display:flex;gap:6px;flex-wrap:wrap;}
.hb{font-size:10px;padding:3px 9px;border-radius:4px;font-weight:600;border:1px solid;}
.hb-r{background:rgba(226,75,74,.25);color:#f09595;border-color:rgba(226,75,74,.35);}
.hb-o{background:rgba(239,159,39,.25);color:#f2c666;border-color:rgba(239,159,39,.35);}
.hb-g{background:rgba(29,158,117,.2);color:#5dcaa5;border-color:rgba(29,158,117,.3);}
.hb-b{background:rgba(55,138,221,.2);color:#85b7eb;border-color:rgba(55,138,221,.3);}

/* Summary */
.summary-bar{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px;}
.sb{background:#fff;border-radius:10px;padding:14px 16px;border:1px solid #dde5ee;text-align:center;}
.sb-val{font-size:30px;font-weight:700;line-height:1;}
.sb-val.red{color:#a32d2d;}.sb-val.amber{color:#854f0b;}.sb-val.green{color:#085041;}.sb-val.blue{color:#0c447c;}
.sb-label{font-size:11px;color:#8a9db5;margin-top:4px;}

/* Alert */
.dem-alert{background:#fff8e6;border:1px solid #f0c84a;border-radius:10px;
           padding:12px 16px;margin-bottom:14px;display:flex;gap:12px;}
.dem-icon{font-size:18px;flex-shrink:0;margin-top:2px;}
.dem-title{font-size:12px;font-weight:700;color:#7a5200;margin-bottom:4px;}
.dem-text{font-size:11px;color:#8b6500;line-height:1.7;}

/* Port grid */
.port-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px;margin-bottom:14px;}
.pcard{background:#fff;border-radius:12px;overflow:hidden;border:1px solid #dde5ee;}
.pcard.cong-high{border-color:#a32d2d;border-width:2px;}
.pcard.cong-mod{border-color:#854f0b;border-width:1.5px;}
.pcard.cong-low{border-color:#1d9e75;}

.pc-head{padding:12px 14px;display:flex;justify-content:space-between;align-items:flex-start;
         border-bottom:1px solid #eef2f8;}
.pc-name{font-size:14px;font-weight:700;}
.pc-sub{font-size:10px;color:#8a9db5;margin-top:2px;}
.pc-badge{font-size:10px;font-weight:700;padding:2px 9px;border-radius:8px;white-space:nowrap;margin-left:8px;flex-shrink:0;}
.pb-high{background:#feecec;color:#a32d2d;}.pb-mod{background:#fff3d6;color:#854f0b;}
.pb-normal{background:#e8f2fb;color:#0c447c;}.pb-low{background:#e8f8f1;color:#085041;}

/* Stats grid: 4 cells */
.pc-stats{display:grid;grid-template-columns:repeat(4,1fr);border-bottom:1px solid #eef2f8;}
.ps-cell{padding:10px 6px;border-right:1px solid #eef2f8;text-align:center;}
.ps-cell:last-child{border-right:none;}
.ps-label{font-size:9px;color:#8a9db5;margin-bottom:3px;white-space:nowrap;}
.ps-val{font-size:20px;font-weight:700;}
.ps-val.v-high{color:#a32d2d;}.ps-val.v-mod{color:#854f0b;}
.ps-val.v-ok{color:#085041;}.ps-val.v-blue{color:#0c447c;}.ps-val.v-gray{color:#aab8c8;}
.ps-sub{font-size:9px;color:#aab8c8;margin-top:2px;}

/* Trend */
.trend-wrap{padding:10px 14px;border-bottom:1px solid #eef2f8;}
.trend-label{font-size:10px;font-weight:600;color:#8a9db5;text-transform:uppercase;margin-bottom:6px;}
.trend-bars{display:flex;align-items:flex-end;gap:3px;height:32px;}
.tb-day{flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;}
.tb-bar{width:100%;border-radius:2px 2px 0 0;min-height:2px;}
.tb-dt{font-size:8px;color:#aab8c8;}

/* Detail */
.pc-detail{padding:10px 14px;}
.pd-row{display:flex;align-items:center;gap:6px;font-size:11px;margin-bottom:5px;}
.pd-row:last-child{margin-bottom:0;}
.pd-icon{width:16px;text-align:center;font-size:12px;flex-shrink:0;}
.pd-label{color:#8a9db5;width:72px;flex-shrink:0;}
.pd-val{font-weight:500;color:#1a1a18;}
.pd-note{color:#aab8c8;margin-left:3px;font-size:10px;}
.data-stale{font-size:10px;color:#aab8c8;font-style:italic;}

/* Table */
.sum-card{background:#fff;border-radius:12px;overflow:hidden;margin-bottom:14px;border:1px solid #dde5ee;}
.sum-head{padding:10px 16px;background:#f5f8fc;border-bottom:1px solid #e8eef6;
          display:flex;justify-content:space-between;align-items:center;}
.sum-title{font-size:12px;font-weight:600;}
.sum-badge{font-size:10px;padding:2px 8px;border-radius:4px;background:#e8f2fb;color:#0c447c;}
table{width:100%;border-collapse:collapse;font-size:12px;}
thead th{font-size:10px;font-weight:600;color:#8a9db5;padding:8px 10px;text-align:right;
         border-bottom:1px solid #eef2f8;white-space:nowrap;}
thead th:first-child{text-align:left;}
tbody tr{border-bottom:1px solid #f5f7fc;}
tbody tr:last-child{border-bottom:none;}
tbody tr:hover{background:#fafbff;}
tbody tr.high-row{background:#fff8f8;}
td{padding:7px 10px;text-align:right;vertical-align:middle;}
td:first-child{text-align:left;font-weight:600;}
.cp{font-size:10px;font-weight:700;padding:2px 8px;border-radius:8px;display:inline-block;}
.cp-high{background:#feecec;color:#a32d2d;}.cp-mod{background:#fff3d6;color:#854f0b;}
.cp-normal{background:#e8f2fb;color:#0c447c;}.cp-low{background:#e8f8f1;color:#085041;}
.td-high{color:#a32d2d;font-weight:700;}.td-mod{color:#854f0b;font-weight:700;}

.footer{background:#fff;border-radius:10px;padding:10px 16px;font-size:11px;color:#8a9db5;
        display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;border:1px solid #dde5ee;}
.ft-brand{font-weight:700;color:#0d2137;font-size:12px;}
.ft-note{font-size:10px;color:#c0ccd8;margin-top:2px;}
.demo-banner{background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:8px 14px;
             margin-bottom:14px;font-size:11px;color:#92400e;text-align:center;}
@media(max-width:700px){.summary-bar,.port-grid{grid-template-columns:1fr;}}
</style>
</head>
<body><div class="wrap">

{% if demo_mode %}
<div class="demo-banner">⚠️ 演示模式 — 以下数据为模拟数据，仅供展示报告格式。真实数据需配置 MYVESSEL_APP_ID / MYVESSEL_APP_SECRET。</div>
{% endif %}

<!-- Header -->
<div class="header">
  <div>
    <div class="h-date">{{ date }} UTC · 数据：船视宝 SDC API 实时采集 · 散货船锚泊/靠泊</div>
    <div class="h-title">⚓ 干散货港口拥堵日报</div>
    <div class="h-sub">铁矿石 · 煤炭 · 全球主要装港 · 实时锚泊等待 · Demurrage 风险</div>
  </div>
  <div class="h-right">
    <span class="brand-tag">{{ brand }}</span>
    <div class="h-badges">
      {% for b in stat_badges %}<span class="hb {{ b.cls }}">{{ b.text }}</span>{% endfor %}
    </div>
  </div>
</div>

<!-- Summary -->
<div class="summary-bar">
  <div class="sb"><div class="sb-val red">{{ total_anchored }}</div><div class="sb-label">锚泊等待（艘）</div></div>
  <div class="sb"><div class="sb-val blue">{{ total_moored }}</div><div class="sb-label">在港作业（艘）</div></div>
  <div class="sb"><div class="sb-val amber">{{ total_estimate }}</div><div class="sb-label">预计到港（艘）</div></div>
  <div class="sb"><div class="sb-val {% if high_ports > 0 %}red{% else %}green{% endif %}">{{ high_ports }}</div><div class="sb-label">高拥堵港口（个）</div></div>
</div>

{% if dem_alerts %}
<div class="dem-alert">
  <span class="dem-icon">⚠️</span>
  <div>
    <div class="dem-title">Demurrage 风险提示</div>
    <div class="dem-text">{{ dem_alerts }}</div>
  </div>
</div>
{% endif %}

<!-- Port cards -->
<div class="port-grid">
{% for p in ports %}
{% set cong = p.congestion %}
{% set cong_labels = {'high':'🔴 高拥堵','mod':'🟡 中等拥堵','normal':'🔵 正常','low':'🟢 宽松'} %}
{% set val_cls = 'v-high' if cong=='high' else ('v-mod' if cong=='mod' else ('v-ok' if cong=='low' else 'v-blue')) %}
<div class="pcard cong-{{ cong }}">
  <div class="pc-head">
    <div>
      <div class="pc-name">{{ p.country }} {{ p.name_cn }}</div>
      <div class="pc-sub">{{ p.name }} · {{ p.cargo }} · {{ p.relevance }}</div>
    </div>
    <span class="pc-badge pb-{{ cong }}">{{ cong_labels[cong] }}</span>
  </div>

  <!-- 4格数据 -->
  <div class="pc-stats">
    <div class="ps-cell">
      <div class="ps-label">⚓ 锚泊等待</div>
      <div class="ps-val {{ val_cls }}">{{ p.n_anchored }}</div>
      <div class="ps-sub">艘</div>
    </div>
    <div class="ps-cell">
      <div class="ps-label">🚢 在港靠泊</div>
      <div class="ps-val v-blue">{{ p.n_moored }}</div>
      <div class="ps-sub">艘</div>
    </div>
    <div class="ps-cell">
      <div class="ps-label">📍 预计到港</div>
      <div class="ps-val v-gray">{{ p.n_estimate }}</div>
      <div class="ps-sub">艘</div>
    </div>
    <div class="ps-cell">
      <div class="ps-label">📊 7日均值</div>
      <div class="ps-val" style="font-size:16px;">{{ p.avg7 if p.avg7 is not none else '—' }}</div>
      <div class="ps-sub">
        {% if p.avg7 is not none and p.avg7 > 0 %}
          {% set pct = ((p.n_anchored - p.avg7) / p.avg7 * 100)|int %}
          {% if pct > 0 %}↑{{ pct }}%{% elif pct < 0 %}↓{{ -pct }}%{% else %}持平{% endif %}
        {% else %}首次{% endif %}
      </div>
    </div>
  </div>

  <!-- 趋势 -->
  {% if p.history_7d %}
  <div class="trend-wrap">
    <div class="trend-label">近7日锚泊趋势</div>
    <div class="trend-bars">
      {% set max_v = namespace(v=1) %}
      {% for h in p.history_7d %}{% if h.anchored > max_v.v %}{% set max_v.v = h.anchored %}{% endif %}{% endfor %}
      {% for h in p.history_7d %}
      {% set bar_h = [(h.anchored / max_v.v * 28)|int, 2]|max %}
      {% set bar_c = '#a32d2d' if p.avg7 and h.anchored >= p.avg7 * 1.4 else ('#ef9f27' if p.avg7 and h.anchored >= p.avg7 * 1.15 else '#378ADD') %}
      <div class="tb-day">
        <div class="tb-bar" style="height:{{ bar_h }}px;background:{{ bar_c }};"></div>
        <div class="tb-dt">{{ h.date[5:] }}</div>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  <div class="pc-detail">
    <div class="pd-row">
      <span class="pd-icon">⏱️</span><span class="pd-label">估计等待</span>
      <span class="pd-val">{{ p.est_wait_days }} 天</span>
      <span class="pd-note">按在港轮转估算</span>
    </div>
    <div class="pd-row">
      <span class="pd-icon">📈</span><span class="pd-label">关联运价</span>
      <span class="pd-val">{{ p.bdi_route }}</span>
      <span class="pd-note">BDI 分项指数</span>
    </div>
    {% if p.n_moor_half > 0 %}
    <div class="pd-row">
      <span class="pd-icon">⛵</span><span class="pd-label">途中锚泊</span>
      <span class="pd-val">{{ p.n_moor_half }} 艘</span>
      <span class="pd-note">（已含在锚泊合计中）</span>
    </div>
    {% endif %}
    {% if not p.data_fresh %}
    <div class="data-stale">⚠️ 暂未获取到该港口数据</div>
    {% endif %}
  </div>
</div>
{% endfor %}
</div>

<!-- Summary Table -->
<div class="sum-card">
  <div class="sum-head">
    <span class="sum-title">各港口拥堵对比汇总</span>
    <span class="sum-badge">{{ date }} UTC · 数据来源：船视宝 SDC API</span>
  </div>
  <table>
    <thead><tr>
      <th style="text-align:left;">港口</th><th style="text-align:left;">货种</th>
      <th>⚓锚泊</th><th>🚢靠泊</th><th>📍预抵</th>
      <th>7日均</th><th>偏差</th><th>估计等待</th><th>拥堵状态</th>
    </tr></thead>
    <tbody>
    {% for p in ports %}
    {% set cong = p.congestion %}
    {% set cong_labels = {'high':'高拥堵','mod':'中等拥堵','normal':'正常','low':'宽松'} %}
    {% set td_cls = 'td-high' if cong=='high' else ('td-mod' if cong=='mod' else '') %}
    <tr {% if cong=='high' %}class="high-row"{% endif %}>
      <td>{{ p.name_cn }}</td>
      <td style="color:#6b7280;">{{ p.cargo }}</td>
      <td class="{{ td_cls }}"><strong>{{ p.n_anchored }}</strong></td>
      <td>{{ p.n_moored }}</td>
      <td style="color:#aab8c8;">{{ p.n_estimate }}</td>
      <td>{{ p.avg7 if p.avg7 is not none else '—' }}</td>
      <td class="{{ td_cls }}">
        {% if p.avg7 is not none and p.avg7 > 0 %}
          {% set pct = ((p.n_anchored - p.avg7) / p.avg7 * 100)|int %}
          {{ '+' if pct>=0 else '' }}{{ pct }}%
        {% else %}—{% endif %}
      </td>
      <td>{{ p.est_wait_days }}天</td>
      <td><span class="cp cp-{{ cong }}">{{ cong_labels[cong] }}</span></td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
</div>

<div class="footer">
  <div>
    <div><span class="ft-brand">{{ brand }}</span> 港口拥堵监测 · 数据：船视宝 SDC API 实时采集</div>
    <div class="ft-note">等待时间为估算值，仅供参考。实际 Demurrage 以 NOR 递交和 Laycan 为准。</div>
  </div>
  <span>生成：{{ generated_at }} UTC{% if demo_mode %} · ⚠️演示模式{% endif %}</span>
</div>

</div></body></html>"""


# ══════════════════════════════════════════════════════════════════════════════
# 6. 渲染 HTML
# ══════════════════════════════════════════════════════════════════════════════
def render_html(port_results: list, generated_at: str, demo_mode: bool = False) -> str:
    today = generated_at[:10]
    total_anchored = sum(p["n_anchored"]  for p in port_results)
    total_moored   = sum(p["n_moored"]    for p in port_results)
    total_estimate = sum(p["n_estimate"]  for p in port_results)
    high_ports     = sum(1 for p in port_results if p["congestion"] == "high")
    mod_ports      = sum(1 for p in port_results if p["congestion"] == "mod")
    normal_ports   = sum(1 for p in port_results if p["congestion"] in ("normal","low"))

    stat_badges = []
    if high_ports: stat_badges.append({"cls":"hb-r","text":f"🔴 高拥堵 {high_ports} 港"})
    if mod_ports:  stat_badges.append({"cls":"hb-o","text":f"🟡 中等 {mod_ports} 港"})
    stat_badges.append({"cls":"hb-g" if not high_ports else "hb-b",
                        "text":f"✅ 正常 {normal_ports} 港"})

    # Demurrage 预警
    dem_parts = []
    for p in port_results:
        if p["congestion"] in ("high","mod"):
            dev = ""
            if p["avg7"] and p["avg7"] > 0:
                pct = int((p["n_anchored"] - p["avg7"]) / p["avg7"] * 100)
                dev = f"（+{pct}%均值）"
            dem_parts.append(
                f"**{p['name_cn']}** {p['n_anchored']}艘锚泊{dev}，"
                f"靠泊 {p['n_moored']} 艘，预计等待 {p['est_wait_days']} 天"
            )
    dem_alerts = "；\n".join(dem_parts) + "。\n建议关注 NOR 递交时间窗口，提前做好 Laytime 计算准备。" if dem_parts else ""

    ctx = dict(
        date=today, brand=Config.BRAND, generated_at=generated_at,
        ports=port_results, stat_badges=stat_badges,
        total_anchored=total_anchored, total_moored=total_moored,
        total_estimate=total_estimate, high_ports=high_ports,
        dem_alerts=dem_alerts, demo_mode=demo_mode,
    )

    if HAS_JINJA2:
        return Template(PORT_HTML).render(**ctx)

    # fallback 纯文本
    lines = [f"<html><body><h2>{Config.BRAND} 港口拥堵日报 {today}</h2>"]
    for p in port_results:
        lines.append(f"<p>{p['name_cn']}: 锚泊{p['n_anchored']} 靠泊{p['n_moored']} [{p['congestion']}]</p>")
    return "\n".join(lines) + "</body></html>"


# ══════════════════════════════════════════════════════════════════════════════
# 7. 截图 + 推送
# ══════════════════════════════════════════════════════════════════════════════
def html_to_image(html_path: str) -> bytes:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1080, "height": 900})
        page.goto(f"file://{html_path}")
        page.wait_for_load_state("networkidle")
        img = page.screenshot(full_page=True)
        browser.close()
    return img


def build_wecom_card(port_results: list, generated_at: str) -> dict:
    today = generated_at[:10]
    cong_emoji = {"high":"🔴","mod":"🟡","normal":"🔵","low":"🟢"}
    cong_label = {"high":"高拥堵","mod":"中等","normal":"正常","low":"宽松"}
    total_anchored = sum(p["n_anchored"] for p in port_results)
    total_estimate = sum(p["n_estimate"] for p in port_results)
    high  = [p for p in port_results if p["congestion"] == "high"]
    mods  = [p for p in port_results if p["congestion"] == "mod"]

    lines = [
        f"# ⚓ {Config.BRAND} 港口拥堵日报 · {today}",
        "> 全球主要干散货装港 · 实时锚泊/靠泊统计 · Demurrage 风险预警",
        "",
        f"**锚泊合计：<font color=\"warning\">{total_anchored} 艘</font>**　"
        f"预计到港：{total_estimate} 艘　"
        + (f"⚠️ 高拥堵：{'、'.join(p['name_cn'] for p in high)}" if high else "✅ 无高拥堵港口"),
        "",
    ]
    if high or mods:
        lines.append("## ⚠️ 重点关注港口")
        for p in high + mods:
            dev = ""
            if p["avg7"] and p["avg7"] > 0:
                pct = int((p["n_anchored"] - p["avg7"]) / p["avg7"] * 100)
                dev = f" | {'↑' if pct>0 else '↓'}{abs(pct)}%均值"
            lines.append(
                f"> {cong_emoji[p['congestion']]} **{p['name_cn']}**（{p['cargo']}）　"
                f"锚泊 **{p['n_anchored']}** 艘 | 靠泊 {p['n_moored']} 艘{dev}　"
                f"预抵 {p['n_estimate']} 艘　估等 **{p['est_wait_days']}天**"
            )
        lines.append("")

    lines += [
        "## 📊 各港口实时概况",
        "| 港口 | 货种 | ⚓锚泊 | 🚢靠泊 | 📍预抵 | 7日均 | 状态 |",
        "|:---|:---|---:|---:|---:|---:|:---:|",
    ]
    for p in port_results:
        avg_s = str(p["avg7"]) if p["avg7"] is not None else "—"
        lines.append(
            f"| {p['name_cn']} | {p['cargo']} | **{p['n_anchored']}** | "
            f"{p['n_moored']} | {p['n_estimate']} | {avg_s} | "
            f"{cong_emoji[p['congestion']]} {cong_label[p['congestion']]} |"
        )
    lines += [
        "", "## 💡 操作建议",
    ]
    if high:
        hnames = "、".join(p["name_cn"] for p in high)
        lines += [
            f"> **{hnames}** 拥堵显著偏高，建议：",
            "> · 确认 NOR 递交时间，做好 Demurrage 预案",
            "> · 评估替代港口可行性，降低滞期敞口",
        ]
    else:
        lines.append("> 当前各港口拥堵程度正常，无需特别关注。")
    lines += [
        "",
        f"<font color=\"comment\">数据：船视宝 SDC API 实时采集 · 仅供参考 · {Config.BRAND}</font>",
    ]
    return {"msgtype": "markdown", "markdown": {"content": "\n".join(lines)}}


def push_wecom(port_results: list, html_path: str, generated_at: str) -> bool:
    if not Config.WECOM_WEBHOOK:
        log.warning("WECOM_WEBHOOK 未配置，跳过推送"); return False
    def _post(payload):
        return requests.post(Config.WECOM_WEBHOOK, json=payload, timeout=30).json()
    img_ok = False
    if html_path:
        try:
            import hashlib as _hl
            img = html_to_image(html_path)
            b64 = __import__('base64').b64encode(img).decode()
            res = _post({"msgtype":"image","image":{"base64":b64,"md5":_hl.md5(img).hexdigest()}})
            if res.get("errcode") == 0: img_ok = True; log.info("✅ 截图推送成功")
        except Exception as e: log.warning(f"截图推送失败: {e}")
    try:
        res = _post(build_wecom_card(port_results, generated_at))
        if res.get("errcode") == 0: log.info("✅ 文字推送成功"); return True
        return img_ok
    except Exception as e: log.error(f"推送异常: {e}"); return img_ok


# ══════════════════════════════════════════════════════════════════════════════
# 8. 主流程
# ══════════════════════════════════════════════════════════════════════════════
def run_once(demo: bool = False) -> bool:
    log.info("=" * 60)
    log.info(f"{Config.BRAND} 港口拥堵日报（船视宝版） — 开始")
    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")

    # 1. 获取数据
    if demo:
        log.info("演示模式：使用模拟数据")
        api_data = _demo_data()
    elif Config.MV_APP_ID and Config.MV_SECRET:
        api_data = fetch_port_dynamics()
        if not api_data:
            log.warning("API 未返回数据，切换演示模式")
            api_data = _demo_data(); demo = True
    else:
        log.warning("未配置 MYVESSEL_APP_ID/SECRET，切换演示模式")
        api_data = _demo_data(); demo = True

    # 2. 分析
    port_results = analyze_ports(api_data)

    # 3. 渲染 HTML
    html = render_html(port_results, now_utc, demo_mode=demo)

    # 4. 保存
    Config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today     = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    html_file = Config.OUTPUT_DIR / f"port_congestion_{today}.html"
    html_file.write_text(html, encoding="utf-8")
    log.info(f"✅ HTML 已保存: {html_file}")

    # 5. 推送
    ok = push_wecom(port_results, str(html_file.resolve()), now_utc)
    log.info(f"推送结果: {'✅ 成功' if ok else '❌ 失败 / WECOM未配置'}")
    log.info("=" * 60)
    return ok


# ══════════════════════════════════════════════════════════════════════════════
# 9. GitHub Actions Workflow
# ══════════════════════════════════════════════════════════════════════════════
WORKFLOW_YML = """\
name: ISOWAY 港口拥堵日报（船视宝版）

on:
  schedule:
    - cron: '0 1 * * *'   # UTC 01:00 = 北京 09:00
  workflow_dispatch:
    inputs:
      demo_mode:
        description: '演示模式（模拟数据，测试推送格式用）'
        type: boolean
        default: false

permissions:
  contents: read

jobs:
  port-congestion:
    name: 港口拥堵日报推送
    runs-on: ubuntu-latest
    timeout-minutes: 5    # 船视宝 API 秒级返回，总耗时 < 3分钟

    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: '3.11', cache: 'pip'}

      - name: 安装依赖
        run: pip install requests jinja2 python-dotenv playwright && playwright install chromium --with-deps

      - name: 恢复7日历史状态
        uses: actions/cache@v4
        with:
          path: reports/.port_state.json
          key: port-state-${{ github.run_id }}
          restore-keys: port-state-

      - run: mkdir -p reports

      - name: 执行港口拥堵日报
        run: |
          ARGS=""
          [ "${{ github.event.inputs.demo_mode }}" = "true" ] && ARGS="--demo"
          python port_congestion_myvessel.py --output-dir ./reports $ARGS
        env:
          WECOM_WEBHOOK:         ${{ secrets.WECOM_WEBHOOK }}
          MYVESSEL_APP_ID:       ${{ secrets.MYVESSEL_APP_ID }}
          MYVESSEL_APP_SECRET:   ${{ secrets.MYVESSEL_APP_SECRET }}

      - name: 保存历史状态
        uses: actions/cache/save@v4
        if: always()
        with:
          path: reports/.port_state.json
          key: port-state-${{ github.run_id }}

      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: port-report-${{ github.run_id }}
          path: reports/port_congestion_*.html
          retention-days: 14
"""

# ══════════════════════════════════════════════════════════════════════════════
# 10. CLI
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"{Config.BRAND} 港口拥堵日报（船视宝版）")
    parser.add_argument("--demo",         action="store_true", help="演示模式（模拟数据）")
    parser.add_argument("--output-dir",   default="./reports")
    parser.add_argument("--gen-workflow", action="store_true", help="生成 GitHub Actions workflow")
    args = parser.parse_args()

    Config.OUTPUT_DIR = Path(args.output_dir)
    Config.STATE_FILE = Config.OUTPUT_DIR / ".port_state.json"

    if args.gen_workflow:
        wf = Path("port_congestion_myvessel.yml")
        wf.write_text(WORKFLOW_YML)
        print(f"✅ Workflow 已生成: {wf}")
        sys.exit(0)

    success = run_once(demo=args.demo)
    sys.exit(0 if success else 1)
