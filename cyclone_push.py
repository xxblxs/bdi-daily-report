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
    <div class="h-title"><img src="data:image/png;base64,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" alt="ISOWAY" style="height:28px;vertical-align:middle;margin-right:8px;"> 全球热带气旋预警报告</div>
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
