"""
ISOWAY 全球热带气旋预警系统
============================
功能：
  1. 从 NOAA IBTrACS NRT 拉取全球所有活跃热带气旋（覆盖西太/印度洋/大西洋/南太等所有海盆）
  2. 从 NOAA NHC RSS/JSON 拉取大西洋/东太平洋系统（含 72h 预报讨论文本）
  3. 自动评估各系统对主要航运航线的影响距离
  4. 生成 HTML 预警报告（长截图用）+ 企业微信推送（截图 + markdown 文字）
  5. 无系统时推送"当前无活跃热带气旋"简报，有系统时立即推送完整预警

数据来源（全部免费，无需 API Key）：
  - IBTrACS NRT: https://www.ncei.noaa.gov/data/.../ibtracs.ACTIVE.list.v04r01.csv
  - NOAA NHC: https://www.nhc.noaa.gov/CurrentStorms.json
  - NOAA NHC RSS: https://www.nhc.noaa.gov/index-{at|ep|cp}.xml

GitHub Actions 部署：
  - 事件触发：每 6 小时轮询一次（cron: '0 */6 * * *'）
  - 有新系统生成或强度显著变化时自动推送
  - 只需 Secret: WECOM_WEBHOOK

运行：
  python cyclone_push.py              # 立即执行（强制推送）
  python cyclone_push.py --check-only # 只检查，不推送
  python cyclone_push.py --force      # 强制推送（即使与上次相同）
"""

import os
import sys
import csv
import io
import re
import json
import math
import base64
import hashlib
import logging
import datetime
import argparse
import time as _time
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

try:
    from jinja2 import Template
    HAS_JINJA2 = True
except ImportError:
    HAS_JINJA2 = False
    print("⚠️  jinja2 未安装，运行: pip install jinja2")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import xml.etree.ElementTree as ET
    HAS_ET = True
except ImportError:
    HAS_ET = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cyclone_alert")


# ══════════════════════════════════════════════════════════════════════════════
# 1. 配置
# ══════════════════════════════════════════════════════════════════════════════

class Config:
    BRAND          = "ISOWAY"
    WECOM_WEBHOOK  = os.getenv("WECOM_WEBHOOK", "")
    OUTPUT_DIR     = Path(os.getenv("OUTPUT_DIR", "./reports"))
    STATE_FILE     = Path(os.getenv("STATE_FILE", "./reports/.cyclone_state.json"))
    TIMEOUT        = 20

    # IBTrACS NRT CSV — 全球实时最优路径（每 3-6 小时更新）
    IBTRACS_URL = (
        "https://www.ncei.noaa.gov/data/"
        "international-best-track-archive-for-climate-stewardship-ibtracs/"
        "v04r01/access/csv/ibtracs.ACTIVE.list.v04r01.csv"
    )

    # NOAA NHC — 大西洋/东太/中太（有活跃系统时含预报文本）
    NHC_JSON  = "https://www.nhc.noaa.gov/CurrentStorms.json"
    NHC_RSS   = {
        "AT": "https://www.nhc.noaa.gov/index-at.xml",
        "EP": "https://www.nhc.noaa.gov/index-ep.xml",
        "CP": "https://www.nhc.noaa.gov/index-cp.xml",
    }

    # 主要干散货航运航线关键点（用于计算气旋影响距离）
    SHIPPING_ROUTES = [
        ("南海中部",       15.0,  115.0),
        ("新加坡/马六甲",   1.5,  104.0),
        ("西太平洋",       30.0,  155.0),
        ("澳大利亚西北",  -20.0,  113.0),
        ("菲律宾海",       15.0,  130.0),
        ("东日本海域",     37.0,  141.0),
        ("孟加拉湾",       15.0,   88.0),
        ("阿拉伯海",       15.0,   65.0),
        ("好望角",        -37.0,   20.0),
        ("莫桑比克海峡",  -20.0,   37.0),
        ("北大西洋西部",   30.0,  -70.0),
        ("加勒比海",       15.0,  -70.0),
        ("墨西哥湾",       24.0,  -90.0),
    ]

    # 强度阈值（参照 Saffir-Simpson 和 WMO 标准，单位：节）
    INTENSITY = {
        "investigate": (0,  25,  "热带扰动",   "INV", "#8a9db5"),
        "td":          (25, 34,  "热带低压",   "TD",  "#378ADD"),
        "ts":          (34, 48,  "热带风暴",   "TS",  "#1D9E75"),
        "sev_ts":      (48, 64,  "强热带风暴", "STS", "#ef9f27"),
        "typhoon":     (64, 96,  "台风",       "TY",  "#e24b4a"),
        "sev_ty":      (96, 114, "强台风",     "STY", "#c0392b"),
        "sup_ty":      (114, 999,"超强台风",   "SuperTY", "#7b241c"),
    }

    # 预警距离阈值（海里）
    WARN_DIST_NM  = 500   # 距航线 500nm 内发预警
    WATCH_DIST_NM = 800   # 距航线 800nm 内发关注
    CLOSE_DIST_NM = 300   # 距航线 300nm 内为危险

    # 海盆中文名
    BASIN_NAMES = {
        "NA": "北大西洋",
        "EP": "东北太平洋",
        "WP": "西北太平洋",
        "NI": "北印度洋",
        "SI": "南印度洋",
        "SP": "南太平洋",
        "SA": "南大西洋",
        "BB": "孟加拉湾",
        "AS": "阿拉伯海",
        "EA": "东澳大利亚",
        "MM": "米克罗尼西亚",
    }

    # 海盆对应洋区（用于告警关联判断）
    BASIN_OCEAN = {
        "NA": "大西洋",
        "EP": "太平洋",
        "WP": "太平洋",
        "NI": "印度洋",
        "SI": "印度洋",
        "SP": "太平洋",
        "BB": "印度洋",
        "AS": "印度洋",
        "EA": "太平洋",
        "MM": "太平洋",
    }


# ══════════════════════════════════════════════════════════════════════════════
# 2. 工具函数
# ══════════════════════════════════════════════════════════════════════════════

def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """计算两点间大圆距离（海里）。"""
    R = 3440.065  # 地球半径（海里）
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    Δφ = math.radians(lat2 - lat1)
    Δλ = math.radians(lon2 - lon1)
    a = math.sin(Δφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(Δλ/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def wind_to_intensity(wind_kn: Optional[float]) -> dict:
    """风速（节）→ 强度等级字典。"""
    if wind_kn is None:
        return {"key": "investigate", "name": "热带扰动", "abbr": "INV", "color": "#8a9db5"}
    for key, (lo, hi, name, abbr, color) in Config.INTENSITY.items():
        if lo <= wind_kn < hi:
            return {"key": key, "name": name, "abbr": abbr, "color": color}
    return {"key": "sup_ty", "name": "超强台风", "abbr": "SuperTY", "color": "#7b241c"}


def sshs_to_text(sshs: Optional[str]) -> str:
    """Saffir-Simpson 等级 → 中文描述。"""
    if sshs is None:
        return ""
    m = {"-5": "热带扰动", "-4": "热带低压", "-3": "亚热带低压",
         "-2": "热带风暴", "-1": "亚热带风暴", "0": "热带风暴",
         "1": "一级飓风/台风", "2": "二级飓风/台风", "3": "三级飓风/台风（主要）",
         "4": "四级飓风/台风（主要）", "5": "五级飓风/台风（毁灭性）"}
    return m.get(str(sshs).strip(), f"SSHS {sshs}")


def deg_to_compass(deg: Optional[float]) -> str:
    if deg is None:
        return "—"
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return dirs[round(float(deg) / 22.5) % 16]


def movement_direction(positions: list) -> Optional[str]:
    """从最后两个最优路径点推算移动方向。"""
    if len(positions) < 2:
        return None
    try:
        p1, p2 = positions[-2], positions[-1]
        dlat = float(p2["lat"]) - float(p1["lat"])
        dlon = float(p2["lon"]) - float(p1["lon"])
        bearing = math.degrees(math.atan2(dlon, dlat)) % 360
        return deg_to_compass(bearing)
    except Exception:
        return None


def movement_speed_kn(positions: list) -> Optional[float]:
    """从最后两个时间步估算移动速度（节）。"""
    if len(positions) < 2:
        return None
    try:
        p1, p2 = positions[-2], positions[-1]
        dist = haversine_nm(float(p1["lat"]), float(p1["lon"]),
                            float(p2["lat"]), float(p2["lon"]))
        # 假设每步 6 小时（IBTrACS 标准间隔）
        return round(dist / 6, 1)
    except Exception:
        return None


def safe_float(v) -> Optional[float]:
    try:
        f = float(v)
        return f if not math.isnan(f) else None
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 3. 数据获取
# ══════════════════════════════════════════════════════════════════════════════

def fetch_ibtracs() -> list[dict]:
    """
    从 NOAA IBTrACS NRT 拉取所有活跃热带气旋，返回经过聚合的风暴列表。
    覆盖：西太平洋、印度洋、南太平洋、大西洋、东太平洋等全部海盆。
    """
    log.info("拉取 IBTrACS NRT 全球活跃气旋数据...")
    try:
        r = requests.get(Config.IBTRACS_URL, timeout=Config.TIMEOUT,
                         headers={"User-Agent": "Mozilla/5.0 ISOWAY-CycloneBot/1.0"})
        r.raise_for_status()
    except Exception as e:
        log.error(f"IBTrACS 拉取失败: {e}")
        return []

    reader = csv.reader(io.StringIO(r.text))
    rows   = list(reader)
    if len(rows) < 3:
        return []

    headers = [h.strip() for h in rows[0]]

    def col(row, name):
        try:
            return row[headers.index(name)].strip()
        except (ValueError, IndexError):
            return ""

    storms: dict[str, dict] = {}
    for row in rows[2:]:   # 跳过表头行和单位行
        sid = col(row, "SID")
        if not sid:
            continue

        if sid not in storms:
            storms[sid] = {
                "sid":      sid,
                "season":   col(row, "SEASON"),
                "name":     col(row, "NAME"),
                "basin":    col(row, "BASIN"),
                "subbasin": col(row, "SUBBASIN"),
                "positions": [],
            }

        # R34 风圈半径（四象限，海里）
        r34_vals = []
        for q in ("USA_R34_NE", "USA_R34_SE", "USA_R34_SW", "USA_R34_NW"):
            v = safe_float(col(row, q))
            if v and v > 0:
                r34_vals.append(v)

        storms[sid]["positions"].append({
            "time":       col(row, "ISO_TIME"),
            "lat":        col(row, "LAT"),
            "lon":        col(row, "LON"),
            "wmo_wind":   col(row, "WMO_WIND"),
            "wmo_pres":   col(row, "WMO_PRES"),
            "nature":     col(row, "NATURE"),
            "usa_wind":   col(row, "USA_WIND"),
            "usa_pres":   col(row, "USA_PRES"),
            "usa_status": col(row, "USA_STATUS"),
            "usa_sshs":   col(row, "USA_SSHS"),
            "dist2land":  col(row, "DIST2LAND"),
            "track_type": col(row, "TRACK_TYPE"),
            "r34_avg":    round(sum(r34_vals) / len(r34_vals)) if r34_vals else None,
        })

    # 汇总每个风暴的关键信息
    result = []
    for s in storms.values():
        if not s["positions"]:
            continue

        latest = s["positions"][-1]
        # 用 USA_WIND > WMO_WIND 优先
        wind_str = latest["usa_wind"] or latest["wmo_wind"]
        pres_str = latest["usa_pres"] or latest["wmo_pres"]
        wind_kn  = safe_float(wind_str)
        pres_mb  = safe_float(pres_str)
        lat      = safe_float(latest["lat"])
        lon      = safe_float(latest["lon"])
        dist2land= safe_float(latest["dist2land"])

        if lat is None or lon is None:
            continue

        intensity = wind_to_intensity(wind_kn)

        # 计算距主要航线最近距离
        route_dists = []
        for rname, rlat, rlon in Config.SHIPPING_ROUTES:
            dist = haversine_nm(lat, lon, rlat, rlon)
            route_dists.append((dist, rname))
        route_dists.sort(key=lambda x: x[0])
        nearest_route_dist = route_dists[0][0]
        nearest_route_name = route_dists[0][1]

        # 评估影响级别
        if nearest_route_dist <= Config.CLOSE_DIST_NM and (wind_kn or 0) >= 34:
            impact = "danger"
        elif nearest_route_dist <= Config.WARN_DIST_NM and (wind_kn or 0) >= 25:
            impact = "warn"
        elif nearest_route_dist <= Config.WATCH_DIST_NM and (wind_kn or 0) >= 25:
            impact = "watch"
        else:
            impact = "monitor"

        # 移动方向/速度
        mov_dir   = movement_direction(s["positions"])
        mov_speed = movement_speed_kn(s["positions"])

        result.append({
            "sid":          s["sid"],
            "name":         s["name"] if s["name"] not in ("UNNAMED", "NOT_NAMED", "") else "未命名扰动",
            "basin":        s["basin"],
            "subbasin":     s["subbasin"],
            "basin_name":   Config.BASIN_NAMES.get(s["basin"], s["basin"]) +
                            (f"/{Config.BASIN_NAMES.get(s['subbasin'], '')}"
                             if s["subbasin"] and s["subbasin"] in Config.BASIN_NAMES else ""),
            "lat":          lat,
            "lon":          lon,
            "wind_kn":      wind_kn,
            "pres_mb":      pres_mb,
            "sshs":         latest["usa_sshs"],
            "sshs_text":    sshs_to_text(latest["usa_sshs"]),
            "dist2land_nm": dist2land,
            "r34_avg_nm":   latest["r34_avg"],
            "intensity":    intensity,
            "last_time":    latest["time"],
            "mov_dir":      mov_dir,
            "mov_speed":    mov_speed,
            "nearest_route_dist": round(nearest_route_dist),
            "nearest_route_name": nearest_route_name,
            "nearby_routes": [(round(d), n) for d, n in route_dists[:4]],
            "impact":       impact,
            "positions":    s["positions"],  # 完整历史，用于生成路径图
        })

    # 按影响级别+风速排序
    impact_order = {"danger": 0, "warn": 1, "watch": 2, "monitor": 3}
    result.sort(key=lambda x: (impact_order[x["impact"]], -(x["wind_kn"] or 0)))

    log.info(f"✅ IBTrACS: 发现 {len(result)} 个活跃系统")
    for s in result:
        log.info(f"  [{s['impact'].upper()}] {s['name']} ({s['basin_name']}) "
                 f"风速 {s['wind_kn']}kn 气压 {s['pres_mb']}mb "
                 f"距 {s['nearest_route_name']} {s['nearest_route_dist']}nm")
    return result


def fetch_nhc_details() -> dict:
    """
    从 NHC JSON + RSS 拉取大西洋/东太平洋系统的补充信息。
    返回字典 {storm_id: {advisory_text, forecast_url, ...}}
    """
    details = {}
    try:
        r = requests.get(Config.NHC_JSON, timeout=Config.TIMEOUT)
        nhc_data = r.json()
        active = nhc_data.get("activeStorms", [])
        for s in active:
            sid = s.get("id", "")
            details[sid] = {
                "nhc_name":   s.get("name", ""),
                "nhc_basin":  s.get("basin", ""),
                "nhc_url":    s.get("stormInfo", {}).get("stormLink", ""),
                "nhc_active": True,
            }
        if active:
            log.info(f"NHC 活跃系统: {len(active)} 个")
    except Exception as e:
        log.warning(f"NHC JSON 拉取失败: {e}")

    # 解析 NHC RSS 取预报摘要文本
    for basin, rss_url in Config.NHC_RSS.items():
        try:
            r = requests.get(rss_url, timeout=Config.TIMEOUT)
            root = ET.fromstring(r.text)
            ns = {"nhc": "https://www.nhc.noaa.gov"}
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                desc  = (item.findtext("description") or "").strip()
                link  = (item.findtext("link") or "").strip()
                # 只处理含有实际系统信息的 item
                if any(kw in title.lower() for kw in
                       ["advisory", "tropical", "hurricane", "typhoon", "cyclone"]):
                    # 提取 ATCF ID
                    m = re.search(r'\b([A-Z]{2}\d{6})\b', link)
                    key = m.group(1) if m else f"{basin}_{title[:30]}"
                    details[key] = {
                        "nhc_basin":       basin,
                        "nhc_title":       title,
                        "nhc_desc":        re.sub(r"<[^>]+>", "", desc)[:300],
                        "nhc_link":        link,
                    }
        except Exception as e:
            log.debug(f"NHC RSS {basin} 解析失败: {e}")

    return details


# ══════════════════════════════════════════════════════════════════════════════
# 4. 状态管理（防止重复推送）
# ══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    try:
        if Config.STATE_FILE.exists():
            return json.loads(Config.STATE_FILE.read_text())
    except Exception:
        pass
    return {"last_storms": {}, "last_push": None}


def save_state(storms: list) -> None:
    Config.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "last_storms": {
            s["sid"]: {"wind_kn": s["wind_kn"], "impact": s["impact"], "lat": s["lat"], "lon": s["lon"]}
            for s in storms
        },
        "last_push": datetime.datetime.utcnow().isoformat(),
    }
    Config.STATE_FILE.write_text(json.dumps(state, indent=2))


def should_push(storms: list, force: bool = False) -> bool:
    """判断是否需要推送（新系统出现/强度显著变化/有危险/强制）。"""
    if force:
        return True

    state = load_state()
    last  = state.get("last_storms", {})

    # 有危险级系统，始终推送
    if any(s["impact"] in ("danger", "warn") for s in storms):
        return True

    # 有新系统出现
    current_sids = {s["sid"] for s in storms}
    last_sids    = set(last.keys())
    if current_sids - last_sids:
        log.info(f"新系统出现: {current_sids - last_sids}")
        return True

    # 有系统强度显著变化（±15kn 以上）
    for s in storms:
        prev = last.get(s["sid"])
        if prev and s["wind_kn"] and prev.get("wind_kn"):
            if abs(s["wind_kn"] - prev["wind_kn"]) >= 15:
                log.info(f"{s['name']} 强度变化 {prev['wind_kn']} → {s['wind_kn']}kn")
                return True

    # 上次推送超过 24 小时，日常推送一次
    last_push = state.get("last_push")
    if last_push:
        elapsed = (datetime.datetime.utcnow() -
                   datetime.datetime.fromisoformat(last_push)).total_seconds()
        if elapsed > 86400:
            return True

    log.info("无显著变化，跳过推送")
    return False


# ══════════════════════════════════════════════════════════════════════════════
# 5. HTML 报告生成
# ══════════════════════════════════════════════════════════════════════════════

CYCLONE_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>全球热带气旋预警 — {{ date }} | {{ brand }}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
     font-size:13px;color:#1a1a18;background:#f0f2f5;padding:20px;}
.wrap{max-width:1060px;margin:0 auto;}

/* ── header ── */
.header{background:linear-gradient(135deg,#1a0a2e 0%,#2d1b69 60%,#0d2137 100%);
        border-radius:14px;padding:20px 24px;margin-bottom:14px;
        display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px;}
.h-left .h-date{font-size:11px;color:rgba(255,255,255,.45);margin-bottom:5px;}
.h-left .h-title{font-size:22px;font-weight:700;color:#fff;letter-spacing:.02em;}
.h-left .h-sub{font-size:12px;color:rgba(255,255,255,.55);margin-top:4px;}
.h-right{display:flex;flex-direction:column;align-items:flex-end;gap:8px;}
.brand-tag{font-size:13px;font-weight:700;color:#fff;padding:4px 14px;
           border-radius:6px;border:1px solid rgba(255,255,255,.25);
           background:rgba(255,255,255,.1);}
.h-stats{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;}
.hst{font-size:10px;font-weight:600;padding:3px 9px;border-radius:4px;border:1px solid;}
.hst-r{background:rgba(226,75,74,.25);color:#f09595;border-color:rgba(226,75,74,.35);}
.hst-o{background:rgba(239,159,39,.25);color:#f2c666;border-color:rgba(239,159,39,.35);}
.hst-g{background:rgba(29,158,117,.2);color:#5dcaa5;border-color:rgba(29,158,117,.3);}
.hst-b{background:rgba(55,138,221,.2);color:#85b7eb;border-color:rgba(55,138,221,.3);}

/* ── no storm ── */
.no-storm{background:#fff;border-radius:12px;padding:32px;text-align:center;
          border:1px solid #e0e6f0;margin-bottom:14px;}
.no-storm-icon{font-size:48px;margin-bottom:12px;}
.no-storm-title{font-size:18px;font-weight:600;color:#1a1a18;margin-bottom:8px;}
.no-storm-text{font-size:13px;color:#6b7280;line-height:1.7;max-width:500px;margin:0 auto;}

/* ── alert bar ── */
.alert-bars{display:flex;flex-direction:column;gap:8px;margin-bottom:14px;}
.abar{border-radius:10px;padding:11px 16px;display:flex;align-items:center;gap:12px;}
.abar-icon{font-size:20px;flex-shrink:0;}
.abar-title{font-size:13px;font-weight:700;margin-bottom:3px;}
.abar-text{font-size:11px;line-height:1.5;}
.abar-danger{background:#2d0a0a;border:1px solid #a32d2d;}
.abar-danger .abar-title{color:#f09595;} .abar-danger .abar-text{color:#d08080;}
.abar-warn{background:#2a1900;border:1px solid #854f0b;}
.abar-warn .abar-title{color:#f2c666;} .abar-warn .abar-text{color:#c9a050;}
.abar-watch{background:#0a1a2a;border:1px solid #185fa5;}
.abar-watch .abar-title{color:#85b7eb;} .abar-watch .abar-text{color:#6a9abf;}

/* ── storm cards ── */
.storms-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(460px,1fr));
             gap:14px;margin-bottom:14px;}
.scard{background:#fff;border-radius:14px;overflow:hidden;border:1px solid #e0e6f0;}
.scard.impact-danger{border-color:#a32d2d;border-width:2px;}
.scard.impact-warn{border-color:#854f0b;border-width:1.5px;}
.scard.impact-watch{border-color:#185fa5;}

/* card header */
.sc-head{padding:14px 18px;display:flex;justify-content:space-between;align-items:flex-start;
         border-bottom:1px solid #f0f2f8;}
.sc-head-left{display:flex;align-items:center;gap:10px;}
.sc-intensity-badge{width:44px;height:44px;border-radius:10px;display:flex;align-items:center;
                    justify-content:center;font-size:11px;font-weight:700;color:#fff;flex-shrink:0;}
.sc-name{font-size:16px;font-weight:700;line-height:1.2;}
.sc-sub{font-size:11px;color:#8a9db5;margin-top:2px;}
.sc-impact-tag{font-size:11px;font-weight:700;padding:3px 10px;border-radius:8px;}
.sit-danger{background:#feecec;color:#a32d2d;}
.sit-warn{background:#fff3d6;color:#854f0b;}
.sit-watch{background:#e8f2fb;color:#185fa5;}
.sit-monitor{background:#f0f2f8;color:#6b7280;}

/* main data grid */
.sc-data{display:grid;grid-template-columns:repeat(4,1fr);gap:0;padding:14px 0;border-bottom:1px solid #f0f2f8;}
.sd-cell{padding:6px 18px;border-right:1px solid #f0f2f8;}
.sd-cell:last-child{border-right:none;}
.sd-label{font-size:10px;color:#8a9db5;font-weight:500;text-transform:uppercase;margin-bottom:3px;}
.sd-val{font-size:20px;font-weight:700;line-height:1.1;}
.sd-unit{font-size:10px;color:#8a9db5;margin-left:2px;}
.sd-sub{font-size:10px;color:#aab8c8;margin-top:2px;}

/* color coding for intensity */
.wind-danger{color:#a32d2d;} .wind-warn{color:#854f0b;} .wind-ok{color:#0F6E56;} .wind-low{color:#185fa5;}

/* detail rows */
.sc-details{padding:10px 18px;display:grid;grid-template-columns:1fr 1fr;gap:6px;border-bottom:1px solid #f0f2f8;}
.detail-row{display:flex;align-items:center;gap:6px;font-size:11px;}
.dr-icon{width:16px;text-align:center;font-size:13px;flex-shrink:0;}
.dr-label{color:#8a9db5;width:80px;flex-shrink:0;}
.dr-val{color:#1a1a18;font-weight:500;}
.dr-note{color:#aab8c8;margin-left:3px;font-size:10px;}

/* shipping impact */
.sc-impact{padding:12px 18px;background:#fafbfd;}
.si-title{font-size:10px;font-weight:600;color:#8a9db5;text-transform:uppercase;
          letter-spacing:.05em;margin-bottom:8px;}
.si-routes{display:flex;flex-direction:column;gap:4px;}
.si-route{display:flex;align-items:center;gap:8px;font-size:11px;}
.si-dist-bar{flex:1;height:5px;background:#e8eef8;border-radius:3px;overflow:hidden;}
.si-dist-fill{height:100%;border-radius:3px;}
.si-route-name{width:120px;flex-shrink:0;color:#6b7280;}
.si-dist-txt{width:60px;text-align:right;font-weight:500;flex-shrink:0;}
.si-dist-danger{color:#a32d2d;} .si-dist-warn{color:#854f0b;} .si-dist-ok{color:#6b7280;}

/* track mini-map (SVG) */
.sc-track{padding:12px 18px;border-top:1px solid #f0f2f8;}
.track-title{font-size:10px;font-weight:600;color:#8a9db5;text-transform:uppercase;margin-bottom:6px;}
.track-svg{width:100%;height:80px;background:#f0f4fa;border-radius:8px;overflow:hidden;}

/* summary table */
.summary-card{background:#fff;border-radius:12px;overflow:hidden;margin-bottom:14px;border:1px solid #e0e6f0;}
.sc-hd{padding:10px 16px;background:#f5f7fc;border-bottom:1px solid #e8eef6;
       display:flex;align-items:center;justify-content:space-between;}
.sc-hd-title{font-size:12px;font-weight:600;}
.sc-hd-badge{font-size:10px;padding:2px 8px;border-radius:4px;background:#e8f2fb;color:#0c447c;}
table{width:100%;border-collapse:collapse;font-size:12px;}
thead th{font-size:10px;font-weight:600;color:#8a9db5;padding:8px 12px;
         text-align:right;border-bottom:1px solid #eef2f8;white-space:nowrap;}
thead th:first-child{text-align:left;}
tbody tr{border-bottom:1px solid #f5f7fc;}
tbody tr:last-child{border-bottom:none;}
tbody tr:hover{background:#fafbff;}
tbody tr.danger-row{background:#fff8f8;}
td{padding:7px 12px;text-align:right;vertical-align:middle;}
td:first-child{text-align:left;font-weight:600;}
.impact-pill{font-size:10px;font-weight:700;padding:2px 8px;border-radius:8px;display:inline-block;}
.ip-danger{background:#feecec;color:#a32d2d;} .ip-warn{background:#fff3d6;color:#854f0b;}
.ip-watch{background:#e8f2fb;color:#185fa5;} .ip-monitor{background:#f0f2f8;color:#6b7280;}

/* footer */
.footer{background:#fff;border-radius:10px;padding:10px 16px;font-size:11px;
        color:#8a9db5;display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;border:1px solid #e0e6f0;}
.ft-brand{font-weight:700;color:#2d1b69;font-size:12px;}

@media(max-width:700px){.storms-grid,.sc-data{grid-template-columns:1fr;}.sc-details{grid-template-columns:1fr;}}
</style>
</head>
<body>
<div class="wrap">

<!-- Header -->
<div class="header">
  <div class="h-left">
    <div class="h-date">{{ generated_at }} UTC · 数据来源：NOAA IBTrACS NRT + NHC RSS · 全球覆盖</div>
    <div class="h-title">🌀 全球热带气旋预警报告</div>
    <div class="h-sub">西太平洋 / 印度洋 / 南太平洋 / 大西洋 · 实时监测 · 航运影响评估</div>
  </div>
  <div class="h-right">
    <span class="brand-tag">{{ brand }}</span>
    <div class="h-stats">
      {% for cls, label in stat_badges %}
      <span class="hst {{ cls }}">{{ label }}</span>
      {% endfor %}
    </div>
  </div>
</div>

<!-- Alert bars (only for warn/danger) -->
{% if alert_storms %}
<div class="alert-bars">
  {% for s in alert_storms %}
  {% set cls = 'abar-danger' if s.impact == 'danger' else 'abar-warn' %}
  {% set icon = '🚨' if s.impact == 'danger' else '⚠️' %}
  <div class="abar {{ cls }}">
    <span class="abar-icon">{{ icon }}</span>
    <div>
      <div class="abar-title">
        {{ s.intensity.name }} {{ s.name }} ({{ s.basin_name }}) — 距 {{ s.nearest_route_name }} 约 {{ s.nearest_route_dist }} 海里
      </div>
      <div class="abar-text">
        当前位置 {{ '%.1f'|format(s.lat) }}°{{ 'N' if s.lat >= 0 else 'S' }} /
        {{ '%.1f'|format(s.lon|abs) }}°{{ 'E' if s.lon >= 0 else 'W' }} ·
        风速 {{ s.wind_kn|int if s.wind_kn else '—' }}kn · 气压 {{ s.pres_mb|int if s.pres_mb else '—' }}hPa ·
        移动方向 {{ s.mov_dir or '—' }} · 移动速度 {{ s.mov_speed or '—' }}kn。
        {% if s.impact == 'danger' %}
        ⚠ 该系统当前位于主要航线 {{ Config_CLOSE_DIST_NM }} 海里范围内，建议相关船舶立即关注并做好绕航准备。
        {% else %}
        建议密切关注该系统动向，适时调整航线计划。
        {% endif %}
      </div>
    </div>
  </div>
  {% endfor %}
</div>
{% endif %}

<!-- No storm placeholder -->
{% if not storms %}
<div class="no-storm">
  <div class="no-storm-icon">✅</div>
  <div class="no-storm-title">当前全球无活跃热带气旋</div>
  <div class="no-storm-text">
    全球各大洋当前未监测到活跃热带气旋系统（含热带低压及以上级别）。<br>
    ISOWAY 气象导航系统将持续每 6 小时轮询 NOAA IBTrACS 数据源，<br>
    一旦有新系统形成或达到热带低压强度，将立即推送预警。
  </div>
</div>
{% endif %}

<!-- Storm cards -->
{% if storms %}
<div class="storms-grid">
{% for s in storms %}
{% set ic = s.intensity.color %}
{% set wc = 'wind-danger' if (s.wind_kn or 0) >= 64 else ('wind-warn' if (s.wind_kn or 0) >= 34 else 'wind-low') %}
{% set impact_sit = 'sit-' + s.impact %}
<div class="scard impact-{{ s.impact }}">
  <!-- Card Head -->
  <div class="sc-head">
    <div class="sc-head-left">
      <div class="sc-intensity-badge" style="background:{{ ic }};">{{ s.intensity.abbr }}</div>
      <div>
        <div class="sc-name">{{ s.name }}</div>
        <div class="sc-sub">{{ s.basin_name }} · {{ s.sid }} · 最后更新 {{ s.last_time[5:16] if s.last_time else '—' }} UTC</div>
      </div>
    </div>
    {% set impact_labels = {'danger': '危险', 'warn': '预警', 'watch': '关注', 'monitor': '监测'} %}
    <span class="sc-impact-tag {{ impact_sit }}">{{ impact_labels[s.impact] }}</span>
  </div>

  <!-- Main data -->
  <div class="sc-data">
    <div class="sd-cell">
      <div class="sd-label">最大风速</div>
      <div class="sd-val {{ wc }}">{{ s.wind_kn|int if s.wind_kn else '—' }}<span class="sd-unit">kn</span></div>
      <div class="sd-sub">{{ s.intensity.name }}</div>
    </div>
    <div class="sd-cell">
      <div class="sd-label">中心气压</div>
      <div class="sd-val">{{ s.pres_mb|int if s.pres_mb else '—' }}<span class="sd-unit">hPa</span></div>
      <div class="sd-sub">{{ s.sshs_text or '—' }}</div>
    </div>
    <div class="sd-cell">
      <div class="sd-label">当前位置</div>
      <div class="sd-val" style="font-size:14px;">{{ '%.1f'|format(s.lat) }}°{{ 'N' if s.lat >= 0 else 'S' }}</div>
      <div class="sd-sub">{{ '%.1f'|format(s.lon|abs) }}°{{ 'E' if s.lon >= 0 else 'W' }}</div>
    </div>
    <div class="sd-cell">
      <div class="sd-label">距最近海岸</div>
      <div class="sd-val">{{ s.dist2land_nm|int if s.dist2land_nm else '—' }}<span class="sd-unit">nm</span></div>
      <div class="sd-sub">{{ '向陆地靠近' if s.dist2land_nm and s.dist2land_nm < 200 else '在海上' }}</div>
    </div>
  </div>

  <!-- Detail rows -->
  <div class="sc-details">
    <div class="detail-row">
      <span class="dr-icon">🧭</span>
      <span class="dr-label">移动方向/速度</span>
      <span class="dr-val">{{ s.mov_dir or '—' }} / {{ s.mov_speed or '—' }}kn</span>
    </div>
    <div class="detail-row">
      <span class="dr-icon">🌀</span>
      <span class="dr-label">34kn 风圈半径</span>
      <span class="dr-val">{{ s.r34_avg_nm or '—' }}<span class="dr-note">nm 平均</span></span>
    </div>
    <div class="detail-row">
      <span class="dr-icon">📡</span>
      <span class="dr-label">所在海盆</span>
      <span class="dr-val">{{ s.basin_name }}</span>
    </div>
    <div class="detail-row">
      <span class="dr-icon">⚠️</span>
      <span class="dr-label">最近影响航线</span>
      <span class="dr-val">{{ s.nearest_route_name }}<span class="dr-note">{{ s.nearest_route_dist }}nm</span></span>
    </div>
  </div>

  <!-- Shipping lane impact -->
  <div class="sc-impact">
    <div class="si-title">🚢 主要航运航线影响评估</div>
    <div class="si-routes">
      {% for dist, rname in s.nearby_routes %}
      {% set bar_w = [100 - (dist / 10), 5]|max|int %}
      {% set dist_cls = 'si-dist-danger' if dist <= 300 else ('si-dist-warn' if dist <= 500 else 'si-dist-ok') %}
      {% set bar_c = '#a32d2d' if dist <= 300 else ('#ef9f27' if dist <= 500 else '#8a9db5') %}
      <div class="si-route">
        <span class="si-route-name">{{ rname }}</span>
        <div class="si-dist-bar"><div class="si-dist-fill" style="width:{{ bar_w }}%;background:{{ bar_c }};"></div></div>
        <span class="si-dist-txt {{ dist_cls }}">{{ dist }} nm</span>
      </div>
      {% endfor %}
    </div>
  </div>
</div>
{% endfor %}
</div>
{% endif %}

<!-- Summary table (only shown when ≥2 storms) -->
{% if storms|length >= 2 %}
<div class="summary-card">
  <div class="sc-hd">
    <span class="sc-hd-title">全球活跃系统汇总</span>
    <span class="sc-hd-badge">{{ generated_at[:10] }} UTC</span>
  </div>
  <table>
    <thead><tr>
      <th style="text-align:left;">名称</th>
      <th>海盆</th><th>风速(kn)</th><th>气压(hPa)</th>
      <th>纬度</th><th>经度</th><th>最近航线(nm)</th><th>影响</th>
    </tr></thead>
    <tbody>
    {% for s in storms %}
    {% set ip_cls = 'ip-' + s.impact %}
    {% set il = {'danger':'危险','warn':'预警','watch':'关注','monitor':'监测'} %}
    <tr {% if s.impact == 'danger' %}class="danger-row"{% endif %}>
      <td>{{ s.intensity.abbr }} {{ s.name }}</td>
      <td>{{ s.basin_name }}</td>
      <td>{{ s.wind_kn|int if s.wind_kn else '—' }}</td>
      <td>{{ s.pres_mb|int if s.pres_mb else '—' }}</td>
      <td>{{ '%.1f'|format(s.lat) }}°{{ 'N' if s.lat >= 0 else 'S' }}</td>
      <td>{{ '%.1f'|format(s.lon|abs) }}°{{ 'E' if s.lon >= 0 else 'W' }}</td>
      <td>{{ s.nearest_route_name }} {{ s.nearest_route_dist }}nm</td>
      <td><span class="impact-pill {{ ip_cls }}">{{ il[s.impact] }}</span></td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
</div>
{% endif %}

<!-- Footer -->
<div class="footer">
  <div>
    <span class="ft-brand">{{ brand }}</span>
    &nbsp;气象导航 · 数据：NOAA IBTrACS NRT（全球）+ NOAA NHC（大西洋/东太平洋）
    · 更新频率：每 6 小时 · 仅供参考，请以官方气象机构公告为准
  </div>
  <span>生成：{{ generated_at }} UTC</span>
</div>

</div>
</body>
</html>"""


def render_html(storms: list, generated_at: str) -> str:
    """渲染 HTML 预警报告。"""
    # 统计 badge
    n_danger  = sum(1 for s in storms if s["impact"] == "danger")
    n_warn    = sum(1 for s in storms if s["impact"] == "warn")
    n_watch   = sum(1 for s in storms if s["impact"] == "watch")
    n_monitor = sum(1 for s in storms if s["impact"] == "monitor")

    stat_badges = []
    if not storms:
        stat_badges.append(("hst-g", "✅ 当前无活跃系统"))
    else:
        if n_danger: stat_badges.append(("hst-r", f"🚨 危险 {n_danger}"))
        if n_warn:   stat_badges.append(("hst-o", f"⚠️ 预警 {n_warn}"))
        if n_watch:  stat_badges.append(("hst-b", f"👁 关注 {n_watch}"))
        if n_monitor:stat_badges.append(("hst-g", f"📡 监测 {n_monitor}"))

    alert_storms = [s for s in storms if s["impact"] in ("danger", "warn")]

    ctx = {
        "date":           generated_at[:10],
        "brand":          Config.BRAND,
        "generated_at":   generated_at,
        "storms":         storms,
        "alert_storms":   alert_storms,
        "stat_badges":    stat_badges,
        "Config_CLOSE_DIST_NM": Config.CLOSE_DIST_NM,
    }

    if HAS_JINJA2:
        return Template(CYCLONE_HTML).render(**ctx)
    else:
        lines = [f"<html><body><h2>{Config.BRAND} 气旋预警 {generated_at}</h2>"]
        for s in storms:
            lines.append(f"<p>{s['intensity']['name']} {s['name']} "
                         f"{s['basin_name']} 风速{s['wind_kn']}kn 影响:{s['impact']}</p>")
        if not storms:
            lines.append("<p>当前无活跃热带气旋</p>")
        lines.append("</body></html>")
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 6. 截图（Playwright）
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


# ══════════════════════════════════════════════════════════════════════════════
# 7. 企业微信推送
# ══════════════════════════════════════════════════════════════════════════════

def build_wecom_card_storms(storms: list, generated_at: str) -> dict:
    """构造企业微信 markdown 消息（有系统时的完整版）。"""
    today = generated_at[:10]
    impact_emoji = {"danger": "🚨", "warn": "⚠️", "watch": "👁", "monitor": "📡"}
    impact_label = {"danger": "危险", "warn": "预警", "watch": "关注", "monitor": "监测"}

    lines = [
        f"# 🌀 {Config.BRAND} 全球热带气旋预警 · {today}",
        f"<font color=\"comment\">数据时间：{generated_at} UTC · NOAA IBTrACS NRT 全球覆盖</font>",
        "",
    ]

    # 每个风暴一块
    for s in storms:
        ic  = s["intensity"]
        col = "warning" if s["impact"] in ("danger", "warn") else \
              ("info" if s["impact"] == "watch" else "comment")
        ei  = impact_emoji[s["impact"]]

        lines += [
            f"## {ei} {ic['name']} **{s['name']}** <font color=\"{col}\">[ {impact_label[s['impact']]} ]</font>",
            f"> **{s['basin_name']}** · {s['sid']}",
            "",
            f"| 参数 | 数值 |",
            f"|:---|---:|",
            f"| 最大风速 | **{int(s['wind_kn']) if s['wind_kn'] else '—'} kn** |",
            f"| 中心气压 | **{int(s['pres_mb']) if s['pres_mb'] else '—'} hPa** |",
            f"| 当前位置 | {s['lat']:.1f}°{'N' if s['lat']>=0 else 'S'} / {abs(s['lon']):.1f}°{'E' if s['lon']>=0 else 'W'} |",
            f"| 移动方向/速 | {s['mov_dir'] or '—'} / {s['mov_speed'] or '—'} kn |",
            f"| 34kn风圈 | {s['r34_avg_nm'] or '—'} nm（均值） |",
            f"| 距最近海岸 | {int(s['dist2land_nm']) if s['dist2land_nm'] else '—'} nm |",
            "",
        ]

        # 航线影响
        lines.append("**🚢 航运影响（距主要航线）**")
        for dist, rname in s["nearby_routes"]:
            dist_col = "warning" if dist <= 300 else ("comment" if dist <= 500 else "")
            tag = "⛔" if dist <= 300 else ("⚠️" if dist <= 500 else "📌")
            if dist_col:
                lines.append(f"> {tag} {rname} <font color=\"{dist_col}\">{dist} nm</font>")
            else:
                lines.append(f"> {tag} {rname} {dist} nm")

        if s["impact"] in ("danger", "warn"):
            lines += [
                "",
                f"> <font color=\"warning\">建议距 {s['nearest_route_name']} 附近的船舶密切关注该系统动向，"
                f"提前规划绕航方案。</font>",
            ]
        lines.append("")

    lines.append(f"<font color=\"comment\">仅供参考，以官方气象机构公告为准 · {Config.BRAND}</font>")

    return {"msgtype": "markdown", "markdown": {"content": "\n".join(lines)}}


def build_wecom_card_no_storm(generated_at: str) -> dict:
    """无活跃系统时的简短推送。"""
    today = generated_at[:10]
    return {
        "msgtype": "markdown",
        "markdown": {
            "content": (
                f"# ✅ {Config.BRAND} 全球热带气旋监测 · {today}\n\n"
                f"> <font color=\"info\">当前全球各大洋**无活跃热带气旋**</font>\n\n"
                "| 监测范围 | 状态 |\n"
                "|:---|:---|\n"
                "| 西北太平洋（台风区） | ✅ 无系统 |\n"
                "| 北印度洋/孟加拉湾 | ✅ 无系统 |\n"
                "| 南印度洋/南太平洋 | ✅ 无系统 |\n"
                "| 大西洋/东太平洋 | ✅ 无系统 |\n\n"
                f"下次自动检测将在 6 小时后进行。\n\n"
                f"<font color=\"comment\">数据：NOAA IBTrACS NRT · {generated_at} UTC · {Config.BRAND}</font>"
            )
        }
    }


def push_wecom(storms: list, html_path: str, generated_at: str) -> bool:
    """推送企业微信（截图 + 文字）。"""
    if not Config.WECOM_WEBHOOK:
        log.warning("WECOM_WEBHOOK 未配置，跳过推送")
        return False

    def _post(payload):
        r = requests.post(Config.WECOM_WEBHOOK, json=payload, timeout=30)
        return r.json()

    img_ok = False

    # 1. 截图
    if html_path:
        try:
            img_bytes = html_to_image(html_path)
            b64 = base64.b64encode(img_bytes).decode()
            md5 = hashlib.md5(img_bytes).hexdigest()
            result = _post({"msgtype": "image", "image": {"base64": b64, "md5": md5}})
            if result.get("errcode") == 0:
                log.info("✅ 截图推送成功")
                img_ok = True
            else:
                log.warning(f"截图推送失败: {result}")
        except Exception as e:
            log.warning(f"截图失败，跳过: {e}")

    # 2. 文字 markdown
    try:
        payload = (build_wecom_card_storms(storms, generated_at)
                   if storms else build_wecom_card_no_storm(generated_at))
        result  = _post(payload)
        if result.get("errcode") == 0:
            log.info("✅ 文字推送成功")
            return True
        else:
            log.error(f"文字推送失败: {result}")
            return img_ok
    except Exception as e:
        log.error(f"推送异常: {e}")
        return img_ok


# ══════════════════════════════════════════════════════════════════════════════
# 8. 主流程
# ══════════════════════════════════════════════════════════════════════════════

def run_once(force: bool = False, check_only: bool = False) -> bool:
    log.info("=" * 60)
    log.info(f"{Config.BRAND} 热带气旋预警 — 开始执行")
    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")

    # 1. 拉取数据
    storms = fetch_ibtracs()

    # 2. 可选：补充 NHC 详细信息
    if any(s["basin"] in ("NA", "EP", "CP") for s in storms):
        nhc_details = fetch_nhc_details()
        # 可将 nhc_details 匹配到对应风暴（此处略，不影响主流程）

    # 3. 判断是否推送
    if check_only:
        log.info(f"Check-only 模式，共 {len(storms)} 个活跃系统，不推送")
        return True

    if not should_push(storms, force=force):
        return True

    # 4. 生成 HTML
    try:
        html = render_html(storms, now_utc)
    except Exception as e:
        log.error(f"HTML 渲染失败: {e}")
        return False

    # 5. 保存 HTML
    Config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    html_file = Config.OUTPUT_DIR / f"cyclone_alert_{today}.html"
    html_file.write_text(html, encoding="utf-8")
    log.info(f"✅ HTML 已保存: {html_file}")

    # 6. 推送企业微信
    ok = push_wecom(storms, str(html_file.resolve()), now_utc)

    # 7. 保存状态
    save_state(storms)

    log.info(f"推送结果: {'✅ 成功' if ok else '❌ 失败'}")
    log.info("=" * 60)
    return ok


# ══════════════════════════════════════════════════════════════════════════════
# 9. CLI 入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"{Config.BRAND} 热带气旋预警推送")
    parser.add_argument("--force",      action="store_true", help="强制推送（忽略状态缓存）")
    parser.add_argument("--check-only", action="store_true", help="只检查数据，不推送")
    parser.add_argument("--output-dir", default="./reports",  help="报告输出目录")
    args = parser.parse_args()

    Config.OUTPUT_DIR = Path(args.output_dir)
    Config.STATE_FILE = Config.OUTPUT_DIR / ".cyclone_state.json"

    success = run_once(force=args.force, check_only=args.check_only)
    sys.exit(0 if success else 1)
