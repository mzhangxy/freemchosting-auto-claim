#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FreeMC Hosting 自动领取 Credits (DrissionPage + GitHub Actions)

流程:
  1. 打开 https://dash.freemchosting.com/login, 填写账号密码
  2. 处理 Cloudflare Turnstile (点击验证框, 等待 cf-turnstile-response token)
  3. 点击 Sign in 登录
  4. 进入 /rewards: 页面上的 Cloudflare Turnstile 先通过验证,
     再点击 Generate reward -> (如再次出现验证同样先通过) Start reward
  5. 处理 LootLabs 任务流:
     - 任务行带 "~50 sec." 等时长标注 -> 点击后弹出的广告页保持 7 秒再关闭,
       等任务在标注时间内自动完成 (正常 50~180 秒, 上限 240 秒)
     - "CONFIRM YOU ARE HUMAN" 等验证任务 -> 自动点击 Turnstile
     - 任务行既没有时长标注也不是验证类 -> 网站Bug, 永远不会完成,
       立刻放弃本轮, 回 rewards 重新开始 (等待超过 360 秒无进展同样判定为卡死)
     - 全部任务完成后点击 CLAIM REWARD / unlockBtn, 等待跳转回 FreeMC Hosting

环境变量:
  MC_USERNAME, MC_PASSWORD   必填, 网站账号密码
  MAX_ROUNDS                 领取轮数(含卡死重试), 默认 3
  LOOT_MAX_SECONDS           单轮 LootLabs 最长处理时间, 默认 600
  DRY_RUN                    1 = 只验证登录并打开 rewards 页, 不实际领取
  SHOT_DIR                   截图目录, 默认 screenshots
"""

import os
import re
import sys
import json
import time
import random
import shutil
import traceback

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from DrissionPage import ChromiumPage, ChromiumOptions

DASH = 'https://dash.freemchosting.com'
LOGIN_URL = DASH + '/login'
REWARDS_URL = DASH + '/rewards'

USERNAME = os.environ.get('MC_USERNAME', '')
PASSWORD = os.environ.get('MC_PASSWORD', '')
# workflow 传的是 PROXY(sing-box socks5 入站), 保留 CLAIM_PROXY 兼容旧配置
PROXY = os.environ.get('CLAIM_PROXY') or os.environ.get('PROXY', '')
MAX_ROUNDS = int(os.environ.get('MAX_ROUNDS', '3') or '3')
LOOT_MAX = int(os.environ.get('LOOT_MAX_SECONDS', '600') or '600')
DRY_RUN = os.environ.get('DRY_RUN', '0') == '1'
SHOT_DIR = os.environ.get('SHOT_DIR', 'screenshots')

VERIFY_RE = re.compile(
    r'confirm|human|验证|captcha|turnstile|hcaptcha|recaptcha', re.I)
DUR_RE = re.compile(r'[~～≈]\s*(\d+)\s*(sec|秒|s\b)', re.I)


def log(msg):
    print(time.strftime('[%H:%M:%S] ') + str(msg), flush=True)


def shot(scope, name):
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        try:
            scope.get_screenshot(path=SHOT_DIR, name=name)
        except Exception:
            scope.get_screenshot(path=SHOT_DIR, name=name, full_page=True)
        log('📸 截图: ' + name)
    except Exception as e:
        log('截图失败(' + name + '): ' + str(e))


def human_pause():
    time.sleep(random.uniform(0.6, 1.4))


def page_snippet(scope, n=400):
    # 该站点用 DP 自带的 .text 取不到内容, 直接用 JS 读 innerText
    v = js_run(scope, "return (document.body && document.body.innerText) || '';")
    return re.sub(r'\s+', ' ', (v or ''))[:n]


# ---------------------------------------------------------------------------
# 浏览器
# ---------------------------------------------------------------------------

def build_page():
    co = ChromiumOptions()
    chrome = (shutil.which('google-chrome') or shutil.which('google-chrome-stable')
              or shutil.which('chromium-browser') or shutil.which('chromium')
              or shutil.which('chrome'))
    if chrome:
        co.set_browser_path(chrome)
    if PROXY:
        co.set_proxy(PROXY)
        log('🌐 使用代理: ' + PROXY)
    co.auto_port(True)
    co.set_argument('--no-sandbox')
    co.set_argument('--disable-dev-shm-usage')
    co.set_argument('--disable-gpu')
    co.set_argument('--window-size=1360,900')
    co.set_argument('--lang=en-US')
    co.set_argument('--disable-blink-features=AutomationControlled')
    # 关键: 禁止 Chrome 对"后台"窗口节流定时器, 否则 LootLabs 的任务倒计时永远走不完
    co.set_argument('--disable-background-timer-throttling')
    co.set_argument('--disable-backgrounding-occluded-windows')
    co.set_argument('--disable-renderer-backgrounding')
    co.set_argument('--disable-features=IntensiveWakeUpThrottling')
    page = ChromiumPage(co)
    try:
        page.set.timeouts(page_load=60, script=60)
        page.set.auto_handle_alert(True)
    except Exception:
        pass
    return page


# ---------------------------------------------------------------------------
# 通用 JS 工具
# ---------------------------------------------------------------------------

def js_run(scope, script, default=None, *args):
    try:
        return scope.run_js(script, *args)
    except Exception:
        return default


def js_bool(scope, script, *args):
    return bool(js_run(scope, script, False, *args))


def js_text(scope, script, *args):
    v = js_run(scope, script, '', *args)
    if isinstance(v, str):
        return v
    return '' if v is None else str(v)


def js_click_btn_with_text(scope, text):
    """在页面里找包含指定文本的可见 button/a 并点击, 返回是否点击成功"""
    script = r"""
const wanted = (arguments[0] || '').toUpperCase();
const els = Array.from(document.querySelectorAll('button, a, div[role="button"]'));
const el = els.find(e => e.offsetParent !== null &&
                         (e.textContent || '').trim().toUpperCase().includes(wanted));
if (el) { el.click(); return true; }
return false;
"""
    return js_bool(scope, script, text)


TAG_BTN_JS = r"""
const wanted = (arguments[0] || '').toUpperCase();
document.querySelectorAll('[data-dp-claim-target]')
        .forEach(e => e.removeAttribute('data-dp-claim-target'));
const els = Array.from(document.querySelectorAll('button, a, div[role="button"]'));
const el = els.find(e => e.offsetParent !== null &&
                         (e.textContent || '').trim().toUpperCase().includes(wanted));
if (!el) return false;
el.setAttribute('data-dp-claim-target', '1');
return true;
"""


def find_btn_by_text(scope, text):
    """该站点的 DP 文本定位器失效, 用 JS 按文本找到按钮并打标记, 再用 CSS 定位做真实点击"""
    if not js_bool(scope, TAG_BTN_JS, text):
        return None
    try:
        return scope.ele('css:[data-dp-claim-target="1"]', timeout=5)
    except Exception:
        return None


def click_continue(scope, tag, throttle):
    """每 2.5 秒最多点一次页面上可见的 Continue 按钮 (参考油猴脚本的强力点击循环)"""
    if time.time() - throttle[0] < 2.5:
        return False
    script = r"""
const els = Array.from(document.querySelectorAll('button, a, span, div'));
const el = els.find(e => {
  if (e.offsetParent === null) return false;
  const t = (e.textContent || '').trim().toUpperCase();
  return t.startsWith('CONTINUE') && t.length < 25;
});
if (el) { el.click(); return true; }
return false;
"""
    if js_bool(scope, script):
        log('🖱️ 点击 Continue (' + tag + ')')
        throttle[0] = time.time()
        return True
    return False


def iter_frames(tab):
    """枚举标签页内所有层级的 frame (顶层 + 嵌套 iframe), LootLabs 的任务界面可能在 iframe 里"""
    out = []
    seen = set()

    def list_frames(scope):
        fs = []
        if hasattr(scope, 'get_frames'):
            try:
                fs = scope.get_frames() or []
            except Exception:
                fs = []
        if not fs:
            try:
                for el in scope.eles('tag:iframe'):
                    try:
                        f = scope.get_frame(el)
                        if f:
                            fs.append(f)
                    except Exception:
                        pass
            except Exception:
                pass
        return fs

    def walk(scope, depth):
        for f in list_frames(scope):
            key = id(f)
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
            if depth < 3:
                walk(f, depth + 1)

    walk(tab, 0)
    return out


def _tab_id(t):
    try:
        return t.tab_id
    except Exception:
        return id(t)


def cleanup_tabs(page, keep_tid):
    """一轮结束后关掉除 keep_tid 外的所有标签页"""
    time.sleep(3)
    try:
        tids = list(page.tab_ids)
    except Exception:
        return
    for tid in tids:
        if tid == keep_tid:
            continue
        try:
            page.get_tab(tid).close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Cloudflare Turnstile (参考 DrissionPage 验证方案)
# ---------------------------------------------------------------------------

def turnstile_present(scope):
    script = r"""
return !!(document.querySelector('iframe[src^="https://challenges.cloudflare.com"]') ||
          document.querySelector('[name="cf-turnstile-response"]'));
"""
    return js_bool(scope, script)


def get_turnstile_token(scope):
    script = r"""
const els = document.querySelectorAll('[name="cf-turnstile-response"]');
for (const e of els) { if (e.value && e.value.length > 10) return e.value; }
return '';
"""
    return js_text(scope, script)


def click_turnstile(scope):
    """进入 challenges.cloudflare.com iframe, 点击复选框 (shadow DOM), 失败则盲点 iframe"""
    clicked = False
    try:
        frame = scope.get_frame('css:iframe[src^="https://challenges.cloudflare.com"]', timeout=3)
        if frame:
            time.sleep(2)
            try:
                sr = frame.ele('tag:body').shadow_root
                if sr:
                    target = sr.ele('css:input[type="checkbox"]') or sr.ele('css:div.main-wrapper')
                    if target:
                        target.click.at(offset_x=10, offset_y=10)
                        clicked = True
                        log('🛡️ 已点击 Turnstile 复选框')
            except Exception:
                pass
            if not clicked:
                try:
                    frame.frame_ele.click.at(offset_x=25, offset_y=30)
                    clicked = True
                    log('🛡️ 已盲点 Turnstile iframe')
                except Exception:
                    pass
    except Exception:
        pass
    return clicked


def wait_turnstile(scope, total=90, tag='Turnstile'):
    """循环点击 Turnstile 直到 token 出现; 页面没有验证组件时直接通过"""
    deadline = time.time() + total
    attempt = 0
    while time.time() < deadline:
        if get_turnstile_token(scope):
            log('✅ ' + tag + ' 已通过')
            return True
        if not turnstile_present(scope):
            return True
        attempt += 1
        if attempt % 2 == 1:
            log('🛡️ ' + tag + ': 第 ' + str(attempt) + ' 次尝试点击验证框...')
            click_turnstile(scope)
        time.sleep(2)
    ok = bool(get_turnstile_token(scope))
    log(('✅ ' if ok else '⚠️ ') + tag + (' 通过' if ok else ' 未通过(超时)'))
    return ok


def turnstile_ready(scope, tag, total=100):
    """提交前保证可提交: 组件还没渲染就等它出现; 没有组件或已有 token 直接通过"""
    for _ in range(4):
        if turnstile_present(scope):
            break
        time.sleep(2)
    if not turnstile_present(scope) or get_turnstile_token(scope):
        return True
    return wait_turnstile(scope, total=total, tag=tag)


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------

def do_login(page):
    log('🌐 打开登录页: ' + LOGIN_URL)
    page.get(LOGIN_URL)
    time.sleep(3)

    # 可能有 Cloudflare 全页拦截, 先过掉
    for _ in range(3):
        if page.ele('css:input[name="username"]', timeout=4):
            break
        if turnstile_present(page):
            wait_turnstile(page, total=45, tag='Turnstile(入口拦截)')
        time.sleep(2)

    if '/login' not in (page.url or ''):
        log('ℹ️ 已处于登录状态: ' + page.url)
        return True

    user_el = page.ele('css:input[name="username"]', timeout=20)
    pwd_el = page.ele('css:input[name="password"]', timeout=10)
    if not user_el or not pwd_el:
        shot(page, 'login_no_fields')
        raise RuntimeError('登录页输入框未找到, 当前页面: ' + page.url)

    log('✍️ 填写账号密码...')
    user_el.input(USERNAME, clear=True)
    human_pause()
    pwd_el.input(PASSWORD, clear=True)
    human_pause()

    log('🛡️ 等待/处理登录 Turnstile...')
    if not wait_turnstile(page, total=90, tag='Turnstile'):
        shot(page, 'login_turnstile_fail')
        raise RuntimeError('Turnstile 验证未通过')

    btn = page.ele('css:button[type="submit"]', timeout=8) or None
    if not btn:
        try:
            btn = [e for e in page.eles('tag:button')
                   if e.states.is_displayed and 'sign in' in (e.text or '').lower()][0]
        except Exception:
            btn = None
    if not btn:
        shot(page, 'login_no_button')
        raise RuntimeError('未找到 Sign in 按钮')
    log('🖱️ 点击 Sign in...')
    btn.click()

    for i in range(60):
        time.sleep(1)
        u = page.url or ''
        if '/login' not in u:
            log('✅ 登录成功 -> ' + u)
            return True
        # 登录失败/ token 过期会停留在登录页, 补一次验证并重新提交
        if i in (10, 25, 40):
            wait_turnstile(page, total=8, tag='Turnstile(重试)')
            try:
                b = page.ele('css:button[type="submit"]', timeout=2)
                if b:
                    b.click()
                    log('🖱️ 重新点击 Sign in...')
            except Exception:
                pass
    shot(page, 'login_stuck')
    raise RuntimeError('登录后未跳转, 页面: ' + page.url + ' | ' + page_snippet(page))


# ---------------------------------------------------------------------------
# LootLabs 页面的 JS 片段
# ---------------------------------------------------------------------------

# 找到第一个待办任务: 返回行文本, 并给行右侧的箭头按钮(真正触发任务的元素)打标记
LOOT_TASK_PICK_JS = r"""
document.querySelectorAll('[data-dp-task-arrow]')
        .forEach(e => e.removeAttribute('data-dp-task-arrow'));
const idle = document.querySelector('.task-ind.ind-idle');
if (!idle) return '';
const task = idle.closest('.task') || idle.parentElement;
const tr = task.getBoundingClientRect();
const cands = Array.from(task.querySelectorAll('a, button, [role="button"], span, div, svg, i'))
  .filter(e => {
    if (e.offsetParent === null) return false;
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.left > tr.left + tr.width * 0.55;
  });
cands.sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left);
let arrow = cands.find(e => /^[→❯»>➔›⇒-]{1,2}$/.test((e.textContent || '').trim()));
if (!arrow && cands.length) arrow = cands[0];
if (arrow) arrow.setAttribute('data-dp-task-arrow', '1');
return (task.textContent || '');
"""

LOOT_IDLE_COUNT_JS = r"""
return document.querySelectorAll('.task-ind.ind-idle').length;
"""

LOOT_SPIN_COUNT_JS = r"""
return document.querySelectorAll('.task-ind.ind-spin').length;
"""

# 真实点击由 DP 完成(css 定位 .task-ind.ind-idle), 这里是 JS 兜底点击
LOOT_CLICK_TASK_JS = r"""
const idle = document.querySelector('.task-ind.ind-idle');
if (idle) { idle.click(); return true; }
return false;
"""

LOOT_MODAL_JS = r"""
const modal = Array.from(document.querySelectorAll('div, h2, h3, p, span'))
  .find(e => e.offsetParent !== null && /Action not completed/i.test(e.textContent || ''));
if (!modal) return '';
const btn = Array.from(document.querySelectorAll('button, a'))
  .find(e => e.offsetParent !== null && /观看视频|watch|continue/i.test(e.textContent || ''));
if (btn) { btn.click(); return 'watch'; }
const closeBtn = document.querySelector('.close-modal, .modal-close') ||
  Array.from(document.querySelectorAll('button, span'))
       .find(e => (e.textContent || '').trim() === '×');
if (closeBtn) { closeBtn.click(); return 'closed'; }
return 'modal';
"""

# 无待办任务时: 点 unlockBtn(可用) 或 CLAIM REWARD 按钮 (锁定状态点了也无害)
LOOT_CLAIM_CLICK_JS = r"""
const btn = document.getElementById('unlockBtn');
if (btn && btn.offsetParent !== null && !btn.disabled) { btn.click(); return 'unlock'; }
const cr = Array.from(document.querySelectorAll('button, a, div, span'))
  .find(e => e.offsetParent !== null &&
             /CLAIM\s*REWARD/i.test((e.textContent || '').trim()) &&
             (e.textContent || '').trim().length < 40);
if (cr) { cr.click(); return 'claim'; }
return '';
"""

LOOT_DIAG_JS = r"""
return JSON.stringify({
  idle: document.querySelectorAll('.task-ind.ind-idle').length,
  spin: document.querySelectorAll('.task-ind.ind-spin').length,
  done: (document.querySelectorAll('.task-ind').length -
         document.querySelectorAll('.task-ind.ind-idle').length -
         document.querySelectorAll('.task-ind.ind-spin').length),
  unlock: (function(){var b=document.getElementById('unlockBtn');
          return b ? ('disabled:' + b.disabled) : '';})(),
  ready: (function(){var r=document.getElementById('readyText');
          return r ? (r.textContent || '').trim().slice(0, 30) : '';})(),
  cr: (function(){var e = Array.from(document.querySelectorAll('button, a, div'))
       .find(function(x){return /CLAIM REWARD/i.test(x.textContent || '') &&
                                 x.offsetParent !== null;});
       return e ? e.tagName : '';})(),
  ts: !!document.querySelector('iframe[src^="https://challenges.cloudflare.com"]')
});
"""


# ---------------------------------------------------------------------------
# LootLabs 流程
# ---------------------------------------------------------------------------

def classify_task(txt):
    """返回 (类型, 等待上限秒): timed=带 ~50 sec. 时长标注; verify=人机验证; None=网站Bug任务"""
    m = DUR_RE.search(txt or '')
    if m:
        return 'timed', 240
    if txt and VERIFY_RE.search(txt):
        return 'verify', 180
    return None, 0


def handle_popup(page, tab, tid, info, popups, claim_clicked_at, throttle):
    """任务弹出的标签页: 广告页保持 7 秒再关闭(太短不计), 验证页点 Turnstile/Continue 后关闭"""
    try:
        if claim_clicked_at and time.time() - claim_clicked_at < 60:
            return  # 领取后打开的标签先不动
        age = time.time() - info['first_seen']
        role = info.get('role', 'ad')
        if role == 'verify':
            if turnstile_present(tab) and not get_turnstile_token(tab):
                click_turnstile(tab)
            if not info.get('continue_clicked'):
                if click_continue(tab, 'verify-popup', throttle):
                    info['continue_clicked'] = time.time()
            if age > 170:
                tab.close()
                popups.pop(tid, None)
                log('🧹 验证弹窗超时, 关闭')
        else:
            # 广告/定时任务弹窗: 保持 7 秒以上才有效, 关闭前顺手点一次 Continue
            if age > 7:
                if not info.get('grace') and click_continue(tab, 'popup', throttle):
                    info['grace'] = time.time()
                g = info.get('grace')
                if not g or time.time() - g > 4:
                    tab.close()
                    popups.pop(tid, None)
                    log('🧹 弹窗已保持足够时间, 关闭 (' + role + ')')
    except Exception:
        popups.pop(tid, None)


def scope_diag(sc):
    v = js_text(sc, LOOT_DIAG_JS)
    try:
        d = json.loads(v)
        return 'idle=%s spin=%s done=%s %s %s cr=%s ts=%s' % (
            d.get('idle'), d.get('spin'), d.get('done'),
            d.get('unlock'), d.get('ready'), d.get('cr'), d.get('ts'))
    except Exception:
        return 'diag?'


def run_lootlabs(page, tab):
    """处理 LootLabs 任务流, 返回 'ok' | 'stuck'(Bug广告,需重开一轮) | 'fail'

    任务状态用 idle/spin 计数判断: 点击后 idle 减少(进入 spin), spin 归零即完成。
    """
    log('⏳ 开始处理 LootLabs 任务...')
    t0 = time.time()
    dash_tid = _tab_id(page)
    loot_tid = _tab_id(tab)
    popups = {}
    task_popup_tid = None
    in_task = False
    task_cap = 0
    task_started = 0.0
    task_is_verify = False
    idle_at_click = 0
    spin_seen = False
    prev_spin = -1
    prev_idle = -1
    reclicks = 0
    last_progress = time.time()
    claim_clicked_at = None
    last_claim_click = 0.0
    cont_throttle = [0.0]
    popup_throttle = [0.0]
    last_diag = 0.0
    last_shot = 0.0

    def totals(scs):
        idle_n = spin_n = 0
        for sc in scs:
            idle_n += js_run(sc, LOOT_IDLE_COUNT_JS, 0) or 0
            spin_n += js_run(sc, LOOT_SPIN_COUNT_JS, 0) or 0
        return idle_n, spin_n

    def dp_click_first_idle(scs):
        for sc in scs:
            try:
                ind = sc.ele('css:.task-ind.ind-idle', timeout=1)
                if ind:
                    ind.click()
                    return True
            except Exception:
                continue
        return False

    while time.time() - t0 < LOOT_MAX:
        time.sleep(1.5)

        # ---- 收集所有标签页: 处理弹窗 / 检测回跳 ----
        try:
            tids = list(page.tab_ids)
        except Exception:
            continue
        for tid in tids:
            if tid in (loot_tid, dash_tid):
                continue
            try:
                t = page.get_tab(tid)
                u = (t.url or '').lower()
            except Exception:
                continue
            if 'freemchosting' in u and claim_clicked_at:
                log('✅ 检测到 FreeMC Hosting 回跳标签页')
                return 'ok'
            if 'lootlabs' in u:
                continue
            if tid not in popups:
                role = 'ad'
                if in_task and task_popup_tid is None:
                    role = 'verify' if task_is_verify else 'timed'
                    task_popup_tid = tid
                popups[tid] = {'first_seen': time.time(), 'role': role, 'continue_clicked': 0}
                log('🪟 新弹窗 (' + role + '): ' + u[:80])
                last_progress = time.time()
            handle_popup(page, t, tid, popups[tid], popups, claim_clicked_at,
                         popup_throttle)

        if task_popup_tid and task_popup_tid not in popups:
            task_popup_tid = None  # 弹窗已关闭, 下个任务的弹窗重新归类

        # ---- loot 标签页状态 ----
        try:
            cur = tab.url or ''
        except Exception:
            continue
        cur_l = cur.lower()

        # 领取后跳转 = 成功
        if claim_clicked_at and 'lootlabs' not in cur_l:
            log('✅ 领取后页面已跳转: ' + cur[:120])
            return 'ok'

        # 任务中途被广告跳走 -> 返回
        if 'lootlabs' not in cur_l:
            if 'freemchosting' in cur_l:
                log('✅ 已回到 FreeMC Hosting')
                return 'ok'
            log('↩️ LootLabs 页面被跳转到: ' + cur[:100] + ' , 返回...')
            try:
                tab.back()
            except Exception:
                pass
            time.sleep(2)
            continue

        # ---- 枚举 frame, 统计任务状态 ----
        scopes = [tab] + iter_frames(tab)
        idle_total, spin_total = totals(scopes)
        if idle_total != prev_idle:
            if prev_idle >= 0:
                last_progress = time.time()
            prev_idle = idle_total

        now = time.time()
        if now - last_diag > 30:
            last_diag = now
            log('📊 idle=%s spin=%s' % (idle_total, spin_total))
            for sc in scopes:
                try:
                    su = (sc.url or '')[:70]
                except Exception:
                    su = '?'
                log('🔍 [' + su + '] ' + scope_diag(sc))
            log('📄 页面文本: ' + page_snippet(scopes[0], 200))
        if now - last_shot > 60:
            last_shot = now
            shot(tab, 'loot_' + str(int(now - t0)) + 's')

        # ---- lootlabs 页面常规处理: 弹窗/Continue/Turnstile ----
        for sc in scopes:
            modal = js_text(sc, LOOT_MODAL_JS)
            if modal:
                log('⚠️ "Action not completed" 弹窗: ' + str(modal))
                last_progress = time.time()
            click_continue(sc, 'lootlabs', cont_throttle)
            if turnstile_present(sc) and not get_turnstile_token(sc):
                if click_turnstile(sc):
                    last_progress = time.time()

        # ---- 任务状态轮询 (基于 idle/spin 计数) ----
        task_done = False
        if in_task:
            waited = time.time() - task_started
            if spin_total > 0:
                spin_seen = True
                if spin_total != prev_spin:
                    prev_spin = spin_total
                    last_progress = time.time()
            if idle_total < idle_at_click and spin_total == 0 and (spin_seen or waited > 25):
                log('✅ 任务已完成 (等待 ' + str(int(waited)) + 's)')
                in_task = False
                task_done = True
                last_progress = time.time()
            elif waited > task_cap:
                log('❌ 任务等待超过 ' + str(task_cap) + 's 仍未完成 -> 判定卡死, 重开一轮')
                return 'stuck'
            elif idle_total >= idle_at_click and spin_total == 0 and waited > 25:
                # 点击没有生效, 重试
                if reclicks < 2:
                    reclicks += 1
                    log('🔁 任务点击似乎未生效, 重试点击 (' + str(reclicks) + '/2)')
                    clicked = dp_click_first_idle(scopes)
                    if not clicked:
                        for sc in scopes:
                            if js_bool(sc, LOOT_CLICK_TASK_JS):
                                clicked = True
                                break
                    if clicked:
                        task_started = time.time()
                        idle_at_click = idle_total
                        last_progress = time.time()
                else:
                    log('❌ 任务多次点击无效 -> 判定卡死, 重开一轮')
                    return 'stuck'

            if task_done and task_popup_tid and task_popup_tid in popups:
                try:
                    page.get_tab(task_popup_tid).close()
                except Exception:
                    pass
                popups.pop(task_popup_tid, None)
                task_popup_tid = None
                log('🧹 任务弹窗已关闭 (任务完成)')

        # ---- 无进行中任务: 点下一个任务 / 尝试领取 ----
        if not in_task:
            pick = ''
            pick_scope = None
            for sc in scopes:
                v = js_text(sc, LOOT_TASK_PICK_JS)
                if v:
                    pick = v
                    pick_scope = sc
                    break
            if pick:
                task_txt = pick
                kind, _cap = classify_task(task_txt)
                if kind is None:
                    log('❌ 任务无时长标注且非验证类 (永不完成): ' +
                        re.sub(r'\s+', ' ', task_txt).strip()[:60] + ' -> 重开一轮')
                    return 'stuck'
                # 优先点击行右侧的箭头按钮(真正触发任务的元素), 找不到再点指示器
                clicked = False
                try:
                    arrow = pick_scope.ele('css:[data-dp-task-arrow="1"]', timeout=2)
                    if arrow:
                        arrow.click()
                        clicked = True
                        log('🖱️ 点击任务行右箭头')
                except Exception:
                    pass
                if not clicked:
                    clicked = dp_click_first_idle([pick_scope])
                if not clicked:
                    clicked = js_bool(pick_scope, LOOT_CLICK_TASK_JS)
                if clicked:
                    in_task = True
                    task_started = time.time()
                    task_is_verify = (kind == 'verify')
                    task_cap = _cap
                    idle_at_click = max(idle_total, 1)
                    spin_seen = False
                    prev_spin = -1
                    reclicks = 0
                    last_progress = time.time()
                    log('📌 点击任务 [' + kind + ', 等待上限 ' + str(task_cap) + 's]: ' +
                        re.sub(r'\s+', ' ', task_txt).strip()[:60])
                else:
                    log('⚠️ 任务点击失败, 下轮重试')
            elif idle_total == 0 and spin_total == 0:
                # 无待办任务 -> 尝试领取 (8 秒节流)
                if claim_clicked_at is None or now - last_claim_click > 8:
                    for sc in scopes:
                        res = js_text(sc, LOOT_CLAIM_CLICK_JS)
                        if res:
                            last_claim_click = time.time()
                            last_progress = time.time()
                            if claim_clicked_at is None:
                                claim_clicked_at = time.time()
                                log('🎉 已点击领取按钮 (' + str(res) + '), 等待跳转...')
                            else:
                                log('🔁 重复点击领取按钮 (' + str(res) + ')')
                            break

        # ---- 卡死保护: 360 秒无任何进展 ----
        if time.time() - last_progress > 360:
            log('❌ 超过 360 秒无任何进展 -> 判定卡死, 重开一轮')
            return 'stuck'

    shot(tab, 'lootlabs_timeout')
    log('⚠️ LootLabs 处理超时 (' + str(LOOT_MAX) + 's)')
    return 'fail'


# ---------------------------------------------------------------------------
# Rewards 页面领取一轮
# ---------------------------------------------------------------------------

def claim_round(page):
    """返回 True=领取成功, 'stuck'=遇到Bug广告需重开一轮, False=无法继续"""
    dash_tid = _tab_id(page)
    log('🧭 打开 Rewards 页面: ' + REWARDS_URL)
    page.get(REWARDS_URL)
    time.sleep(4)
    log('💰 ' + page_snippet(page, 260))

    # Generate reward 表单带 Cloudflare Turnstile, 必须先通过验证再点击;
    # token 被服务端拒绝时会整页刷新出空的验证框, 需重新验证后重试 (最多 3 次)
    gen_clicked = False
    for attempt in range(1, 4):
        if not turnstile_ready(page, 'Turnstile(rewards#' + str(attempt) + ')',
                               total=100 if attempt == 1 else 60):
            shot(page, 'rewards_turnstile_fail_' + str(attempt))
            log('❌ rewards 页 Turnstile 未通过, 放弃本轮')
            return False
        if attempt > 1:
            # token 已被消费/过期, 刷新页面重新拿
            page.get(REWARDS_URL)
            time.sleep(4)
            if not turnstile_ready(page, 'Turnstile(rewards#' + str(attempt) + ')', total=60):
                shot(page, 'rewards_turnstile_fail_' + str(attempt))
                return False

        gen = find_btn_by_text(page, 'Generate reward')
        if gen:
            human_pause()
            try:
                gen.click()
            except Exception:
                gen = None
        if not gen and not js_click_btn_with_text(page, 'Generate reward'):
            log('ℹ️ 未找到 "Generate reward" 按钮 (可能冷却/达每日上限)。URL: ' + (page.url or ''))
            log('页面文本: ' + page_snippet(page, 500))
            shot(page, 'rewards_no_generate')
            return False
        log('🪙 已点击 Generate reward' + (' (第 ' + str(attempt) + ' 次)' if attempt > 1 else ''))

        # 表单 POST 会整页刷新; 若 token 被拒, 新页面里验证框无 token
        start = None
        for _ in range(10):
            time.sleep(2)
            start = find_btn_by_text(page, 'Start reward')
            if start:
                break
        if not start and turnstile_present(page) and not get_turnstile_token(page):
            log('⚠️ 第 ' + str(attempt) + ' 次点击后未出现 Start reward 且验证框已重置')
            log('页面文本: ' + page_snippet(page, 300))
            continue
        if not start:
            # 页面加载慢的情况再多等 10 秒
            for _ in range(5):
                time.sleep(2)
                start = find_btn_by_text(page, 'Start reward')
                if start:
                    break
        if start:
            gen_clicked = True
            break
        log('❌ 点击 Generate reward 后未出现 Start reward。URL: ' + (page.url or ''))
        log('页面文本: ' + page_snippet(page, 500))
        shot(page, 'rewards_no_start')
        return False
    if not gen_clicked:
        shot(page, 'rewards_generate_exhausted')
        log('❌ Generate reward 重试次数用尽')
        return False

    # Start reward 表单如果也带了验证组件, 同样先通过再点
    if not turnstile_ready(page, 'Turnstile(start)', total=60):
        shot(page, 'rewards_start_turnstile_fail')
        log('❌ Start reward 前 Turnstile 未通过, 放弃本轮')
        return False
    human_pause()
    try:
        start.click()
    except Exception:
        if not js_click_btn_with_text(page, 'Start reward'):
            start = None
    if not start:
        log('ℹ️ "Start reward" 点击失败。URL: ' + (page.url or ''))
        log('页面文本: ' + page_snippet(page, 500))
        shot(page, 'rewards_no_start')
        return False
    log('🚀 已点击 Start reward, 等待 LootLabs 页面打开...')

    loot_tab = None
    deadline = time.time() + 45
    while time.time() < deadline and not loot_tab:
        time.sleep(2)
        if 'lootlabs' in (page.url or '').lower():
            loot_tab = page
            break
        try:
            for tid in page.tab_ids:
                try:
                    t = page.get_tab(tid)
                    if 'lootlabs' in (t.url or '').lower():
                        loot_tab = t
                        break
                except Exception:
                    continue
            if loot_tab:
                break
        except Exception:
            continue

    if not loot_tab:
        shot(page, 'no_lootlabs')
        log('❌ 未检测到 LootLabs 页面, 当前标签: ' + str(page.tab_ids))
        return False

    log('🎯 LootLabs 已打开: ' + (loot_tab.url or '')[:120])
    try:
        res = run_lootlabs(page, loot_tab)
    finally:
        cleanup_tabs(page, dash_tid)
    if res == 'ok':
        log('✅ 本轮 LootLabs 流程完成')
    elif res == 'stuck':
        log('♻️ 本轮遇到卡死广告, 将重新开始')
    else:
        log('⚠️ 本轮 LootLabs 流程未确认完成')
    return {'ok': True, 'stuck': 'stuck', 'fail': False}[res]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    if not USERNAME or not PASSWORD:
        log('❌ 缺少环境变量 MC_USERNAME / MC_PASSWORD')
        sys.exit(2)

    log('🚀 启动 (MAX_ROUNDS=' + str(MAX_ROUNDS) + ', DRY_RUN=' + str(DRY_RUN) + ')')
    page = build_page()
    try:
        last_err = None
        for attempt in range(1, 4):
            try:
                do_login(page)
                last_err = None
                break
            except Exception as e:
                last_err = e
                log('❌ 登录失败 (第 ' + str(attempt) + '/3 次): ' + str(e))
                shot(page, 'login_fail_' + str(attempt))
                time.sleep(5)
        if last_err is not None:
            raise last_err

        if DRY_RUN:
            page.get(REWARDS_URL)
            time.sleep(5)
            turnstile_ready(page, 'Turnstile(rewards)', total=60)
            log('页面文本片段: ' + page_snippet(page, 500))
            shot(page, 'rewards_dryrun')
            log('✅ DRY_RUN 完成: 登录、rewards 页面与 Turnstile 正常')
            return

        total_ok = 0
        for r in range(1, MAX_ROUNDS + 1):
            log('===== 第 ' + str(r) + '/' + str(MAX_ROUNDS) + ' 轮领取 =====')
            try:
                res = claim_round(page)
            except Exception as e:
                log('本轮异常: ' + str(e))
                traceback.print_exc()
                shot(page, 'round_' + str(r) + '_error')
                res = False
            if res is True:
                total_ok += 1
                time.sleep(20)  # 冷却 15 秒后再开下一轮
            elif res == 'stuck':
                continue  # 卡死轮不计冷却, 立刻重开
            else:
                log('本轮无法继续 (冷却/上限), 结束循环')
                break

        shot(page, 'final')
        log('🎉 运行结束, 共成功领取 ' + str(total_ok) + ' 轮')
        if total_ok == 0:
            sys.exit(1)
    finally:
        try:
            page.quit()
        except Exception:
            pass


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
