# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""页面与资源的一致性检查。

HTML 只留结构，样式在 `pages/assets/app.css`，脚本在 `pages/assets/app.js`，
两者都由插件自己的 `/astrbot_plugin_sandbox/api/asset` 路由提供。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

def strip_js(code: str) -> str:
    """去掉注释、字符串、正则字面量，只留真正的代码结构。"""
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    # 先剥字符串：否则 "//" 这种字符串会被当成注释开头，把后面整段吃掉
    code = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', code)
    code = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", code)
    code = re.sub(r"`(?:[^`\\]|\\.)*`", "``", code)
    code = re.sub(r"//[^\n]*", "", code)
    return code


ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "pages"
PAGE_DIR = PAGES / "sandbox"
OUTER = PAGES / "index.html"
INNER = PAGE_DIR / "index.html"
CSS = PAGE_DIR / "app.css"
JS = PAGE_DIR / "app.js"


class TestAssets(unittest.TestCase):
    def test_assets_exist(self):
        for path in (CSS, JS):
            self.assertTrue(path.is_file(), f"{path.name} 缺失")
            self.assertGreater(path.stat().st_size, 1000, f"{path.name} 太小，可能没抽出来")

    def test_css_has_no_external_reference(self):
        css = CSS.read_text("utf-8")
        self.assertNotIn("url(http", css)
        self.assertNotIn("@import", css)
        self.assertIn("prefers-color-scheme", css)

    def test_js_balanced(self):
        """有 node 就让 V8 验语法；没有就只数花括号（括号会被正则字面量干扰）。"""
        node = shutil.which("node")
        if node:
            result = subprocess.run(
                [node, "--check", str(JS)], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return
        js = strip_js(JS.read_text("utf-8"))
        self.assertEqual(js.count("{"), js.count("}"), "花括号不配平")

    def test_js_ids_exist_in_html(self):
        js = JS.read_text("utf-8")
        html = INNER.read_text("utf-8")
        ids = set(re.findall(r'\bid="([^"]+)"', html))
        refs = set(re.findall(r'\$\("([^"]+)"\)', js))
        self.assertEqual(sorted(refs - ids), [], "这些 id 被引用但不存在")


class TestPageLayout(unittest.TestCase):
    def test_page_dir_has_everything(self):
        for path in (INNER, CSS, JS):
            self.assertTrue(path.is_file(), f"{path.name} 缺失")

    def test_outer_index_is_redirect_stub(self):
        stub = OUTER.read_text("utf-8")
        self.assertIn("sandbox/index.html", stub)
        self.assertLess(len(stub), 1000, "pages/index.html 应该只是跳转桩，不是页面副本")

    def test_has_expected_tabs(self):
        html = INNER.read_text("utf-8")
        for tab in ("overview", "config", "setup", "users", "files", "exec", "rules", "logs"):
            self.assertIn(f'data-tab="{tab}"', html, f"缺少 {tab} 标签")
            self.assertIn(f'id="panel-{tab}"', html, f"缺少 {tab} 面板")

    def test_no_duplicate_ids(self):
        html = INNER.read_text("utf-8")
        ids = re.findall(r'\bid="([^"]+)"', html)
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        self.assertEqual(duplicates, [], f"重复 id：{duplicates}")

    def test_no_inline_style_or_script(self):
        html = INNER.read_text("utf-8")
        self.assertNotIn("<style>", html)
        self.assertNotIn("<script>", html)

    def test_includes_bridge_sdk(self):
        """AstrBot 没自动插入时（静态访问）要能自己引。"""
        html = INNER.read_text("utf-8")
        self.assertIn("/api/plugin/page/bridge-sdk.js", html)

    def test_references_assets_relatively(self):
        """和别的插件一致：资源放在 index.html 旁边，用相对路径引用。"""
        html = INNER.read_text("utf-8")
        self.assertIn('href="./app.css"', html)
        self.assertIn('src="./app.js"', html)

    def test_no_external_resources(self):
        html = INNER.read_text("utf-8")
        for attr in ('src="http', "src='http", 'href="http', "href='http"):
            self.assertNotIn(attr, html, f"页面引用了外部资源：{attr}")

    def test_profile_options_are_dynamic(self):
        """套餐选项不能写死在 HTML 里，否则后端换发行版页面还显示旧名字。"""
        html = INNER.read_text("utf-8")
        self.assertNotIn("Debian 常用", html)
        self.assertNotIn("Debian 全量", html)
        js = JS.read_text("utf-8")
        self.assertIn("fillProfiles", js)
        self.assertIn("data.profiles", js)

    def test_has_onboarding_tour(self):
        """漫游引导：首次自动弹出，之后可以从顶栏重看。"""
        html = INNER.read_text("utf-8")
        self.assertIn('id="tour-start"', html)
        js = JS.read_text("utf-8")
        self.assertIn("TOUR_STEPS", js)
        self.assertIn("tourShow", js)
        # 引导「看过没」现在存服务端（iframe 里 localStorage 不可用）
        self.assertIn("UI.tour_seen", js)
        self.assertIn('api("/ui/prefs"', js)
        self.assertIn("tour-hole", js)

    def test_preview_failure_is_visible(self):
        """预览失败要把原因写在预览区，不能只弹 toast。"""
        js = JS.read_text("utf-8")
        self.assertIn("预览失败：", js)
        self.assertIn("下载", js)

    def test_config_form_and_root_switch(self):
        """设置页要能直接改配置，包括「沙盒内是否 root」。"""
        html = INNER.read_text("utf-8")
        self.assertIn('id="config-form"', html)
        self.assertIn('id="config-save"', html)
        js = JS.read_text("utf-8")
        self.assertIn("chroot_uid", js)
        self.assertIn("nobody", js)
        # 端点叫 settings，不叫 config：/config 会和宿主 AstrBot 自己的 /api/config 撞车
        self.assertIn("/settings/save", js)

    def test_has_download_and_copy_buttons(self):
        """不依赖 AstrBot 的文件发送通道，页面自己也能把文件拿出来。"""
        html = INNER.read_text("utf-8")
        self.assertIn('id="preview-download"', html)
        self.assertIn('id="preview-copy"', html)
        self.assertIn("/file/raw", JS.read_text("utf-8"))

    def test_has_boot_status_and_noscript(self):
        html = INNER.read_text("utf-8")
        self.assertIn('id="boot-status"', html)
        self.assertIn("<noscript>", html)


class TestScriptBehaviour(unittest.TestCase):
    def setUp(self):
        self.js = JS.read_text("utf-8")

    def test_storage_access_is_guarded(self):
        """localStorage 在沙盒 iframe 里会抛异常，裸调用会让整页脚本死掉。"""
        self.assertIn("function storageGet", self.js)
        self.assertIn("function storageSet", self.js)
        bare = [
            line
            for line in self.js.splitlines()
            if "localStorage." in line
            and "function storage" not in line
            and "return window.localStorage" not in line
            and "window.localStorage.setItem(key" not in line
        ]
        self.assertEqual(bare, [], f"这些行直接用了 localStorage：{bare}")

    def test_error_handlers_present(self):
        self.assertIn('window.addEventListener("error"', self.js)
        self.assertIn('window.addEventListener("unhandledrejection"', self.js)

    def test_uses_bridge(self):
        """受限 iframe 里 fetch 不可用，后端调用必须走 AstrBotPluginView。"""
        self.assertIn("AstrBotPluginView", self.js)
        self.assertIn("apiGet", self.js)
        self.assertIn("apiPost", self.js)
        self.assertIn("waitBridge", self.js)
        self.assertNotIn("API_CANDIDATES", self.js)

    def test_reports_opaque_origin(self):
        self.assertIn("sandboxBanner", self.js)
        self.assertIn('window.location.origin === "null"', self.js)

    def test_diagnose_reports_bridge(self):
        self.assertIn("function diagnose", self.js)
        self.assertIn("findBridge", self.js)

    def test_no_undefined_function_calls(self):
        """粗查：调用了但没定义的函数（这类错误只会等到运行时才炸）。"""
        code = strip_js(self.js)
        # 定义从「原文 + 剥离后」两处取：剥离器对反引号/正则字面量不完美，
        # 只用来找「调用了但从没定义」的函数，宁可漏报不要误报。
        defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", code))
        defined |= set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", self.js))
        # 函数参数也是可调用的（例如回调里的 fn(...)）
        for params in re.findall(r"function[^(]*\(([^)]*)\)", self.js):
            for name in params.split(","):
                name = name.strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", name or ""):
                    defined.add(name)
        called = set(re.findall(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(", code))
        builtins = {
            "if", "for", "while", "switch", "catch", "function", "return", "typeof",
            "new", "throw", "else", "do", "var", "resolve", "reject",
            "setTimeout", "clearTimeout", "setInterval", "clearInterval",
            "fetch", "Promise", "Object", "Array", "JSON", "String", "Number",
            "Boolean", "Date", "Math", "RegExp", "Error", "URLSearchParams",
            "encodeURIComponent", "decodeURIComponent", "parseInt", "parseFloat",
            "isNaN", "console", "window", "document", "localStorage",
            "Blob", "Uint8Array", "atob", "URL", "navigator", "ResizeObserver",
            "isFinite", "requestAnimationFrame", "performance",
        }
        unknown = sorted(called - defined - builtins)
        self.assertEqual(unknown, [], f"这些函数被调用但没定义：{unknown}")

    def test_every_action_marks_start_and_finish(self):
        """每个操作都要写「开始 / 完成」，不能让用户以为还在转圈。"""
        self.assertIn("function beginMark", self.js)
        self.assertIn("function endMark", self.js)
        self.assertIn('"[完成] "', self.js)
        self.assertIn('"[失败] "', self.js)
        starts = set(re.findall(r'beginMark\("([^"]+)"\)', self.js))
        ends = set(re.findall(r'endMark\("([^"]+)"', self.js))
        self.assertTrue(starts, "没有任何开始标记")
        self.assertEqual(starts - ends, set(), "这些操作只有开始没有结束标记")
        for label in ("环境自检", "检测下载源", "下载 rootfs", "诊断连接", "安装软件包"):
            self.assertIn(label, starts, f"{label} 缺少开始标记")

    def test_auto_refresh_does_not_clobber_output(self):
        """自动刷新只更新状态，不能把用户刚看到的输出冲掉。"""
        self.assertIn("startAutoRefresh", self.js)
        self.assertIn("document.hidden", self.js)
        self.assertIn("function loadSetup(writeOutput)", self.js)
        self.assertIn("loadSetup(false)", self.js)
        self.assertIn("taskRunning", self.js)


class TestMobileLayout(unittest.TestCase):
    """手机访问：viewport、断点、触屏细节都要在。"""

    def setUp(self):
        self.html = INNER.read_text("utf-8")
        self.css = CSS.read_text("utf-8")

    def test_viewport_and_breakpoints(self):
        self.assertIn("viewport-fit=cover", self.html)
        self.assertIn("width=device-width", self.html)
        for width in ("600px", "420px", "380px"):
            self.assertIn(f"@media (max-width: {width})", self.css, f"缺少 {width} 断点")

    def test_touch_details(self):
        self.assertIn("touch-action: manipulation", self.css)
        self.assertIn("-webkit-text-size-adjust: 100%", self.css)
        self.assertIn("-webkit-tap-highlight-color: transparent", self.css)

    def test_tables_scroll_instead_of_overflow(self):
        self.assertIn("overflow-x: auto", self.css)

    def test_no_fixed_width_wider_than_phone(self):
        offenders = [
            m.group(0)
            for m in re.finditer(r"(?<!max-)(?<!min-)width:\s*(\d{3,})px", self.css)
            if int(m.group(1)) > 360
        ]
        self.assertEqual(offenders, [], f"这些固定宽度会溢出手机屏：{offenders}")


if __name__ == "__main__":
    unittest.main()

class TestFormControls(unittest.TestCase):
    """表单控件：浮动标签要能优雅降级，表格要能横向滚。"""

    def setUp(self):
        self.css = CSS.read_text("utf-8")

    def test_labels_stack_above_input(self):
        """标签在输入框上方，不浮到边框上 —— 浮上去会把说明挤成一团。"""
        self.assertIn(".field > label {", self.css)
        self.assertIn("flex-direction: column", self.css)
        self.assertNotIn("@supports selector(:has(*))", self.css)

    def test_tables_scroll_and_sticky_header(self):
        for host in ("#users-table", "#trust-table", "#scope-table"):
            self.assertIn(host, self.css)
        self.assertIn("position: sticky", self.css)
        self.assertIn("border-collapse: collapse", self.css)

class TestMotion(unittest.TestCase):
    """动效：波纹、数字滚动、进度条从 0 长、退场动画。"""

    def setUp(self):
        self.js = JS.read_text("utf-8")
        self.css = CSS.read_text("utf-8")

    def test_ripple_wired(self):
        self.assertIn("function bindRipple", self.js)
        self.assertIn("bindRipple();", self.js)
        self.assertIn(".ripple", self.css)
        self.assertIn("ripple-out", self.css)

    def test_numbers_count_up(self):
        self.assertIn("function countUp", self.js)
        self.assertIn("countUp(num, item[1])", self.js)

    def test_meter_starts_from_zero(self):
        self.assertIn("requestAnimationFrame(function () { fill.style.width = width; })", self.js)
        self.assertIn("width: 0;", self.css)

    def test_toast_exit_animation(self):
        self.assertIn("hiding", self.js)
        self.assertIn("toast-out", self.css)

    def test_reduced_motion_is_respected(self):
        self.assertIn("prefers-reduced-motion: reduce", self.css)

class TestNoHorizontalOverflow(unittest.TestCase):
    """手机上不能出现横向滚动：栅格轨道必须能收缩。"""

    def setUp(self):
        self.css = CSS.read_text("utf-8")

    def test_grid_tracks_can_shrink(self):
        self.assertIn("grid-template-columns: minmax(0, 1fr)", self.css)
        self.assertIn(".deck > .panel {", self.css)
        self.assertIn("align-self: start", self.css)
        self.assertIn("min-width: 0", self.css)

    def test_form_grid_never_wider_than_container(self):
        self.assertIn("minmax(min(100%, 200px), 1fr)", self.css)

    def test_block_uses_gap_not_margins(self):
        self.assertIn(".block > * { margin-top: 0; margin-bottom: 0; }", self.css)

class TestFormSpacing(unittest.TestCase):
    """表单里的栅格和按钮行不能贴在一起（截图里按钮压到复选框上了）。"""

    def setUp(self):
        self.css = CSS.read_text("utf-8")

    def test_form_is_flex_with_gap(self):
        self.assertIn(".block form {", self.css)
        self.assertIn("gap: var(--sp-4);", self.css)

class TestConfirmDialogs(unittest.TestCase):
    """危险操作必须有正式弹窗，不能再用浏览器原生 confirm。"""

    def setUp(self):
        self.js = JS.read_text("utf-8")
        self.html = INNER.read_text("utf-8")
        self.css = CSS.read_text("utf-8")

    def test_modal_exists(self):
        for node in ("modal", "modal-title", "modal-body", "modal-ok", "modal-cancel"):
            self.assertIn(f'id="{node}"', self.html)
        self.assertIn(".modal-scrim", self.css)

    def test_no_native_confirm_left(self):
        # 只允许 confirmDialog 内部的兜底那一处
        self.assertLessEqual(self.js.count("window.confirm"), 1)

    def test_rootfs_replace_needs_typing(self):
        self.assertIn("requireText", self.js)
        self.assertIn("替换现有的 rootfs", self.js)

    def test_auto_bootstrap_notice(self):
        self.assertIn("renderBootstrapNotice", self.js)
        self.assertIn("auto_bootstrap", self.js)

class TestSplash(unittest.TestCase):
    """启动画面：不能首帧闪一下，也不能卡死。"""

    def setUp(self):
        self.js = JS.read_text("utf-8")
        self.html = INNER.read_text("utf-8")
        self.css = CSS.read_text("utf-8")

    def test_splash_markup(self):
        self.assertIn('id="boot-splash"', self.html)
        self.assertIn('id="splash-hint"', self.html)

    def test_splash_styles(self):
        self.assertIn(".splash[data-state=\"hide\"]", self.css)
        self.assertIn("html.booting", self.css)
        self.assertIn("html.ready", self.css)

    def test_boot_sequence_has_minimum_and_timeout(self):
        self.assertIn("function finishBoot", self.js)
        self.assertIn("700 - (Date.now() - bootStarted)", self.js)
        self.assertIn("window.setTimeout(finishBoot, 6000)", self.js)

class TestRequestFeedback(unittest.TestCase):
    """每个请求都要有可见反馈，且不能无声挂死。"""

    def setUp(self):
        self.js = JS.read_text("utf-8")
        self.html = INNER.read_text("utf-8")
        self.css = CSS.read_text("utf-8")

    def test_api_has_timeout(self):
        self.assertIn("function withTimeout", self.js)
        self.assertIn("options.timeout || 20000", self.js)
        self.assertIn("超时（", self.js)

    def test_progress_bar_exists(self):
        self.assertIn('id="progress"', self.html)
        self.assertIn(".progress {", self.css)

    def test_button_busy_state(self):
        self.assertIn("is-busy", self.js)
        self.assertIn(".btn.is-busy", self.css)
        self.assertIn("正在保存", self.js)
        # 反馈走提示条，不改按钮文案
        self.assertNotIn('button.textContent = "保存中', self.js)

class TestSecurityTab(unittest.TestCase):
    """安全页：tab、面板、列表容器都要在。"""

    def setUp(self):
        self.html = INNER.read_text("utf-8")
        self.js = JS.read_text("utf-8")
        self.css = CSS.read_text("utf-8")

    def test_tab_and_panel(self):
        self.assertIn('data-tab="security"', self.html)
        self.assertIn('id="panel-security"', self.html)
        for node in ("sec-list", "sec-host", "sec-scan", "sec-upgrade"):
            self.assertIn(f'id="{node}"', self.html)

    def test_wired_in_js(self):
        self.assertIn("function loadSecurity", self.js)
        self.assertIn('api("/security/scan")', self.js)
        self.assertIn('api("/security/fix"', self.js)
        self.assertIn('"security"', self.js)

    def test_styles(self):
        self.assertIn(".sec-row", self.css)
        self.assertIn('.sec-badge[data-level="high"]', self.css)

class TestSecurityProgress(unittest.TestCase):
    """修复要有进度输出，不能点完就没动静。"""

    def setUp(self):
        self.js = JS.read_text("utf-8")
        self.html = INNER.read_text("utf-8")

    def test_output_area(self):
        self.assertIn('id="sec-out"', self.html)

    def test_polling(self):
        self.assertIn("function pollSecurityTask", self.js)
        self.assertIn('api("/security/task"', self.js)
        self.assertIn("900", self.js)

    def test_overview_notice(self):
        self.assertIn("function renderSecurityNotice", self.js)
        self.assertIn("security_summary", self.js)

class TestTabSwitchInterrupt(unittest.TestCase):
    """快速连点标签：旧的展开动画要能被打断，不能叠着跑。"""

    def setUp(self):
        self.js = JS.read_text("utf-8")

    def test_stop_before_start(self):
        self.assertIn("function stopCardAnim", self.js)
        # switchTab 里第一件事就是停掉上一次的动画
        body = self.js[self.js.index("function switchTab(name) {"):]
        self.assertIn("stopCardAnim();", body[:400])

    def test_token_guards_stale_callbacks(self):
        self.assertIn("cardAnim.token", self.js)
        self.assertIn("if (token !== cardAnim.token) { return; }", self.js)


class TestTabLeaveRace(unittest.TestCase):
    """快速连点标签：旧面板的退场必须能被取消，展开动画必须能被打断。"""

    def setUp(self):
        self.js = JS.read_text("utf-8")

    def test_leave_timer_is_cancelled(self):
        self.assertIn("var leaveTimers = {}", self.js)
        self.assertIn("function cancelLeave", self.js)
        self.assertIn("cancelLeave(next);", self.js)
        self.assertIn("window.clearTimeout(timer)", self.js)

    def test_anim_is_cancelled_before_switch(self):
        body = self.js[self.js.index("function switchTab(name) {"):]
        self.assertIn("stopCardAnim();", body[:400])

class TestTabPanelConsistency(unittest.TestCase):
    """TAB_ORDER 里的每个 tab 都必须有对应面板，反之亦然。

    之前 switchTab 里写死了一份 8 个 key 的数组，新增「安全」页时漏改，
    点了标签面板一直是 hidden —— 整页空白。这条测试就是防这个。
    """

    def setUp(self):
        self.js = JS.read_text("utf-8")
        self.html = INNER.read_text("utf-8")

    def _order(self):
        import re

        match = re.search(r"var TAB_ORDER = \[([^\]]+)\]", self.js)
        self.assertIsNotNone(match, "找不到 TAB_ORDER")
        return re.findall(r'"([a-z]+)"', match.group(1))

    def test_every_tab_has_panel(self):
        for key in self._order():
            self.assertIn(f'id="panel-{key}"', self.html, f"缺少 panel-{key}")
            self.assertIn(f'data-tab="{key}"', self.html, f"缺少 tab {key}")

    def test_every_panel_in_order(self):
        import re

        panels = set(re.findall(r'id="panel-([a-z]+)"', self.html))
        self.assertEqual(sorted(panels - set(self._order())), [], "有面板没进 TAB_ORDER")

    def test_switch_uses_tab_order(self):
        self.assertIn("TAB_ORDER.forEach(function (key) {", self.js)

class TestConfigGrouping(unittest.TestCase):
    """设置页要按用途分组，不能一大坨平铺。"""

    def setUp(self):
        self.js = JS.read_text("utf-8")
        self.css = CSS.read_text("utf-8")

    def test_grouping_in_js(self):
        self.assertIn("config-group", self.js)
        self.assertIn("field.group", self.js)

    def test_group_style(self):
        self.assertIn(".config-group {", self.css)

class TestDeckHeight(unittest.TestCase):
    """卡片高度必须跟着内容，不能被 deck 旧高度拉伸。"""

    def setUp(self):
        self.css = CSS.read_text("utf-8")
        self.js = JS.read_text("utf-8")

    def test_panel_does_not_stretch(self):
        self.assertIn("align-self: start", self.css)

    def test_no_inline_pixel_height(self):
        """高度交给内容决定，不能再写死像素（会形成反馈环）。"""
        self.assertIn('deck.style.height = ""', self.js)
        self.assertNotIn("deck.style.height = target", self.js)
        self.assertNotIn("offsetHeight + \"px\"", self.js)
        self.assertNotIn("function syncDeckHeight", self.js)

class TestDeckAnimation(unittest.TestCase):
    """切页要「先收回旧卡，再展开新卡」，并且能被打断。"""

    def setUp(self):
        self.js = JS.read_text("utf-8")
        self.css = CSS.read_text("utf-8")

    def test_collapse_then_expand(self):
        """先收旧卡，收完再换，换了再展开 —— 不能并发。"""
        self.assertIn("function animateCard", self.js)
        # 收到「空卡」的固定高度，不是 0
        self.assertIn("Math.min(floor, current.offsetHeight), 180", self.js)
        self.assertIn("--card-min-height", self.css)
        # 新卡从「空卡」高度展开到内容高度
        self.assertIn("animateCard(next, floor, target, 280", self.js)
        # 展开必须在收起的回调里
        collapse_at = self.js.index("Math.min(floor, current.offsetHeight), 180")
        reveal_at = self.js.index("reveal();", collapse_at)
        self.assertLess(collapse_at, reveal_at)

    def test_interruptible(self):
        self.assertIn("function stopCardAnim", self.js)
        self.assertIn("stopCardAnim();", self.js)
        self.assertIn("cardAnim.token", self.js)

    def test_clip_on_card_not_layout(self):
        """裁切要裁在卡片上，圆角才会全程都在；裁在 deck 上会切出直线。"""
        self.assertIn(".panel.animating {", self.css)
        self.assertIn("overflow: hidden", self.css)
        self.assertNotIn(".deck.animating", self.css)

    def test_single_motion(self):
        """抽卡只在 .pulling 上触发，基础 .panel 不许挂常驻动画。"""
        # card-pull 只能出现一次，而且是在 .pulling 那条规则里
        self.assertEqual(self.css.count("animation: card-pull"), 1)
        pull_at = self.css.index(".panel.pulling {")
        anim_at = self.css.index("animation: card-pull")
        self.assertGreater(anim_at, pull_at)
        self.assertNotIn("card-sink", self.css)

    def test_first_visit_language_picker(self):
        self.assertIn("function showLanguagePicker", self.js)
        self.assertIn('api("/ui/prefs"', self.js)

    def test_tour_only_once(self):
        self.assertIn("UI.tour_seen", self.js)
        self.assertNotIn('storageGet("sandbox-tour-done")', self.js)

class TestSplashIcon(unittest.TestCase):
    """启动画面用插件自己的图标，不是一块渐变。"""

    def setUp(self):
        self.html = INNER.read_text("utf-8")
        self.css = CSS.read_text("utf-8")

    def test_uses_real_logo(self):
        self.assertIn('class="splash-mark" src="logo.png"', self.html)

    def test_no_gradient_placeholder(self):
        self.assertNotIn("linear-gradient(140deg, var(--md-primary)", self.css)

    def test_logo_file_present(self):
        from pathlib import Path as _P

        self.assertTrue((_P(INNER).parent / "logo.png").is_file())


class TestCardPull(unittest.TestCase):
    """切页要有抽卡的手感：新卡从侧面滑进来。"""

    def setUp(self):
        self.js = JS.read_text("utf-8")
        self.css = CSS.read_text("utf-8")

    def test_keyframes_exist(self):
        self.assertIn(".panel.pulling {", self.css)
        self.assertIn("@keyframes card-pull", self.css)

    def test_direction_follows_tab_order(self):
        self.assertIn("--pull-x", self.js)
        self.assertIn("--pull-r", self.js)

    def test_class_is_cleared(self):
        self.assertIn('next.classList.remove("pulling")', self.js)

class TestTabDirection(unittest.TestCase):
    """switchTab 里用到的变量必须真的定义过 —— forward 漏定义过一次，切页直接抛错。"""

    def setUp(self):
        self.js = JS.read_text("utf-8")
        start = self.js.index("function switchTab(name) {")
        rest = self.js[start + 10:]
        end = rest.index("\n  function ")
        self.body = self.js[start:start + 10 + end]

    def test_forward_is_defined(self):
        self.assertIn("var forward =", self.body)

    def test_pull_uses_forward_after_definition(self):
        self.assertLess(self.body.index("var forward ="), self.body.index("var pullX"))

class TestCheckboxLayout(unittest.TestCase):
    """复选项：方框在左、文字在右。label 在 DOM 里排在 input 前面，
    不给它指定列就会掉进 18px 那一列，文字竖着排。"""

    def setUp(self):
        self.css = CSS.read_text("utf-8")
        start = self.css.index('.field:has(> input[type="checkbox"]) {')
        self.block = self.css[start:start + 2000]

    def test_label_placed_in_second_column(self):
        label_rule = self.block[self.block.index("> label {"):]
        self.assertIn("grid-column: 2", label_rule[:300])

    def test_input_placed_in_first_column(self):
        input_rule = self.block[self.block.index('> input[type="checkbox"] {'):]
        self.assertIn("grid-column: 1", input_rule[:300])

