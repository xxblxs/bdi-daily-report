#!/usr/bin/env python3
"""
Build the report HUB pages + sitemap-reports.xml from all generated reports.

Scans seo_pages/data/<type>/*.json, regenerates each report page (via generator),
writes a hub page per type (/reports/<type>/) listing all dates newest-first, a top
/reports/ landing, and sitemap-reports.xml covering every report + hub URL.

Usage: python build_index.py <out_dir> [--site https://www.navgreen.cn]
"""
import argparse
import datetime
import json
import pathlib
import sys

# Make `generator` importable whether run as `python seo_pages/build_index.py`
# (from repo root) or from inside seo_pages/.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import generator  # noqa: E402

DATA_ROOT = pathlib.Path(__file__).resolve().parent / "data"
TYPES = {
    "bdi-market":      ("干散货市场日报", "Dry Bulk Market Daily"),
    "port-congestion": ("干散货全球港口拥堵日报", "Global Port Congestion Report"),
    "sea-conditions":  ("全球主要航线海况日报", "Global Sea Conditions Report"),
    "cyclone":         ("全球热带气旋预警日报", "Global Tropical Cyclone Alert"),
    "bunker-fuel":     ("全球船用燃油日报", "Global Bunker Fuel Report"),
}

HUB_CSS = generator.CSS


def hub_page(site, type_slug, zh, en, dates):
    items = "".join(
        f'<li><a href="{site}/reports/{type_slug}/{d}/">{zh} · {d}</a></li>' for d in dates)
    url = f"{site}/reports/{type_slug}/"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{zh} · 历史归档 | NAVGreen</title>
<meta name="description" content="{zh}（{en}）每日归档：BDI、各船型 TCE、主要航线运费与市场解读。NAVGreen 航运商业操作系统。"/>
<link rel="canonical" href="{url}"/>
<link rel="alternate" hreflang="zh" href="{url}"/><link rel="alternate" hreflang="en" href="{url}"/>
<link rel="alternate" hreflang="x-default" href="{url}"/>
<style>{HUB_CSS} ul{{list-style:none;padding:0}} li{{border-bottom:1px solid #14201b;padding:12px 0}}</style>
</head><body>
<header class="site"><div class="wrap"><a class="brand" href="{site}/">NAV<span>Green</span></a>
<nav><a href="{site}/reports/">全部日报</a></nav></div></header>
<main class="wrap"><div class="hero"><div class="kicker">REPORTS · ARCHIVE</div>
<h1>{zh}</h1><div class="sub">{en} — 每日更新归档</div></div>
<h2>历史报告 · {len(dates)} 期</h2><ul>{items}</ul></main>
<footer class="site"><div class="wrap">© {datetime.date.today().year} NAVGreen · <a href="{site}/">www.navgreen.cn</a></div></footer>
</body></html>"""


def landing_page(site, type_links):
    cards = "".join(
        f'<div class="view"><div class="ti">{zh}</div><div>{en}</div>'
        f'<p><a href="{site}/reports/{slug}/">查看归档 →</a></p></div>'
        for slug, (zh, en) in type_links)
    url = f"{site}/reports/"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>航运市场日报 · Maritime Market Reports | NAVGreen</title>
<meta name="description" content="NAVGreen 每日航运市场数据报告：干散货 BDI、港口拥堵、航线海况、船用燃油、台风路径。数据驱动的航运决策。"/>
<link rel="canonical" href="{url}"/>
<style>{HUB_CSS}</style></head><body>
<header class="site"><div class="wrap"><a class="brand" href="{site}/">NAV<span>Green</span></a></div></header>
<main class="wrap"><div class="hero"><div class="kicker">MARITIME MARKET REPORTS</div>
<h1>航运市场日报</h1><div class="sub">Daily, data-driven maritime market intelligence</div></div>
<div class="views" style="margin-top:24px">{cards}</div></main>
<footer class="site"><div class="wrap">© {datetime.date.today().year} NAVGreen · <a href="{site}/">www.navgreen.cn</a></div></footer>
</body></html>"""


def sitemap(site, urls):
    today = datetime.date.today().isoformat()
    body = "".join(
        f"  <url><loc>{u}</loc><lastmod>{m}</lastmod>"
        f"<changefreq>daily</changefreq><priority>{p}</priority></url>\n"
        for u, m, p in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{body}</urlset>\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--site", default=generator.SITE_DEFAULT)
    args = ap.parse_args()
    site = args.site.rstrip("/")
    out = pathlib.Path(args.out_dir)

    sm_urls = [(f"{site}/reports/", datetime.date.today().isoformat(), "0.8")]
    type_links = []

    for slug, (zh, en) in TYPES.items():
        d = DATA_ROOT / slug
        files = sorted(d.glob("*.json"), reverse=True) if d.exists() else []
        dates = []
        for i, f in enumerate(files):
            p = json.loads(f.read_text(encoding="utf-8"))
            prev = p["date"] if i + 1 < len(files) else None  # newer→older; prev is the next file
            prev_date = json.loads(files[i + 1].read_text(encoding="utf-8"))["date"] if i + 1 < len(files) else None
            page = (generator.build_html(p, site, prev_date) if slug == "bdi-market"
                    else generator.render_generic(p, site, prev_date))
            pd = out / "reports" / slug / p["date"]
            pd.mkdir(parents=True, exist_ok=True)
            (pd / "index.html").write_text(page, encoding="utf-8")
            dates.append(p["date"])
            sm_urls.append((f"{site}/reports/{slug}/{p['date']}/", p["date"], "0.7"))
        # hub
        hub_dir = out / "reports" / slug
        hub_dir.mkdir(parents=True, exist_ok=True)
        (hub_dir / "index.html").write_text(hub_page(site, slug, zh, en, dates), encoding="utf-8")
        sm_urls.append((f"{site}/reports/{slug}/", datetime.date.today().isoformat(), "0.8"))
        type_links.append((slug, (zh, en)))

    # landing + sitemap
    land = out / "reports"
    land.mkdir(parents=True, exist_ok=True)
    (land / "index.html").write_text(landing_page(site, type_links), encoding="utf-8")
    (out / "sitemap-reports.xml").write_text(sitemap(site, sm_urls), encoding="utf-8")
    print(f"  ✓ hub + landing + sitemap-reports.xml ({len(sm_urls)} urls)")


if __name__ == "__main__":
    main()
