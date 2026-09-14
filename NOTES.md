# 调试笔记与交接 (2026-09-14)

> 给下次运行/继续开发的人看的。今天完成了 rewards 页 Turnstile 适配、云端代理修复、
> 嵌套 captcha 处理器；**未解决**的是 LootLabs "CONFIRM YOU ARE HUMAN" 验证任务，
> 已确定下次换 SeleniumBase UC 模式重写浏览器层（见下方计划）。

## 一、已解决并验证 ✅（全部在 GitHub main 上）

### 1. rewards 页新增的 Cloudflare Turnstile（本次需求）
- `claim_round()` 里通过 `turnstile_ready()`: 等组件渲染 → 点击复选框 → 等
  `cf-turnstile-response` token → 再点 Generate reward。
- token 被服务端拒绝时表单 POST 整页刷新出空验证框, 自动重新验证重试 (最多 3 次)。
- Start reward 前如果出现验证组件同样先过 (`Turnstile(start)`)。
- **实测 5 次运行全部一次通过, 无问题。**

### 2. 云端 socks 代理被静默忽略 (commit 08aaa089 / 2be897e)
- DrissionPage 的 `set_proxy("socks5://...")` **不支持 socks, 会静默忽略**
  (日志里那句"你似乎在设置使用socks代理, 暂时不支持这种代理"就是它),
  导致云端跑时浏览器直连 runner 数据中心 IP。
- 修复: socks 走 Chrome 参数 `--proxy-server=`; 最终 workflow 改用 sing-box 的
  http 入站 `http://127.0.0.1:1081` (set_proxy 原生支持, 用户要求的方案)。
- 启动时访问 `api.ipify.org` 记录浏览器出口 IP, 方便确认代理是否生效。

### 3. LootLabs "Packet blocked" 检测 (commit 08aaa089)
- LootLabs 对数据中心/被拉黑 IP 返回 "Packet blocked ... use of VPN"。
- 现在会刷新重试 2 次, 仍被屏蔽则判定出口 IP 被拉黑, 提前结束 ('fail'),
  不再空耗 LOOT_MAX_SECONDS。
- **注意: 换再好的节点也可能被拉黑, 这个检测就是为这种情况准备的。**

### 4. 嵌套 captcha 处理器 (commit 64df5f9 / fe31e70, 离线 mock 全链路 PASS)
- 验证任务的 captcha 页 (nerventualken.com/captcha 等随机域名) 嵌在 lootlabs
  页面的 iframe 里, 里面是 Turnstile + `#go`(Continue) 按钮, **点了 Continue 才
  POST /captcha/verify 完成验证**。
- 现有实现: `solve_captcha_frames()` 深度枚举 frame → `probe_captcha()` 探测
  (穿透 shadow DOM) → `tick_captcha()` 点击复选框 → `click_captcha_continue()`
  点 Continue → 渲染失败自动重载 frame (每个 ≤2 次)。
- 找 gate 用"按域名定向查找 + iter_frames 兜底" (后者对深层跨域 iframe 不稳定)。
- 离线 mock (嵌套 iframe + 假复选框) 验证了 DP 坐标点击能准确落进嵌套 iframe。
- **生产环境未能验证通过, 原因见下节 —— 所以决定换框架。**

### 5. 本机 self-hosted runner 环境
- `C:\Users\Mzhangxy\actions-runner`, 用 `start-runner.cmd` 启动 (当前在线)。
- runner 连 GitHub 走: xray socks(127.0.0.1:10808) → `http2socks.py` 桥接成
  http 代理(127.0.0.1:18181) → `HTTPS_PROXY` 环境变量 (Node 的 action 只认 http 代理)。
- 注意: runner 目录下若有 `.proxy` 残留文件会覆盖环境变量 (踩过坑, 已删)。
- job 里的浏览器走家宽直连 (不设代理), 家宽 IP 没被 LootLabs 拉黑。
- 本地仓库 git 已配置 `http.proxy socks5h://127.0.0.1:10808`, fetch/push 正常。

## 二、未解决: 验证任务在 DrissionPage(CDP) 浏览器里过不去 ❌

生产环境 (self-hosted, 家宽 IP, LootLabs 页面本身加载正常) 的稳定现象:

1. 点击验证任务后, gate iframe **延迟 1~2 分钟才生成** (已加任务行滚动 + 240s 上限)。
2. gate 页面结构 (curl 直接抓 HTML 确认): Turnstile managed 模式
   (sitekey `0x4AAAAAAEO6tvECK-X4VCvq`, action=unlock), token 只是**启用** `#go`
   按钮, 必须再点 Continue 才完成验证。
3. **挑战 iframe 永远不生成**: `widget=True` 但 `ts=False` —— 对照实验发现连顶层
   页面用 Turnstile 测试 sitekey 也不渲染 iframe, 而 dash.freemchosting.com 的
   登录/rewards Turnstile 在同一浏览器里正常通过。
   → 结论: **Cloudflare 检测到了 CDP 调试连接** (DrissionPage 架构层面, 无法规避)。
4. 截图 (run 34789270685 的 loot_501s.jpg) 显示组件在屏幕上渲染出了复选框, 但 DOM
   完全查不到 → closed shadow root, JS 穿透不了。
5. 出现过 "Check expired. Please tick the box again." —— 说明 Turnstile 曾**自动
   通过发了 token**, 5 分钟过期没人点 Continue。理论上抢到 `go==1` 窗口点 Continue
   就能过, 但 frame 枚举盲区 + 2 分钟延迟让窗口很难抓。
6. 对容器坐标 (30,33) 的真实点击 (CDP Input 事件) 落点正确 (mock 验证过) 但生产
   环境点了没反应 —— 印证是环境检测而非坐标问题。

**结论: 手搓 CDP 点击这条路走到头了, 换 SeleniumBase UC 模式。**

## 三、明天计划: 迁移 SeleniumBase UC 模式

- `pip install seleniumbase`, `requirements.txt` 加依赖。
- 核心 API: `SB(uc=True)` / `sb.uc_open_with_reconnect(url)` (敏感页面加载时断开
  WebDriver 连接躲过检测) / `sb.uc_gui_handle_captcha()` 或 `sb.uc_gui_click_captcha()`
  (PyAutoGUI **操作系统级真实鼠标点击** Turnstile, 不受 shadow DOM/嵌套 iframe 影响,
  只要屏幕上可见就能点)。
- 重写范围: 浏览器层 (登录、rewards、lootlabs 导航与点击)。
  lootlabs 的 JS 片段 (LOOT_TASK_PICK_JS / idle/spin 计数 / Continue / Packet
  blocked) 与框架无关, 平移即可; 轮次控制、卡死判定、每日上限检测原样保留。
- PyAutoGUI 需要真实桌面 → self-hosted Windows runner 满足; 云端 runner 没有
  display 且 LootLabs 本来就封数据中心 IP, 不用考虑。
- **直接完整跑一次实测: 网站对 DRY_RUN 也计每日次数 (README 已注明), 没有"免费测试"。**
- 一次完整跑 = 消耗 1 次生成; 失败在 captcha 环节的轮会判 'stuck'/'fail',
  MAX_ROUNDS 控制总量, 别开太大。

## 四、账号与额度

- 测试账号 `michael2026` (GitHub Secrets: `MC_USERNAME` / `MC_PASSWORD`, 值不可读)。
- 每日 **5 次**生成机会, **2026-09-14 已用完 5/5** (第 6 次 run 正确检测到
  "Generate reward 按钮消失"并干净退出, 这个保护逻辑也是好的)。
- 今天消耗明细: 云端 2 次 (验证代理修复, IP 被拉黑) + 家宽 3 次 (验证 captcha 修复)。
- 额度重置时间未确认, 大概率 UTC 0 点 (北京时间早上 8 点)。

## 五、相关 run 与提交 (排查时对照)

| run | 环境 | commit | 结果 |
|---|---|---|---|
| 34786819183 | 云端 | bd7923a8 | 代理没生效, LootLabs Packet blocked |
| 34787320580 | 云端 | 08aaa089 | 代理生效(出口 119.237.45.6)但该 IP 被拉黑 |
| 34788354512 | 家宽 | 2e5eceb | 走到验证任务, gate 里的 Turnstile 没人点, 180s 卡死 |
| 34789270685 | 家宽 | 11dfcef | 发现 shadow DOM; 点了坐标没反应; 重载后 err |
| 34790296974 | 家宽 | 64df5f9 | 坐标点击+定向查找版, 仍未出 token |
| 34791061256 | 家宽 | fe31e70 | 每日 5/5 用完, 干净退出 |

今日 commits: bd7923a8 → 08aaa089 → 2be897e → 11dfcef → 64df5f9 → fe31e70 → 6ccdb85。
截图在各 run 的 Artifacts (`screenshots-selfhosted-<run_id>`), 本机缓存:
`C:\Users\Mzhangxy\AppData\Local\Temp\shots5\`、`shots6\`。

## 六、遗留小项

- 验证 gate iframe 延迟 1~2 分钟生成, 怀疑与任务行可见性有关 (已加点击后立即滚动,
  未验证效果)。
- `tempermonkey.txt` (手动油猴方案) 未同步这些修复, 只作为参考保留。
