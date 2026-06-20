#!/usr/bin/env python3
"""
Emit a structured JSON snapshot of a daily report, consumed by generator.py.

Call this from each pipeline script (e.g. bdi_daily_push_add_fuel.run_once) right
after the data + analysis + views are computed — everything is already in memory,
so no extra API calls. The JSON is the single source of truth for the SEO page.

Integration in bdi_daily_push_add_fuel.run_once() — after line ~2111
`html, market_analysis, views = render_html(data, news)`:

    from seo_pages.emit_json import emit_bdi_json
    json_path = emit_bdi_json(data, news, market_analysis, views, fuel_data)
    # then (optional, same run): generate + publish
    #   from seo_pages.generator import build_html ; from seo_pages.publish import publish_page
"""
import json
import pathlib
from collections import defaultdict

DATA_ROOT = pathlib.Path(__file__).resolve().parent / "data"


def build_headline_safe(data: dict) -> str:
    idx = data["indices"]["BDI"]
    pct = idx.get("pct")
    if pct is None:
        return "市场数据已更新"
    sign = "↑" if pct >= 0 else "↓"
    return f"BDI {sign}{abs(pct):.1f}%"


def emit_bdi_json(data: dict, news: list, market_analysis: list, views,
                  fuel_data: dict | None = None) -> str:
    """Serialize the BDI report into seo_pages/data/bdi-market/<date>.json."""
    # `views` from generate_views() is a tuple of per-shiptype view dicts/tuples;
    # normalize to a list of {shiptype,title,text}.
    norm_views = []
    for v in (views or []):
        if isinstance(v, dict):
            norm_views.append({"shiptype": v.get("shiptype", ""),
                               "title": v.get("title", ""), "text": v.get("text", "")})
        elif isinstance(v, (list, tuple)) and len(v) >= 3:
            norm_views.append({"shiptype": v[0], "title": v[1], "text": v[2]})

    fuel = None
    if fuel_data:
        gs = fuel_data.get("global_stats", {})
        fuel = {
            "ifo380_avg": gs.get("ifo380", {}).get("avg"),
            "vlsfo_avg": gs.get("vlsfo", {}).get("avg"),
            "mgo_avg": gs.get("mgo", {}).get("avg"),
            "fresh_count": fuel_data.get("fresh_count"),
        }

    payload = {
        "report_type": "bdi-market",
        "date": data["date"],
        "headline": build_headline_safe(data),
        "indices": data["indices"],
        "routes": data["routes"],
        "trend": data.get("trend", []),
        "week_chg": data.get("week_chg", {}),
        "market_analysis": market_analysis or [],
        "views": norm_views,
        "fuel": fuel,
        "sources": ["Baltic Exchange", "Hellenic Shipping News", "Splash247"],
    }
    return _write("bdi-market", data["date"], payload)


def _write(slug: str, date: str, payload: dict) -> str:
    out_dir = DATA_ROOT / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{date}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(out)


def _num(v, prefix="", suffix=""):
    return f"{prefix}{v}{suffix}" if v is not None else "—"


def _fmt(v, nd: int = 0, prefix: str = "", suffix: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        text = f"{v:,.{nd}f}" if nd else f"{v:,.0f}"
        return f"{prefix}{text}{suffix}"
    return f"{prefix}{v}{suffix}"


def _plain(s: str) -> str:
    return strip_html(str(s or "")).strip()


# ── Port congestion ───────────────────────────────────────────────────────────
def emit_port_json(port_results: list, date: str) -> str:
    """Map analyze_ports() output → port-congestion report JSON."""
    severe = sum(1 for p in port_results if p.get("congestion") == "high")
    moderate = sum(1 for p in port_results if p.get("congestion") == "mod")
    normalish = sum(1 for p in port_results if p.get("congestion") in ("normal", "low"))
    tot_anchored = sum(p.get("n_anchored", 0) or 0 for p in port_results)
    tot_estimate = sum(p.get("n_estimate", 0) or 0 for p in port_results)
    cong_zh = {"high": "严重", "mod": "中度", "low": "轻度", "normal": "正常"}
    cong_cls = {"high": "dn", "mod": "neu", "low": "up", "normal": "up"}

    grouped = defaultdict(lambda: {
        "ports": 0, "anchored": 0, "moored": 0, "inbound": 0,
        "high": 0, "mod": 0, "max_wait": 0,
    })
    rows = []
    for p in port_results:
        c = p.get("congestion", "normal")
        group = p.get("group") or "未分组"
        g = grouped[group]
        g["ports"] += 1
        g["anchored"] += p.get("n_anchored", 0) or 0
        g["moored"] += p.get("n_moored", 0) or 0
        g["inbound"] += p.get("n_estimate", 0) or 0
        g["high"] += 1 if c == "high" else 0
        g["mod"] += 1 if c == "mod" else 0
        g["max_wait"] = max(g["max_wait"], p.get("est_wait_days", 0) or 0)
        rows.append([
            group,
            f'{p.get("name","")} {p.get("name_cn","")}'.strip(),
            p.get("cargo", "—"),
            {"v": str(p.get("n_anchored", 0) or 0), "cls": "dn" if c == "high" else ""},
            {"v": str(p.get("n_moored", 0) or 0)},
            {"v": str(p.get("n_estimate", 0) or 0)},
            {"v": str(p.get("est_wait_days", 0) or 0), "cls": "dn" if (p.get("est_wait_days") or 0) >= 3 else ""},
            {"v": cong_zh.get(c, c), "cls": cong_cls.get(c, "")},
        ])
    group_rows = []
    for group, g in sorted(grouped.items(), key=lambda kv: kv[1]["anchored"], reverse=True):
        risk_cls = "dn" if g["high"] else ("neu" if g["mod"] else "up")
        group_rows.append([
            group,
            {"v": str(g["ports"])},
            {"v": str(g["anchored"]), "cls": risk_cls},
            {"v": str(g["moored"])},
            {"v": str(g["inbound"])},
            {"v": _fmt(g["max_wait"], 1), "cls": "dn" if g["max_wait"] >= 3 else ""},
            {"v": f'{g["high"]}/{g["mod"]}', "cls": risk_cls},
        ])
    risk_rank = {"high": 2, "mod": 1}
    risk = sorted(
        [p for p in port_results if p.get("congestion") in ("high", "mod")],
        key=lambda p: (risk_rank.get(p.get("congestion"), 0), p.get("est_wait_days", 0) or 0, p.get("n_anchored", 0) or 0),
        reverse=True,
    )[:5]
    notes = [{
        "type": "bear" if p.get("congestion") == "high" else "neu",
        "tag": f'{"高风险" if p.get("congestion")=="high" else "关注"} · {p.get("group","")} · {p.get("name","")} {p.get("name_cn","")}'.strip(),
        "text": f'<strong>锚泊 {p.get("n_anchored",0)} 艘，估等约 {p.get("est_wait_days",0)} 天。</strong>'
                f'{p.get("cargo","")}泊位，建议提前计算 Laytime。',
    } for p in risk] or [{"type": "neu", "tag": "市场资讯", "text": "当前各港口拥堵程度总体正常。"}]

    payload = {
        "report_type": "port-congestion", "slug": "port-congestion", "date": date,
        "type_zh": "干散货全球港口拥堵日报", "type_en": "Global Port Congestion Report",
        "headline": f"严重拥堵 {severe} 港 · 总锚泊 {tot_anchored} 艘 · 预抵 {tot_estimate} 艘",
        "keywords": "港口拥堵,干散货,锚泊,靠泊,滞期风险,铁矿石港口,port congestion,dry bulk demurrage",
        "summary_zh": f"{date} 全球干散货港口监测：严重拥堵 {severe} 港、中度 {moderate} 港、正常 {normalish} 港，"
                      f"总锚泊 {tot_anchored} 艘、预抵 {tot_estimate} 艘。下表为各港锚泊/靠泊/预抵与估等天数。",
        "summary_en": f"On {date}, {severe} ports show severe congestion and {moderate} moderate across the monitored "
                      f"dry-bulk network, with {tot_anchored} vessels at anchor and {tot_estimate} inbound. "
                      "Port-by-port anchored/berth/inbound counts and estimated waiting days are tabulated below.",
        "sections": [
            {"kind": "cards", "title_zh": "全球港口概览", "title_en": "Overview", "items": [
                {"label": "严重拥堵", "value": str(severe), "sub": "港", "direction": "dn"},
                {"label": "中度拥堵", "value": str(moderate), "sub": "港", "direction": "neu"},
                {"label": "正常运营", "value": str(normalish), "sub": "港", "direction": "up"},
                {"label": "总锚泊", "value": str(tot_anchored), "sub": "艘", "direction": "neu"},
                {"label": "预抵", "value": str(tot_estimate), "sub": "艘", "direction": "neu"},
            ]},
            {"kind": "table", "title_zh": "按装港/卸港分区汇总", "title_en": "Regional Loading/Discharge Summary", "columns": [
                {"zh": "分区", "en": "Group"}, {"zh": "港口", "en": "Ports", "num": True},
                {"zh": "锚泊", "en": "Anchored", "num": True}, {"zh": "靠泊", "en": "Berthed", "num": True},
                {"zh": "预抵", "en": "Inbound", "num": True}, {"zh": "最长估等", "en": "Max wait", "num": True},
                {"zh": "严重/中度", "en": "High/Mod", "num": True},
            ], "rows": group_rows},
            {"kind": "notes", "title_zh": "Demurrage 风险提示", "title_en": "Demurrage Risk", "items": notes},
            {"kind": "table", "title_zh": "各港口拥堵明细", "title_en": "Port-by-Port", "columns": [
                {"zh": "分区", "en": "Group"}, {"zh": "港口", "en": "Port"}, {"zh": "货种", "en": "Cargo"},
                {"zh": "锚泊", "en": "Anchored", "num": True}, {"zh": "靠泊", "en": "Berth", "num": True},
                {"zh": "预抵", "en": "Inbound", "num": True}, {"zh": "估等(天)", "en": "Wait", "num": True},
                {"zh": "评级", "en": "Level", "num": True},
            ], "rows": rows},
        ],
        "sources": ["MyVessel 港口实时数据", "ISOWAY"],
    }
    return _write("port-congestion", date, payload)


# ── Sea conditions (marine) ───────────────────────────────────────────────────
def _marine_risk(r: dict) -> str:
    wh, wind, sh = r.get("wh_max") or 0, r.get("wind") or 0, r.get("sh_max") or 0
    if wh >= 2.5 or wind >= 25 or sh >= 2.0: return "high"
    if wh >= 1.5 or wind >= 15 or sh >= 1.2: return "mod"
    if wh >= 0.8 or wind >= 8: return "low"
    return "calm"


def emit_marine_json(routes: list, views: dict, date: str) -> str:
    risk_zh = {"high": "需注意", "mod": "关注", "low": "适航", "calm": "平稳"}
    risk_cls = {"high": "dn", "mod": "neu", "low": "up", "calm": "up"}
    rows = []
    forecast_rows = []
    worst_wave = 0
    max_gust = 0
    risk_counts = defaultdict(int)
    for r in routes:
        if r.get("error"):
            continue
        rk = _marine_risk(r)
        risk_counts[rk] += 1
        wh_max = r.get("wh_max") or 0
        gust = r.get("gust") or 0
        worst_wave = max(worst_wave, wh_max)
        max_gust = max(max_gust, gust)
        f5d_values = [(d, v) for d, v in zip(r.get("f5d_dates", []), r.get("f5d_max", [])) if v is not None]
        peak_date, peak_wave = max(f5d_values, key=lambda x: x[1]) if f5d_values else ("—", None)
        rows.append([
            r.get("name", ""),
            {"v": _num(round(r["wind"]) if r.get("wind") is not None else None), "cls": "dn" if (r.get("wind") or 0) >= 25 else ""},
            {"v": _num(round(r["gust"]) if r.get("gust") is not None else None), "cls": "dn" if (r.get("gust") or 0) >= 30 else ""},
            {"v": str(r.get("wdir2_txt") or r.get("wdir2") or "—")},
            {"v": _fmt(r.get("wh"), 1)},
            {"v": _num(r.get("wh_max")), "cls": risk_cls.get(rk, "")},
            {"v": _fmt(r.get("wp_max"), 1)},
            {"v": _num(r.get("sh_max"))},
            {"v": risk_zh.get(rk, rk), "cls": risk_cls.get(rk, "")},
        ])
        forecast_rows.append([
            r.get("name", ""),
            {"v": _fmt(peak_wave, 1), "cls": "dn" if (peak_wave or 0) >= 2.5 else ""},
            peak_date,
            {"v": _fmt(r.get("wind_max12h"), 0), "cls": "dn" if (r.get("wind_max12h") or 0) >= 25 else ""},
            {"v": _fmt(r.get("wwh_max"), 1)},
        ])
    vmap = views or {}
    view_items = []
    for key, label in [("apac", "西太/亚太 W.PACIFIC"), ("io", "印度洋 INDIAN OCEAN"), ("atl", "大西洋 ATLANTIC")]:
        v = vmap.get(key) or {}
        if v:
            view_items.append({"label": label, "title": v.get("verdict", ""), "text": strip_html(v.get("text", ""))})
    worst = max((_marine_risk(r) for r in routes if not r.get("error")), key=lambda x: ["calm", "low", "mod", "high"].index(x), default="calm")
    payload = {
        "report_type": "sea-conditions", "slug": "sea-conditions", "date": date,
        "type_zh": "全球主要航线海况日报", "type_en": "Global Sea Conditions Report",
        "headline": f"全球航线海况 · 最高评级 {risk_zh.get(worst, worst)}",
        "keywords": "航线海况,浪高,风速,涌浪,适航,天气路由,weather routing,sea state,wave height",
        "summary_zh": f"{date} 主要航线海况：各航区浪高/涌高/风速与分区观点见下，供天气路由与 ETA 决策参考。",
        "summary_en": f"On {date}, per-route wave height, swell and wind across the main shipping legs, plus a "
                      "regional outlook, are listed below to support weather routing and ETA decisions.",
        "sections": [
            {"kind": "cards", "title_zh": "海况概览", "title_en": "Sea-State Overview", "items": [
                {"label": "需注意", "value": str(risk_counts["high"]), "sub": "航区", "direction": "dn" if risk_counts["high"] else "up"},
                {"label": "关注", "value": str(risk_counts["mod"]), "sub": "航区", "direction": "neu"},
                {"label": "适航/平稳", "value": str(risk_counts["low"] + risk_counts["calm"]), "sub": "航区", "direction": "up"},
                {"label": "最高浪高", "value": _fmt(worst_wave, 1, suffix="m"), "sub": "5日预报", "direction": "dn" if worst_wave >= 2.5 else "neu"},
                {"label": "最大阵风", "value": _fmt(max_gust, 0, suffix="kn"), "sub": "当前", "direction": "dn" if max_gust >= 30 else "neu"},
            ]},
            {"kind": "table", "title_zh": "各航区海况", "title_en": "Route Sea State", "columns": [
                {"zh": "航区", "en": "Route"}, {"zh": "风速(节)", "en": "Wind", "num": True},
                {"zh": "阵风(节)", "en": "Gust", "num": True}, {"zh": "风向", "en": "Dir", "num": True},
                {"zh": "当前浪高", "en": "Now", "num": True}, {"zh": "5日峰值(米)", "en": "5d peak", "num": True},
                {"zh": "周期(秒)", "en": "Period", "num": True}, {"zh": "涌高(米)", "en": "Swell", "num": True},
                {"zh": "评级", "en": "Risk", "num": True},
            ], "rows": rows},
            {"kind": "table", "title_zh": "5日预报峰值", "title_en": "5-Day Forecast Peaks", "columns": [
                {"zh": "航区", "en": "Route"}, {"zh": "峰值浪高", "en": "Peak wave", "num": True},
                {"zh": "峰值日期", "en": "Date"}, {"zh": "12h最大风", "en": "Max wind", "num": True},
                {"zh": "风浪峰值", "en": "Wind wave", "num": True},
            ], "rows": forecast_rows},
        ] + ([{"kind": "views", "title_zh": "分区海况观点", "title_en": "Regional Outlook", "items": view_items}] if view_items else []),
        "sources": ["Open-Meteo Marine", "NAVGreen 气象"],
    }
    return _write("sea-conditions", date, payload)


# ── Cyclone ───────────────────────────────────────────────────────────────────
def emit_cyclone_json(storms: list, date: str) -> str:
    if not storms:
        return ""  # event-driven: no page on storm-free days
    notes = []
    forecast_rows = []
    max_wind = 0
    for s in storms:
        impact = s.get("impact", "")
        wind = s.get("wind") or s.get("wind_kn")
        max_wind = max(max_wind, wind or 0)
        mov_dir = s.get("mov_dir") or s.get("movement_dir") or s.get("direction") or "—"
        mov_speed = s.get("mov_speed") or s.get("movement_speed") or s.get("speed")
        forecasts = s.get("forecasts") or s.get("forecast") or []
        for i, fp in enumerate(forecasts[:6], start=1):
            forecast_rows.append([
                s.get("name", "UNNAMED"),
                {"v": str(i)},
                fp.get("time") or fp.get("valid_time") or fp.get("date") or "—",
                f'{_fmt(fp.get("lat"), 1)} / {_fmt(fp.get("lon"), 1)}',
                {"v": _fmt(fp.get("wind") or fp.get("wind_kn"), 0), "cls": "dn" if (fp.get("wind") or fp.get("wind_kn") or 0) >= 64 else ""},
            ])
        notes.append({
            "type": "bear" if impact in ("danger", "warn") else "neu",
            "tag": f'{"台风/飓风" if impact=="danger" else "热带系统"} · {s.get("name","UNNAMED")}',
            "text": f'<strong>中心 {s.get("lat","—")}°N, {s.get("lon","—")}°E，'
                    f'中心风速约 {_num(wind)} 节{("（"+s.get("sshs_text","")+"）") if s.get("sshs_text") else ""}'
                    f'{("，气压 "+str(s.get("pres_mb"))+" hPa") if s.get("pres_mb") else ""}。</strong>'
                    f'移动方向/速度：{mov_dir} / {_fmt(mov_speed, 0, suffix=" kn")}。'
                    + (f'最近航线：{s.get("nearest_route_name")}（约 {round(s.get("nearest_route_dist"))} 海里）。'
                       if s.get("nearest_route_name") else "")
                    + "建议受影响航线船舶提前绕避，留足安全余量。",
        })
    danger = sum(1 for s in storms if s.get("impact") == "danger")
    payload = {
        "report_type": "cyclone", "slug": "cyclone", "date": date,
        "type_zh": "全球热带气旋预警日报", "type_en": "Global Tropical Cyclone Alert",
        "headline": f"{len(storms)} 个活跃热带系统" + (f" · {danger} 个达台风/飓风级" if danger else ""),
        "keywords": "台风,热带气旋,飓风,台风路径,航线避台,tropical cyclone,typhoon,hurricane,storm track",
        "summary_zh": f"{date} 全球活跃热带系统 {len(storms)} 个。下方为各系统位置、强度、最近航线与航运影响,供避台航线规划。",
        "summary_en": f"On {date}, {len(storms)} active tropical system(s) are tracked. Position, intensity, the "
                      "nearest shipping route and impact for each are summarized below for voyage avoidance.",
        "sections": [
            {"kind": "cards", "title_zh": "活跃系统概览", "title_en": "Active Systems", "items": [
                {"label": "活跃系统", "value": str(len(storms)), "sub": "个", "direction": "dn" if danger else "neu"},
                {"label": "台风/飓风级", "value": str(danger), "sub": "个", "direction": "dn" if danger else "up"},
                {"label": "最强风速", "value": _fmt(max_wind, 0), "sub": "kn", "direction": "dn" if max_wind >= 64 else "neu"},
                {"label": "预报路径点", "value": str(len(forecast_rows)), "sub": "个", "direction": "neu"},
            ]},
            {"kind": "notes", "title_zh": "各系统详情与航运影响", "title_en": "Storm Details & Shipping Impact", "items": notes},
        ] + ([{"kind": "table", "title_zh": "预报路径", "title_en": "Forecast Track", "columns": [
            {"zh": "系统", "en": "Storm"}, {"zh": "序号", "en": "#", "num": True},
            {"zh": "时间", "en": "Time"}, {"zh": "纬度/经度", "en": "Lat/Lon"},
            {"zh": "风速(kn)", "en": "Wind", "num": True},
        ], "rows": forecast_rows}] if forecast_rows else []),
        "sources": ["NAVGreen 气象", "NHC", "IBTrACS"],
    }
    return _write("cyclone", date, payload)


# ── Bunker fuel ───────────────────────────────────────────────────────────────
def emit_fuel_json(fuel_data: dict, date: str) -> str:
    if not fuel_data:
        return ""
    gs = fuel_data.get("global_stats", {})
    ifo = gs.get("ifo380", {}).get("avg")
    vlsfo = gs.get("vlsfo", {}).get("avg")
    lsmgo = gs.get("lsmgo", {}).get("avg")
    spread = fuel_data.get("avg_spread")
    rows = []
    for p in (fuel_data.get("key_ports") or [])[:8]:
        rows.append([
            f'{p.get("port","")}',
            p.get("district", "—"),
            {"v": _num(p.get("ifo380"), "$")},
            {"v": _num(p.get("vlsfo"), "$")},
            {"v": _num(p.get("lsmgo"), "$")},
            {"v": _num(p.get("spread_vh"), "$")},
        ])
    china_rows = []
    for p in (fuel_data.get("china_ports") or [])[:12]:
        ifo_p = p.get("ifo380")
        vlsfo_p = p.get("vlsfo")
        spread_vh = round(vlsfo_p - ifo_p, 1) if vlsfo_p and ifo_p else None
        china_rows.append([
            p.get("portName") or p.get("port") or "",
            p.get("district", "CHINA"),
            {"v": _num(ifo_p, "$")},
            {"v": _num(vlsfo_p, "$")},
            {"v": _num(p.get("lsmgo"), "$")},
            {"v": _num(spread_vh, "$")},
        ])
    spread_rows = []
    for i, p in enumerate((fuel_data.get("spread_list") or [])[:10], start=1):
        spread_v = p.get("spread")
        spread_rows.append([
            {"v": str(i)},
            p.get("port", ""),
            p.get("district", ""),
            {"v": _num(spread_v, "$"), "cls": "up" if (spread_v or 0) >= 150 else "neu"},
        ])
    view_items = []
    for key, label in [("price_level", "价格水平 PRICE"), ("scrubber", "脱硫塔价差 SCRUBBER"), ("regional", "区域价差 REGIONAL")]:
        v = (fuel_data.get("views") or {}).get(key) or {}
        if v:
            view_items.append({"label": label, "title": v.get("verdict", ""), "text": _plain(v.get("text", ""))})
    payload = {
        "report_type": "bunker-fuel", "slug": "bunker-fuel", "date": date,
        "type_zh": "全球船用燃油日报", "type_en": "Global Bunker Fuel Report",
        "headline": f"IFO380 均价 {_num(ifo, '$', '/吨')} · VLSFO {_num(vlsfo, '$')} · LSMGO {_num(lsmgo, '$')}",
        "keywords": "船用燃油,燃油价格,IFO380,VLSFO,LSMGO,加油港,bunker price,marine fuel,scrubber spread",
        "summary_zh": f"{date} 全球船用燃油均价：IFO380 {_num(ifo,'$')}/吨、VLSFO {_num(vlsfo,'$')}、LSMGO {_num(lsmgo,'$')};"
                      f"VLSFO−IFO380 脱硫塔价差约 {_num(spread,'$')}/吨。下表为关键港口价格。",
        "summary_en": f"On {date}, global bunker averages: IFO380 {_num(ifo,'$')}/t, VLSFO {_num(vlsfo,'$')}/t, "
                      f"LSMGO {_num(lsmgo,'$')}/t, scrubber spread ~{_num(spread,'$')}/t. Key-port prices below.",
        "sections": [
            {"kind": "cards", "title_zh": "全球均价概览", "title_en": "Global Averages (USD/t)", "items": [
                {"label": "IFO380 均价", "value": _num(ifo, "$"), "sub": "全球", "direction": "neu"},
                {"label": "VLSFO 均价", "value": _num(vlsfo, "$"), "sub": "全球", "direction": "neu"},
                {"label": "LSMGO 均价", "value": _num(lsmgo, "$"), "sub": "全球", "direction": "neu"},
                {"label": "脱硫塔价差", "value": _num(spread, "$"), "sub": "VLSFO−IFO380", "direction": "up"},
                {"label": "有效港口", "value": str(fuel_data.get("fresh_count") or "—"), "sub": "近30天", "direction": "neu"},
            ]},
            {"kind": "table", "title_zh": "关键港口价格", "title_en": "Key Ports", "columns": [
                {"zh": "港口", "en": "Port"}, {"zh": "区域", "en": "District"},
                {"zh": "IFO380", "en": "", "num": True},
                {"zh": "VLSFO", "en": "", "num": True}, {"zh": "LSMGO", "en": "", "num": True},
                {"zh": "价差", "en": "Spread", "num": True},
            ], "rows": rows},
        ] + ([{"kind": "table", "title_zh": "中国港口专区", "title_en": "China Ports", "columns": [
            {"zh": "港口", "en": "Port"}, {"zh": "区域", "en": "District"},
            {"zh": "IFO380", "en": "", "num": True}, {"zh": "VLSFO", "en": "", "num": True},
            {"zh": "LSMGO", "en": "", "num": True}, {"zh": "价差", "en": "Spread", "num": True},
        ], "rows": china_rows}] if china_rows else []) + ([{"kind": "table", "title_zh": "Scrubber 价差排行", "title_en": "Scrubber Spread Ranking", "columns": [
            {"zh": "排名", "en": "#", "num": True}, {"zh": "港口", "en": "Port"},
            {"zh": "区域", "en": "District"}, {"zh": "VLSFO−IFO380", "en": "Spread", "num": True},
        ], "rows": spread_rows}] if spread_rows else []) + ([{"kind": "views", "title_zh": "价格观点", "title_en": "Market Views", "items": view_items}] if view_items else []),
        "sources": ["Ship & Bunker", "NAVGreen"],
    }
    return _write("bunker-fuel", date, payload)


def strip_html(s: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", s or "")
