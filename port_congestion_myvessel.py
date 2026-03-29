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
    <div class="h-date">{{ date }} UTC · 数据：船视宝 SDC API · 23个核心干散货港口实时动态</div>
    <div class="h-title">⚓ 干散货全球港口拥堵日报</div>
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
    <span class="sum-badge">数据：船视宝 SDC API · 23个核心港口</span>
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
    <div><span class="ft-brand">{{ brand }}</span> 港口拥堵监测 v2.0 · 数据：船视宝 SDC API · 23个核心干散货港口</div>
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

    # 按分组展示
    groups_order = ["装港·澳大利亚","装港·巴西","装港·南非","装港·印尼","装港·西非","卸港·中国","卸港·印度"]
    lines.append("## 📊 各港口实时概况")
    for grp in groups_order:
        grp_ports = [p for p in port_results if p["group"] == grp]
        if not grp_ports: continue
        lines.append(f"\n**{grp}**")
        lines.append("| 港口 | ⚓锚泊 | 🚢靠泊 | 📍预抵 | 状态 |")
        lines.append("|:---|---:|---:|---:|:---:|")
        for p in grp_ports:
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

    lines += ["", f"<font color=\"comment\">数据：船视宝 SDC API · 仅供参考 · {Config.BRAND}</font>"]
    return {"msgtype":"markdown","markdown":{"content":"\n".join(lines)}}


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
