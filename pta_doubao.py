"""PTA ↔ 豆包 C 语言自动化（Windows 双击 BAT 一键启动）。

运行方式：将本文件与 START_PTA_Doubao.bat 放在同一目录，双击 BAT。
依赖：Python 3、Playwright；启动器会检查并尝试自动安装。
说明：网页结构可能变化；仅在允许 AI 辅助和自动提交的环境中使用。
编译及样例检查不能保证隐藏测试全部通过，更不能保证满分。
"""

import json
import re
import time
import hashlib
import difflib
import traceback
import shutil
import subprocess
import tempfile
import os
import sys
import configparser
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# =================== 用户配置 ===================
PTA_URL = "https://pintia.cn/"
DOUBAO_URL = "https://www.doubao.com/chat/"
MAX_QUESTIONS = 500
AUTO_SUBMIT = True  # 自动填写、提交并尝试进入下一题
WAIT_ANSWER_SECONDS = 240  # 豆包单题最多等待 180 秒
ENABLE_COMPILE_CHECK = True  # 找到 GCC/Clang 时编译检查
ENABLE_SAMPLE_CHECK = True   # 只有可靠提取到样例时才运行样例测试
REQUIRE_COMPILER = False     # True: 未找到编译器就停止提交；False: 打印警告并继续
PROFILE_PATH = str((Path(__file__).parent / "browser_profile").resolve())
LOG_DIR = Path(__file__).parent / "run_logs"

# 可选：修改 settings.ini，无须修改 Python 源码。
_settings = configparser.ConfigParser()
_settings.read(Path(__file__).with_name("settings.ini"), encoding="utf-8")
MAX_QUESTIONS = _settings.getint("automation", "max_questions", fallback=MAX_QUESTIONS)
AUTO_SUBMIT = _settings.getboolean("automation", "auto_submit", fallback=AUTO_SUBMIT)
WAIT_ANSWER_SECONDS = _settings.getint("automation", "wait_answer_seconds", fallback=WAIT_ANSWER_SECONDS)
ENABLE_COMPILE_CHECK = _settings.getboolean("verification", "compile_check", fallback=ENABLE_COMPILE_CHECK)
ENABLE_SAMPLE_CHECK = _settings.getboolean("verification", "sample_check", fallback=ENABLE_SAMPLE_CHECK)
REQUIRE_COMPILER = _settings.getboolean("verification", "require_compiler", fallback=REQUIRE_COMPILER)

# 以下是启发式选择器，并非 PTA / 豆包官方接口
PTA_PROBLEM_SELECTORS = [
    "[class*='problem-content']", "[class*='problemContent']",
    "[class*='problem_detail']", "[class*='problemDetail']",
    "article", "main",
]
DOUBAO_INPUT_SELECTORS = [
    "[data-testid*='chat-input'] [contenteditable='true']",
    "[data-testid*='input'] [contenteditable='true']",
    "[role='textbox'][contenteditable='true']",
    ".ProseMirror[contenteditable='true']",
    "[contenteditable='true'][data-placeholder]",
    "textarea[placeholder]",
    "[contenteditable='true']",
    "textarea",
]


PROMPT = """你是一名严格的 C11 程序设计与算法审查员。请独立完成下面的 PTA 编程题。

输出要求：只输出一个 ```c 代码块，必须是标准 C11，不能使用 C++，不能解释。
解题前请在内部进行以下检查（不要输出思考过程）：
1. 精确核对输入格式、输出格式、大小写、空格、换行以及多组数据处理。
2. 结合数据范围选取正确算法和复杂度；考虑最大值、最小值、空输入、重复值、负数、溢出、除零和数组越界等适用边界。
3. 需要时使用 long long / int64_t；避免未定义行为、非标准函数、固定数组过小、递归栈溢出。
4. 在内部模拟题目样例和至少三个边界情况；检查所有分支。
5. 复核代码的 C11 可编译性，并确保程序不输出提示文字或调试信息。
6. 如题意不足以确定正确程序，先指出不确定性，不要伪造确定答案。

题目：
{problem}
"""


def stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def first_visible(page, selectors):
    for css in selectors:
        try:
            matches = page.locator(css)
            for i in range(min(matches.count(), 8)):
                element = matches.nth(i)
                if element.is_visible():
                    return element
        except Exception:
            pass
    return None


def get_problem(page):
    candidates = []
    for selector in PTA_PROBLEM_SELECTORS:
        try:
            for el in page.locator(selector).all()[:6]:
                if el.is_visible():
                    txt = el.inner_text(timeout=3000).strip()
                    if len(txt) > 80:
                        candidates.append(txt)
        except Exception:
            continue
    if not candidates:
        raise RuntimeError("找不到 PTA 题目文本；请调整 PTA_PROBLEM_SELECTORS")
    # 优先使用最短的有效文本，避免获取整个页面的导航内容
    return min(candidates, key=len)


def _input_candidates(page):
    """从网页及内嵌 frame 中寻找最可能的聊天输入框。"""
    candidates = []
    for frame in page.frames:
        for priority, selector in enumerate(DOUBAO_INPUT_SELECTORS):
            try:
                loc = frame.locator(selector)
                for i in range(min(loc.count(), 12)):
                    el = loc.nth(i)
                    if not el.is_visible() or not el.is_enabled():
                        continue
                    metadata = el.evaluate("""e => ({
                        box: e.getBoundingClientRect().toJSON(),
                        placeholder: e.getAttribute('data-placeholder') ||
                                     e.getAttribute('placeholder') || '',
                        outer: e.outerHTML.slice(0, 300)
                    })""")
                    box = metadata['box']
                    if box['width'] < 120 or box['height'] < 15:
                        continue
                    hint = metadata['placeholder']
                    score = 100 - priority * 8 + min(box['width'], 1000) / 100
                    if any(k in hint.lower() for k in ('发送', '提问', '输入', '消息', '问问', 'ask', 'message')):
                        score += 20
                    if frame == page.main_frame:
                        score += 3
                    candidates.append((score, el, metadata))
            except Exception:
                continue
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates


def _editor_text(editor):
    try:
        return editor.evaluate("""e =>
            ('value' in e ? e.value : e.innerText || e.textContent || '').trim()
        """)
    except Exception:
        return ''


def send_prompt(page, prompt):
    page.bring_to_front()
    page.wait_for_timeout(1500)
    candidates = _input_candidates(page)
    if not candidates:
        raise RuntimeError('找不到豆包输入框。请确认豆包聊天页面已加载且已登录。')
    editor = candidates[0][1]
    print('识别到豆包输入框：', candidates[0][2]['outer'][:180], flush=True)

    editor.click(timeout=10000)
    try:
        editor.fill(prompt, timeout=15000)
    except Exception:
        # 某些富文本编辑器不能直接 fill，退化为键盘输入
        editor.click()
        page.keyboard.press('ControlOrMeta+A')
        page.keyboard.insert_text(prompt)

    typed = _editor_text(editor)
    if len(typed) < min(80, len(prompt) // 2):
        raise RuntimeError('文本没有成功写入豆包输入框，已停止，避免误操作。')
    print(f'已填入豆包输入框（{len(typed)} 字），正在点击发送……', flush=True)

    # 先尝试显式发送按钮；避免 Enter 在富文本框内仅换行
    sent_by_button = False
    for frame in page.frames:
        selectors = [
            "button[aria-label*='发送']", "button[title*='发送']",
            "[role='button'][aria-label*='发送']",
            "button[data-testid*='send']", "button[aria-label*='Send']",
            "button[title*='Send']",
        ]
        for selector in selectors:
            try:
                buttons = frame.locator(selector)
                for i in range(buttons.count() - 1, -1, -1):
                    btn = buttons.nth(i)
                    if btn.is_visible() and btn.is_enabled():
                        btn.click(timeout=3000)
                        sent_by_button = True
                        break
                if sent_by_button:
                    break
            except Exception:
                continue
        if sent_by_button:
            break
    if not sent_by_button:
        editor.press('Enter')

    # 必须验证输入框内容已消失，不能只凭按过 Enter 就声称成功
    for _ in range(25):
        page.wait_for_timeout(400)
        remaining = _editor_text(editor)
        if len(remaining) < 30:
            print('豆包输入框已清空，消息大概率发送成功。', flush=True)
            return

    raise RuntimeError(
        '发送后豆包输入框仍有题目内容：可能没有点中发送按钮，或 Enter 只换行。'
        '请查看 run_logs 错误截图。'
    )


def code_snapshot(page):
    """读取当前全部代码块；历史消息节点可能复用，因此不只看数量。"""
    try:
        return [x.strip() for x in page.locator("pre code").all_inner_texts()]
    except Exception:
        return []


def get_code(page, previous_blocks):
    """等待最新代码变化并稳定，兼容豆包复用历史代码块 DOM。"""
    deadline = time.monotonic() + WAIT_ANSWER_SECONDS
    previous_last = previous_blocks[-1] if previous_blocks else None
    last_candidate = None
    stable_since = None
    start = time.monotonic()
    next_report = 0

    while time.monotonic() < deadline:
        current = code_snapshot(page)
        newest = current[-1] if current else None
        changed = (
            newest is not None and len(newest) >= 15
            and (len(current) > len(previous_blocks) or newest != previous_last)
        )
        if changed:
            if newest != last_candidate:
                last_candidate = newest
                stable_since = time.monotonic()
            elif stable_since and time.monotonic() - stable_since >= 8:
                print(f"[豆包] 检测到本轮代码稳定：{len(newest)} 字符", flush=True)
                return newest
        else:
            last_candidate = None
            stable_since = None

        elapsed = int(time.monotonic() - start)
        if elapsed >= next_report:
            print(f"[豆包等待] {elapsed}s；历史块 {len(previous_blocks)}，当前块 {len(current)}；最新块变化={changed}", flush=True)
            next_report += 10
        page.wait_for_timeout(1000)

    name = LOG_DIR / f"doubao_timeout_{stamp()}.txt"
    name.write_text(
        f"发送前代码块: {len(previous_blocks)}\n"
        f"现在代码块: {len(code_snapshot(page))}\n"
        f"最后候选长度: {len(last_candidate or '')}\n"
        f"最后候选预览: {(last_candidate or '')[:1000]}\n",
        encoding='utf-8',
    )
    raise RuntimeError(f"豆包新代码识别超时，已保存诊断信息：{name}")


def read_pta_editor(page):
    """从 Monaco 模型或常见编辑器读取代码，用于检查写入结果。"""
    for frame in page.frames:
        try:
            value = frame.evaluate("""() => {
                if (window.monaco && window.monaco.editor) {
                    const models = window.monaco.editor.getModels();
                    if (models.length) return models[0].getValue();
                }
                const cm = document.querySelector('.cm-content[contenteditable="true"]');
                if (cm) return cm.innerText;
                const ta = document.querySelector('textarea:not([readonly])');
                if (ta && ta.offsetParent !== null) return ta.value;
                return null;
            }""")
            if value is not None:
                return value
        except Exception:
            pass
    return None


def fill_pta_editor(page, code):
    """只操作编辑器，禁止误选整页；写入后必须核对内容。"""
    page.bring_to_front()
    code = code.strip()
    if not code or len(code) < 20:
        raise RuntimeError('生成代码为空或过短，不填写 PTA')

    for frame in page.frames:
        try:
            # 优先用 Monaco 官方编辑器模型写入，避开 Ctrl+A 选中整页的问题。
            changed = frame.evaluate("""code => {
                if (!(window.monaco && window.monaco.editor)) return false;
                const models = window.monaco.editor.getModels();
                if (!models.length) return false;
                models[0].setValue(code);
                return models[0].getValue() === code;
            }""", code)
            if changed:
                print('Monaco 模型写入成功')
                break
        except Exception:
            continue
    else:
        # 找不到 Monaco 全局模型时，只点击编辑器内部可编辑区域。
        edited = False
        for frame in page.frames:
            for selector in ('.monaco-editor .view-lines',
                             '.cm-content[contenteditable="true"]',
                             '.CodeMirror-code',
                             'textarea:not([readonly])'):
                try:
                    loc = frame.locator(selector).first
                    if not loc.is_visible(timeout=1200):
                        continue
                    loc.click(timeout=3000)
                    # 仅在确认焦点属于目标编辑器后使用 Ctrl+A。
                    focused = frame.evaluate("""() => {
                        const a = document.activeElement;
                        return !!a && (!!a.closest('.monaco-editor, .cm-editor, .CodeMirror') ||
                            (a.tagName === 'TEXTAREA' && !a.readOnly));
                    }""")
                    if not focused:
                        continue
                    page.keyboard.press('ControlOrMeta+A')
                    page.keyboard.insert_text(code)
                    edited = True
                    break
                except Exception:
                    continue
            if edited:
                break
        if not edited:
            raise RuntimeError('未找到可聚焦的 PTA 代码编辑器，未执行提交')

    page.wait_for_timeout(800)
    actual = read_pta_editor(page)
    if actual is None:
        raise RuntimeError('无法读取 PTA 编辑器内容，出于安全考虑停止自动提交')
    def normalized(value):
        # 统一换行符，仅忽略空白行差异（PTA 编辑器偶尔多插入空白行）。
        # 保留每一行非空代码的原始内容，变量名、运算符、缩进均须一致。
        value = value.replace('\r\n', '\n').replace('\r', '\n')
        return '\n'.join(line for line in value.split('\n') if line.strip())

    expected = normalized(code)
    received = normalized(actual)
    if expected != received:
        # 重新尝试用 Monaco 模型写入一次，再做严格核对。
        for frame in page.frames:
            try:
                did_retry = frame.evaluate('''value => {
                    if (!window.monaco || !window.monaco.editor) return false;
                    const models = window.monaco.editor.getModels();
                    if (!models.length) return false;
                    models[0].setValue(value);
                    return true;
                }''', code)
                if did_retry:
                    page.wait_for_timeout(500)
                    actual = read_pta_editor(page)
                    received = normalized(actual) if actual is not None else ''
                    break
            except Exception:
                continue

    if expected != received:
        changes = list(difflib.ndiff(expected, received))
        details = [x for x in changes if x.startswith('- ') or x.startswith('+ ')]
        mismatch_file = LOG_DIR / f'editor_mismatch_{stamp()}.txt'
        mismatch_file.parent.mkdir(exist_ok=True)
        mismatch_file.write_text(
            f'期望长度: {len(code)}，实际长度: {len(actual) if actual is not None else 0}\n'
            + '差异字符（- 为预期，+ 为编辑器）：\n'
            + '\n'.join(repr(x) for x in details[:30])
            + '\n\n=== 期望代码 ===\n' + code
            + '\n\n=== 编辑器代码 ===\n' + str(actual),
            encoding='utf-8'
        )
        raise RuntimeError(
            f'PTA 编辑器内容不一致（期望 {len(code)}，实际 {len(actual) if actual is not None else 0} 字符）。'
            f'差异日志：{mismatch_file}；为了避免提交错误代码，已停止。'
        )
    print(f'已核实 PTA 编辑器代码一致（{len(expected)} 字符，纯空白行差异已忽略）', flush=True)


def click_named(page, expressions):
    for expression in expressions:
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=re.compile(expression, re.I))
                for el in loc.all():
                    if el.is_visible() and el.is_enabled():
                        el.click(timeout=4000)
                        return True
            except Exception:
                continue
        try:
            loc = page.get_by_text(re.compile(expression, re.I), exact=True)
            for el in loc.all()[:4]:
                if el.is_visible():
                    el.click(timeout=4000)
                    return True
        except Exception:
            continue
    return False


def dump_pta_controls(page):
    """输出网页当前可见的按钮，便于定位网站版本差异。"""
    print("\n[调试] 当前 PTA 页面：", page.url)
    lines = []
    for frame in page.frames:
        try:
            buttons = frame.locator("button, [role='button'], input[type='submit'], input[type='button']")
            for i in range(min(buttons.count(), 150)):
                b = buttons.nth(i)
                if not b.is_visible():
                    continue
                info = b.evaluate("""el => ({text:(el.innerText||el.textContent||el.value||'').trim().slice(0,100),
                    title:el.getAttribute('title'),aria:el.getAttribute('aria-label'),
                    testid:el.getAttribute('data-e2e'),disabled:!!el.disabled})""")
                lines.append({"frame": frame.url, **info})
        except Exception:
            pass
    for item in lines:
        print("[按钮]", item)
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"pta_buttons_{stamp()}.json"
    path.write_text(json.dumps(lines, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[调试] 按钮清单已保存：{path}")


def submit(page):
    # 滚动至页面底部：不少 PTA 版本的提交按钮位于编辑器下方。
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(1.0)

    names = [
        r"提交本题作答", r"Submit For This Problem", r"递交评测",
        r"提交代码", r"提交本题", r"^提交$", r"^Submit$"
    ]
    # 优先匹配真实按钮，避免误点“提交试卷”或“结束考试”。
    for frame in page.frames:
        for expression in names:
            for role in ("button",):
                try:
                    matches = frame.get_by_role(role, name=re.compile(expression, re.I))
                    for btn in matches.all():
                        if btn.is_visible() and btn.is_enabled():
                            btn.scroll_into_view_if_needed()
                            btn.click(timeout=5000)
                            print(f"已点击提交按钮：{expression}")
                            time.sleep(1)
                            click_named(page, [r"^确认$", r"^确定$", r"^Confirm$"])
                            return
                except Exception:
                    continue

        # 有些图标按钮的可见文本在嵌套 span 中，辅助按文本查找父 button。
        try:
            candidates = frame.locator("button, [role='button'], input[type='submit']")
            for i in range(min(candidates.count(), 200)):
                btn = candidates.nth(i)
                if not btn.is_visible() or not btn.is_enabled():
                    continue
                data = btn.evaluate("""el => [el.innerText,el.textContent,el.value,
                    el.getAttribute('aria-label'),el.getAttribute('title')].filter(Boolean).join(' ').trim()""")
                if any(re.search(x, data, re.I) for x in names):
                    btn.scroll_into_view_if_needed()
                    btn.click(timeout=5000)
                    print(f"已点击提交按钮：{data[:80]}")
                    time.sleep(1)
                    click_named(page, [r"^确认$", r"^确定$", r"^Confirm$"])
                    return
        except Exception:
            pass

    dump_pta_controls(page)
    raise RuntimeError('提交按钮不存在或处于禁用状态；请检查代码是否真正出现在编辑器中')


def next_problem(page, previous):
    if not click_named(page, [r"下一题", r"下一个题目", r"^Next$"]):
        return False
    for _ in range(20):
        time.sleep(0.75)
        try:
            if get_problem(page) != previous:
                return True
        except Exception:
            pass
    return False



def _normalize_output(value):
    """样例仅做宽松的空白比较，不替代 PTA 判题。"""
    return value.replace('\r\n', '\n').strip().split()


def _extract_sample(problem):
    """保守识别一组 PTA 输入/输出样例；无法确定时跳过。"""
    pattern = (r'(?:^|\n)\s*输入样例\s*[:：]?\s*\n'
               r'(?P<input>.*?)'
               r'(?:\n)\s*输出样例\s*[:：]?\s*\n'
               r'(?P<output>.*?)'
               r'(?=\n\s*(?:代码长度限制|时间限制|内存限制|提示|注意|作者|单位|输入格式|输出格式|题目列表|$)|\Z)')
    m = re.search(pattern, problem, re.S | re.I)
    if not m:
        return None
    sample_input = m.group('input').strip()
    sample_output = m.group('output').strip()
    # 网页抓取可能把按钮、导航、代码块装饰混入样例；宁可跳过也不误判。
    for value in (sample_input, sample_output):
        if not value or len(value) > 3000 or any(s in value for s in ('复制内容', '全屏', '```', '提交本题')):
            return None
    return sample_input + '\n', sample_output


def precheck_code(code, problem, round_number):
    """编译并在可靠提取到样例时运行；不能证明隐藏测试满分。"""
    if not ENABLE_COMPILE_CHECK:
        print('[校验] 已关闭编译检查', flush=True)
        return
    compiler = shutil.which('gcc') or shutil.which('clang')
    if not compiler and os.name == 'nt':
        for path in (r'C:\msys64\mingw64\bin\gcc.exe',
                     r'C:\msys64\ucrt64\bin\gcc.exe',
                     r'C:\msys64\clang64\bin\clang.exe'):
            if Path(path).is_file():
                compiler = path
                break
    if not compiler:
        message = '[校验] 未检测到 GCC/Clang，无法本地编译检查。'
        if REQUIRE_COMPILER:
            raise RuntimeError(message + ' 已按配置停止提交。')
        print(message + ' 将继续，但不代表代码正确。', flush=True)
        return

    with tempfile.TemporaryDirectory(prefix='pta_c_check_') as temp:
        path = Path(temp)
        src = path / 'solution.c'
        exe = path / ('solution.exe' if os.name == 'nt' else 'solution')
        src.write_text(code, encoding='utf-8')
        cmd = [compiler, '-std=c11', '-O2', '-Wall', '-Wextra', str(src), '-o', str(exe)]
        try:
            compiled = subprocess.run(cmd, capture_output=True, text=True,
                                      encoding='utf-8', errors='replace', timeout=30)
        except subprocess.TimeoutExpired:
            raise RuntimeError('C 语言编译检查超时，已停止提交')
        if compiled.returncode != 0:
            log = LOG_DIR / f'compile_failed_{round_number:03d}.txt'
            log.write_text(compiled.stdout + '\n' + compiled.stderr, encoding='utf-8')
            raise RuntimeError(f'C11 编译失败，已停止提交。详情：{log}')
        print(f'[校验] C11 编译通过（{Path(compiler).name}）', flush=True)
        sample = _extract_sample(problem) if ENABLE_SAMPLE_CHECK else None
        if not sample:
            print('[校验] 未可靠提取到输入输出样例，跳过样例运行。', flush=True)
            return
        sample_input, expected_output = sample
        try:
            result = subprocess.run([str(exe)], input=sample_input,
                                    capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=5)
        except subprocess.TimeoutExpired:
            raise RuntimeError('样例运行超时，已停止提交')
        if result.returncode != 0 or _normalize_output(result.stdout) != _normalize_output(expected_output):
            log = LOG_DIR / f'sample_failed_{round_number:03d}.txt'
            log.write_text('=== 输入 ===\n' + sample_input + '\n=== 期望 ===\n'
                           + expected_output + '\n=== 实际 ===\n' + result.stdout
                           + '\n=== 错误 ===\n' + result.stderr, encoding='utf-8')
            raise RuntimeError(f'样例检查未通过，已停止提交。详情：{log}')
        print('[校验] 可识别样例运行通过（不代表隐藏测试满分）', flush=True)


def launch_browser(playwright):
    """优先使用已安装的 Playwright Chromium，随后尝试 Chrome/Edge。"""
    errors = []
    for label, opts in [('Playwright Chromium', {}),
                        ('Google Chrome', {'channel': 'chrome'}),
                        ('Microsoft Edge', {'channel': 'msedge'})]:
        try:
            context = playwright.chromium.launch_persistent_context(
                PROFILE_PATH, headless=False,
                viewport={'width': 1440, 'height': 900}, **opts)
            print(f'[浏览器] 已启动 {label}', flush=True)
            return context
        except Exception as exc:
            errors.append(f'{label}: {str(exc)[:180]}')
    raise RuntimeError('未能启动浏览器。可先运行 python -m playwright install chromium\n'
                       + '\n'.join(errors))

def main():
    if not 1 <= MAX_QUESTIONS <= 1000:
        raise ValueError("settings.ini 的 max_questions 必须在 1~1000 之间")
    LOG_DIR.mkdir(exist_ok=True)
    print(f"[配置] 最多处理 {MAX_QUESTIONS} 题；自动提交={AUTO_SUBMIT}；编译检查={ENABLE_COMPILE_CHECK}；样例检查={ENABLE_SAMPLE_CHECK}", flush=True)
    print("[提示] 高质量校验模式：必须通过本地编译；样例可识别时必须通过。无法保证正确率达到95%。", flush=True)
    print("[提示] 未核实 PTA 判题结果前，不把提交次数计为正确题数。", flush=True)
    seen = set()
    log_file = LOG_DIR / f"session_{stamp()}.jsonl"

    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        pta = browser.new_page()
        doubao = browser.new_page()
        pta.goto(PTA_URL, wait_until="domcontentloaded")
        doubao.goto(DOUBAO_URL, wait_until="domcontentloaded")

        print("请登录 PTA 和豆包，在 PTA 打开第一道编程题，并将语言设为 C (gcc)。")
        input("准备好后，在此窗口按回车开始：")

        for number in range(1, MAX_QUESTIONS + 1):
            print(f"\n========== 第 {number} 题 ==========")
            try:
                print(f"[阶段 1/6] 读取 PTA 题目；页面: {pta.url}", flush=True)
                problem = get_problem(pta)
                print(f"题目读取成功，长度 {len(problem)} 字，开头：{problem[:75]!r}", flush=True)
                digest = hashlib.sha256(problem.encode("utf-8")).hexdigest()
                if digest in seen:
                    print("检测到重复题目，为避免重复提交，停止运行。")
                    break
                seen.add(digest)

                previous_code_blocks = code_snapshot(doubao)
                print(f"[阶段 2/6] 发送到豆包；发送前代码块数量 {len(previous_code_blocks)}", flush=True)
                send_prompt(doubao, PROMPT.format(problem=problem))
                print("已通过发送后输入框检查，正在等待豆包返回 C 语言代码……")
                code = get_code(doubao, previous_code_blocks)
                print(f"[阶段 3/6] 收到 C 代码：{len(code)} 字符", flush=True)
                (LOG_DIR / f"doubao_round_{number:03d}.c").write_text(code, encoding="utf-8")

                if any(x in code for x in ("#include <iostream>", "std::", "using namespace std", "#include <bits/stdc++.h>")):
                    raise RuntimeError("检测到疑似 C++ 代码，停止提交，请人工检查")
                if re.search(r'(?m)^\s*#include\s*<\s*(iostream|bits/stdc\+\+\.h)\s*>', code):
                    raise RuntimeError("发现 C++ 头文件，停止提交")
                if not re.search(r"\bmain\s*\(", code):
                    raise RuntimeError("豆包回答缺少 main 函数，可能提取了错误代码块，已停止")

                print('[校验] 提交前 C 语言编译及样例检查', flush=True)
                precheck_code(code, problem, number)
                print("[阶段 4/6] 写入并核对 PTA 编辑器", flush=True)
                fill_pta_editor(pta, code)
                print("已填写 PTA 编辑器。")
                (LOG_DIR / f"question_{number:03d}_{digest[:8]}.c").write_text(code, encoding="utf-8")

                if AUTO_SUBMIT:
                    print("[阶段 5/6] 点击 PTA 提交按钮", flush=True)
                    submit(pta)
                    print("已点击提交。判题状态尚未核实；编译和样例测试不能证明隐藏测试通过。")
                    time.sleep(4)
                else:
                    input("请核对代码并手动提交，完成后按回车继续：")

                with log_file.open("a", encoding="utf-8") as out:
                    out.write(json.dumps({"question": number, "hash": digest, "auto_submit": AUTO_SUBMIT,
                                          "time": datetime.now().isoformat()}, ensure_ascii=False) + "\n")

                print("[阶段 6/6] 切换下一题", flush=True)
                if not next_problem(pta, problem):
                    print("没有找到下一题，或者下一题未成功打开，已停止。")
                    break

            except Exception as exc:
                print(f"错误：{exc}", flush=True)
                traceback.print_exc()
                for name, page in (("pta", pta), ("doubao", doubao)):
                    try:
                        page.screenshot(path=str(LOG_DIR / f"error_{name}_{stamp()}.png"), full_page=True)
                    except Exception:
                        pass
                print(f"截图与日志保存在：{LOG_DIR}", flush=True)
                print("本题失败后已停止，不会盲目提交或跳过。", flush=True)
                break

        input("任务结束，按回车关闭浏览器：")
        browser.close()


if __name__ == "__main__":
    main()
