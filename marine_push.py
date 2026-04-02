"""
ISOWAY 全球航线海况日报 — 自动化推送脚本
==========================================
功能：
  1. 从 Open-Meteo Marine API 拉取全球 11 个关键航区实时海况数据
     （NOAA GFS Wave 0.25° + ECMWF WAM，完全免费，无需 API Key）
  2. 自动计算各航区适航评级（适航 / 关注 / 需注意）
  3. 生成完整 HTML 海况日报（长截图报告）
  4. 推送到企业微信群机器人（长截图 + 文字 markdown 各一条）

依赖安装：
  pip install requests jinja2 python-dotenv playwright
  playwright install chromium

配置方式：
  复制 .env.example → .env，填写 WECOM_WEBHOOK 即可运行

运行：
  python marine_push.py              # 立即执行一次
  python marine_push.py --schedule   # 定时执行（需 pip install schedule）

推送频率建议：一周两次（周一 + 周四），在 GitHub Actions 里单独配置
"""

import os
import sys
import math
import json
import base64
import hashlib
import logging
import datetime
import argparse
import time as _time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

try:
    from jinja2 import Template
    HAS_JINJA2 = True
except ImportError:
    HAS_JINJA2 = False
    print("⚠️  jinja2 未安装。运行: pip install jinja2")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── 日志 ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("marine_report")


# ══════════════════════════════════════════════════════════════════════════════
# 1. 配置区
# ══════════════════════════════════════════════════════════════════════════════

class Config:
    # 企业微信 webhook（必填）
    WECOM_WEBHOOK = os.getenv("WECOM_WEBHOOK", "")

    # 报告输出目录
    OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./reports"))

    # Open-Meteo API（免费，无需 Key）
    MARINE_API  = "https://marine-api.open-meteo.com/v1/marine"
    WEATHER_API = "https://api.open-meteo.com/v1/forecast"

    # 请求超时（秒）
    TIMEOUT = 15

    # 品牌名称
    BRAND = "ISOWAY"

    # ── 11 个关键航区定义 ─────────────────────────────────────────────────────
    # 说明：马六甲、黑海等内陆浅水区 GFS Wave 无数据，改用附近开阔海域坐标
    ROUTES = [
        # (中文名称, 英文标识, 纬度, 经度, 区域分组, 相关干散货航线说明)
        ("南海中部",     "SCS-N",  15.0,   115.0, "亚太",  "西澳/巴西→中国 主干道"),
        ("南海南部",     "SCS-S",   5.0,   109.0, "亚太",  "新加坡→中国途径"),
        ("西太平洋",     "WPAC",   30.0,   155.0, "亚太",  "日本/韩国→北美"),
        ("澳大利亚西北", "AUNW",  -20.0,   113.0, "亚太",  "Port Hedland 铁矿石出口"),
        ("北印度洋",     "NIO",    15.0,    68.0, "印度洋", "波斯湾→中国/印度"),
        ("亚丁湾",       "ADEN",   12.0,    50.0, "印度洋", "欧洲↔亚洲 苏伊士走廊"),
        ("红海南部",     "RSEA",   14.0,    42.0, "印度洋", "苏伊士运河南端"),
        ("地中海西部",   "WMED",   37.0,     5.0, "欧洲",  "地中海↔大西洋"),
        ("北大西洋中部", "NATL",   45.0,   -35.0, "欧洲",  "欧洲↔北美 主力航线"),
        ("好望角海域",   "CAPE",  -37.0,    20.0, "非洲",  "欧亚绕好望角 重要节点"),
        ("巴西东海岸",   "BRAZ",  -23.0,   -40.0, "美洲",  "Santos 铁矿/大豆出口"),
    ]


# ══════════════════════════════════════════════════════════════════════════════
# 2. 数据获取
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_one_route(route: tuple) -> dict:
    """拉取单个航区的海浪数据和风速数据，返回聚合结果字典。"""
    name, code, lat, lon, region, route_note = route

    marine_params = (
        "wave_height,wave_direction,wave_period,"
        "wind_wave_height,wind_wave_direction,wind_wave_period,"
        "swell_wave_height,swell_wave_direction,swell_wave_period"
    )
    daily_params = (
        "wave_height_max,wave_period_max,"
        "swell_wave_height_max,wind_wave_height_max"
    )
    wind_params = "wind_speed_10m,wind_direction_10m,wind_gusts_10m"

    base = {
        "name": name, "code": code, "lat": lat, "lon": lon,
        "region": region, "route_note": route_note,
        "error": None,
    }

    try:
        # ── 海浪数据 ──────────────────────────────────────────────────────────
        mr = requests.get(
            Config.MARINE_API,
            params={"latitude": lat, "longitude": lon,
                    "hourly": marine_params,
                    "daily": daily_params,
                    "forecast_days": 5, "timezone": "UTC"},
            timeout=Config.TIMEOUT,
        )
        mr.raise_for_status()
        md = mr.json()

        h = md.get("hourly", {})
        d = md.get("daily",  {})

        # ── 风速数据 ──────────────────────────────────────────────────────────
        wr = requests.get(
            Config.WEATHER_API,
            params={"latitude": lat, "longitude": lon,
                    "hourly": wind_params,
                    "forecast_days": 1, "timezone": "UTC",
                    "wind_speed_unit": "kn"},
            timeout=Config.TIMEOUT,
        )
        wr.raise_for_status()
        wh = wr.json().get("hourly", {})

        # 当前值取索引 0（UTC 00:00），12小时内最大风速
        def safe(arr, idx=0):
            try:
                v = arr[idx]
                return v if v is not None else None
            except (IndexError, TypeError):
                return None

        winds_12h = [v for v in (wh.get("wind_speed_10m") or [])[:12] if v is not None]
        max_wind_12h = max(winds_12h) if winds_12h else None

        # 5日最大波高（日序列）
        f5d_max = d.get("wave_height_max", [None]*5)
        f5d_dates = d.get("time", [])

        base.update({
            # 今日最大值
            "wh_max":  safe(d.get("wave_height_max")),
            "wp_max":  safe(d.get("wave_period_max")),
            "sh_max":  safe(d.get("swell_wave_height_max")),
            "wwh_max": safe(d.get("wind_wave_height_max")),
            # 当前值（UTC 00:00）
            "wh":     safe(h.get("wave_height")),
            "wdir":   safe(h.get("wave_direction")),
            "wper":   safe(h.get("wave_period")),
            "sh":     safe(h.get("swell_wave_height")),
            "sdir":   safe(h.get("swell_wave_direction")),
            "sper":   safe(h.get("swell_wave_period")),
            "wwh":    safe(h.get("wind_wave_height")),
            # 风况
            "wind":   safe(wh.get("wind_speed_10m")),
            "wdir2":  safe(wh.get("wind_direction_10m")),
            "gust":   safe(wh.get("wind_gusts_10m")),
            "wind_max12h": max_wind_12h,
            # 5日预报
            "f5d_max":   [v for v in f5d_max[:5]],
            "f5d_dates": f5d_dates[:5],
        })

    except Exception as e:
        log.warning(f"  {name} 数据拉取失败: {e}")
        base["error"] = str(e)
        # 填充空值
        for k in ("wh_max","wp_max","sh_max","wwh_max","wh","wdir","wper",
                  "sh","sdir","sper","wwh","wind","wdir2","gust","wind_max12h"):
            base[k] = None
        base["f5d_max"] = [None]*5
        base["f5d_dates"] = []

    return base


def fetch_marine_data() -> list[dict]:
    """并发拉取所有航区数据，返回列表。"""
    log.info(f"拉取 {len(Config.ROUTES)} 个航区海况数据（并发）...")
    results = [None] * len(Config.ROUTES)

    with ThreadPoolExecutor(max_workers=6) as pool:
        future_to_idx = {
            pool.submit(_fetch_one_route, route): i
            for i, route in enumerate(Config.ROUTES)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                name = Config.ROUTES[idx][0]
                log.error(f"  {name} 并发拉取异常: {e}")
                results[idx] = {
                    "name": name, "code": Config.ROUTES[idx][1],
                    "lat": Config.ROUTES[idx][2], "lon": Config.ROUTES[idx][3],
                    "region": Config.ROUTES[idx][4],
                    "route_note": Config.ROUTES[idx][5],
                    "error": str(e),
                }

    ok  = sum(1 for r in results if r and not r.get("error"))
    log.info(f"✅ 海况数据拉取完成: {ok}/{len(results)} 个航区成功")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# 3. 数据分析 — 评级 / 方向文字 / 观点
# ══════════════════════════════════════════════════════════════════════════════

def _deg_to_compass(deg) -> str:
    """角度转16方位罗盘。"""
    if deg is None:
        return "—"
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return dirs[round(deg / 22.5) % 16]


def _beaufort(kn) -> str:
    """节 → 蒲福风级字符串。"""
    if kn is None:
        return "—"
    scale = [1,3,6,10,16,21,27,33,40,47,55,63]
    for b, limit in enumerate(scale):
        if kn < limit:
            return f"{b}级"
    return "12级"


def _risk_level(route: dict) -> str:
    """
    自动计算适航评级，仅基于真实数据字段：
      - wh_max  今日最大综合波高（米）
      - wind    当前风速（节）
      - sh_max  今日最大涌浪高度（米）
    返回：'high' / 'mod' / 'low' / 'calm'
    """
    wh   = route.get("wh_max") or 0
    wind = route.get("wind")   or 0
    sh   = route.get("sh_max") or 0

    if wh >= 2.5 or wind >= 25 or sh >= 2.0:
        return "high"
    if wh >= 1.5 or wind >= 15 or sh >= 1.2:
        return "mod"
    if wh >= 0.8 or wind >= 8:
        return "low"
    return "calm"


_RISK_LABELS = {
    "high": ("🔴 需注意", "risk-high", "rb-high"),
    "mod":  ("🟡 关注",   "risk-mod",  "rb-mod"),
    "low":  ("🟢 适航",   "risk-low",  "rb-low"),
    "calm": ("🔵 平稳",   "risk-calm", "rb-calm"),
}


def _bar_color(wh_max) -> str:
    if wh_max is None:
        return "#8a9db5"
    if wh_max >= 2.5: return "#e24b4a"
    if wh_max >= 1.5: return "#ef9f27"
    return "#1d9e75"


def _bar_pct(wh_max, scale=3.0) -> int:
    """波高柱状图高度百分比，scale=满格对应波高（米）。"""
    if wh_max is None:
        return 5
    return max(5, min(100, round(wh_max / scale * 100)))


def _fmt(v, decimals=2) -> str:
    if v is None:
        return "—"
    return f"{v:.{decimals}f}"


def _generate_views(routes: list[dict]) -> dict:
    """根据各航区数据生成三大区域文字观点（纯数据驱动）。"""

    def region_routes(name):
        return [r for r in routes if r.get("region") == name and not r.get("error")]

    def avg_wh(rlist):
        vals = [r["wh_max"] for r in rlist if r.get("wh_max") is not None]
        return sum(vals) / len(vals) if vals else 0

    # 亚太
    apac = region_routes("亚太")
    apac_avg = avg_wh(apac)
    apac_max_r = max(apac, key=lambda r: r.get("wh_max") or 0, default={})
    apac_verdict = "整体平稳" if apac_avg < 1.0 else ("需关注局部" if apac_avg < 1.8 else "海况偏差")
    apac_text = (
        f"亚太航区平均波高 {apac_avg:.1f}m，"
        f"最高点位于 {apac_max_r.get('name','—')}（{_fmt(apac_max_r.get('wh_max'),1)}m）。"
        "南海整体适航，西太平洋涌浪周期较长，注意船舶摇摆。"
        f"澳大利亚西北（Port Hedland 附近）波高 {_fmt(next((r.get('wh_max') for r in apac if 'AUNW' in r.get('code','')),None),1)}m，铁矿石装港海况正常。"
    )

    # 印度洋/中东
    io = region_routes("印度洋")
    io_avg = avg_wh(io)
    nio_r  = next((r for r in io if r["code"] == "NIO"), {})
    aden_r = next((r for r in io if r["code"] == "ADEN"), {})
    io_verdict = "季风过渡期" if io_avg >= 1.0 else "总体平稳"
    io_text = (
        f"北印度洋波高 {_fmt(nio_r.get('wh_max'),1)}m，"
        f"风速 {_fmt(nio_r.get('wind'),0)}kn（{_beaufort(nio_r.get('wind'))}）。"
        f"亚丁湾波高 {_fmt(aden_r.get('wh_max'),1)}m，"
        f"风速 {_fmt(aden_r.get('wind'),0)}kn。"
        "苏伊士走廊建议关注红海南部风况，受区域地缘局势影响船舶绕行好望角时需同步参考好望角数据。"
    )

    # 大西洋/欧洲
    eu  = region_routes("欧洲")
    afr = region_routes("非洲")
    ame = region_routes("美洲")
    atl_routes = eu + afr + ame
    atl_avg  = avg_wh(atl_routes)
    cape_r   = next((r for r in afr if r["code"] == "CAPE"), {})
    natl_r   = next((r for r in eu  if r["code"] == "NATL"), {})
    f5d_cape = [v for v in (cape_r.get("f5d_max") or []) if v is not None]
    cape_peak = max(f5d_cape) if f5d_cape else None
    atl_verdict = "好望角需关注" if (cape_r.get("wh_max") or 0) >= 1.5 else "整体可行"
    atl_text = (
        f"好望角海域今日最大波高 {_fmt(cape_r.get('wh_max'),1)}m，"
        f"涌浪 {_fmt(cape_r.get('sh_max'),1)}m / 周期 {_fmt(cape_r.get('wp_max'),0)}s，"
        + (f"5日内峰值预计 {_fmt(cape_peak,1)}m。" if cape_peak else "")
        + f"北大西洋波高 {_fmt(natl_r.get('wh_max'),1)}m，"
        f"阵风 {_fmt(natl_r.get('gust'),0)}kn。"
        "建议过好望角船舶密切关注南大洋低压系统动向，择机过角。"
    )

    return {
        "apac": {"verdict": apac_verdict, "text": apac_text, "direction": "ok" if apac_avg < 1.0 else "warn"},
        "io":   {"verdict": io_verdict,   "text": io_text,   "direction": "ok" if io_avg < 1.0 else "warn"},
        "atl":  {"verdict": atl_verdict,  "text": atl_text,  "direction": "ok" if (cape_r.get("wh_max") or 0) < 1.5 else "warn"},
    }


# ══════════════════════════════════════════════════════════════════════════════
# 4. 航区 SVG 地图生成（纯 Python，不依赖外网）
# ══════════════════════════════════════════════════════════════════════════════

def _merc_y(lat: float) -> float:
    """纬度 → 墨卡托投影 Y 值"""
    import math
    lat_r = math.radians(max(-85.0, min(85.0, lat)))
    return math.log(math.tan(math.pi / 4 + lat_r / 2))


def _latlon_to_xy(lat: float, lon: float, bbox: list, W: int, H: int):
    """经纬度 → SVG 画布坐标"""
    lon_min, lat_min, lon_max, lat_max = bbox
    x = (lon - lon_min) / (lon_max - lon_min) * W
    y_min = _merc_y(lat_min)
    y_max = _merc_y(lat_max)
    y_val = _merc_y(lat)
    y = (1.0 - (y_val - y_min) / (y_max - y_min)) * H
    return round(x, 1), round(y, 1)


def _wh_to_color(wh: float) -> str:
    """波高 → 热力色（蓝→青→绿→黄→橙→红）"""
    if wh is None or wh < 0.5:  return "#378ADD"   # 蓝  平稳
    if wh < 1.0:                 return "#1D9E75"   # 绿  轻浪
    if wh < 1.5:                 return "#5dbb8a"   # 浅绿 小浪
    if wh < 2.5:                 return "#ef9f27"   # 橙  中浪
    if wh < 3.5:                 return "#e24b4a"   # 红  大浪
    return "#7b241c"                                # 暗红 巨浪


# 各航区 bbox [lon_min, lat_min, lon_max, lat_max]
# 各航区精确 bbox [lon_min, lat_min, lon_max, lat_max]
# 收窄 bbox 让海岸线更突出、更易辨认
_ROUTE_BBOX = {
    "SCS-N": [108, 8,  125, 25],   # 南海中部：广东/越南/菲律宾
    "SCS-S": [102, 0,  118, 14],   # 南海南部：马来西亚/婆罗洲
    "WPAC":  [140, 18, 172, 42],   # 西太平洋：日本南部到关岛
    "AUNW":  [108, -26, 122, -12], # 澳大利亚西北：Hedland附近
    "NIO":   [58,  5,  82,  24],   # 北印度洋：印度/斯里兰卡
    "ADEN":  [42,  8,  60,  20],   # 亚丁湾：也门/索马里
    "RSEA":  [34, 10,  46, 24],    # 红海南部：厄立特里亚/吉布提
    "WMED":  [-5, 30,  18, 46],    # 地中海西部：直布罗陀/撒丁岛
    "NATL":  [-50, 33, -15, 58],   # 北大西洋中部：亚速尔群岛附近
    "CAPE":  [8,  -42,  35, -22],  # 好望角：南非西南端
    "BRAZ":  [-50, -32, -25, -8],  # 巴西东海岸：里约/桑托斯
}

# 各航区关键地标标注 [(lat, lon, 名称, 锚点方向), ...]
_LANDMARKS = {
    "SCS-N": [(22.3, 114.1, "香港", "end"),   (10.8, 106.7, "胡志明", "start"),
              (14.1, 121.0, "马尼拉", "start"), (20.0, 110.3, "海口", "middle")],
    "SCS-S": [(1.3,  103.8, "新加坡", "start"), (3.1, 113.0, "古晋", "middle"),
              (5.4,  100.3, "槟城", "end")],
    "WPAC":  [(35.7, 139.7, "东京", "end"),    (26.2, 127.7, "冲绳", "end"),
              (13.4, 144.8, "关岛", "start")],
    "AUNW":  [(-20.3, 118.6, "Hedland", "start"), (-17.9, 122.2, "Broome", "start"),
              (-14.5, 112.0, "↑印度洋", "middle")],
    "NIO":   [(19.1, 72.9, "孟买", "end"),    (6.9, 79.9, "科伦坡", "start"),
              (22.6, 68.0, "坎德拉", "end"),  (11.7, 79.8, "金奈", "start")],
    "ADEN":  [(12.8, 45.0, "亚丁", "end"),    (11.5, 43.1, "吉布提", "start"),
              (15.6, 54.1, "萨拉拉", "start")],
    "RSEA":  [(15.6, 39.5, "马萨瓦", "end"),  (12.6, 43.1, "吉布提", "start"),
              (21.5, 39.2, "吉达", "end")],
    "WMED":  [(36.1, -5.4, "直布罗陀", "start"), (37.5, 12.5, "突尼斯", "start"),
              (43.3,  5.4, "马赛", "middle"),   (38.1,  13.4, "巴勒莫", "middle")],
    "NATL":  [(38.7, -27.2, "亚速尔", "middle"), (51.5, -9.5, "爱尔兰", "end"),
              (38.7, -9.1, "里斯本", "end")],
    "CAPE":  [(-34.4, 18.5, "好望角", "start"), (-33.9, 25.6, "伊丽莎白港", "start"),
              (-29.9, 31.0, "德班", "start")],
    "BRAZ":  [(-23.0, -43.2, "里约", "middle"), (-33.5, -70.7, "桑托斯", "middle"),
              (-8.1, -34.9, "累西腓", "middle")],
}

# 海岸线多边形数据（精细化，各航区专用）
_COASTLINES = [
    # ── 中国/越南/华南海岸（SCS-N、SCS-S关键）
    [(18,109),(20,110),(21,110),(22,114),(22.5,113.5),(22,114),
     (23,117),(24,118),(25,119),(26,120),(28,121),(30,122),
     (32,122),(35,120),(36,120),(38,121),(39,122),(40,122),
     (40,119),(38,117),(36,120),(35,117),(22,113),(20,110),(18,109)],
    # ── 越南东海岸
    [(8,104),(10,104),(12,109),(14,110),(16,108),(18,107),(20,107),(20,106),(16,108),(12,109),(8,104)],
    # ── 马来半岛/新加坡
    [(1.2,104),(2,104),(4,103),(5,101),(6,100),(7,100),(5,101),(3,103),(1.2,104)],
    # ── 婆罗洲北部
    [(3,108),(4,108),(5,115),(6,117),(7,117),(5,116),(4,114),(3,110),(3,108)],
    # ── 菲律宾吕宋岛
    [(14,120),(15,121),(17,122),(18,122),(18,121),(16,120),(14,120)],
    # ── 菲律宾中部
    [(10,123),(11,124),(13,124),(14,123),(12,122),(10,123)],
    # ── 日本九州/本州西部
    [(31,131),(33,131),(34,131),(34,134),(35,135),(35,136),(34,136)],
    # ── 日本本州东部
    [(34,136),(35,137),(36,138),(37,138),(38,141),(39,142),(40,142),
     (41,141),(40,140),(38,140),(36,138),(35,137),(34,136)],
    # ── 日本四国/纪伊半岛
    [(33,132),(33,134),(34,135),(33,134),(33,132)],
    # ── 澳大利亚西北海岸（AUNW精细）
    [(-13,130),(-14,129),(-15,128),(-17,122),(-20,119),(-22,114),
     (-25,114),(-26,113),(-28,114),(-29,115),(-22,113),(-20,119),(-17,122),(-13,130)],
    # ── 印度半岛西海岸
    [(23,68),(22,70),(20,73),(18,73),(16,74),(14,74),(12,75),
     (10,76),(8,77),(8,80),(10,80),(12,80),(14,80),(16,81),(18,84),(20,87),(22,88),(23,87),(22,88),(20,87),(18,73),(22,72),(23,68)],
    # ── 斯里兰卡
    [(10,80),(9,80),(8,81),(7,82),(8,81),(9,81),(10,80)],
    # ── 阿拉伯半岛/亚丁湾南岸
    [(12,44),(13,44),(14,48),(15,50),(16,52),(18,56),(20,58),(22,59),
     (24,57),(22,59),(18,57),(15,50),(12,44)],
    # ── 非洲之角/索马里
    [(12,44),(11,43),(11,41),(10,42),(8,48),(5,48),(5,47),(8,48),(11,41),(12,44)],
    # ── 红海西岸（厄立特里亚/苏丹）
    [(22,37),(20,37),(18,38),(16,39),(15,39),(14,41),(13,42),(12,43),(13,42),(15,39),(18,38),(22,37)],
    # ── 红海东岸（沙特/也门）
    [(24,38),(22,39),(20,40),(18,41),(16,43),(14,43),(15,43),(18,41),(22,39),(24,38)],
    # ── 伊比利亚半岛
    [(44,-9),(43,-9),(42,-9),(40,-9),(38,-9),(37,-9),(36,-6),(36,-5),(37,-2),
     (39,-1),(40,0),(41,1),(42,3),(43,2),(44,3),(44,1),(43,0),(44,-2),(44,-8),(44,-9)],
    # ── 北非海岸（地中海南岸）
    [(35,-5),(35,0),(36,5),(37,10),(37,12),(33,13),(32,14),(31,24),
     (31,30),(30,32),(32,32),(33,35),(36,35),(37,10),(36,5),(35,-5)],
    # ── 意大利/撒丁岛
    [(38,14),(39,15),(40,16),(41,16),(42,15),(43,14),(44,12),(44,13),(43,14),(42,15),(40,15),(38,14)],
    # ── 好望角/南非
    [(-34,26),(-33,26),(-32,29),(-31,30),(-30,31),(-29,32),(-28,33),(-26,33),
     (-25,33),(-22,35),(-20,35),(-18,36),(-15,36),(-26,33),(-29,32),(-32,29),
     (-33,26),(-34,26),(-34,24),(-34,22),(-34,20),(-34,19),(-33,18),(-30,17),
     (-28,16),(-25,15),(-22,14),(-28,16),(-34,19),(-34,26)],
    # ── 南美东海岸（巴西）
    [(-5,-35),(-8,-35),(-10,-37),(-13,-38),(-15,-39),(-20,-40),
     (-23,-43),(-25,-48),(-28,-49),(-30,-51),(-33,-52),(-35,-57),
     (-38,-57),(-35,-57),(-33,-52),(-30,-51),(-23,-43),(-15,-39),(-5,-35)],
]


def generate_route_svg(route: dict, W: int = 320, H: int = 170) -> str:
    """
    为单个航区生成 SVG 波高地图（纯 Python，无外网依赖）
    改进：精确 bbox 让地图更清晰、加粗海岸线、加入地标城市名、波高泡泡更直观
    """
    code = route.get("code", "")
    bbox = _ROUTE_BBOX.get(code, [
        route.get("lon", 0) - 12, route.get("lat", 0) - 10,
        route.get("lon", 0) + 12, route.get("lat", 0) + 10,
    ])
    lon_min, lat_min, lon_max, lat_max = bbox

    def sv(lat, lon):
        return _latlon_to_xy(lat, lon, bbox, W, H)

    wh   = route.get("wh_max") or 0
    wind = route.get("wind")   or 0
    sh   = route.get("sh_max") or 0
    risk = route.get("risk", "calm")
    main_color = _wh_to_color(wh)

    # 背景：统一深海蓝，用细微渐变区分风险
    bg_dark  = {"high": "#150808", "mod": "#110e04", "low": "#04100a", "calm": "#050d14"}.get(risk, "#050d14")
    bg_light = {"high": "#1e0f0f", "mod": "#191408", "low": "#071a10", "calm": "#0a1a26"}.get(risk, "#0a1a26")

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" style="display:block;border-radius:8px;overflow:hidden;">',
        '<defs>',
        f'  <linearGradient id="bg-{code}" x1="0" y1="0" x2="0.3" y2="1">',
        f'    <stop offset="0%" stop-color="{bg_dark}"/>',
        f'    <stop offset="100%" stop-color="{bg_light}"/>',
        f'  </linearGradient>',
        # 波高热力光晕
        f'  <radialGradient id="glow-{code}">',
        f'    <stop offset="0%"   stop-color="{main_color}" stop-opacity="0.50"/>',
        f'    <stop offset="35%"  stop-color="{main_color}" stop-opacity="0.18"/>',
        f'    <stop offset="70%"  stop-color="{main_color}" stop-opacity="0.05"/>',
        f'    <stop offset="100%" stop-color="{main_color}" stop-opacity="0"/>',
        f'  </radialGradient>',
        # 陆地渐变
        f'  <linearGradient id="land-{code}" x1="0" y1="0" x2="0" y2="1">',
        f'    <stop offset="0%"   stop-color="#1d3d5c"/>',
        f'    <stop offset="100%" stop-color="#162e44"/>',
        f'  </linearGradient>',
        '</defs>',
        f'<rect width="{W}" height="{H}" fill="url(#bg-{code})"/>',
    ]

    # ── 经纬度网格（每5度，细密一些）
    step = 5
    for lat in range(int(lat_min // step) * step, int(lat_max // step + 1) * step, step):
        if lat_min - step < lat < lat_max + step:
            try:
                x0, y0 = sv(lat, lon_min)
                x1, _  = sv(lat, lon_max)
                is_10  = (lat % 10 == 0)
                alpha  = "0.12" if is_10 else "0.06"
                lines.append(f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y0}" '
                              f'stroke="rgba(255,255,255,{alpha})" stroke-width="0.5"/>')
                if is_10 and lat_min < lat < lat_max:
                    lbl = f'{abs(lat)}°{"N" if lat >= 0 else "S"}'
                    lines.append(f'<text x="3" y="{y0 - 2}" fill="rgba(255,255,255,0.30)" '
                                 f'font-size="8" font-family="Arial,sans-serif">{lbl}</text>')
            except Exception:
                pass
    for lon in range(int(lon_min // step) * step, int(lon_max // step + 1) * step, step):
        if lon_min - step < lon < lon_max + step:
            try:
                x0, y0 = sv(lat_max, lon)
                _,  y1 = sv(lat_min, lon)
                is_10  = (lon % 10 == 0)
                alpha  = "0.12" if is_10 else "0.06"
                lines.append(f'<line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y1}" '
                              f'stroke="rgba(255,255,255,{alpha})" stroke-width="0.5"/>')
                if is_10 and lon_min < lon < lon_max:
                    lbl = f'{abs(lon)}°{"E" if lon >= 0 else "W"}'
                    lines.append(f'<text x="{x0 + 2}" y="{H - 4}" fill="rgba(255,255,255,0.30)" '
                                 f'font-size="8" font-family="Arial,sans-serif">{lbl}</text>')
            except Exception:
                pass

    # ── 海岸线（加粗 + 高亮边缘，提升辨识度）
    margin_lon = (lon_max - lon_min) * 0.4
    margin_lat = (lat_max - lat_min) * 0.4
    for coast in _COASTLINES:
        pts_in_bbox = [(la, lo) for la, lo in coast
                       if (lon_min - margin_lon <= lo <= lon_max + margin_lon and
                           lat_min - margin_lat <= la <= lat_max + margin_lat)]
        if len(pts_in_bbox) < 2:
            continue
        path_parts = []
        for i, (la, lo) in enumerate(coast):
            try:
                x, y = sv(la, lo)
                path_parts.append(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}")
            except Exception:
                continue
        if len(path_parts) >= 3:
            d = " ".join(path_parts) + " Z"
            # 填充（陆地色）
            lines.append(f'<path d="{d}" fill="url(#land-{code})" opacity="0.90"/>')
            # 海岸线描边（明亮细线，提升辨识）
            lines.append(f'<path d="{d}" fill="none" stroke="#4a8ab5" '
                         f'stroke-width="1.2" opacity="0.85"/>')

    # ── 波高热力光晕（以航区测量坐标为中心）
    r_lat = route.get("lat") or (lat_min + lat_max) / 2
    r_lon = route.get("lon") or (lon_min + lon_max) / 2
    try:
        cx, cy = sv(r_lat, r_lon)
    except Exception:
        cx, cy = W / 2, H / 2

    # 光晕半径随波高变化（0m→小，4m+→占满1/3区域）
    r_glow = min(W, H) * (0.20 + min(wh / 4.5, 1.0) * 0.30)
    lines.append(f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" '
                 f'rx="{r_glow:.1f}" ry="{r_glow * 0.78:.1f}" '
                 f'fill="url(#glow-{code})"/>')

    # ── 中心位置标记（菱形 + 脉冲圆）
    lines += [
        # 外圈（淡色脉冲感）
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="12" fill="none" '
        f'stroke="{main_color}" stroke-width="0.8" opacity="0.30"/>',
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="7" fill="none" '
        f'stroke="{main_color}" stroke-width="1.0" opacity="0.55"/>',
        # 中心实心点
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" '
        f'fill="{main_color}" stroke="white" stroke-width="1.5" opacity="0.95"/>',
    ]

    # ── 地标城市标注
    landmarks = _LANDMARKS.get(code, [])
    for (la, lo, name, anchor) in landmarks:
        if not (lon_min <= lo <= lon_max and lat_min <= la <= lat_max):
            continue
        try:
            lx, ly = sv(la, lo)
            # 小圆点
            lines.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.5" '
                         f'fill="rgba(255,255,255,0.75)"/>')
            # 城市名（带描边提升可读性）
            offset_x = -4 if anchor == "end" else (4 if anchor == "start" else 0)
            lines.append(f'<text x="{lx + offset_x:.1f}" y="{ly - 5:.1f}" '
                         f'text-anchor="{anchor}" font-size="9" font-weight="600" '
                         f'font-family="PingFang SC,Microsoft YaHei,Arial,sans-serif" '
                         f'stroke="#050d14" stroke-width="2.5" paint-order="stroke">'
                         f'{name}</text>')
            lines.append(f'<text x="{lx + offset_x:.1f}" y="{ly - 5:.1f}" '
                         f'text-anchor="{anchor}" font-size="9" font-weight="600" '
                         f'font-family="PingFang SC,Microsoft YaHei,Arial,sans-serif" '
                         f'fill="rgba(255,255,255,0.90)">{name}</text>')
        except Exception:
            continue

    # ── 右上角数据信息框
    wh_txt = f"{wh:.1f}m" if wh > 0 else "—"
    wn_txt = f"{wind:.0f}kn" if wind > 0 else "—"
    sh_txt = f"{sh:.1f}m" if sh > 0 else "—"
    bw, bh = 70, 52
    bx, by = W - bw - 4, 4
    lines += [
        f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="5" '
        f'fill="rgba(5,13,20,0.72)" stroke="{main_color}" stroke-width="1" opacity="0.95"/>',
        # 波高大字
        f'<text x="{bx + bw - 5}" y="{by + 18}" text-anchor="end" '
        f'fill="{main_color}" font-size="18" font-weight="700" '
        f'font-family="Arial,sans-serif">{wh_txt}</text>',
        f'<text x="{bx + bw - 5}" y="{by + 27}" text-anchor="end" '
        f'fill="rgba(255,255,255,0.40)" font-size="7.5" '
        f'font-family="Arial,sans-serif">波高峰值</text>',
        # 风速 / 涌浪
        f'<text x="{bx + 6}" y="{by + 39}" '
        f'fill="rgba(255,255,255,0.72)" font-size="8.5" '
        f'font-family="Arial,sans-serif">风 {wn_txt}</text>',
        f'<text x="{bx + 6}" y="{by + 50}" '
        f'fill="rgba(255,255,255,0.72)" font-size="8.5" '
        f'font-family="Arial,sans-serif">涌 {sh_txt}</text>',
    ]

    # ── 底部色阶条（渐变版，更美观）
    bar_x, bar_y = 6, H - 13
    bar_w, bar_h = W - 80, 5
    # 用线性渐变填充色阶
    lines += [
        '<defs>',
        f'  <linearGradient id="legend-{code}" x1="0" y1="0" x2="1" y2="0">',
        f'    <stop offset="0%"   stop-color="#378ADD"/>',
        f'    <stop offset="30%"  stop-color="#1D9E75"/>',
        f'    <stop offset="55%"  stop-color="#ef9f27"/>',
        f'    <stop offset="80%"  stop-color="#e24b4a"/>',
        f'    <stop offset="100%" stop-color="#7b241c"/>',
        f'  </linearGradient>',
        '</defs>',
        f'<rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="{bar_h}" rx="2" '
        f'fill="url(#legend-{code})" opacity="0.80"/>',
        f'<text x="{bar_x}" y="{H - 2}" fill="rgba(255,255,255,0.38)" '
        f'font-size="7.5" font-family="Arial,sans-serif">0m</text>',
        f'<text x="{bar_x + bar_w}" y="{H - 2}" text-anchor="end" '
        f'fill="rgba(255,255,255,0.38)" font-size="7.5" font-family="Arial,sans-serif">4m+</text>',
        # 当前波高指示线
    ]
    if wh > 0:
        indicator_x = bar_x + min(wh / 4.5, 1.0) * bar_w
        lines += [
            f'<line x1="{indicator_x:.1f}" y1="{bar_y - 2}" '
            f'x2="{indicator_x:.1f}" y2="{bar_y + bar_h + 2}" '
            f'stroke="white" stroke-width="1.5" opacity="0.80"/>',
        ]

    # ── 数据来源（右下角极小字）
    lines.append(f'<text x="{W - 4}" y="{H - 2}" text-anchor="end" '
                 f'fill="rgba(255,255,255,0.18)" font-size="6.5" '
                 f'font-family="Arial,sans-serif">GFS Wave</text>')

    lines.append('</svg>')
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 5. HTML 报告生成
# ══════════════════════════════════════════════════════════════════════════════

MARINE_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>全球航线海况日报 — {{ date }} | {{ brand }}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
     font-size:13px;color:#1a1a18;background:#edf1f7;padding:20px;}
.wrap{max-width:1060px;margin:0 auto;}

/* header */
.header{background:linear-gradient(135deg,#0d2137 0%,#1a3a5c 100%);
        border-radius:14px;padding:18px 24px;margin-bottom:14px;
        display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px;}
.h-date{font-size:11px;color:rgba(255,255,255,.5);margin-bottom:4px;}
.h-title{font-size:21px;font-weight:600;color:#fff;letter-spacing:.02em;}
.h-sub{font-size:12px;color:rgba(255,255,255,.55);margin-top:3px;}
.h-badges{display:flex;gap:6px;flex-wrap:wrap;align-items:center;}
.hb{font-size:10px;padding:3px 9px;border-radius:4px;font-weight:500;border:1px solid;}
.hb-g{background:rgba(29,158,117,.2);color:#5dcaa5;border-color:rgba(29,158,117,.3);}
.hb-a{background:rgba(239,159,39,.2);color:#f2a623;border-color:rgba(239,159,39,.3);}
.hb-r{background:rgba(226,75,74,.2);color:#f09595;border-color:rgba(226,75,74,.3);}
.h-brand{font-size:13px;font-weight:600;color:rgba(255,255,255,.8);
         padding:4px 12px;border-radius:6px;border:1px solid rgba(255,255,255,.2);
         background:rgba(255,255,255,.08);}

/* alert banner */
.alerts{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap;}
.alert{flex:1;min-width:220px;border-radius:10px;padding:10px 14px;
       display:flex;align-items:flex-start;gap:10px;}
.al-icon{font-size:17px;flex-shrink:0;margin-top:1px;}
.al-title{font-size:12px;font-weight:600;margin-bottom:3px;}
.al-text{font-size:11px;line-height:1.55;}
.al-ok{background:#e8f8f1;border:1px solid #6fdaaa;}
.al-ok .al-title{color:#085041;} .al-ok .al-text{color:#0a6650;}
.al-warn{background:#fff8e6;border:1px solid #f0c84a;}
.al-warn .al-title{color:#7a5200;} .al-warn .al-text{color:#8b6500;}
.al-info{background:#e8f2fb;border:1px solid #90c6f4;}
.al-info .al-title{color:#0c447c;} .al-info .al-text{color:#185fa5;}

/* route cards */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;margin-bottom:14px;}
.rcard{background:#fff;border:1px solid #dde5ee;border-radius:12px;overflow:hidden;}
.rcard.risk-high{border-color:#e24b4a;border-width:1.5px;}
.rcard.risk-mod {border-color:#ef9f27;border-width:1.5px;}
.rcard.risk-low {border-color:#1d9e75;}
.rcard.risk-calm{border-color:#378add;}
.rc-head{padding:10px 14px;display:flex;justify-content:space-between;align-items:center;
         border-bottom:1px solid #eef2f8;}
.rc-name{font-size:13px;font-weight:600;}
.rc-code{font-size:10px;color:#aab8c8;margin-top:1px;}
.rc-badge{font-size:10px;font-weight:600;padding:2px 9px;border-radius:10px;}
.rb-high{background:#feecec;color:#a32d2d;}
.rb-mod{background:#fff3d6;color:#854f0b;}
.rb-low{background:#e8f8f1;color:#085041;}
.rb-calm{background:#e8f2fb;color:#0c447c;}
.rc-body{padding:12px 14px;}
.rc-main{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:10px;}
.rcs-label{font-size:10px;color:#8a9db5;margin-bottom:2px;}
.rcs-val{font-size:18px;font-weight:600;line-height:1.1;}
.rcs-unit{font-size:10px;color:#8a9db5;margin-left:1px;}
.rcs-sub{font-size:10px;color:#aab8c8;margin-top:1px;}
.rcs-val.v-high{color:#a32d2d;} .rcs-val.v-mod{color:#854f0b;} .rcs-val.v-ok{color:#085041;} .rcs-val.v-calm{color:#185fa5;}
.divider{height:1px;background:#eef2f8;margin:8px 0;}
.rc-row{display:flex;gap:6px;margin-bottom:5px;align-items:center;font-size:11px;}
.rc-row:last-child{margin-bottom:0;}
.rc-icon{width:16px;text-align:center;font-size:13px;flex-shrink:0;}
.rc-lbl{color:#8a9db5;width:68px;flex-shrink:0;}
.rc-val{color:#1a1a18;font-weight:500;}
.rc-note{color:#aab8c8;margin-left:3px;}
.rc-note-warn{color:#854f0b;margin-left:3px;font-weight:500;}

/* 5-day forecast bars */
.forecast{display:flex;gap:4px;margin-top:10px;}
.fc-day{flex:1;text-align:center;}
.fc-date{font-size:9px;color:#aab8c8;margin-bottom:2px;}
.fc-wrap{height:28px;background:#f0f4f8;border-radius:4px;overflow:hidden;
         display:flex;align-items:flex-end;}
.fc-bar{width:100%;border-radius:3px 3px 0 0;}
.fc-val{font-size:9px;color:#5a7a9a;margin-top:2px;font-weight:500;}

/* summary table */
.tcard{background:#fff;border:1px solid #dde5ee;border-radius:12px;overflow:hidden;margin-bottom:14px;}
.tc-head{padding:10px 16px;background:#f5f8fc;border-bottom:1px solid #e8eef6;
         display:flex;align-items:center;justify-content:space-between;}
.tc-title{font-size:12px;font-weight:600;}
.tc-badge{font-size:10px;padding:2px 8px;border-radius:4px;background:#e8f2fb;color:#0c447c;}
table{width:100%;border-collapse:collapse;font-size:12px;}
thead th{font-size:10px;font-weight:600;color:#8a9db5;padding:8px 10px;
         text-align:right;border-bottom:1px solid #eef2f8;white-space:nowrap;}
thead th:first-child{text-align:left;}
tbody tr{border-bottom:1px solid #f5f8fc;}
tbody tr:last-child{border-bottom:none;}
tbody tr:hover{background:#fafcff;}
td{padding:7px 10px;text-align:right;vertical-align:middle;}
td:first-child{text-align:left;font-weight:500;}
.risk-pill{font-size:10px;font-weight:600;padding:2px 8px;border-radius:8px;display:inline-block;}
.rp-h{background:#feecec;color:#a32d2d;} .rp-m{background:#fff3d6;color:#854f0b;}
.rp-l{background:#e8f8f1;color:#085041;} .rp-c{background:#e8f2fb;color:#0c447c;}
.td-high{color:#a32d2d;font-weight:600;} .td-mod{color:#854f0b;font-weight:600;}
.td-ok{color:#085041;} .td-calm{color:#185fa5;}

/* analysis views */
.views{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px;}
.vc{background:#fff;border:1px solid #dde5ee;border-radius:12px;padding:12px 14px;}
.vc-seg{font-size:10px;font-weight:600;color:#8a9db5;text-transform:uppercase;
        letter-spacing:.05em;margin-bottom:5px;}
.vc-verdict{font-size:13px;font-weight:600;margin-bottom:5px;}
.v-ok-t{color:#085041;} .v-warn-t{color:#854f0b;}
.vc-text{font-size:11px;color:#6b7280;line-height:1.65;}

/* footer */
.footer{background:#fff;border:1px solid #dde5ee;border-radius:10px;padding:10px 16px;
        font-size:11px;color:#8a9db5;display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;}
.ft-brand{font-weight:700;color:#0d2137;font-size:12px;}

@media(max-width:700px){
  .grid,.views{grid-template-columns:1fr;}
  .alerts{flex-direction:column;}
  .rc-main{grid-template-columns:repeat(3,1fr);}
}

/* ── 航区 SVG 地图 ─────────────────────────────────────────── */
.grid{grid-template-columns:repeat(auto-fill,minmax(320px,1fr));}
.rc-map-wrap{
  width:100%;margin-bottom:10px;border-radius:8px;overflow:hidden;
  border:1px solid rgba(255,255,255,0.08);
  background:#0d2137;
}
.rc-map-wrap svg{display:block;width:100%;height:auto;}
</style>
</head>
<body>
<div class="wrap">

<!-- Header -->
<div class="header">
  <div>
    <div class="h-date">{{ date }} · 数据来源：Open-Meteo Marine API（NOAA GFS Wave 0.25° + ECMWF WAM）· 免费无需 Key</div>
    <div class="h-title">全球主要航线海况日报</div>
    <div class="h-sub">{{ route_count }} 个关键航区 · 波高 / 涌浪 / 风速 · 5日预报 · 航行安全评级</div>
  </div>
  <div class="h-badges">
    {% for b in summary_badges %}<span class="hb {{ b.cls }}">{{ b.text }}</span>{% endfor %}
    <span class="h-brand">{{ brand }}</span>
  </div>
</div>

<!-- Alert banners -->
<div class="alerts">
  {% for a in alert_banners %}
  <div class="alert {{ a.cls }}">
    <div class="al-icon">{{ a.icon }}</div>
    <div>
      <div class="al-title">{{ a.title }}</div>
      <div class="al-text">{{ a.text }}</div>
    </div>
  </div>
  {% endfor %}
</div>

<!-- Route cards（含纯本地 SVG 波高地图）-->
<div class="grid">
{% for r in routes %}
{% set risk = r.risk %}
{% set label, card_cls, badge_cls = risk_labels[risk] %}
<div class="rcard {{ card_cls }}">
  <div class="rc-head">
    <div>
      <div class="rc-name">{{ r.name }}</div>
      <div class="rc-code">{{ r.code }} · {{ r.route_note }}</div>
    </div>
    <span class="rc-badge {{ badge_cls }}">{{ label }}</span>
  </div>
  <div class="rc-body">

    <!-- SVG 波高地图（纯本地生成，无需外网）-->
    <div class="rc-map-wrap">
      {{ r.route_svg|safe }}
    </div>

    <!-- 数值指标 -->
    <div class="rc-main">
      <div>
        <div class="rcs-label">今日最大波高</div>
        <div class="rcs-val {{ 'v-high' if (r.wh_max or 0)>=2.5 else ('v-mod' if (r.wh_max or 0)>=1.5 else ('v-ok' if (r.wh_max or 0)>=0.8 else 'v-calm')) }}">
          {{ '%.2f'|format(r.wh_max) if r.wh_max else '—' }}<span class="rcs-unit">m</span>
        </div>
        <div class="rcs-sub">当前 {{ '%.1f'|format(r.wh) if r.wh else '—' }}m</div>
      </div>
      <div>
        <div class="rcs-label">波浪周期</div>
        <div class="rcs-val">{{ '%.1f'|format(r.wper) if r.wper else '—' }}<span class="rcs-unit">s</span></div>
        <div class="rcs-sub">涌浪 {{ '%.1f'|format(r.sper) if r.sper else '—' }}s</div>
      </div>
      <div>
        <div class="rcs-label">当前风速</div>
        <div class="rcs-val {{ 'v-high' if (r.wind or 0)>=25 else ('v-mod' if (r.wind or 0)>=15 else 'v-ok') }}">
          {{ '%.1f'|format(r.wind) if r.wind else '—' }}<span class="rcs-unit">kn</span>
        </div>
        <div class="rcs-sub">阵风 {{ '%.1f'|format(r.gust) if r.gust else '—' }}kn</div>
      </div>
    </div>
    <div class="divider"></div>
    <div class="rc-row">
      <span class="rc-icon">🌊</span>
      <span class="rc-lbl">涌浪高度</span>
      <span class="rc-val">{{ '%.2f'|format(r.sh) if r.sh else '—' }}m</span>
      <span class="rc-note">来向 {{ r.sdir_compass }}</span>
    </div>
    <div class="rc-row">
      <span class="rc-icon">💨</span>
      <span class="rc-lbl">风向 / 风级</span>
      <span class="rc-val">{{ r.wdir2_compass }}</span>
      <span class="rc-note{% if (r.wind or 0)>=15 %}-warn{% endif %}">{{ r.beaufort }}</span>
    </div>
    <div class="rc-row">
      <span class="rc-icon">📍</span>
      <span class="rc-lbl">参考坐标</span>
      <span class="rc-val">{{ r.lat }}° / {{ r.lon }}°</span>
    </div>
    <!-- 5-day forecast -->
    <div class="forecast">
      {% for i in range(5) %}
      {% set v = r.f5d_max[i] if r.f5d_max and i < r.f5d_max|length else None %}
      {% set dt = r.f5d_dates[i][5:] if r.f5d_dates and i < r.f5d_dates|length else '—' %}
      <div class="fc-day">
        <div class="fc-date">{{ dt }}</div>
        <div class="fc-wrap">
          <div class="fc-bar" style="height:{{ bar_pct(v) }}%;background:{{ bar_color(v) }};"></div>
        </div>
        <div class="fc-val">{{ '%.1f'|format(v) if v else '—' }}m</div>
      </div>
      {% endfor %}
    </div>
  </div>
</div>
{% endfor %}
</div>

<!-- Summary table -->
<div class="tcard">
  <div class="tc-head">
    <span class="tc-title">各航区海况汇总对比</span>
    <span class="tc-badge">{{ date }} UTC</span>
  </div>
  <table>
    <thead>
      <tr>
        <th style="text-align:left;">航区</th>
        <th>波高(当前)</th><th>波高(峰值)</th><th>涌浪高度</th>
        <th>波浪周期</th><th>风速(kn)</th><th>5日峰值</th><th>评级</th>
      </tr>
    </thead>
    <tbody>
      {% for r in routes %}
      {% set risk = r.risk %}
      {% set cls = 'td-high' if risk=='high' else ('td-mod' if risk=='mod' else ('td-ok' if risk=='low' else 'td-calm')) %}
      {% set pill_cls = 'rp-h' if risk=='high' else ('rp-m' if risk=='mod' else ('rp-l' if risk=='low' else 'rp-c')) %}
      {% set rl, _, __ = risk_labels[risk] %}
      <tr {% if risk=='high' %}style="background:#fff8f8;"{% endif %}>
        <td>{{ r.name }}</td>
        <td class="{{ cls }}">{{ '%.1f'|format(r.wh) if r.wh else '—' }}m</td>
        <td class="{{ cls }}"><strong>{{ '%.1f'|format(r.wh_max) if r.wh_max else '—' }}m</strong></td>
        <td>{{ '%.1f'|format(r.sh_max) if r.sh_max else '—' }}m</td>
        <td>{{ '%.1f'|format(r.wp_max) if r.wp_max else '—' }}s</td>
        <td class="{{ 'td-high' if (r.wind or 0)>=25 else ('td-mod' if (r.wind or 0)>=15 else '') }}">
          {{ '%.0f'|format(r.wind) if r.wind else '—' }}
        </td>
        <td class="{{ cls }}">{{ '%.1f'|format(r.f5d_peak) if r.f5d_peak else '—' }}m</td>
        <td><span class="risk-pill {{ pill_cls }}">{{ rl }}</span></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>

<!-- Analysis views -->
<div class="views">
  {% for key, seg, icon in [('apac','亚太航线','🌏'),('io','印度洋/中东','⚓'),('atl','大西洋/欧洲','⛵')] %}
  {% set v = analysis[key] %}
  <div class="vc">
    <div class="vc-seg">{{ icon }} {{ seg }}</div>
    <div class="vc-verdict {{ 'v-ok-t' if v.direction=='ok' else 'v-warn-t' }}">{{ v.verdict }}</div>
    <div class="vc-text">{{ v.text }}</div>
  </div>
  {% endfor %}
</div>

<!-- Footer -->
<div class="footer">
  <div>
    <span class="ft-brand">{{ brand }}</span>
    &nbsp;气象导航服务 &nbsp;·&nbsp;
    海浪数据：Open-Meteo Marine API（GFS Wave + ECMWF WAM）&nbsp;·&nbsp;
    预报时效：5天 · 分辨率 0.25°
  </div>
  <span>生成时间：{{ generated_at }}</span>
</div>

</div>
</body>
</html>"""


def render_marine_html(routes: list[dict], views: dict) -> str:
    """渲染海况日报 HTML，供截图和存档。"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")

    # 给每个 route 注入派生字段
    enriched = []
    for r in routes:
        rc = dict(r)
        rc["risk"]          = _risk_level(r)
        rc["sdir_compass"]  = _deg_to_compass(r.get("sdir"))
        rc["wdir2_compass"] = _deg_to_compass(r.get("wdir2"))
        rc["beaufort"]      = _beaufort(r.get("wind"))
        f5d = [v for v in (r.get("f5d_max") or []) if v is not None]
        rc["f5d_peak"]      = max(f5d) if f5d else None
        # 生成航区 SVG 地图（纯本地，不依赖外网）
        try:
            rc["route_svg"] = generate_route_svg(rc, W=320, H=170)
        except Exception:
            rc["route_svg"] = ""
        enriched.append(rc)

    # 汇总 badge
    high_routes = [r for r in enriched if r["risk"] == "high"]
    mod_routes  = [r for r in enriched if r["risk"] == "mod"]
    ok_routes   = [r for r in enriched if r["risk"] in ("low","calm")]
    badges = []
    if ok_routes:
        badges.append({"cls": "hb-g", "text": f"✓ 适航 {len(ok_routes)} 区"})
    if mod_routes:
        badges.append({"cls": "hb-a", "text": f"⚠ 关注 {len(mod_routes)} 区"})
    if high_routes:
        badges.append({"cls": "hb-r", "text": f"🔴 注意 {len(high_routes)} 区: " + "、".join(r["name"] for r in high_routes)})

    # 告警横幅（动态生成）
    banners = []
    apac_ok = all(r["risk"] in ("low","calm") for r in enriched if r["region"] == "亚太")
    banners.append({
        "cls": "al-ok" if apac_ok else "al-warn",
        "icon": "✅" if apac_ok else "⚠️",
        "title": "亚太 / 南海航线",
        "text": views["apac"]["text"],
    })
    banners.append({
        "cls": "al-ok" if views["io"]["direction"] == "ok" else "al-warn",
        "icon": "✅" if views["io"]["direction"] == "ok" else "⚠️",
        "title": "印度洋 / 中东航线",
        "text": views["io"]["text"],
    })
    banners.append({
        "cls": "al-ok" if views["atl"]["direction"] == "ok" else "al-warn",
        "icon": "✅" if views["atl"]["direction"] == "ok" else "⚠️",
        "title": "大西洋 / 欧洲 / 好望角",
        "text": views["atl"]["text"],
    })

    ctx = {
        "date":          today,
        "brand":         Config.BRAND,
        "route_count":   len(enriched),
        "routes":        enriched,
        "summary_badges": badges,
        "alert_banners": banners,
        "analysis":      views,
        "risk_labels":   _RISK_LABELS,
        "generated_at":  datetime.datetime.now().strftime("%Y-%m-%d %H:%M CST"),
        "bar_pct":       _bar_pct,
        "bar_color":     _bar_color,
    }

    if HAS_JINJA2:
        return Template(MARINE_HTML_TEMPLATE).render(**ctx)
    else:
        # 极简后备
        lines = [f"<html><body><h2>{Config.BRAND} 海况日报 {today}</h2>"]
        for r in enriched:
            lines.append(f"<p>{r['name']}: 波高 {_fmt(r.get('wh_max'),1)}m 风速 {_fmt(r.get('wind'),0)}kn [{r['risk']}]</p>")
        lines.append("<p>请安装 jinja2 获得完整报告</p></body></html>")
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 5. 截图（Playwright，与 bdi_daily_push.py 保持一致）
# ══════════════════════════════════════════════════════════════════════════════

def html_to_image(html_path: str) -> bytes:
    """用 Playwright 截全页长图，返回 PNG 字节。
    SVG 地图为纯本地静态内容，无需等待外网，直接截图即可。
    """
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1080, "height": 900})
        page.goto(f"file://{html_path}", wait_until="domcontentloaded")
        img = page.screenshot(full_page=True)
        browser.close()
    return img


# ══════════════════════════════════════════════════════════════════════════════
# 6. 企业微信推送 — 图片 + 文字卡片
# ══════════════════════════════════════════════════════════════════════════════

def build_marine_wecom_card(routes: list[dict], views: dict) -> dict:
    """构造企业微信 markdown 消息体。"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")

    def fv(v, dec=1):
        return f"{v:.{dec}f}" if v is not None else "—"

    # 各航区评级
    rated = [(r, _risk_level(r)) for r in routes]
    high  = [(r, lv) for r, lv in rated if lv == "high"]
    mod   = [(r, lv) for r, lv in rated if lv == "mod"]

    lines = [
        f"# 🌊 {Config.BRAND} 全球航线海况日报 · {today}",
        "",
    ]

    # 概况一行
    ok_count = sum(1 for _, lv in rated if lv in ("low","calm"))
    lines += [
        f"> 覆盖 **{len(routes)}** 个航区 · "
        f"<font color=\"info\">适航 {ok_count} 区</font> · "
        + (f"<font color=\"warning\">关注 {len(mod)} 区</font> · " if mod else "")
        + (f"<font color=\"warning\">需注意 {len(high)} 区</font>" if high else ""),
        "",
    ]

    # 重点预警（有 high/mod 才出现）
    if high or mod:
        lines.append("## ⚠️ 重点关注航区")
        for r, lv in high + mod:
            tag = "🔴 需注意" if lv == "high" else "🟡 关注"
            lines.append(
                f"> **{tag} {r['name']}**　"
                f"波高 {fv(r.get('wh_max'))}m / "
                f"涌浪 {fv(r.get('sh_max'))}m / "
                f"风速 {fv(r.get('wind'),0)}kn（{_beaufort(r.get('wind'))}）"
            )
        lines.append("")

    # 各航区表格
    lines += [
        "## 📊 各航区海况一览",
        "| 航区 | 波高峰值 | 涌浪 | 风速 | 评级 |",
        "|:---|---:|---:|---:|:---:|",
    ]
    risk_emoji = {"high": "🔴", "mod": "🟡", "low": "🟢", "calm": "🔵"}
    for r, lv in rated:
        lines.append(
            f"| {r['name']} | {fv(r.get('wh_max'))}m | "
            f"{fv(r.get('sh_max'))}m | {fv(r.get('wind'),0)}kn | {risk_emoji[lv]} |"
        )

    # 分区观点
    lines += [
        "",
        "## 🔍 分区航行分析",
        f"**🌏 亚太** <font color=\"{'info' if views['apac']['direction']=='ok' else 'warning'}\">{views['apac']['verdict']}</font>",
        f"> {views['apac']['text']}",
        "",
        f"**⚓ 印度洋/中东** <font color=\"{'info' if views['io']['direction']=='ok' else 'warning'}\">{views['io']['verdict']}</font>",
        f"> {views['io']['text']}",
        "",
        f"**⛵ 大西洋/欧洲** <font color=\"{'info' if views['atl']['direction']=='ok' else 'warning'}\">{views['atl']['verdict']}</font>",
        f"> {views['atl']['text']}",
        "",
        f"<font color=\"comment\">数据：Open-Meteo Marine API（GFS Wave + ECMWF WAM）· 仅供参考 · {Config.BRAND}</font>",
    ]

    return {
        "msgtype": "markdown",
        "markdown": {"content": "\n".join(lines)},
    }


def push_marine_wecom(routes: list[dict], views: dict, html_path: str = "") -> bool:
    """推送海况日报到企业微信：先发长截图，再发文字 markdown。"""
    if not Config.WECOM_WEBHOOK:
        log.warning("WECOM_WEBHOOK 未配置，跳过推送")
        return False

    def _post(payload):
        r = requests.post(Config.WECOM_WEBHOOK, json=payload, timeout=30)
        return r.json()

    img_ok = False

    # ── 1. 长截图 ─────────────────────────────────────────────────────────────
    if html_path:
        try:
            img_bytes = html_to_image(html_path)
            b64 = base64.b64encode(img_bytes).decode()
            md5 = hashlib.md5(img_bytes).hexdigest()
            result = _post({"msgtype": "image", "image": {"base64": b64, "md5": md5}})
            if result.get("errcode") == 0:
                log.info("✅ 企业微信海况截图推送成功")
                img_ok = True
            else:
                log.warning(f"企业微信海况截图推送失败: {result}")
        except Exception as e:
            log.warning(f"海况截图失败，跳过: {e}")

    # ── 2. 文字 markdown（无论截图是否成功，都发送）───────────────────────────
    try:
        result = _post(build_marine_wecom_card(routes, views))
        if result.get("errcode") == 0:
            log.info("✅ 企业微信海况文字推送成功")
            return True
        else:
            log.error(f"企业微信海况文字推送失败: {result}")
            return img_ok
    except Exception as e:
        log.error(f"企业微信海况推送异常: {e}")
        return img_ok


# ══════════════════════════════════════════════════════════════════════════════
# 7. 主流程
# ══════════════════════════════════════════════════════════════════════════════

def run_once() -> bool:
    """拉数据 → 分析 → 生成 HTML → 截图 → 推送企业微信。"""
    log.info("=" * 60)
    log.info(f"{Config.BRAND} 海况日报 — 开始执行")

    # 1. 拉取所有航区数据
    routes = fetch_marine_data()
    if not routes:
        log.error("❌ 所有航区数据拉取失败")
        return False

    # 2. 生成观点分析
    views = _generate_views(routes)

    # 3. 渲染 HTML
    try:
        html = render_marine_html(routes, views)
    except Exception as e:
        log.error(f"❌ HTML 渲染失败: {e}")
        return False

    # 4. 保存 HTML 文件
    Config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    html_file = Config.OUTPUT_DIR / f"marine_daily_{today}.html"
    html_file.write_text(html, encoding="utf-8")
    log.info(f"✅ HTML 已保存: {html_file}")

    # 5. 推送企业微信（截图 + 文字）
    ok = push_marine_wecom(routes, views, html_path=str(html_file.resolve()))
    log.info(f"推送结果: {'✅ 成功' if ok else '❌ 失败'}")
    log.info("=" * 60)
    return ok


def run_scheduled(run_days: list[int] = None, run_time: str = "08:00"):
    """
    定时执行模式。
    run_days: 星期几执行，0=周一…6=周日，默认 [0, 3]（周一、周四）
    run_time: 执行时间，格式 HH:MM（本地时间）
    """
    try:
        import schedule
        import time
    except ImportError:
        log.error("请先安装 schedule: pip install schedule")
        sys.exit(1)

    if run_days is None:
        run_days = [0, 3]  # 默认周一、周四

    day_names = ["周一","周二","周三","周四","周五","周六","周日"]
    day_funcs  = [
        schedule.every().monday, schedule.every().tuesday,
        schedule.every().wednesday, schedule.every().thursday,
        schedule.every().friday, schedule.every().saturday,
        schedule.every().sunday,
    ]

    scheduled = []
    for d in run_days:
        day_funcs[d].at(run_time).do(run_once)
        scheduled.append(day_names[d])

    log.info(f"定时模式已启动：每 {' / '.join(scheduled)} {run_time} 执行")

    while True:
        schedule.run_pending()
        import time as t
        t.sleep(60)


# ══════════════════════════════════════════════════════════════════════════════
# 8. CLI 入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"{Config.BRAND} 海况日报自动化推送")
    parser.add_argument("--schedule",   action="store_true", help="启动定时模式")
    parser.add_argument("--output-dir", default="./reports",  help="报告输出目录")
    parser.add_argument("--time",       default="08:00",      help="定时执行时间 HH:MM（默认 08:00）")
    parser.add_argument("--days",       default="0,3",        help="定时执行星期（0=周一…6=周日，逗号分隔，默认 0,3 即周一周四）")
    args = parser.parse_args()

    Config.OUTPUT_DIR = Path(args.output_dir)

    if args.schedule:
        days = [int(d.strip()) for d in args.days.split(",")]
        run_scheduled(run_days=days, run_time=args.time)
    else:
        success = run_once()
        sys.exit(0 if success else 1)
