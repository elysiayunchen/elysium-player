/* ==========================================================================
   Elysium · 窗口壳行为层 (window-chrome.js)

   只负责窗口壳，不碰 app.js 的任何业务状态：
     · 探测平台（desktop / android / web），写 html[data-platform]
     · 探测 js_api 是否存在，决定窗口按钮是否可用
     · 绑定最小化 / 最大化 / 关闭，同步最大化与激活态
     · 主题切换时同步 DWM 暗色模式，并把 Acrylic 重申回来
     · 首屏 ready 标记（CSS 有定时淡出兜底，JS 挂了也不会白屏）

   全部窗口 API 都通过 window.pywebview.api 调用；浏览器调试 (--serve)
   和未来的 Android APK 没有这个对象，本脚本会静默降级，不抛错、不拦事件。

   宿主注入约定（后续 APK）：
     window.ELYSIUM_PLATFORM = 'android'   // 优于 UA 探测
     也可以用 ?platform=android 临时预览移动壳。
   ========================================================================== */
(function () {
  'use strict';

  var root = document.documentElement;
  var api = null;
  var qs = null;
  try { qs = new URLSearchParams(window.location.search); } catch (e) { qs = null; }
  var qget = function (k) { return qs ? qs.get(k) : null; };

  /* 无宿主验收开关：?wc=preview 时用 no-op api 显示完整窗口壳（含三按钮）。
     只在没有真 pywebview 时生效；生产桌面宿主走 pywebviewready 注入的真 api，
     APK / --serve 不带这个参数。material 保持 false，避免浏览器白底透成灰。 */
  if (qget('wc') === 'preview' && !window.pywebview) {
    window.pywebview = { api: {
      win_material_state: function () {
        return { material: false, acrylic: false, platform: 'desktop' };
      },
      win_minimize: function () {},
      win_toggle_maximize: function () {},
      win_close: function () {},
      win_is_maximized: function () { return false; },
      win_set_theme: function () {}
    } };
  }

  /* ---- 平台 ------------------------------------------------------------ */
  var platform = qget('platform') || window.ELYSIUM_PLATFORM || '';
  if (!platform) {
    if (window.pywebview) platform = 'desktop';
    else if (/Android/i.test(navigator.userAgent || '')) platform = 'android';
    else platform = 'web';
  }
  root.setAttribute('data-platform', platform);

  /* 一键回滚：?chrome=off 或宿主设置 data-chrome="off" */
  if (qget('chrome') === 'off') root.setAttribute('data-chrome', 'off');
  if (!root.getAttribute('data-chrome')) root.setAttribute('data-chrome', 'on');

  /* ---- 工具 ------------------------------------------------------------ */
  // pywebview 的 js_api 一般返回 Promise，老版本可能同步返回普通值；
  // Promise.resolve 两种情况都吃得住。
  function settle(value, ok, fail) {
    try {
      Promise.resolve(value).then(
        function (v) { if (ok) ok(v); },
        function () { if (fail) fail(); }
      );
    } catch (e) {
      if (fail) fail();
    }
  }
  function call(name) {
    if (!api || typeof api[name] !== 'function') return null;
    try { return api[name](); } catch (e) { return null; }
  }

  /* ---- 主题 → DWM 暗色 / Acrylic -------------------------------------- */
  function currentDark() {
    var t = root.getAttribute('data-theme');
    if (t === 'light') return false;
    if (t === 'dark') return true;
    try { return window.matchMedia('(prefers-color-scheme: dark)').matches; }
    catch (e) { return true; }
  }
  function syncTheme() {
    if (!api || typeof api.win_set_theme !== 'function') return;
    try { api.win_set_theme(currentDark()); } catch (e) {}
  }

  /* ---- 最大化状态 → 圆角归零 ------------------------------------------ */
  var maxTimer = null;
  function syncMaximized() {
    if (!api || typeof api.win_is_maximized !== 'function') return;
    var r = call('win_is_maximized');
    if (r === null) return;
    settle(r, function (v) {
      root.setAttribute('data-window-state', v ? 'maximized' : 'normal');
    });
  }
  function scheduleMax() {
    if (maxTimer) clearTimeout(maxTimer);
    maxTimer = setTimeout(syncMaximized, 120);
  }

  /* ---- 窗口按钮 -------------------------------------------------------- */
  var ACTS = { min: 'win_minimize', max: 'win_toggle_maximize', close: 'win_close' };
  function bindControls() {
    Object.keys(ACTS).forEach(function (key) {
      var el = document.querySelector('.wc-ctrl[data-act="' + key + '"]');
      if (!el || el.dataset.wcBound === '1') return;
      el.dataset.wcBound = '1';
      el.addEventListener('click', function (ev) {
        ev.stopPropagation();
        ev.preventDefault();
        call(ACTS[key]);
        if (key === 'max') scheduleMax();
      });
    });
  }

  /* ---- 激活态 ---------------------------------------------------------- */
  root.setAttribute('data-window-active', document.hasFocus() ? 'true' : 'false');
  window.addEventListener('focus', function () { root.setAttribute('data-window-active', 'true'); });
  window.addEventListener('blur', function () { root.setAttribute('data-window-active', 'false'); });
  window.addEventListener('resize', scheduleMax);

  /* ---- 主题变更观察 ---------------------------------------------------- */
  try {
    if (window.MutationObserver) {
      new MutationObserver(syncTheme).observe(root, {
        attributes: true, attributeFilter: ['data-theme']
      });
    }
  } catch (e) {}

  /* ---- API 就绪 -------------------------------------------------------- */
  function onApiReady() {
    try { api = (window.pywebview && window.pywebview.api) || null; } catch (e) { api = null; }
    if (!api) return;                       // web / android：按钮保持隐藏
    root.setAttribute('data-wc-api', 'on');
    bindControls();

    var st = call('win_material_state');
    if (st !== null) {
      settle(st, function (s) {
        root.setAttribute('data-wc-material', s && s.material ? 'on' : 'off');
      }, function () { root.setAttribute('data-wc-material', 'off'); });
    } else {
      root.setAttribute('data-wc-material', 'off');
    }
    syncTheme();
    syncMaximized();
  }

  window.addEventListener('pywebviewready', onApiReady);
  // 事件可能早于 defer 脚本执行，补一次直接探测
  if (window.pywebview && window.pywebview.api) onApiReady();

  /* ---- 首屏 ------------------------------------------------------------ */
  function markReady() { root.classList.add('wc-ready'); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', markReady);
  } else {
    markReady();
  }

  /* ---- 给 APK 宿主 / 后续 B 档调用的公开开关 ---------------------------- */
  window.ElysiumChrome = {
    version: '1.0.0',
    platform: platform,
    setPlatform: function (p) { root.setAttribute('data-platform', String(p)); },
    off: function () { root.setAttribute('data-chrome', 'off'); },
    on: function () { root.setAttribute('data-chrome', 'on'); },
    refresh: function () { onApiReady(); syncMaximized(); }
  };
})();
