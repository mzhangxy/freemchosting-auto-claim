#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FreeMC Hosting 自动领取 Credits (DrissionPage + GitHub Actions)

流程:
  1. 打开 https://dash.freemchosting.com/login, 填写账号密码
  2. 处理 Cloudflare Turnstile (点击验证框, 等待 cf-turnstile-response token)
  3. 点击 Sign in 登录
  4. 进入 /rewards: 点击 Generate reward -> Start reward
  5. 处理 LootLabs 任务流: 关闭广告弹窗 / 勾选验证 / 点击 Continue / 领取奖励

环境变量:
  MC_USERNAME, MC_PASSWORD   必填, 网站账号密码
  MAX_ROUNDS                 领取轮数, 默认 2
  LOOT_MAX_SECONDS           单轮 LootLabs 最长处理时间, 默认 480
  DRY_RUN                    1 = 只验证登录并打开 rewards 页, 不实际领取
  SHOT_DIR                   截图目录, 默认 screenshots
"""

import os
import re
import sys
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
MAX_ROUNDS = int(os.environ.get('MAX_ROUNDS', '2') or '2')
LOOT_MAX = int(os.environ.get('LOOT_MAX_SECONDS', '480') or '480')
DRY_RUN = os.environ.get('DRY_RUN', '0') == '1'
SHOT_DIR = os.environ.get('SHOT_DIR', 'screenshots')

VERIFY_RE = re.compile(
    r'confirm|human|验证|captcha|turnstile|hcaptcha|recaptcha|video|视频|watch|观看', re.I)


def log(msg):
    print(time.strftime('[%H:%M:%S] ') + str(msg), flush=True)


def shot(scope, name):
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        scope.get_screenshot(path=SHOT_DIR, name=name, full_page=True)
        log('📸 截图: ' + name + '.png')
    except Exception as e:
        log('截图失败(' + name + '): ' + str(e))


def human_pause():
    time.sleep(random.uniform(0.6, 1.4))


def page_snippet(scope, n=400):
    try:
        return re.sub(r'\s+', ' ', (scope.text or ''))[:n]
    except Exception:
        return ''


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
    co.auto_port(True)
    co.set_argument('--no-sandbox')
    co.set_argument('--disable-dev-shm-usage')
    co.set_argument('--disable-gpu')
    co.set_argument('--window-size=1360,900')
    co.set_argument('--lang=en-US')
    co.set_argument('--disable-blink-features=AutomationControlled')
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

BLOCK_OPEN_JS = r"""
if (!window.__origOpen) { window.__origOpen = window.open; }
window.open = function () { return null; };
return true;
"""

RESTORE_OPEN_JS = r"""
if (window.__origOpen) { window.open = window.__origOpen; window.__origOpen = null; }
return true;
"""

LOOT_PEEK_JS = r"""
const idle = document.querySelector('.task-ind.ind-idle');
if (!idle) return '';
const task = idle.closest('.task') || idle.parentElement;
return (task && task.textContent) ? task.textContent : 'task';
"""

LOOT_CLICK_TASK_JS = r"""
const idle = document.querySelector('.task-ind.ind-idle');
if (idle) { idle.click(); return true; }
return false;
"""

LOOT_IDLE_COUNT_JS = r"""
return document.querySelectorAll('.task-ind.ind-idle').length;
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

# 判断领取按钮是否可点 (unlockBtn 可用 / Mission Complete / 按钮变成 go|is-success)
LOOT_CLAIM_READY_JS = r"""
const btn = document.getElementById('unlockBtn');
const ready = document.getElementById('readyText');
const vis = e => !!e && e.offsetParent !== null;
const enabled = vis(btn) && !btn.disabled;
const mission = vis(ready) && /Mission Complete/i.test(ready.textContent || '');
const go = vis(btn) && (btn.classList.contains('go') || btn.classList.contains('is-success'));
if (enabled || mission || go) return 'ready';
return 'wait';
"""

LOOT_CLAIM_CLICK_JS = r"""
const btn = document.getElementById('unlockBtn');
if (btn) { btn.click(); return 'btn'; }
const alt = Array.from(document.querySelectorAll('button, a, div, span'))
  .find(e => e.offsetParent !== null && /CLAIM\s*REWARD/i.test(e.textContent || ''));
if (alt) { alt.click(); return 'alt'; }
return '';
"""


# ---------------------------------------------------------------------------
# LootLabs 流程
# ---------------------------------------------------------------------------

def _tab_id(t):
    try:
        return t.tab_id
    except Exception:
        return id(t)


def handle_popup(page, tab, tid, info, popups, claim_clicked_at, throttle):
    """处理任务弹出的标签页: 验证弹窗点 Continue 后关闭, 广告弹窗 6 秒后关闭"""
    try:
        if claim_clicked_at and time.time() - claim_clicked_at < 60:
            return  # 领取后打开的标签先不动
        age = time.time() - info['first_seen']
        if info['verify']:
            if turnstile_present(tab):
                click_turnstile(tab)
            if not info.get('continue_clicked'):
                if click_continue(tab, 'verify-popup', throttle):
                    info['continue_clicked'] = time.time()
            if info.get('continue_clicked') and time.time() - info['continue_clicked'] > 3:
                tab.close()
                popups.pop(tid, None)
                log('🧹 验证弹窗已处理并关闭')
            elif age > 100:
                tab.close()
                popups.pop(tid, None)
                log('🧹 验证弹窗超时, 强制关闭')
        else:
            if age > 6:
                tab.close()
                popups.pop(tid, None)
                log('🧹 广告弹窗已关闭')
    except Exception:
        popups.pop(tid, None)


def run_lootlabs(page, tab):
    log('⏳ 开始处理 LootLabs 任务...')
    t0 = time.time()
    popups = {}
    cont_throttle = [0.0]
    popup_throttle = [0.0]
    in_task = False
    task_started_at = 0.0
    task_is_verify = False
    verify_until = 0.0
    verify_popup_seen = False
    claim_clicked_at = None
    loot_tid = _tab_id(tab)

    while time.time() - t0 < LOOT_MAX:
        time.sleep(1.5)

        # ---- 巡检所有标签页 ----
        loot_tab_obj = None
        try:
            tids = list(page.tab_ids)
        except Exception:
            continue
        for tid in tids:
            try:
                t = page.get_tab(tid)
                u = (t.url or '').lower()
            except Exception:
                continue
            if tid == loot_tid:
                loot_tab_obj = t
            elif 'lootlabs' not in u and 'freemchosting' not in u:
                is_verify_popup = time.time() < verify_until and not verify_popup_seen
                info = popups.setdefault(tid, {'first_seen': time.time(), 'verify': is_verify_popup})
                if is_verify_popup:
                    verify_popup_seen = True
                handle_popup(page, t, tid, info, popups, claim_clicked_at, popup_throttle)

        if loot_tab_obj is None:
            log('❌ LootLabs 标签页已关闭')
            return False

        try:
            cur = loot_tab_obj.url or ''
        except Exception:
            continue
        cur_l = cur.lower()

        # ---- 领取后跳转 = 本轮成功 ----
        if claim_clicked_at and 'lootlabs' not in cur_l:
            log('✅ 领取后页面已跳转: ' + cur[:120])
            return True

        # ---- 任务中途被广告跳走 → 返回 ----
        if 'lootlabs' not in cur_l:
            if 'freemchosting' in cur_l:
                log('✅ 已回到 FreeMC Hosting')
                return True
            log('↩️ LootLabs 页面被跳转到: ' + cur[:100] + ' , 返回...')
            try:
                loot_tab_obj.back()
            except Exception:
                pass
            time.sleep(2)
            continue

        # ---- lootlabs 页面常规处理 ----
        modal = js_text(loot_tab_obj, LOOT_MODAL_JS)
        if modal:
            log('⚠️ 检测到 "Action not completed" 弹窗: ' + str(modal))
            if modal == 'watch':
                # 重新以验证模式尝试: 允许弹窗
                verify_until = time.time() + 60
                verify_popup_seen = False
                in_task = False
                js_run(loot_tab_obj, RESTORE_OPEN_JS)

        click_continue(loot_tab_obj, 'lootlabs', cont_throttle)

        if turnstile_present(loot_tab_obj) and not get_turnstile_token(loot_tab_obj):
            click_turnstile(loot_tab_obj)

        idle_count = js_run(loot_tab_obj, LOOT_IDLE_COUNT_JS, 0) or 0
        try:
            idle_count = int(idle_count)
        except Exception:
            idle_count = 0

        if in_task:
            limit = 90 if task_is_verify else 25
            if idle_count == 0 or time.time() - task_started_at > limit:
                in_task = False

        if not in_task:
            if idle_count > 0:
                txt = js_text(loot_tab_obj, LOOT_PEEK_JS)
                is_verify = bool(txt and VERIFY_RE.search(txt))
                if not is_verify:
                    js_run(loot_tab_obj, BLOCK_OPEN_JS)  # 广告任务: 阻止弹窗
                if js_bool(loot_tab_obj, LOOT_CLICK_TASK_JS):
                    in_task = True
                    task_started_at = time.time()
                    task_is_verify = is_verify
                    if is_verify:
                        verify_until = time.time() + 60
                        verify_popup_seen = False
                    kind = '[验证]' if is_verify else '[广告]'
                    log('📌 点击任务 (剩 ' + str(idle_count) + ' 个): ' +
                        kind + ' ' + (txt or 'task').strip().replace('\n', ' ')[:70])
            else:
                # 没有待办任务 → 尝试领取
                state = js_text(loot_tab_obj, LOOT_CLAIM_READY_JS)
                if state == 'ready':
                    clicked = ''
                    try:
                        btn = loot_tab_obj.ele('#unlockBtn', timeout=2)
                        if btn:
                            btn.click()
                            clicked = 'btn'
                    except Exception:
                        pass
                    if not clicked:
                        clicked = js_text(loot_tab_obj, LOOT_CLAIM_CLICK_JS)
                    if clicked:
                        if not claim_clicked_at:
                            claim_clicked_at = time.time()
                            log('🎉 已点击领取按钮 (' + str(clicked) + '), 等待跳转...')

        if claim_clicked_at and time.time() - claim_clicked_at > 90:
            log('⏰ 领取后长时间无跳转, 重试点击...')
            claim_clicked_at = None

    shot(page, 'lootlabs_timeout')
    log('⚠️ LootLabs 处理超时 (' + str(LOOT_MAX) + 's)')
    return False


# ---------------------------------------------------------------------------
# Rewards 页面领取一轮
# ---------------------------------------------------------------------------

def find_btn_by_text(scope, text):
    """优先找可见的 button/a 标签, 其次任意可见元素, 供真实点击"""
    try:
        els = scope.eles('text_:' + text)
    except Exception:
        return None
    best = None
    for el in (els or []):
        try:
            if el.tag in ('button', 'a') and el.states.is_displayed:
                best = el
                break
        except Exception:
            continue
    if best is None:
        for el in reversed(els or []):
            try:
                if el.states.is_displayed:
                    best = el
                    break
            except Exception:
                continue
    return best


def claim_round(page):
    log('🧭 打开 Rewards 页面: ' + REWARDS_URL)
    page.get(REWARDS_URL)
    time.sleep(4)
    wait_turnstile(page, total=25, tag='Turnstile(rewards)')
    time.sleep(2)

    gen = find_btn_by_text(page, 'Generate reward')
    if not gen:
        log('ℹ️ 未找到 "Generate reward" 按钮 (可能冷却中)。页面文本: ' + page_snippet(page))
        shot(page, 'rewards_no_generate')
        return False
    human_pause()
    gen.click()
    log('🪙 已点击 Generate reward')

    start = None
    for _ in range(20):
        time.sleep(2)
        start = find_btn_by_text(page, 'Start reward')
        if start:
            break
    if not start:
        log('ℹ️ 未出现 "Start reward" 按钮。页面文本: ' + page_snippet(page))
        shot(page, 'rewards_no_start')
        return False
    human_pause()
    start.click()
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
    ok = run_lootlabs(page, loot_tab)
    log('✅ 本轮 LootLabs 流程完成' if ok else '⚠️ 本轮 LootLabs 流程未确认完成')
    return ok


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
            log('页面文本片段: ' + page_snippet(page, 500))
            shot(page, 'rewards_dryrun')
            log('✅ DRY_RUN 完成: 登录与 rewards 页面访问正常')
            return

        total_ok = 0
        for r in range(1, MAX_ROUNDS + 1):
            log('===== 第 ' + str(r) + '/' + str(MAX_ROUNDS) + ' 轮领取 =====')
            try:
                ok = claim_round(page)
            except Exception as e:
                log('本轮异常: ' + str(e))
                traceback.print_exc()
                shot(page, 'round_' + str(r) + '_error')
                ok = False
            if ok:
                total_ok += 1
                time.sleep(20)  # 冷却 15 秒后再开下一轮
            else:
                log('本轮未成功, 停止后续轮次')
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
