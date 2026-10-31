# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""预览渲染器的真机测试：把 app.js 里的纯函数抽出来丢给 node 跑。

覆盖：语法高亮分词、CSV 引号解析、Markdown 结构、十六进制。
没有 node 就跳过（不影响其它测试）。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "pages" / "sandbox" / "app.js"

HARNESS_HEAD = r"""
function node(tag) {
  return {
    tag: tag, children: [], attrs: {}, style: {}, className: "",
    setAttribute: function (k, v) { this.attrs[k] = v; },
    appendChild: function (c) { this.children.push(c); return c; },
    addEventListener: function () {},
    textContent: ""
  };
}
function textNode(text) { return { text: text, isText: true }; }
var document = {
  createElement: function (tag) { return node(tag); },
  createTextNode: function (text) { return textNode(text); }
};
function el(tag, attrs, kids) {
  var n = node(tag);
  if (attrs) {
    Object.keys(attrs).forEach(function (k) {
      if (k === "text") { n.textContent = attrs[k]; } else { n.setAttribute(k, attrs[k]); }
    });
  }
  (kids || []).forEach(function (c) { n.appendChild(c); });
  return n;
}
var state = { previewMode: "view", preview: null };
var $ = function () { return node("div"); };
function iconNode() { return node("span"); }
function kindOf() { return "text"; }
function flat(n) {
  if (n.isText) { return n.text; }
  return "<" + n.tag + ">" + (n.children || []).map(flat).join("") + (n.textContent || "");
}
"""

HARNESS_TAIL = r"""
var results = [];
function check(name, ok, extra) { results.push([name, ok, extra || ""]); }

var toks = tokenize("def f(x):\n    return 'hi'  # 注释", "python");
var kinds = toks.map(function (t) { return t[0] + ":" + t[1]; }).join("|");
check("python 关键字", kinds.indexOf("kw:def") >= 0, kinds);
check("字符串", kinds.indexOf("str:'hi'") >= 0, kinds);
check("注释", kinds.indexOf("com:# 注释") >= 0, kinds);

var csv = parseCsv('name,score\n"a,b",42\nc,7', ",");
check("CSV 引号内逗号", csv.length === 3 && csv[1][0] === "a,b", JSON.stringify(csv));
check("CSV 列数", csv[0].length === 2, JSON.stringify(csv));

var mdText = flat(renderMarkdown("# 标题\n\n- 一项\n- 两项\n\n`code` 和 **粗**"));
check("markdown 标题", mdText.indexOf("<h2>") >= 0, mdText.slice(0, 80));
check("markdown 列表", mdText.indexOf("<ul>") >= 0, mdText.slice(0, 120));
check("markdown 行内代码", mdText.indexOf("<code>") >= 0, mdText.slice(0, 120));
check("markdown 粗体", mdText.indexOf("<strong>") >= 0, mdText.slice(0, 120));

var hexText = flat(hexDump("AAECAwQ=", 5));
check("十六进制", hexText.indexOf("00 01 02 03 04") >= 0, hexText.slice(0, 120));

var jsonText = flat(renderRich('{"a":1}', "json"));
check("JSON 美化", jsonText.indexOf('"a":') >= 0 && jsonText.indexOf("<br") < 0, jsonText.slice(0, 80));

var bad = 0;
results.forEach(function (r) {
  console.log((r[1] ? "PASS " : "FAIL ") + r[0] + (r[1] ? "" : "  -> " + r[2]));
  if (!r[1]) { bad += 1; }
});
process.exit(bad === 0 ? 0 : 1);
"""


def _slice_preview_functions(source: str) -> str:
    start = source.index("  // ---------- 预览：语法高亮")
    end = source.index("  function openFilesTab() {")
    return source[start:end]


class TestPreviewRenderers(unittest.TestCase):
    def test_renderers(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("环境里没有 node")

        source = JS.read_text("utf-8")
        script = HARNESS_HEAD + _slice_preview_functions(source) + HARNESS_TAIL

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(script)
            path = Path(handle.name)
        try:
            result = subprocess.run(
                [node, str(path)], capture_output=True, text=True, timeout=60
            )
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(
            result.returncode, 0,
            result.stdout + result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
