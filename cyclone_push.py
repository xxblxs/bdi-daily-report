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


def _r34_to_px(r34_nm: float, bbox: list, W: int) -> float:
    """34kn风圈半径（海里）→ SVG像素大小"""
    if not r34_nm:
        return 0
    lon_span = bbox[2] - bbox[0]
    px_per_deg = W / lon_span
    nm_per_deg = 60.0
    return r34_nm / nm_per_deg * px_per_deg


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
                           "r34": p.get("r34_avg")})
        except Exception:
            continue

    if len(track) < 2:
        # 至少用当前位置造一个点
        track = [{"lat": cur_lat, "lon": cur_lon,
                  "wind": storm.get("wind_kn"), "time": "", "r34": None}]

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

    # ── 绘制34kn风圈（最新位置）─────────────────────────────────────────────────
    latest = track[-1]
    r34_nm = latest.get("r34")
    cur_x, cur_y = sv(latest["lat"], latest["lon"])
    if r34_nm:
        r34_px = _r34_to_px(float(r34_nm), bbox, W)
        lines.append(
            f'<ellipse cx="{cur_x}" cy="{cur_y}" rx="{r34_px}" ry="{r34_px*0.8}" '
            f'fill="rgba(226,75,74,0.12)" stroke="rgba(226,75,74,0.35)" '
            f'stroke-width="1" stroke-dasharray="4,3"/>'
        )

    # ── 绘制轨迹线（颜色按风速渐变）─────────────────────────────────────────────
    for i in range(len(track) - 1):
        p1, p2 = track[i], track[i+1]
        x1, y1 = sv(p1["lat"], p1["lon"])
        x2, y2 = sv(p2["lat"], p2["lon"])
        color = _wind_to_color(p2.get("wind") or 0)
        lines.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{color}" stroke-width="2.5" stroke-linecap="round" opacity="0.9"/>'
        )

    # ── 绘制轨迹点（每隔2个画一个，节省空间）──────────────────────────────────────
    for i, p in enumerate(track):
        x, y = sv(p["lat"], p["lon"])
        wind = p.get("wind") or 0
        color = _wind_to_color(wind)
        r = 3 if i < len(track) - 1 else 0  # 非当前位置小圆点
        if i % 2 == 0 and i < len(track) - 1:
            lines.append(
                f'<circle cx="{x}" cy="{y}" r="{r}" '
                f'fill="{color}" opacity="0.8"/>'
            )

    # ── 绘制预报轨迹（虚线，来自 NAVGreen forecast[]）────────────────────────────
    forecast_pts = storm.get("forecasts", [])
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

        for i in range(len(fc_track) - 1):
            p1, p2 = fc_track[i], fc_track[i+1]
            x1, y1 = sv(p1["lat"], p1["lon"])
            x2, y2 = sv(p2["lat"], p2["lon"])
            fc_color = _wind_to_color(p2.get("wind") or 0)
            lines.append(
                f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                f'stroke="{fc_color}" stroke-width="2.5" stroke-linecap="round" '
                f'stroke-dasharray="7,5" opacity="0.7"/>'
            )
            mx, my = sv(p2["lat"], p2["lon"])
            lines.append(
                f'<polygon points="{mx},{my-5} {mx+4},{my} {mx},{my+5} {mx-4},{my}" '
                f'fill="{fc_color}" opacity="0.75"/>'
            )

    # ── 当前位置：脉冲动画大圆 ─────────────────────────────────────────────────
    cur_wind  = latest.get("wind") or storm.get("wind_kn") or 0
    cur_color = _wind_to_color(float(cur_wind))
    lines += [
        # 外圈脉冲
        f'<circle cx="{cur_x}" cy="{cur_y}" r="16" fill="none" '
        f'stroke="{cur_color}" stroke-width="1.5" opacity="0.4">',
        f'  <animate attributeName="r" values="14;22;14" dur="2.5s" repeatCount="indefinite"/>',
        f'  <animate attributeName="opacity" values="0.4;0;0.4" dur="2.5s" repeatCount="indefinite"/>',
        f'</circle>',
        # 中圈
        f'<circle cx="{cur_x}" cy="{cur_y}" r="8" fill="{cur_color}" opacity="0.35"/>',
        # 实心中心
        f'<circle cx="{cur_x}" cy="{cur_y}" r="5" fill="{cur_color}" '
        f'stroke="white" stroke-width="1.5" filter="url(#glow)"/>',
        # 台风符号 🌀 (用文字)
        f'<text x="{cur_x}" y="{cur_y - 13}" text-anchor="middle" '
        f'font-size="14" fill="{cur_color}" filter="url(#glow)">🌀</text>',
    ]

    # ── 风暴名标签 ─────────────────────────────────────────────────────────────
    name     = storm.get("name", "")
    abbr     = storm.get("intensity", {}).get("abbr", "")
    wind_kn  = storm.get("wind_kn")
    label_x  = min(W - 10, max(10, cur_x + 12))
    label_y  = max(20, cur_y - 8)
    lines += [
        f'<rect x="{label_x-4}" y="{label_y-12}" width="{len(name)*7+55}" height="28" '
        f'rx="4" fill="rgba(0,0,0,0.65)" stroke="{cur_color}" stroke-width="1"/>',
        f'<text x="{label_x}" y="{label_y}" fill="white" font-size="11" font-weight="700">'
        f'{abbr} {name}</text>',
        f'<text x="{label_x}" y="{label_y+12}" fill="{cur_color}" font-size="10">'
        f'{int(wind_kn) if wind_kn else "—"}kn · {storm.get("intensity",{}).get("name","")}</text>',
    ]

    # ── 图例 ──────────────────────────────────────────────────────────────────
    legend_items = [
        ("#378ADD", "热带低压 <34kn"),
        ("#1D9E75", "热带风暴 34-48kn"),
        ("#ef9f27", "强热带风暴 48-64kn"),
        ("#e24b4a", "台风/飓风 64-96kn"),
        ("#7b241c", "超强台风 ≥114kn"),
    ]
    lx, ly = 6, H - 8 - len(legend_items) * 13
    lines.append(f'<rect x="4" y="{ly-4}" width="150" height="{len(legend_items)*13+8}" '
                 f'rx="4" fill="rgba(0,0,0,0.5)"/>')
    for j, (lcolor, ltext) in enumerate(legend_items):
        yl = ly + j * 13
        lines.append(f'<rect x="{lx}" y="{yl}" width="10" height="4" '
                     f'rx="2" fill="{lcolor}"/>')
        lines.append(f'<text x="{lx+14}" y="{yl+4}" fill="rgba(255,255,255,0.7)" '
                     f'font-size="8">{ltext}</text>')

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
    <div class="h-date">{{ generated_at }} UTC · 全球多源气象数据融合 · 实时监测</div>
    <div class="h-title"><img src="data:image/png;base64,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/PGuBVt945vfedtll35j19/+7vdrDTVQ1wMkAQIJbCPEKGNAIBBGLshJliGmTRlg222f9YM3vP6fv/U3f7PN5wcmx++hEDKkkCuQMObRMSAgEInVYleka75w7iWn/ttnznlLVXcRYoRtRomGyZ2mueD8k1+3xWYbfCNsZLAYlkCwbEIWkKRMquY73//Jfm9/+5GnUM8ACxuMwUYSI5QL8kuzT9h5qy03+rLcIoSosBJIpCQzWDSY0w465EPf+vFP5jw/VZNRACEEFIShbXn9a1/+3aOOPOCVncjFICYiiUdLDkZYbbcgfvjjX+x/4EEfPLbnSWQK3AUMMnKgTNzO58gjDzp5px1ecUClZHxicVNm7PUv75l39S/nUuiAGJbIgaNgFrHJRmvPufD8z85abcokwgVJQACmb+WlUgp9KzbbTERKhMAVsigkreGznzv/1BtvumMdZ4XV4ihAhVwTSmZMn8K7DnnzTt2KHhMwASpAizAgcAWusWD+okVPmzPnd8/4/g9/su/PrvrNS2688ba15t29iFKMnUhCEqNsY5uqNtNnTGGzTZ/2s799ybY3vuRF216w2SYbfK2utSgwYYGNMCAsMIFRV7gBM0oS9xNGjBBGJDIYg4QxKWEHCxcu3vI311z/3O99/z93+tlVv9pk7vU3bXP3PYvJYiQhCUmMMMNSIDGpW7HeumuwxRYb/uBvnr/N95//vGdfttFG6/9kUl0VSFAPkSzhCrnGYph5dJKlapZqkUS64sbf3/r6Y0/+wp6dThcDoQCB06QTSst668y4++0H7r13pxYC5MBKoAAVEzMgcIBMusUECxY2637ikycft3ConRRRUVUViqCKQBHYycwZ03oHH7DXTp0OBIlTiMBKoAAtRtx62z2bnHPuV44eHBIp4SiYYRaiIEwYtn/tK37wzK03/4wYBHcBIYnlSQRKyKrXNbBgYTvj9DO++Im75s1fp6QaI1AC7mKQTWkbtvqrjW7dc483vLWWWibQZJl65llfPveGG28nXYELFuAKyZiGtWdOv/mtb9nzwKmTuwQJiL6Vn0op9K3YbDMhGSGgQoakkE6silJEyDharIKpkGtwoVJCJlV0mYgRwoABgwwWApKEEJkC1QwN9ja6554FU26/415uvvmWQ+64485J9967gIULFyGZgYHJTJ06lZlrrcVTnrLWDzfYcO0f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" alt="ISOWAY" style="height:28px;vertical-align:middle;margin-right:8px;"> 全球热带气旋预警报告</div>
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
    ISOWAY 气象导航系统将持续每 6 小时轮询全球台风数据，<br>
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
  <!-- 轨迹地图 -->
  {% if s.track_svg %}
  <div style="padding:12px 14px;border-top:1px solid #f0f2f8;">
    <div style="font-size:10px;font-weight:600;color:#8a9db5;text-transform:uppercase;
                letter-spacing:.05em;margin-bottom:8px;">📍 历史轨迹 & 当前位置</div>
    {{ s.track_svg|safe }}
  </div>
  {% endif %}
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
    &nbsp;气象导航 · 全球实时气象监测 · 预报路径为虚线
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
