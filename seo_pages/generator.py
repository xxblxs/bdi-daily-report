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
    title_en = f"Dry Bulk Market Daily {p['date']} · BDI {bdi_val} ({bdi_ps}) | {BRAND}"
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
<html lang="zh-CN" data-lang="zh" data-title-zh="{esc(title)}" data-title-en="{esc(title_en)}">
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
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');
*{box-sizing:border-box}body{margin:0;background:#050A14;color:#F1F5F9;
font-family:'Inter',ui-sans-serif,system-ui,'PingFang SC','Microsoft YaHei',sans-serif;line-height:1.6}
a{color:#00F0A0;text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px}
header.site{border-bottom:1px solid rgba(255,255,255,0.08);background:#050A14;position:sticky;top:0;z-index:5}
header.site .wrap{display:flex;justify-content:space-between;align-items:center;padding:14px 20px}
.brand{font-weight:800;font-size:20px;color:#fff}.brand span{color:#00F0A0}
.hero{padding:40px 0 24px;border-bottom:1px solid rgba(255,255,255,0.08)}
.kicker{font-size:12px;letter-spacing:.18em;color:#00F0A0;font-family:ui-monospace,monospace;text-transform:uppercase}
h1{font-size:34px;line-height:1.15;color:#fff;margin:10px 0 6px;letter-spacing:-.5px}
.sub{color:#94A3B8;font-style:italic}
.headline{margin-top:10px;color:#F1F5F9;font-family:ui-monospace,monospace;font-size:14px}
.en{background:#0F172A;border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:16px 18px;margin:22px 0;color:#CBD5E1;font-size:15px}
.en b{color:#fff}
h2{font-size:13px;letter-spacing:.16em;color:#64748B;font-family:ui-monospace,monospace;text-transform:uppercase;
margin:38px 0 14px;padding-bottom:8px;border-bottom:1px solid rgba(255,255,255,0.08)}
.cards{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
.card{background:#0F172A;border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:14px}
.card .nm{font-size:12px;color:#94A3B8}.card .v{font-size:26px;font-weight:800;color:#fff;margin:4px 0}
.up{color:#00F0A0}.dn{color:#f97066}.neu{color:#94A3B8}
table{width:100%;border-collapse:collapse;font-size:14px;margin:6px 0 4px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid #14201b}
th{color:#64748B;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums}
.sl{color:#64748B;font-size:12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:28px}
.note{border-left:3px solid #00F0A0;padding:10px 14px;margin:10px 0;background:#0F172A;border-radius:0 6px 6px 0}
.note.bear{border-color:#f97066}.note.neu{border-color:#5b7,opacity:.9}
.note .tag{font-size:11px;font-family:ui-monospace,monospace;letter-spacing:.06em;color:#94A3B8;display:block;margin-bottom:4px}
.views{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.view{background:#0F172A;border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:16px}
.view .st{font-size:11px;color:#64748B;font-family:ui-monospace,monospace}
.view .ti{font-size:18px;font-weight:700;color:#00F0A0;margin:6px 0}
.related{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}
.related a{background:#0F172A;border:1px solid rgba(255,255,255,0.08);border-radius:6px;padding:10px 14px;color:#F1F5F9;font-size:14px}
.related a:hover{border-color:#00F0A0;text-decoration:none}
footer.site{border-top:1px solid rgba(255,255,255,0.08);margin-top:46px;padding:24px 0;color:#64748B;font-size:13px}
@media(max-width:760px){.cards{grid-template-columns:repeat(2,1fr)}.views,.grid2{grid-template-columns:1fr}h1{font-size:26px}}
/* bilingual toggle: show only the active language */
html[data-lang="zh"] .len{display:none}
html[data-lang="en"] .lzh{display:none}
/* site header — matches navgreen-home */
.nav{display:flex;align-items:center;gap:34px}
.nav .nitem{display:flex;flex-direction:column;align-items:center;line-height:1.1;color:#F1F5F9;font-size:15px;font-weight:500}
.nav .nitem .sub{font-size:9px;letter-spacing:.14em;color:#64748B;font-family:'JetBrains Mono',ui-monospace,monospace;text-transform:uppercase;margin-top:3px}
.nav .nitem:hover{color:#00F0A0}
.nav .nitem:hover .sub{color:#00F0A0}
.divider{width:1px;height:28px;background:rgba(255,255,255,0.08)}
.login{color:#F1F5F9;font-size:14px}.login:hover{color:#00F0A0}
.demo{background:#00F0A0;color:#06120d;font-weight:700;font-size:14px;padding:9px 18px;border-radius:4px}
.demo:hover{background:#fff;text-decoration:none}
.langsw{display:inline-flex;align-items:center;gap:7px;padding:6px 12px;border-radius:9999px;border:1px solid rgba(255,255,255,0.1);background:rgba(255,255,255,0.05)}
.langsw:hover{border-color:rgba(0,240,160,0.3)}
.langsw .gl{width:14px;height:14px;color:#94A3B8}
.langsw button{background:none;border:0;padding:0;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:12px;letter-spacing:.08em;color:#94A3B8;cursor:pointer}
.langsw button.on{color:#fff}
.langsw .sep{color:rgba(255,255,255,0.2);font-size:12px}
/* site footer — matches navgreen-home */
.foot{border-top:1px solid rgba(255,255,255,0.08);margin-top:64px;padding:56px 0 28px}
.foot .cols{display:grid;grid-template-columns:2fr 1fr 1fr 1.4fr;gap:32px}
.foot .tag{color:#94A3B8;font-size:14px;font-weight:300;max-width:320px;margin:16px 0 10px}
.foot .loc{color:#64748B;font-size:13px}
.foot h4{color:#fff;font-size:11px;font-family:'JetBrains Mono',ui-monospace,monospace;letter-spacing:.18em;margin:0 0 18px}
.foot ul{list-style:none;padding:0;margin:0}.foot li{margin-bottom:14px}
.foot a.fl{color:#94A3B8;font-size:14px;font-weight:300}.foot a.fl:hover{color:#00F0A0;text-decoration:none}
.foot .sub-in{display:flex;gap:0;margin-top:4px}
.foot .sub-in input{flex:1;background:#0F172A;border:1px solid rgba(255,255,255,0.08);border-right:0;color:#F1F5F9;padding:10px 12px;border-radius:4px 0 0 4px;font-family:inherit;font-size:13px}
.foot .sub-in a{background:#00F0A0;color:#06120d;padding:0 16px;display:flex;align-items:center;border-radius:0 4px 4px 0;font-weight:700}
.foot .botbar{display:flex;justify-content:space-between;align-items:center;margin-top:44px;padding-top:22px;border-top:1px solid rgba(255,255,255,0.08);color:#64748B;font-size:12px;font-family:'JetBrains Mono',ui-monospace,monospace;letter-spacing:.05em}
.foot .botbar .lk{display:flex;gap:26px}.foot .botbar a{color:#64748B}.foot .botbar a:hover{color:#fff;text-decoration:none}
@media(max-width:760px){.nav{display:none}.foot .cols{grid-template-columns:1fr 1fr}.foot .botbar{flex-direction:column;gap:12px}}
"""

# Globe EN/CN pill — matches navgreen-home's LanguageSwitcher. Buttons drive the toggle JS.
_GLOBE = ('<svg class="gl" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
          'stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/>'
          '<path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>')
LANG_SWITCH = ('<div class="langsw" role="group" aria-label="language">' + _GLOBE +
               '<button type="button" data-l="en">EN</button>'
               '<span class="sep">/</span>'
               '<button type="button" data-l="zh">CN</button></div>')

LANG_JS = """<script>(function(){
var L=localStorage.getItem('ng_lang')||'zh';
function set(l){var d=document.documentElement;d.setAttribute('data-lang',l);localStorage.setItem('ng_lang',l);
document.querySelectorAll('.langsw button').forEach(function(b){b.classList.toggle('on',b.dataset.l===l)});
var t=d.getAttribute('data-title-'+l);if(t)document.title=t;}
document.querySelectorAll('.langsw button').forEach(function(b){b.addEventListener('click',function(){set(b.dataset.l)})});
set(L);})();</script>"""


def _nitem(href, zh, en, sub):
    return (f'<a class="nitem" href="{href}"><span>'
            f'<span class="lzh">{esc(zh)}</span><span class="len">{esc(en)}</span></span>'
            f'<span class="sub">{esc(sub)}</span></a>')


def site_header(site: str) -> str:
    """Header matching navgreen-home: logo + nav + EN/CN pill + Login + Book a Demo."""
    nav = (
        _nitem(f"{site}/", "数据洞察", "Insights", "INSIGHTS")
        + _nitem(f"{site}/", "核心技术", "Technology", "TECHNOLOGY")
        + _nitem(f"{site}/blog", "资源", "Resources", "RESOURCES")
        + _nitem(f"{site}/reports/", "市场日报", "Reports", "REPORTS")
    )
    return (
        '<header class="site"><div class="wrap">'
        f'<a class="brand" href="{site}/">NAV<span>Green</span></a>'
        f'<div style="display:flex;align-items:center;gap:24px">'
        f'<div class="nav">{nav}</div>'
        '<div class="divider"></div>'
        f'{LANG_SWITCH}'
        f'<a class="login" href="https://vip.navgreen.cn"><span class="lzh">登录</span><span class="len">Login</span></a>'
        f'<a class="demo" href="{site}/"><span class="lzh">预约演示 →</span><span class="len">Book a Demo →</span></a>'
        '</div></div></header>'
    )


def _fl(href, zh, en):
    return f'<li><a class="fl" href="{href}"><span class="lzh">{esc(zh)}</span><span class="len">{esc(en)}</span></a></li>'


def site_footer(site: str) -> str:
    """Footer matching navgreen-home: brand + Solutions/Resources/Subscribe + bottom bar."""
    yr = datetime.date.today().year
    return (
        '<footer class="foot"><div class="wrap"><div class="cols">'
        '<div>'
        f'<a class="brand" href="{site}/">NAV<span>Green</span></a>'
        '<p class="tag"><span class="lzh">通过预测智能与自动化金融结算，开创海事物流的未来。</span>'
        '<span class="len">Pioneering the future of maritime logistics through predictive intelligence and automated financial settlements.</span></p>'
        '<p class="loc"><span class="lzh">北京 · 新加坡 · 鹿特丹</span><span class="len">Beijing · Singapore · Rotterdam</span></p>'
        '</div>'
        '<div><h4><span class="lzh">解决方案</span><span class="len">SOLUTIONS</span></h4><ul>'
        + _fl(f"{site}/solutions/freight-prediction-bulk-carriers", "运费预测", "Freight Prediction")
        + _fl(f"{site}/solutions/demurrage-defense-charterers", "滞期抗辩", "Demurrage Defense")
        + '</ul></div>'
        '<div><h4><span class="lzh">资源</span><span class="len">RESOURCES</span></h4><ul>'
        + _fl(f"{site}/blog", "航运洞察", "Insights")
        + _fl(f"{site}/cases", "客户案例", "Case Studies")
        + _fl(f"{site}/reports/", "市场日报", "Market Reports")
        + '</ul></div>'
        '<div><h4><span class="lzh">订阅</span><span class="len">SUBSCRIBE</span></h4>'
        '<div class="sub-in"><input type="email" placeholder="Email Address" aria-label="email"/>'
        f'<a href="{site}/" aria-label="subscribe">→</a></div></div>'
        '</div>'
        '<div class="botbar">'
        f'<span>© {yr} <span class="lzh">NAVGreen 科技. 保留所有权利。</span><span class="len">NAVGreen Technology. All rights reserved.</span></span>'
        '<span class="lk">'
        f'<a href="{site}/"><span class="lzh">隐私政策</span><span class="len">Privacy Policy</span></a>'
        f'<a href="{site}/"><span class="lzh">服务条款</span><span class="len">Terms of Service</span></a>'
        f'<a href="{site}/sitemap.xml"><span class="lzh">网站地图</span><span class="len">Sitemap</span></a>'
        '</span></div></div></footer>'
    )


def biL(v) -> str:
    """Render a bilingual field. Dict {zh,en} -> two lang spans; str -> plain."""
    if isinstance(v, dict) and ("zh" in v or "en" in v):
        return (f'<span class="lzh">{esc(v.get("zh",""))}</span>'
                f'<span class="len">{esc(v.get("en",""))}</span>')
    return esc(v)


def bi_title(zh: str, en: str) -> str:
    return f'<span class="lzh">{esc(zh)}</span><span class="len">{esc(en)}</span>'


INDEX_LABELS = {
    "BDI": ("BDI 综合", "BDI Composite"), "BCI": ("BCI 海岬型", "BCI Capesize"),
    "BPI": ("BPI 巴拿马型", "BPI Panamax"), "BSI": ("BSI 灵便型", "BSI Supramax"),
    "BHSI": ("BHSI 小灵便", "BHSI Handysize"),
}


def card(key: str, d: dict) -> str:
    v = d.get("val")
    zh, en = INDEX_LABELS.get(key, (d.get("label", key), d.get("label", key)))
    return (f'<div class="card"><div class="nm">{bi_title(zh, en)}</div>'
            f'<div class="v">{int(v) if v is not None else "—"}</div>'
            f'<div class="{dir_class(d.get("direction"))}">{esc(d.get("pct_str","—"))} '
            f'({"+" if (d.get("diff") or 0)>=0 else ""}{d.get("diff","")})</div></div>')


def en_drivers(p: dict) -> str:
    """Deterministic English market-driver commentary from the indices."""
    idx = p["indices"]
    def s(k):
        d = idx.get(k, {}); ps = d.get("pct_str", "n/a")
        return f"{ps}"
    return (f'<div class="note neu"><span class="tag">Market Drivers</span>'
            f'<strong>Capesize (BCI) {s("BCI")}, Panamax (BPI) {s("BPI")}.</strong> '
            f'Smaller sizes Supramax {s("BSI")} / Handysize {s("BHSI")}. '
            'Movements are driven by iron-ore and coal tonne-mile demand against vessel supply, '
            'port congestion and bunker costs; see the per-shiptype outlook below.</div>')


def en_views(p: dict) -> str:
    """Deterministic English per-shiptype outlook."""
    idx = p["indices"]
    def block(label, k):
        d = idx.get(k, {})
        return (f'<div class="view"><div class="st">{label}</div>'
                f'<div class="ti">{d.get("val","—")} ({d.get("pct_str","—")})</div>'
                f'<div>Daily change {d.get("pct_str","—")}; assess fixtures against the forward curve.</div></div>')
    return (block("CAPESIZE", "BCI") + block("PANAMAX", "BPI")
            + block("SUPRAMAX / HANDYSIZE", "BSI"))


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
    cards = "".join(card(k, p["indices"][k]) for k in ["BDI", "BCI", "BPI", "BSI", "BHSI"])

    trend_rows = "".join(
        f'<tr><td>{esc(t["date"])}</td><td class="r">{int(t["BDI"])}</td>'
        f'<td class="r">{int(t["BCI"])}</td><td class="r">{int(t["BPI"])}</td></tr>'
        for t in p.get("trend", []))
    week_rows = "".join(
        f'<tr><td>{esc(k)}</td><td class="r {dir_class("up" if (v.get("pct") or 0)>=0 else "dn")}">{esc(v.get("pct_str","—"))}</td></tr>'
        for k, v in p.get("week_chg", {}).items())
    analysis_zh = "".join(
        f'<div class="note {esc(a.get("type","neu"))}"><span class="tag">{esc(a.get("tag",""))}</span>{a.get("text","")}</div>'
        for a in p.get("market_analysis", []))
    analysis = f'<div class="lzh">{analysis_zh}</div><div class="len">{en_drivers(p)}</div>'
    views_zh = "".join(
        f'<div class="view"><div class="st">{esc(v.get("shiptype",""))}</div>'
        f'<div class="ti">{esc(v.get("title",""))}</div><div>{esc(v.get("text",""))}</div></div>'
        for v in p.get("views", []))
    views = (f'<div class="lzh"><div class="views">{views_zh}</div></div>'
             f'<div class="len"><div class="views">{en_views(p)}</div></div>')
    related = "".join(f'<a href="{site}{href}">{biL({"zh": zh, "en": en})}</a>' for href, zh, en in NAV_LINKS)
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

    idx = p["indices"]
    zh_sum = (f"{date} BDI {int(idx['BDI'].get('val') or 0)}（{idx['BDI'].get('pct_str','')}）；"
              f"BCI {int(idx['BCI'].get('val') or 0)}、BPI {int(idx['BPI'].get('val') or 0)}、"
              f"BSI {int(idx['BSI'].get('val') or 0)}、BHSI {int(idx['BHSI'].get('val') or 0)}。"
              "下方为各船型 TCE、主要航线运费、5 日走势与市场解读。")
    summary = biRaw({"zh": zh_sum, "en": en_summary(p)})
    prev_lk = (f'<a href="{site}/reports/bdi-market/{prev_date}/">{bi_title("← 前一日 "+prev_date, "← Previous "+prev_date)}</a> · '
               if prev_date else "")

    return head(p, site, url_path) + f"""
<body>
{site_header(site)}
<main class="wrap">
  <div class="hero">
    <div class="kicker">DRY BULK MARKET · DAILY REPORT · {esc(date)}</div>
    <h1>{bi_title('干散货市场日报', 'Dry Bulk Market Daily')}</h1>
    <div class="sub">Baltic Exchange Daily Briefing — {esc(date)}</div>
    <div class="headline">{esc(p.get("headline",""))}</div>
  </div>

  <div class="en">{summary}</div>

  <h2>{bi_title('市场指数概览', 'Market Indices')}</h2>
  <div class="cards">{cards}</div>

  <div class="grid2" style="margin-top:24px">
    <div>
      <h2 style="margin-top:0">{bi_title('期租 TCE ($/天)', 'Time-Charter TCE ($/day)')}</h2>
      <table><thead><tr><th>{bi_title('航线', 'Route')}</th><th class="r">{bi_title('运费', 'Rate')}</th><th class="r">{bi_title('涨跌', 'Chg')}</th></tr></thead>
      <tbody>{route_rows(p,"day")}</tbody></table>
      <h2>{bi_title('现货运价 ($/吨)', 'Spot ($/t)')}</h2>
      <table><thead><tr><th>{bi_title('航线', 'Route')}</th><th class="r">{bi_title('运价', 'Rate')}</th><th class="r">{bi_title('涨跌', 'Chg')}</th></tr></thead>
      <tbody>{route_rows(p,"ton")}</tbody></table>
    </div>
    <div>
      <h2 style="margin-top:0">{bi_title('近 5 日走势', '5-Day Trend')}</h2>
      <table><thead><tr><th>{bi_title('日期', 'Date')}</th><th class="r">BDI</th><th class="r">BCI</th><th class="r">BPI</th></tr></thead>
      <tbody>{trend_rows}</tbody></table>
      <h2>{bi_title('周环比', 'Week-over-Week')}</h2>
      <table><thead><tr><th>{bi_title('指数', 'Index')}</th><th class="r">{bi_title('5 日变化', '5-day')}</th></tr></thead>
      <tbody>{week_rows}</tbody></table>
    </div>
  </div>

  <h2>{bi_title('今日市场驱动因素', 'Market Drivers')}</h2>
  {analysis}

  <h2>{bi_title('分船型市场观点', 'By Shiptype')}</h2>
  {views}

  {fuel_html}

  <h2>{bi_title('延伸阅读', 'From NAVGreen')}</h2>
  <div class="related">{related}</div>

  <h2>{bi_title('更多', 'More')}</h2>
  <p>{prev_lk}<a href="{site}/reports/bdi-market/">{bi_title('全部干散货市场日报 →', 'All Dry Bulk Market Daily →')}</a></p>
  <p style="color:#64748B;font-size:13px;margin-top:28px">
  {bi_title('数据来源', 'Sources')}: {esc(" / ".join(p.get("sources", [])))}。
  {bi_title('本报告由 NAVGreen 自动生成，仅供参考，不构成投资建议。', 'Auto-generated by NAVGreen for reference only; not investment advice.')}</p>
</main>
{site_footer(site)}
{LANG_JS}
</body></html>"""


# ──────────────────────────────────────────────────────────────────────────────
# Generic section-based renderer — for report types other than bdi-market
# (port-congestion / sea-conditions / cyclone / bunker-fuel). The JSON carries a
# list of `sections`, each rendered by kind: cards | table | notes | views | stat.
# ──────────────────────────────────────────────────────────────────────────────

def head_generic(p: dict, site: str, url_path: str) -> str:
    url = site + url_path
    title = f"{p['type_zh']} {p['date']} | {BRAND}"
    title_en = f"{p.get('type_en', p['type_zh'])} {p['date']} | {BRAND}"
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
<html lang="zh-CN" data-lang="zh" data-title-zh="{esc(title)}" data-title-en="{esc(title_en)}">
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


def biRaw(v) -> str:
    """Bilingual field that may contain HTML (e.g. <strong>) — not escaped."""
    if isinstance(v, dict) and ("zh" in v or "en" in v):
        return f'<span class="lzh">{v.get("zh","")}</span><span class="len">{v.get("en","")}</span>'
    return str(v)


def _cell(c) -> str:
    """Table cell: str (neutral) | {v,cls} (neutral value) | {zh,en,cls} (bilingual)."""
    if isinstance(c, dict):
        cls = c.get("cls", "")
        if "zh" in c or "en" in c:
            return f'<td class="r {cls}">{biL(c)}</td>'
        return f'<td class="r {cls}">{esc(c.get("v",""))}</td>'
    return f"<td>{esc(c)}</td>"


def _render_section(s: dict) -> str:
    title = f'<h2>{bi_title(s.get("title_zh",""), s.get("title_en") or s.get("title_zh",""))}</h2>'
    kind = s.get("kind")
    if kind == "cards":
        cards = "".join(
            f'<div class="card"><div class="nm">{biL(i.get("label",""))}</div>'
            f'<div class="v">{esc(i.get("value",""))}</div>'
            f'<div class="{i.get("direction","neu")}">{biL(i.get("sub",""))}</div></div>'
            for i in s.get("items", []))
        return f'{title}<div class="cards">{cards}</div>'
    if kind == "table":
        head = "".join(
            f'<th class="{"r" if c.get("num") else ""}">{esc(c.get("zh",""))}'
            f'{("<br><span class=sl>"+esc(c["en"])+"</span>") if c.get("en") else ""}</th>'
            for c in s.get("columns", []))
        rows = "".join("<tr>" + "".join(_cell(c) for c in row) + "</tr>" for row in s.get("rows", []))
        return f'{title}<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>'
    if kind == "notes":
        notes = "".join(
            f'<div class="note {i.get("type","neu")}"><span class="tag">{biL(i.get("tag",""))}</span>{biRaw(i.get("text",""))}</div>'
            for i in s.get("items", []))
        return f"{title}{notes}"
    if kind == "views":
        vs = "".join(
            f'<div class="view"><div class="st">{biL(i.get("label",""))}</div>'
            f'<div class="ti">{biL(i.get("title",""))}</div><div>{biRaw(i.get("text",""))}</div></div>'
            for i in s.get("items", []))
        return f'{title}<div class="views">{vs}</div>'
    return ""


def render_generic(p: dict, site: str, prev_date: str | None) -> str:
    slug = p["slug"]
    date = p["date"]
    url_path = f"/reports/{slug}/{date}/"
    sections = "".join(_render_section(s) for s in p.get("sections", []))
    related = "".join(f'<a href="{site}{href}">{biL({"zh": zh, "en": en})}</a>' for href, zh, en in NAV_LINKS)
    prev_link = (f'<a href="{site}/reports/{slug}/{prev_date}/">{bi_title("← 前一日 "+prev_date, "← Previous "+prev_date)}</a> · '
                 if prev_date else "")
    summary = biRaw({"zh": p.get("summary_zh", ""), "en": p.get("summary_en", "")}) if (p.get("summary_zh") or p.get("summary_en")) else ""
    return head_generic(p, site, url_path) + f"""
<body>
{site_header(site)}
<main class="wrap">
  <div class="hero">
    <div class="kicker">{esc(p.get('type_en','').upper())} · {esc(date)}</div>
    <h1>{bi_title(p['type_zh'], p.get('type_en', p['type_zh']))}</h1>
    <div class="sub">{esc(p.get('type_en',''))} — {esc(date)}</div>
    <div class="headline">{biRaw(p.get('headline',''))}</div>
  </div>
  {f'<div class="en">{summary}</div>' if summary else ''}
  {sections}
  <h2>{bi_title('延伸阅读', 'From NAVGreen')}</h2>
  <div class="related">{related}</div>
  <h2>{bi_title('更多', 'More')}</h2>
  <p>{prev_link}<a href="{site}/reports/{slug}/">{bi_title('全部' + p['type_zh'] + ' →', 'All ' + p.get('type_en', p['type_zh']) + ' →')}</a></p>
  <p style="color:#64748B;font-size:13px;margin-top:28px">
  {bi_title('数据来源', 'Sources')}: {esc(" / ".join(p.get("sources", [])))}。
  {bi_title('本报告由 NAVGreen 自动生成，仅供参考，不构成投资/航行决策建议。', 'Auto-generated by NAVGreen for reference only; not investment or navigation advice.')}</p>
</main>
{site_footer(site)}
{LANG_JS}
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data_json")
    ap.add_argument("out_dir")
    ap.add_argument("--site", default=SITE_DEFAULT)
    ap.add_argument("--prev", default=None)
    args = ap.parse_args()

    p = json.loads(pathlib.Path(args.data_json).read_text(encoding="utf-8"))
    slug = p.get("slug") or "bdi-market"
    page = (build_html(p, args.site.rstrip("/"), args.prev) if slug == "bdi-market"
            else render_generic(p, args.site.rstrip("/"), args.prev))

    out = pathlib.Path(args.out_dir) / "reports" / slug / p["date"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    print(f"  ✓ {out / 'index.html'}  ({len(strip_tags(page))} chars text)")


if __name__ == "__main__":
    main()
