# 航运市场日报 — 自动化推送系统

每天自动拉取实时数据，生成 HTML 日报截图，推送到企业微信群。

目前包含三套报告：

| 报告 | 脚本 | 触发时间 |
|------|------|----------|
| 干散货市场（BDI + 燃油） | `bdi_daily_push_add_fuel.py` | 每个工作日 12:00 |
| 全球航线海况 | `marine_push.py` | 每周一、周四 08:30 |
| 全球热带气旋预警 | `cyclone_push.py` | 每天 4 次（08:00 / 14:00 / 20:00 / 次日02:00）|

---

## 目录结构

```
├── bdi_daily_push_add_fuel.py   # 干散货 + 燃油主脚本（生产用）
├── bdi_daily_push.py            # 干散货原版脚本（备份，仅 BDI）
├── marine_push.py               # 全球航线海况脚本
├── cyclone_push.py              # 全球热带气旋预警脚本
├── requirements.txt             # Python 依赖
├── .env.example                 # 配置模板
├── .env                         # 本地实际配置（不提交 git）
├── .github/
│   └── workflows/
│       ├── bdi_daily.yml        # 干散货日报定时 workflow
│       ├── marine_daily.yml     # 海况日报定时 workflow
│       └── cyclone_alert.yml    # 气旋预警定时 workflow
└── reports/                     # 生成的 HTML 报告（自动创建）
```

---

## 本地运行

### 1. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置 Webhook

```bash
cp .env.example .env
# 编辑 .env，填入 WECOM_WEBHOOK
```

`.env` 示例：
```
WECOM_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key
NAVGREEN_TOKEN=你的token（燃油数据用，可留空）
```

### 3. 立即执行测试

```bash
# 干散货 + 燃油日报
python bdi_daily_push_add_fuel.py

# 海况日报
python marine_push.py

# 气旋预警
python cyclone_push.py
```

---

## GitHub Actions 部署（生产方式）

### 配置步骤

1. 将代码推送到 GitHub 私有仓库
2. 进入仓库 **Settings → Secrets and variables → Actions**
3. 添加以下 Secrets：

| Secret 名称 | 说明 | 是否必填 |
|-------------|------|----------|
| `WECOM_WEBHOOK` | 企业微信群机器人 Webhook 地址 | ✅ 必填 |
| `NAVGREEN_TOKEN` | 燃油价格 API Token | 选填，留空跳过燃油报告 |

4. 进入 **Actions** 页面，点击对应 Workflow → **Run workflow** 手动测试

### 修改定时时间

编辑对应 workflow 文件，修改 cron 表达式（UTC 时间 = 北京时间 - 8 小时）：

```yaml
- cron: '0 4 * * 1-5'   # UTC 04:00 = 北京 12:00，周一至周五
```

常用对照：

| 北京时间 | cron（UTC） |
|---------|-------------|
| 08:30 | `30 0 * * *` |
| 09:00 | `0 1 * * 1-5` |
| 12:00 | `0 4 * * 1-5` |
| 18:30 | `30 10 * * 1-5` |

---

## 企业微信机器人 Webhook 获取

1. 打开企业微信，进入目标**群聊**
2. 右上角 `···` → **添加群机器人** → 新创建一个机器人
3. 创建完成后复制 Webhook 地址：
   ```
   https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxx
   ```
4. 填入 `.env` 的 `WECOM_WEBHOOK`，或用 gh CLI 更新 GitHub Secret：
   ```bash
   gh secret set WECOM_WEBHOOK --repo 用户名/仓库名 --body "新的webhook地址"
   ```

---

## 燃油数据 Token 获取

`NAVGREEN_TOKEN` 用于拉取全球港口燃油价格，有效期约 30 天：

1. 登录 vip.navgreen.cn
2. 打开浏览器开发者工具（F12）→ **Application** → **Local Storage**
3. 找到 `token` 字段，复制值
4. 更新 GitHub Secret：
   ```bash
   gh secret set NAVGREEN_TOKEN --repo 用户名/仓库名 --body "你的token"
   ```

Token 过期后燃油模块自动跳过，不影响 BDI 日报正常推送。

---

## 常见问题

**Q: Actions 到时间没有自动执行？**
A: GitHub Actions 定时任务可能延迟 15–30 分钟，属正常现象。可在 Actions 页面手动点 Run workflow 验证。

**Q: 周末 / 节假日会报错吗？**
A: 不会。脚本自动过滤空数据，如 API 返回的最新有效数据是上一交易日，会正常生成该日报告。

**Q: 新闻抓取失败了怎么办？**
A: 不影响指数数据和推送，报告中新闻部分留空，其余正常显示。

**Q: 气旋预警会重复推送吗？**
A: 不会。脚本通过状态缓存文件（`.cyclone_state.json`）记录已推送的系统，同一气旋不会重复发送，只在状态发生变化时才推送。

**Q: 如何查看历史报告？**
A: 每次 Actions 执行后，HTML 报告会上传为 Artifact，在 Actions 运行记录页面可下载，保留 14–30 天。
