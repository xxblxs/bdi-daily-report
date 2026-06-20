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
    out_dir = DATA_ROOT / "bdi-market"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{data['date']}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(out)
