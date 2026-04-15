"""
ISOWAY 全球热带气旋预警系统
============================
功能：
  1. 从 NOAA IBTrACS NRT 拉取全球所有活跃热带气旋（覆盖西太/印度洋/大西洋/南太等所有海盆）
  2. 从 NOAA NHC RSS/JSON 拉取大西洋/东太平洋系统（含 72h 预报讨论文本）
  3. 自动评估各系统对主要航运航线的影响距离
  4. 生成 HTML 预警报告（长截图用）+ 企业微信推送（截图 + markdown 文字）
  5. 无系统时推送"当前无活跃热带气旋"简报，有系统时立即推送完整预警

数据来源（三层融合，均免费）：
  - Layer 1: NAVGreen current/storms/v1（最实时，含完整历史+预报轨迹）需 NAVGREEN_TOKEN
  - Layer 2: NOAA IBTrACS NRT CSV（全球覆盖，补充 SSHS/R34/气压，有2-3天延迟）
  - Layer 3: NOAA NHC JSON/RSS（大西洋/东太平洋官方实时补充）

GitHub Actions 部署：
  - 事件触发：每 6 小时轮询一次（cron: '0 */6 * * *'）
  - 有新系统生成或强度显著变化时自动推送
  - Secrets: WECOM_WEBHOOK（必填）+ NAVGREEN_TOKEN（强烈建议，否则数据有延迟）

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

    # NAVGreen API（最实时数据源，含完整轨迹）
    NAVGREEN_CURRENT_URL = "https://miniapi.navgreen.cn/api/mete/forecast/current/storms/v1"
    NAVGREEN_TOKEN       = os.getenv("NAVGREEN_TOKEN", "")

    # IBTrACS NRT CSV — 全球覆盖兜底（每 3-6 小时更新，但有1-2天延迟）
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

# ══════════════════════════════════════════════════════════════════════════════
# 数据源优先级策略（三层融合）：
#
#  Layer 1 — NAVGreen current/storms/v1（最实时，每小时更新，含完整历史+预报轨迹）
#            ✅ 当前位置最准确，有 history[] + forecast[] 完整轨迹
#            ⚠️  只有当前活跃系统，无全球基础信息（海盆、SSHS等）
#
#  Layer 2 — NOAA IBTrACS NRT CSV（全球覆盖，含海盆/SSHS/R34风圈，但有2-3天延迟）
#            ✅ 覆盖 NAVGreen 没有的系统（大西洋、东太平洋）
#            ✅ 有 R34 风圈半径、SSHS 等级等详细参数
#            ⚠️  位置比 NAVGreen 落后 1-2 天
#
#  Layer 3 — NOAA NHC JSON/RSS（大西洋/东太平洋官方，实时）
#            ✅ 补充 NHC 活跃系统名称和链接
#
#  融合逻辑：
#   - NAVGreen 有的系统 → 用 NAVGreen 的位置/轨迹（最新），用 IBTrACS 补充 R34/SSHS
#   - IBTrACS 有但 NAVGreen 没有的系统 → 直接用 IBTrACS（保底）
#   - 轨迹画图：优先用 NAVGreen history[]（完整且最新），fallback IBTrACS positions
# ══════════════════════════════════════════════════════════════════════════════

NAVGREEN_TOKEN_ENV = "NAVGREEN_TOKEN"   # GitHub Secret 名

def fetch_navgreen_storms() -> list[dict]:
    """
    Layer 1: 从 NAVGreen current/storms/v1 拉取实时台风数据。
    返回格式统一化的风暴列表，包含完整历史轨迹和预报轨迹。
    需要 NAVGREEN_TOKEN 环境变量（从 localStorage 获取的登录 token）。
    """
    token = os.getenv(NAVGREEN_TOKEN_ENV, "")
    if not token:
        log.warning("NAVGREEN_TOKEN 未配置，跳过 NAVGreen 数据源")
        return []

    url = "https://miniapi.navgreen.cn/api/mete/forecast/current/storms/v1"
    try:
        r = requests.get(url, headers={"token": token}, timeout=Config.TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"NAVGreen storms API 失败: {e}")
        return []

    raw_storms = data.get("data", [])
    if not raw_storms:
        log.info("NAVGreen: 当前无活跃系统")
        return []

    result = []
    for s in raw_storms:
        name     = s.get("name", "UNNAMED")
        lat      = safe_float(s.get("lat"))
        lon      = safe_float(s.get("lon"))
        wind_ms  = safe_float(s.get("windSpeed"))   # 单位 m/s
        strength = s.get("strength", "")

        if lat is None or lon is None:
            continue

        # m/s → 节（1 m/s ≈ 1.94384 kn）
        wind_kn = round(wind_ms * 1.94384, 1) if wind_ms else None

        # 历史轨迹（API 返回倒序，反转为时间正序）
        history_raw = list(reversed(s.get("history", [])))
        # 预报轨迹
        forecast_raw = s.get("forecast", [])

        # 统一格式化轨迹点
        def fmt_pt(p, is_forecast=False):
            w_ms = safe_float(p.get("windSpeed"))
            return {
                "time":     p.get("time", ""),
                "lat":      str(p.get("lat", "")),
                "lon":      str(p.get("lon", "")),
                "usa_wind": str(round(w_ms * 1.94384)) if w_ms else "",
                "wmo_wind": "",
                "usa_pres": str(p.get("pressure", "") or ""),
                "r34_avg":  None,
                "r50_avg":  None,
                "r64_avg":  None,
                "is_forecast": is_forecast,
            }

        positions  = [fmt_pt(p) for p in history_raw]
        forecasts  = [fmt_pt(p, True) for p in forecast_raw]

        # 推断移动方向（最后两个历史点）
        mov_dir   = movement_direction(positions) if len(positions) >= 2 else None
        mov_speed = movement_speed_kn(positions)  if len(positions) >= 2 else None

        result.append({
            "sid":         f"NG-{s.get('id', name).lower()}",  # NAVGreen 内部 ID
            "ng_id":       s.get("id", ""),                    # 原始 NAVGreen id
            "name":        name,
            "basin":       _guess_basin(lat, lon),
            "subbasin":    "",
            "basin_name":  _basin_name_from_latlon(lat, lon),
            "lat":         lat,
            "lon":         lon,
            "wind_kn":     wind_kn,
            "pres_mb":     None,     # NAVGreen v1 无气压，后面从 IBTrACS 补
            "sshs":        None,     # 后面从 IBTrACS 补
            "sshs_text":   "",
            "dist2land_nm": None,
            "r34_avg_nm":  None,     # 后面从 IBTrACS 补
            "r50_avg_nm":  None,     # 后面从 IBTrACS 补
            "r64_avg_nm":  None,     # 后面从 IBTrACS 补
            "intensity":   wind_to_intensity(wind_kn),
            "last_time":   positions[-1]["time"] if positions else "",
            "mov_dir":     mov_dir,
            "mov_speed":   mov_speed,
            "positions":   positions,    # 历史轨迹（用于画图）
            "forecasts":   forecasts,    # 预报轨迹（画虚线）
            "source":      "navgreen",
        })

    log.info(f"✅ NAVGreen: {len(result)} 个活跃系统（含实时位置和完整轨迹）")
    return result


def _guess_basin(lat: float, lon: float) -> str:
    """根据经纬度推断 IBTrACS 海盆代码。"""
    if lat >= 0:
        if lon >= 100 or lon <= -120: return "WP"   # 西北太平洋
        if -120 < lon < -40:          return "NA"   # 北大西洋（粗略）
        if -40 <= lon < 100:          return "NI"   # 北印度洋
        if lon < -120:                return "EP"   # 东北太平洋
    else:
        if 20 <= lon < 90:            return "SI"   # 南印度洋
        if 90 <= lon <= 180:          return "SP"   # 南太平洋
        if -70 <= lon < 20:           return "SA"   # 南大西洋
    return "WP"


def _basin_name_from_latlon(lat: float, lon: float) -> str:
    basin = _guess_basin(lat, lon)
    names = {
        "WP": "西北太平洋", "EP": "东北太平洋", "NA": "北大西洋",
        "NI": "北印度洋",  "SI": "南印度洋",   "SP": "南太平洋",
        "SA": "南大西洋",
    }
    return names.get(basin, basin)


def merge_storm_data(ng_storms: list, ibt_storms: list) -> list:
    """
    三层融合：
    - NAVGreen 系统（有实时位置）+ IBTrACS 补充 SSHS/R34/气压
    - IBTrACS 独有系统（NAVGreen 没覆盖的，如大西洋）直接加入
    """
    # 建立 NAVGreen id 的索引（用 ng_id 和 name 匹配）
    ng_by_name = {s["name"].lower(): s for s in ng_storms}
    ng_by_id   = {s["ng_id"].lower(): s for s in ng_storms}

    merged = []
    ibt_matched = set()

    # 遍历 NAVGreen 系统，尝试从 IBTrACS 补充字段
    for ng in ng_storms:
        ng_name = ng["name"].lower()
        ng_id   = ng["ng_id"].lower()

        # 在 IBTrACS 里找同名系统
        ibt_match = None
        for ibt in ibt_storms:
            ibt_name = (ibt.get("name") or "").lower()
            if ibt_name == ng_name or ibt_name in ng_id or ng_id in ibt_name:
                ibt_match = ibt
                ibt_matched.add(ibt["sid"])
                break

        storm = dict(ng)  # 以 NAVGreen 为基础
        if ibt_match:
            # 用 IBTrACS 补充 NAVGreen 没有的字段
            storm["pres_mb"]    = storm["pres_mb"] or ibt_match.get("pres_mb")
            storm["sshs"]       = ibt_match.get("sshs")
            storm["sshs_text"]  = ibt_match.get("sshs_text", "")
            storm["r34_avg_nm"] = ibt_match.get("r34_avg_nm")
            storm["r50_avg_nm"] = ibt_match.get("r50_avg_nm")
            storm["r64_avg_nm"] = ibt_match.get("r64_avg_nm")
            storm["dist2land_nm"] = ibt_match.get("dist2land_nm")
            # IBTrACS 的 SID 留作参考
            storm["ibt_sid"]    = ibt_match.get("sid", "")
            log.info(f"  ✅ 融合: {ng['name']} — NAVGreen位置(-{abs(ng['lat']):.1f}°S/{ng['lon']}°E) + IBTrACS详情(SSHS={storm['sshs']})")
        else:
            log.info(f"  ℹ️  {ng['name']} — 仅 NAVGreen 数据，无 IBTrACS 匹配")

        merged.append(storm)

    # 把 IBTrACS 独有的系统（NAVGreen 没有的）也加入
    for ibt in ibt_storms:
        if ibt["sid"] not in ibt_matched:
            ibt_copy = dict(ibt)
            ibt_copy["source"] = "ibtracs"
            ibt_copy["forecasts"] = []
            merged.append(ibt_copy)
            log.info(f"  ➕ IBTrACS独有: {ibt['name']} ({ibt['basin_name']})")

    # 计算每个风暴的航线影响（统一用最新位置）
    for storm in merged:
        lat, lon = storm["lat"], storm["lon"]
        route_dists = []
        for rname, rlat, rlon in Config.SHIPPING_ROUTES:
            dist = haversine_nm(lat, lon, rlat, rlon)
            route_dists.append((dist, rname))
        route_dists.sort(key=lambda x: x[0])

        storm["nearest_route_dist"] = round(route_dists[0][0])
        storm["nearest_route_name"] = route_dists[0][1]
        storm["nearby_routes"] = [(round(d), n) for d, n in route_dists[:4]]

        wind_kn = storm.get("wind_kn") or 0
        dist    = storm["nearest_route_dist"]
        if dist <= Config.CLOSE_DIST_NM and wind_kn >= 34:
            storm["impact"] = "danger"
        elif dist <= Config.WARN_DIST_NM and wind_kn >= 25:
            storm["impact"] = "warn"
        elif dist <= Config.WATCH_DIST_NM and wind_kn >= 25:
            storm["impact"] = "watch"
        else:
            storm["impact"] = "monitor"

    # 排序：危险 > 预警 > 关注 > 监测，同级按风速降序
    impact_order = {"danger": 0, "warn": 1, "watch": 2, "monitor": 3}
    merged.sort(key=lambda x: (impact_order[x["impact"]], -(x.get("wind_kn") or 0)))
    return merged


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
        # R50 风圈半径（四象限，海里）
        r50_vals = []
        for q in ("USA_R50_NE", "USA_R50_SE", "USA_R50_SW", "USA_R50_NW"):
            v = safe_float(col(row, q))
            if v and v > 0:
                r50_vals.append(v)
        # R64 风圈半径（四象限，海里）
        r64_vals = []
        for q in ("USA_R64_NE", "USA_R64_SE", "USA_R64_SW", "USA_R64_NW"):
            v = safe_float(col(row, q))
            if v and v > 0:
                r64_vals.append(v)

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
            "r50_avg":    round(sum(r50_vals) / len(r50_vals)) if r50_vals else None,
            "r64_avg":    round(sum(r64_vals) / len(r64_vals)) if r64_vals else None,
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
            "r50_avg_nm":   latest["r50_avg"],
            "r64_avg_nm":   latest["r64_avg"],
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
# 4b. 台风轨迹地图 SVG 生成（纯内置，无需外部地图服务）
# ══════════════════════════════════════════════════════════════════════════════


# ── 各海盆的地图显示范围 [lon_min, lat_min, lon_max, lat_max] ─────────────────
BASIN_BBOX = {
    "WP":  [90,  -5,  180, 45],   # 西北太平洋（台风区）
    "EP":  [-140, 5,  -70, 35],   # 东北太平洋
    "NA":  [-100, 5,  -20, 45],   # 北大西洋
    "NI":  [40,   0,   100, 30],  # 北印度洋（含孟加拉湾+阿拉伯海）
    "SI":  [30,  -45,  115, -5],  # 南印度洋
    "SP":  [90,  -45,  180, -5],  # 南太平洋（东澳）
    "SA":  [-60, -35,   10,  5],  # 南大西洋
    "DEFAULT": [60, -50, 190, 50],
}


def _merc_y(lat_deg: float) -> float:
    """Web墨卡托Y值（处理极端纬度）"""
    lat = max(-85.0, min(85.0, lat_deg))
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def latlon_to_svg(lat: float, lon: float, bbox: list, W: int, H: int):
    """经纬度 → SVG画布坐标（墨卡托投影）"""
    lon_min, lat_min, lon_max, lat_max = bbox
    # 经度线性映射
    x = (lon - lon_min) / (lon_max - lon_min) * W
    # 纬度墨卡托
    y_min = _merc_y(lat_min)
    y_max = _merc_y(lat_max)
    y_val = _merc_y(lat)
    y = (1.0 - (y_val - y_min) / (y_max - y_min)) * H
    return round(x, 1), round(y, 1)


def _wind_to_color(wind_kn: float) -> str:
    """风速 → 颜色（蓝→绿→黄→橙→红→深红）"""
    if wind_kn is None or wind_kn < 25:
        return "#8a9db5"   # 灰蓝  扰动/低压
    if wind_kn < 34:
        return "#378ADD"   # 蓝    热带低压
    if wind_kn < 48:
        return "#1D9E75"   # 绿    热带风暴
    if wind_kn < 64:
        return "#ef9f27"   # 橙    强热带风暴
    if wind_kn < 96:
        return "#e24b4a"   # 红    台风
    if wind_kn < 114:
        return "#c0392b"   # 深红  强台风
    return "#7b241c"       # 暗红  超强台风


def _nm_to_px(radius_nm: float, bbox: list, W: int) -> float:
    """海里半径 → SVG像素大小"""
    if not radius_nm:
        return 0
    lon_span = bbox[2] - bbox[0]
    px_per_deg = W / lon_span
    nm_per_deg = 60.0
    return radius_nm / nm_per_deg * px_per_deg


def generate_track_svg(storm: dict, W: int = 680, H: int = 360) -> str:
    """
    为单个风暴生成带轨迹的 SVG 地图（嵌入HTML）
    
    storm 字段要求：
      basin, lat, lon, positions (list of dicts with lat/lon/wind/time)
    """
    basin    = storm.get("basin", "DEFAULT")
    bbox     = BASIN_BBOX.get(basin, BASIN_BBOX["DEFAULT"])
    positions = storm.get("positions", [])
    
    # 根据当前位置自动调整 bbox 中心（确保风暴在视野内）
    cur_lat = storm.get("lat", 0) or 0
    cur_lon = storm.get("lon", 0) or 0
    lon_mid = (bbox[0] + bbox[2]) / 2
    lat_mid = (bbox[1] + bbox[3]) / 2
    lon_half = (bbox[2] - bbox[0]) / 2
    lat_half = (bbox[3] - bbox[1]) / 2
    # 如果当前位置偏离bbox中心太多，重新居中
    if abs(cur_lon - lon_mid) > lon_half * 0.6 or abs(cur_lat - lat_mid) > lat_half * 0.6:
        bbox = [cur_lon - lon_half, cur_lat - lat_half,
                cur_lon + lon_half, cur_lat + lat_half]

    def sv(lat, lon):
        return latlon_to_svg(lat, lon, bbox, W, H)

    # ── 筛选有效轨迹点 ────────────────────────────────────────────────────────
    track = []
    for p in positions:
        try:
            lat = float(p.get("lat", "") or 0)
            lon = float(p.get("lon", "") or 0)
            wind = float(p.get("usa_wind") or p.get("wmo_wind") or 0) or None
            t    = (p.get("time") or "")[:16]
            if lat == 0 and lon == 0:
                continue
            # 只保留 bbox 范围内 ± 20% 的点
            margin_lon = (bbox[2] - bbox[0]) * 0.2
            margin_lat = (bbox[3] - bbox[1]) * 0.2
            if not (bbox[0]-margin_lon <= lon <= bbox[2]+margin_lon and
                    bbox[1]-margin_lat <= lat <= bbox[3]+margin_lat):
                continue
            track.append({"lat": lat, "lon": lon, "wind": wind, "time": t,
                           "r34": p.get("r34_avg"),
                           "r50": p.get("r50_avg"),
                           "r64": p.get("r64_avg")})
        except Exception:
            continue

    if len(track) < 2:
        # 至少用当前位置造一个点
        track = [{"lat": cur_lat, "lon": cur_lon,
                  "wind": storm.get("wind_kn"), "time": "",
                  "r34": None, "r50": None, "r64": None}]

    # ── SVG 开始 ──────────────────────────────────────────────────────────────
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        f'style="background:#0d2137;border-radius:10px;display:block;">',

        # 定义渐变和滤镜
        '<defs>',
        '  <filter id="glow"><feGaussianBlur stdDeviation="2.5" result="blur"/>'
        '<feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter>',
        '  <radialGradient id="bgGrad" cx="50%" cy="50%">',
        '    <stop offset="0%" stop-color="#0d2a45"/>',
        '    <stop offset="100%" stop-color="#081520"/>',
        '  </radialGradient>',
        '</defs>',

        # 背景
        f'<rect width="{W}" height="{H}" fill="url(#bgGrad)"/>',
    ]

    # ── 绘制简化海岸线（重要地标）─────────────────────────────────────────────
    coastlines = _get_coastline_paths(basin, bbox, W, H)
    for path_d in coastlines:
        lines.append(f'<path d="{path_d}" fill="#1a3a5c" stroke="#2a5a8c" '
                     f'stroke-width="0.5" opacity="0.7"/>')

    # ── 经纬度网格 ─────────────────────────────────────────────────────────────
    lon_min, lat_min, lon_max, lat_max = bbox
    # 纬线（每10度）
    for lat in range(int(lat_min//10)*10, int(lat_max//10+1)*10, 10):
        if lat_min <= lat <= lat_max:
            x0, y0 = sv(lat, lon_min)
            x1, y1 = sv(lat, lon_max)
            lines.append(f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}" '
                         f'stroke="rgba(255,255,255,0.08)" stroke-width="0.5"/>')
            lines.append(f'<text x="{x0+3}" y="{y0-2}" '
                         f'fill="rgba(255,255,255,0.3)" font-size="9">'
                         f'{abs(lat)}°{"N" if lat>=0 else "S"}</text>')
    # 经线（每10度）
    for lon in range(int(lon_min//10)*10, int(lon_max//10+1)*10, 10):
        if lon_min <= lon <= lon_max:
            x0, y0 = sv(lat_max, lon)
            x1, y1 = sv(lat_min, lon)
            lines.append(f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}" '
                         f'stroke="rgba(255,255,255,0.08)" stroke-width="0.5"/>')
            lines.append(f'<text x="{x0+2}" y="{y1-3}" '
                         f'fill="rgba(255,255,255,0.3)" font-size="9">'
                         f'{abs(lon)}°{"E" if lon>=0 else "W"}</text>')

    # ── 绘制多层同心风圈（最新位置）Windy 风格 ────────────────────────────────
    latest = track[-1]
    cur_x, cur_y = sv(latest["lat"], latest["lon"])

    WIND_CIRCLE_DEFS = [
        ("r34", "#FFD700", "rgba(255,215,0,0.10)"),    # 34kt 大风圈 — 金黄色
        ("r50", "#FF8C00", "rgba(255,140,0,0.12)"),    # 50kt 暴风圈 — 橙色
        ("r64", "#FF1493", "rgba(255,20,147,0.14)"),   # 64kt 飓风圈 — 深粉色
    ]
    for rkey, stroke_color, fill_color in WIND_CIRCLE_DEFS:
        r_nm = latest.get(rkey)
        if r_nm:
            r_px = _nm_to_px(float(r_nm), bbox, W)
            lines.append(
                f'<ellipse cx="{cur_x}" cy="{cur_y}" '
                f'rx="{r_px:.1f}" ry="{r_px*0.8:.1f}" '
                f'fill="{fill_color}" '
                f'stroke="{stroke_color}" stroke-width="1.5" opacity="0.85"/>'
            )

    # ── 绘制轨迹线（颜色按风速渐变，加粗）──────────────────────────────────────
    for i in range(len(track) - 1):
        p1, p2 = track[i], track[i+1]
        x1, y1 = sv(p1["lat"], p1["lon"])
        x2, y2 = sv(p2["lat"], p2["lon"])
        color = _wind_to_color(p2.get("wind") or 0)
        lines.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{color}" stroke-width="4.5" stroke-linecap="round" opacity="1.0"/>'
        )

    # ── 绘制轨迹点（每个点都画，增大标记）─────────────────────────────────────────
    for i, p in enumerate(track):
        if i == len(track) - 1:
            continue  # 最后一个点（当前位置）单独画
        x, y = sv(p["lat"], p["lon"])
        wind = p.get("wind") or 0
        color = _wind_to_color(wind)
        lines.append(
            f'<circle cx="{x}" cy="{y}" r="4" '
            f'fill="{color}" stroke="rgba(0,0,0,0.3)" stroke-width="0.5" opacity="0.9"/>'
        )

    # ── 绘制预报轨迹（不确定性锥体 + 粗虚线 + 圆形标记）────────────────────────
    forecast_pts = storm.get("forecasts", [])

    # 如果没有预报数据，基于轨迹移动方向自动生成合成预报（+24/48/72/96h）
    if (not forecast_pts or len(forecast_pts) == 0) and len(track) >= 2:
        p_prev, p_cur = track[-2], track[-1]
        dlat = p_cur["lat"] - p_prev["lat"]
        dlon = p_cur["lon"] - p_prev["lon"]
        dist_step = math.sqrt(dlat**2 + dlon**2)
        if dist_step > 0.01:  # 有明显移动才生成预报
            cur_wind_val = float(latest.get("wind") or storm.get("wind_kn") or 0)
            cur_r34 = latest.get("r34")
            cur_r50 = latest.get("r50")
            cur_r64 = latest.get("r64")
            forecast_pts = []
            for hours in [24, 48, 72, 96]:
                steps = hours / 6.0  # 每步约6h
                fc_lat = p_cur["lat"] + dlat * steps
                fc_lon = p_cur["lon"] + dlon * steps
                # 风速保持不变（保守估计）
                forecast_pts.append({
                    "lat": str(round(fc_lat, 2)),
                    "lon": str(round(fc_lon, 2)),
                    "windSpeed": None,
                    "usa_wind": str(int(cur_wind_val)),
                    "pressure": None,
                })

    if forecast_pts and track:
        fc_track = [{"lat": latest["lat"], "lon": latest["lon"],
                     "wind": safe_float(latest.get("usa_wind") or latest.get("wind"))}]
        for fp in forecast_pts:
            try:
                flat = safe_float(fp.get("lat"))
                flon = safe_float(fp.get("lon"))
                if flat is None or flon is None: continue
                if abs(flat) < 0.01 and abs(flon) < 0.01: continue
                fw = safe_float(fp.get("usa_wind") or fp.get("windSpeed"))
                if fw and fw > 5:  # m/s → kn
                    fw = fw * 1.94384
                fc_track.append({"lat": flat, "lon": flon, "wind": fw})
            except Exception:
                continue

        if len(fc_track) >= 2:
            # ── 不确定性锥体 ──────────────────────────────────────────────────
            spread_per_step_nm = 25
            cone_left = []
            cone_right = []
            for i, fp in enumerate(fc_track):
                fx, fy = sv(fp["lat"], fp["lon"])
                if i == 0:
                    cone_left.append((fx, fy))
                    cone_right.append((fx, fy))
                    continue
                prev = fc_track[i-1]
                dx = fx - sv(prev["lat"], prev["lon"])[0]
                dy = fy - sv(prev["lat"], prev["lon"])[1]
                d = math.sqrt(dx**2 + dy**2)
                if d < 0.5:
                    cone_left.append((fx, fy))
                    cone_right.append((fx, fy))
                    continue
                px, py = -dy/d, dx/d  # 垂直方向
                spread_px = _nm_to_px(spread_per_step_nm * i, bbox, W)
                spread_px = max(spread_px, 3)
                cone_left.append((fx + px * spread_px, fy + py * spread_px))
                cone_right.append((fx - px * spread_px, fy - py * spread_px))

            points = cone_left + list(reversed(cone_right))
            pts_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
            lines.append(
                f'<polygon points="{pts_str}" '
                f'fill="rgba(255,255,255,0.08)" '
                f'stroke="rgba(255,255,255,0.20)" stroke-width="1"/>'
            )

            # ── 预报中心线（粗虚线）─────────────────────────────────────────────
            for i in range(len(fc_track) - 1):
                p1, p2 = fc_track[i], fc_track[i+1]
                x1, y1 = sv(p1["lat"], p1["lon"])
                x2, y2 = sv(p2["lat"], p2["lon"])
                fc_color = _wind_to_color(p2.get("wind") or 0)
                lines.append(
                    f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                    f'stroke="{fc_color}" stroke-width="4.5" stroke-linecap="round" '
                    f'stroke-dasharray="10,7" opacity="0.85"/>'
                )

            # ── 预报位置标记 + 预报风圈 ──────────────────────────────────────────
            # 用最新位置的风圈数据绘制每个预报点的风圈
            cur_r34 = latest.get("r34")
            cur_r50 = latest.get("r50")
            cur_r64 = latest.get("r64")
            for i in range(1, len(fc_track)):
                fp = fc_track[i]
                fx, fy = sv(fp["lat"], fp["lon"])
                fc_color = _wind_to_color(fp.get("wind") or 0)
                # 预报风圈（逐渐扩大，模拟不确定性增长）
                scale = 1.0 + i * 0.15
                fc_wind_circles = [
                    (cur_r34, "#FFD700", "rgba(255,215,0,0.06)"),
                    (cur_r50, "#FF8C00", "rgba(255,140,0,0.07)"),
                    (cur_r64, "#FF1493", "rgba(255,20,147,0.08)"),
                ]
                for r_nm, stroke_c, fill_c in fc_wind_circles:
                    if r_nm:
                        r_px = _nm_to_px(float(r_nm) * scale, bbox, W)
                        lines.append(
                            f'<ellipse cx="{fx}" cy="{fy}" '
                            f'rx="{r_px:.1f}" ry="{r_px*0.8:.1f}" '
                            f'fill="{fill_c}" '
                            f'stroke="{stroke_c}" stroke-width="1" opacity="0.6"/>'
                        )
                # 预报位置标记
                lines.append(
                    f'<circle cx="{fx}" cy="{fy}" r="5" '
                    f'fill="{fc_color}" stroke="white" stroke-width="1.5" opacity="0.9"/>'
                )
                # 预报时间标注
                hours_ahead = i * 24
                lines.append(
                    f'<text x="{fx}" y="{fy-9}" text-anchor="middle" '
                    f'fill="rgba(255,255,255,0.7)" font-size="8" font-weight="600">+{hours_ahead}h</text>'
                )

    # ── 当前位置：更大的脉冲标记 ──────────────────────────────────────────────
    cur_wind  = latest.get("wind") or storm.get("wind_kn") or 0
    cur_color = _wind_to_color(float(cur_wind))
    lines += [
        # 外圈脉冲（更大）
        f'<circle cx="{cur_x}" cy="{cur_y}" r="20" fill="none" '
        f'stroke="{cur_color}" stroke-width="2" opacity="0.5">',
        f'  <animate attributeName="r" values="16;28;16" dur="2s" repeatCount="indefinite"/>',
        f'  <animate attributeName="opacity" values="0.5;0;0.5" dur="2s" repeatCount="indefinite"/>',
        f'</circle>',
        # 中圈
        f'<circle cx="{cur_x}" cy="{cur_y}" r="12" fill="{cur_color}" opacity="0.3"/>',
        # 实心中心（更大）
        f'<circle cx="{cur_x}" cy="{cur_y}" r="7" fill="{cur_color}" '
        f'stroke="white" stroke-width="2" filter="url(#glow)"/>',
        # 台风符号
        f'<text x="{cur_x}" y="{cur_y - 18}" text-anchor="middle" '
        f'font-size="16" fill="white" filter="url(#glow)">&#x1F300;</text>',
    ]

    # ── 风暴名标签 ─────────────────────────────────────────────────────────────
    name     = storm.get("name", "")
    abbr     = storm.get("intensity", {}).get("abbr", "")
    wind_kn  = storm.get("wind_kn")
    label_x  = min(W - 10, max(10, cur_x + 14))
    label_y  = max(20, cur_y - 10)
    lines += [
        f'<rect x="{label_x-4}" y="{label_y-12}" width="{len(name)*7+55}" height="28" '
        f'rx="4" fill="rgba(0,0,0,0.65)" stroke="{cur_color}" stroke-width="1"/>',
        f'<text x="{label_x}" y="{label_y}" fill="white" font-size="11" font-weight="700">'
        f'{abbr} {name}</text>',
        f'<text x="{label_x}" y="{label_y+12}" fill="{cur_color}" font-size="10">'
        f'{int(wind_kn) if wind_kn else "—"}kn · {storm.get("intensity",{}).get("name","")}</text>',
    ]

    # ── 图例（双列：强度色标 + 风圈颜色）───────────────────────────────────────
    legend_items = [
        ("#378ADD", "TD <34kn"),
        ("#1D9E75", "TS 34-48kn"),
        ("#ef9f27", "STS 48-64kn"),
        ("#e24b4a", "TY 64-96kn"),
        ("#7b241c", "SuperTY 114+kn"),
    ]
    wind_circle_legend = [
        ("#FFD700", "34kt gale"),
        ("#FF8C00", "50kt storm"),
        ("#FF1493", "64kt hurricane"),
        (None, "--- forecast"),
    ]
    all_legend = legend_items + [(None, None)] * max(0, len(wind_circle_legend) - len(legend_items))
    all_wc = wind_circle_legend + [(None, None)] * max(0, len(legend_items) - len(wind_circle_legend))
    lx, ly = 6, H - 8 - max(len(legend_items), len(wind_circle_legend)) * 14
    legend_h = max(len(legend_items), len(wind_circle_legend)) * 14 + 8
    legend_w = 280
    lines.append(f'<rect x="4" y="{ly-4}" width="{legend_w}" height="{legend_h}" '
                 f'rx="4" fill="rgba(0,0,0,0.55)"/>')
    # 左列：强度色标
    for j, (lcolor, ltext) in enumerate(legend_items):
        yl = ly + j * 14
        lines.append(f'<rect x="{lx}" y="{yl}" width="14" height="5" '
                     f'rx="2" fill="{lcolor}"/>')
        lines.append(f'<text x="{lx+18}" y="{yl+5}" fill="rgba(255,255,255,0.75)" '
                     f'font-size="8.5">{ltext}</text>')
    # 右列：风圈颜色 + 预报线型
    rc_x = 130
    for j, (lcolor, ltext) in enumerate(wind_circle_legend):
        yl = ly + j * 14
        if lcolor and "forecast" not in ltext:
            lines.append(f'<circle cx="{rc_x+4}" cy="{yl+3}" r="4" '
                         f'fill="none" stroke="{lcolor}" stroke-width="1.5"/>')
        else:
            # 预报虚线图例
            lines.append(f'<line x1="{rc_x-2}" y1="{yl+3}" x2="{rc_x+10}" y2="{yl+3}" '
                         f'stroke="rgba(255,255,255,0.6)" stroke-width="2" stroke-dasharray="4,3"/>')
        lines.append(f'<text x="{rc_x+14}" y="{yl+5}" fill="rgba(255,255,255,0.75)" '
                     f'font-size="8.5">{ltext}</text>')

    # ── 数据来源标注 ──────────────────────────────────────────────────────────
    lines.append(
        f'<text x="{W-4}" y="{H-4}" text-anchor="end" '
        f'fill="rgba(255,255,255,0.3)" font-size="8">NOAA IBTrACS NRT</text>'
    )

    lines.append('</svg>')
    return "\n".join(lines)


def _get_coastline_paths(basin: str, bbox: list, W: int, H: int) -> list:
    """
    返回简化海岸线的 SVG path 字符串列表。
    使用关键地标多边形（澳大利亚、亚洲大陆、日本等），
    不依赖外部库，轻量内置。
    """
    def sv(lat, lon):
        return latlon_to_svg(lat, lon, bbox, W, H)

    def poly_path(points):
        """多边形坐标 → SVG path"""
        coords = []
        for i, (lat, lon) in enumerate(points):
            x, y = sv(lat, lon)
            coords.append(f"{'M' if i==0 else 'L'}{x},{y}")
        return " ".join(coords) + " Z"

    paths = []

    # ── 澳大利亚 ─────────────────────────────────────────────────────────────
    australia = [
        (-37.5,140),(-38,143),(-39,147),(-37,150),(-34,151),(-32,152),
        (-28,154),(-25,153),(-23,151),(-20,149),(-17,146),(-15,145),
        (-12,143),(-12,136),(-14,129),(-16,124),(-20,119),(-22,114),
        (-25,114),(-29,115),(-32,116),(-34,119),(-34,123),(-34,125),
        (-32,125),(-34,122),(-34,128),(-33,133),(-32,137),(-34,140),(-37.5,140)
    ]
    paths.append(poly_path(australia))

    # ── 亚洲大陆南部（简化）─────────────────────────────────────────────────
    if bbox[0] < 160 and bbox[3] > 0:
        asia_south = [
            (5,100),(8,98),(10,99),(13,100),(16,103),(18,107),(20,110),
            (22,114),(24,118),(26,120),(30,122),(32,122),(35,120),(38,121),
            (40,122),(40,118),(37,117),(35,118),(35,115),(30,120),(25,119),
            (22,113),(18,110),(15,108),(12,109),(10,104),(8,100),(5,100)
        ]
        paths.append(poly_path(asia_south))

    # ── 日本（简化）─────────────────────────────────────────────────────────
    if bbox[0] < 145 and bbox[3] > 30:
        japan = [
            (31,131),(32,132),(33,131),(33,130),(31,131)
        ]
        paths.append(poly_path(japan))
        japan2 = [
            (34,136),(35,137),(36,137),(37,138),(38,141),
            (40,142),(41,141),(40,140),(38,141),(36,138),(34,136)
        ]
        paths.append(poly_path(japan2))

    # ── 菲律宾（简化）───────────────────────────────────────────────────────
    if 110 < bbox[0]+bbox[2]//2 < 145 and bbox[3] > 5:
        phil = [
            (7,126),(9,125),(11,124),(14,122),(17,122),(18,121),(17,120),
            (14,121),(12,123),(9,126),(7,126)
        ]
        paths.append(poly_path(phil))

    # ── 新西兰（简化）───────────────────────────────────────────────────────
    if basin in ("SP", "DEFAULT") and bbox[2] > 165:
        nz_north = [
            (-34,173),(-37,175),(-39,177),(-41,175),(-39,174),(-37,174),(-34,173)
        ]
        paths.append(poly_path(nz_north))
        nz_south = [
            (-41,174),(-43,172),(-45,169),(-46,168),(-46,170),(-44,172),(-42,173),(-41,174)
        ]
        paths.append(poly_path(nz_south))

    # ── 印度半岛（简化）─────────────────────────────────────────────────────
    if basin in ("NI", "DEFAULT") and 65 < bbox[0]+bbox[2]//2 < 100:
        india = [
            (23,68),(22,72),(20,73),(18,73),(16,73),(13,80),(10,80),
            (8,77),(8,80),(10,76),(14,75),(16,74),(18,73),(22,73),(23,68)
        ]
        paths.append(poly_path(india))

    # ── 非洲东部/马达加斯加（简化）──────────────────────────────────────────
    if basin in ("SI", "DEFAULT"):
        madagascar = [
            (-12,49),(-15,50),(-18,48),(-20,44),(-22,44),(-24,44),
            (-25,47),(-25,48),(-22,48),(-20,48),(-15,50),(-12,49)
        ]
        paths.append(poly_path(madagascar))

    return paths


# ══════════════════════════════════════════════════════════════════════════════
# 5. HTML 报告生成
# ══════════════════════════════════════════════════════════════════════════════

CYCLONE_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>全球热带气旋预警 — {{ date }} | {{ brand }}</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Mono:wght@400;500&family=Outfit:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing:border-box; margin:0; padding:0; }
:root {
  --ink:#0f1217; --ink-2:#4a5261; --ink-m:#8a93a3;
  --surface:#ffffff; --sf-soft:#f5f6f8; --sf-mid:#eceef2;
  --accent:#1a0a2e; --accent-l:#ede8f5; --accent-mid:#4a2d96;
  --red:#c0392b; --red-l:#fdf0ee; --amber:#b86c0a; --amber-l:#fdf5e8;
  --green:#1e7f5a; --green-l:#e8f5f0;
  --blue:#185fa5; --blue-l:#e8f2fb;
  --border:#dde1e8; --border-s:#c4c9d4;
}
body { font-family:'Outfit','PingFang SC','Microsoft YaHei',sans-serif;
       background:#eef0f4; color:var(--ink); padding:2.5rem 1.5rem;
       -webkit-print-color-adjust:exact; print-color-adjust:exact; }
.page { max-width:1060px; margin:0 auto; background:var(--surface);
        border:1px solid var(--border); box-shadow:0 8px 40px rgba(0,0,0,0.08); }
/* Header — 台风用深紫色 */
.header { background:linear-gradient(135deg,#1a0a2e 0%,#2d1b69 60%,#0d2137 100%);
          padding:2rem 2.8rem 1.6rem; position:relative; overflow:hidden; }
.header::before { content:''; position:absolute; top:-70px; right:-70px;
  width:300px; height:300px; border-radius:50%; background:rgba(255,255,255,0.03); }
.header::after  { content:''; position:absolute; bottom:-90px; left:200px;
  width:220px; height:220px; border-radius:50%; background:rgba(255,255,255,0.02); }
.header-top { display:flex; justify-content:space-between;
              align-items:flex-start; margin-bottom:1.2rem; }
.doc-label { font-family:'DM Mono',monospace; font-size:10px; letter-spacing:0.15em;
             color:rgba(255,255,255,0.38); text-transform:uppercase; margin-bottom:0.4rem; }
.doc-title { font-family:'DM Serif Display',serif; font-size:24px; color:#fff; line-height:1.3; }
.doc-title em { font-style:italic; color:rgba(255,255,255,0.60); }
.doc-meta { text-align:right; }
.doc-meta .meta-date { font-family:'DM Mono',monospace; font-size:11px;
                       color:rgba(255,255,255,0.45); letter-spacing:0.05em; }
.doc-meta .meta-ref  { font-size:11px; color:rgba(255,255,255,0.28); margin-top:3px; }
.status-strip { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
.sb { display:inline-flex; align-items:center; gap:5px; font-size:11px; font-weight:500;
      padding:4px 10px; border-radius:2px; letter-spacing:0.02em; }
.sb-r { background:rgba(192,57,43,0.30); color:#f0a09a; outline:1px solid rgba(192,57,43,0.35); }
.sb-o { background:rgba(184,108,10,0.30); color:#f5c76e; outline:1px solid rgba(184,108,10,0.35); }
.sb-b { background:rgba(24,95,165,0.30);  color:#85b7eb; outline:1px solid rgba(24,95,165,0.35); }
.sb-g { background:rgba(30,127,90,0.25);  color:#7de0b8; outline:1px solid rgba(30,127,90,0.30); }
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
/* No storm */
.no-storm { border:1px solid var(--border); padding:2.5rem; text-align:center; margin-bottom:1.8rem; }
.no-storm-icon  { font-size:48px; margin-bottom:12px; }
.no-storm-title { font-family:'DM Serif Display',serif; font-size:20px; color:var(--ink); margin-bottom:8px; }
.no-storm-text  { font-size:12.5px; color:var(--ink-m); line-height:1.7; max-width:500px; margin:0 auto; }
/* Alert bars */
.alert-bars { display:flex; flex-direction:column; gap:6px; margin-bottom:1.8rem; }
.abar { padding:10px 16px; display:flex; align-items:flex-start; gap:12px;
         border-left:3px solid transparent; }
.abar-icon  { font-size:18px; flex-shrink:0; margin-top:1px; }
.abar-title { font-size:12px; font-weight:700; margin-bottom:3px; letter-spacing:0.02em; }
.abar-text  { font-size:11px; line-height:1.55; }
.abar-danger { background:#fff8f8; border-left-color:var(--red);   border:1px solid #f0c0bc; }
.abar-danger .abar-title { color:var(--red); }
.abar-danger .abar-text  { color:#7a2a24; }
.abar-warn   { background:var(--amber-l); border-left-color:var(--amber); border:1px solid #e8c990; }
.abar-warn   .abar-title { color:var(--amber); }
.abar-warn   .abar-text  { color:#7a4a0a; }
.abar-watch  { background:var(--blue-l); border-left-color:var(--blue); border:1px solid #b0cce4; }
.abar-watch  .abar-title { color:var(--blue); }
.abar-watch  .abar-text  { color:#184070; }
/* Storm cards */
.storms-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(460px,1fr));
               gap:1px; background:var(--border); border:1px solid var(--border);
               margin-bottom:1.8rem; }
.scard { background:var(--surface); overflow:hidden; }
.scard.impact-danger { outline:2px solid var(--red);   outline-offset:-2px; }
.scard.impact-warn   { outline:1.5px solid var(--amber); outline-offset:-1.5px; }
.scard.impact-watch  { outline:1px solid var(--blue); outline-offset:-1px; }
/* Card head */
.sc-head { padding:12px 16px; display:flex; justify-content:space-between;
            align-items:flex-start; border-bottom:1px solid var(--border); }
.sc-head-left { display:flex; align-items:center; gap:10px; }
.sc-intensity-badge { width:42px; height:42px; border-radius:6px;
                       display:flex; align-items:center; justify-content:center;
                       font-family:'DM Mono',monospace; font-size:11px;
                       font-weight:700; color:#fff; flex-shrink:0; }
.sc-name { font-family:'DM Serif Display',serif; font-size:17px; line-height:1.2; }
.sc-sub  { font-family:'DM Mono',monospace; font-size:9.5px; color:var(--ink-m); margin-top:2px; letter-spacing:0.04em; }
.sc-impact-tag { font-size:9px; font-weight:700; padding:3px 9px; border-radius:2px;
                  letter-spacing:0.06em; text-transform:uppercase; }
.sit-danger  { background:var(--red-l);   color:var(--red);   border:1px solid #e8b4b0; }
.sit-warn    { background:var(--amber-l); color:var(--amber); border:1px solid #e8c990; }
.sit-watch   { background:var(--blue-l);  color:var(--blue);  border:1px solid #b0cce4; }
.sit-monitor { background:var(--sf-mid);  color:var(--ink-m); border:1px solid var(--border); }
/* Data grid */
.sc-data { display:grid; grid-template-columns:repeat(4,1fr);
            gap:1px; background:var(--border); border-bottom:1px solid var(--border); }
.sd-cell  { background:var(--sf-soft); padding:8px 14px; }
.sd-label { font-size:9.5px; font-weight:600; letter-spacing:0.08em;
             text-transform:uppercase; color:var(--ink-m); margin-bottom:3px; }
.sd-val   { font-family:'DM Mono',monospace; font-size:20px;
             font-weight:500; color:var(--ink); line-height:1.1; }
.sd-unit  { font-size:10px; color:var(--ink-m); margin-left:2px; }
.sd-sub   { font-size:10px; color:var(--ink-m); margin-top:2px; }
.wind-danger { color:var(--red); } .wind-warn { color:var(--amber); }
.wind-ok { color:var(--green); } .wind-low { color:var(--blue); }
/* Detail rows */
.sc-details { padding:9px 16px; display:grid; grid-template-columns:1fr 1fr;
               gap:5px; border-bottom:1px solid var(--border); }
.detail-row { display:flex; align-items:center; gap:6px; font-size:11px; }
.dr-icon  { width:16px; text-align:center; font-size:12px; flex-shrink:0; }
.dr-label { color:var(--ink-m); width:78px; flex-shrink:0; font-size:10.5px; }
.dr-val   { color:var(--ink); font-weight:500; font-family:'DM Mono',monospace; font-size:11.5px; }
.dr-note  { color:var(--ink-m); margin-left:3px; font-size:10px; }
/* Shipping impact */
.sc-impact { padding:10px 16px; background:var(--sf-soft); }
.si-title  { font-size:9.5px; font-weight:700; letter-spacing:0.10em;
              text-transform:uppercase; color:var(--ink-m); margin-bottom:7px; }
.si-routes { display:flex; flex-direction:column; gap:4px; }
.si-route  { display:flex; align-items:center; gap:8px; font-size:11px; }
.si-dist-bar  { flex:1; height:4px; background:var(--sf-mid); border-radius:2px; overflow:hidden; }
.si-dist-fill { height:100%; border-radius:2px; }
.si-route-name { width:110px; flex-shrink:0; color:var(--ink-2); }
.si-dist-txt { width:58px; text-align:right; font-family:'DM Mono',monospace;
                font-size:11.5px; font-weight:500; flex-shrink:0; }
.si-dist-danger { color:var(--red); } .si-dist-warn { color:var(--amber); }
.si-dist-ok { color:var(--ink-m); }
/* Track SVG */
.sc-track { padding:10px 16px; border-top:1px solid var(--border); }
.track-title { font-size:9.5px; font-weight:700; letter-spacing:0.10em;
               text-transform:uppercase; color:var(--ink-m); margin-bottom:5px; }
/* Summary table */
.tcard { border:1px solid var(--border); margin-bottom:1.8rem; }
.tc-head { padding:9px 14px; background:var(--sf-soft);
            border-bottom:1.5px solid var(--border-s);
            display:flex; align-items:center; justify-content:space-between; }
.tc-title { font-size:10px; font-weight:600; letter-spacing:0.12em;
             text-transform:uppercase; color:var(--ink-m); }
.tc-badge { font-family:'DM Mono',monospace; font-size:10px;
             color:var(--ink-m); letter-spacing:0.04em; }
table { width:100%; border-collapse:collapse; font-size:12px; }
thead tr { background:var(--sf-soft); border-bottom:1.5px solid var(--border-s); }
thead th { padding:8px 12px; font-size:9.5px; font-weight:600; letter-spacing:0.10em;
            text-transform:uppercase; color:var(--ink-m); text-align:right; }
thead th:first-child { text-align:left; }
tbody tr { border-bottom:1px solid var(--border); }
tbody tr:last-child { border-bottom:none; }
tbody tr:hover { background:var(--sf-soft); }
tbody tr.danger-row { background:#fff8f8; }
td { padding:7px 12px; text-align:right; color:var(--ink-2); vertical-align:middle; }
td:first-child { text-align:left; font-weight:600; color:var(--ink); }
.impact-pill { font-size:9px; font-weight:700; padding:2px 8px; border-radius:2px;
               display:inline-block; letter-spacing:0.05em; text-transform:uppercase; }
.ip-danger  { background:var(--red-l);   color:var(--red);   border:1px solid #e8b4b0; }
.ip-warn    { background:var(--amber-l); color:var(--amber); border:1px solid #e8c990; }
.ip-watch   { background:var(--blue-l);  color:var(--blue);  border:1px solid #b0cce4; }
.ip-monitor { background:var(--sf-mid);  color:var(--ink-m); border:1px solid var(--border); }
/* Footer */
.footer { border-top:1.5px solid var(--border-s); padding:1.1rem 2.8rem;
          display:flex; justify-content:space-between; align-items:center;
          background:var(--sf-soft); }
.footer .f-left  { font-size:10.5px; color:var(--ink-m); line-height:1.6; }
.footer .f-left strong { color:var(--ink-2); font-weight:600; }
.footer .f-right { font-family:'DM Mono',monospace; font-size:10px;
                   color:var(--ink-m); text-align:right; letter-spacing:0.05em; }
@media(max-width:700px){
  .storms-grid,.sc-data{grid-template-columns:1fr;}
  .sc-details{grid-template-columns:1fr;}
  .body{padding:1.5rem;}
}
</style>
</head>
<body>
<div class="page">

<div class="header">
  <div class="header-top">
    <div>
      <div class="doc-label">Tropical Cyclone Warning System · Global Monitor</div>
      <div class="doc-title">🌀 全球热带气旋预警报告<br><em>Global Tropical Cyclone Alert</em></div>
    </div>
    <div class="doc-meta">
      <div class="meta-date">{{ generated_at }} UTC</div>
      <div class="meta-ref">{{ brand }} · 西太平洋 / 印度洋 / 大西洋</div>
    </div>
  </div>
  <div class="status-strip">
    {%- for cls, text in stat_badges %}
    <span class="sb {{ 'sb-r' if cls=='hst-r' else ('sb-o' if cls=='hst-o' else ('sb-b' if cls=='hst-b' else 'sb-g')) }}">{{ text }}</span>
    {%- endfor %}
  </div>
</div>

<div class="body">

{%- if not storms %}
<!-- 无活跃气旋 -->
<div class="section-head">
  <span class="section-num">01</span>
  <span class="section-label">系统状态</span>
  <span class="section-line"></span>
</div>
<div class="no-storm">
  <div class="no-storm-icon">🌊</div>
  <div class="no-storm-title">当前无活跃热带气旋</div>
  <div class="no-storm-text">
    全球各大洋盆地目前未监测到达到热带气旋强度的活跃系统。
    监测范围覆盖西太平洋、印度洋（北部/南部）、大西洋及南太平洋。
    下次更新：6 小时后。
  </div>
</div>
{%- else %}
<!-- 预警横幅 -->
{%- if alert_storms %}
<div class="section-head">
  <span class="section-num">01</span>
  <span class="section-label">重点预警</span>
  <span class="section-line"></span>
</div>
<div class="alert-bars">
  {%- for s in alert_storms %}
  <div class="abar abar-{{ 'danger' if s.impact=='danger' else 'warn' }}">
    <div class="abar-icon">{{ '🚨' if s.impact=='danger' else '⚠️' }}</div>
    <div>
      <div class="abar-title">{{ s.intensity.name }} {{ s.name }} · {{ s.basin_name }}</div>
      <div class="abar-text">
        风速 {{ s.wind_kn|int if s.wind_kn else '—' }}kn · 气压 {{ s.pres_mb|int if s.pres_mb else '—' }}hPa ·
        距最近航线 {{ s.nearest_route_name or '—' }} {{ s.nearest_route_dist|int if s.nearest_route_dist else '—' }}nm
      </div>
    </div>
  </div>
  {%- endfor %}
</div>
{%- endif %}

<!-- 气旋详情 -->
<div class="section-head">
  <span class="section-num">{{ '02' if alert_storms else '01' }}</span>
  <span class="section-label">活跃气旋系统详情</span>
  <span class="section-line"></span>
</div>

<div class="storms-grid">
{%- for s in storms %}
{%- set ic = s.intensity %}
<div class="scard impact-{{ s.impact }}">
  <div class="sc-head">
    <div class="sc-head-left">
      <div class="sc-intensity-badge" style="background:{{ ic.color }};letter-spacing:0.04em;">
        {{ ic.abbr }}
      </div>
      <div>
        <div class="sc-name">{{ s.name }}</div>
        <div class="sc-sub">{{ s.basin_name }} · {{ s.sid }} · 最后更新 {{ s.last_update or '—' }} UTC</div>
      </div>
    </div>
    <span class="sc-impact-tag sit-{{ s.impact }}">
      {{ '🚨 危险' if s.impact=='danger' else ('⚠️ 预警' if s.impact=='warn' else ('👁 关注' if s.impact=='watch' else '📡 监测')) }}
    </span>
  </div>

  <div class="sc-data">
    <div class="sd-cell">
      <div class="sd-label">最大风速</div>
      <div class="sd-val wind-{{ 'danger' if (s.wind_kn or 0)>=64 else ('warn' if (s.wind_kn or 0)>=48 else ('ok' if (s.wind_kn or 0)>=34 else 'low')) }}">
        {{ s.wind_kn|int if s.wind_kn else '—' }}<span class="sd-unit">kn</span>
      </div>
      <div class="sd-sub">{{ ic.name }}</div>
    </div>
    <div class="sd-cell">
      <div class="sd-label">中心气压</div>
      <div class="sd-val">{{ s.pres_mb|int if s.pres_mb else '—' }}<span class="sd-unit">hPa</span></div>
      <div class="sd-sub">{{ '热带低压' if (s.pres_mb or 1020) > 1000 else '气压较低' }}</div>
    </div>
    <div class="sd-cell">
      <div class="sd-label">当前位置</div>
      <div class="sd-val" style="font-size:15px;">
        {{ '%.1f'|format(s.lat|abs) }}°{{ 'N' if s.lat>=0 else 'S' }}
      </div>
      <div class="sd-sub">{{ '%.1f'|format(s.lon|abs) }}°{{ 'E' if s.lon>=0 else 'W' }}</div>
    </div>
    <div class="sd-cell">
      <div class="sd-label">距最近海岸</div>
      <div class="sd-val" style="font-size:15px;">
        {{ s.dist2land_nm|int if s.dist2land_nm else '—' }}<span class="sd-unit">nm</span>
      </div>
      <div class="sd-sub">{{ '在海上' if (s.dist2land_nm or 0)>50 else '近海' }}</div>
    </div>
  </div>

  <div class="sc-details">
    <div class="detail-row">
      <span class="dr-icon">🧭</span>
      <span class="dr-label">移动方向/速</span>
      <span class="dr-val">{{ s.mov_dir or '—' }} / {{ s.mov_speed or '—' }} kn</span>
    </div>
    <div class="detail-row">
      <span class="dr-icon">🌪</span>
      <span class="dr-label">34kn风圈</span>
      <span class="dr-val">{{ s.r34_avg_nm or '—' }} nm<span class="dr-note">均值</span></span>
    </div>
    <div class="detail-row">
      <span class="dr-icon">🌪</span>
      <span class="dr-label">50kn风圈</span>
      <span class="dr-val">{{ s.r50_avg_nm or '—' }} nm<span class="dr-note">均值</span></span>
    </div>
    <div class="detail-row">
      <span class="dr-icon">🌪</span>
      <span class="dr-label">64kn风圈</span>
      <span class="dr-val">{{ s.r64_avg_nm or '—' }} nm<span class="dr-note">均值</span></span>
    </div>
    <div class="detail-row">
      <span class="dr-icon">🌍</span>
      <span class="dr-label">所在海盆</span>
      <span class="dr-val">{{ s.basin_name }}</span>
    </div>
    <div class="detail-row">
      <span class="dr-icon">⚠️</span>
      <span class="dr-label">最近航线</span>
      <span class="dr-val" style="color:{{ '#c0392b' if s.nearest_route_dist<=300 else ('#b86c0a' if s.nearest_route_dist<=500 else 'inherit') }};">
        {{ s.nearest_route_name or '—' }} {{ s.nearest_route_dist|int if s.nearest_route_dist else '' }} nm
      </span>
    </div>
  </div>

  <div class="sc-impact">
    <div class="si-title">🚢 主要航运航线影响（距离）</div>
    <div class="si-routes">
      {%- for dist, rname in s.nearby_routes[:5] %}
      <div class="si-route">
        <span class="si-route-name">{{ rname }}</span>
        <div class="si-dist-bar">
          <div class="si-dist-fill" style="width:{{ [100-(dist/20),5]|max|int }}%;
            background:{{ '#c0392b' if dist<=300 else ('#b86c0a' if dist<=500 else '#2d5f96') }};"></div>
        </div>
        <span class="si-dist-txt {{ 'si-dist-danger' if dist<=300 else ('si-dist-warn' if dist<=500 else 'si-dist-ok') }}">
          {{ dist|int }} nm
        </span>
      </div>
      {%- endfor %}
    </div>
  </div>

  {%- if s.track_svg %}
  <div class="sc-track">
    <div class="track-title">历史轨迹 &amp; 预报路径 &amp; 风圈范围</div>
    {{ s.track_svg|safe }}
  </div>
  {%- endif %}
</div>
{%- endfor %}
</div>

<!-- 全球活跃系统汇总 -->
<div class="section-head">
  <span class="section-num">{{ '03' if alert_storms else '02' }}</span>
  <span class="section-label">全球活跃系统汇总</span>
  <span class="section-line"></span>
</div>

<div class="tcard">
  <div class="tc-head">
    <span class="tc-title">Active Systems Summary</span>
    <span class="tc-badge">{{ generated_at }} UTC</span>
  </div>
  <table>
    <thead>
      <tr>
        <th>系统名称</th>
        <th style="text-align:right;">海盆</th>
        <th style="text-align:right;">风速(kn)</th>
        <th style="text-align:right;">气压(hPa)</th>
        <th style="text-align:right;">纬度</th>
        <th style="text-align:right;">经度</th>
        <th style="text-align:right;">最近航线(nm)</th>
        <th style="text-align:right;">影响等级</th>
      </tr>
    </thead>
    <tbody>
      {%- for s in storms %}
      <tr {{ 'class="danger-row"' if s.impact=='danger' }}>
        <td>{{ s.intensity.abbr }} {{ s.name }}</td>
        <td style="text-align:right;color:var(--ink-m);font-size:11px;">{{ s.basin_name }}</td>
        <td style="text-align:right;font-family:'DM Mono',monospace;font-size:12px;font-weight:500;
          color:{{ '#c0392b' if (s.wind_kn or 0)>=64 else ('#b86c0a' if (s.wind_kn or 0)>=48 else 'inherit') }};">
          {{ s.wind_kn|int if s.wind_kn else '—' }}
        </td>
        <td style="text-align:right;font-family:'DM Mono',monospace;font-size:12px;">
          {{ s.pres_mb|int if s.pres_mb else '—' }}
        </td>
        <td style="text-align:right;font-family:'DM Mono',monospace;font-size:12px;">
          {{ '%.1f'|format(s.lat) }}°{{ 'N' if s.lat>=0 else 'S' }}
        </td>
        <td style="text-align:right;font-family:'DM Mono',monospace;font-size:12px;">
          {{ '%.1f'|format(s.lon|abs) }}°{{ 'E' if s.lon>=0 else 'W' }}
        </td>
        <td style="text-align:right;font-family:'DM Mono',monospace;font-size:12px;
          color:{{ '#c0392b' if (s.nearest_route_dist or 9999)<=300 else ('#b86c0a' if (s.nearest_route_dist or 9999)<=500 else 'inherit') }};">
          {{ s.nearest_route_dist|int if s.nearest_route_dist else '—' }}
        </td>
        <td style="text-align:right;">
          <span class="impact-pill ip-{{ s.impact }}">
            {{ '危险' if s.impact=='danger' else ('预警' if s.impact=='warn' else ('关注' if s.impact=='watch' else '监测')) }}
          </span>
        </td>
      </tr>
      {%- endfor %}
    </tbody>
  </table>
</div>
{%- endif %}

</div><!-- /body -->

<div class="footer">
  <div class="f-left">
    <strong>{{ brand }}</strong> 气象导航服务 · 仅供参考，以官方气象机构公告为准
  </div>
  <div class="f-right">
    CYCLONE ALERT REPORT<br>
    {{ brand }} · {{ generated_at }} UTC
  </div>
</div>

</div><!-- /page -->
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

    # 为每个风暴生成轨迹 SVG
    for s in storms:
        try:
            s["track_svg"] = generate_track_svg(s)
        except Exception as e:
            log.warning(f"轨迹SVG生成失败 {s.get('name')}: {e}")
            s["track_svg"] = ""

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
        page = browser.new_page(viewport={"width": 1080, "height": 900}, device_scale_factor=2)
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
        f"<font color=\"comment\">数据时间：{generated_at} UTC · 全球实时气象监测</font>",
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
            f"| 50kn风圈 | {s['r50_avg_nm'] or '—'} nm（均值） |",
            f"| 64kn风圈 | {s['r64_avg_nm'] or '—'} nm（均值） |",
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
                f"<font color=\"comment\">全球实时气象监测 · {generated_at} UTC · {Config.BRAND}</font>"
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

    # 1. 截图 → PDF 推送
    if html_path:
        try:
            img_bytes = html_to_image(html_path)
            # ── 图片消息（暂时关闭，后续可能恢复） ──
            # b64 = base64.b64encode(img_bytes).decode()
            # md5 = hashlib.md5(img_bytes).hexdigest()
            # result = _post({"msgtype": "image", "image": {"base64": b64, "md5": md5}})
            # if result.get("errcode") == 0:
            #     log.info("✅ 截图推送成功")
            # else:
            #     log.warning(f"截图推送失败: {result}")

            # ── PDF 转换 + 推送 ──
            try:
                from utils import convert_and_push_pdf
                import datetime as _dt
                _today = _dt.datetime.now().strftime("%Y-%m-%d")
                convert_and_push_pdf(img_bytes, Config.WECOM_WEBHOOK,
                                     "台风路径追踪日报", _today)
            except Exception as pdf_e:
                log.warning(f"PDF 推送失败（不影响主流程）: {pdf_e}")
        except Exception as e:
            log.warning(f"截图失败: {e}")

    # 2. 文字 markdown（暂时关闭，后续可能恢复）
    # try:
    #     payload = (build_wecom_card_storms(storms, generated_at)
    #                if storms else build_wecom_card_no_storm(generated_at))
    #     result  = _post(payload)
    #     if result.get("errcode") == 0:
    #         log.info("✅ 文字推送成功")
    #     else:
    #         log.error(f"文字推送失败: {result}")
    # except Exception as e:
    #     log.error(f"推送异常: {e}")

    return True


# ══════════════════════════════════════════════════════════════════════════════
# 8. 主流程
# ══════════════════════════════════════════════════════════════════════════════

def run_once(force: bool = False, check_only: bool = False) -> bool:
    log.info("=" * 60)
    log.info(f"{Config.BRAND} 热带气旋预警 — 开始执行")
    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")

    # ── 三层数据融合 ─────────────────────────────────────────────────────────
    # Layer 1: NAVGreen（最实时位置 + 完整轨迹）
    ng_storms = fetch_navgreen_storms()

    # Layer 2: IBTrACS NRT（全球覆盖 + SSHS/R34/气压）
    ibt_storms = fetch_ibtracs()

    # Layer 3: NHC 详细信息（大西洋/东太 补充）
    if any(s.get("basin") in ("NA", "EP", "CP") for s in ibt_storms):
        fetch_nhc_details()   # 可扩展匹配，当前仅记录日志

    # 融合：NAVGreen 实时位置 + IBTrACS 详情
    storms = merge_storm_data(ng_storms, ibt_storms)

    log.info(f"融合后共 {len(storms)} 个活跃系统 "
             f"(NAVGreen:{len(ng_storms)} IBTrACS:{len(ibt_storms)})")

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
