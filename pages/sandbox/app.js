/* astrbot_plugin_sandbox — Copyright (C) 2026 maid
   SPDX-License-Identifier: AGPL-3.0-or-later */
(function () {
  "use strict";

  // 启动画面：先遮住，等第一份数据回来再淡出，避免首帧闪一下
  var bootStarted = Date.now();
  var bootFinished = false;
  document.documentElement.classList.add("booting");

  function splashHint(text) {
    var node = document.getElementById("splash-hint");
    if (node) { node.textContent = text; }
  }

  function finishBoot() {
    if (bootFinished) { return; }
    bootFinished = true;
    var splash = document.getElementById("boot-splash");
    // 太快回来会一闪而过，最少停 700ms，让动画走完
    var wait = Math.max(0, 700 - (Date.now() - bootStarted));
    window.setTimeout(function () {
      var root = document.documentElement;
      root.classList.remove("booting");
      root.classList.add("ready");
      if (!splash) { return; }
      splash.setAttribute("data-state", "hide");
      window.setTimeout(function () {
        if (splash.parentNode) { splash.parentNode.removeChild(splash); }
      }, 340);
    }, wait);
  }

  var state = { users: [], currentPath: "", preview: null, previewMode: "view", setup: null };

  // 页面文案：只覆盖导航和标题，正文内容由后端按语言生成
  var TEXT = {
    zh: {
      "tab.overview": "概览", "tab.config": "设置", "tab.setup": "部署",
      "tab.users": "权限", "tab.files": "文件", "tab.exec": "执行",
      "tab.rules": "规则", "tab.logs": "日志", "tab.security": "安全",
      "btn.refresh": "刷新状态", "btn.tour": "引导", "btn.scan": "重新扫描",
      "btn.upgrade": "升级沙盒包", "btn.fix": "修复", "btn.done": "已处理",
      "btn.summary": "翻译并总结"
    },
    en: {
      "tab.overview": "Overview", "tab.config": "Settings", "tab.setup": "Setup",
      "tab.users": "Access", "tab.files": "Files", "tab.exec": "Run",
      "tab.rules": "Rules", "tab.logs": "Logs", "tab.security": "Security",
      "btn.refresh": "Refresh", "btn.tour": "Tour", "btn.scan": "Rescan",
      "btn.upgrade": "Upgrade packages", "btn.fix": "Fix", "btn.done": "Mark done",
      "btn.summary": "Translate & summarize"
    }
  };

  var LANG = "zh";
  var UI = { language: "", tour_seen: false };

  function savePrefs(payload) {
    if (payload.language) { UI.language = payload.language; }
    if (payload.tour_seen) { UI.tour_seen = true; }
    api("/ui/prefs", { method: "POST", body: payload }).catch(function () { /* 存不上也不影响用 */ });
  }

  // 第一次进页面先问语言。iframe 里 localStorage 不可用，所以存到服务端。
  function showLanguagePicker() {
    if (document.getElementById("lang-scrim")) { return; }
    var scrim = el("div", { class: "lang-scrim" });
    scrim.setAttribute("id", "lang-scrim");
    var card = el("div", { class: "lang-card" });
    card.appendChild(el("h2", { text: "选择语言 / Choose language" }));
    card.appendChild(el("p", {
      class: "lang-desc",
      text: "之后可以在「设置 → 语言」里改。 / You can change this later in Settings."
    }));
    var row = el("div", { class: "lang-actions" });
    [["zh", "中文"], ["en", "English"]].forEach(function (pair) {
      var btn = el("button", {
        class: "btn" + (pair[0] === "zh" ? " primary" : ""),
        type: "button",
        text: pair[1]
      });
      btn.addEventListener("click", function () {
        savePrefs({ language: pair[0] });
        applyLanguage(pair[0]);
        if (scrim.parentNode) { scrim.parentNode.removeChild(scrim); }
      });
      row.appendChild(btn);
    });
    card.appendChild(row);
    scrim.appendChild(card);
    document.body.appendChild(scrim);
  }

  function L(key) {
    var table = TEXT[LANG] || TEXT.zh;
    return table[key] || (TEXT.zh[key] || key);
  }

  function applyLanguage(lang) {
    LANG = TEXT[lang] ? lang : "zh";
    document.documentElement.setAttribute("lang", LANG === "en" ? "en" : "zh-CN");
    document.querySelectorAll("[data-i18n]").forEach(function (node) {
      node.textContent = L(node.getAttribute("data-i18n"));
    });
    document.querySelectorAll(".tab").forEach(function (tab) {
      var key = "tab." + tab.dataset.tab;
      tab.textContent = L(key);
    });
    ["refresh", "tour-start", "sec-scan", "sec-upgrade"].forEach(function (id) {
      var node = $(id);
      if (!node) { return; }
      var key = id === "tour-start" ? "btn.tour"
        : id === "refresh" ? "btn.refresh"
        : id === "sec-scan" ? "btn.scan" : "btn.upgrade";
      node.textContent = L(key);
    });
  }

  var ICON_BOX =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M4 7.5 12 3l8 4.5v9L12 21l-8-4.5z"/><path d="M4 7.5 12 12l8-4.5M12 12v9"/></svg>';

  // 空状态：图标 + 主文案 + 说明，比一行灰字好看
  function emptyState(title, desc) {
    var wrap = el("div", { class: "empty-state" });
    wrap.innerHTML = ICON_BOX;
    wrap.appendChild(el("div", { class: "title", text: title }));
    if (desc) { wrap.appendChild(el("div", { class: "desc", text: desc })); }
    return wrap;
  }

  var ICONS = {
    dir: '<svg class="ico" data-kind="dir" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4.2l1.8 2h9A1.5 1.5 0 0 1 21 8.5v9A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/></svg>',
    text: '<svg class="ico" data-kind="text" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><path d="M9 12h6M9 16h6"/></svg>',
    image: '<svg class="ico" data-kind="image" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="8.5" cy="10" r="1.5"/><path d="M21 16l-5-5-6 6"/></svg>',
    bin: '<svg class="ico" data-kind="bin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5 12 12l8-4.5M12 12v9"/></svg>'
  };

  var TEXT_EXT = ["txt", "md", "py", "js", "json", "yaml", "yml", "toml", "ini",
    "conf", "sh", "log", "csv", "html", "css", "xml", "cfg", "env", "rst"];
  var IMAGE_EXT = ["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico", "avif"];

  function kindOf(name) {
    var parts = String(name).split(".");
    if (parts.length < 2) { return "text"; }
    var ext = parts.pop().toLowerCase();
    if (IMAGE_EXT.indexOf(ext) >= 0) { return "image"; }
    if (TEXT_EXT.indexOf(ext) >= 0) { return "text"; }
    return "bin";
  }

  function iconNode(kind) {
    var holder = document.createElement("span");
    holder.innerHTML = ICONS[kind] || ICONS.text;
    return holder.firstElementChild;
  }

  function meter(label, ratio, valueText, tone) {
    var wrap = el("div", { class: "meter" });
    wrap.appendChild(el("div", { class: "meter-head" }, [
      el("span", { text: label }),
      el("b", { text: valueText })
    ]));
    var track = el("div", { class: "meter-track" });
    var fill = el("div", { class: "meter-fill" });
    if (tone) { fill.setAttribute("data-tone", tone); }
    var width = Math.round(Math.max(0, Math.min(1, Number(ratio) || 0)) * 100) + "%";
    track.appendChild(fill);
    // 先 0 再赋值，transition 才会真的跑
    window.requestAnimationFrame(function () { fill.style.width = width; });
    wrap.appendChild(track);
    return wrap;
  }

  function renderMetrics(m) {
    var host = $("overview-metrics");
    if (!host) { return; }
    host.textContent = "";
    if (!m || !m.cpu_count) {
      host.appendChild(el("p", { class: "muted", text: "读不到宿主资源（/proc 不可用）。" }));
      return;
    }
    var memPct = Math.round((m.mem_ratio || 0) * 100);
    var diskPct = Math.round((m.disk_ratio || 0) * 100);
    var loadPct = Math.round((m.load_ratio || 0) * 100);

    host.appendChild(meter(
      "内存", m.mem_ratio,
      (m.mem_used_mb / 1024).toFixed(1) + " / " + (m.mem_total_mb / 1024).toFixed(1) + " GB · " + memPct + "%",
      memPct >= 90 ? "error" : ""
    ));
    host.appendChild(meter(
      "磁盘", m.disk_ratio,
      m.disk_free_gb + " GB 可用 / " + m.disk_total_gb + " GB · " + diskPct + "%",
      diskPct >= 90 ? "error" : ""
    ));
    if (m.load1 === null || m.load1 === undefined) {
      // 读不到就说读不到，不能显示成 0
      host.appendChild(meter("负载 · 1 分钟", 0, "读不到（容器里没有 /proc/loadavg）", ""));
    } else {
      host.appendChild(meter(
        "负载 · 1 分钟", m.load_ratio,
        m.load1 + " / " + m.cpu_count + " 核 · " + loadPct + "%",
        loadPct >= 85 ? "error" : (loadPct >= 60 ? "warn" : "")
      ));
    }
  }

  function terminalBlock(command, data) {
    var box = el("div", { class: "terminal" });
    box.appendChild(el("div", { class: "terminal-bar" }, [
      el("i", {}), el("i", {}), el("i", {}),
      el("span", { class: "terminal-title", text: "sandbox · " + data.duration_ms + "ms" })
    ]));
    var body = el("div", { class: "terminal-body" });
    body.appendChild(el("div", { class: "term-line prompt", text: "$ " + command }));
    if (data.stdout) { body.appendChild(el("div", { class: "term-line", text: data.stdout })); }
    if (data.stderr) { body.appendChild(el("div", { class: "term-line err", text: data.stderr })); }
    if (!data.stdout && !data.stderr) {
      body.appendChild(el("div", { class: "term-line", text: "(无输出)" }));
    }
    var status = "exit=" + data.exit_code;
    if (data.timed_out) { status += " · 超时已终止"; }
    if (data.truncated) { status += " · 输出已截断"; }
    var line = el("div", { class: "term-line status", text: status });
    line.setAttribute("data-ok", data.exit_code === 0 ? "true" : "false");
    body.appendChild(line);
    box.appendChild(body);
    window.setTimeout(function () { body.scrollTop = body.scrollHeight; }, 0);
    return box;
  }

  // MD3 水波纹：按下点扩散一圈，比只变色有反馈
  function bindRipple() {
    document.addEventListener("pointerdown", function (event) {
      // 只给按钮和标签做波纹：文件树整行很宽，波纹会被裁成一条灰带
      var target = event.target && event.target.closest
        ? event.target.closest(".btn, .tab")
        : null;
      if (!target || target.disabled) { return; }
      var rect = target.getBoundingClientRect();
      var size = Math.min(Math.max(rect.width, rect.height) * 1.4, 220);
      var dot = document.createElement("span");
      dot.className = "ripple";
      dot.style.width = size + "px";
      dot.style.height = size + "px";
      dot.style.left = (event.clientX - rect.left - size / 2) + "px";
      dot.style.top = (event.clientY - rect.top - size / 2) + "px";
      target.appendChild(dot);
      var drop = function () {
        if (dot.parentNode) { dot.parentNode.removeChild(dot); }
      };
      dot.addEventListener("animationend", drop);
      // 动画被打断（例如元素被重绘）时兜底清掉，别留一块灰
      window.setTimeout(drop, 800);
    });
  }

  // 数字从 0 滚上去；非数字直接写死
  function countUp(node, value) {
    var target = Number(value);
    var numeric = isFinite(target) && String(value).trim() !== "" && !/[^\d.\-]/.test(String(value));
    if (!numeric) {
      node.textContent = String(value);
      return;
    }
    var started = null;
    var duration = 520;
    node.textContent = "0";
    function frame(now) {
      if (started === null) { started = now; }
      var progress = Math.min(1, (now - started) / duration);
      var eased = 1 - Math.pow(1 - progress, 3);
      node.textContent = String(Math.round(target * eased));
      if (progress < 1) { window.requestAnimationFrame(frame); }
    }
    window.requestAnimationFrame(frame);
  }

  function skeletonBlock(rows) {
    var box = el("div", { class: "stack" });
    for (var i = 0; i < rows; i += 1) {
      box.appendChild(el("div", { class: "skeleton " + (i % 3 === 1 ? "w70" : "w40") }));
    }
    return box;
  }

  function $(id) { return document.getElementById(id); }

  // 受限 iframe 里 localStorage 会抛 SecurityError，一次异常就会让整页脚本停摆，
  // 所以统一包起来；能读到就用（例如直连调试），读不到也不影响。
  function storageGet(key) {
    try { return window.localStorage.getItem(key); } catch (error) { return null; }
  }

  function storageSet(key, value) {
    try { window.localStorage.setItem(key, value); return true; } catch (error) { return false; }
  }

  function bootStatus(text, isError) {
    var node = $("boot-status");
    if (node) {
      node.textContent = text;
      node.style.color = isError ? "var(--md-error)" : "var(--md-on-surface-variant)";
    }
    var dot = $("status-dot");
    if (dot) {
      dot.setAttribute("data-state", isError ? "error" : (text.indexOf("已连接") === 0 ? "ok" : ""));
    }
  }

  function toast(message, kind) {
    var box = $("toast");
    if (!box) { return; }
    box.textContent = message;
    box.dataset.kind = kind === "error" ? "error" : (kind === "loading" ? "loading" : "ok");
    box.hidden = false;
    box.classList.remove("hiding");
    window.clearTimeout(toast.timer);
    // 进行中的提示不自动消失，等结果把它替换掉
    if (kind === "loading") { return; }
    var life = kind === "error" ? 5200 : 2600;
    toast.timer = window.setTimeout(function () {
      box.classList.add("hiding");
      window.setTimeout(function () {
        box.hidden = true;
        box.classList.remove("hiding");
      }, 180);
    }, life);
  }

  var modalResolve = null;

  function closeModal(result) {
    var scrim = $("modal");
    if (scrim) { scrim.hidden = true; }
    if (modalResolve) {
      var resolve = modalResolve;
      modalResolve = null;
      resolve(result);
    }
  }

  // 正式确认框：标题 + 后果清单 + 可选「输入词语才能继续」
  function confirmDialog(options) {
    var opts = options || {};
    var scrim = $("modal");
    if (!scrim) { return Promise.resolve(window.confirm(opts.title || "确认")); }

    $("modal-title").textContent = opts.title || "确认";
    var body = $("modal-body");
    body.textContent = "";
    (opts.lines || []).forEach(function (line) {
      body.appendChild(el("p", { text: line }));
    });

    var gate = $("modal-gate");
    var gateInput = $("modal-gate-input");
    var ok = $("modal-ok");
    if (opts.requireText) {
      gate.hidden = false;
      gateInput.value = "";
      gateInput.setAttribute("placeholder", opts.requireText);
      ok.disabled = true;
    } else {
      gate.hidden = true;
      gateInput.value = "";
      ok.disabled = false;
    }
    ok.textContent = opts.confirmText || "确定";
    ok.className = "btn " + (opts.danger ? "danger-solid" : "primary");
    $("modal-cancel").textContent = opts.cancelText || "取消";

    scrim.hidden = false;
    window.setTimeout(function () {
      (opts.requireText ? gateInput : $("modal-cancel")).focus();
    }, 40);
    return new Promise(function (resolve) { modalResolve = resolve; });
  }

  function pageOrigin() {
    // origin 在受限 iframe 里是 "null"，但 location.href 仍然可读
    try {
      var match = String(window.location.href).match(/^([a-z]+:\/\/[^/]+)/i);
      return match ? match[1] : "";
    } catch (error) { return ""; }
  }

  function sandboxBanner() {
    var opaque = false;
    try { opaque = window.location.origin === "null"; } catch (error) { opaque = true; }
    if (!opaque) { return; }
    var box = document.createElement("div");
    box.className = "card";
    box.style.cssText = "margin:16px 24px;border-left:3px solid var(--md-primary)";
    box.textContent =
      "页面在 AstrBot 的受限 iframe 里（origin=null，正常）。所有请求都通过 " +
      "AstrBotPluginView bridge 转发，不会直连接口。";
    var main = document.querySelector("main");
    if (main && main.parentNode) { main.parentNode.insertBefore(box, main); }
  }

  var bridge = null;
  var bridgeContext = null;
  var bridgePromise = null;

  function findBridge() {
    return window.AstrBotPluginView || window.AstrBotPluginPage || null;
  }

  // bridge SDK 可能在本脚本之后才注入，所以轮询等一会儿。
  // 注意：ready 返回的是「上下文」，不是 bridge 本身，别拿它当 bridge 用。
  function waitBridge(timeoutMs) {
    if (bridgePromise) { return bridgePromise; }
    var limit = timeoutMs || 3000;
    bridgePromise = new Promise(function (resolve) {
      var started = Date.now();
      (function poll() {
        var found = findBridge();
        if (found) { resolve(found); return; }
        if (Date.now() - started >= limit) { resolve(null); return; }
        window.setTimeout(poll, 100);
      })();
    }).then(function (found) {
      bridge = found;
      if (!found) { return null; }
      if (typeof found.ready !== "function") { return found; }
      return found.ready().then(function (context) {
        bridgeContext = context || null;
        return found;
      }).catch(function () { return found; });
    });
    return bridgePromise;
  }

  // 不同版本的 SDK 方法名可能不一样，做个薄适配
  function callGet(api, endpoint, params) {
    if (api && typeof api.apiGet === "function") { return api.apiGet(endpoint, params); }
    if (api && typeof api.get === "function") { return api.get(endpoint, params); }
    if (api && typeof api.request === "function") {
      return api.request("GET", endpoint, params);
    }
    throw new Error("bridge 没有 apiGet 方法（可用：" + Object.keys(api || {}).join(",") + "）");
  }

  function callPost(api, endpoint, body) {
    if (api && typeof api.apiPost === "function") { return api.apiPost(endpoint, body); }
    if (api && typeof api.post === "function") { return api.post(endpoint, body); }
    if (api && typeof api.request === "function") {
      return api.request("POST", endpoint, body);
    }
    throw new Error("bridge 没有 apiPost 方法（可用：" + Object.keys(api || {}).join(",") + "）");
  }

  // 没有 bridge 时的退路：直接打插件路由（页面被直接打开、非同源 iframe 时）
  function directCall(endpoint, options) {
    var url = "/astrbot_plugin_sandbox/" + endpoint;
    var init = { method: (options.method || "GET").toUpperCase(), headers: {} };
    if (init.method === "POST") {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body || {});
    } else if (options.query) {
      var parts = [];
      Object.keys(options.query).forEach(function (key) {
        parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(options.query[key]));
      });
      if (parts.length) { url += "?" + parts.join("&"); }
    }
    return fetch(url, init).then(function (response) {
      return response.json().catch(function () {
        return { status: "error", message: "HTTP " + response.status };
      });
    });
  }

  // 正在飞的请求数：用来给按钮和顶部进度条提供反馈
  var inflight = 0;
  var inflightHooks = [];

  function onInflight(fn) { inflightHooks.push(fn); }

  function bumpInflight(delta) {
    inflight = Math.max(0, inflight + delta);
    inflightHooks.forEach(function (fn) {
      try { fn(inflight); } catch (error) { /* 反馈层出错不能影响请求 */ }
    });
  }

  // 没有超时的 Promise 会让按钮永远「没反应」，必须兜住
  function withTimeout(promise, ms, label) {
    return new Promise(function (resolve, reject) {
      var done = false;
      var timer = window.setTimeout(function () {
        if (done) { return; }
        done = true;
        reject(new Error((label || "请求") + "超时（" + Math.round(ms / 1000) + " 秒），接口没有响应"));
      }, ms);
      promise.then(function (value) {
        if (done) { return; }
        done = true;
        window.clearTimeout(timer);
        resolve(value);
      }, function (error) {
        if (done) { return; }
        done = true;
        window.clearTimeout(timer);
        reject(error);
      });
    });
  }

  function api(path, options) {
    options = options || {};
    var endpoint = String(path || "").replace(/^\/+/, "");
    bumpInflight(1);
    var settled = withTimeout(rawApi(endpoint, options), options.timeout || 20000, endpoint);
    settled.then(function () { bumpInflight(-1); }, function () { bumpInflight(-1); });
    return settled;
  }

  function rawApi(endpoint, options) {
    return waitBridge().then(function (current) {
      if (!current) {
        return directCall(endpoint, options);
      }
      if ((options.method || "GET").toUpperCase() === "POST") {
        return callPost(current, endpoint, options.body || {});
      }
      return callGet(current, endpoint, options.query || {});
    }).then(function (data) {
      if (data && data.status === "error") {
        throw new Error(data.message || "后端返回错误");
      }
      if (data && data.ok === false) {
        throw new Error(data.error || "后端返回错误");
      }
      return data;
    });
  }

  var busyButtons = [];

  onInflight(function (count) {
    var bar = $("progress");
    if (bar) { bar.hidden = count === 0; }
    if (count === 0) {
      busyButtons.forEach(function (btn) {
        btn.classList.remove("is-busy");
        btn.removeAttribute("aria-busy");
      });
      busyButtons = [];
    }
  });

  // 点按钮后如果确实发出了请求，就给按钮一个「执行中」外观
  document.addEventListener("click", function (event) {
    var btn = event.target && event.target.closest
      ? event.target.closest(".btn")
      : null;
    if (!btn || btn.disabled) { return; }
    var before = inflight;
    window.setTimeout(function () {
      if (inflight > before && !btn.disabled) {
        btn.classList.add("is-busy");
        btn.setAttribute("aria-busy", "true");
        busyButtons.push(btn);
      }
    }, 60);
  }, true);

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (key === "text") { node.textContent = attrs[key]; }
      else if (key === "class") { node.className = attrs[key]; }
      else { node.setAttribute(key, attrs[key]); }
    });
    (children || []).forEach(function (child) { node.appendChild(child); });
    return node;
  }

  function fmtBytes(value) {
    if (!value) { return "0B"; }
    var units = ["B", "KB", "MB", "GB", "TB"];
    var n = value;
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return (i === 0 ? Math.round(n) : n.toFixed(1)) + units[i];
  }

  var TAB_ORDER = ["overview", "config", "setup", "users", "files", "exec", "rules", "logs", "security"];

  // 每张卡的退场定时器。快速连点时不取消，上一次的定时器会把刚显示的
  // 同一张卡又 hidden 掉 —— 页面就空白了。
  var leaveTimers = {};

  function cancelLeave(panel) {
    if (!panel) { return; }
    var timer = leaveTimers[panel.id];
    if (timer) {
      window.clearTimeout(timer);
      delete leaveTimers[panel.id];
    }
    panel.classList.remove("leaving");
    panel.style.zIndex = "";
  }

  // ---------- 卡片缩放（收 → 换 → 展，一步接一步） ----------

  var cardAnim = { token: 0, timer: null };

  function stopCardAnim() {
    cardAnim.token += 1;
    if (cardAnim.timer) {
      window.clearTimeout(cardAnim.timer);
      cardAnim.timer = null;
    }
  }

  // 给「卡片自己」的高度做过渡。裁切在卡片上，圆角全程都在。
  function animateCard(panel, from, to, ms, done) {
    if (!panel) { if (done) { done(); } return; }
    var token = ++cardAnim.token;
    panel.classList.add("animating");
    panel.style.transition = "none";
    panel.style.height = from + "px";
    void panel.offsetHeight;
    panel.style.transition = "height " + ms + "ms var(--md-ease-emphasized)";
    panel.style.height = to + "px";
    cardAnim.timer = window.setTimeout(function () {
      if (token !== cardAnim.token) { return; }
      cardAnim.timer = null;
      panel.style.transition = "";
      panel.classList.remove("animating");
      if (done) { done(); }
    }, ms + 30);
  }

  // 卡片最小高度：还没加载出来的那种「空卡」高度，固定值
  function minCardHeight() {
    var value = window.getComputedStyle(document.documentElement)
      .getPropertyValue("--card-min-height").trim();
    var parsed = parseInt(value, 10);
    return isNaN(parsed) ? 148 : parsed;
  }

  function contentHeight(panel) {
    var previous = panel.style.height;
    panel.style.height = "auto";
    var height = panel.offsetHeight;
    panel.style.height = previous;
    return height;
  }

  function switchTab(name) {
    var deck = $("deck");
    var next = $("panel-" + name);
    if (!next) { return; }
    var current = deck ? deck.querySelector(".panel:not([hidden])") : null;

    // 上一次还没走完就点了别的：立刻停，从当前高度开始收
    stopCardAnim();
    cancelLeave(next);
    if (current === next) { return; }

    function reveal() {
      TAB_ORDER.forEach(function (key) {
        var tab = $("tab-" + key);
        if (tab) { tab.setAttribute("aria-selected", key === name ? "true" : "false"); }
        var panel = $("panel-" + key);
        if (!panel) { return; }
        if (key === name) {
          panel.hidden = false;
          panel.style.zIndex = "2";
        } else {
          panel.hidden = true;
        }
      });

      moveIndicator(name);

      // 新卡从 0 展开到内容高度，展开完把高度交还给内容
      var target = contentHeight(next);
      var floor = minCardHeight();
      next.style.height = floor + "px";
      animateCard(next, floor, target, 280, function () {
        next.style.height = "";
        next.style.zIndex = "";
        try { window.scrollTo({ top: 0, behavior: "smooth" }); } catch (error) { window.scrollTo(0, 0); }
      });
    }

    if (current && current.offsetHeight > 0) {
      // 先把旧卡收到「空卡」高度（固定值），再换卡 —— 不并发、也不收到 0
      var floor = minCardHeight();
      animateCard(current, current.offsetHeight, Math.min(floor, current.offsetHeight), 180, function () {
        current.hidden = true;
        current.style.height = "";
        current.classList.remove("animating");
        reveal();
      });
    } else {
      reveal();
    }
  }

  // 指示条滑到选中项下面（宽度固定 24px，靠 transform 移动）
  function moveIndicator(name) {
    var tab = $("tab-" + name);
    var bar = $("tab-indicator");
    if (!tab || !bar) { return; }
    bar.style.width = "24px";
    bar.style.transform = "translateX(" + (tab.offsetLeft + tab.offsetWidth / 2 - 12) + "px)";
  }

  // 卡片高度交给内容决定：清掉内联高度，让 deck 自然撑开。
  // 之前是「量卡片高度 → 写死给 deck」，卡片又被 deck 拉伸，形成反馈环，
  // 切到短页面就留一大截空白 —— 这就是「卡片特别长」的原因。
  function resetDeckHeight() {
    var deck = $("deck");
    if (deck) { deck.style.height = ""; }
  }

  window.addEventListener("error", function (event) {
    var message = (event && event.message) || "未知错误";
    bootStatus("脚本出错：" + message, true);
    toast("页面脚本出错：" + message, "error");
  });
  window.addEventListener("unhandledrejection", function (event) {
    var reason = event && event.reason;
    var message = (reason && reason.message) || String(reason || "未知原因");
    bootStatus("请求失败：" + message, true);
    toast("请求失败：" + message, "error");
  });

  document.querySelectorAll(".tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      switchTab(tab.dataset.tab);
      if (tab.dataset.tab === "logs") { loadLogs(); }
      if (tab.dataset.tab === "rules") { loadRules(); }
      if (tab.dataset.tab === "setup") { loadSetup(true); }
      if (tab.dataset.tab === "config") { loadConfig(); }
      if (tab.dataset.tab === "files") { openFilesTab(); }
      if (tab.dataset.tab === "security") { loadSecurity(); }
    });
  });

  // ---------- 概览 ----------

  function chip(label, on) {
    var node = el("span", { class: "chip", text: label });
    node.setAttribute("data-on", on ? "true" : "false");
    return node;
  }

  function renderSecurityNotice(summary) {
    var host = document.querySelector("#panel-overview .panel-body");
    if (!host) { return; }
    var old = document.getElementById("security-notice");
    if (old && old.parentNode) { old.parentNode.removeChild(old); }
    if (!summary || !summary.high) { return; }

    var box = el("div", { class: "notice" });
    box.setAttribute("id", "security-notice");
    box.appendChild(el("strong", { text: "有 " + summary.high + " 项高风险" }));
    box.appendChild(el("p", {
      text: "中 " + summary.medium + " · 低 " + summary.low + "，去「安全」页看详情和处理。"
    }));
    var go = el("button", { class: "btn sm", type: "button", text: "去处理" });
    go.addEventListener("click", function () { switchTab("security"); loadSecurity(); });
    box.appendChild(go);
    host.insertBefore(box, host.firstChild);
  }

  function renderBootstrapNotice(info) {
    var host = document.querySelector("#panel-overview .panel-body");
    if (!host) { return; }
    var old = document.getElementById("bootstrap-notice");
    if (old && old.parentNode) { old.parentNode.removeChild(old); }
    if (!info || !info.auto) { return; }

    var when = info.at ? new Date(info.at * 1000).toLocaleString() : "刚刚";
    var box = el("div", { class: "notice" });
    box.setAttribute("id", "bootstrap-notice");
    box.appendChild(el("strong", { text: "启动时自动下载了 rootfs" }));
    box.appendChild(el("p", {
      text: when + " · " + (info.preset || "") + " " + (info.version || "") +
        " · 原因：" + (info.reason || "rootfs 未就绪")
    }));
    box.appendChild(el("p", {
      class: "hint",
      text: "这不是你点的按钮。不想要这种行为，就把配置里的 auto_bootstrap 关掉。"
    }));
    host.insertBefore(box, host.firstChild);
  }

  function renderOverview(data) {
    if (data && data.ui) {
      UI.language = data.ui.language || "";
      UI.tour_seen = !!data.ui.tour_seen;
    }
    if (data && (data.language || UI.language)) {
      applyLanguage(UI.language || data.language);
    }
    if (data && !UI.language) { showLanguagePicker(); }
    $("backend-meta").textContent =
      data.backend + " · " + (data.rootfs_ready ? "rootfs 就绪" : "rootfs 未就绪") +
      " · " + data.session_count + " 个沙盒";

    var dot = $("ov-dot");
    if (dot) {
      dot.setAttribute("data-state", data.enabled && data.rootfs_ready ? "ok" : "error");
    }

    var backend = $("ov-backend");
    if (backend) { backend.textContent = data.backend_desc || data.backend; }

    var rootfs = $("ov-rootfs");
    if (rootfs) {
      var bits = ["rootfs " + (data.rootfs_ready ? "就绪" : "未就绪")];
      if (data.rootfs_preset) {
        bits.push((data.rootfs_preset + " " + (data.rootfs_version || "")).trim());
      }
      bits.push(data.session_count + " 个沙盒");
      rootfs.textContent = bits.join(" · ");
    }

    var chips = $("ov-chips");
    if (chips) {
      chips.textContent = "";
      chips.appendChild(chip("已启用", data.enabled));
      chips.appendChild(chip("联网", data.allow_network));
      chips.appendChild(chip("导入宿主文件", data.allow_import));
    }

    var stats = $("overview-stats");
    if (stats) {
      stats.textContent = "";
      var items = [
        ["沙盒数量", data.session_count, "个"],
        ["授权用户", data.users, "人"],
        ["每用户配额", data.quota_text, ""],
        ["命令超时", data.timeout_seconds, "秒"]
      ];
      items.forEach(function (item) {
        var value = el("div", { class: "stat-value" });
        var num = el("span", { class: "stat-num" });
        value.appendChild(num);
        countUp(num, item[1]);
        if (item[2]) { value.appendChild(el("span", { class: "unit", text: item[2] })); }
        stats.appendChild(el("div", { class: "stat" }, [
          value,
          el("div", { class: "stat-label", text: item[0] })
        ]));
      });
    }

    renderMetrics(data.metrics);
    renderSecurityNotice(data.security_summary);
    renderBootstrapNotice(data.bootstrap);

    var detail = $("overview-detail");
    if (detail) {
      detail.textContent = "";
      var rows = [
        ["后端说明", data.backend_desc],
        ["rootfs", ((data.rootfs_preset || "-") + " " + (data.rootfs_version || "") + " " + (data.rootfs_arch || "")).trim()],
        ["数据目录", data.base_dir],
        ["审计日志", data.audit_path],
        ["运行时长", Math.round(data.uptime_seconds / 60) + " 分钟"],
        ["健康", data.health && data.health.ok ? "正常" : (data.health ? data.health.reason : "-")],
        ["进程", data.health ? data.health.rss_mb + "MB · " + data.health.threads + " 线程" : "-"]
      ];
      rows.forEach(function (pair) {
        detail.appendChild(el("div", { class: "kv-row" }, [
          el("span", { class: "kv-key", text: pair[0] }),
          el("span", { class: "kv-val", text: String(pair[1] || "-") })
        ]));
      });
      (data.warnings || []).forEach(function (warning) {
        detail.appendChild(el("p", { class: "warn", text: warning }));
      });
    }
  }

  // ---------- 漫游引导 ----------

  var TOUR_STEPS = [
    { sel: "#boot-status", title: "先看这行字", text: "它显示页面有没有连上 AstrBot。绿色系就是通了，红字是具体错误。" },
    { sel: '[data-tab="setup"]', title: "部署", text: "第一次用：选套餐 → 点「下载 rootfs」→ 等它装完包。" },
    { sel: "#setup-apply", title: "一键授权", text: "填你在当前平台的用户 ID，点这个按钮就能开始用 /sbx。" },
    { sel: '[data-tab="config"]', title: "设置", text: "配置都能在这里改，包括「沙盒内是不是 root」。" },
    { sel: '[data-tab="files"]', title: "文件", text: "选用户就能浏览沙盒，支持预览、下载、删除。" },
    { sel: '[data-tab="exec"]', title: "执行", text: "临时跑命令看输出，和聊天里的 /sbx run 一样。" }
  ];

  var tour = { index: 0, mask: null, hole: null, card: null };

  function tourCleanup() {
    [tour.mask, tour.hole, tour.card].forEach(function (node) {
      if (node && node.parentNode) { node.parentNode.removeChild(node); }
    });
    tour.mask = tour.hole = tour.card = null;
  }

  function tourShow(index) {
    if (index < 0 || index >= TOUR_STEPS.length) { return tourFinish(); }
    tour.index = index;
    var step = TOUR_STEPS[index];
    var target = document.querySelector(step.sel);

    if (!tour.mask) {
      tour.mask = el("div", { class: "tour-mask" });
      document.body.appendChild(tour.mask);
    }
    if (!tour.hole) {
      tour.hole = el("div", { class: "tour-hole" });
      document.body.appendChild(tour.hole);
    }
    if (!tour.card) {
      tour.card = el("div", { class: "tour-card" });
      document.body.appendChild(tour.card);
    }

    tour.card.textContent = "";
    tour.card.appendChild(el("h3", { text: step.title }));
    tour.card.appendChild(el("p", { text: step.text }));
    var actions = el("div", { class: "tour-actions" });
    actions.appendChild(el("span", { class: "tour-step", text: (index + 1) + "/" + TOUR_STEPS.length }));

    if (index > 0) {
      var prev = el("button", { class: "btn sm ghost", type: "button", text: "上一步" });
      prev.addEventListener("click", function () { tourShow(index - 1); });
      actions.appendChild(prev);
    }
    var skip = el("button", { class: "btn sm ghost", type: "button", text: "跳过" });
    skip.addEventListener("click", tourFinish);
    actions.appendChild(skip);
    var next = el("button", { class: "btn sm primary", type: "button", text: index === TOUR_STEPS.length - 1 ? "完成" : "下一步" });
    next.addEventListener("click", function () { tourShow(index + 1); });
    actions.appendChild(next);
    tour.card.appendChild(actions);

    if (!target) {
      tour.hole.style.width = "0px";
      tour.hole.style.height = "0px";
      tour.card.style.left = "16px";
      tour.card.style.top = "80px";
      return;
    }
    target.scrollIntoView({ block: "center", behavior: "smooth" });
    var rect = target.getBoundingClientRect();
    var pad = 6;
    tour.hole.style.left = (rect.left - pad) + "px";
    tour.hole.style.top = (rect.top - pad) + "px";
    tour.hole.style.width = (rect.width + pad * 2) + "px";
    tour.hole.style.height = (rect.height + pad * 2) + "px";

    var cardTop = rect.bottom + 12;
    if (cardTop + 200 > window.innerHeight) { cardTop = Math.max(16, rect.top - 200); }
    tour.card.style.top = cardTop + "px";
    tour.card.style.left = Math.min(
      Math.max(16, rect.left),
      window.innerWidth - 340
    ) + "px";
  }

  function tourFinish() {
    tourCleanup();
    savePrefs({ tour_seen: true });
  }

  $("tour-start").addEventListener("click", function () { tourShow(0); });
  window.addEventListener("resize", function () {
    if (tour.card) { tourShow(tour.index); }
  });

  // ---------- 设置 ----------

  function fieldRow(field) {
    var row = el("div", { class: "field" });
    var input;

    if (field.key === "chroot_uid") {
      // 最常改的一项：沙盒里是不是 root
      input = el("select", { "data-key": field.key });
      [["65534", "nobody（安全，apt 用不了）"], ["0", "root（apt 直接可用，风险更高）"]]
        .forEach(function (pair) {
          var option = el("option", { value: pair[0], text: pair[1] });
          if (String(field.value) === pair[0]) { option.selected = true; }
          input.appendChild(option);
        });
    } else if (field.kind === "bool") {
      input = el("input", { type: "checkbox", "data-key": field.key });
      input.checked = !!field.value;
    } else if (field.kind === "select") {
      input = el("select", { "data-key": field.key });
      (field.options || []).forEach(function (value) {
        var option = el("option", { value: value, text: value });
        if (value === field.value) { option.selected = true; }
        input.appendChild(option);
      });
    } else if (field.kind === "int") {
      input = el("input", { type: "number", "data-key": field.key, min: "0" });
      input.value = field.value === undefined ? "" : field.value;
    } else if (field.kind === "list") {
      input = el("input", { type: "text", "data-key": field.key });
      input.value = (field.value || []).join(", ");
    } else {
      input = el("input", { type: "text", "data-key": field.key });
      input.value = field.value === undefined ? "" : field.value;
    }

    var label = el("label", { text: field.label + (field.restart ? "（需重启）" : "") });
    row.appendChild(label);
    row.appendChild(input);
    if (field.hint) { row.appendChild(el("p", { class: "hint", text: field.hint })); }
    return row;
  }

  function loadConfig() {
    var host = $("config-form");
    host.textContent = "";
    host.appendChild(skeletonBlock(6));
    return api("/settings").catch(function () {
      return api("/config");   // 旧版本后端没有 /settings
    }).then(function (data) {
      var fields = data.fields || [];
      host.textContent = "";
      if (!fields.length) {
        host.appendChild(el("p", {
          class: "warn",
          text: "接口返回了 0 个配置项，可能是版本不匹配。"
        }));
        return data;
      }
      // 按分组渲染：同组字段排在一起，组名做小标题
      var groups = {};
      var order = [];
      fields.forEach(function (field) {
        var name = field.group || "其它";
        if (!groups[name]) { groups[name] = []; order.push(name); }
        groups[name].push(field);
      });

      order.forEach(function (name) {
        host.appendChild(el("div", { class: "config-group", text: name }));
        var grid = el("div", { class: "form-grid" });
        groups[name].forEach(function (field) {
          try {
            grid.appendChild(fieldRow(field));
          } catch (error) {
            grid.appendChild(el("p", {
              class: "warn",
              text: "字段 " + field.key + " 渲染失败：" + error.message
            }));
          }
        });
        host.appendChild(grid);
      });
      if (data.warnings && data.warnings.length) {
        host.insertBefore(el("p", { class: "warn", text: data.warnings.join("；") }), host.firstChild);
      }
      $("config-path").textContent = "配置文件：" + (data.path || "（还没生成）");
      return data;
    }).catch(function (error) {
      // 错误留在页面上，不要只弹一个 3 秒就消失的提示
      host.textContent = "";
      host.appendChild(el("p", { class: "warn", text: "加载配置失败：" + error.message }));
      var retry = el("button", { class: "btn sm", type: "button", text: "重试" });
      retry.addEventListener("click", loadConfig);
      host.appendChild(retry);
      return null;
    });
  }

  $("config-save").addEventListener("click", function () {
    var button = $("config-save");
    button.disabled = true;
    toast("正在保存设置…", "loading");
    var payload = {};
    document.querySelectorAll("#config-form [data-key]").forEach(function (input) {
      var key = input.getAttribute("data-key");
      if (input.type === "checkbox") { payload[key] = input.checked; return; }
      var value = input.value;
      if (value === "") { return; }
      payload[key] = value;
    });
    api("/settings/save", { method: "POST", body: payload })
      .then(function (data) {
        button.disabled = false;
        var extra = (data.restart_labels || []).length
          ? "；需重启：" + data.restart_labels.join("、")
          : "";
        toast("已保存" + extra);
        bootStatus("已连接 · 自动刷新 10s");
        loadConfig();
        loadSetup(false);
      })
      .catch(function (error) {
        button.disabled = false;
        toast("保存失败：" + error.message, "error");
      });
  });

  // ---------- 部署 ----------

  function setupOut(text) {
    $("setup-out").textContent = text;
    $("setup-out").scrollTop = 0;
  }

  function clock() {
    var now = new Date();
    function pad(value) { return (value < 10 ? "0" : "") + value; }
    return pad(now.getHours()) + ":" + pad(now.getMinutes()) + ":" + pad(now.getSeconds());
  }

  // 每个操作都明确写出「开始 / 完成」，避免用户以为还在转圈
  function beginMark(label) {
    return "[开始] " + label + "  " + clock();
  }

  function endMark(label, startedAt, ok) {
    var seconds = ((Date.now() - startedAt) / 1000).toFixed(1);
    return (ok === false ? "[失败] " : "[完成] ") + label + "  用时 " + seconds + "s  " + clock();
  }

  // 套餐选项由后端给，页面不写死（否则后端换了发行版，页面还写着旧名字）
  function fillProfiles(data) {
    var select = $("setup-profile");
    var profiles = data.profiles || [];
    if (!profiles.length) { return; }
    var current = data.preferred || data.profile || "";
    select.textContent = "";
    profiles.forEach(function (item) {
      var option = document.createElement("option");
      option.value = item.name;
      option.textContent = item.name + " · " + item.description + "（约 " + item.approx_mb + "MB）";
      if (item.name === current) { option.selected = true; }
      select.appendChild(option);
    });
  }

  function renderAdvice(data) {
    var host = $("setup-advice");
    host.textContent = "";
    var lines = [
      ["后端", data.backend + "（" + data.backend_desc + "）"],
      ["rootfs", data.rootfs && data.rootfs.ready
        ? (data.rootfs.preset + " " + data.rootfs.version + " " + data.rootfs.arch)
        : "未就绪"],
      ["worker", data.worker && data.worker.alive ? "运行中" : "未启动"],
      ["健康", data.health && data.health.ok ? "正常" : (data.health ? data.health.reason : "-")],
      ["容器", data.runtime && data.runtime.in_container
        ? (data.runtime.container_kind + (data.runtime.is_root ? " / root" : " / 非 root"))
        : "否"],
      ["用户数", String(data.counts ? data.counts.users : 0)]
    ];
    lines.forEach(function (pair) {
      host.appendChild(el("div", { class: "row" }, [
        el("span", { class: "muted", text: pair[0] + "：" }),
        el("span", { class: "mono", text: String(pair[1]) })
      ]));
    });
    (data.advice || []).forEach(function (tip) {
      host.appendChild(el("p", { class: "warn", text: tip }));
    });
  }

  function loadSetup(writeOutput) {
    var startedAt = Date.now();
    return api("/setup/probe")
      .then(function (data) {
        state.setup = data;
        fillProfiles(data);
        renderAdvice(data);
        if (!writeOutput) { return data; }
        setupOut(beginMark("环境自检") + "\n" +
          "后端：" + data.backend + "\n" +
          "rootfs：" + (data.rootfs.ready ? data.rootfs.path : "未就绪") + "\n" +
          "数据目录：" + data.paths.base + "\n" +
          "会话目录：" + data.paths.sessions + "\n" +
          "日志：" + data.paths.logs + "\n" +
          "worker：" + JSON.stringify(data.worker) + "\n" +
          "health：" + JSON.stringify(data.health) + "\n" +
          "套餐：" + (data.profile || "-") + "（可选：" +
          (data.profiles || []).map(function (item) {
            return item.name + " " + item.approx_mb + "MB";
          }).join(" / ") + "）" +
          "\n\n" + endMark("环境自检", startedAt, true)
        );
        return data;
      })
      .catch(function (error) { setupOut("自检失败：" + error.message); });
  }

  function runSelfTest(deep) {
    var startedAt = Date.now();
    var label = deep ? "深度自检" : "快速自检";
    setupOut(beginMark(label) + "\n可能要几秒，别关页面…");
    api("/setup/test", { method: "POST", body: { deep: !!deep } })
      .then(function (data) {
        var lines = (data.checks || []).map(function (check) {
          var mark = check.skipped ? "跳过" : (check.ok ? "通过" : "失败");
          return "[" + mark + "] " + check.name + "  " + check.ms + "ms  " + check.detail;
        });
        lines.push("");
        lines.push("结果：" + data.passed + "/" + data.total + (data.ok ? "，可以用了" : "，有失败项"));
        lines.push("（自检结束，结果会一直留在这里，自动刷新不会冲掉）");

        setupOut(lines.join("\n"));
      })
      .catch(function (error) { setupOut("自检失败：" + error.message); });
  }

  function diagnose() {
    var lines = [
      "页面地址：" + window.location.href,
      "origin：" + window.location.origin + "（受限 iframe 里是 null，正常）",
      "AstrBotPluginView：" + (findBridge() ? "已注入" : "没找到"),
      "bridge 方法：" + (bridge ? Object.keys(bridge).join(",") : "-"),
      "上下文：" + (bridgeContext ? JSON.stringify(bridgeContext).slice(0, 200) : "-"),
      "bridge-sdk.js：" + (window.__bridgeSdkFailed ? "加载失败（404？）" : "未报错"),
      "localStorage：" + (storageGet("sandbox-probe") === null ? "不可用（正常，走 bridge）" : "可用")
    ];
    var startedAt = Date.now();
    setupOut(beginMark("诊断连接") + "\n" + lines.join("\n") + "\n\n正在通过 bridge 测试接口…");
    api("/overview").then(function (data) {
      lines.push("[成功] overview → backend=" + data.backend);
      lines.push("");
      lines.push(endMark("诊断连接", startedAt, true));
      setupOut(lines.join("\n"));
    }).catch(function (error) {
      lines.push("[失败] " + error.message);
      lines.push("");
      lines.push(endMark("诊断连接", startedAt, false));
      setupOut(lines.join("\n"));
    });
  }

  function checkSources() {
    var startedAt = Date.now();
    setupOut(beginMark("检测下载源") + "\n正在爬镜像索引并探测，最多十几秒…");
    api("/setup/sources")
      .then(function (data) {
        var perPreset = data.presets || {};
        var summary = Object.keys(perPreset).map(function (name) {
          return name + " " + perPreset[name];
        }).join(" / ");
        var lines = [
          "架构：" + data.arch + "，找到 " + data.found + " 个候选（" + summary + "）",
          "按发行版各取前 2 个探测，共 " + data.checked + " 个：",
          ""
        ];
        (data.sources || []).forEach(function (item) {
          var mark = item.ok ? "可用" : "失败";
          var size = item.ok && item.bytes ? "  " + fmtBytes(item.bytes) : "";
          lines.push("[" + mark + "] " + item.preset + "  " + item.status + size);
          lines.push("       " + item.url);
          if (item.error) { lines.push("       " + item.error); }
        });
        var usable = (data.sources || []).filter(function (item) { return item.ok; }).length;
        lines.push("");
        if (!data.found) {
          lines.push("一个候选都没找到：索引站点可能被网络挡住，直接填 rootfs_url 更快。");
        } else if (usable) {
          lines.push("探测完成：" + usable + "/" + data.checked + " 可用。");
          lines.push("下一步：选套餐后点「下载 rootfs」；已经下过就不用管，直接聊天里用 /sbx。");
        } else {
          lines.push("全部不可用：容器出网可能被限制，建议填 rootfs_url（支持本地 tar 路径）。");
        }
        lines.push("");
        lines.push(endMark("检测下载源", startedAt, usable > 0));
        setupOut(lines.join("\n"));
      })
      .catch(function (error) { setupOut("检测失败：" + error.message); });
  }

  var taskRunning = false;

  function runRootfs() {
    var profile = $("setup-profile").value;
    var current = state.setup && state.setup.rootfs;
    var ready = !!(current && current.ready);
    var options = ready ? {
      title: "替换现有的 rootfs？",
      lines: [
        "当前已经有一套可用的 rootfs：" + (current.preset || "-") + " " + (current.version || ""),
        "继续会把它整个换掉 —— 所有用户沙盒里的系统文件、你手动装的软件包都会消失。",
        "/sandbox 下的用户文件会保留，但新系统里可能没有对应的解释器或库。",
        "正在运行的任务会失败。"
      ],
      confirmText: "替换",
      danger: true,
      requireText: "替换"
    } : {
      title: "下载 rootfs？",
      lines: [
        "将下载 " + profile + " 套餐并安装基础软件包，大约几分钟。",
        "下载期间沙盒不可用。"
      ],
      confirmText: "开始下载"
    };
    confirmDialog(options).then(function (yes) {
      if (yes) { startRootfs(profile); }
    });
  }

  function startRootfs(profile) {
    taskRunning = true;
    var sizes = { minimal: "约 20MB", standard: "约 180MB", full: "约 700MB" };
    var startedAt = Date.now();
    setupOut(beginMark("下载 rootfs") + "\n套餐 " + profile + "（" + (sizes[profile] || "") + "）…");
    api("/setup/rootfs", { method: "POST", body: { profile: profile } })
      .then(function (data) {
        var taskId = data.task.id;
        var timer = window.setInterval(function () {
          api("/setup/task", { query: { task_id: taskId } })
            .then(function (payload) {
              var task = payload.task;
              var body = (task.lines || []).join("\n");
              var tail = task.done
                ? endMark("下载 rootfs", startedAt, task.ok) + (task.ok ? "" : "\n原因：" + task.error)
                : "… 进行中（已 " + Math.round((Date.now() - startedAt) / 1000) + "s）";
              setupOut(body + "\n\n" + tail);
              if (task.done) {
                window.clearInterval(timer);
                taskRunning = false;
                loadSetup(false);
              }
            })
            .catch(function (error) {
              window.clearInterval(timer);
              setupOut("查询进度失败：" + error.message);
            });
        }, 900);
      })
      .catch(function (error) { setupOut("启动下载失败：" + error.message); });
  }

  function applyUser() {
    var userId = $("setup-user").value.trim();
    if (!userId) { toast("先填用户 ID", "error"); return; }
    api("/setup/apply", {
      method: "POST",
      body: { user_id: userId, quota_mb: Number($("setup-quota").value || 0) }
    }).then(function (data) {
      toast("已授权");
      var lines = ["[完成] 一键授权  " + clock(), "已授权 " + data.user.user_id, data.hint];
      if (data.guide) {
        lines.push("", "把下面这段发给对方：", "────────────", data.guide);
      }
      setupOut(lines.join("\n"));
      loadSetup();
    }).catch(function (error) { toast(error.message, "error"); });
  }

  $("setup-audit").addEventListener("click", function () {
    toast("正在体检…", "loading");
    api("/security").then(function (data) {
      setupOut(data.report || "（没有内容）");
      toast("体检完成");
    }).catch(function (error) { toast("体检失败：" + error.message, "error"); });
  });

  $("setup-harden").addEventListener("click", function () {
    confirmDialog({
      title: "清掉 setuid 位？",
      lines: [
        "会把 rootfs 里所有 setuid/setgid 位清掉。",
        "这不会破坏普通命令，但沙盒里任何依赖 setuid 的程序（su、passwd、mount）会失效。",
        "这是好事：它们正是 chroot 逃逸的入口。"
      ],
      confirmText: "清理"
    }).then(function (yes) {
      if (!yes) { return; }
      toast("正在清理…", "loading");
      api("/harden", { method: "POST" }).then(function (data) {
        setupOut("[完成] 清理 setuid\n" + (data.message || ""));
        toast("清理完成");
      }).catch(function (error) { toast("清理失败：" + error.message, "error"); });
    });
  });

  $("setup-probe").addEventListener("click", function () { loadSetup(true); });
  $("setup-test").addEventListener("click", function () { runSelfTest(false); });
  $("setup-test-deep").addEventListener("click", function () { runSelfTest(true); });
  $("pkg-install").addEventListener("click", function () {
    var names = $("pkg-names").value.trim();
    if (!names) { toast("先填包名", "error"); return; }
    confirmDialog({
      title: "安装软件包",
      lines: [
        "将以 root 身份在 rootfs 里安装：" + names,
        "这会写进共享的 rootfs，所有用户的沙盒都会受影响。",
        "安装期间正在跑的命令可能失败。"
      ],
      confirmText: "安装"
    }).then(function (yes) { if (yes) { installPackages(names); } });
  });

  function installPackages(names) {
    var startedAt = Date.now();
    setupOut(beginMark("安装软件包") + "\n" + names + "\n正在安装，可能要一会儿…");
    api("/packages/install", { method: "POST", body: { packages: names.split(/\s+/) } })
      .then(function (data) {
        setupOut(endMark("安装软件包", startedAt, true) + "\n" + (data.message || ""));
        toast("安装完成");
      })
      .catch(function (error) {
        setupOut(endMark("安装软件包", startedAt, false) + "\n" + error.message);
        toast(error.message, "error");
      });
  }

  $("setup-diag").addEventListener("click", diagnose);
  $("setup-sources").addEventListener("click", checkSources);
  $("setup-rootfs").addEventListener("click", runRootfs);
  $("setup-apply").addEventListener("click", applyUser);

  // ---------- 权限 ----------

  function fillUserOptions() {
    ["user-options", "user-options-2"].forEach(function (id) {
      var list = $(id);
      if (!list) { return; }
      list.textContent = "";
      state.users.forEach(function (user) {
        list.appendChild(el("option", { value: user.user_id }));
      });
    });
  }

  function renderUsers(data) {
    state.users = data.users || [];
    fillUserOptions();

    var host = $("users-table");
    host.textContent = "";
    if (!state.users.length) {
      host.appendChild(emptyState(
        "还没有单独授权的用户",
        "上面的表单加一个，或者在插件配置里用 allowed_users。"
      ));
    } else {
      var table = el("table", {});
      var head = el("tr", {}, [
        el("th", { text: "用户" }),
        el("th", { text: "备注" }),
        el("th", { class: "num", text: "配额" }),
        el("th", { class: "num", text: "超时" }),
        el("th", { text: "联网" }),
        el("th", { text: "导入" }),
        el("th", { text: "能力" }),
        el("th", { text: "状态" }),
        el("th", { text: "操作" })
      ]);
      table.appendChild(el("thead", {}, [head]));
      var body = el("tbody", {});
      state.users.forEach(function (user) {
        var tr = el("tr", {}, [
          el("td", { class: "mono", text: user.user_id }),
          el("td", { text: user.note || "-" }),
          el("td", { class: "num", text: user.quota_bytes ? fmtBytes(user.quota_bytes) : "全局" }),
          el("td", { class: "num", text: user.timeout_seconds ? user.timeout_seconds + "s" : "全局" }),
          el("td", { text: user.allow_network === null ? "全局" : (user.allow_network ? "允许" : "禁止") }),
          el("td", { text: user.allow_import === null ? "全局" : (user.allow_import ? "允许" : "禁止") }),
          el("td", { text: capabilityText(user) }),
          el("td", {}, [
            el("span", { class: "badge " + (user.enabled ? "ok" : "off"), text: user.enabled ? "启用" : "停用" })
          ])
        ]);
        var actions = el("td", {}, [
          el("button", { class: "btn sm ghost", type: "button", text: "编辑" }),
          el("button", { class: "btn sm ghost", type: "button", text: "删除" })
        ]);
        actions.children[0].addEventListener("click", function () { editUser(user); });
        actions.children[1].addEventListener("click", function () { deleteUser(user.user_id); });
        tr.appendChild(actions);
        body.appendChild(tr);
      });
      table.appendChild(body);
      host.appendChild(table);
    }

    var globalHost = $("users-global");
    globalHost.textContent = "";
    var conf = data.global_config;
    var lines = [
      ["allow_all", conf.allow_all ? "开启" : "关闭"],
      ["allowed_users", (conf.allowed_users || []).join("、") || "（空）"],
      ["allowed_groups", (conf.allowed_groups || []).join("、") || "（空）"],
      ["admin_users", (conf.admin_users || []).join("、") || "（空）"]
    ];
    lines.forEach(function (pair) {
      globalHost.appendChild(el("div", { class: "row" }, [
        el("span", { class: "muted", text: pair[0] + "：" }),
        el("span", { class: "mono", text: pair[1] })
      ]));
    });
    globalHost.appendChild(el("p", {
      class: "hint",
      text: "改这些要动 AstrBot 的插件配置，这里只读。"
    }));
  }

  function capabilityText(user) {
    var names = [];
    if (user.allow_exec) { names.push("执行"); }
    if (user.allow_py) { names.push("Py"); }
    if (user.allow_export) { names.push("导出"); }
    if (user.allow_reset) { names.push("重置"); }
    return names.length ? names.join(" ") : "无";
  }

  function editUser(user) {
    $("u-id").value = user.user_id;
    $("u-note").value = user.note || "";
    $("u-quota").value = user.quota_bytes ? Math.round(user.quota_bytes / (1024 * 1024)) : 0;
    $("u-timeout").value = user.timeout_seconds || 0;
    $("u-network").value = user.allow_network === null ? "" : String(user.allow_network);
    $("u-import").value = user.allow_import === null ? "" : String(user.allow_import);
    $("u-enabled").value = String(user.enabled);
    $("u-exec").checked = user.allow_exec !== false;
    $("u-py").checked = user.allow_py !== false;
    $("u-export").checked = user.allow_export !== false;
    $("u-reset").checked = user.allow_reset !== false;
    $("u-id").focus();
  }

  function deleteUser(userId) {
    confirmDialog({
      title: "删除授权",
      lines: [
        "用户 " + userId + " 将不能再使用沙盒。",
        "已经生成的沙盒文件不会被删除，重新授权后还能看到。"
      ],
      confirmText: "删除",
      danger: true
    }).then(function (yes) {
      if (!yes) { return; }
      api("/users/delete", { method: "POST", body: { user_id: userId } })
        .then(function () { toast("已删除"); return loadUsers(); })
        .catch(function (error) { toast(error.message, "error"); });
    });
  }

  $("user-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var payload = {
      user_id: $("u-id").value.trim(),
      note: $("u-note").value.trim(),
      quota_bytes: Number($("u-quota").value || 0) * 1024 * 1024,
      timeout_seconds: Number($("u-timeout").value || 0),
      enabled: $("u-enabled").value === "true",
      allow_network: $("u-network").value === "" ? null : $("u-network").value === "true",
      allow_import: $("u-import").value === "" ? null : $("u-import").value === "true",
      allow_exec: $("u-exec").checked,
      allow_py: $("u-py").checked,
      allow_export: $("u-export").checked,
      allow_reset: $("u-reset").checked
    };
    if (!payload.user_id) { toast("用户 ID 不能为空", "error"); return; }
    toast("正在保存用户…", "loading");
    api("/users/save", { method: "POST", body: payload })
      .then(function () { toast("权限已保存"); return loadUsers(); })
      .catch(function (error) { toast(error.message, "error"); });
  });

  $("user-reset").addEventListener("click", function () {
    $("user-form").reset();
    $("u-quota").value = 0;
    $("u-timeout").value = 0;
  });

  function loadUsers() {
    return api("/users").then(function (data) {
      renderUsers(data);
      return data;
    }).catch(function (error) { toast(error.message, "error"); });
  }

  // ---------- 文件 ----------

  function renderTree(data) {
    var host = $("tree");
    host.textContent = "";
    if (!data.entries || !data.entries.length) {
      host.appendChild(emptyState("这个目录是空的", "换一层看看，或者用「执行」页建个文件。"));
      $("tree-summary").textContent = "";
      return;
    }
    $("tree-summary").textContent = data.count + " 项 · " + data.summary.bytes_text;
    host.appendChild(renderNodes(data.entries, true));
    if (data.truncated) {
      host.appendChild(el("p", { class: "hint", text: "条目太多，只显示了一部分。" }));
    }
  }

  function renderNodes(entries, expanded) {
    var frag = document.createDocumentFragment();
    entries.forEach(function (entry) {
      if (entry.kind === "dir") {
        var details = el("details", {});
        if (expanded) { details.open = true; }
        var summary = el("summary");
        summary.appendChild(iconNode("dir"));
        summary.appendChild(el("span", { text: entry.name }));
        details.appendChild(summary);
        if (entry.children) { details.appendChild(renderNodes(entry.children, false)); }
        frag.appendChild(details);
      } else {
        var button = el("button", {
          class: "file",
          type: "button",
          "data-path": entry.path
        }, [
          iconNode(kindOf(entry.name)),
          el("span", { class: "name", text: entry.name }),
          el("span", { class: "size", text: entry.size_text })
        ]);
        button.addEventListener("click", function () { openFile(entry.path); });
        frag.appendChild(button);
      }
    });
    return frag;
  }

  function openFile(path) {
    var user = $("f-user").value.trim();
    if (!user) {
      $("preview-body").textContent = "";
      $("preview-body").appendChild(el("p", { class: "empty", text: "先在上面选一个用户" }));
      return;
    }
    $("preview-title").textContent = path;
    $("preview-body").textContent = "";
    $("preview-body").appendChild(el("p", { class: "empty", text: "读取中…" }));
    api("/file", { query: { user: user, path: path } })
      .then(function (data) {
        state.preview = data;
        state.currentPath = path;
        renderPreview(data);
      })
      .catch(function (error) {
        // 失败原因直接写在预览区，比一闪而过的 toast 有用
        $("preview-body").textContent = "";
        $("preview-body").appendChild(el("p", { class: "warn", text: "预览失败：" + error.message }));
        $("preview-body").appendChild(el("p", {
          class: "hint",
          text: "可以点上面的「下载」把文件拿到本机，或用「复制宿主路径」直接去服务器上取。"
        }));
      });
  }


  // ---------- 预览：语法高亮 / Markdown / CSV / JSON / 十六进制 ----------

  var LANG_BY_EXT = {
    py: "python", python: "python", pyw: "python",
    js: "js", mjs: "js", cjs: "js", ts: "js", jsx: "js", tsx: "js",
    sh: "shell", bash: "shell", zsh: "shell",
    yml: "yaml", yaml: "yaml", toml: "yaml", ini: "yaml", conf: "yaml", env: "yaml",
    json: "json", sql: "sql",
    css: "css", scss: "css", html: "html", htm: "html", xml: "html", svg: "html"
  };

  var KEYWORDS = {
    python: "def class return if elif else for while import from as with try except finally raise yield lambda pass break continue global nonlocal assert del in is not and or None True False self async await",
    js: "function var let const return if else for while do switch case break continue new class extends super import export from default async await try catch finally throw typeof instanceof this null undefined true false delete void",
    shell: "if then else elif fi for while do done case esac function return export local echo cd ls cat rm mkdir cp mv grep sed awk",
    yaml: "true false null yes no on off",
    sql: "select from where insert into values update set delete create table drop alter join left right inner outer on group by order having limit offset as and or not null",
    json: "true false null",
    css: "",
    html: ""
  };

  function langOf(ext) { return LANG_BY_EXT[ext] || ""; }

  // 逐字符扫一遍，产出 [类型, 文本] 片段；比正则替换安全，不会互相吃掉
  function tokenize(code, lang) {
    var tokens = [];
    var i = 0;
    var n = code.length;
    var keywords = {};
    (KEYWORDS[lang] || "").split(" ").forEach(function (word) {
      if (word) { keywords[word] = true; }
    });

    var lineMark = "#";
    if (lang === "js" || lang === "css" || lang === "html" || lang === "json") { lineMark = "//"; }
    if (lang === "sql") { lineMark = "--"; }
    var blockMark = lang === "js" || lang === "css" || lang === "sql";

    while (i < n) {
      var ch = code.charAt(i);

      if (code.substr(i, lineMark.length) === lineMark) {
        var endLine = code.indexOf("\n", i);
        if (endLine < 0) { endLine = n; }
        tokens.push(["com", code.slice(i, endLine)]);
        i = endLine;
        continue;
      }

      if (blockMark && ch === "/" && code.charAt(i + 1) === "*") {
        var endBlock = code.indexOf("*/", i + 2);
        endBlock = endBlock < 0 ? n : endBlock + 2;
        tokens.push(["com", code.slice(i, endBlock)]);
        i = endBlock;
        continue;
      }

      if (ch === '"' || ch === "'" || ch === "`") {
        var quote = ch;
        var j = i + 1;
        while (j < n) {
          if (code.charAt(j) === "\\") { j += 2; continue; }
          if (code.charAt(j) === quote) { j += 1; break; }
          if (code.charAt(j) === "\n" && quote !== "`") { break; }
          j += 1;
        }
        tokens.push(["str", code.slice(i, j)]);
        i = j;
        continue;
      }

      if (/[0-9]/.test(ch)) {
        var k = i;
        while (k < n && /[0-9a-fA-FxX._]/.test(code.charAt(k))) { k += 1; }
        tokens.push(["num", code.slice(i, k)]);
        i = k;
        continue;
      }

      if (/[A-Za-z_$]/.test(ch)) {
        var m = i;
        while (m < n && /[A-Za-z0-9_$]/.test(code.charAt(m))) { m += 1; }
        var word = code.slice(i, m);
        tokens.push([keywords[word] ? "kw" : "id", word]);
        i = m;
        continue;
      }

      tokens.push(["p", ch]);
      i += 1;
    }
    return tokens;
  }

  // 带行号的代码块
  function codeBlock(code, lang) {
    var wrap = el("div", { class: "code" });
    var tokens = tokenize(code, lang);
    var lines = [[]];

    tokens.forEach(function (token) {
      var parts = token[1].split("\n");
      parts.forEach(function (part, index) {
        if (index > 0) { lines.push([]); }
        if (part) { lines[lines.length - 1].push([token[0], part]); }
      });
    });

    var limit = Math.min(lines.length, 2000);
    for (var index = 0; index < limit; index += 1) {
      var row = el("div", { class: "code-line" });
      row.appendChild(el("span", { class: "ln", text: String(index + 1) }));
      var cell = el("span", { class: "lc" });
      if (!lines[index].length) {
        cell.appendChild(document.createTextNode(" "));
      } else {
        lines[index].forEach(function (pair) {
          if (pair[0] === "p" || pair[0] === "id") {
            cell.appendChild(document.createTextNode(pair[1]));
          } else {
            cell.appendChild(el("span", { class: "tok-" + pair[0], text: pair[1] }));
          }
        });
      }
      row.appendChild(cell);
      wrap.appendChild(row);
    }

    if (lines.length > limit) {
      wrap.appendChild(el("p", { class: "hint", text: "只显示前 " + limit + " 行。" }));
    }
    return wrap;
  }

  // 行内元素：`code`、**粗**、*斜*、[文字](http…)
  function inlineInto(node, text) {
    var pattern = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*]+\*)|(\[[^\]]+\]\(https?:\/\/[^)\s]+\))/g;
    var last = 0;
    var match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > last) {
        node.appendChild(document.createTextNode(text.slice(last, match.index)));
      }
      var token = match[0];
      if (token.charAt(0) === "`") {
        node.appendChild(el("code", { text: token.slice(1, -1) }));
      } else if (token.indexOf("**") === 0) {
        node.appendChild(el("strong", { text: token.slice(2, -2) }));
      } else if (token.charAt(0) === "*") {
        node.appendChild(el("em", { text: token.slice(1, -1) }));
      } else {
        var linkMatch = /^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/.exec(token);
        if (linkMatch) {
          var link = el("a", { href: linkMatch[2], text: linkMatch[1] });
          link.setAttribute("target", "_blank");
          link.setAttribute("rel", "noreferrer");
          node.appendChild(link);
        } else {
          node.appendChild(document.createTextNode(token));
        }
      }
      last = match.index + token.length;
    }
    if (last < text.length) {
      node.appendChild(document.createTextNode(text.slice(last)));
    }
    return node;
  }

  function renderMarkdown(text) {
    var root = el("div", { class: "md" });
    var lines = text.split("\n");
    var index = 0;
    var list = null;

    while (index < lines.length) {
      var line = lines[index];

      if (/^```/.test(line)) {
        var fence = line.slice(3).trim().toLowerCase();
        var buffer = [];
        index += 1;
        while (index < lines.length && !/^```/.test(lines[index])) {
          buffer.push(lines[index]);
          index += 1;
        }
        index += 1;
        root.appendChild(codeBlock(buffer.join("\n"), langOf(fence) || fence));
        continue;
      }

      var heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        var level = Math.min(heading[1].length + 1, 6);
        var node = document.createElement("h" + level);
        inlineInto(node, heading[2]);
        root.appendChild(node);
        list = null;
        index += 1;
        continue;
      }

      if (/^\s*[-*+]\s+/.test(line)) {
        if (!list || list.tagName !== "UL") {
          list = el("ul");
          root.appendChild(list);
        }
        var li = el("li");
        inlineInto(li, line.replace(/^\s*[-*+]\s+/, ""));
        list.appendChild(li);
        index += 1;
        continue;
      }

      if (/^\s*\d+\.\s+/.test(line)) {
        if (!list || list.tagName !== "OL") {
          list = el("ol");
          root.appendChild(list);
        }
        var oli = el("li");
        inlineInto(oli, line.replace(/^\s*\d+\.\s+/, ""));
        list.appendChild(oli);
        index += 1;
        continue;
      }

      if (/^>\s?/.test(line)) {
        var quote = el("blockquote");
        inlineInto(quote, line.replace(/^>\s?/, ""));
        root.appendChild(quote);
        list = null;
        index += 1;
        continue;
      }

      if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
        root.appendChild(el("hr"));
        list = null;
        index += 1;
        continue;
      }

      if (!line.trim()) {
        list = null;
        index += 1;
        continue;
      }

      var para = el("p");
      inlineInto(para, line);
      root.appendChild(para);
      list = null;
      index += 1;
    }
    return root;
  }

  // CSV：支持引号包裹和转义引号
  function parseCsv(text, delimiter) {
    var rows = [];
    var row = [];
    var field = "";
    var quoted = false;
    for (var index = 0; index < text.length; index += 1) {
      var ch = text.charAt(index);
      if (quoted) {
        if (ch === '"' && text.charAt(index + 1) === '"') { field += '"'; index += 1; }
        else if (ch === '"') { quoted = false; }
        else { field += ch; }
        continue;
      }
      if (ch === '"') { quoted = true; continue; }
      if (ch === delimiter) { row.push(field); field = ""; continue; }
      if (ch === "\n") { row.push(field); rows.push(row); row = []; field = ""; continue; }
      if (ch !== "\r") { field += ch; }
    }
    if (field || row.length) { row.push(field); rows.push(row); }
    return rows;
  }

  function renderCsv(text, delimiter) {
    var rows = parseCsv(text, delimiter).filter(function (row) {
      return row.length && !(row.length === 1 && !row[0].trim());
    });
    if (!rows.length) { return el("p", { class: "empty", text: "空表格。" }); }

    var maxCols = 0;
    rows.slice(0, 200).forEach(function (row) {
      maxCols = Math.max(maxCols, row.length);
    });
    maxCols = Math.min(maxCols, 24);

    var wrap = el("div", { class: "csv-wrap" });
    var table = el("table", { class: "csv" });
    var head = el("thead");
    var headRow = el("tr");
    rows[0].slice(0, maxCols).forEach(function (cell) {
      headRow.appendChild(el("th", { text: cell }));
    });
    head.appendChild(headRow);
    table.appendChild(head);

    var tbody = el("tbody");
    rows.slice(1, 201).forEach(function (row) {
      var tr = el("tr");
      row.slice(0, maxCols).forEach(function (cell) {
        tr.appendChild(el("td", { text: cell }));
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);

    if (rows.length > 201) {
      wrap.appendChild(el("p", { class: "hint", text: "只显示前 200 行，共 " + rows.length + " 行。" }));
    }
    return wrap;
  }

  function hexDump(base64, total) {
    var raw;
    try { raw = atob(base64); } catch (error) {
      return el("p", { class: "hint", text: "头部读不出来。" });
    }
    var lines = [];
    for (var offset = 0; offset < raw.length; offset += 16) {
      var chunk = raw.slice(offset, offset + 16);
      var hex = [];
      var ascii = "";
      for (var index = 0; index < 16; index += 1) {
        if (index < chunk.length) {
          var code = chunk.charCodeAt(index) & 0xff;
          hex.push(("0" + code.toString(16)).slice(-2));
          ascii += code >= 32 && code < 127 ? chunk.charAt(index) : ".";
        } else {
          hex.push("  ");
          ascii += " ";
        }
      }
      lines.push(
        ("000000" + offset.toString(16)).slice(-6) + "  " +
        hex.join(" ") + "  |" + ascii + "|"
      );
    }
    var wrap = el("div", { class: "code" });
    wrap.appendChild(el("pre", { class: "hex", text: lines.join("\n") }));
    if (total && total > raw.length) {
      wrap.appendChild(el("p", { class: "hint", text: "只显示前 " + raw.length + " 字节，共 " + total + " 字节。" }));
    }
    return wrap;
  }

  function renderRich(content, ext) {
    if (ext === "md" || ext === "markdown") { return renderMarkdown(content); }
    if (ext === "csv") { return renderCsv(content, ","); }
    if (ext === "tsv") { return renderCsv(content, "\t"); }
    if (ext === "json") {
      var pretty = content;
      try { pretty = JSON.stringify(JSON.parse(content), null, 2); } catch (error) { pretty = content; }
      return codeBlock(pretty, "json");
    }
    return codeBlock(content, langOf(ext));
  }

  function renderTextPreview(body, data) {
    var name = data.path || data.name || "";
    var ext = (name.split(".").pop() || "").toLowerCase();
    var content = data.content || "";

    if (state.previewMode === "edit") {
      var area = el("textarea", { rows: "18", spellcheck: "false" });
      area.value = content;
      body.appendChild(area);
    } else {
      body.appendChild(renderRich(content, ext));
    }
    if (data.truncated) {
      body.appendChild(el("p", {
        class: "hint",
        text: "文件较大，只加载了前一部分；保存会覆盖整份内容。"
      }));
    }
  }

  function renderPreview(data) {
    var body = $("preview-body");
    body.textContent = "";
    $("preview-title").textContent = data.path || data.name || "预览";
    $("preview-save").hidden = data.kind !== "text";
    $("preview-download").hidden = data.kind === "dir" || data.kind === "unknown";
    $("preview-copy").hidden = data.kind === "dir" || data.kind === "unknown";
    $("preview-delete").hidden = false;
    $("preview-edit").hidden = data.kind !== "text";
    $("preview-edit").textContent = state.previewMode === "edit" ? "预览" : "源码";

    if (data.kind === "text") {
      renderTextPreview(body, data);
      return;
    }

    if (data.kind === "image") {
      body.appendChild(el("img", { class: "preview", src: data.data_url, alt: data.name }));
      return;
    }

    if (data.kind === "binary") {
      body.appendChild(el("p", { class: "hint", text: data.message || "二进制文件，不提供预览。" }));
      if (data.head_b64) {
        body.appendChild(hexDump(data.head_b64, data.size_bytes || data.head_bytes));
      }
      body.appendChild(el("p", {
        class: "hint",
        text: "大小 " + (data.size_text || "-") + "，点上面的「下载」可以取到本机。"
      }));
      return;
    }

    if (data.kind === "dir") {
      var list = el("div", { class: "tree" });
      (data.entries || []).forEach(function (entry) {
        list.appendChild(el("div", { class: "file static" }, [
          iconNode(entry.kind === "dir" ? "dir" : kindOf(entry.name)),
          el("span", { class: "name", text: entry.name }),
          el("span", { class: "size", text: entry.size_text || "" })
        ]));
      });
      body.appendChild(list);
      return;
    }

    body.appendChild(el("p", { class: "empty", text: "无法预览这个条目（" + (data.kind || "?") + "）。" }));
  }

  $("preview-edit").addEventListener("click", function () {
    state.previewMode = state.previewMode === "edit" ? "view" : "edit";
    if (state.preview) { renderPreview(state.preview); }
  });

  function openFilesTab() {
    api("/users/all").then(function (data) {
      var select = $("f-user");
      var users = data.users || [];
      if (!users.length) {
        select.textContent = "";
        select.appendChild(el("option", { value: "", text: "还没有用户" }));
        return;
      }
      var current = select.value;
      select.textContent = "";
      users.forEach(function (user) {
        var option = el("option", {
          value: user.id,
          text: user.label + (user.has_session ? "" : "（空）")
        });
        select.appendChild(option);
      });
      if (current) { select.value = current; }
      if (!select.value) { select.value = users[0].id; }
      $("f-load").click();
    }).catch(function (error) { toast(error.message, "error"); });
  }

  $("f-up").addEventListener("click", function () {
    var path = $("f-path").value.trim() || ".";
    var parts = path.split("/").filter(function (item) { return item && item !== "."; });
    parts.pop();
    $("f-path").value = parts.length ? parts.join("/") : ".";
    $("f-load").click();
  });

  $("f-user").addEventListener("change", function () {
    $("f-path").value = ".";
    $("f-load").click();
  });

  $("f-load").addEventListener("click", function () {
    var user = $("f-user").value.trim();
    if (!user) { toast("先填用户", "error"); return; }
    var path = $("f-path").value.trim() || ".";
    api("/tree", { query: { user: user, path: path, depth: 2 } })
      .then(renderTree)
      .catch(function (error) { toast(error.message, "error"); });
  });

  $("preview-download").addEventListener("click", function () {
    var user = $("f-user").value.trim();
    if (!user || !state.currentPath) { return; }
    toast("正在准备下载…");
    api("/file/raw", { query: { user: user, path: state.currentPath } })
      .then(function (data) {
        var binary = atob(data.b64);
        var bytes = new Uint8Array(binary.length);
        for (var i = 0; i < binary.length; i++) { bytes[i] = binary.charCodeAt(i); }
        var blob = new Blob([bytes], { type: data.mime });
        var link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = data.name;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        window.setTimeout(function () { URL.revokeObjectURL(link.href); }, 2000);
        toast("已下载 " + data.name);
      })
      .catch(function (error) { toast(error.message, "error"); });
  });

  $("preview-copy").addEventListener("click", function () {
    var user = $("f-user").value.trim();
    if (!user || !state.currentPath) { return; }
    api("/file", { query: { user: user, path: state.currentPath } })
      .then(function (data) {
        var text = data.path || state.currentPath;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text);
          toast("已复制：" + text);
        } else {
          window.prompt("手动复制这个路径：", text);
        }
      })
      .catch(function (error) { toast(error.message, "error"); });
  });

  $("preview-save").addEventListener("click", function () {
    var user = $("f-user").value.trim();
    var area = $("preview-body").querySelector("textarea");
    if (!user || !area || !state.currentPath) { return; }
    api("/file/save", {
      method: "POST",
      body: { user: user, path: state.currentPath, content: area.value }
    }).then(function () { toast("已保存"); $("f-load").click(); })
      .catch(function (error) { toast(error.message, "error"); });
  });

  $("preview-delete").addEventListener("click", function () {
    var user = $("f-user").value.trim();
    if (!user || !state.currentPath) { return; }
    function doDelete() {
      api("/file/delete", { method: "POST", body: { user: user, path: state.currentPath } })
      .then(function () {
        toast("已删除");
        $("preview-body").textContent = "";
        $("preview-title").textContent = "预览";
        $("preview-save").hidden = true;
        $("preview-delete").hidden = true;
        $("f-load").click();
      })
        .catch(function (error) { toast(error.message, "error"); });
    }
    confirmDialog({
      title: "删除文件",
      lines: [
        "会删掉沙盒里的 " + state.currentPath + "。",
        "删了就找不回来，宿主机上的原件不受影响。"
      ],
      confirmText: "删除",
      danger: true
    }).then(function (yes) { if (yes) { doDelete(); } });
  });

  // ---------- 安全 ----------

  var SEV_LABEL = { high: "高", medium: "中", low: "低", info: "提示" };

  function loadSecurity() {
    var list = $("sec-list");
    list.textContent = "";
    list.appendChild(skeletonBlock(4));
    api("/security/scan").then(function (data) {
      list.textContent = "";
      var host = data.host || {};
      $("sec-host").textContent = [
        "内核 " + (host.kernel || "未知"),
        host.host_kernel ? "容器内（内核来自宿主）" : "宿主机",
        host.nft ? "有 nft" : "无 nft",
        host.iptables ? "有 iptables" : "无 iptables"
      ].join(" · ");

      var items = data.items || [];
      if (!items.length) {
        list.appendChild(emptyState("没有发现风险", "配置层面的检查都过了。"));
        return;
      }
      var aiOn = !!data.ai;
      items.forEach(function (item) {
        item.ai = aiOn;
        list.appendChild(securityRow(item));
      });
    }).catch(function (error) {
      list.textContent = "";
      list.appendChild(el("p", { class: "warn", text: "扫描失败：" + error.message }));
    });
  }

  function securityRow(item) {
    var row = el("div", { class: "sec-row" });
    var badge = el("span", {
      class: "sec-badge",
      text: SEV_LABEL[item.severity] || item.severity
    });
    badge.setAttribute("data-level", item.severity || "info");
    row.appendChild(badge);

    var main = el("div", { class: "sec-main" });
    main.appendChild(el("div", { class: "sec-title", text: item.title }));
    if (item.detail) { main.appendChild(el("div", { class: "sec-detail", text: item.detail })); }
    if (item.fix) { main.appendChild(el("div", { class: "sec-fix", text: "修法：" + item.fix })); }

    // 点开看原文 + 让 AI 翻译总结
    var more = el("div", { class: "sec-more", hidden: "hidden" });
    var original = el("p", { class: "sec-orig", text: item.detail || "（没有更多原文）" });
    more.appendChild(original);
    if (item.ai) {
      var aiBtn = el("button", { class: "btn sm ghost", type: "button", text: "翻译并总结" });
      aiBtn.addEventListener("click", function (event) {
        event.stopPropagation();
        runSummary(item, more, aiBtn);
      });
      more.appendChild(aiBtn);
    }
    main.appendChild(more);
    main.addEventListener("click", function () { more.hidden = !more.hidden; });
    row.appendChild(main);

    var actions = el("div", { class: "sec-actions" });
    if (item.fixable) {
      var button = el("button", { class: "btn sm", type: "button", text: "修复" });
      button.addEventListener("click", function (event) {
        event.stopPropagation();
        runSecurityFix(item.action, button);
      });
      actions.appendChild(button);
    }
    var done = el("button", { class: "btn sm ghost", type: "button", text: "已处理" });
    done.addEventListener("click", function (event) {
      event.stopPropagation();
      dismissFinding(item, done);
    });
    actions.appendChild(done);
    row.appendChild(actions);
    return row;
  }

  function runSecurityFix(action, button) {
    confirmDialog({
      title: "执行修复",
      lines: [
        action === "suid" ? "清掉 rootfs 里所有 setuid/setgid 位。" : "把沙盒 rootfs 的软件包升到最新。",
        "只动沙盒，不碰宿主内核。"
      ],
      confirmText: "执行"
    }).then(function (yes) {
      if (!yes) { return; }
      if (button) { button.disabled = true; }
      toast("正在修复…", "loading");
      var out = $("sec-out");
      out.hidden = false;
      out.textContent = "";
      api("/security/fix", { method: "POST", body: { action: action } })
        .then(function (data) {
          pollSecurityTask(data.task_id, out, button);
        })
        .catch(function (error) {
          toast("修复失败：" + error.message, "error");
          if (button) { button.disabled = false; }
        });
    });
  }

  function dismissFinding(item, button) {
    confirmDialog({
      title: "标记为已处理？",
      lines: [
        "「" + item.title + "」之后不再出现在风险列表里。",
        "适合这种情况：你已经手工升级过，或者确认这条和本机无关。",
        "想让它回来，删掉 plugin_data 里的 security_dismissed.json 即可。"
      ],
      confirmText: "标记"
    }).then(function (yes) {
      if (!yes) { return; }
      button.disabled = true;
      api("/security/dismiss", { method: "POST", body: { id: item.id } })
        .then(function () { toast("已标记"); loadSecurity(); })
        .catch(function (error) {
          button.disabled = false;
          toast("标记失败：" + error.message, "error");
        });
    });
  }

  function runSummary(item, more, button) {
    var text = (item.detail || "") + "\n" + (item.fix || "");
    var host = more.querySelector(".sec-summary");
    if (!host) {
      host = el("div", { class: "sec-summary" });
      more.appendChild(host);
    }
    host.textContent = "正在翻译…";
    button.disabled = true;
    api("/security/summary", { method: "POST", body: { id: item.id, text: text } })
      .then(function (data) {
        host.textContent = data.summary || "（没有内容）";
      })
      .catch(function (error) {
        host.textContent = "摘要失败：" + error.message;
      })
      .then(function () { button.disabled = false; });
  }

  function pollSecurityTask(taskId, out, button) {
    var tries = 0;
    var timer = window.setInterval(function () {
      tries += 1;
      api("/security/task", { query: { task_id: taskId } })
        .then(function (data) {
          var task = data.task || {};
          out.textContent = (task.lines || []).join("\n");
          out.scrollTop = out.scrollHeight;
          if (task.done) {
            window.clearInterval(timer);
            toast(task.ok ? "修复完成" : "修复失败，看下面的输出", task.ok ? "ok" : "error");
            if (button) { button.disabled = false; }
            loadSecurity();
          }
        })
        .catch(function (error) {
          window.clearInterval(timer);
          toast("查询进度失败：" + error.message, "error");
          if (button) { button.disabled = false; }
        });
      if (tries > 600) { window.clearInterval(timer); }
    }, 900);
  }

  $("sec-scan").addEventListener("click", function () { loadSecurity(); });

  $("sec-upgrade").addEventListener("click", function () {
    confirmDialog({
      title: "升级沙盒软件包？",
      lines: [
        "会在 rootfs 里执行 apt-get update && upgrade（或 apk upgrade）。",
        "只影响沙盒系统，宿主机不动。",
        "升级期间沙盒不可用，已装的第三方包可能被顺带更新。"
      ],
      confirmText: "升级"
    }).then(function (yes) {
      if (!yes) { return; }
      toast("正在升级…", "loading");
      var out = $("sec-out");
      out.hidden = false;
      out.textContent = "";
      api("/security/fix", { method: "POST", body: { action: "upgrade" } })
        .then(function (data) { pollSecurityTask(data.task_id, out, null); })
        .catch(function (error) { toast("升级失败：" + error.message, "error"); });
    });
  });

  // ---------- 执行 ----------

  $("e-run").addEventListener("click", function () {
    var user = $("e-user").value.trim();
    var command = $("e-cmd").value;
    if (!user) { toast("先填用户", "error"); return; }
    if (!command.trim()) { toast("命令不能为空", "error"); return; }
    var host = $("e-out");
    host.textContent = "";
    host.appendChild(el("p", { class: "muted", text: "执行中" }));
    api("/exec", { method: "POST", body: { user: user, command: command } })
      .then(function (data) {
        host.textContent = "";
        host.appendChild(terminalBlock(command, data));
      })
      .catch(function (error) { host.textContent = ""; toast(error.message, "error"); });
  });

  // ---------- 规则 ----------

  function makeTable(headers, rows) {
    var table = el("table", {});
    table.appendChild(el("thead", {}, [
      el("tr", {}, headers.map(function (header) {
        return el("th", { text: header });
      }))
    ]));
    var body = el("tbody", {});
    rows.forEach(function (cells) {
      var tr = el("tr", {});
      cells.forEach(function (cell) {
        var td = el("td", {});
        if (typeof cell === "string") { td.textContent = cell; }
        else if (cell) { td.appendChild(cell); }
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    table.appendChild(body);
    return table;
  }

  function actionButton(label, onClick) {
    var button = el("button", { class: "btn sm ghost", type: "button", text: label });
    button.addEventListener("click", onClick);
    return button;
  }

  function renderTrust(data) {
    var host = $("trust-table");
    host.textContent = "";
    if (!data.rules || !data.rules.length) {
      host.appendChild(emptyState(
        "还没有信任规则",
        "命中的命令会跳过内存和 CPU 看门狗。只加自己确认过的命令。"
      ));
      return;
    }
    var rows = data.rules.map(function (rule) {
      var actions = el("div", { class: "row" }, [
        actionButton("删除", function () { deleteTrust(rule.pattern); })
      ]);
      return [
        rule.pattern,
        rule.note || "-",
        rule.timeout_seconds ? rule.timeout_seconds + "s" : "全局",
        rule.bypass_memory ? "跳过" : "检查",
        rule.bypass_cpu ? "跳过" : "检查",
        rule.enabled ? "启用" : "停用",
        actions
      ];
    });
    host.appendChild(makeTable(
      ["命令", "备注", "超时", "内存", "CPU", "状态", "操作"],
      rows
    ));
  }

  function renderScope(data) {
    var host = $("scope-table");
    host.textContent = "";
    if (!data.rules || !data.rules.length) {
      host.appendChild(emptyState(
        "还没有场景规则",
        "没规则时按插件配置里的全局白名单走。"
      ));
      return;
    }
    var scopeNames = { group: "群聊", private: "私聊", all: "全部" };
    var rows = data.rules.map(function (rule) {
      var actions = el("div", { class: "row" }, [
        actionButton("删除", function () { deleteScope(rule.key); })
      ]);
      return [
        scopeNames[rule.scope] || rule.scope,
        rule.scope === "private" ? "-" : rule.target,
        rule.allow ? "允许" : "禁止",
        (rule.users && rule.users.length) ? rule.users.join("、") : "所有人",
        rule.note || "-",
        actions
      ];
    });
    host.appendChild(makeTable(
      ["场景", "目标", "结果", "限定用户", "备注", "操作"],
      rows
    ));
  }

  function deleteTrust(pattern) {
    confirmDialog({
      title: "删除信任规则",
      lines: [
        "删除后，命中 " + pattern + " 的命令会重新受内存和 CPU 看门狗约束。",
        "正在运行的命令不受影响。"
      ],
      confirmText: "删除",
      danger: true
    }).then(function (yes) {
      if (!yes) { return; }
      api("/trust/delete", { method: "POST", body: { pattern: pattern } })
        .then(function () { toast("已删除"); loadRules(); })
        .catch(function (error) { toast(error.message, "error"); });
    });
  }

  function deleteScope(key) {
    confirmDialog({
      title: "删除场景规则",
      lines: [
        "删除后，" + key + " 这个场景会回落到下一级规则（或全局白名单）。",
        "已经授权的用户不受影响。"
      ],
      confirmText: "删除",
      danger: true
    }).then(function (yes) {
      if (!yes) { return; }
      api("/scope/delete", { method: "POST", body: { key: key } })
        .then(function () { toast("已删除"); loadRules(); })
        .catch(function (error) { toast(error.message, "error"); });
    });
  }

  $("trust-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var payload = {
      pattern: $("t-pattern").value.trim(),
      note: $("t-note").value.trim(),
      timeout_seconds: Number($("t-timeout").value || 0),
      bypass_memory: $("t-mem").checked,
      bypass_cpu: $("t-cpu").checked,
      enabled: $("t-enabled").checked
    };
    if (!payload.pattern) { toast("命令通配符不能为空", "error"); return; }
    toast("正在保存信任规则…", "loading");
    api("/trust/save", { method: "POST", body: payload })
      .then(function () { toast("信任规则已保存"); $("trust-form").reset(); loadRules(); })
      .catch(function (error) { toast(error.message, "error"); });
  });

  $("scope-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var payload = {
      scope: $("s-scope").value,
      target: $("s-target").value.trim() || "*",
      allow: $("s-allow").value === "true",
      users: $("s-users").value.split(",").map(function (item) {
        return item.trim();
      }).filter(Boolean),
      note: $("s-note").value.trim()
    };
    toast("正在保存场景规则…", "loading");
    api("/scope/save", { method: "POST", body: payload })
      .then(function () { toast("场景规则已保存"); $("scope-form").reset(); loadRules(); })
      .catch(function (error) { toast(error.message, "error"); });
  });

  function loadRules() {
    api("/trust").then(renderTrust).catch(function (error) {
      toast(error.message, "error");
    });
    api("/scope").then(renderScope).catch(function (error) {
      toast(error.message, "error");
    });
  }

  // ---------- 日志 ----------

  function loadLogs() {
    var view = $("log-view");
    view.textContent = "读取中";
    api("/logs", { query: { lines: 200 } })
      .then(function (data) {
        view.textContent = (data.lines && data.lines.length)
          ? data.lines.join("\n")
          : "还没有日志。";
      })
      .catch(function (error) {
        view.textContent = "";
        toast(error.message, "error");
      });
  }

  $("log-refresh").addEventListener("click", loadLogs);

  $("log-cleanup").addEventListener("click", function () {
    api("/logs/cleanup", { method: "POST", body: {} })
      .then(function (data) {
        toast("已清理 " + (data.removed || []).length + " 个文件");
        loadLogs();
      })
      .catch(function (error) { toast(error.message, "error"); });
  });

  // ---------- 启动 ----------

  function loadOverview() {
    return api("/overview").then(renderOverview).catch(function (error) {
      $("backend-meta").textContent = error.message;
    });
  }

  $("refresh").addEventListener("click", function () {
    var button = $("refresh");
    button.disabled = true;
    var label = button.textContent;
    button.textContent = "刷新中";
    Promise.all([loadOverview(), loadUsers()]).then(function () {
      button.disabled = false;
      button.textContent = label;
      toast("已刷新");
    }).catch(function (error) {
      button.disabled = false;
      button.textContent = label;
      toast(error.message, "error");
    });
  });

  sandboxBanner();
  bindRipple();

  $("modal-ok").addEventListener("click", function () {
    if ($("modal-ok").disabled) { return; }
    closeModal(true);
  });
  $("modal-cancel").addEventListener("click", function () { closeModal(false); });
  $("modal").addEventListener("click", function (event) {
    if (event.target === $("modal")) { closeModal(false); }
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !$("modal").hidden) { closeModal(false); }
  });
  $("modal-gate-input").addEventListener("input", function () {
    var need = $("modal-gate-input").getAttribute("placeholder") || "";
    $("modal-ok").disabled = $("modal-gate-input").value.trim() !== need;
  });
  bootStatus("脚本已启动，正在连接…");
  moveIndicator("overview");
  resetDeckHeight();

  window.addEventListener("resize", function () {
    moveIndicator(currentTab() || "overview");
    resetDeckHeight();
  });

  splashHint("正在读取概览…");
  loadOverview().then(function () {
    bootStatus("已连接 · 自动刷新 10s");
    loadUsers();
    startAutoRefresh();
    splashHint("正在读取权限…");
    finishBoot();
    if (!UI.tour_seen) {
      window.setTimeout(function () { tourShow(0); }, 900);
    }
  });

  // 兜底：接口一直不回来也别让启动画面卡死
  window.setTimeout(finishBoot, 6000);

  var autoTimer = null;
  function currentTab() {
    var active = document.querySelector('.tab[aria-selected="true"]');
    return active ? active.dataset.tab : "";
  }

  function startAutoRefresh() {
    if (autoTimer) { return; }
    autoTimer = window.setInterval(function () {
      if (document.hidden) { return; }
      var tab = currentTab();
      if (tab === "overview") { loadOverview(); }
      if (tab === "setup" && !taskRunning) { loadSetup(false); }
      if (tab === "logs") { loadLogs(); }
    }, 10000);
  }
})();
