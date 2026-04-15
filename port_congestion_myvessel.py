"""
ISOWAY 港口拥堵日报 — 船视宝版 v2.0
=====================================
数据来源：船视宝 SDC API (svc.data.myvessel.cn)
认证方式：GET /ada/oauth/token?client_id=&client_secret=&grant_type=client_credentials
          → Bearer Token，有效期 3600 秒，自动刷新

覆盖港口（23个，干散货核心航线）：
  装港：澳大利亚 5 | 巴西 2 | 南非 1 | 印尼 5 | 西非 2
  卸港：中国 6 | 印度 2

运行：
  python port_congestion_myvessel.py              # 正常模式
  python port_congestion_myvessel.py --demo       # 演示模式（无需API）
  python port_congestion_myvessel.py --gen-workflow  # 生成 GitHub Actions workflow
"""

import os, sys, json, time, hashlib, base64, logging, datetime, argparse
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
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("port_congestion")


# ══════════════════════════════════════════════════════════════════════════════
# 1. 配置
# ══════════════════════════════════════════════════════════════════════════════
class Config:
    BRAND      = "ISOWAY"
    MV_BASE    = "https://svc.data.myvessel.cn"
    # 认证：OAuth2 client_credentials
    MV_CLIENT_ID     = os.getenv("MYVESSEL_APP_ID",     "wxjfkjyxgs_api01")
    MV_CLIENT_SECRET = os.getenv("MYVESSEL_APP_SECRET",  "14c7c4fdd6cdaef0b1674fc0ae083e6f")
    WECOM_WEBHOOK    = os.getenv("WECOM_WEBHOOK", "")
    WECOM_WEBHOOK_CUSTOM = os.getenv("WECOM_WEBHOOK_CUSTOM", "")  # 定制报告推送，未配则用 WECOM_WEBHOOK
    OUTPUT_DIR       = Path(os.getenv("OUTPUT_DIR", "./reports"))
    STATE_FILE       = Path(os.getenv("STATE_FILE", "./reports/.port_state.json"))
    TIMEOUT          = 30

    # 拥堵阈值（与7日均值对比）
    CONGESTION_HIGH = 0.40
    CONGESTION_MOD  = 0.15
    CONGESTION_LOW  = -0.15

    # ── 港口清单（23个，干散货核心航线）─────────────────────────────────────
    # port_code = 船视宝专用代码（来自港口代码表，≠ UN/LOCODE）
    PORTS = [
        # ── 澳大利亚装港 ─────────────────────────────────────────────────────
        {"id":"port_hedland",   "port_code":"AUPHE", "name":"Port Hedland",
         "name_cn":"黑德兰港",    "flag":"🇦🇺", "country":"澳大利亚",
         "cargo":"铁矿石",        "group":"装港·澳大利亚",
         "bdi":"C5TC",           "note":"西澳→中国/日韩 Cape 主力装港"},
        {"id":"dampier",        "port_code":"AUDPR", "name":"Dampier",
         "name_cn":"丹皮尔港",    "flag":"🇦🇺", "country":"澳大利亚",
         "cargo":"铁矿石",        "group":"装港·澳大利亚",
         "bdi":"C5TC",           "note":"Rio Tinto 西澳主力装港"},
        {"id":"newcastle",      "port_code":"AUNTL", "name":"Newcastle",
         "name_cn":"纽卡斯尔",    "flag":"🇦🇺", "country":"澳大利亚",
         "cargo":"动力煤",        "group":"装港·澳大利亚",
         "bdi":"P2A_82",         "note":"全球最大煤港，Panamax/Cape 主力"},
        {"id":"hay_point",      "port_code":"AUHAY", "name":"Hay Point/DBCT",
         "name_cn":"黑点港",      "flag":"🇦🇺", "country":"澳大利亚",
         "cargo":"焦煤",          "group":"装港·澳大利亚",
         "bdi":"P3A_82",         "note":"昆士兰焦煤主力出口港"},
        {"id":"gladstone",      "port_code":"AUGLE", "name":"Gladstone",
         "name_cn":"格拉德斯通",  "flag":"🇦🇺", "country":"澳大利亚",
         "cargo":"煤炭/铝土",     "group":"装港·澳大利亚",
         "bdi":"P2A_82",         "note":"昆士兰综合能源出口港"},
        # ── 巴西装港 ─────────────────────────────────────────────────────────
        {"id":"tubarao",        "port_code":"BRTUB", "name":"Tubarao",
         "name_cn":"图巴朗港",    "flag":"🇧🇷", "country":"巴西",
         "cargo":"铁矿石",        "group":"装港·巴西",
         "bdi":"C3",             "note":"C3 巴西→青岛 Cape 标准航线起点"},
        {"id":"ponta_madeira",  "port_code":"BRPDM", "name":"Ponta da Madeira",
         "name_cn":"马德拉角港",  "flag":"🇧🇷", "country":"巴西",
         "cargo":"铁矿石",        "group":"装港·巴西",
         "bdi":"C3",             "note":"Vale S11D 出口港，近年扩建"},
        # ── 南非装港 ─────────────────────────────────────────────────────────
        {"id":"richards_bay",   "port_code":"ZARIB", "name":"Richards Bay",
         "name_cn":"理查兹湾",    "flag":"🇿🇦", "country":"南非",
         "cargo":"动力煤",        "group":"装港·南非",
         "bdi":"P2A_82",         "note":"RBCT 南非煤炭主力出口港"},
        # ── 印尼装港（东南亚核心）────────────────────────────────────────────
        {"id":"samarinda",      "port_code":"IDSAM", "name":"Samarinda",
         "name_cn":"三马林达",    "flag":"🇮🇩", "country":"印度尼西亚",
         "cargo":"动力煤",        "group":"装港·印尼",
         "bdi":"S2_58",          "note":"东加里曼丹煤炭集散港"},
        {"id":"balikpapan",     "port_code":"IDBAL", "name":"Balikpapan",
         "name_cn":"巴厘巴板",    "flag":"🇮🇩", "country":"印度尼西亚",
         "cargo":"动力煤",        "group":"装港·印尼",
         "bdi":"S2_58",          "note":"东加里曼丹最大港口"},
        {"id":"tanjung_bara",   "port_code":"IDTBR", "name":"Tanjung Bara",
         "name_cn":"丹戒巴拉",    "flag":"🇮🇩", "country":"印度尼西亚",
         "cargo":"动力煤",        "group":"装港·印尼",
         "bdi":"S2_58",          "note":"东卡里曼丹煤炭专用码头"},
        {"id":"bontang",        "port_code":"IDBON", "name":"Bontang",
         "name_cn":"邦坦港",      "flag":"🇮🇩", "country":"印度尼西亚",
         "cargo":"动力煤",        "group":"装港·印尼",
         "bdi":"S2_58",          "note":"东加里曼丹出口港"},
        {"id":"muara_satui",    "port_code":"IDMUS", "name":"Muara Satui",
         "name_cn":"麻拉沙堆",    "flag":"🇮🇩", "country":"印度尼西亚",
         "cargo":"动力煤",        "group":"装港·印尼",
         "bdi":"S2_58",          "note":"南加里曼丹煤炭出口港"},
        # ── 西非装港 ─────────────────────────────────────────────────────────
        {"id":"kamsar",         "port_code":"GNKAM", "name":"Kamsar",
         "name_cn":"卡姆萨尔",    "flag":"🇬🇳", "country":"几内亚",
         "cargo":"铝矾土",        "group":"装港·西非",
         "bdi":"C3",             "note":"全球最大铝矾土出口港"},
        {"id":"conakry",        "port_code":"GNCON", "name":"Conakry",
         "name_cn":"科纳克里",    "flag":"🇬🇳", "country":"几内亚",
         "cargo":"铝矾土",        "group":"装港·西非",
         "bdi":"C3",             "note":"几内亚首都港，铝矾土出口"},
        # ── 中国主要卸港 ─────────────────────────────────────────────────────
        {"id":"qingdao",        "port_code":"CNQIN", "name":"Qingdao",
         "name_cn":"青岛港",      "flag":"🇨🇳", "country":"中国",
         "cargo":"铁矿石",        "group":"卸港·中国",
         "bdi":"C3/C5TC",        "note":"C3/C5TC 终点，最大铁矿石卸货港"},
        {"id":"tianjin",        "port_code":"CNTJN", "name":"Tianjin",
         "name_cn":"天津港",      "flag":"🇨🇳", "country":"中国",
         "cargo":"铁矿石/煤炭",   "group":"卸港·中国",
         "bdi":"C3/C5TC",        "note":"北方最大综合散货港"},
        {"id":"dalian",         "port_code":"CNDAL", "name":"Dalian",
         "name_cn":"大连港",      "flag":"🇨🇳", "country":"中国",
         "cargo":"铁矿石",        "group":"卸港·中国",
         "bdi":"C5TC",           "note":"东北铁矿石主要卸货港"},
        {"id":"caofeidian",     "port_code":"CNCFD", "name":"Caofeidian",
         "name_cn":"曹妃甸港",    "flag":"🇨🇳", "country":"中国",
         "cargo":"铁矿石/煤炭",   "group":"卸港·中国",
         "bdi":"C3/C5TC",        "note":"华北重要散货港，宝武钢铁专用"},
        {"id":"rizhao",         "port_code":"CNRIZ", "name":"Rizhao",
         "name_cn":"日照港",      "flag":"🇨🇳", "country":"中国",
         "cargo":"铁矿石/煤炭",   "group":"卸港·中国",
         "bdi":"C3/C5TC",        "note":"华东铁矿石核心卸货港"},
        {"id":"lianyungang",    "port_code":"CNLYG", "name":"Lianyungang",
         "name_cn":"连云港",      "flag":"🇨🇳", "country":"中国",
         "cargo":"铁矿石",        "group":"卸港·中国",
         "bdi":"C5TC",           "note":"苏北铁矿石卸货港"},
        # ── 印度卸港 ─────────────────────────────────────────────────────────
        {"id":"paradip",        "port_code":"INPAR", "name":"Paradip",
         "name_cn":"巴拉迪布港",  "flag":"🇮🇳", "country":"印度",
         "cargo":"铁矿石/煤炭",   "group":"卸港·印度",
         "bdi":"P2A_82",         "note":"东印度最大散货港"},
        {"id":"visakhapatnam",  "port_code":"INVIS", "name":"Visakhapatnam",
         "name_cn":"维沙卡帕特南","flag":"🇮🇳", "country":"印度",
         "cargo":"铁矿石/煤炭",   "group":"卸港·印度",
         "bdi":"P2A_82",         "note":"东海岸重要散货港"},
    ]

    # ── 定制港口清单（客户专属）────────────────────────────────────────────────
    CUSTOM_PORTS = [
        {"id":"nauru",          "port_code":"NRNRI", "name":"Nauru",
         "name_cn":"瑙鲁",       "flag":"🇳🇷", "country":"瑙鲁",
         "cargo":"综合",          "group":"太平洋岛国",
         "bdi":"—",              "note":"太平洋岛国港口"},
        {"id":"tarawa",         "port_code":"KITAI", "name":"Tarawa",
         "name_cn":"塔拉瓦",     "flag":"🇰🇮", "country":"基里巴斯",
         "cargo":"综合",          "group":"太平洋岛国",
         "bdi":"—",              "note":"基里巴斯首都港"},
        {"id":"suva",           "port_code":"FJSUV", "name":"Suva",
         "name_cn":"苏瓦",       "flag":"🇫🇯", "country":"斐济",
         "cargo":"综合",          "group":"太平洋岛国",
         "bdi":"—",              "note":"斐济首都港，南太主要中转"},
        {"id":"port_vila",      "port_code":"VUPVI", "name":"Port Vila",
         "name_cn":"维拉港",     "flag":"🇻🇺", "country":"瓦努阿图",
         "cargo":"综合",          "group":"太平洋岛国",
         "bdi":"—",              "note":"瓦努阿图首都港"},
        {"id":"dar_es_salaam",  "port_code":"TZDRS", "name":"Dar es Salaam",
         "name_cn":"达累斯萨拉姆","flag":"🇹🇿", "country":"坦桑尼亚",
         "cargo":"综合",          "group":"东非",
         "bdi":"—",              "note":"坦桑尼亚最大港口"},
        {"id":"tanga",          "port_code":"TZTAN", "name":"Tanga",
         "name_cn":"坦噶",       "flag":"🇹🇿", "country":"坦桑尼亚",
         "cargo":"综合",          "group":"东非",
         "bdi":"—",              "note":"坦桑尼亚北部港口"},
        {"id":"lae",            "port_code":"PGLAE", "name":"Lae",
         "name_cn":"莱城",       "flag":"🇵🇬", "country":"巴布亚新几内亚",
         "cargo":"综合",          "group":"太平洋岛国",
         "bdi":"—",              "note":"巴新第二大城市港口"},
    ]


# ══════════════════════════════════════════════════════════════════════════════
# 2. 船视宝 API 认证（OAuth2 Bearer Token）
# ══════════════════════════════════════════════════════════════════════════════
_token_cache = {"token": None, "expires_at": 0}

def get_access_token() -> str:
    """获取或刷新船视宝 Bearer Token（自动缓存，提前60秒刷新）。"""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    url = (f"{Config.MV_BASE}/ada/oauth/token"
           f"?client_id={Config.MV_CLIENT_ID}"
           f"&client_secret={Config.MV_CLIENT_SECRET}"
           f"&grant_type=client_credentials")
    try:
        r = requests.get(url, timeout=Config.TIMEOUT)
        r.raise_for_status()
        j = r.json()
        token = j.get("access_token")
        if not token:
            raise ValueError(f"未获取到 access_token: {j}")
        _token_cache["token"]      = token
        _token_cache["expires_at"] = now + j.get("expires_in", 3600)
        log.info(f"✅ Token 获取成功，scope={j.get('scope')}，有效期 {j.get('expires_in')}s")
        return token
    except Exception as e:
        log.error(f"Token 获取失败: {e}")
        raise


def mv_post(endpoint: str, payload: dict) -> dict:
    """调用船视宝 SDC API（POST JSON + Bearer Token）。"""
    token = get_access_token()
    url   = f"{Config.MV_BASE}{endpoint}"
    try:
        r = requests.post(
            url,
            json=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {token}",
            },
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
def fetch_port_dynamics(ports=None) -> list:
    """
    调用 /sdc/v1/ports/analytics/dynamic/current/count 获取所有港口实时动态。
    返回字段：moor（锚泊-港口）、moorHalfway（锚泊-途中）、berth（靠泊）、
              estimate（预抵）、repair（修理）
    注意：不传 vesselType 限制，否则返回空。
    ports: 可选港口列表，默认用 Config.PORTS
    """
    port_list = ports or Config.PORTS
    port_codes = [p["port_code"] for p in port_list]
    log.info(f"查询 {len(port_codes)} 个港口动态（船视宝 API）...")

    payload = {
        "portCodes":      port_codes,
        "cascadeType":    1,
        "includeFish":    True,
        "dwt":            0,
        "dwt2":           400000,
        "teu":            0,
        "teu2":           25000,
        "grt":            0,
        "grt2":           400000,
        "length":         "0.0",
        "length2":        "400.0",
        "width":          "0.0",
        "width2":         "100.0",
        "vesselType":     [],
        "vesselSubType":  [],
        "vesselSub2Type": [],
    }

    resp = mv_post("/sdc/v1/ports/analytics/dynamic/current/count", payload)

    if not resp.get("success") or not resp.get("data"):
        log.warning(f"API 返回异常或无数据: {resp}")
        return []

    data = resp["data"]
    log.info(f"✅ 返回 {len(data)} 个港口")
    for item in data:
        log.info(f"  {item.get('portCode','?'):6s} {item.get('nameCn','?'):10s} "
                 f"锚泊={item.get('moor',0)+item.get('moorHalfway',0)} "
                 f"靠泊={item.get('berth',0)} 预抵={item.get('estimate',0)}")
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


def analyze_ports(api_data: list, ports=None, history_prefix: str = "") -> list:
    """分析港口拥堵数据。
    ports: 可选港口列表，默认用 Config.PORTS
    history_prefix: 历史数据 key 前缀（定制报告用 "custom_" 避免污染主报告）
    """
    port_list = ports or Config.PORTS
    api_index = {item.get("portCode", "").upper(): item for item in api_data}
    state     = load_state()
    history   = state.get("port_history", {})
    results   = []

    for port_cfg in port_list:
        pc   = port_cfg["port_code"].upper()
        item = api_index.get(pc, {})

        n_moor_port = item.get("moor",        0) or 0
        n_moor_half = item.get("moorHalfway", 0) or 0
        n_anchored  = n_moor_port + n_moor_half
        n_moored    = item.get("berth",        0) or 0
        n_estimate  = item.get("estimate",     0) or 0
        n_repair    = item.get("repair",       0) or 0
        data_fresh  = bool(item)

        hist_key = f"{history_prefix}{port_cfg['id']}"
        hist = history.get(hist_key, [])
        avg7 = round(sum(h["anchored"] for h in hist[-7:]) / len(hist[-7:]), 1) if hist else None

        if avg7 is not None and avg7 > 0:
            ratio = (n_anchored - avg7) / avg7
            congestion = ("high"   if ratio >= Config.CONGESTION_HIGH  else
                          "mod"    if ratio >= Config.CONGESTION_MOD   else
                          "low"    if ratio <= Config.CONGESTION_LOW   else "normal")
        elif n_anchored >= 20:
            congestion = "mod"
        else:
            congestion = "normal"

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
        })

    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    for r in results:
        hk = f"{history_prefix}{r['id']}"
        if hk not in history: history[hk] = []
        history[hk].append({"date": today, "anchored": r["n_anchored"], "moored": r["n_moored"]})
        if len(history[hk]) > 30: history[hk] = history[hk][-30:]

    state["port_history"] = history
    save_state(state)

    for r in results:
        r["history_7d"] = history.get(f"{history_prefix}{r['id']}", [])[-7:]

    # 按拥堵等级排序，同级按锚泊数降序
    impact_order = {"high": 0, "mod": 1, "normal": 2, "low": 3}
    results.sort(key=lambda x: (impact_order[x["congestion"]], -x["n_anchored"]))
    return results


def _demo_data() -> list:
    import random as rnd; rnd.seed(42)
    demo = {
        "AUPHE": (8,2,15,0),   "AUDPR": (19,4,3,50),
        "AUNTL": (0,0,8,88),   "AUHAY": (5,1,4,12),  "AUGLE": (3,1,5,8),
        "BRTUB": (13,1,7,48),  "BRPDM": (34,4,11,83),
        "ZARIB": (16,2,16,29),
        "IDSAM": (28,5,12,35), "IDBAL": (22,4,10,28), "IDTBR": (15,3,8,20),
        "IDBON": (10,2,6,15),  "IDMUS": (12,2,7,18),
        "GNKAM": (8,2,4,12),   "GNCON": (4,1,3,8),
        "CNQIN": (12,3,25,40), "CNTJN": (8,2,20,30), "CNDAL": (6,1,15,20),
        "CNCFD": (10,2,18,25), "CNRIZ": (9,2,16,22), "CNLYG": (5,1,12,18),
        "INPAR": (7,2,10,15),  "INVIS": (5,1,8,12),
    }
    result = []
    for port in Config.PORTS:
        pc = port["port_code"]
        m, mh, b, e = demo.get(pc, (3,1,4,6))
        result.append({
            "portCode": pc, "nameCn": port["name_cn"], "nameEn": port["name"],
            "moor": m, "moorHalfway": mh, "berth": b, "estimate": e, "repair": 0,
        })
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 5. HTML 模板
# ══════════════════════════════════════════════════════════════════════════════
PORT_HTML = """<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>港口拥堵日报 — {{ date }} | {{ brand }}</title>
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
body { font-family:'Outfit','PingFang SC','Microsoft YaHei',sans-serif;
       background:#eef0f4; color:var(--ink); padding:2.5rem 1.5rem;
       -webkit-print-color-adjust:exact; print-color-adjust:exact; }
.page { max-width:1160px; margin:0 auto; background:var(--surface);
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
.status-strip { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
.sb { display:inline-flex; align-items:center; gap:5px; font-size:11px; font-weight:500;
      padding:4px 10px; border-radius:2px; letter-spacing:0.02em; }
.sb-r { background:rgba(192,57,43,0.28); color:#f0a09a; outline:1px solid rgba(192,57,43,0.3); }
.sb-o { background:rgba(184,108,10,0.28); color:#f5c76e; outline:1px solid rgba(184,108,10,0.3); }
.sb-g { background:rgba(30,127,90,0.28);  color:#7de0b8; outline:1px solid rgba(30,127,90,0.3); }
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
/* KPI 统计行 */
.kpi-row { display:grid; grid-template-columns:repeat(5,1fr);
           gap:1px; background:var(--border); border:1px solid var(--border);
           margin-bottom:1.8rem; }
.kpi { background:var(--surface); padding:1rem 1.2rem; text-align:center; }
.kpi .k-val   { font-family:'DM Mono',monospace; font-size:24px;
                font-weight:500; color:var(--ink); line-height:1; }
.kpi .k-val.red   { color:var(--red); }
.kpi .k-val.amber { color:var(--amber); }
.kpi .k-val.green { color:var(--green); }
.kpi .k-val.blue  { color:var(--accent-mid); }
.kpi .k-label { font-size:9.5px; font-weight:600; letter-spacing:0.08em;
                text-transform:uppercase; color:var(--ink-m); margin-top:4px; }
/* Demurrage alert */
.dem-alert { background:var(--amber-l); border:1px solid #e8c990;
             padding:1rem 1.4rem; margin-bottom:1.8rem;
             display:flex; gap:12px; align-items:flex-start; }
.dem-icon  { font-size:18px; flex-shrink:0; }
.dem-title { font-size:11px; font-weight:700; color:var(--amber); margin-bottom:4px;
             letter-spacing:0.05em; text-transform:uppercase; }
.dem-text  { font-size:11.5px; color:var(--ink-2); line-height:1.7; }
/* Group header */
.group-hd { font-size:10px; font-weight:700; letter-spacing:0.12em;
            text-transform:uppercase; color:var(--ink-m);
            padding:8px 0 6px; border-bottom:1.5px solid var(--border-s);
            margin:1.6rem 0 10px; display:flex; align-items:center; gap:8px; }
.group-count { font-family:'DM Mono',monospace; font-size:10px;
               background:var(--accent-l); color:var(--accent-mid);
               padding:2px 8px; border-radius:2px; font-weight:600; }
/* Port grid */
.port-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
             gap:1px; background:var(--border); border:1px solid var(--border);
             margin-bottom:1.4rem; }
.pcard { background:var(--surface); overflow:hidden; }
.pcard.cong-high { outline:2px solid var(--red);   outline-offset:-2px; }
.pcard.cong-mod  { outline:1.5px solid var(--amber); outline-offset:-1.5px; }
.pcard.cong-low  { outline:1px solid var(--green); outline-offset:-1px; }
/* Port card head */
.pc-head { padding:9px 12px; display:flex; justify-content:space-between;
           align-items:flex-start; border-bottom:1px solid var(--border); }
.pc-name { font-size:13px; font-weight:600; color:var(--ink); }
.pc-sub  { font-family:'DM Mono',monospace; font-size:9px;
           color:var(--ink-m); margin-top:2px; letter-spacing:0.04em; }
.pc-badge { font-size:9px; font-weight:700; padding:2px 7px; border-radius:2px;
            white-space:nowrap; margin-left:6px; flex-shrink:0;
            letter-spacing:0.05em; text-transform:uppercase; }
.pb-high   { background:var(--red-l);   color:var(--red);   border:1px solid #e8b4b0; }
.pb-mod    { background:var(--amber-l); color:var(--amber); border:1px solid #e8c990; }
.pb-normal { background:var(--accent-l);color:var(--accent-mid); border:1px solid #b0c8e4; }
.pb-low    { background:var(--green-l); color:var(--green); border:1px solid #a0d4bc; }
/* Stats row */
.pc-stats { display:grid; grid-template-columns:repeat(4,1fr);
            gap:1px; background:var(--border); border-bottom:1px solid var(--border); }
.ps-cell  { background:var(--sf-soft); padding:7px 6px; text-align:center; }
.ps-label { font-size:8.5px; font-weight:600; letter-spacing:0.07em;
            text-transform:uppercase; color:var(--ink-m); margin-bottom:3px; white-space:nowrap; }
.ps-val   { font-family:'DM Mono',monospace; font-size:17px; font-weight:500; }
.ps-val.v-high  { color:var(--red); }
.ps-val.v-mod   { color:var(--amber); }
.ps-val.v-blue  { color:var(--accent-mid); }
.ps-val.v-gray  { color:var(--ink-m); }
.ps-val.v-ok    { color:var(--green); }
.ps-sub { font-size:8.5px; color:var(--ink-m); margin-top:1px; }
/* Trend */
.trend-wrap  { padding:7px 12px; border-bottom:1px solid var(--border); }
.trend-label { font-size:8.5px; font-weight:700; letter-spacing:0.08em;
               text-transform:uppercase; color:var(--ink-m); margin-bottom:4px; }
.trend-bars  { display:flex; align-items:flex-end; gap:3px; height:22px; }
.tb-day { flex:1; display:flex; flex-direction:column; align-items:center; gap:1px; }
.tb-bar { width:100%; border-radius:1px 1px 0 0; min-height:2px; }
.tb-dt  { font-size:7px; color:var(--ink-m); }
/* Detail rows */
.pc-detail { padding:7px 12px; }
.pd-row { display:flex; align-items:center; gap:5px; font-size:10.5px; margin-bottom:3px; }
.pd-row:last-child { margin-bottom:0; }
.pd-icon  { width:14px; text-align:center; flex-shrink:0; }
.pd-label { color:var(--ink-m); width:58px; flex-shrink:0; font-size:10px; }
.pd-val   { font-weight:500; color:var(--ink); font-family:'DM Mono',monospace; font-size:11px; }
.pd-note  { color:var(--ink-m); margin-left:3px; font-size:9.5px; }
.no-data  { font-size:10px; color:var(--ink-m); font-style:italic; padding:4px 0; }
/* Summary table */
.tcard { border:1px solid var(--border); margin-bottom:1.8rem; }
.tc-head { padding:9px 14px; background:var(--sf-soft);
           border-bottom:1.5px solid var(--border-s);
           display:flex; justify-content:space-between; align-items:center; }
.tc-title { font-size:10px; font-weight:600; letter-spacing:0.12em;
            text-transform:uppercase; color:var(--ink-m); }
.tc-badge { font-family:'DM Mono',monospace; font-size:10px;
            color:var(--ink-m); letter-spacing:0.04em; }
table { width:100%; border-collapse:collapse; font-size:12px; }
thead tr { background:var(--sf-soft); border-bottom:1.5px solid var(--border-s); }
thead th { padding:7px 10px; font-size:9.5px; font-weight:600; letter-spacing:0.10em;
           text-transform:uppercase; color:var(--ink-m); text-align:right; }
thead th:first-child, thead th:nth-child(2), thead th:nth-child(3) { text-align:left; }
tbody tr { border-bottom:1px solid var(--border); }
tbody tr:last-child { border-bottom:none; }
tbody tr:hover { background:var(--sf-soft); }
tbody tr.high-row { background:#fdf0ee; }
td { padding:6px 10px; text-align:right; color:var(--ink-2); vertical-align:middle; }
td:first-child, td:nth-child(2), td:nth-child(3) { text-align:left; }
td:first-child { font-weight:600; color:var(--ink); }
.cp { font-size:9px; font-weight:700; padding:2px 7px; border-radius:2px;
      display:inline-block; letter-spacing:0.05em; text-transform:uppercase; }
.cp-high   { background:var(--red-l);   color:var(--red);   border:1px solid #e8b4b0; }
.cp-mod    { background:var(--amber-l); color:var(--amber); border:1px solid #e8c990; }
.cp-normal { background:var(--accent-l);color:var(--accent-mid); border:1px solid #b0c8e4; }
.cp-low    { background:var(--green-l); color:var(--green); border:1px solid #a0d4bc; }
.td-high { color:var(--red)   !important; font-weight:600 !important; }
.td-mod  { color:var(--amber) !important; font-weight:600 !important; }
.td-gray { color:var(--ink-m) !important; }
/* Footer */
.footer { border-top:1.5px solid var(--border-s); padding:1.1rem 2.8rem;
          display:flex; justify-content:space-between; align-items:center;
          background:var(--sf-soft); }
.footer .f-left  { font-size:10.5px; color:var(--ink-m); line-height:1.6; }
.footer .f-left strong { color:var(--ink-2); font-weight:600; }
.footer .f-right { font-family:'DM Mono',monospace; font-size:10px;
                   color:var(--ink-m); text-align:right; letter-spacing:0.05em; }
.demo-banner { background:var(--amber-l); border:1px solid #e8c990;
               padding:8px 14px; margin-bottom:1rem;
               font-size:11px; color:var(--amber); font-weight:600; }
@media(max-width:720px){
  .kpi-row{grid-template-columns:repeat(3,1fr);}
  .port-grid{grid-template-columns:1fr;}
  .body{padding:1.5rem;}
}
</style>
</head>
<body>
<div class="page">

<div class="header">
  <div class="header-top">
    <div>
      <div class="doc-label">Port Intelligence · Dry Bulk Congestion Monitor</div>
      <div class="doc-title">干散货全球港口拥堵日报<br><em>Global Port Congestion Report</em></div>
    </div>
    <div class="doc-meta">
      <div class="meta-date">{{ date }}</div>
      <div class="meta-ref">{{ brand }} · {{ ports|length }} 港口实时数据</div>
    </div>
  </div>
  <div class="status-strip">
    {% for b in stat_badges %}
    <span class="sb {{ 'sb-r' if b.cls=='hb-r' else ('sb-o' if b.cls=='hb-o' else 'sb-g') }}">{{ b.text }}</span>
    {% endfor %}
  </div>
</div>

<div class="body">

{% if demo_mode %}
<div class="demo-banner">⚠ 演示模式 — 当前显示为示例数据，请配置 MYVESSEL_TOKEN 获取实时数据</div>
{% endif %}

<!-- 01 总览 -->
<div class="section-head">
  <span class="section-num">01</span>
  <span class="section-label">全球港口概览</span>
  <span class="section-line"></span>
</div>

<div class="kpi-row">
  <div class="kpi">
    <div class="k-val red">{{ high_ports }}</div>
    <div class="k-label">严重拥堵</div>
  </div>
  <div class="kpi">
    <div class="k-val amber">{{ mod_ports }}</div>
    <div class="k-label">中度拥堵</div>
  </div>
  <div class="kpi">
    <div class="k-val green">{{ normal_ports }}</div>
    <div class="k-label">正常运营</div>
  </div>
  <div class="kpi">
    <div class="k-val blue">{{ total_anchored }}</div>
    <div class="k-label">总锚泊(艘)</div>
  </div>
  <div class="kpi">
    <div class="k-val blue">{{ total_estimate }}</div>
    <div class="k-label">预抵(艘)</div>
  </div>
</div>

{% if dem_alerts %}
<div class="dem-alert">
  <div class="dem-icon">⚠️</div>
  <div>
    <div class="dem-title">Demurrage 风险提示</div>
    <div class="dem-text">{{ dem_alerts }}</div>
  </div>
</div>
{% endif %}

<!-- 02 各区域港口详情 -->
<div class="section-head">
  <span class="section-num">02</span>
  <span class="section-label">各区域港口详情</span>
  <span class="section-line"></span>
</div>

{# 按 group 字段分组展示 #}
{% set groups = ports | groupby('group') %}
{% for grp_name, grp_ports in groups %}
<div class="group-hd">
  {{ grp_name }}
  <span class="group-count">{{ grp_ports|list|length }} 港</span>
</div>
<div class="port-grid">
{% for p in grp_ports %}
{% set cong = p.congestion %}
<div class="pcard cong-{{ cong }}">
  <div class="pc-head">
    <div>
      <div class="pc-name">{{ p.flag }}{{ p.name_cn }} <span style="font-weight:400;font-size:.85em;color:#666">{{ p.name }}</span></div>
      <div class="pc-sub">{{ p.country }} · {{ p.cargo }}</div>
    </div>
    <span class="pc-badge pb-{{ cong }}">
      {{ '严重拥堵' if cong=='high' else ('中度拥堵' if cong=='mod' else ('轻度拥堵' if cong=='low' else '正常')) }}
    </span>
  </div>
  <div class="pc-stats">
    <div class="ps-cell">
      <div class="ps-label">锚泊</div>
      <div class="ps-val {{ 'v-high' if (p.n_anchored or 0)>=30 else ('v-mod' if (p.n_anchored or 0)>=15 else 'v-blue') }}">
        {{ p.n_anchored if p.data_fresh else '—' }}
      </div>
      <div class="ps-sub">艘</div>
    </div>
    <div class="ps-cell">
      <div class="ps-label">靠泊</div>
      <div class="ps-val v-blue">
        {{ p.n_moored if p.data_fresh else '—' }}
      </div>
      <div class="ps-sub">艘</div>
    </div>
    <div class="ps-cell">
      <div class="ps-label">预抵</div>
      <div class="ps-val v-gray">
        {{ p.n_estimate if p.data_fresh else '—' }}
      </div>
      <div class="ps-sub">艘</div>
    </div>
    <div class="ps-cell">
      <div class="ps-label">估等</div>
      <div class="ps-val {{ 'v-high' if (p.est_wait_days or 0)>=5 else ('v-mod' if (p.est_wait_days or 0)>=2 else 'v-ok') }}">
        {{ '%.1f'|format(p.est_wait_days) if p.est_wait_days else '—' }}
      </div>
      <div class="ps-sub">天</div>
    </div>
  </div>
  {% if p.trend_data %}
  <div class="trend-wrap">
    <div class="trend-label">近期锚泊趋势</div>
    <div class="trend-bars">
      {% set max_v = (p.trend_data | map(attribute='n') | list | max) or 1 %}
      {% for t in p.trend_data %}
      <div class="tb-day">
        <div class="tb-bar" style="height:{{ ((t.n or 0)/max_v*100)|int }}%;
          background:{{ '#c0392b' if (t.n or 0)>=30 else ('#b86c0a' if (t.n or 0)>=15 else '#2d5f96') }};"></div>
        <div class="tb-dt">{{ t.date[-5:] if t.date else '' }}</div>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}
  <div class="pc-detail">
    {% if p.est_wait_days and p.est_wait_days >= 2 %}
    <div class="pd-row">
      <span class="pd-icon">⚠️</span>
      <span class="pd-label">Demurrage</span>
      <span class="pd-val" style="color:var(--amber);">预计 {{ '%.1f'|format(p.est_wait_days) }} 天等待风险</span>
    </div>
    {% endif %}
    <div class="pd-row">
      <span class="pd-icon">📦</span>
      <span class="pd-label">货类</span>
      <span class="pd-val" style="font-family:inherit;">{{ p.cargo }}</span>
    </div>
    {% if not p.data_fresh %}
    <div class="no-data">⚠ 数据更新中</div>
    {% endif %}
  </div>
</div>
{% endfor %}
</div>
{% endfor %}

<!-- 03 汇总表 -->
<div class="section-head">
  <span class="section-num">03</span>
  <span class="section-label">全港口汇总对比</span>
  <span class="section-line"></span>
</div>

<div class="tcard">
  <div class="tc-head">
    <span class="tc-title">Port Congestion Summary</span>
    <span class="tc-badge">{{ date }} UTC</span>
  </div>
  <table>
    <thead>
      <tr>
        <th>港口</th><th>区域</th><th>货类</th>
        <th style="text-align:right;">锚泊(艘)</th>
        <th style="text-align:right;">靠泊</th>
        <th style="text-align:right;">预抵</th>
        <th style="text-align:right;">7日均值</th>
        <th style="text-align:right;">估等(天)</th>
        <th style="text-align:right;">评级</th>
      </tr>
    </thead>
    <tbody>
      {% for p in ports | sort(attribute='n_anchored', reverse=True) %}
      {% set cong = p.congestion %}
      <tr {% if cong=='high' %}class="high-row"{% endif %}>
        <td>{{ p.flag }}{{ p.name_cn }} <span style="color:#888;font-size:10.5px;">{{ p.name }}</span></td>
        <td style="color:var(--ink-m);font-size:10.5px;">{{ p.group }}</td>
        <td style="color:var(--ink-m);font-size:10.5px;">{{ p.cargo }}</td>
        <td class="{{ 'td-high' if (p.n_anchored or 0)>=30 else ('td-mod' if (p.n_anchored or 0)>=15 else '') }}"
            style="font-family:'DM Mono',monospace;font-size:12px;font-weight:500;">
          {{ p.n_anchored if p.data_fresh else '—' }}
        </td>
        <td style="font-family:'DM Mono',monospace;font-size:12px;">{{ p.n_moored if p.data_fresh else '—' }}</td>
        <td style="font-family:'DM Mono',monospace;font-size:12px;">{{ p.n_estimate if p.data_fresh else '—' }}</td>
        <td style="font-family:'DM Mono',monospace;font-size:12px;color:var(--ink-m);">
          {{ '%.0f'|format(p.avg7) if p.avg7 else '—' }}
        </td>
        <td class="{{ 'td-high' if (p.est_wait_days or 0)>=5 else ('td-mod' if (p.est_wait_days or 0)>=2 else '') }}"
            style="font-family:'DM Mono',monospace;font-size:12px;">
          {{ '%.1f'|format(p.est_wait_days) if p.est_wait_days else '—' }}
        </td>
        <td><span class="cp cp-{{ cong }}">{{ '严重' if cong=='high' else ('中度' if cong=='mod' else ('轻度' if cong=='low' else '正常')) }}</span></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>

</div><!-- /body -->

<div class="footer">
  <div class="f-left">
    <strong>{{ brand }}</strong> 港口智能监控 · 本报告仅供参考
  </div>
  <div class="f-right">
    PORT CONGESTION REPORT<br>
    {{ brand }} · {{ generated_at }}
  </div>
</div>

</div><!-- /page -->
</body>
</html>"""


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
    stat_badges.append({"cls":"hb-g","text":f"✅ 正常 {normal_ports} 港"})

    dem_parts = []
    for p in port_results:
        if p["congestion"] in ("high","mod") and p["data_fresh"]:
            dev = ""
            if p["avg7"] and p["avg7"] > 0:
                pct = int((p["n_anchored"] - p["avg7"]) / p["avg7"] * 100)
                dev = f"（+{pct}%均值）"
            dem_parts.append(f"**{p['name_cn']} {p['name']}** {p['n_anchored']}艘{dev}，估等 {p['est_wait_days']} 天")
    dem_alerts = "；\n".join(dem_parts) + "\n\n建议关注 NOR 递交时间窗口，提前做好 Laytime 计算准备。" if dem_parts else ""

    ctx = dict(
        date=today, brand=Config.BRAND, generated_at=generated_at,
        ports=port_results, stat_badges=stat_badges,
        total_anchored=total_anchored, total_moored=total_moored,
        total_estimate=total_estimate, high_ports=high_ports,
        mod_ports=mod_ports, normal_ports=normal_ports,
        dem_alerts=dem_alerts, demo_mode=demo_mode,
    )
    if HAS_JINJA2:
        return Template(PORT_HTML).render(**ctx)
    return f"<html><body><h2>{Config.BRAND} 港口拥堵日报 {today}</h2>" + \
           "".join(f"<p>{p['flag']}{p['name_cn']} {p['name']}: 锚泊{p['n_anchored']}</p>" for p in port_results) + \
           "</body></html>"


# ══════════════════════════════════════════════════════════════════════════════
# 7. 截图 + 推送
# ══════════════════════════════════════════════════════════════════════════════
def html_to_image(html_path: str) -> bytes:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1160, "height": 900})
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
    high   = [p for p in port_results if p["congestion"] == "high"]
    mods   = [p for p in port_results if p["congestion"] == "mod"]

    lines = [
        f"# ⚓ {Config.BRAND} 港口拥堵日报 · {today}",
        "> 全球23个核心干散货装卸港 · 实时锚泊统计 · Demurrage 风险预警",
        "",
        f"**全港锚泊合计：<font color=\"warning\">{total_anchored} 艘</font>**　"
        + (f"⚠️ 高拥堵：{'、'.join(p['name_cn'] + ' ' + p['name'] for p in high)}" if high else "✅ 无高拥堵港口"),
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
                f"> {cong_emoji[p['congestion']]} **{p['flag']}{p['name_cn']} {p['name']}**（{p['cargo']}）　"
                f"锚泊 **{p['n_anchored']}** 艘 | 靠泊 {p['n_moored']} 艘{dev}　"
                f"预抵 {p['n_estimate']} 艘　估等 **{p['est_wait_days']}天**"
            )
        lines.append("")

    # 锚泊排名 Top 8（不按分组展开，节省字符）
    top_ports = sorted(port_results, key=lambda p: p["n_anchored"], reverse=True)[:8]
    lines.append("## 📊 锚泊 Top 8 港口")
    lines.append("| 港口 | ⚓锚泊 | 🚢靠泊 | 📍预抵 | 状态 |")
    lines.append("|:---|---:|---:|---:|:---:|")
    for p in top_ports:
        lines.append(
            f"| {p['flag']}{p['name_cn']} {p['name']} | **{p['n_anchored']}** | "
            f"{p['n_moored']} | {p['n_estimate']} | "
            f"{cong_emoji[p['congestion']]} {cong_label[p['congestion']]} |"
        )

    lines += [
        "", "## 💡 操作建议",
    ]
    if high:
        hnames = "、".join(p["name_cn"] + " " + p["name"] for p in high)
        lines += [
            f"> **{hnames}** 拥堵显著偏高：",
            "> · 确认 NOR 递交时间，做好 Demurrage 预案",
            "> · 评估替代港口或调整 ETA 窗口",
        ]
    elif mods:
        lines.append("> 当前有中等拥堵港口，建议关注动态，提前安排 Laytime 计算。")
    else:
        lines.append("> 当前各港口拥堵程度正常，无需特别关注。")

    lines += ["", f"<font color=\"comment\">仅供参考 · {Config.BRAND}</font>"]
    content = "\n".join(lines)
    # 企业微信 markdown 上限 4096 字符，超出则截断并提示
    if len(content) > 4000:
        content = content[:3900] + "\n\n> …（更多港口详情见 HTML 报告）"
    return {"msgtype":"markdown","markdown":{"content":content}}


def push_wecom(port_results: list, html_path: str, generated_at: str,
               webhook_url: str = "", pdf_title: str = "散货全球港口拥堵日报") -> bool:
    wh = webhook_url or Config.WECOM_WEBHOOK
    if not wh:
        log.warning("WECOM_WEBHOOK 未配置，跳过推送"); return False
    def _post(payload):
        return requests.post(wh, json=payload, timeout=30).json()
    # 1. 截图 → PDF 推送
    if html_path:
        try:
            img = html_to_image(html_path)
            # ── 图片消息（暂时关闭，后续可能恢复） ──
            # import hashlib as _hl
            # b64 = __import__('base64').b64encode(img).decode()
            # res = _post({"msgtype":"image","image":{"base64":b64,"md5":_hl.md5(img).hexdigest()}})
            # if res.get("errcode") == 0:
            #     log.info("✅ 截图推送成功")
            # else:
            #     log.warning(f"截图推送失败: {res}")

            # ── PDF 转换 + 推送 ──
            try:
                from utils import convert_and_push_pdf
                import datetime as _dt
                _today = _dt.datetime.now().strftime("%Y-%m-%d")
                convert_and_push_pdf(img, wh, pdf_title, _today)
            except Exception as pdf_e:
                log.warning(f"PDF 推送失败（不影响主流程）: {pdf_e}")
        except Exception as e: log.warning(f"截图失败: {e}")

    # 2. 文字消息（暂时关闭，后续可能恢复）
    # try:
    #     res = _post(build_wecom_card(port_results, generated_at))
    #     if res.get("errcode") == 0: log.info("✅ 文字推送成功")
    #     else: log.error(f"文字推送失败: {res}")
    # except Exception as e: log.error(f"推送异常: {e}")

    return True


# ══════════════════════════════════════════════════════════════════════════════
# 8. 主流程
# ══════════════════════════════════════════════════════════════════════════════
def run_once(demo: bool = False) -> bool:
    log.info("=" * 65)
    log.info(f"{Config.BRAND} 港口拥堵日报 v2.0 — 开始（{len(Config.PORTS)} 个港口）")
    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")

    if demo:
        log.info("演示模式：使用模拟数据")
        api_data = _demo_data()
    elif Config.MV_CLIENT_ID and Config.MV_CLIENT_SECRET:
        try:
            api_data = fetch_port_dynamics()
            if not api_data:
                log.warning("API 无数据，切换演示模式")
                api_data = _demo_data(); demo = True
        except Exception as e:
            log.error(f"API 调用失败: {e}，切换演示模式")
            api_data = _demo_data(); demo = True
    else:
        log.warning("未配置 API 凭证，切换演示模式")
        api_data = _demo_data(); demo = True

    port_results = analyze_ports(api_data)
    html = render_html(port_results, now_utc, demo_mode=demo)

    Config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today     = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    html_file = Config.OUTPUT_DIR / f"port_congestion_{today}.html"
    html_file.write_text(html, encoding="utf-8")
    log.info(f"✅ HTML 已保存: {html_file}")

    ok = push_wecom(port_results, str(html_file.resolve()), now_utc)
    log.info(f"推送结果: {'✅ 成功' if ok else '❌ 失败/未配置'}")

    # ── 定制港口报告（独立流程，失败不影响主报告）──────────────────────────────
    if Config.CUSTOM_PORTS and not demo:
        try:
            log.info("-" * 65)
            log.info(f"定制港口报告 — 开始（{len(Config.CUSTOM_PORTS)} 个港口）")
            import time as _time; _time.sleep(3)  # 避免 API 频率限制

            custom_api = fetch_port_dynamics(Config.CUSTOM_PORTS)
            if custom_api:
                custom_results = analyze_ports(custom_api, Config.CUSTOM_PORTS,
                                               history_prefix="custom_")
                custom_html = render_html(custom_results, now_utc)
                custom_file = Config.OUTPUT_DIR / f"port_congestion_custom_{today}.html"
                custom_file.write_text(custom_html, encoding="utf-8")
                log.info(f"✅ 定制 HTML 已保存: {custom_file}")

                custom_wh = Config.WECOM_WEBHOOK_CUSTOM or Config.WECOM_WEBHOOK
                push_wecom(custom_results, str(custom_file.resolve()), now_utc,
                           webhook_url=custom_wh, pdf_title="定制港口拥堵日报")
                log.info("✅ 定制港口报告推送完成")
            else:
                log.warning("定制港口 API 无数据，跳过")
        except Exception as e:
            log.warning(f"定制港口报告失败（不影响主报告）: {e}")

    log.info("=" * 65)
    return ok


# ══════════════════════════════════════════════════════════════════════════════
# 9. CLI
# ══════════════════════════════════════════════════════════════════════════════
WORKFLOW_YML = """\
name: ISOWAY 港口拥堵日报 v2

on:
  schedule:
    - cron: '0 1 * * *'      # UTC 01:00 = 北京 09:00
  workflow_dispatch:
    inputs:
      demo_mode:
        description: '演示模式（模拟数据，测试推送）'
        type: boolean
        default: false

permissions:
  contents: read

jobs:
  port-congestion:
    name: 港口拥堵日报
    runs-on: ubuntu-latest
    timeout-minutes: 5

    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: '3.11', cache: 'pip'}

      - name: 安装依赖
        run: pip install requests jinja2 python-dotenv playwright && playwright install chromium --with-deps

      - name: 恢复7日历史
        uses: actions/cache@v4
        with:
          path: reports/.port_state.json
          key: port-state-${{ github.run_id }}
          restore-keys: port-state-

      - run: mkdir -p reports

      - name: 执行港口日报
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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"{Config.BRAND} 港口拥堵日报 v2.0")
    parser.add_argument("--demo",         action="store_true")
    parser.add_argument("--output-dir",   default="./reports")
    parser.add_argument("--gen-workflow", action="store_true")
    args = parser.parse_args()

    Config.OUTPUT_DIR = Path(args.output_dir)
    Config.STATE_FILE = Config.OUTPUT_DIR / ".port_state.json"

    if args.gen_workflow:
        wf = Path("port_congestion_myvessel.yml")
        wf.write_text(WORKFLOW_YML)
        print(f"✅ Workflow 已生成: {wf}")
        sys.exit(0)

    sys.exit(0 if run_once(demo=args.demo) else 1)
