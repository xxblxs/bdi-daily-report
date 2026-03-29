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
def fetch_port_dynamics() -> list:
    """
    调用 /sdc/v1/ports/analytics/dynamic/current/count 获取所有港口实时动态。
    返回字段：moor（锚泊-港口）、moorHalfway（锚泊-途中）、berth（靠泊）、
              estimate（预抵）、repair（修理）
    注意：不传 vesselType 限制，否则返回空。
    """
    port_codes = [p["port_code"] for p in Config.PORTS]
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


def analyze_ports(api_data: list) -> list:
    api_index = {item.get("portCode", "").upper(): item for item in api_data}
    state     = load_state()
    history   = state.get("port_history", {})
    results   = []

    for port_cfg in Config.PORTS:
        pc   = port_cfg["port_code"].upper()
        item = api_index.get(pc, {})

        n_moor_port = item.get("moor",        0) or 0
        n_moor_half = item.get("moorHalfway", 0) or 0
        n_anchored  = n_moor_port + n_moor_half
        n_moored    = item.get("berth",        0) or 0
        n_estimate  = item.get("estimate",     0) or 0
        n_repair    = item.get("repair",       0) or 0
        data_fresh  = bool(item)

        hist = history.get(port_cfg["id"], [])
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
        pid = r["id"]
        if pid not in history: history[pid] = []
        history[pid].append({"date": today, "anchored": r["n_anchored"], "moored": r["n_moored"]})
        if len(history[pid]) > 30: history[pid] = history[pid][-30:]

    state["port_history"] = history
    save_state(state)

    for r in results:
        r["history_7d"] = history.get(r["id"], [])[-7:]

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
<title>港口拥堵日报 — {{ date }} | {{ brand }}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;
     font-size:13px;color:#1a1a18;background:#f0f4f8;padding:20px;}
.wrap{max-width:1160px;margin:0 auto;}

/* Header */
.header{background:linear-gradient(135deg,#0d2137 0%,#0f3a5c 100%);
        border-radius:14px;padding:18px 24px;margin-bottom:14px;
        display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px;}
.h-date{font-size:11px;color:rgba(255,255,255,.45);margin-bottom:5px;}
.h-title{font-size:22px;font-weight:700;color:#fff;}
.h-sub{font-size:12px;color:rgba(255,255,255,.55);margin-top:4px;}
.h-right{display:flex;flex-direction:column;align-items:flex-end;gap:8px;}
.brand-tag{font-size:13px;font-weight:700;color:#fff;padding:4px 14px;border-radius:6px;
           border:1px solid rgba(255,255,255,.2);background:rgba(255,255,255,.08);}
.h-badges{display:flex;gap:6px;flex-wrap:wrap;}
.hb{font-size:10px;padding:3px 9px;border-radius:4px;font-weight:600;border:1px solid;}
.hb-r{background:rgba(226,75,74,.25);color:#f09595;border-color:rgba(226,75,74,.35);}
.hb-o{background:rgba(239,159,39,.25);color:#f2c666;border-color:rgba(239,159,39,.35);}
.hb-g{background:rgba(29,158,117,.2);color:#5dcaa5;border-color:rgba(29,158,117,.3);}

/* Summary */
.summary-bar{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:14px;}
.sb{background:#fff;border-radius:10px;padding:12px 14px;border:1px solid #dde5ee;text-align:center;}
.sb-val{font-size:26px;font-weight:700;line-height:1;}
.sb-val.red{color:#a32d2d;}.sb-val.amber{color:#854f0b;}.sb-val.green{color:#085041;}
.sb-val.blue{color:#0c447c;}.sb-val.gray{color:#6b7280;}
.sb-label{font-size:10px;color:#8a9db5;margin-top:4px;}

/* Alert */
.dem-alert{background:#fff8e6;border:1px solid #f0c84a;border-radius:10px;
           padding:12px 16px;margin-bottom:14px;display:flex;gap:12px;}
.dem-icon{font-size:18px;flex-shrink:0;}
.dem-title{font-size:12px;font-weight:700;color:#7a5200;margin-bottom:4px;}
.dem-text{font-size:11px;color:#8b6500;line-height:1.7;}

/* Group section */
.group-header{font-size:12px;font-weight:700;color:#4a5568;padding:8px 0 6px;
              border-bottom:2px solid #e2e8f0;margin:14px 0 10px;
              display:flex;align-items:center;gap:6px;}
.group-count{font-size:10px;background:#e8f2fb;color:#0c447c;
             padding:2px 7px;border-radius:8px;font-weight:600;}

/* Port grid */
.port-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:10px;margin-bottom:4px;}
.pcard{background:#fff;border-radius:10px;overflow:hidden;border:1px solid #dde5ee;}
.pcard.cong-high{border-color:#a32d2d;border-width:2px;}
.pcard.cong-mod{border-color:#854f0b;border-width:1.5px;}
.pcard.cong-low{border-color:#1d9e75;}

.pc-head{padding:10px 12px;display:flex;justify-content:space-between;
         align-items:flex-start;border-bottom:1px solid #eef2f8;}
.pc-name{font-size:13px;font-weight:700;}
.pc-sub{font-size:9px;color:#8a9db5;margin-top:2px;}
.pc-badge{font-size:9px;font-weight:700;padding:2px 7px;border-radius:8px;
          white-space:nowrap;margin-left:6px;flex-shrink:0;}
.pb-high{background:#feecec;color:#a32d2d;}.pb-mod{background:#fff3d6;color:#854f0b;}
.pb-normal{background:#e8f2fb;color:#0c447c;}.pb-low{background:#e8f8f1;color:#085041;}

/* Stats row */
.pc-stats{display:grid;grid-template-columns:repeat(4,1fr);border-bottom:1px solid #eef2f8;}
.ps-cell{padding:8px 4px;border-right:1px solid #eef2f8;text-align:center;}
.ps-cell:last-child{border-right:none;}
.ps-label{font-size:9px;color:#8a9db5;margin-bottom:2px;white-space:nowrap;}
.ps-val{font-size:18px;font-weight:700;}
.ps-val.v-high{color:#a32d2d;}.ps-val.v-mod{color:#854f0b;}
.ps-val.v-blue{color:#0c447c;}.ps-val.v-gray{color:#aab8c8;}.ps-val.v-ok{color:#085041;}
.ps-sub{font-size:9px;color:#aab8c8;margin-top:1px;}

/* Trend */
.trend-wrap{padding:8px 12px;border-bottom:1px solid #eef2f8;}
.trend-label{font-size:9px;font-weight:600;color:#8a9db5;text-transform:uppercase;margin-bottom:5px;}
.trend-bars{display:flex;align-items:flex-end;gap:3px;height:24px;}
.tb-day{flex:1;display:flex;flex-direction:column;align-items:center;gap:1px;}
.tb-bar{width:100%;border-radius:2px 2px 0 0;min-height:2px;}
.tb-dt{font-size:7px;color:#aab8c8;}

/* Detail */
.pc-detail{padding:8px 12px;}
.pd-row{display:flex;align-items:center;gap:5px;font-size:10px;margin-bottom:4px;}
.pd-row:last-child{margin-bottom:0;}
.pd-icon{width:14px;text-align:center;flex-shrink:0;}
.pd-label{color:#8a9db5;width:60px;flex-shrink:0;}
.pd-val{font-weight:500;}
.pd-note{color:#aab8c8;margin-left:3px;font-size:9px;}
.no-data{font-size:10px;color:#aab8c8;font-style:italic;padding:4px 0;}

/* Summary table */
.sum-card{background:#fff;border-radius:12px;overflow:hidden;margin-bottom:14px;border:1px solid #dde5ee;}
.sum-head{padding:10px 16px;background:#f5f8fc;border-bottom:1px solid #e8eef6;
          display:flex;justify-content:space-between;align-items:center;}
.sum-title{font-size:12px;font-weight:600;}
.sum-badge{font-size:10px;padding:2px 8px;border-radius:4px;background:#e8f2fb;color:#0c447c;}
table{width:100%;border-collapse:collapse;font-size:11px;}
thead th{font-size:10px;font-weight:600;color:#8a9db5;padding:7px 8px;text-align:right;
         border-bottom:1px solid #eef2f8;white-space:nowrap;}
thead th:first-child,thead th:nth-child(2),thead th:nth-child(3){text-align:left;}
tbody tr{border-bottom:1px solid #f5f7fc;}
tbody tr:last-child{border-bottom:none;}
tbody tr:hover{background:#fafbff;}
tbody tr.high-row{background:#fff8f8;}
td{padding:6px 8px;text-align:right;vertical-align:middle;}
td:first-child,td:nth-child(2),td:nth-child(3){text-align:left;}
td:first-child{font-weight:600;}
.cp{font-size:10px;font-weight:700;padding:2px 7px;border-radius:8px;display:inline-block;}
.cp-high{background:#feecec;color:#a32d2d;}.cp-mod{background:#fff3d6;color:#854f0b;}
.cp-normal{background:#e8f2fb;color:#0c447c;}.cp-low{background:#e8f8f1;color:#085041;}
.td-high{color:#a32d2d;font-weight:700;}.td-mod{color:#854f0b;font-weight:700;}
.td-gray{color:#aab8c8;}

.footer{background:#fff;border-radius:10px;padding:10px 16px;font-size:11px;color:#8a9db5;
        display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;border:1px solid #dde5ee;}
.ft-brand{font-weight:700;color:#0d2137;font-size:12px;}
.ft-note{font-size:10px;color:#c0ccd8;margin-top:2px;}
.demo-banner{background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:8px 14px;
             margin-bottom:14px;font-size:11px;color:#92400e;text-align:center;}
@media(max-width:700px){.summary-bar{grid-template-columns:repeat(3,1fr);}
                         .port-grid{grid-template-columns:1fr;}}
</style>
</head>
<body><div class="wrap">

{% if demo_mode %}
<div class="demo-banner">⚠️ 演示模式 — 以下为模拟数据，仅供展示格式。真实数据需配置 MYVESSEL_APP_ID / MYVESSEL_APP_SECRET。</div>
{% endif %}

<div class="header">
  <div>
    <div class="h-date">{{ date }} UTC · 23个核心干散货港口实时动态</div>
    <div class="h-title"><img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAMgAAADICAYAAACtWK6eAAAAAXNSR0IArs4c6QAAAERlWElmTU0AKgAAAAgAAYdpAAQAAAABAAAAGgAAAAAAA6ABAAMAAAABAAEAAKACAAQAAAABAAAAyKADAAQAAAABAAAAyAAAAACbWz2VAABAAElEQVR4Ae2dB7xU1bX/uYiosYAUQUC5gCgKylNQARtoohE1drFFRZ/GqDHmvReNxt5LjP/EkqDG3lHRaIyKgiIiUYqKSBUQKdJ7b//v7zDrsM+ZM3Nn7vR753w+Z/Y+u6y91tpr7b12nTp1yk8qHKhwEm2F3/12omq1t+L0008Xb+pGcKHMrwim1JigUMWrsiUI5SfIASlG3RivygoR5E2N/1KFW+WL2EQtZY1nBAS6wu/xhTAph/nl1pgGxCUWuspPAg6IT8YruRsSpKvJwS79onMjrxRBrj1Ksyn2WlhJu1H2YkkTlGPkJQy1VTkk/HrFA09ZMKfwen7vOxYnBakxjxFWYwjKkBDxQ2+4FTQ+1ajKT5FXLk+MN8oqv5RFjWyYXwTVjKfG2IpZqA4TBIGySs8C2BoDwpRDrh6zPmp0o1EbFcRVBPlV0aYQ5koAyk88B6QMpiDu2CM+ZQ0JMWJrCDlJyTDFUCVLETSWkKsn3AqGvzenqt2/xqtaoRi1rapNOayS9S2/LW5ZfG3jS5neKjggwagNjymG0apWMDxFWe41jDtl1+dAbVEQEezSKr8phLk+U2q4x6W9hpOaOXm1aZDuKog4V9sUw6SlNtW50Vx2U+CAFMQbd6SQtqYlEe1uA1FWkppWw2V6MuJARc+ePes5SqKGIjwuy6iAcuYyB0qdAzZjZ4qhXsTtVUqdvpzgX4oMMpxr6xiiuoIgxRDPzJW/zMPqcrOI80lByjZ0+hUkxbDGJf3ctTRHKQmatXyqqnJFRwus+GKKENU7RIVFQyqHehwQM0vlMRtaQmC7SEsF93ziKSUwRXHLLSuHy40a6Pcq3ZmNkXKXkoJns0rEC3tMGWzQbT1IbeWN8aXWuSYIckvJNMxVRZmSiBemFC6PclVurYJrTC4FooWr3hq3m3ThwoUNtuKpW7duC95tE1SGV1dr1qxZTdKZ48aNW9+tW7elCdKWg7PEgWJUEGsNZTPXGGV45513tunSpUurbbbZpnLrrbfuoLeioqItNO6MUuyGuzXfO+PW5418iBdP1m7cuHEx7opNmzbN3rBhw9T169dP5x1H2IS5c+dOa9++/bxIAOXAtDlQTApiuMiVIEhRpCAlObj88ccft99uu+06oQg9UIDutPodcXfn3QGacvKgMJtQnnm8Y1GYLynk07Vr145q2LDh1JwUWAuAmlDmm1SVq1cKYH7hIGVwcdJ3ySjIrFmzmiCMh9NL9Ka1Pwx53QOFkKIX7EFZloHLKHD5aMWKFR9OnTp1VOfOnVcUDKESK9gVxnygbsogobceog63Y9Tt37+/mVOmJCWhGGPHjt2hVatWR2y77bZ96CWO4m2RDiPV6vOsIc+8mFsHoZ6Nf4kLByFviK41Vxj+HXEa8J1ovOJmDfjpWSbQq7yycuXKfzZt2nQkkSXBZ/CU7OQdV21gy9djymhE6sirZmDsGh2XAZYmX7ilXc6SJUva169f/zwUog9mVPsUASxC8DVm+Ab3WwbcE9atWzeHccR8Wvd5s2fPlqLU6dGjx6ooeMOGDdtu11133UT6nXgaYMK1ovymvPvw7o3i7InSaFyzU1R+hdWrV28v3hvIey1lDwOXZxctWvRGixYt5ifKUyThWgdTQ2ryUiRoZQ8NKYB6DXvd7+yVkmNIKMaBCNXztPJLEPKkD+lmIIRv8V5Ni30Ib8tXXnklZ1PUILPVqlWr2qB4p1HmnbzDwWFpUiSJhJappL1nzpw5Uq5ifTx5qelXm4pIe6Qo+nbDLK7oXKZij129evXbtN6rkwkc5st4FOEvS5cuPZGp2MYJCMkX3RVMFrRZvnx5X5TmDSlsMtyJn4+CPbJgwYKOCfAuZLB4Zo2rGpmSkJt0GeYSWRIEIvBdEa7+tLIJZYu4WQjX3xCuXiNGjPhJukyx9IsXL96ZQnap4q32LJgmEaClD7i+Cc4JexYagaU0Bn8Gn2LrUaQgeX0KIaQqs+jHGAjT7o0aNfoj44zzse23iaoVzJJhCNqzCNwbO+yww49RacJhX3/99c7NmzevZAywh8YDjF8khJWMHST4LSgr6cAb5dHgfQHlzuWdzTseoR8PLlNmzpw5PdUZKnq4PZlt60N554LDnmE89U1ZC4D758mTJ/+9Y8eOC6PS5CBM8mFyaRM3VkxJyI4hWyNdjREYMF+G0EeaIwjkepkrmFzH9+vXb+uqmIBd34xxyzHAvJ1879A6TwfGOoQvqw9wV9HbTaKM/phUv8dM6qG1mKrw++677xosW7bsNyjCyEQIAfdbaDiuKlhZiq/xZlSW+JR/MAhxF4Thg0SCgtIMRJiOqAoztc6kvYD3NQR3biJ4uQynXM12TYSef2AuncBMVcNkeDNdXZ+xk8yv4VF4qWEA1pPz5s3bNRmcNOKsl1AWUwqFmd/cNEAWT1IjpHgwygCTm2++uS7CcSVCEGmX0yp/QfypgwcPTjgtHmuJ+9ASvw2cZVFCVsgwlGWaxhX0fIeI3kTsYiFxW2i9AkX5Pgpf4IynITk2Uf4UwiU7em2gHfabbMkt2cclqmSJEOLz589viUC8nkAY5hD3+6+++iqhqULLXMng/HYEalIUjCyHrc8UXqwn+ISe8FeYTY0SVd706dNboFAPkT5u1g4lWYOS3PHDDz9slyh/gnAppimAknhT3rGp25LuMYxeI1DfpiQWV3Iugn04lR0p2JgTzyJACRcAsfP3lflC/oWZCm2y/MBfRjmfo4B/R2D/D4U9C7z/m+9neD9LlreqOGBPp3e8dcaMGa0SVR7m4qH0ih9HwaL8QWogEuUNhZtihF3rzRRe8o/9DZkRY91kyRHGFObFEr5wxVPpcxD+XyYiiFZzPwT1cfIuD+fNxbdacF6ZfstxV/HORGE+Bv8+KEufbJQJzbPpEW7BvIocp2BabotSXpuAX1O1RpSIX6FwyY291tiagoSSluaniDMlKUkCZWMj4PdECRaC9yGtaeSUJ0rTjHx/zpdiROHnhklRwOcxNyxTP/SPh87zgBM51tLMGL3J1+Fy4MkqFOyyFEXaVZAUs5ROMk9BYuiWnOZrLxNC8HK4ghG21ZgatyeyqSU0pPkunC9X35S1FEEcyPserftg3qkRrfd60uXEvAPuYHqFQ6PEEpOrCfg8HUU7PLwhKk8ozBrWUHDpfUoZop6SUwwREZudeSVcsQjeYsYaZ0cRyubBSio9TqHCMLL9jRIPcvHReoZWtbH3T8TUuYP4geA9J9vluvCAvxoT7u7x48dr53Dcg6JcjSKtdfPIz+D/xrjENTBASmCmVMmTR8/QiNb47XBlIgRTEIIeUQQSfjIt5bRwnnx8a4wThZMbhtLsQs92FAp8M4I6lLfKzZPVwR1lHIZydnXLNj/KcBrlxq3zkOcByorcfWB5S9E129DMKLkahJdkj2EVILMKgXs1LBwI1iha5NaWzlztpSL9/aRPvPkqDCzL39jzvzJ8UnXBuR29yzkI52MxkyxrWKEEi4EfxsmzMOhJDqPMuIYE/j6VKu6lkM6UwuxDrwcBcY8JpUBAFI4aU1CxcWYVvcl/2Le0WziPTCoq+8OsSVY1ACGMq9gHtrdwQ7kbaYU7jGdV35MmTdqJMcRh0H439IwCZjUwic+CAvZzTC5/BlPb5CknbvBeU8wtKYF7+7cphblV1UdRxlO99ai0uPGDTC2ZXGGkabW7YHLlbSAeL36bQ8BPaxv1eLVH6g6EPSNTRZdFAOdwepWhicpMJ1wNCKadd7IRHnoLfeIlylMJ/+KUBHpSGbiHq6N4vmMXsQmhGtFrGGcR+DvCFU+3/17UdnRMrV9QuTmZEQrjUNU3Y58zRQOC+Cz4Pm/0ZOpy20n7bNGI0I9jB4LXy4GX35Bq0TDck6j3YhLknEzxL2R+Eei3BIVEJFtl08JdEDYrqNRRUSvG2NBSjrgFw6oEORfx4PEdCrw1OPUVfEyky7PFE8Gh0bgpW3ijCJMwSfcJ4yczFToCYxK+V7LJsVc4bSl8m/bLLfozGqkwlBb4EC5QGMg5B3+fEMoyhZmYno0bN/7BhYHAnMi5jOdIW+1DSC68DP0bUezTOX+yiXMiL+hCBoTwNs6aTwC/ti5sCSBnOBah9DOgYREbJWd17dp1nZsmyv/999/vzAUTw4EduRgalSdZGHydjOn2C86V6E4u/8HcOpSw18G7qQWSdjr0HAa/p1tY2c0zBxhbtFT377aSCNNczIGDw6jEzKq8bBdx8UnkRzneZqxwPYK0KlGaiPB1pF8CzWMwx16icfgNtHaIbf4Lk+x9azYqAk61g1CQSTap4BYIf08Bt8BGR/B8V2MiN13Zn0cOUPmvhWp6LUJ3WhgFTJhDUZycm1WUoZN/n6vFlyCHcHM/NyI8892A6vopawUt9Re04jdC515h2oG7A2nGVxd+VD6ZW/DZxiR+kSj9/4XT0+Pd4icoe/LHAQTi4nBlIHR3hDFgP9E+COvUcNpcfCOI87W3izJ3An4r3kN4j0KgLiPuf2l9H8P9UmXj6vrQL3B1DdAahWX6QOdSYL7AlO9+Lh8Q0qz2IsITnMegkIFLKgiuoPznXTrAaY2moF18yv4cc0BCTwUtdiuC3mSgdqG6RU+YMEGXFvzHTZdrP8rwgItD2K8JBeGA0D6tOF2sgAB1QthOJO5qFP9RYAyGPu3HitvakQr+COVy4PSztR9ddgfMyankTScNMAdqYdalERqa01BNdOHw/Q30NXHTlf054oBOx6EMb7kVgCD9GL7jifh6KEfcuoibLxd+hHMW5scuichHUA4jzUZwXqgxVKJ0w4cP3wnF2Zdxhk4t3kTL/E/wlemW8iEq8sxAUbwpV3C6IRf0gt+DYRoYjxwBfYFeEWX6Szhd+TsHHKCF+iVCEqhrwvrGitLsnPegHNcFEuXxg94h4VZwer/dwN8bnyC0pxi+qbgy3YCtc/SX8/6D92sEscpBPg3KA9zZtSfl6rrTbD+ajTsvjD+KeZNbEGWvKptaYS5l+ZvFr+a0ilNdxqMIr8fOXNtftulY7VEITmBGxc2Taz+C+2WiLSPaDoOweKcaSfenTFjEjSz1tYBHr3QBAvkEvAmsR7h0oiQDKC+jE4kuPNcPrxeAQ2CCQPTT8w1x01FXw8JmcCb0l/OGOIAAPOwynIpZhNLsEUumnQF1pkyZ0oyKCUz9unny4UcB1sWmmitYSDvQJYPyK4j39n9Bz6tuXKZ+tvg3pFc6Avr/RBljecPkxgWEE1T3G+X7KDweYUX9YHAI9HAoasLeNVP6a3V+tnvvC7OXuhVIBfzOYYoUpIKW9Bk3TaH84HG9cAPHG7Xtw8GzDor9nPCCnuEWHpv58pTcwjJxAb+dlEW9FIqYlz1nmH9/DOPMGOWvbh1A8yxobRVOV/7OjAMVMqVcRtNKfv7WW2/9JAbWG3tQQb2pgJy1kiof4Z5CET/ybnDxCfvB713hBk6XIySBmS1g3Kn0wJCp5a3qo1BHYsvvmxmbonOrZ2HgfJJ4SNk5Ww8C9vLwWRJMLZnFgcNe8OP/RWOak1B/XJoT6MUAlBmq7giTO+W5ka7abv7zeg5NZVJB3hpDWFiz+U2L/Dj2dmPcLsA9kzJv4X2ZdySv38Phnyw7nLTdEZC5KIp/ARt5fx3DSYNmb3sG7k9RkptyzW+tvKO8kbeWxHDKyIHWd8P3cKEQv3eBwptFWARtck1rDP5WyXYa5AmHnBYT1XsMYbAXuGiA1jduFdetlGz5UdSl2qAXplhXmKrSaaWvVllKx4a9FrrxHaHRNaFXWh7SnB5LsxxF98ZQfO9Fmo+0edHS5cqNbTAMtOrCJxsPdG+kFznDxR0TU43XBBc+9ZWvXkQ9iJ2WrXm9CULTGZ77vYcqAME73q0AFttah7txtzKy7ac3uM4t3/XTOv9U5YHmSnqPPdWzgduPvJ8S7I0xUIreSqJ00NdN+aUoCBGd5Zx2LryQP2tjFHqrC1V+Lh5omqibJ13coe9StyxoRY/ycpu8pyDgIrdG7WL3+ItgPeIyFubLPAgQSpiOzObtobwvErX04HusIYJQ/FREkP4/UnIG7F5vweC5I9/egh9x3sZK8rRVmMYKHuHRP1KQrCgJ5dWlJ3vBcM22C+2/cUnQyUQaj0Avwvddbpoc+U1BUgKfFeamVFIWEmmlmS3U3oEigaMStSh1L2EbDDwtYWu2i/e173y4lLdfmzZt9tTslLaUJypTQheLA+WKrcnXyb7DeRDWuqTZiu3v+4fi3ArW3wPoO+OHsjaycHcdSpnS3zikWyC0XuX2Ih06dFiG0gQmK9iG3wdrIPIWlXTLI734ooYzzB/VgdUD3uRPSSlIkyZNfgkTXQH8asqUKe+7JFLRV/K6adzonPgprz5nULrxv4Gtd95551MTFKJKkUDrTzjny4UW2wHrhSvMHs5TVMqPYAUW3BTGIDOsJFmpR/7/cBqKGbfBU2Vm+nCupW1lZWVghZ29Yc9jWn1nsOFHG86LJOsxLWky1xRCrvgq3liY5at5CqIVZ4TlXKNQLg3ys+5BIQ02OUjU102TLz/ldsU8moF7bpS5Ba4rEL7vhQ9m1wK5KIo3k0UvoT/gDAu5NziX0IT+13BT7B+BVelmWsqvN+OHHvgZepHJGQOKBhDXi8CXF9yk8OLCEL1udNgvmo1v5g/zImVlCAPXtwGPistHmBFVZVnbb799VxTEWtw6VKIOQgWYy788hXuYKuFmKwECvj8m1kIUpF3r1q397eUogy0Kas3EO/UHHd7YgzyeS+uqXke8WEz6WcIJe9zLh4Jsz981K8599K3W0SpfJqb53XRp+zl1qVsdA6ZP2kASZICWtlgBNh3vpUIhNe5ZaVngRfdDDz10T/tO4prsihfGH7nGB+uVXT4lARcdZYVEx+Yn1IhLWhrCr7GHjy+V+EazZs3mWCbZrnTPfe073y6VvzMKsBGhr7/jjjv620mo8MD27xBe65mlakben8fCV0HD8pi/i1waAqvwWHDAyajyA5CcD3rr56BlihOUNS/0Xer2EJil4ynrQysAXmzToEGDuENuFo8reXEVQY2D5GITpieOHy9/Mt4pvsrHF7gqU2YxQWyhxohUJSfFQ9suaHVNiISJLjR41UWJHuYYhLGNG5ZPP4qxC/81vguuzpT7A2tax2bCA3c2/084L4aTaNezBsU/jjzemIk0PxC2WGs60NLZSxH9kxPFsKLat2+/FHPxbfvOpgtvuh955JEHuDAxPV92vzGzzkhy3ZF4577KKkWw/1CXX/zJypNUMLNSQgQQbGhpu8oWoSLICI5IXacOwn84QtTWIuk9vmcRzt+3pHCYepbFF8itD471KXs9r79FBKHfPYbPsl69eq3RTA7pvL1HxO2AIpxv+GJeTSJuEwIqWs3M2Or111+3sYYlzbnL2s4bFJI1QTOEoa8evUZgLMmf9QyiF1loaeDJ3i1btvQbGQuPudY4mCJIjuS3x/VbWLVdAS/Eo4GmKYlwSFoRdLtHu0jCzHc1TWhhmtqFqT+z70K6CP0G8K1Uryc88LeWi6k0FWfTT37yk50REi+O1vRAxizewqDSkPcbuTQIPchnptlixgUyI/L6UP5IcJ6Zi0KhuzcN3PYGe5999plNnX5k3/BnK/gSqHOLc1xrVKUQepPKkJOvJLwizpTT3EjE1dXS3X+D8PgPax+uuaUNgJf4kQXyUMGLUAod/Z0GCjr+21iLYXz/IJTYf3S3CGQ1vYe+I54N7AA4XGkwOfy7hPG/pDB7XPvdwnLl0lPr1GIuno00ar1cvKnTc92CKHtIeA+Xmx6/5EZylNMnqXBmoWTT8mSgkmo+rWlbeod2BgAmzqFlG2HfcmmRvBVqNyzfflq92ZQpenW8Vi3+etZE2hLeHL9mpb6VCy0d5IYfKdaYMWNGfvLJJzuTxu9VoNWfcmXFfZejjz66Zzivvpni9jY5RsVlEDY6g7zJssKWipPdBDRyw6hbm6BQz7vvRRdd5G/odNPG/JIb9Rw5fXKpIKYcKiNcTspdIoPYg2DWtsYFKQc2rLfQpjBt3Sb+EIsvlIuAz0Cwt6Pit1VLCB7rUe5OfNfDr/urxgg30gQGqArTgwINO+aYY1YccMABh5Cm5eZQrzf50vyYHdfDj8C9wmplmbD4PeH+uMfSZ+qCs192prDC+WnUjnXNLGYkp1De15aOOm3YqFGjROMQS5ZzNyy42ShQiuHClaabsqQNH6HwzA7LiCB+Yn65TAnui0B5rbQbnm8/A+yhCGkLNY2UvYkZK7WC3lQtOM9lcD4FvakLrl2jcENBXlM49J5q8QjMWkysSfpmzecohKov+UdZvI6rXnPNNY8Qdj7C9qmFZ9GdCKw1WYTng4I3baAncHUprAvQAF2H+RlK2COBsFkW6y3kmqKYP20SEah6CM5XapHtwXY9wgVE13yjxRXSBY9jUYQ7YziwLDOvBWOnkfpGyPsLZ00mIPT+GRHDV4uDmt2SCUW8f4Ec4d9pDKY1HplowNdJwG0Ei31TDYD7L8FgfHOmwrL90DPtBfjAdUoqL1sP9F7p4gy9J7iw4d8Q4iVHBXskvNV9lNcUQyaF13LG9gnZt3qPatuKDFp3paWpBIb3IDyLEETfJlcgrVBki7w5R35+JdQs+I0CV2/sQCXPZdq5pbWQxHv7xegdDqOV3DGMFfFvtmvXbgnpTyC+scUT/gVTvmsYy9xD3N7qPYlfw1aWBixGvkQZvVGckZiZr1ueTFytTyG0e6MYv0ZYXwX+IOA1CMOEvqzMqgHfeghPDtUYQvMyK48eZE8aiCb2XQhX9nF1HhEkJdCrR0ogZdmg6VtcT1lwM3qwQdshdL5AwbypTHlqMOw9Gn/AxEib3tLkw6WlG4TgViA4XXB0xlyXSx+CX+ORNVT8x8KD8D7gG0BJwoYwPom7FekudCPpId7QHbfw4FKFU877rBm0wF5/EoU5WnmZ/bm5Y8eOa3VBAhdTo0s7tyfpLuTR2KxS+eyBf2TZpGtH11DmHLbbT6LMFfC0M5stfwHMI8BvX/L5Yz7lJd8CcB9D2k95PyJdC8ZXTxGVUetOWV11Pgb8VwgW/tn777//NPzeeAo8dgE3rQfZAive0njEGCmJvfYdrP0MaVFLphq1BwF5zgWJadGDykt6Ftzy5tIFj1MQtp9bGeD5OHgN1jfucJytdKshQhZ3Py+COoj4CqZ/DyWtJ8HKx7NIMMnjTRPjLsOs6k36/2yORju4Xgce3Y/QvsE7gW/fPLM0yVzSz+OdHizWy6FJhfGU9RhKeqqOGbh819FhygtMvScrJ1EcZega0v1isCVLmuJ+0U2PWXpBLL4gTnV7ELf3EOIiTr1IVh9a4N2pQNndCwD8I4L3rFsALdlBpPEY64bn0w9uc6nED1gAvC9W7iYEqwG4eQefpNS0zmrpT6NF3MnFjbwbMRnvYaJBW2d+By1+iwyMycC8ljzeqjtRm5i9+zut7m4Gg7hGbIv/H/tO1yW/b77ElEXKp7+O+AC43rS0YKp3wtxtDT5NUfSv1WNB8yvQeEu6ZbrpKac+Pa16PM1eefKD0nzjpgHHvd3vUvKrMtVj5ExAdfhIXXAipiBcJ9CSjaFSC/YgyA+zeKdjtF5LTwUj66snCiHCZnNepZn2VqEoo8JI0lqO0jQtPdBB5POPESsdArs8nD6b38CHdetGUvY9LHAeo6tNxWf43Yjv7oxFLkUJ/gbeg6BjCumXgO8kKYvSYfrtD87rMsUJmIHjyjQkZ7kwKfufKq/8VJMDUiAq+VQqcITL2Hz4JdRU6H6cpfYuXVCZhOnxigevv4osTKVjCYszBcl7nuIRgsAVRrnCHRy0yPomvLpM5+NRzvqabaN8/cvWHSjMx7gLEpWPML8Lul4vJ8VGeT5KlDbVcMp7WjywJ7bTwDt+LBjgG1gUtnRlFw7oBhAJF4J0Na3yPVTIC7Rq19NzXITtephuazdG6UAVrd4lVLa2euTlUeuq8hEcb7rVLRQ81oFjJwkSghcnSIR515FC34EIQaD3cOFk6gfHSfDuIXh2nPg5evTohuB1CDjfCo5DKDvle7Hg/V2iF1jH63gx9fIz8rvjprTRJbuuI/WtkNgNKysMEPGz8Hu7nVV2+YEDdN+VtL5/pgI9s8WYFXaJn0mlPUNF/dSu/dH5CrXctEyB28TDebPxzeLc8dp/RSWuDMNDAJ9XZSJM6j0CQqRPhSse4X0vnDeTb2CvQvlGwJc7NPAfOnTojuDZhu9zwWkAPJPAVeuBz+cLZ3j7MDy+R37gDa4WsC2ZtM7lT+4wrbsTMKduid60mLK8cZjKK8Qj7fURLAQCbplUYh8qYLbDoJS85JE97W95RyB6KiylzNVIhBAOE95UZtzVpgjpUujQKnFFVO9BnLaSy/Q6pxpFx2WJKcUn9BZXoXBtGROpN+0I/VeAn7aSxylwHJAqAihjHUp9gPDG/xllTaZ3rEc5p1aRtaro2SiybwmooYNn/l3KlLWJ7+4qt5CPFMSzLQuJBMy+Eob49mdVnI2Kp+IG0wMdITp0QRmVeiMCkrIZEQUzKgzh7s3MTgfwDVzGrLQo6sMqnzRnEh/oPcBlBb1c59i1Nxn9FRq0DoVnV+g+KY0n6Ck60TDcQLi2qmfExzDNwJtJ2I6YZw2gYTbf6zCHOmrRkm/FVfdZAr/8aWTtVgb/wK3zKEgP8bOQj81IFQwHhFoDxYxbOtUScNbC9AftCh72MR0Ikz+pbg2G89Eq/0uMQhjjeg/iZk2bNm1XlY1/cjgvLfy9yksvcms4LtVv6Jth5o5g0QhcJqUgPKtK4eKDQnjjLZS+G+V4Ew7g8FuVD0393bRp+peh4O0Exx7q6hUXRqEVxAZIcs1vuObFpVXSeke1bWOXma6fSh2L8Hi2PuFbI5TXIbSZ9iargdkFpdsb+HG9B3EXimkI7J9cXOSHxik0BA0RiK7k9Qei4XSxtEtQwC/A2fvvEEuDsLxjAsUM1J58D7K4TF1wWg2Oy+WGYRF+p+iCPn/xFhofVBh4VlvZVQ75ewqOPdD0qls+5fzS4vLtaqHQeg9voSbfCKg8dsHexIJQsr3/1UKLhah9uCTgLXqT+0aOHHkbVwTdiWAPZGHufha5bB9QWrARkBfAdySV9gzwA1syCHuPLRhPoPCHAf83LmAqXAskV7EVZDHun8hrN9H7yRDMZQjiaOL/jbuKBUb9b3prJeB7BeF3ULY3kyTbn4W7B9PlG2WsBZxO8E3mncv3EhqNVYRp31sj3sbg3pxyfbNH5ZPGOxtCvG/uQEMLxQFDC8e5fPzTh7ksJBnsgo0/aFG14BTXErstSDb8tEqy1zuJCfqvblqt2yk3bmdtVWUhKLPVapM3YA4ibHOA2VrjHhTlizAchPtxlY2CXWZxgsGrv017nJ7iLMY0u9MrdAHOS4T7rTh+9RpdY/l3Je0/CEvVnJISfAjO92MSXUBjcQY4XIJ7D2H/5B1N+dqiknCqmfxr4N3e/fr12xravjb8weMm4QS+f7Ow6rjk7yk49oBTuAf5lcXVOhfmPF4dplYnj1pMKtrb+CdGIzCdqYy4NYzqwGYmxjOtoOeRcH6U6jud+NPWdYTzdgTrasKOQ2l821uKQdij5PVNL3AbC47eYqLwBfczENYpYfjutxSHfF8D+z5NJNBjHkS+UyjzPuJ0j3HChUAXjutHKby/bqBn7AQPPUXCXQ/8A2OzTmPc9On6U1AQv87Eh1rzTJ06VX+mMjddhmaaHmF5QVtAxGhNVSKEfcEjqeAlK5PW2NtESUWfjRAGZq3Ip+nRX0RVqs6AoFjnI4CaivXzgcsP5LnBtn8gmPsi8AMS4UCRuuztAxThCikE+6X2V0MAnVr3mJMoX6rh0PWi8FfPY3mA/Rm8q4uSaJtMqr2ZZQ+4wO/l8gdawj3I+W58rfHD8JMCnMrjB5XwDRVzqDFbykql34mgptXCkn4EeRtCSxcEJS4v5dxmZchFmXajnJMQ3CelCC7J5J9I+hvsfDnK05i011HGIjed/KSVWfoRynWFBusoRgeU6HJgDiIu04mIQHEo2yXCHVzetgjouEph4NbPwqrprhXvBMseeBBoDCjrEIurVS6V+1A1mZqVbBIklOTa2EV2Hu+1Mg5ejyNocUIZLpSKnKAZOGVkjHAp34PJOwK4XlIEdgTjigMQsNPx3w3MT4gLwEXAdGfvW+Q/nXUMbzDKtplGhP2BuO/dMvnWDsNREk7GOntgsjUF9hnAfRO4vmnm5snUD9xVrNu0pZdtjt87XUh5Cym/Ob3HHoRlqow6BOcN9sVH8NVCoT/OEf58+xMDSlNrHhgtm7jgD8L7lgTOZby2Z1BxdyCTMxIhiNA8p9ZNPRHCcgzC+mv8TyI0XhYqdj75A4N5RRC/Ato/J+11av2tXK0oA/NalGy6lUla6cVocLkZQd0Pm38HlLgHOD8EjEAPZHmy6YLLZ8IPt6/BRXm9rf3gkI3xoxYK/a0ksTHNt1aW3FqpINC9E5UfaCGNKYRLKjT/788yEbQKgXia8IwGhFZG2AX+D1T8OSas5kpoJRyUranXam1BB7bWF3Sm/FngXAxMXylUDgLSjfBHSKf/KJQC6dXs0s0ozH6sLtdHKbR15I+8Ods+E+aJvuHJNcIR/Abrm/IXglMzeryjwDHhzJfSpvhM1syfytCj3dmUNcXJq17LPwOzOVXOfzWrW7CZXY86iG7Ou9BhhIRCt3j0Z+r3cG3F4Nu/sIGKWTJ58uRdsPe3pTU9ltbrdeKzviGRVv0xyoi8JUUmBcLdFxyfRmm+oSIXgtca55USf0/cKPDrD6xb6FVOI08HtYxOtVYgYAdAx220jt+IB8DYSL5PUZYr6VX2klKAhzZtXkGaj7IkjC67q/SLv9DRXjNs+L2BOPjcjdn4E3ANmEFVAkuQALg6J+MvUAO7NbzwGyLi58HDpg7vcuGVMhgO5i/s9isY3BXG+LMfCMEn2POBwRjMGejwVf+AFFjcQxj/7cRnzQvcqQjCKclqQgI8Y8aMVhJm5/X+gzCcD8R0d1U79VC4j0P7JIRgBe9U/E/TIp8jwdC+Js0KoVjaTzWE+DjzLGtEpgAI/g8TLeDzlJKDzwIdMQC3v6SQPaUk8HqAyy81HJTr90z4A7t93bRZ8JsyCJQUQo8UxVMOd2zqxeTzR3alOAgDluP/X/UM4fJhXmCvEz3LiW4aKuyhlGqhmokQhBfUa7hlVuXXdm1moQ5E6M9HIW4ExiPQ8RE0Dud9n2+d0utL77G/lAJFlHl1MbS8CC9c06KaWGcvG4pxsQbj4OZNLPB9o8Za4LkuW6UA+36Xp/DlRBc2vPnAjc+iX4ogBdGrR66nJDHF8OLdbt9Lla8ftjNsDXNmwfALubnivahyiZ/k3gLC1ou9SfempaWiprH1wT6z7nJe+izK/yld/K1cCfpY796911RVCMKvvy5YB+7z6C1mUMErwFE3v+tpyFnvZsDdl/d8bifZi7hIc66qcnIdD/6LaZAGsDXmv8GxIUI7kR78A3DuDx1Zkxv45Z99F03A7uDSRh2Pd7+z7DcF0VYZbbWSa3+jkOWi0gQHw1tgmgQGq2EQtMBnu60JzHzaTUMrfQzxm6eM3IQ58FP2p+AT6MFcXMzPTt429Ar30Vtoe8dYBG0G7xxe35zMAXpZB0lv8ZLGTQjoZIB7+8igJ2s7ooG7ngbkb1gODY13cuHxcy4x9CiXu/FZ9lvvIbDhHiXLReUAHIPbQ2GWf5ab1tibcrSiNIhF8PyZLpexufJTga9qRslwiHIpW7censo7APyyPpGQK9pcuIwFjkRJzlEY7hBoec2Nz8SPog0CZmA8KT5qrxf8+tKFTSN4VBSPsxQmBZFimHJkCWyewOhcBQzzV6fxz2Jg7N88qE2HMDsn075uJYX94LGU1u8Rtop4i4TJ2KFDRQjDbfRAE8NwivUbnn7LJMR2uCN5F9JQDcoGrvDgE8y2k7W9J4pn9Lyt6Fm8xUiVB5+XaT0qKm0Ww9xeJItg8wCKStJtg/7OWJi3gTHLgW7RtGxPZqPyqgODHm0W+N2MGeCvBLu4uX5NW6NUx5PnFegIrKZXp+xc5oGm69WDUIamnn8E32oPysm7HJpfg0fHhqa6XfZ4ftL1Jr1PGniM1WxhXMJywBYO0Po+4XMMD0IW2Pqs1Ws3vhB+WjpdHnEzY6oqFUWU0Yq2Jv1F0PKalKwQOCcqEwFdyop9OxSj2r0GMDYi3F9Do9aBAje4b6lZb1uJTa16wfDxBhcvGj/v4gs3T9m/hQPe9BTd+0Uu0xCoANOYKtWlzv7ZCTdtvv1SFA3Q2Tio2baUHu2ngsbjUZa/IlRfIFuZ7m3KiGwU4zl6j58BZEtTngJEUwrovxt6DqG3iJu2N4ZoSh/FuR7zaRcLk0vdBrYe0esEGkM3bchvY4lQcM35FIF6rUWxgVMdmNgJ5vsKgH+i7sAy0nXGAsHyVqNTqMe8JEFRllLZL9KC9tLA03CtypVJqXUX8vbhfRA4I3iX5AVpCoG3G6QcuO9XVSZ4aTvQNPAcgFL9DsU4WHVRFY0oj44Ja4ZvAGX49j+9r8ab/s4KcFhLT9u5KnixeFd2UsxSGslEmJTBVQw3rEJXXVIB46zCYNw6KtE7XWckUjl/svhicsFVG+0+ZybmD9zK3s7wTdWVwmi3MPl/Bo3/h8K9AC9GIJS6ITHr08bA/hQFPSvMQwRXq/4ziB9GT/e4pl5JdzCzeYG7hpPRpStNZXIBR6cXtcfrbDc9Pcqpbrnwbax6GjdN2B9b0DMl8xvVcLpMvg14JjAyySuitDhjj/xSlg28FidmPskZ6QsI8x6E5TbOft9o31oP4VvXYhbtg0Av5h0ELW+y8PYh58lnVhPZupiVjThrvxuLak1ZcGzDwmQHLebxLSWswL87fuu55Pozf6Eyl/K9Mha2gtb9WoR0J2Br75Vm3XSBw2SUYjY8n9e6detFofxVfmoMwqLwmeB4Nrh6jQRwF6Isnaiz2QaA734sBF9i3yhSP/Jdat8J3LooSQV/uWGLfJId8yfIUlrB1luY9rvfPiUIVR+3daElG+XOiNA970h8yUyjInTzeF9DKM7TgN0nNEsebXehl90Z2A21VQR+7R/1Uv7uSqO0mLLeWZRsoADcNpT3K2h8h9ffeGh1SA/Yn3L8xln4EuZfGUseXfzdOwVcPHnp2bOnpo0jZScFGCWTRAT6THOxpvXS/Lg/NUrrokM8+7lpUKJbrAJKyYWWxZgTgzBbbtBmTbvLy6Wt2P06Ooy51Y2e/BoU433RlKwO6FUCG0Ex17S/y89C/u9TNN9MKcwtdlZVCz8Rp64xUjkMIi3Kmz4H8SBQt1mcXHqRvWBsQXe/uvhVxy8hgQadS/k3762YNprdagcsf1LCpblQfvBpSS/RCxz/wPu6Wn/w3iLhSYgn7XdaE3JxR6medbNA82NufMwfJR85V4yoQiNwK3wQrdP52KxPGSYI00R6kf1btGhhNrRu+HuVzYCnWpoa4q6ADt1VPEkDV+gei1D+wDhjBvTPxjxZk8omynR5ocmRNm3a7AA/m/O2ZTywd2ys0x5YezPGqdaN61gDN1CPtxs+MvOA/xXw/D1ZmH3H8fd771gaXJngklWNUTXGyNsTueSft9LTKAg7+W0Gj7MRDO+CORjaHibq0oX3DQy9yqMM5k8hrmQU33BP4mpssAck7QFtxyodg1md3V7NQH9+ZWXlEhRHq936H8GZ8Ef/uT6V9ItJmgofNpG2EuGXwNfFvxevdh+3AlYDwprgqofP+AG/xUwEPOcCotzjKMtXDnCfzPacIaQJ4y7FsLFqXpXExbeo/fQQj7pdMQx/yUVYA3cYHFhsctOX/YXlAObV0xH1pf9w9B8auRtjaUwZpCiu3wVRUn4jImdIM87oilL48/+0mivpjv/LLRAGHwe3U7KH/Vope3LOAepqDWONQF0xKXEc4f5ubep2KfVZqfp01jgkVyX9uF1hlYPtTCjVRWX0EG+7tYkN+6gLkzhtcCz3Ii6TisBP7/83t57wV6Awg1zU6GFedtJYg+vKlxNdIl7neKIwFjFZsVcTkQ8TT3CZKrsWm7XSTc+0YcF7EfBaReu41sW1tvrhxSLtCHDrSIu78MPnD7xaxwC+eyxNTmXIxSMffrMRjSi5OesW2bKg/+jW3xX7D7M6T7qEEqFLEt7wE+TRA276W+jHmevvxHtTHosu2qLggz9rpXoC0a1o6AK9PPX1ulOHpd1rOITIawoipZBy5Jw4GB7Yt0Prs5oWKWDfamxCeHVPG5J1ozYJVnkaUGXTQk7CXHgOvM7k7Lo3/amdqvrDTMKzdky1aDUgCWLwZkJ48ZNxos59+ONEvGu5T7iHhKnYnmwIs2Do1dSbXM1V5/TRIZqTTjppCHt7DraCqAj9sf2ZfPvl07PczNToTZYmDRdwG7T1YTzvPKYh5V/IlKR3aYOmUolfyfckpi0XjBo1aj5lVxx88MHtSduT8KNIfyBp/s1axU0tW7Z8ivDAlUZp4FKtpAjdGsqsx2s9e7XgZJhJB676sM7xqsHR+gr/0zLYrTvSDCBNYHXd0hfaTUdBTBEkgL4QxgjIi2K4zGJd5MiddtrpPQmBwhHITYSdwNrIvyydtivwDEF4U902bVl9F7A6nzEdgZuCwE/CNJhBpNYddkD5OlB+cxRmT97WvHEXnJH+M3qzx3bcccdLWL+QQqfDcx+PdDyYeePhxcUsyO3F2lG/QikJDVR/aD7DxZ1e9jf8CdBfnbC1NDJH8KdGw52wkvOqUm1WoZAtksu4CoQv8N8etERfu9dYKjFTw0cg2P55EilSvh+UaRU2ti4j8Keoc4UDPHhWZ/mNUTJHod8/Z5GrcsNwqZvv2XUcGJjTmzYDl8Bdx+AbmIU0vIvVtV7C8DOlMAWRm5dxhiGQzMWW1S18gVvNabXuCuch3R/DFVjTvuk1xjELJBPTe2gYdoXuE/RBC91dvV++aKZO9E+/p2/GZMsvyvCEiwM4zbV1jy2pitdnymE9hKscFqewonoYnIcPS61CIPz//RCyOv6JAP3TrZya4qelng4P/gCN3gXQarXpNf5I+A+ikQbjM+LPJrwDQns/Qrkq17TTWz4UFhIU4SQUxx+YCwem468KpyvGb+sdJPyuIlSUwmqm7opF+Ce4lc73aCqkoctstagIzXduulL1S854P0cQr9K/UGmLDVPLR/H9dxTAO7EXpk09CGluQ0nuwj+O/P4KdjhtJt/0Ev/RDe0u7zWjR/gUFy519Cl3EduhLjd5cfkdJTBFEYJmRpnCFBfSIWwwJY6lwgP2PcLwRChZHS5TOBjh8M+VuBVW7H7oW4NQjUEJ/sKi2/G6bwthPxnBe5i4dMynlRofQG+VU9jp8kS9me4QCPG9gl4scFsi+K4Ad38GMpS+qD6lAHrUe9jMlMLkl6up25J4EJRHmDr8tSFL5epW8kuYyQmcLWB2pw8zW08ys1NUZywMb3PBfz2CtAChm4lST1cvySxZBTQ2Zhq5C+8efAdaastbCBd8V2Iy9eYu348p32RIRxAuZwo3YHIRdguzazcXAs/qlimC3Le6cAqWj1a1gUwrt9VDwJbRUsWtP6A4F7vpis0P3ujEhmUovRYgp0lReKt9eVuu6QO3jVokDVe+JgcU5ZYPPR9hWsX9R3w4b7F9SzlsDFJsuKWMD+semq0JnIFGab7VH++EgWCW3eRWXNlffQ7Q4Fwb5u+UKVNax0w5HzB1M0+mYTht+TuPHEDwL/VrJOah1RpKaxZ3swcVe1M4bU36RiB/4M3pMeQo5dB6BzwP7JejJ1mvMVMeRaFcVCIO0HI9FBZ0wl7WRsdwHnqYm8Jpa8I3dL2LQLbi7YNw5mShFOW4LsxPTanDa10KF3gYqAc2LIbzlb/zyIGvvvpqeyovsMqu2iLs6ajLkLWOQEubEyEKSEmePqDzH9rzZCzXthyENmtT3OqV6JEvMPjmSjlQyBfDZBKmcx4y48tPsXBAV9HQiuoPIgOPlCSqJ9FiWnhAGchYAh8I7vfQca7qgBmlPaD/deh9ibDOjAmaMXv0N8gITIenSxZlzEfg4y7GmMotiPQSccoBDm+E10WKRUZqPR4IRDsq6JuwENCahpXEa90QpkOJS2dNIQy6IN8o9kKE8z5tK9clbPjvQZD9/VfEr+XtT6v/Cwk344NBfKe9DgIvv6Q36hIWLPUcUcpBOaNR1Ebh9OXvIuIAA8a2VFSckjCYf1p/wBNGlXMJu1HZ7xZE0tMsFCWYRu9wl/72ja0krfHfR9j0ZGAQcu3ZGgD9ukk+sPUjWT548qoG32F+6Y4r4MX1HPD8S81khdOXvwvPAdfW9fy6OBrBGRsWACrxfebv46aAaYW3oTf5I3mKblyCTM+nlxtA73gJ22n2j/UIL4FrdQ+HhdkS+AYuRay8OmpbCOEtwWVoIAMf4PilFLbwopA2Blrq0FtjHymES6S2zHgEI1Bto8wtBGAULWPkeRGUpxd54sYxYYHI1TeCthb85qHIX6CwL2IiXa0xBoJ5FYL5OuXm9M93YuXG/ZegpIdtOzoQFtgDJz6o50BxS1E5RJbkxzbr6rvGPFGK4YbJXwcbPdLcQglmogyRJ9o02EcgZdPntDdB4JegBON4x2L+fMk7APv9aRTiGcrvj+ANJM134OFfdJArxaSMJfDjjqFDhwauCDVpAZ+zwMX/70jDAxyHl6JZ5exHFIluA2skl7wrokwhPGXg21oCi/OIZLW9FZX7jlWqubTWG7Cz7w4fuDLOIBTdyBc3dWz5M3Upfw2CuYh3qXoO4BVkawk0volSBs73Gw90QpOe+H7wixu70Mi8rnhLW2Ku/kbBNugKdddfYqREo+sqhymEuXE5pAS00A9HCTUVPTRqpkZAdC8XLeuppPkqKm8ph9H6j8aEOxEyrYEJ8I3e7GDoDtyCKHrRFa2Q3xU1dR4AUNwfnvzElERyk1B2ipuM5NhZxaZKXAUV+zsqOM50kolBK/qHqFkuoaCFSBTsQoTq61JWCuGO0Esxznf/1s5lsxYcofV6eBLYdKi8hC3QmMhNX6J+yY77ligZydGuluZjMvwMIRmnCg8/mBsfoUQJzy1IUVCSC8n/MYpWEJMojHMq3+C6FtoGgfv5yXbWMh7SCnzcLJXKkGKpV0leJUUXa0oQRixReDhd7fxmh2lTxh+B/6gwQaOVXIWS/Jkdwbsl4U4FwtINZXsQodMVQUX5IOzTEfp+McGWUEQ+mKB7kO5RKVKYEMK0ZedJepxSWwAUvWpENb6QW37S5QCCcw4KMTssFPqWcKEov2d+v0kyuGy5aIjJoj++eQxhmqq8hXyg50eU9mXGTmeyvhN3LZFLi47GIvzXkmduFM7wYCbjs8A1Pm7+IvWbYvhuaEBepGgnRyth65Y8W+axzOFXIlD/QEgiz2wTPJXe5g8ISpuqStO5eEyRY3hv5x1M3nlRgpfNMMpYjCB/Bg0P4PZmjBC3+h3Gmx6jOTRJMXQUN+4hfAPwnmSWKlkvGgZbqG9TBJVvZrdcCzd/SvgVTBCTYGfdX0GP+9KbHMelZzdwOVyknU3vsAChelqmGcdMv0xCjx8lQeQSu/Ycm92XV5fO7cWx2d15dY/V1rgpnbRDgmX6aH+VeruZvPrbtq/4/hJ8J/LHOrrcTsemkz4o+QGkP48jsWfEcIhLj2J/Qe9zY+PGjYv6X4RB3ATf/YdkybdeyZK5eFN/iklBhItVquxEEVrQR5vwunfvfhHCfBUCtEcUMgjlalrq94h7FmH7GKGfH5UuUZhmyjp37rwLilOf8/OVpKvHu4kz581MaCljES34dMLq4OovIHROfQl7sOb26NFjVSLYUeFaq+Bmw6NRqj7QdQJlxO1HUz7KnEY593BrzLPgtyIKVhGFSTlMdoSW/CZDRdHgCqmMnthf+RoxUhbzZwQ3G5lpQZtim9+GUHp3TSFciZ4ZCPCjjFVOrcrezwZeqcLQLl9MraPBTTegTE6EvMI1zoDWm9XbpQq/CNJJXmzwLbkx+SkaGcoGj1wiBU8EK6xoHgk9ynIFLWvcxsew0KFMMxioa1X6ai6UOESD9zQIcem2yg67CSufwfb23AN2ALhejrC/jNBXpdiatv2W9Y7r2NjZIg08izGp8SkruLkVkRWAGQBRhatrNJzkd7vNDEBnN6vWP9q3b98bk6ovJkovzKptk5WA8shk+YE0up93AulHqyVHKGdgJs3v2LHj2mT5E8Vp1zH/8tsYU2k3mYAopf6JVhsv9S+0lYSpkUn2kGXDMHB5jDHXG02bNl2WLHGRx5liaLyRNbkxYSwG2oWL3oIOztNlBD1EZ4TyTN6TEdS90sivTkf/QaIducsQ0inKi386Qjuf77qMS3A2eg0Hwk7Uph0op738JNXgvjGKoBa/AW/KdQn87zG39Ic1r957773D2UJTUjwH7xr92MawMJEpV3A4YzF8v/fee9szrduL8ccD9AwjEeyiWVkHlw0yocDtr6zRnBD+Q5ti4F+x4pBvoVR5Vqbcgs9U5aJimP2qd9BBB3WmR+lOi38YZRzIq/8dz8tdtPQ0WsP5gd5lLL3FJ/QWHzIOGrvbbrulNeOVC96UGkwT1mzjbXBlCshvrxRCdrG6dIUpPmv2IrCK8mHAvCOKUsmYpStK0gG/1j90dajMox2qqzj0DLqpfTXuLF6NcXSAaTRrIt/QY0ws0TGFTT4UhdlngpwtwTJFkNCLUFOIKEWo8YqRjKm6fqhbt25NWKRrwsJeo9hYYheUxeMVbhtem/nawEzUOL7XoQgVCP9c3NmEzWKWajlTsvP4W7N1ycorsTg1ojXOujDlsBZArl6Fi2Dz4y0/ZQ4EOeCcxVCEyUwwUQG+hEi2HimAWj+DqS7SzCkro1b3GsaEshvJAWtAJSMmO+aPzJCPQBPmbJUVhqdvEZlTxWD2qCfbJ3SJQB3s+40s0D3Dn2bOiyIKk6QN5sxpmCtaL6jAZte7gvdTvgdxLf/0qHxRYdoIyEC8F/C6Eq/xhEcnsMZjAg1lbeHz5s2bR27TYODcmXTH8Gpbif4m4HNw/jiqHIUxA3UWZbWSHxqG8xcOn8gf9ZBWU85aF/GiMcWehT8/htNSdCPGK+cTXk844J9FuufddPD2vwj7ObDM5FmKWfg4NNu3m9z365xJp06d+hLg7S8D/lYsRPZv2LDhd36ioEeyoj9tqujfv78rMzmVnSAKuf/yiKQYaw1yXyIlIFx/kaDZg/DFnanWyTjS6fKFyH9gUl6EegFp7tP5kGSIx86P3AmspLeKED8WeJdGXXfKKndPw1cuOH+QqExtQwfWYktP2kH4xeu4hxV73Y07xdLiX6F/14pLSAAwr7R0cvneQFopu/8w+7W7xjpuOsr/lZ8ggQclvtnNw7hpDJsjvf+QT5BF9Ehu9ETStjmqtH9dIvNGiYTaqYz1tIT7uYVr5Zuwl500Sb1U5lgq80gXhvk5KLQf8d8mBRCKBL+3w1tNdFsKgudf8oZ/9rhx4+JuoFe54N7HBYkcL+OqonaGk+vSq+2Lovu3JlL2W268+TUVDR0jXLjyU9aTlsZc1k5OBKafFFx/jLpEztKj/J3AUYug3oN/BdcEHRSLTyQjCq+ximG8KYhblYLQtd9glSWXyl7FOxDv81T2APx+i2vpaAH1z1SBCgPOgaSPOzNB/kW8X/Nqy/lk3rizJAjj++EWlDKesPKElnqVKAZSZpxyk/e8qLQI428dmPq32Suj0mnBEDy3SP2WTAuB3drNo8sqwOGlLUn4O6mVKx9005ifNFtD6yA3LXy7KRZvyqGeQuPT8pMPDiRTEG1ZxyTwz5wjE/NoZY9y8dI6BRV+Cum8f6hCGN7k5GDgbAZ5/S7kbAAACs1JREFUmiN8AUUi3SJa3KsRqN3VIuvVVZwI3yGEvxKWPwm6hM3KZoxyritIwLnB4sxVz0O5OvMReBDCVy2N6xKuC+W8h/LX0tIHelOlJbJCPUssmRqMNbz+pdbgfp0LU374sxt4+P9xjn8108uHh9NB+38bXLnQPDx27l10m4IomxQk0AApsPzkgAPJFASTqD2V5P+JDGkfT4SCTBwq+F7MgZbhNOT7q1vxCOJEFKtLKF2gwukxLkbwAltOULSzLU/sSlT/thDwHEIZARgo0c8Ji2vpgTtfSmuw5CKwOyC40wxPcBwhpXXTyC+lIZ1vhqk3oOy3LR/+79kIuVM4H7icY2nkAn+0mw64bck7x9JQxgrGT91icExBTEkCdIbLKn9nkQPJFIQB9T5UlH/5ABX4uf7PMJ3iUYQW5HNvEqSxXxXXegLThMAHT2scMO/opT43oWXwvhVCNswECnchr2bD/Afa/AkIlEKr5m5LH/jHJpS7hxsP3ff7gBwPON1rZZJ+TawROdHC5KJ8fZ0snlf4kjdw6R7432jp4NELLgx4ZKaV9RZ+72l5ym4eOJBMQRBGmViBQTWCM1pnJRCMuJ4iCl0E7wK34hGKN6PSERbXKoZbdeCs4VTfPpYfIbrVhY29fpzFyTShrIkWT9p+4O4PrKEr0BsSf72lxd2IwB5vsMyFliYohW+ywTtvEK//8KAs/65dYH8hhbB85tJY7AkOfmOBfzFmYCW90MnA9Xs68n/s/ImP9RoGpuzmkwPJFER40Bpe6wiO70WAdDH0+1Tu70kTNzVsNMgE8TPhIe0vY3EptYiU8Xc3P8rZx2DjP5w4f1BPC32XxaFIPYjzTDQJH8LZFYF8wGCBv2sKVYgWi1PyqOlqeBX4D0cJtpUHXX9w8m/ApOptca6LEl9m6eSimO9StjvOY/lk0f6xPCnxyIVf9meZA1UpCD3FdgjTM26lRvjXkOZfrsAYmght4M4sBOLIWFxKlU/+gJmFgF1isGXuIczuuGGIDeQp5xbDU627TDNoPdbCyLeBHuEwwdJJQPBfZHGkf83KMFfXgyLIfg+Esk1WD2fxjBfaEOb/jQLw3wBeXK8oPKDp31ZW2IWHvzOYZbcIOFCVgghFmQu0iGdSscOp+HCd+t+KA96f+/Xrp63pnnAgVAHlYvD903TIpswb/QLwoCAXu/kRSv8PaPAv5JThriTTpsTPLB9C94DyaP2ENP5UM+HeOAPajrO0cikjbjGPHulo6POJB37cbBW4PuHAWU0P18nF1fwzZswImFqWB159AK/j/izV8pXdAnAgFQUxtCT4VHov8mhVfQTy4s/mWCXLRfB+a3lCQqPFtFMtLhWX9He5sDFlznXzgcuv3HgpIGHtCbN7gzeA8xGWh7jHLD1C/m1s8HynhUHXKsyxuNON5HOngJdSThd6IM3c+S+4hscSkesdwgUeXUOZvsJRbvl/0K2Sisml4pOupCfCNSZY+yEU1yBoY0zA5NISfmd/Pokg/E8o7qFEMMPhsUW2z5386zFrzD73kkuYES5TBtn0t1K+P1ZAiae706kaGwDPE0zi1srex7zSFLH3QMsXDPDVA/qPJgZIu9zSKB/vbN457ku8ttJbMm0/WUyvtbsPyPGA5+6k9WHKjHWiy95i4UAqCqILDpLhqytF1RqbZCAYS2yWCwE+gG+/p8E/h9a3bTJ4FkfrfDIw/bUQKWIYl5hN75tTKOwXMlUMFwTvSYMnF+HXNhXfzIL+x0k729Lzfa+bXn5g+lO7li5Vl97r+jA8faMgrVEQdx3niah05bACc6AqBaGCj0TgBuPunQxVtYAmNGo5TUHIoxmigRYnF4F7G8WJ/AcmK4MFx4OBE9joR28UOYBVr2HwEboNvOvtG/p+YTDNBZ9/WDxptc5jPcomlPdoSycXk645aX609DFXCp/s9ZNDwwx4EXdZdYSCBBTZxaFU/SnNwpQqccJb9jxb2F9l+3dPTu69j7CcHTY/lI5WvSkS4W/LkPCQdrXieLRqfC1h/tZ1js8ex9Zt7a86cnOSLb/qEVDG07iSdADb6v2dtAjpeHCJbGURNk8BBYWt5HV5vTUICSdxQ7ZA3+xDmT+wMNL6EwrgOJ33c4uTCw6nk6aZhak3QcH+K/xC7/4WhlI+Z+nJ37JJkyZpjbssb9ktAg4k6kEQFE2LfoQbeBD2z2nJ70YgzsI9lRmfGwjz5/GVGKF8Kkwaaa8KAOIDodfFzh8Tdx95buN9gm/dahJIiqAvwdzqHoZp37pphDT+Ap5lBt5LlsZ1wb0ZZcRdho3ivOim0xQ3tI03eOTRGkVrN02UnzWUg0jrmpVjBctNC25hE6vG9SAuvSXrT6QgIoh1Bm291oVtKT8Ixgx6hjZhhmjAjVBcR7w/pkgFKEI7k4H1MWF44W/SvRKGhyLYomQ4eR3S9w+nJ+x8N6F6MjcN8U+58SG/rXh7vRdp/XEQNGv6O2DqlRUkxL1i/aTi/P1KEgaZCTFcvXUM5uwbKw0V7i+kuULj+sk7mt7Azi5EkkyPczzpRrn5ovwIlc6mvI1ytI8EFAoEx9+4cMB3mdZEQsn8T5TnbDc9DcFaFNufPJjK4SnwDPxNHL1HTx9AvMcUxDO9UYBfuvChZZRgWjbiK6HR3wUAvi9YXE1x43Z6liJhmBAfYyfb4pTOVcx36WjVqtUCvn+LojzMuOAMxg+9SF+JXb690iEE2gT4JcL03pgxY57ntpGlbv6wn1vY32af0Yf77LPPCYxrjuU0aWdgeZsMgQWojVMQliEo2j/524Dh4fyJvlGQd8DL36cFPhMqKytnJ0qPMrzH7Yt/IY83Q0eZUxkXTbX04KZV+iHw51OF4S4cOHDgMItP4m5UHHwcAF33At/b2YsCbiBYN614x3fBbynxD1r5RH+mfOWndDgQt9nOUNcWC1r2RnrdNQaLT8fF9KpnsErk1kLrKbweNkRrVFgoSfmzVDlQUcx/o1BETJUJpVcNSFkhklRMTWSOaFLlb+KVqSAhkKvv2vaIF0a3N66IfVu41b9nUtU25qRCrzEolbSlksaUw2irjZVvtIsXRr/rV10qjcXpu/zUEg5IEExAagnJPpmiW/S7PPDGYc4/uyqu/NRwDniCEKv0Gk5qWuSZcpirzOYXz2prw5EWE93EpcYw4Sub2q1s+TX9WH42c0AKYeMOl19lc6oaEiJmFvMj/FTJesxvU7dW+SYMm1OVf8OKIP6Ew8pcSpEDJmwpJs9bMgm/tYSuKwRMIcJu3pArgYLEMz3Go81f5d+0OVCMCmKVK2JUwdaD6Nv1lytfHIl+yryJ5kvaoa7ApZ05RxlcBVERMg+kyHLLFQ8Tyk/+OFCMPYgpgbnFqMT5qqHaTHu+eFyS5agXsbckCcgC0lKOYmzAskBa6YAo9gqwXqR0OJo5puFeQw1FbeRD5pwsQ6iRHLD/kTdFUSNm/hpJcJmoMgfS4YCUoW5sV7L8eou9pwfF8lPmQP44ILNK/9dX7j3yx/NySSXEASlI+SkCDpS77sJVgmdKUbzc8iC8cPWQtORyS5WUPTmJlELoMcUwRdkcuvm3rDAuNwrot8oqIAo1vmg1QrZZMFGDJIUoK0URikLZxMpdpZgyqBGy3kJhYUUIf+cOozLktDlglZh2xnKGpBwwRZBibGQ2yuupmbr1XMJMYcrKkZSNhY/8/wRmH+xgm54IAAAAAElFTkSuQmCC" alt="ISOWAY" style="height:28px;vertical-align:middle;margin-right:8px;"> 干散货全球港口拥堵日报</div>
    <div class="h-sub">铁矿石 · 煤炭 · 铝矾土 · 装卸港实时锚泊 · Demurrage 风险预警</div>
  </div>
  <div class="h-right">
    <span class="brand-tag">{{ brand }}</span>
    <div class="h-badges">
      {% for b in stat_badges %}<span class="hb {{ b.cls }}">{{ b.text }}</span>{% endfor %}
    </div>
  </div>
</div>

<div class="summary-bar">
  <div class="sb"><div class="sb-val red">{{ total_anchored }}</div><div class="sb-label">总锚泊等待（艘）</div></div>
  <div class="sb"><div class="sb-val blue">{{ total_moored }}</div><div class="sb-label">总在港靠泊（艘）</div></div>
  <div class="sb"><div class="sb-val gray">{{ total_estimate }}</div><div class="sb-label">预计到港（艘）</div></div>
  <div class="sb"><div class="sb-val amber">{{ high_ports + mod_ports }}</div><div class="sb-label">拥堵预警港口（个）</div></div>
  <div class="sb"><div class="sb-val green">{{ normal_ports }}</div><div class="sb-label">正常/宽松港口（个）</div></div>
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

{% set groups_order = ["装港·澳大利亚","装港·巴西","装港·南非","装港·印尼","装港·西非","卸港·中国","卸港·印度"] %}
{% for group_name in groups_order %}
  {% set group_ports = ports | selectattr("group","equalto",group_name) | list %}
  {% if group_ports %}
  <div class="group-header">
    {{ group_name }}
    <span class="group-count">{{ group_ports|length }} 个港口</span>
  </div>
  <div class="port-grid">
  {% for p in group_ports %}
  {% set cong = p.congestion %}
  {% set cong_labels = {'high':'🔴 高拥堵','mod':'🟡 中等拥堵','normal':'🔵 正常','low':'🟢 宽松'} %}
  {% set val_cls = 'v-high' if cong=='high' else ('v-mod' if cong=='mod' else ('v-ok' if cong=='low' else 'v-blue')) %}
  <div class="pcard cong-{{ cong }}">
    <div class="pc-head">
      <div>
        <div class="pc-name">{{ p.flag }} {{ p.name_cn }}</div>
        <div class="pc-sub">{{ p.name }} · {{ p.cargo }} · {{ p.note }}</div>
      </div>
      <span class="pc-badge pb-{{ cong }}">{{ cong_labels[cong] }}</span>
    </div>
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
        <div class="ps-val" style="font-size:14px;">{{ p.avg7 if p.avg7 is not none else '—' }}</div>
        <div class="ps-sub">
          {% if p.avg7 is not none and p.avg7 > 0 %}
            {% set pct = ((p.n_anchored - p.avg7) / p.avg7 * 100)|int %}
            {% if pct > 15 %}<span style="color:#854f0b">↑{{ pct }}%</span>
            {% elif pct < -15 %}<span style="color:#085041">↓{{ -pct }}%</span>
            {% else %}持平{% endif %}
          {% else %}首次{% endif %}
        </div>
      </div>
    </div>

    {% if p.history_7d %}
    <div class="trend-wrap">
      <div class="trend-label">近7日锚泊趋势</div>
      <div class="trend-bars">
        {% set max_v = namespace(v=1) %}
        {% for h in p.history_7d %}{% if h.anchored > max_v.v %}{% set max_v.v = h.anchored %}{% endif %}{% endfor %}
        {% for h in p.history_7d %}
        {% set bar_h = [(h.anchored / max_v.v * 22)|int, 2]|max %}
        {% set bar_c = '#a32d2d' if p.avg7 and h.anchored >= p.avg7*1.4 else ('#ef9f27' if p.avg7 and h.anchored >= p.avg7*1.15 else '#378ADD') %}
        <div class="tb-day">
          <div class="tb-bar" style="height:{{ bar_h }}px;background:{{ bar_c }};"></div>
          <div class="tb-dt">{{ h.date[5:] }}</div>
        </div>
        {% endfor %}
      </div>
    </div>
    {% endif %}

    <div class="pc-detail">
      {% if not p.data_fresh %}
      <div class="no-data">暂未获取到该港口实时数据</div>
      {% else %}
      <div class="pd-row">
        <span class="pd-icon">⏱️</span><span class="pd-label">估计等待</span>
        <span class="pd-val">{{ p.est_wait_days }} 天</span>
        <span class="pd-note">按靠泊轮转估算</span>
      </div>
      <div class="pd-row">
        <span class="pd-icon">📈</span><span class="pd-label">关联指数</span>
        <span class="pd-val">{{ p.bdi }}</span>
        <span class="pd-note">BDI 分项</span>
      </div>
      {% if p.n_moor_half > 0 %}
      <div class="pd-row">
        <span class="pd-icon">⛵</span><span class="pd-label">途中锚泊</span>
        <span class="pd-val">{{ p.n_moor_half }} 艘</span>
        <span class="pd-note">（已含在合计中）</span>
      </div>
      {% endif %}
      {% endif %}
    </div>
  </div>
  {% endfor %}
  </div>
  {% endif %}
{% endfor %}

<!-- 汇总表 -->
<div class="sum-card">
  <div class="sum-head">
    <span class="sum-title">全港拥堵汇总对比 · {{ date }}</span>
    <span class="sum-badge">23个核心干散货港口</span>
  </div>
  <table>
    <thead><tr>
      <th>港口</th><th>分组</th><th>货种</th>
      <th>⚓锚泊</th><th>🚢靠泊</th><th>📍预抵</th>
      <th>7日均</th><th>偏差</th><th>估等</th><th>状态</th>
    </tr></thead>
    <tbody>
    {% for p in ports %}
    {% set cong = p.congestion %}
    {% set td_cls = 'td-high' if cong=='high' else ('td-mod' if cong=='mod' else ('td-gray' if not p.data_fresh else '')) %}
    {% set cong_labels = {'high':'高拥堵','mod':'中等拥堵','normal':'正常','low':'宽松'} %}
    <tr {% if cong=='high' %}class="high-row"{% endif %}>
      <td>{{ p.flag }} {{ p.name_cn }}</td>
      <td style="color:#6b7280;white-space:nowrap;">{{ p.group.split('·')[1] if '·' in p.group else p.group }}</td>
      <td style="color:#6b7280;">{{ p.cargo }}</td>
      <td class="{{ td_cls }}"><strong>{{ p.n_anchored }}</strong></td>
      <td>{{ p.n_moored }}</td>
      <td class="td-gray">{{ p.n_estimate }}</td>
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
    <div><span class="ft-brand">{{ brand }}</span> 港口拥堵监测 · 23个核心干散货港口 · 仅供参考，以官方数据为准</div>
    <div class="ft-note">等待时间为估算值，仅供参考。实际 Demurrage 以 NOR 递交时间和 Laycan 为准。</div>
  </div>
  <span>{{ generated_at }} UTC{% if demo_mode %} · ⚠️演示模式{% endif %}</span>
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
    stat_badges.append({"cls":"hb-g","text":f"✅ 正常 {normal_ports} 港"})

    dem_parts = []
    for p in port_results:
        if p["congestion"] in ("high","mod") and p["data_fresh"]:
            dev = ""
            if p["avg7"] and p["avg7"] > 0:
                pct = int((p["n_anchored"] - p["avg7"]) / p["avg7"] * 100)
                dev = f"（+{pct}%均值）"
            dem_parts.append(f"**{p['name_cn']}** {p['n_anchored']}艘{dev}，估等 {p['est_wait_days']} 天")
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
           "".join(f"<p>{p['flag']}{p['name_cn']}: 锚泊{p['n_anchored']}</p>" for p in port_results) + \
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
                f"> {cong_emoji[p['congestion']]} **{p['flag']}{p['name_cn']}**（{p['cargo']}）　"
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
            f"| {p['flag']}{p['name_cn']} | **{p['n_anchored']}** | "
            f"{p['n_moored']} | {p['n_estimate']} | "
            f"{cong_emoji[p['congestion']]} {cong_label[p['congestion']]} |"
        )

    lines += [
        "", "## 💡 操作建议",
    ]
    if high:
        hnames = "、".join(p["name_cn"] for p in high)
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
        log.error(f"文字推送失败: {res}")
        return img_ok
    except Exception as e: log.error(f"推送异常: {e}"); return img_ok


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
