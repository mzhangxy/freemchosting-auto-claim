# freemchosting-auto-claim

在 GitHub Actions 上自动登录 [dash.freemchosting.com](https://dash.freemchosting.com/login) 并领取 Credits 的脚本（DrissionPage 驱动真实 Chrome）。

## 工作原理

1. 打开登录页，填写账号密码（来自 GitHub Secrets，不会出现在代码里）
2. 遇到 Cloudflare Turnstile 时，进入 `challenges.cloudflare.com` 的 iframe，通过 shadow DOM 点击复选框，循环等待 `cf-turnstile-response` token 出现
3. 点击 Sign in 登录
4. 进入 `/rewards` 页面，点击 **Generate reward → Start reward**
5. 在打开的 LootLabs 页面自动完成任务：关闭广告弹窗、勾选人机验证、点击 Continue、等待倒计时结束后点击领取按钮，直到跳转回 FreeMC Hosting

## 使用方法

### 1. 配置 Secrets（必须）

在仓库 **Settings → Secrets and variables → Actions** 中添加两个 secret：

| Secret | 说明 |
|---|---|
| `MC_USERNAME` | 网站用户名或邮箱 |
| `MC_PASSWORD` | 网站密码 |

### 2. 运行

- **自动运行**：workflow 已配置每 3 小时（UTC 每天的 `0/3/6/9/12/15/18/21` 点 41 分）定时运行
- **手动运行**：Actions → Auto Claim Credits → Run workflow，可指定领取轮数 `max_rounds`

每次运行结束后，可在 run 详情页的 **Artifacts** 里下载 `screenshots-xxx` 查看各步骤截图，方便排查问题。

## 注意事项

- **Cloudflare 风控**：GitHub Actions 的 IP 属于数据中心 IP，Cloudflare 可能给出更严格的验证。脚本会多次尝试点击验证框，如果仍然失败，可在 run 日志的截图中查看具体卡在哪一步。
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
