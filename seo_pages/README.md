# seo_pages — 日报 → 可收录网页（程序化 SEO）

把每日数据报告变成 navgreen.cn 上可被搜索引擎收录的**双语网页**，托管在阿里云 OSS + CDN（`navgreen.cn/reports/...`），与主站 Docker 镜像解耦。

## 流程

```
主脚本(run_once) ──emit_json──▶ seo_pages/data/<type>/<date>.json
                                          │
                build_index.py ───────────┤  生成每日页 + hub + 落地页 + sitemap-reports.xml → out/
                                          ▼
                publish.py ──▶ 上传 OSS + 推送 百度/IndexNow
```

## 文件

| 文件 | 作用 |
|------|------|
| `generator.py` | 单篇 JSON → 双语 SEO HTML（title/canonical/hreflang/OG + Dataset & NewsArticle JSON-LD + 内链 navgreen + 真实文字表格） |
| `emit_json.py` | 在 `run_once()` 里把当日数据写成 JSON（已接入 `bdi_daily_push_add_fuel.py`） |
| `build_index.py` | 扫 `data/` 重建所有页 + hub + 落地 + `sitemap-reports.xml` |
| `publish.py` | OSS 上传 + 百度链接提交 + Bing IndexNow（全部读环境变量；缺凭据自动跳过） |
| `data/` | JSON 归档（入库，历史 hub 的数据源；CI 每日回提交） |

## 本地试跑

```bash
python seo_pages/generator.py seo_pages/sample_data/bdi-market-2026-06-17.json out
python seo_pages/build_index.py out --site https://www.navgreen.cn
open out/reports/bdi-market/2026-06-17/index.html
```

## 需要的 GitHub Secrets（用于自动发布）

`OSS_ENDPOINT` `OSS_BUCKET` `OSS_AK_ID` `OSS_AK_SECRET`（阿里云 OSS）、
`BAIDU_PUSH_TOKEN`（百度搜索资源平台 普通收录 token）、
`INDEXNOW_KEY`（Bing IndexNow，根目录需放 `<key>.txt`）。

> 全部缺省时，`publish.py` 仅本地生成、不外发，安全无副作用。

## 扩展到其它报告类型

在 `build_index.py` 的 `TYPES` 加一行（slug → 中英名），为对应脚本加一个 `emit_*_json`，
`generator.py` 按需扩展模板字段。当前已实现：`bdi-market`（干散货市场/BDI）。
