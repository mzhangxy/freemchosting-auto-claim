# freemchosting-auto-claim

在 GitHub Actions 上自动登录 [dash.freemchosting.com](https://dash.freemchosting.com/login) 并领取 Credits 的脚本（DrissionPage 驱动真实 Chrome）。

## 工作原理

1. 打开登录页，填写账号密码（来自 GitHub Secrets，不会出现在代码里）
2. 遇到 Cloudflare Turnstile 时，进入 `challenges.cloudflare.com` 的 iframe，通过 shadow DOM 点击复选框，循环等待 `cf-turnstile-response` token 出现
3. 点击 Sign in 登录
4. 进入 `/rewards` 页面，先通过页面上的 Cloudflare Turnstile 验证，再点击 **Generate reward → Start reward**（Generate/Start 表单若再次出现验证框会自动先通过；token 被拒绝时自动重新验证并重试，最多 3 次）
5. 在打开的 LootLabs 页面自动完成任务：关闭广告弹窗、勾选人机验证、点击 Continue、等待倒计时结束后点击领取按钮，直到跳转回 FreeMC Hosting

## 使用方法

### 1. 配置 Secrets（必须）

在仓库 **Settings → Secrets and variables → Actions** 中添加两个 secret：

| Secret | 说明 |
|---|---|
| `MC_USERNAME` | 网站用户名或邮箱 |
| `MC_PASSWORD` | 网站密码 |
| `NODE_LINK` | 代理节点链接（云端领取必需，见下） |

### 2. 运行

- **自动运行**：workflow 已配置每天 UTC 3:41 定时运行
- **手动运行**：Actions → Auto Claim Credits → Run workflow，可指定领取轮数 `max_rounds`

每次运行结束后，可在 run 详情页的 **Artifacts** 里下载 `screenshots-xxx` 查看各步骤截图，方便排查问题。

## 注意事项

- **LootLabs 屏蔽数据中心 IP（重要）**：GitHub官方 runner 的 IP 属于数据中心 IP，LootLabs 会返回 "Packet blocked ... use of VPN" 直接拒绝加载任务页，导致云端领取失败（登录和每日上限检测都正常）。**本脚本已在家庭宽带网络下完整跑通**（登录 → 任务 → CF 验证 → 领取到账）。云端要用有两种办法：
  1. 在 Secrets 里加一个 `NODE_LINK`（节点链接，支持 `vmess://`、`vless://`、`trojan://`、`hysteria2://` 等格式，出口需为住宅/ISP IP）。workflow 会自动启动 sing-box 将节点转成本地代理（`http://127.0.0.1:1081`），浏览器全程走节点出口；节点未配置或启动失败时自动降级为直连（也兼容旧 `CLAIM_PROXY` 变量）；
  2. 改用自托管 runner：在自己电脑上装 GitHub self-hosted runner，然后手动运行 **Auto Claim Credits (Self-hosted)** workflow（电脑需开机）。
- **每日上限**： rewards 页每天最多 5 次生成机会，脚本检测到 "Generate reward" 按钮消失（次数用尽/冷却中）会自动停止。
- **卡死重试**：任务行带 "~50 sec." 之类时长标注的会自动完成（正常 50~180 秒）；既无时长标注又不是验证类的任务属于网站 Bug（永远不会完成），脚本会立刻放弃本轮并重新开始；单任务等待超过 240 秒或全局 360 秒无进展同样重开。
- **定时任务休眠**：GitHub 会在公共仓库 60 天无活动后自动暂停定时 workflow，届时手动 Run 一次或 push 一次即可恢复。
- **公共仓库**：公共仓库的 Actions 分钟数免费不限量，账号密码只存在 Secrets 中，不会泄露。如改为私有仓库，请注意 GitHub 免费账户每月只有 2000 分钟额度。

## 本地运行（调试用）

```bash
pip install -r requirements.txt
export MC_USERNAME=你的邮箱
export MC_PASSWORD=你的密码
DRY_RUN=1 python claim.py   # 只验证登录，不实际领取
python claim.py             # 实际领取
```
