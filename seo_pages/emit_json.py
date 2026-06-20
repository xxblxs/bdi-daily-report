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


CARGO_EN = {
    "铁矿石": "Iron ore", "煤炭": "Coal", "动力煤": "Thermal coal", "焦煤": "Coking coal",
    "铝土": "Bauxite", "粮食": "Grain", "煤炭/铝土": "Coal/Bauxite", "铁矿石/煤炭": "Iron ore/Coal",
}


def _cargo_en(zh: str) -> str:
    return CARGO_EN.get(zh, zh)


# ── Port congestion ───────────────────────────────────────────────────────────
def emit_port_json(port_results: list, date: str) -> str:
    """Map analyze_ports() output → bilingual port-congestion report JSON."""
    severe = sum(1 for p in port_results if p.get("congestion") == "high")
    moderate = sum(1 for p in port_results if p.get("congestion") == "mod")
    normalish = sum(1 for p in port_results if p.get("congestion") in ("normal", "low"))
    tot_anchored = sum(p.get("n_anchored", 0) or 0 for p in port_results)
    tot_estimate = sum(p.get("n_estimate", 0) or 0 for p in port_results)
    cong_zh = {"high": "严重", "mod": "中度", "low": "轻度", "normal": "正常"}
    cong_en = {"high": "Severe", "mod": "Moderate", "low": "Light", "normal": "Normal"}
    cong_cls = {"high": "dn", "mod": "neu", "low": "up", "normal": "up"}

    rows = []
    for p in port_results:
        c = p.get("congestion", "normal")
        rows.append([
            f'{p.get("name","")} {p.get("name_cn","")}'.strip(),
            {"zh": p.get("cargo", "—"), "en": _cargo_en(p.get("cargo", "—"))},
            {"v": str(p.get("n_anchored", 0) or 0), "cls": "dn" if c == "high" else ""},
            {"v": str(p.get("n_moored", 0) or 0)},
            {"v": str(p.get("n_estimate", 0) or 0)},
            {"v": str(p.get("est_wait_days", 0) or 0), "cls": "dn" if (p.get("est_wait_days") or 0) >= 3 else ""},
            {"zh": cong_zh.get(c, c), "en": cong_en.get(c, c), "cls": cong_cls.get(c, "")},
        ])
    risk = [p for p in port_results if p.get("congestion") in ("high", "mod")][:3]
    notes = [{
        "type": "bear" if p.get("congestion") == "high" else "neu",
        "tag": {"zh": f'{"高风险" if p.get("congestion")=="high" else "关注"} · {p.get("name","")} {p.get("name_cn","")}'.strip(),
                "en": f'{"High risk" if p.get("congestion")=="high" else "Watch"} · {p.get("name","")}'},
        "text": {"zh": f'<strong>锚泊 {p.get("n_anchored",0)} 艘，估等约 {p.get("est_wait_days",0)} 天。</strong>'
                       f'{p.get("cargo","")}泊位，建议提前计算 Laytime。',
                 "en": f'<strong>{p.get("n_anchored",0)} vessels at anchor, est. ~{p.get("est_wait_days",0)} days wait.</strong> '
                       f'{_cargo_en(p.get("cargo",""))} berth — calculate laytime in advance.'},
    } for p in risk] or [{"type": "neu", "tag": {"zh": "市场资讯", "en": "Note"},
                          "text": {"zh": "当前各港口拥堵程度总体正常。", "en": "Congestion is broadly normal across the network."}}]

    payload = {
        "report_type": "port-congestion", "slug": "port-congestion", "date": date,
        "type_zh": "干散货全球港口拥堵日报", "type_en": "Global Port Congestion Report",
        "headline": {"zh": f"严重拥堵 {severe} 港 · 总锚泊 {tot_anchored} 艘 · 预抵 {tot_estimate} 艘",
                     "en": f"{severe} severe · {tot_anchored} at anchor · {tot_estimate} inbound"},
        "keywords": "港口拥堵,干散货,锚泊,靠泊,滞期风险,铁矿石港口,port congestion,dry bulk demurrage",
        "summary_zh": f"{date} 全球干散货港口监测：严重拥堵 {severe} 港、中度 {moderate} 港、正常 {normalish} 港，"
                      f"总锚泊 {tot_anchored} 艘、预抵 {tot_estimate} 艘。下表为各港锚泊/靠泊/预抵与估等天数。",
        "summary_en": f"On {date}, {severe} ports show severe congestion and {moderate} moderate across the monitored "
                      f"dry-bulk network, with {tot_anchored} vessels at anchor and {tot_estimate} inbound. "
                      "Port-by-port anchored/berth/inbound counts and estimated waiting days are tabulated below.",
        "sections": [
            {"kind": "cards", "title_zh": "全球港口概览", "title_en": "Overview", "items": [
                {"label": {"zh": "严重拥堵", "en": "Severe"}, "value": str(severe), "sub": {"zh": "港", "en": "ports"}, "direction": "dn"},
                {"label": {"zh": "中度拥堵", "en": "Moderate"}, "value": str(moderate), "sub": {"zh": "港", "en": "ports"}, "direction": "neu"},
                {"label": {"zh": "正常运营", "en": "Normal"}, "value": str(normalish), "sub": {"zh": "港", "en": "ports"}, "direction": "up"},
                {"label": {"zh": "总锚泊", "en": "At anchor"}, "value": str(tot_anchored), "sub": {"zh": "艘", "en": "vessels"}, "direction": "neu"},
                {"label": {"zh": "预抵", "en": "Inbound"}, "value": str(tot_estimate), "sub": {"zh": "艘", "en": "vessels"}, "direction": "neu"},
            ]},
            {"kind": "notes", "title_zh": "Demurrage 风险提示", "title_en": "Demurrage Risk", "items": notes},
            {"kind": "table", "title_zh": "各港口拥堵明细", "title_en": "Port-by-Port", "columns": [
                {"zh": "港口", "en": "Port"}, {"zh": "货种", "en": "Cargo"},
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


MARINE_VERDICT_EN = {
    "整体平稳": "Generally calm", "需关注局部": "Watch locally", "海况偏差": "Rough seas",
    "季风过渡期": "Monsoon transition", "总体平稳": "Broadly calm",
    "好望角需关注": "Cape of Good Hope — watch", "整体可行": "Workable",
}


def emit_marine_json(routes: list, views: dict, date: str) -> str:
    risk_zh = {"high": "需注意", "mod": "关注", "low": "适航", "calm": "平稳"}
    risk_en = {"high": "Caution", "mod": "Watch", "low": "Workable", "calm": "Calm"}
    risk_cls = {"high": "dn", "mod": "neu", "low": "up", "calm": "up"}
    rows = []
    for r in routes:
        if r.get("error"):
            continue
        rk = _marine_risk(r)
        rows.append([
            r.get("name", ""),
            {"v": _num(round(r["wind"]) if r.get("wind") is not None else None), "cls": "dn" if (r.get("wind") or 0) >= 25 else ""},
            {"v": str(r.get("wdir2_txt") or r.get("wdir2") or "—")},
            {"v": _num(r.get("wh_max")), "cls": risk_cls.get(rk, "")},
            {"v": _num(r.get("sh_max"))},
            {"zh": risk_zh.get(rk, rk), "en": risk_en.get(rk, rk), "cls": risk_cls.get(rk, "")},
        ])
    vmap = views or {}
    view_items = []
    for key, lz, le in [("apac", "西太/亚太", "W.Pacific"), ("io", "印度洋", "Indian Ocean"), ("atl", "大西洋", "Atlantic")]:
        v = vmap.get(key) or {}
        if v:
            verdict = v.get("verdict", "")
            ve = MARINE_VERDICT_EN.get(verdict, verdict)
            view_items.append({
                "label": {"zh": lz, "en": le},
                "title": {"zh": verdict, "en": ve},
                "text": {"zh": strip_html(v.get("text", "")),
                         "en": f"{le}: {ve}. See the sea-state table above for wave height, swell and wind by leg."},
            })
    worst = max((_marine_risk(r) for r in routes if not r.get("error")), key=lambda x: ["calm", "low", "mod", "high"].index(x), default="calm")
    payload = {
        "report_type": "sea-conditions", "slug": "sea-conditions", "date": date,
        "type_zh": "全球主要航线海况日报", "type_en": "Global Sea Conditions Report",
        "headline": {"zh": f"全球航线海况 · 最高评级 {risk_zh.get(worst, worst)}",
                     "en": f"Global sea state · peak risk {risk_en.get(worst, worst)}"},
        "keywords": "航线海况,浪高,风速,涌浪,适航,天气路由,weather routing,sea state,wave height",
        "summary_zh": f"{date} 主要航线海况：各航区浪高/涌高/风速与分区观点见下，供天气路由与 ETA 决策参考。",
        "summary_en": f"On {date}, per-route wave height, swell and wind across the main shipping legs, plus a "
                      "regional outlook, are listed below to support weather routing and ETA decisions.",
        "sections": [
            {"kind": "table", "title_zh": "各航区海况", "title_en": "Route Sea State", "columns": [
                {"zh": "航区", "en": "Route"}, {"zh": "风速(节)", "en": "Wind", "num": True},
                {"zh": "风向", "en": "Dir", "num": True}, {"zh": "最大浪高(米)", "en": "Wave", "num": True},
                {"zh": "涌高(米)", "en": "Swell", "num": True}, {"zh": "评级", "en": "Risk", "num": True},
            ], "rows": rows},
        ] + ([{"kind": "views", "title_zh": "分区海况观点", "title_en": "Regional Outlook", "items": view_items}] if view_items else []),
        "sources": ["Open-Meteo Marine", "NAVGreen 气象"],
    }
    return _write("sea-conditions", date, payload)


# ── Cyclone ───────────────────────────────────────────────────────────────────
def emit_cyclone_json(storms: list, date: str) -> str:
    if not storms:
        return ""  # event-driven: no page on storm-free days
    notes = []
    for s in storms:
        impact = s.get("impact", "")
        wind = s.get("wind") or s.get("wind_kn")
        nm = s.get("name", "UNNAMED")
        notes.append({
            "type": "bear" if impact in ("danger", "warn") else "neu",
            "tag": {"zh": f'{"台风/飓风" if impact=="danger" else "热带系统"} · {nm}',
                    "en": f'{"Typhoon/Hurricane" if impact=="danger" else "Tropical system"} · {nm}'},
            "text": {
                "zh": f'<strong>中心 {s.get("lat","—")}°N, {s.get("lon","—")}°E，'
                      f'中心风速约 {_num(wind)} 节{("（"+s.get("sshs_text","")+"）") if s.get("sshs_text") else ""}'
                      f'{("，气压 "+str(s.get("pres_mb"))+" hPa") if s.get("pres_mb") else ""}。</strong>'
                      + (f'最近航线：{s.get("nearest_route_name")}（约 {round(s.get("nearest_route_dist"))} 海里）。'
                         if s.get("nearest_route_name") else "")
                      + "建议受影响航线船舶提前绕避，留足安全余量。",
                "en": f'<strong>Centre {s.get("lat","—")}°N, {s.get("lon","—")}°E, '
                      f'max wind ~{_num(wind)} kn{(" ("+s.get("sshs_text","")+")") if s.get("sshs_text") else ""}'
                      f'{(", pressure "+str(s.get("pres_mb"))+" hPa") if s.get("pres_mb") else ""}.</strong> '
                      + (f'Nearest route: {s.get("nearest_route_name")} (~{round(s.get("nearest_route_dist"))} nm). '
                         if s.get("nearest_route_name") else "")
                      + "Vessels on affected routes should divert early and keep ample margin.",
            },
        })
    danger = sum(1 for s in storms if s.get("impact") == "danger")
    payload = {
        "report_type": "cyclone", "slug": "cyclone", "date": date,
        "type_zh": "全球热带气旋预警日报", "type_en": "Global Tropical Cyclone Alert",
        "headline": {"zh": f"{len(storms)} 个活跃热带系统" + (f" · {danger} 个达台风/飓风级" if danger else ""),
                     "en": f"{len(storms)} active tropical system(s)" + (f" · {danger} at typhoon/hurricane strength" if danger else "")},
        "keywords": "台风,热带气旋,飓风,台风路径,航线避台,tropical cyclone,typhoon,hurricane,storm track",
        "summary_zh": f"{date} 全球活跃热带系统 {len(storms)} 个。下方为各系统位置、强度、最近航线与航运影响,供避台航线规划。",
        "summary_en": f"On {date}, {len(storms)} active tropical system(s) are tracked. Position, intensity, the "
                      "nearest shipping route and impact for each are summarized below for voyage avoidance.",
        "sections": [
            {"kind": "cards", "title_zh": "活跃系统概览", "title_en": "Active Systems", "items": [
                {"label": {"zh": "活跃系统", "en": "Active systems"}, "value": str(len(storms)), "sub": {"zh": "个", "en": ""}, "direction": "dn" if danger else "neu"},
                {"label": {"zh": "台风/飓风级", "en": "Typhoon/Hurricane"}, "value": str(danger), "sub": {"zh": "个", "en": ""}, "direction": "dn" if danger else "up"},
            ]},
            {"kind": "notes", "title_zh": "各系统详情与航运影响", "title_en": "Storm Details & Shipping Impact", "items": notes},
        ],
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
        mv = p.get("vlsfo_move") if isinstance(p.get("vlsfo_move"), (int, float)) else None
        rows.append([
            f'{p.get("port","")}',
            {"v": _num(p.get("ifo380"), "$")},
            {"v": _num(p.get("vlsfo"), "$")},
            {"v": _num(p.get("lsmgo"), "$")},
            {"v": _num(p.get("spread_vh"), "$")},
        ])
    payload = {
        "report_type": "bunker-fuel", "slug": "bunker-fuel", "date": date,
        "type_zh": "全球船用燃油日报", "type_en": "Global Bunker Fuel Report",
        "headline": {"zh": f"IFO380 均价 {_num(ifo, '$', '/吨')} · VLSFO {_num(vlsfo, '$')} · LSMGO {_num(lsmgo, '$')}",
                     "en": f"IFO380 avg {_num(ifo, '$', '/t')} · VLSFO {_num(vlsfo, '$')} · LSMGO {_num(lsmgo, '$')}"},
        "keywords": "船用燃油,燃油价格,IFO380,VLSFO,LSMGO,加油港,bunker price,marine fuel,scrubber spread",
        "summary_zh": f"{date} 全球船用燃油均价：IFO380 {_num(ifo,'$')}/吨、VLSFO {_num(vlsfo,'$')}、LSMGO {_num(lsmgo,'$')};"
                      f"VLSFO−IFO380 脱硫塔价差约 {_num(spread,'$')}/吨。下表为关键港口价格。",
        "summary_en": f"On {date}, global bunker averages: IFO380 {_num(ifo,'$')}/t, VLSFO {_num(vlsfo,'$')}/t, "
                      f"LSMGO {_num(lsmgo,'$')}/t, scrubber spread ~{_num(spread,'$')}/t. Key-port prices below.",
        "sections": [
            {"kind": "cards", "title_zh": "全球均价概览", "title_en": "Global Averages (USD/t)", "items": [
                {"label": {"zh": "IFO380 均价", "en": "IFO380 avg"}, "value": _num(ifo, "$"), "sub": {"zh": "全球", "en": "Global"}, "direction": "neu"},
                {"label": {"zh": "VLSFO 均价", "en": "VLSFO avg"}, "value": _num(vlsfo, "$"), "sub": {"zh": "全球", "en": "Global"}, "direction": "neu"},
                {"label": {"zh": "LSMGO 均价", "en": "LSMGO avg"}, "value": _num(lsmgo, "$"), "sub": {"zh": "全球", "en": "Global"}, "direction": "neu"},
                {"label": {"zh": "脱硫塔价差", "en": "Scrubber spread"}, "value": _num(spread, "$"), "sub": {"zh": "VLSFO−IFO380", "en": "VLSFO−IFO380"}, "direction": "up"},
            ]},
            {"kind": "table", "title_zh": "关键港口价格", "title_en": "Key Ports", "columns": [
                {"zh": "港口", "en": "Port"}, {"zh": "IFO380", "en": "", "num": True},
                {"zh": "VLSFO", "en": "", "num": True}, {"zh": "LSMGO", "en": "", "num": True},
                {"zh": "价差", "en": "Spread", "num": True},
            ], "rows": rows},
        ],
        "sources": ["Ship & Bunker", "NAVGreen"],
    }
    return _write("bunker-fuel", date, payload)


def strip_html(s: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", s or "")
