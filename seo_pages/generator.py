#!/usr/bin/env python3
"""
Daily report -> bilingual SEO HTML generator.

Consumes a structured JSON (emitted by the daily pipeline, see emit_json.py) and
renders a self-contained, crawlable HTML page for navgreen.cn/reports/<type>/<date>/.

Chinese-primary (for Baidu) with bilingual labels + an English summary section
(for Google/hreflang). Semantic tables (real text, not screenshots), full per-page
SEO head, Dataset + NewsArticle JSON-LD, and internal links into navgreen pillars.

Usage:
    python generator.py <data.json> <out_dir> [--site https://www.navgreen.cn] [--prev YYYY-MM-DD]
"""
import argparse
import datetime
import html
import json
import pathlib

SITE_DEFAULT = "https://www.navgreen.cn"
BRAND = "NAVGreen"

# Internal-link targets into the navgreen content graph (pillar/cluster SEO).
NAV_LINKS = [
    ("/solutions/freight-prediction-bulk-carriers", "运费预测软件", "Freight Prediction"),
    ("/blog/bdi-forecast", "BDI 预测：如何解读波罗的海干散货指数", "Read the BDI"),
    ("/blog/factors-affecting-freight-rates", "哪些因素影响干散货运费", "Freight Rate Drivers"),
    ("/blog/forecast-dry-bulk-freight-rates", "如何用 AI 预测干散货运费", "Forecast Freight with AI"),
]


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def fmt_num(v, unit="day") -> str:
    if v is None:
        return "—"
    if unit == "ton":
        return f"${v:,.1f}/吨"
    if unit == "day":
        return f"${v:,.0f}/天"
    return f"{v:,.0f}"


def dir_class(direction: str) -> str:
    return {"up": "up", "dn": "dn"}.get(direction, "neu")


def strip_tags(s: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", s or "")


def en_summary(p: dict) -> str:
    """Deterministic English market summary (no translation API needed)."""
    idx = p["indices"]
    def line(k, name):
        d = idx.get(k, {})
        v = d.get("val")
        ps = d.get("pct_str", "n/a")
        return f"{name} at {int(v) if v is not None else 'n/a'} ({ps})"
    parts = [line("BDI", "BDI"), line("BCI", "Capesize (BCI)"),
             line("BPI", "Panamax (BPI)"), line("BSI", "Supramax (BSI)"),
             line("BHSI", "Handysize (BHSI)")]
    return (f"On {p['date']}, the Baltic Dry Index closed with " + parts[0] + ". "
            f"By segment: {parts[1]}, {parts[2]}, {parts[3]}, {parts[4]}. "
            "Daily route rates (TCE and spot) and the week-over-week trend are tabulated below, "
            "followed by the market commentary and per-shiptype outlook.")


def head(p: dict, site: str, url_path: str) -> str:
    idx = p["indices"]["BDI"]
    bdi_val = int(idx.get("val") or 0)
    bdi_ps = idx.get("pct_str", "")
    title = f"干散货市场日报 {p['date']} · BDI {bdi_val} ({bdi_ps}) | {BRAND}"
    desc = (f"{p['date']} 波罗的海干散货指数 BDI {bdi_val}（{bdi_ps}）；"
            f"海岬型 BCI {int(p['indices']['BCI'].get('val') or 0)}、"
            f"巴拿马型 BPI {int(p['indices']['BPI'].get('val') or 0)}。"
            "含各船型 TCE、主要航线运费、5 日走势与市场解读。NAVGreen 航运商业操作系统。")
    url = site + url_path
    kw = "BDI,波罗的海干散货指数,干散货运费,海岬型,巴拿马型,灵便型,TCE,Baltic Dry Index,dry bulk freight,capesize rates"

    dataset = {
        "@context": "https://schema.org", "@type": "Dataset",
        "name": f"干散货市场日报 {p['date']}", "description": strip_tags(desc),
        "url": url, "inLanguage": ["zh-CN", "en"],
        "dateModified": p["date"],
        "creator": {"@type": "Organization", "name": BRAND, "url": site + "/"},
        "variableMeasured": [
            {"@type": "PropertyValue", "name": k, "value": v.get("val")}
            for k, v in p["indices"].items()
        ],
        "isAccessibleForFree": True,
    }
    article = {
        "@context": "https://schema.org", "@type": "NewsArticle",
        "headline": strip_tags(title),
        "description": strip_tags(desc),
        "datePublished": p["date"] + "T09:00:00+08:00",
        "dateModified": p["date"] + "T09:00:00+08:00",
        "inLanguage": "zh-CN", "mainEntityOfPage": url,
        "author": {"@type": "Organization", "name": BRAND},
        "publisher": {"@type": "Organization", "name": BRAND,
                      "logo": {"@type": "ImageObject", "url": site + "/logo.png"}},
    }
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}"/>
<meta name="keywords" content="{esc(kw)}"/>
<meta name="robots" content="index, follow"/>
<link rel="canonical" href="{esc(url)}"/>
<link rel="alternate" hreflang="zh" href="{esc(url)}"/>
<link rel="alternate" hreflang="en" href="{esc(url)}"/>
<link rel="alternate" hreflang="x-default" href="{esc(url)}"/>
<meta property="og:type" content="article"/>
<meta property="og:title" content="{esc(title)}"/>
<meta property="og:description" content="{esc(desc)}"/>
<meta property="og:url" content="{esc(url)}"/>
<meta property="og:site_name" content="{BRAND}"/>
<meta property="og:image" content="{esc(site)}/og-image.png"/>
<meta name="twitter:card" content="summary_large_image"/>
<meta name="twitter:title" content="{esc(title)}"/>
<meta name="twitter:description" content="{esc(desc)}"/>
<script type="application/ld+json">{json.dumps(dataset, ensure_ascii=False)}</script>
<script type="application/ld+json">{json.dumps(article, ensure_ascii=False)}</script>
<style>{CSS}</style>
</head>"""


CSS = """
*{box-sizing:border-box}body{margin:0;background:#070c0a;color:#cfe0d8;
font-family:ui-sans-serif,system-ui,'PingFang SC','Microsoft YaHei',sans-serif;line-height:1.6}
a{color:#10B981;text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px}
header.site{border-bottom:1px solid #18241f;background:#070c0a;position:sticky;top:0;z-index:5}
header.site .wrap{display:flex;justify-content:space-between;align-items:center;padding:14px 20px}
.brand{font-weight:800;font-size:20px;color:#fff}.brand span{color:#10B981}
.hero{padding:40px 0 24px;border-bottom:1px solid #18241f}
.kicker{font-size:12px;letter-spacing:.18em;color:#10B981;font-family:ui-monospace,monospace;text-transform:uppercase}
h1{font-size:34px;line-height:1.15;color:#fff;margin:10px 0 6px;letter-spacing:-.5px}
.sub{color:#8aa399;font-style:italic}
.headline{margin-top:10px;color:#cfe0d8;font-family:ui-monospace,monospace;font-size:14px}
.en{background:#0c1510;border:1px solid #18241f;border-radius:8px;padding:16px 18px;margin:22px 0;color:#aebfb7;font-size:15px}
.en b{color:#fff}
h2{font-size:13px;letter-spacing:.16em;color:#7f968d;font-family:ui-monospace,monospace;text-transform:uppercase;
margin:38px 0 14px;padding-bottom:8px;border-bottom:1px solid #18241f}
.cards{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
.card{background:#0c1510;border:1px solid #18241f;border-radius:8px;padding:14px}
.card .nm{font-size:12px;color:#8aa399}.card .v{font-size:26px;font-weight:800;color:#fff;margin:4px 0}
.up{color:#10B981}.dn{color:#f97066}.neu{color:#8aa399}
table{width:100%;border-collapse:collapse;font-size:14px;margin:6px 0 4px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid #14201b}
th{color:#7f968d;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums}
.sl{color:#6f857c;font-size:12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:28px}
.note{border-left:3px solid #10B981;padding:10px 14px;margin:10px 0;background:#0c1510;border-radius:0 6px 6px 0}
.note.bear{border-color:#f97066}.note.neu{border-color:#5b7,opacity:.9}
.note .tag{font-size:11px;font-family:ui-monospace,monospace;letter-spacing:.06em;color:#8aa399;display:block;margin-bottom:4px}
.views{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.view{background:#0c1510;border:1px solid #18241f;border-radius:8px;padding:16px}
.view .st{font-size:11px;color:#7f968d;font-family:ui-monospace,monospace}
.view .ti{font-size:18px;font-weight:700;color:#10B981;margin:6px 0}
.related{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}
.related a{background:#0c1510;border:1px solid #18241f;border-radius:6px;padding:10px 14px;color:#cfe0d8;font-size:14px}
.related a:hover{border-color:#10B981;text-decoration:none}
footer.site{border-top:1px solid #18241f;margin-top:46px;padding:24px 0;color:#6f857c;font-size:13px}
@media(max-width:760px){.cards{grid-template-columns:repeat(2,1fr)}.views,.grid2{grid-template-columns:1fr}h1{font-size:26px}}
"""


def card(d: dict) -> str:
    v = d.get("val")
    return (f'<div class="card"><div class="nm">{esc(d.get("label",""))}</div>'
            f'<div class="v">{int(v) if v is not None else "—"}</div>'
            f'<div class="{dir_class(d.get("direction"))}">{esc(d.get("pct_str","—"))} '
            f'({"+" if (d.get("diff") or 0)>=0 else ""}{d.get("diff","")})</div></div>')


def route_rows(p: dict, unit: str) -> str:
    rows = ""
    for k, r in p["routes"].items():
        if r.get("unit") != unit:
            continue
        rows += (f'<tr><td>{esc(r["label"])}<div class="sl">{esc(r.get("sublabel",""))}</div></td>'
                 f'<td class="r">{fmt_num(r.get("val"), unit)}</td>'
                 f'<td class="r {dir_class(r.get("direction"))}">{esc(r.get("pct_str","—"))}</td></tr>')
    return rows


def build_html(p: dict, site: str, prev_date: str | None) -> str:
    date = p["date"]
    url_path = f"/reports/bdi-market/{date}/"
    cards = "".join(card(p["indices"][k]) for k in ["BDI", "BCI", "BPI", "BSI", "BHSI"])

    trend_rows = "".join(
        f'<tr><td>{esc(t["date"])}</td><td class="r">{int(t["BDI"])}</td>'
        f'<td class="r">{int(t["BCI"])}</td><td class="r">{int(t["BPI"])}</td></tr>'
        for t in p.get("trend", []))
    week_rows = "".join(
        f'<tr><td>{esc(k)}</td><td class="r {dir_class("up" if (v.get("pct") or 0)>=0 else "dn")}">{esc(v.get("pct_str","—"))}</td></tr>'
        for k, v in p.get("week_chg", {}).items())
    analysis = "".join(
        f'<div class="note {esc(a.get("type","neu"))}"><span class="tag">{esc(a.get("tag",""))}</span>{a.get("text","")}</div>'
        for a in p.get("market_analysis", []))
    views = "".join(
        f'<div class="view"><div class="st">{esc(v.get("shiptype",""))}</div>'
        f'<div class="ti">{esc(v.get("title",""))}</div><div>{esc(v.get("text",""))}</div></div>'
        for v in p.get("views", []))
    related = "".join(f'<a href="{site}{href}">{esc(zh)} · {esc(en)}</a>' for href, zh, en in NAV_LINKS)
    prev_link = (f'<a href="{site}/reports/bdi-market/{prev_date}/">← 前一日 {prev_date}</a> · '
                 if prev_date else "")
    fuel = p.get("fuel") or {}
    fuel_html = ""
    if fuel:
        fuel_html = (
            '<h2>船用燃油 · Bunker Fuel (USD/t)</h2><table><thead><tr>'
            '<th>品种</th><th class="r">均价</th></tr></thead><tbody>'
            f'<tr><td>IFO380</td><td class="r">${fuel.get("ifo380_avg","—")}</td></tr>'
            f'<tr><td>VLSFO</td><td class="r">${fuel.get("vlsfo_avg","—")}</td></tr>'
            f'<tr><td>MGO</td><td class="r">${fuel.get("mgo_avg","—")}</td></tr>'
            f'</tbody></table>')

    return head(p, site, url_path) + f"""
<body>
<header class="site"><div class="wrap">
  <a class="brand" href="{site}/">NAV<span>Green</span></a>
  <nav><a href="{site}/reports/bdi-market/">市场日报 Reports</a></nav>
</div></header>
<main class="wrap">
  <div class="hero">
    <div class="kicker">DRY BULK MARKET · DAILY REPORT · {esc(date)}</div>
    <h1>干散货市场日报</h1>
    <div class="sub">Baltic Exchange Daily Briefing — {esc(date)}</div>
    <div class="headline">{esc(p.get("headline",""))}</div>
  </div>

  <div class="en" lang="en">{esc(en_summary(p))}</div>

  <h2>市场指数概览 · Market Indices</h2>
  <div class="cards">{cards}</div>

  <div class="grid2" style="margin-top:24px">
    <div>
      <h2 style="margin-top:0">期租 TCE ($/天) · Time-Charter</h2>
      <table><thead><tr><th>航线 Route</th><th class="r">运费</th><th class="r">涨跌</th></tr></thead>
      <tbody>{route_rows(p,"day")}</tbody></table>
      <h2>现货运价 ($/吨) · Spot</h2>
      <table><thead><tr><th>航线 Route</th><th class="r">运价</th><th class="r">涨跌</th></tr></thead>
      <tbody>{route_rows(p,"ton")}</tbody></table>
    </div>
    <div>
      <h2 style="margin-top:0">近 5 日走势 · 5-Day Trend</h2>
      <table><thead><tr><th>日期</th><th class="r">BDI</th><th class="r">BCI</th><th class="r">BPI</th></tr></thead>
      <tbody>{trend_rows}</tbody></table>
      <h2>周环比 · Week-over-Week</h2>
      <table><thead><tr><th>指数</th><th class="r">5 日变化</th></tr></thead>
      <tbody>{week_rows}</tbody></table>
    </div>
  </div>

  <h2>今日市场驱动因素 · Market Drivers</h2>
  {analysis}

  <h2>分船型市场观点 · By Shiptype</h2>
  <div class="views">{views}</div>

  {fuel_html}

  <h2>延伸阅读 · From NAVGreen</h2>
  <div class="related">{related}</div>

  <h2>更多 · More Reports</h2>
  <p>{prev_link}<a href="{site}/reports/bdi-market/">全部干散货市场日报 →</a></p>
</main>
<footer class="site"><div class="wrap">
  数据来源 Sources: {esc(" / ".join(p.get("sources", [])))}。
  本报告由 NAVGreen 自动生成，仅供参考，不构成投资建议。
  © {datetime.date.today().year} {BRAND} · <a href="{site}/">www.navgreen.cn</a>
</div></footer>
</body></html>"""


# ──────────────────────────────────────────────────────────────────────────────
# Generic section-based renderer — for report types other than bdi-market
# (port-congestion / sea-conditions / cyclone / bunker-fuel). The JSON carries a
# list of `sections`, each rendered by kind: cards | table | notes | views | stat.
# ──────────────────────────────────────────────────────────────────────────────

def head_generic(p: dict, site: str, url_path: str) -> str:
    url = site + url_path
    title = f"{p['type_zh']} {p['date']} | {BRAND}"
    desc = p.get("summary_zh") or f"{p['type_zh']}（{p.get('type_en','')}）{p['date']}：{p.get('headline','')}。NAVGreen 航运商业操作系统。"
    kw = p.get("keywords", "")
    dataset = {
        "@context": "https://schema.org", "@type": "Dataset",
        "name": f"{p['type_zh']} {p['date']}", "description": strip_tags(desc)[:300],
        "url": url, "inLanguage": ["zh-CN", "en"], "dateModified": p["date"],
        "creator": {"@type": "Organization", "name": BRAND, "url": site + "/"},
        "isAccessibleForFree": True,
    }
    article = {
        "@context": "https://schema.org", "@type": "NewsArticle",
        "headline": strip_tags(title), "description": strip_tags(desc)[:300],
        "datePublished": p["date"] + "T09:00:00+08:00",
        "dateModified": p["date"] + "T09:00:00+08:00",
        "inLanguage": "zh-CN", "mainEntityOfPage": url,
        "author": {"@type": "Organization", "name": BRAND},
        "publisher": {"@type": "Organization", "name": BRAND,
                      "logo": {"@type": "ImageObject", "url": site + "/logo.png"}},
    }
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}"/>
<meta name="keywords" content="{esc(kw)}"/>
<meta name="robots" content="index, follow"/>
<link rel="canonical" href="{esc(url)}"/>
<link rel="alternate" hreflang="zh" href="{esc(url)}"/>
<link rel="alternate" hreflang="en" href="{esc(url)}"/>
<link rel="alternate" hreflang="x-default" href="{esc(url)}"/>
<meta property="og:type" content="article"/>
<meta property="og:title" content="{esc(title)}"/>
<meta property="og:description" content="{esc(desc)}"/>
<meta property="og:url" content="{esc(url)}"/>
<meta property="og:site_name" content="{BRAND}"/>
<meta property="og:image" content="{esc(site)}/og-image.png"/>
<meta name="twitter:card" content="summary_large_image"/>
<script type="application/ld+json">{json.dumps(dataset, ensure_ascii=False)}</script>
<script type="application/ld+json">{json.dumps(article, ensure_ascii=False)}</script>
<style>{CSS}</style>
</head>"""


def _render_section(s: dict) -> str:
    title = f'<h2>{esc(s.get("title_zh",""))}{(" · " + s["title_en"]) if s.get("title_en") else ""}</h2>'
    kind = s.get("kind")
    if kind == "cards":
        cards = "".join(
            f'<div class="card"><div class="nm">{esc(i.get("label",""))}</div>'
            f'<div class="v">{esc(i.get("value",""))}</div>'
            f'<div class="{i.get("direction","neu")}">{esc(i.get("sub",""))}</div></div>'
            for i in s.get("items", []))
        return f'{title}<div class="cards">{cards}</div>'
    if kind == "table":
        head = "".join(
            f'<th class="{"r" if c.get("num") else ""}">{esc(c.get("zh",""))}'
            f'{("<br><span class=sl>"+esc(c["en"])+"</span>") if c.get("en") else ""}</th>'
            for c in s.get("columns", []))
        rows = ""
        for row in s.get("rows", []):
            rows += "<tr>" + "".join(
                (f'<td class="r {c.get("cls","")}">{esc(c.get("v",""))}</td>' if isinstance(c, dict)
                 else f"<td>{esc(c)}</td>")
                for c in row) + "</tr>"
        return f'{title}<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>'
    if kind == "notes":
        notes = "".join(
            f'<div class="note {i.get("type","neu")}"><span class="tag">{esc(i.get("tag",""))}</span>{i.get("text","")}</div>'
            for i in s.get("items", []))
        return f"{title}{notes}"
    if kind == "views":
        vs = "".join(
            f'<div class="view"><div class="st">{esc(i.get("label",""))}</div>'
            f'<div class="ti">{esc(i.get("title",""))}</div><div>{esc(i.get("text",""))}</div></div>'
            for i in s.get("items", []))
        return f'{title}<div class="views">{vs}</div>'
    return ""


def render_generic(p: dict, site: str, prev_date: str | None) -> str:
    slug = p["slug"]
    date = p["date"]
    url_path = f"/reports/{slug}/{date}/"
    sections = "".join(_render_section(s) for s in p.get("sections", []))
    related = "".join(f'<a href="{site}{href}">{esc(zh)} · {esc(en)}</a>' for href, zh, en in NAV_LINKS)
    prev_link = (f'<a href="{site}/reports/{slug}/{prev_date}/">← 前一日 {prev_date}</a> · '
                 if prev_date else "")
    en_block = f'<div class="en" lang="en">{esc(p.get("summary_en",""))}</div>' if p.get("summary_en") else ""
    return head_generic(p, site, url_path) + f"""
<body>
<header class="site"><div class="wrap">
  <a class="brand" href="{site}/">NAV<span>Green</span></a>
  <nav><a href="{site}/reports/{slug}/">{esc(p['type_zh'])}</a></nav>
</div></header>
<main class="wrap">
  <div class="hero">
    <div class="kicker">{esc(p.get('type_en','').upper())} · {esc(date)}</div>
    <h1>{esc(p['type_zh'])}</h1>
    <div class="sub">{esc(p.get('type_en',''))} — {esc(date)}</div>
    <div class="headline">{esc(p.get('headline',''))}</div>
  </div>
  {en_block}
  {sections}
  <h2>延伸阅读 · From NAVGreen</h2>
  <div class="related">{related}</div>
  <h2>更多 · More</h2>
  <p>{prev_link}<a href="{site}/reports/{slug}/">全部{esc(p['type_zh'])} →</a></p>
</main>
<footer class="site"><div class="wrap">
  数据来源 Sources: {esc(" / ".join(p.get("sources", [])))}。
  本报告由 NAVGreen 自动生成，仅供参考，不构成投资/航行决策建议。
  © {datetime.date.today().year} {BRAND} · <a href="{site}/">www.navgreen.cn</a>
</div></footer>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data_json")
    ap.add_argument("out_dir")
    ap.add_argument("--site", default=SITE_DEFAULT)
    ap.add_argument("--prev", default=None)
    args = ap.parse_args()

    p = json.loads(pathlib.Path(args.data_json).read_text(encoding="utf-8"))
    html_out = build_html(p, args.site.rstrip("/"), args.prev)

    out = pathlib.Path(args.out_dir) / "reports" / "bdi-market" / p["date"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(html_out, encoding="utf-8")
    print(f"  ✓ {out / 'index.html'}  ({len(strip_tags(html_out))} chars text)")


if __name__ == "__main__":
    main()
