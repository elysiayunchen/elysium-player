'use strict';

/* ==========================================================================
   Elysium 前端
   结构：工具 → 状态 → 主题 → 音质/媒体 → 播放 → 收藏 → 渲染组件
        → 视图 → 下载 → 设置 → 事件 → boot
   ========================================================================== */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// Android 宿主（Chaquopy APK）注入 window.ElysiumAndroid；UA 兜底 --serve 浏览器调试
const IS_ANDROID = (typeof window !== 'undefined' && window.ELYSIUM_PLATFORM === 'android') ||
  /Android/i.test(navigator.userAgent || '');
const icon = (n) => `<svg><use href="#i-${n}"/></svg>`;

const pad = (n) => (n < 10 ? '0' + n : '' + n);
const fmtTime = (sec) => {
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
};
const fmtSize = (n) => {
  n = Number(n) || 0;
  if (n >= 1073741824) return (n / 1073741824).toFixed(1) + ' GB';
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n >= 1024) return Math.round(n / 1024) + ' KB';
  return n + ' B';
};
const fmtSpeed = (n) => (n ? fmtSize(n) + '/s' : '');

async function post(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const d = await r.json().catch(() => ({}));
  if (d && d.ok === false && d.error) throw new Error(d.error);
  return d;
}
async function getJson(url) {
  const r = await fetch(url);
  return r.json();
}
/* 设置写入串行化：后发的必须后到。
   服务端是线程化的，多条连接上的写入可能反序到达，落盘值会回退到旧的那一次。
   （注：曾观察到「主题被写回旧值」，实测根因是重复实例 + 自测脚本残留点击，不是这里；
    这条串行化是防御性加固，避免多连接并发时的乱序写。） */
let _settingsChain = Promise.resolve();
function postSettings(payload) {
  _settingsChain = _settingsChain
    .then(() => post('/api/store', { op: 'settings', payload }))
    .catch(() => {});
  return _settingsChain;
}
let rpcSeq = 0;
async function rpc(fn, params) {
  const t0 = performance.now();
  const d = await post('/api/rpc', { fn, params });
  if (!d.ok) throw new Error(d.error || '请求失败');
  return d.data;
}

/* ------------------------------------------------------------------ 状态 */
/* 歌手页是「一次抓全、分批渲染」：几百行的表格一次性插进 DOM 会卡住主线程，
   探测也会被塞满。所以只渲前 ARTIST_PAGE 条，滚动到底再续。 */
const ARTIST_PAGE = 60;
const ARTIST_ALBUM_PAGE = 30;

const state = {
  settings: {},
  version: '',                     // 后端 /api/store 带来的 _version，设置页展示用
  update: null,                    // /api/update/status 的缓存：{latest, has_update, url, error…}
  updateDl: null,                  // /api/update/download 的进度：{state, url, path, got, total, speed, verified…}
  _updToastShown: false,           // 本次运行只弹一次新版本 toast，之后只靠红点
  favs: [],
  playlists: [],                    // 用户自建歌单 [{id,name,items:[...]}]
  recent: [],
  tasks: [],
  downloads: [],
  platform: 'kuwo',
  quality: 'best',
  route: { name: 'home' },
  stack: [],
  items: [],                       // 搜索结果（已跨源折叠，重复项在 alts 里）
  reco: null,                      // 本地推荐候选
  stats: null,                     // 我的统计（/api/stats/summary）
  statsLoading: false,
  taste: {},                       // 口味画像
  genreList: null,                 // 风格分类表（后端 /api/genres）
  styles: null,                    // 全风格卡片歌单
  recoLoading: false,
  recoError: false,
  styleLoading: false,
  styleError: false,
  recoExpanded: false,
  recentExpanded: false,
  q: { keyword: '', page: 1, total: 0 },
  list: [],                        // 当前渲染的曲目（播放上下文）
  playList: [],
  playIndex: -1,
  playing: null,
  currentQuality: null,
  resumePos: 0,                    // 启动恢复的待播位置（真正起播时用掉）
  queueSource: '',               // 队列来源（搜索结果 / 为你推荐 / 歌手页…）
  npTab: 'lyric',                // Now Playing 面板：lyric / queue
  mode: 'order',                   // order / one / shuffle
  tabs: { search: 'song', artist: 'song', library: 'song' },
  panelTab: 'queue',
  loading: false,
  local: { items: [], dir: '', roots: [], scanning: null, loaded: false, loading: false, error: '' },
  works: [],                       // K 歌作品（懒加载：进「我的作品」才拉）
  worksLoaded: false,
  worksLoading: false,
  artist: null,
  artistShown: ARTIST_PAGE,          // 歌手页曲目：先渲这么多，剩下的滚动再补
  artistAlbumShown: ARTIST_ALBUM_PAGE,
  artistSeq: 0,                      // 防抖：连点不同歌手时旧请求不许回填
  artistFilling: false,              // 后台补齐全量曲目中
  album: null,
  urlCache: new Map(),
  imp: null,                       // 导入预览（dry-run，确认后才落盘）
  impTimer: 0,
};

const MODES = [
  { key: 'order', icon: 'repeat', label: '顺序播放' },
  { key: 'one', icon: 'repeat-one', label: '单曲循环' },
  { key: 'shuffle', icon: 'shuffle', label: '随机播放' },
];
let lastSaveAt = 0;
const LS_LAST = 'elysium.lastTrack';

/* ------------------------------------------------------------------ 提示 */
function toast(msg, kind = '') {
  const box = $('#toasts');
  const el = document.createElement('div');
  el.className = 'toast' + (kind === 'err' ? ' err' : '');
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .25s,transform .25s';
    el.style.opacity = '0';
    el.style.transform = 'translateY(6px)';
    setTimeout(() => el.remove(), 260);
  }, 2600);
}

/* 主题化输入浮层，替代原生 prompt()（原生对话框不受主题控制、与整体风格断裂）。
   返回 Promise<string|null>：确定给字符串（未trim，调用方自行处理），取消/关层给 null。 */
let _askResolve = null;
function askText(title, value = '') {
  return new Promise((resolve) => {
    if (_askResolve) _askResolve(null);      // 上一次没答完的按取消处理
    _askResolve = resolve;
    $('#askTitle').textContent = title;
    const inp = $('#askInput');
    inp.value = value || '';
    $('#askLayer').hidden = false;
    inp.focus();
    inp.select();
  });
}
function _askDone(val) {
  const r = _askResolve;
  _askResolve = null;
  $('#askLayer').hidden = true;
  if (r) r(val);
}

/* ------------------------------------------------------------------ 主题 */
const mqLight = window.matchMedia('(prefers-color-scheme: light)');
function resolveTheme(mode) {
  if (mode === 'auto') return mqLight.matches ? 'light' : 'dark';
  return mode === 'light' ? 'light' : 'dark';
}
function applyTheme(mode) {
  const real = resolveTheme(mode || 'dark');
  document.documentElement.dataset.theme = real;
  const btn = $('#themeBtn');
  if (btn) btn.innerHTML = real === 'dark' ? icon('sun') : icon('moon');
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', real === 'dark' ? '#08080b' : '#fbfbfd');
}
mqLight.addEventListener('change', () => {
  if ((state.settings.theme || 'dark') === 'auto') applyTheme('auto');
});

/* ------------------------------------------------------ 来源 / 音质（实测） */
const SRC = {
  kuwo: { label: '酷我', cls: 'kuwo' },
  wyy: { label: '网易云', cls: 'wyy' },
  netease: { label: '网易云', cls: 'wyy' },
  migu: { label: '咪咕', cls: 'migu' },
  qq: { label: 'QQ音乐', cls: 'qq' },
  kugou: { label: '酷狗', cls: 'kugou' },
};
const srcOf = (it) => SRC[(it && it.platform) || '']
  || { label: String((it && it.platform) || '?').toUpperCase(), cls: 'x' };

/* 来源只做标注，不分成两个渠道。同一首在另一源也有时（alts），
   徽章变成可点的换源开关 —— 两个源都拿得到，但屏幕上永远只有一行。 */
function srcBadge(it, idx) {
  if (it.platform === 'local') return `<span class="src src-loc" title="本地文件 · 下载目录">本地</span>`;
  const s = srcOf(it);
  const alt = (it && it.alts && it.alts.length) ? it.alts[0] : null;
  if (alt && idx != null && idx >= 0) {
    const o = srcOf(alt);
    const more = it.alts.length > 1 ? ` 等 ${it.alts.length + 1} 个来源` : '';
    return `<button class="src src-${s.cls} swap" data-act="altswap" data-i="${idx}"
      title="同一首还有${o.label}版本${more} · 点击切换">${s.label}<i>⇄</i></button>`;
  }
  return `<span class="src src-${s.cls}" title="来源：${s.label}">${s.label}</span>`;
}

const qKey = (m) => (m.format === 'flac' ? 'flac' : String(m.bitrate));
const qName = (m) => (m.format === 'flac' ? 'FLAC' : m.bitrate + 'K');
const qChip = (m) => {
  const cls = m.format === 'flac' ? 'flac' : Number(m.bitrate) >= 320 ? 'hi' : 'lo';
  return `<span class="chip ${cls}">${qName(m)}</span>`;
};
const _br = (m) => Number(m.bitrate) || 0;

/* 实测探针：plat:id -> {valid,fmt,label,kbps,size,mismatch} */
const PROBE = new Map();
const pk = (it) => `${it.platform}:${it.id}`;

/* 列表里的音质格子：只认实测结果，上游声称不算数 */
function qualityChip(it, idx) {
  if (it.platform === 'local') {            // 本地文件：mutagen 读出的就是真实值
    const fmt = (it.format === 'flac' ? 'FLAC' : String(it.format || '').toUpperCase());
    const br = it.bitrate ? ' ' + it.bitrate + 'k' : '';
    const size = it.size ? `<span class="mono">${fmtSize(it.size)}</span>` : '';
    return `<span class="chip ${it.format === 'flac' ? 'flac' : (it.bitrate || 0) >= 192 ? 'hi' : 'lo'}"
      title="本地文件 · ${esc(it.id)}">${esc(fmt || '音轨')}${br}</span>${size}`;
  }
  const p = PROBE.get(pk(it));
  if (!p) {
    const q = bestQuality(it);
    const tip = q ? `正在实测真实格式与码率（上游声称 ${q.format === 'flac' ? 'FLAC' : q.bitrate + 'K'}）` : '正在实测';
    return `<span class="chip probing" title="${esc(tip)}">检测中</span>`;
  }
  /* 探测没跑出结果（请求失败 / 上游无响应）：落成静态「待测」，不再永远 pulse */
  if (p.unresolved) {
    return `<span class="chip wait" title="${esc(p.reason || '实测未完成')}">待测</span>`;
  }
  if (!p.valid) {
    const btn = idx == null ? ''
      : `<button class="lnk" data-act="swap" data-i="${idx}" title="${esc(p.reason || '链接不可用')}">换源</button>`;
    return `<span class="chip dead" title="${esc(p.reason || '链接不可用')}">失效</span>${btn}`;
  }
  let label = '音轨';
  let cls = 'hi';
  if (p.fmt === 'flac') { label = 'FLAC'; cls = 'flac'; }
  else if (p.fmt === 'mp3') { label = 'MP3'; cls = p.kbps >= 192 ? 'hi' : 'lo'; }
  else { label = String(p.label || p.fmt || '').toUpperCase() || '音轨'; cls = 'hi'; }
  const br = p.kbps ? ' ' + p.kbps + 'k' : '';
  const flag = p.mismatch ? ' flag' : '';
  const tip = p.mismatch ? '上游标称无损，实测并非 FLAC' : '';
  const size = p.size ? `<span class="mono">${fmtSize(p.size)}</span>` : '';
  return `<span class="chip ${cls}${flag}"${tip ? ` title="${esc(tip)}"` : ''}>${label}${br}</span>${size}`;
}

let _probeSeq = 0;
/* 一批 12 条：服务端并发 8，基本两轮就回来。批越小出结果越早，
   整列不会一次性卡在「检测中」等最后一条慢的。 */
const PROBE_CHUNK = 12;

/* 回填已探测的格子：只改对应单元格，不重排。
   列表行有 data-row（下标指向 state.items），推荐卡没有 → 不给它「换源」按钮，
   否则那个下标会被误当成 state.items 的下标，换成另一首歌。 */
function paintProbeCells(items) {
  const byKey = new Map();
  (items || []).forEach((it) => byKey.set(pk(it), it));
  document.querySelectorAll('[data-pk]').forEach((row) => {
    const k = row.getAttribute('data-pk');
    const p = PROBE.get(k);
    if (!p) return;
    const it = byKey.get(k);
    const cell = row.querySelector('.tr-spec, .rspec');
    const idxAttr = row.getAttribute('data-row');
    if (it && cell) cell.innerHTML = qualityChip(it, idxAttr == null ? null : Number(idxAttr));
    row.classList.toggle('dead', !p.valid && !p.unresolved);
  });
}

/* 探测结果条目带 at 时间戳：失效/超时的结果 10 分钟后允许重测，
   不然网络恢复后那一列永远停在「失效」。 */
const PROBE_TTL = 10 * 60 * 1000;

/* 渲染完一批就并发探一遍，回来只改对应格子，不重排。
   分片串行：前一片回来先回填，尾部的条目不会永远停在骨架屏。 */
async function probeRows(items) {
  const all = (items || []).filter((it) => it && it.id && it.platform && it.platform !== 'local');
  if (!all.length) return;
  const seq = ++_probeSeq;
  const list = all.filter((it) => {
    const hit = PROBE.get(pk(it));
    return !hit || Date.now() - (hit.at || 0) > PROBE_TTL;
  });
  if (!list.length) return;                       // 全部已有实测结果，不重复探测
  for (let off = 0; off < list.length; off += PROBE_CHUNK) {
    if (seq !== _probeSeq) return;
    const chunk = list.slice(off, off + PROBE_CHUNK);
    const payload = chunk.map((it) => {
      const q = bestQuality(it);
      return Object.assign({}, it, { _probeq: q ? { format: q.format, bitrate: q.bitrate } : {} });
    });
    try {
      const res = await post('/api/probe', { items: payload, limit: PROBE_CHUNK });
      if (seq !== _probeSeq) return;
      Object.entries(res.data || {}).forEach(([k, v]) =>
        PROBE.set(k, Object.assign({ at: Date.now() }, v)));
      chunk.forEach((it) => {
        const k = pk(it);
        if (!PROBE.has(k)) PROBE.set(k, { at: Date.now(), unresolved: true, reason: '探测未返回结果' });
      });
    } catch (e) {
      if (seq !== _probeSeq) return;
      chunk.forEach((it) => PROBE.set(pk(it), { at: Date.now(), unresolved: true, reason: '探测请求失败' }));
    }
    paintProbeCells(chunk);
  }
}

/* 在线播放音质档位排序：auto=mp3 优先（起播快/省流量）、best/flac=无损优先、
   320/128=指定档优先；数组里保留其余档位用于失败自动降级。 */
function qualityRank(item, want) {
  const minfo = item.minfo || [];
  const mp3 = minfo.filter((m) => m.format === 'mp3').sort((a, b) => _br(b) - _br(a));
  const flac = minfo.filter((m) => m.format === 'flac').sort((a, b) => _br(b) - _br(a));
  const w = want || state.settings.play_quality || 'auto';
  if (w === 'best' || w === 'flac') return [...flac, ...mp3];
  if (w === '320' || w === '128') {
    const n = Number(w);
    return [...mp3.filter((m) => _br(m) === n), ...mp3.filter((m) => _br(m) !== n), ...flac];
  }
  return [...mp3, ...flac];
}

function bestFor(item, mode) {
  const minfo = item.minfo || [];
  if (!minfo.length) return null;
  if (mode === 'play') return qualityRank(item)[0] || minfo[0];
  const flac = minfo.filter((m) => m.format === 'flac').sort((a, b) => _br(b) - _br(a));
  const mp3 = minfo.filter((m) => m.format === 'mp3').sort((a, b) => _br(b) - _br(a));
  const want = state.quality;                    // 下载音质，与在线播放分开
  if (want === '320') return mp3.find((m) => _br(m) === 320) || mp3[0] || flac[0];
  if (want === '128') return mp3.find((m) => _br(m) === 128) || mp3[mp3.length - 1] || flac[0];
  return flac[0] || mp3[0] || minfo[0];
}

function playCandidates(item) {
  if (item && item.platform === 'work') return [{ format: 'wav', bitrate: 0 }];   // K 歌作品：wav 一种
  return qualityRank(item);
}

const songKey = (it) => `${it.platform}_${it.id}`;

/* 歌单条目落盘时只存了展示字段（无 minfo/time/sign），直接播/下会报
   「没有可用音质」。回源取一次完整条目补全，再写回原数组，下次不必重取。 */
async function ensureMinfo(item) {
  if (!item || !item.platform || !item.id) return item;
  if (item.platform === 'work') return item;   // 作品没有在线音质可补
  if ((item.minfo || []).length) return item;
  try {
    const d = await rpc('song', { platform: item.platform, id: item.id });
    const full = ((d && d.list) || [])[0];
    if (full && (full.minfo || []).length) {
      const merged = Object.assign({}, item, full, { name: item.name || full.name });
      if (Array.isArray(state.list)) {
        const i = state.list.indexOf(item);
        if (i >= 0) state.list[i] = merged;
      }
      return merged;
    }
  } catch (e) { /* 回源失败按原样返回，播放侧再报错 */ }
  return item;
}

async function fetchUrl(item, q) {
  const key = `${item.platform}:${item.id}:${q.format}:${q.bitrate}`;
  const hit = state.urlCache.get(key);
  if (hit && Date.now() - hit.at < 25 * 60 * 1000) return hit.url;
  const data = await rpc('geturl', {
    platform: item.platform, id: item.id,
    format: q.format, bitrate: q.bitrate,
    time: item.time, sign: item.sign,
  });
  const url = data && data.url;
  if (!url) throw new Error('没有拿到播放链接');
  state.urlCache.set(key, { url, at: Date.now() });
  return url;
}

const mediaKey = (item, q) => `${item.platform}_${item.id}_${q.bitrate}_${q.format}`;
function mediaSrc(item, q) {
  if (item.platform === 'work') {           // K 歌作品：本机 works/ 目录里的 wav
    return '/api/works/file?id=' + encodeURIComponent(item.id);
  }
  if (item.platform === 'local') {          // 本地文件直连磁盘，不走在线代理与缓存
    return '/api/local/file?p=' + encodeURIComponent(item.id);
  }
  const p = new URLSearchParams({
    key: mediaKey(item, q), ext: q.format, platform: item.platform,
    sid: item.id, fmt: q.format, br: q.bitrate, name: item.name || '',
  });
  if (item.time) p.set('time', item.time);
  if (item.sign) p.set('sign', item.sign);
  return '/api/media?' + p.toString();
}

/* ------------------------------------------------------------------ 收藏 */
const fKind = (f) => f.kind || 'song';
const isFav = (kind, id, platform) => state.favs.some(
  (f) => fKind(f) === kind && String(f.id) === String(id)
    && (!platform || f.platform === platform));
const favsOf = (kind) => state.favs.filter((f) => fKind(f) === kind);

async function toggleFav(kind, payload) {
  try {
    const data = await post('/api/store', { op: 'fav_toggle', payload: { kind, ...payload } });
    state.favs = (data.data && data.data.favorites) || [];
    renderSide();
    render();
    return data.data ? data.data.active : false;
  } catch (e) {
    toast(e.message || '收藏失败', 'err');
    return false;
  }
}

/* ------------------------------------------------------------------ 歌单 */
// 与收藏完全独立：加歌到歌单不写 favorites，反之亦然。
async function plOp(op, payload) {
  try {
    const data = await post('/api/store', { op, payload });
    state.playlists = (data.data && data.data.playlists) || [];
    renderSide();
    render();
    return true;
  } catch (e) {
    toast(e.message || '歌单操作失败', 'err');
    return false;
  }
}

const plById = (id) => state.playlists.find((p) => p.id === id) || null;

async function plCreate(name) {
  const n = String(name || '').trim();
  if (!n) return false;
  return plOp('playlist_create', { name: n });
}

async function plAddSongs(plid, songs) {
  const list = (Array.isArray(songs) ? songs : [songs]).filter((s) => s && s.id && s.platform);
  if (!list.length) return false;
  const before = (plById(plid) || { items: [] }).items.length;
  const ok = await plOp('playlist_add', { id: plid, items: list });
  if (!ok) return false;
  const after = (plById(plid) || { items: [] }).items.length;
  return after - before;                    // 返回实际新增数
}

/* ------------------------------------------------------------------ 导入歌单 */
/* 三桶一律「声明 + 用户决定」：L1 默认纳入，L2/L3 默认不选、绝不静默丢弃。
   预览是 dry-run，只有点「导入所选」才写 data.json。 */
const IMPORT_ACCEPT = '.kgl,.txt,.csv,.m3u8,.m3u';
const impSrcLabel = (p) => ({ kuwo: '酷我', wyy: '网易云' }[p] || p || '未知');

function impReset() {
  if (state.impTimer) { clearInterval(state.impTimer); state.impTimer = 0; }
  const prev = state.imp && state.imp.enhance;
  state.imp = {
    phase: 'input', task: '', done: 0, total: 0, result: null,
    filename: '', fileText: '',
    enhance: prev ? { ...prev } : { enabled: false, token: '', userid: '' },
    manual: {}, name: '', target: '',
  };
}

function openImport() {
  $('#npDrawer').hidden = true;
  $('#panelDrawer').hidden = true;
  $('#settingsDrawer').hidden = true;
  $('#importDrawer').hidden = false;
  if (!state.imp) impReset();
  renderImport();
}

function impInputHtml() {
  const e = state.imp.enhance;
  return `<div class="imp-pane">
    <div class="imp-lead">粘贴歌单链接 / 分享文本，或选择客户端导出的歌单文件</div>
    <textarea class="imp-ta" id="impText" placeholder="网易云 / QQ音乐 / 酷狗歌单链接、分享文案，或&#10;邓紫棋 - 泡沫&#10;孙燕姿 - 我不难过">${esc(state.imp.fileText || '')}</textarea>
    <div class="imp-row">
      <button class="btn sm" data-imp="file">选择文件</button>
      <input type="file" id="impFile" accept="${IMPORT_ACCEPT}" data-imp-field="file" hidden>
      <span class="imp-a" id="impFileName">${esc(state.imp.filename || '未选择文件')}</span>
    </div>
    <div class="imp-hint">主通道：酷狗客户端「播放列表 → 菜单 → 导出列表」得到的 .kgl，或 .txt / .csv / .m3u8 / 纯文本「歌名 - 歌手」。零登录、零联网取数。<br>次通道：三家歌单链接；酷狗新版分享链接免登录只给前 10 首，会明确提示。</div>
    <button class="btn primary" data-imp="start">解析并预览</button>

    <div class="imp-sec">
      <h4>接口自检与更新</h4>
      <div class="imp-ver" id="impVer">规则版本加载中…</div>
      <div class="imp-row">
        <button class="btn sm" data-imp="health">检查三家接口</button>
        <button class="btn sm" data-imp="update">检查更新</button>
      </div>
      <div id="impHealth"></div>
    </div>

    <div class="imp-sec">
      <h4>酷狗完整歌单 · 可选增强</h4>
      <label class="imp-a"><input type="checkbox" id="impEnhanceOn" data-imp-field="enhance" ${e.enabled ? 'checked' : ''}> 启用（默认关闭，仅本次请求带凭据）</label>
      <div class="imp-row">
        <input class="imp-in" id="impToken" data-imp-field="token" placeholder="token" value="${esc(e.token)}" style="flex:1">
        <input class="imp-in" id="impUserid" data-imp-field="userid" placeholder="userid" value="${esc(e.userid)}" style="width:130px">
      </div>
      <div class="imp-hint">新版酷狗分享链接免登录只给 10 首预览；填入凭据后可取全量。凭据不写入 data.json、不上传别处。最稳的仍是本地 .kgl 导出文件。</div>
    </div>
  </div>`;
}

function impProgressHtml() {
  const s = state.imp;
  const pct = s.total ? Math.round(s.done / s.total * 100) : 0;
  return `<div class="imp-pane">
    <div class="imp-progress" id="impProgText">正在逐条核对歌名与作者… ${s.done}/${s.total || '?'}</div>
    <div class="imp-bar"><i id="impProgBar" style="width:${pct}%"></i></div>
    <button class="btn sm" data-imp="cancel">取消</button>
  </div>`;
}

function impCandHtml(c, attrs, on) {
  return `<div class="imp-cand${on ? ' on' : ''}" ${attrs}>
    <span>${esc(c.name || '')}</span>
    <span class="imp-a">${esc(c.artist || '')}</span>
    <span class="mono">${esc(impSrcLabel(c.platform))}</span>
  </div>`;
}

function impRowHtml(x, tier) {
  const cands = x.candidates || [];
  let extra = '';
  if (tier === 'l2' && cands.length) {
    extra = `<div class="imp-cands">${cands.map((c, i) => impCandHtml(c,
      `data-imp="cand" data-tier="l2" data-i="${x.index}" data-c="${i}"`,
      i === x.selected)).join('')}</div>`;
  }
  if (tier === 'l3') {
    const m = state.imp.manual[x.index] || {};
    extra = `<div class="imp-manual">
      <input class="imp-in" data-imp-field="mq" data-i="${x.index}" placeholder="手动搜索歌名" value="${esc(m.q || '')}">
      <button class="btn sm" data-imp="manual-search" data-i="${x.index}">查找</button>
    </div>`;
    if ((m.list || []).length) {
      extra += `<div class="imp-cands">${m.list.map((c, i) => impCandHtml(c,
        `data-imp="manual-pick" data-i="${x.index}" data-c="${i}"`,
        x.item && x.item.id === c.id && x.item.platform === c.platform)).join('')}</div>`;
    }
  }
  return `<div class="imp-item${x.chosen ? ' pick' : ''}">
    <div class="imp-item-top">
      <input type="checkbox" data-imp-field="pick" data-tier="${tier}" data-i="${x.index}" ${x.chosen ? 'checked' : ''}>
      <div class="imp-item-main">
        <div class="imp-n">${esc(x.name || x.raw || '（未解析出行）')}</div>
        <div class="imp-a">${esc(x.artist || '作者未知')}${x.raw && x.raw !== x.name ? ' · 原文 ' + esc(x.raw) : ''}</div>
        ${extra}
      </div>
    </div>
  </div>`;
}

function impResultHtml() {
  const r = state.imp.result;
  const b = r.buckets || {};
  const src = r.source || {};
  const n1 = (b.l1 || []).length, n2 = (b.l2 || []).length, n3 = (b.l3 || []).length;
  const chosen = ['l1', 'l2', 'l3'].reduce((n, t) =>
    n + (b[t] || []).filter((x) => x.chosen).length, 0);
  const targets = `<option value="">新建歌单</option>` + state.playlists.map((p) =>
    `<option value="${esc(p.id)}" ${state.imp.target === p.id ? 'selected' : ''}>合并到「${esc(p.name)}」</option>`).join('');
  const tier = (t, title, note) => (b[t] || []).length
    ? `<div class="imp-tier">${title} <em>${note} · ${(b[t] || []).length} 首</em></div>` +
      (b[t] || []).map((x) => impRowHtml(x, t)).join('')
    : '';
  return `<div class="imp-pane">
    <div class="imp-sum">
      <span>来源 <b>${esc(src.label || '本地')}</b></span>
      <span>共 <b>${n1 + n2 + n3}</b> 首</span>
      <span>精密匹配 <b>${n1}</b></span>
      <span>待确认 <b>${n2}</b></span>
      <span>未找到 <b>${n3}</b></span>
    </div>
    ${src.partial || src.note ? `<div class="imp-warn">${esc(src.note || '该来源只返回了部分曲目，建议改用本地导出文件导入完整歌单。')}</div>` : ''}
    ${r.order === 'swap' ? '<div class="imp-hint">已按「歌名 - 歌手」方向解析（自动判定）。</div>' : ''}
    <div class="imp-row">
      <input class="imp-in" id="impName" placeholder="歌单名称" value="${esc(state.imp.name)}" style="flex:1">
      <select class="imp-in" id="impTarget" style="width:190px">${targets}</select>
    </div>
    ${tier('l1', '① 精密匹配', '歌名与作者都严格对上，默认纳入')}
    ${tier('l2', '② 歌名对上、作者对不上', '默认不选，勾选后才加入，绝不擅自合并')}
    ${tier('l3', '③ 本地音源未找到', '默认不选，可手动搜索指定，绝不静默丢弃')}
    <div class="imp-foot">
      <span class="grow">已勾选 <b>${chosen}</b> 首</span>
      <button class="btn sm" data-imp="reset">重新导入</button>
      <button class="btn primary" data-imp="commit">导入所选</button>
    </div>
  </div>`;
}

function renderImport() {
  const box = $('#importBody');
  if (!box) return;
  if (!state.imp) impReset();
  const s = state.imp;
  // 重绘前把用户已编辑的字段收回来：点候选 / 勾选都会整块重建 DOM，
  // 不回收的话刚输入的歌单名、凭据会悄悄回到旧值。
  const nm = box.querySelector('#impName'); if (nm) s.name = nm.value;
  const tg = box.querySelector('#impTarget'); if (tg) s.target = tg.value;
  const tk = box.querySelector('#impToken'); if (tk) s.enhance.token = tk.value.trim();
  const uid = box.querySelector('#impUserid'); if (uid) s.enhance.userid = uid.value.trim();
  const en = box.querySelector('#impEnhanceOn'); if (en) s.enhance.enabled = en.checked;
  box.innerHTML = s.phase === 'running' ? impProgressHtml()
    : s.phase === 'result' && s.result ? impResultHtml() : impInputHtml();
  if (s.phase === 'input') loadImportVersion();
}

function renderImportProgress() {
  const s = state.imp;
  const t = $('#impProgText'), bar = $('#impProgBar');
  if (t) t.textContent = `正在逐条核对歌名与作者… ${s.done}/${s.total || '?'}`;
  if (bar) bar.style.width = (s.total ? Math.round(s.done / s.total * 100) : 0) + '%';
}

async function loadImportVersion() {
  try {
    const d = await getJson('/api/update/check');
    const u = d.data || {};
    const el = $('#impVer');
    if (el) el.textContent = `规则版本 ${u.rules_version || '?'} · 客户端 v${u.client_version || '?'}` +
      (u.degraded && u.degraded.length ? ' · 规则文件降级' : '');
  } catch (e) { /* 版本信息只是提示，失败不打断导入 */ }
}

/* ------------------------------------------------------------------ 更新追踪
   后台线程每 6h 查一次 GitHub Releases，这里只轮询它缓存的结论（秒回、不打 GitHub）。
   提醒三层：左侧「设置」红点 → 启动 toast → 设置页「更新」区块；绝不自动下载。 */
function updDismissed() {
  return state.settings.update_dismissed || '';
}

function updTipText() {
  const u = state.update;
  const cur = state.version ? `当前 v${state.version}` : '';
  if (!u) return `还没检查过${cur ? ' · ' + cur : ''}`;
  if (u.has_update) return `发现新版本 ${u.latest}${cur ? ' · ' + cur : ''}，去发布页下载`;
  if (u.error) return `上次检查失败（网络不可达）${cur ? ' · ' + cur : ''}`;
  if (u.latest) return `已是最新${cur ? ' · ' + cur : ''}`;
  return `还没检查过${cur ? ' · ' + cur : ''}`;
}

function paintUpdateBadge() {
  const u = state.update;
  const on = !!(u && u.has_update && u.latest !== updDismissed());
  document.querySelectorAll('[data-nav="settings"]').forEach((el) => {
    el.classList.toggle('has-update', on);
  });
}

function paintUpdateSection() {
  const tip = $('#updTip');
  if (tip) tip.textContent = updTipText();
  const show = !!(state.update && state.update.has_update);
  const open = $('#updOpen');
  if (open) open.hidden = !show;
  const dismiss = $('#updDismiss');
  if (dismiss) dismiss.hidden = !(show && state.update.latest !== updDismissed());
  paintUpdateAssets();
  paintUpdateBadge();     // 忽略/发现新版本都可能改红点，统一在这里兜一遍
}

/* 直链 / 镜像行：打不开 GitHub 的用户也能拿链接去任意下载器。
   「下载」走应用内通道：后端流式下载 + SHA-256 强制校验，文件落到下载目录，
   替换 / 运行由用户手动完成（不碰运行中的 exe，砖不了机）。 */
function dlLabel(url) {
  const d = state.updateDl;
  if (d && d.url === url) {
    if (d.state === 'running') {
      const pct = d.total ? Math.min(100, Math.round(d.got / d.total * 100)) : null;
      return pct === null ? '下载中…' : `${pct}%`;
    }
    if (d.state === 'done') return '重新下载';
    if (d.state === 'error') return '重试';
  }
  return '下载';
}

function paintUpdateAssets() {
  const box = $('#updAssets');
  if (!box) return;
  const u = state.update;
  const assets = (u && u.has_update ? (u.assets || []) : []);
  const dl = state.updateDl;
  const rows = assets.map((a) => {
    const urls = [{ label: 'GitHub 直链', url: a.url }].concat(
      (a.mirrors || []).map((m, i) => ({ label: `镜像 ${i + 1}`, url: m })));
    const sha = a.sha256
      ? `<p class="tip">SHA-256 前 16 位 <code>${esc(String(a.sha256).slice(0, 16))}…</code>，下载完成后自动比对</p>`
      : '<p class="tip">远端未提供 SHA-256，请留意下载来源</p>';
    const prog = dl && dl.state === 'running'
      ? `<p class="tip">${fmtSize(dl.got)}${dl.total ? ' / ' + fmtSize(dl.total) : ''} · ${fmtSpeed(dl.speed) || '…'}</p>`
      : '';
    const doneTip = (dl && dl.state === 'done' && dl.path) ? `
      <p class="tip">${dl.verified ? 'SHA-256 已通过 · ' : ''}${esc(dl.path)}</p>
      ${IS_ANDROID
        ? `<div class="field-acts"><button class="btn sm" data-act="upd-install" data-path="${esc(dl.path)}">安装更新</button></div>`
        : '<p class="tip">手动运行即可完成更新</p>'}` : '';
    const errTip = dl && dl.state === 'error'
      ? `<p class="tip">下载失败：${esc(dl.error || '')}（可换镜像再试）</p>`
      : '';
    const lines = urls.map((x) => `
      <div class="field-acts" style="margin-top:4px">
        <button class="btn sm" data-act="upd-open-url" data-url="${esc(x.url)}">${esc(x.label)}</button>
        <button class="btn sm ghost" data-act="upd-dl" data-url="${esc(x.url)}" data-sha="${esc(a.sha256 || '')}"${dl && dl.state === 'running' ? ' disabled' : ''}>${dlLabel(x.url)}</button>
        <button class="btn sm ghost" data-act="upd-copy" data-url="${esc(x.url)}">复制</button>
      </div>`).join('');
    return `
      <div class="field" style="display:block">
        <div><label>${esc(a.name || '下载')}</label>${sha}${prog}${doneTip}${errTip}</div>
        ${lines}
      </div>`;
  });
  const none = u && u.has_update && !assets.length
    ? '<p class="tip">远端未提供直链，请从发布页下载</p>' : '';
  box.innerHTML = rows.join('') + none;
}

function openExternal(url) {
  const u = String(url || '');
  if (!/^https?:\/\//i.test(u)) return;
  if (IS_ANDROID && window.ElysiumAndroid && window.ElysiumAndroid.openUrl) {
    window.ElysiumAndroid.openUrl(u);      // Android WebView 里 window.open 不可靠，走原生 Intent
  } else if (window.pywebview && window.pywebview.api && window.pywebview.api.open_url) {
    window.pywebview.api.open_url(u);      // WebView2 里交给系统默认浏览器
  } else {
    window.open(u, '_blank');
  }
}

async function copyText(t) {
  try {
    await navigator.clipboard.writeText(t);
    return true;
  } catch (e) {
    // WebView2 旧内核 / 非安全上下文兜底：隐藏 textarea + execCommand
    const ta = document.createElement('textarea');
    ta.value = t;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
    ta.remove();
    return ok;
  }
}

let updDlTimer = null;
function pollUpdateDl() {
  clearTimeout(updDlTimer);
  getJson('/api/update/download').then((d) => {
    const prev = state.updateDl;
    state.updateDl = d.data || { state: 'idle' };
    if (prev && prev.state === 'running') {          // 只在状态跃迁时弹，刷新页面不重复打扰
      if (state.updateDl.state === 'done') {
        toast(IS_ANDROID ? '新版本已下载完成，去「更新」里点「安装更新」'
                         : '新版本已下载到下载目录，手动运行即可完成更新');
      }
      if (state.updateDl.state === 'error') toast('下载失败：' + (state.updateDl.error || ''), 'err');
    }
    paintUpdateAssets();
    if (state.updateDl.state === 'running') updDlTimer = setTimeout(pollUpdateDl, 1000);
  }).catch(() => {
    if (state.updateDl && state.updateDl.state === 'running') {
      updDlTimer = setTimeout(pollUpdateDl, 3000);   // 网络抖动重试，别把进度条弄丢
    }
  });
}

async function loadUpdateStatus(refresh) {
  try {
    const d = await getJson('/api/update/status' + (refresh ? '?refresh=1' : ''));
    state.update = d.data || null;
    paintUpdateBadge();
    paintUpdateSection();
    const u = state.update;
    if (u && u.has_update && !state._updToastShown && u.latest !== updDismissed()) {
      state._updToastShown = true;
      toast(`发现新版本 ${u.latest}，设置页可查看`);
    }
  } catch (e) { /* 更新提示失败不打扰主流程 */ }
}

async function startImportPreview() {
  const ta = $('#impText');
  const text = String((ta && ta.value) || '').trim();
  if (!text) { toast('先粘贴内容或选一个文件', 'err'); return; }
  const hasUrl = /https?:\/\//i.test(text);
  const e = state.imp.enhance;
  state.imp.fileText = text;
  state.imp.phase = 'running';
  state.imp.done = 0; state.imp.total = 0; state.imp.task = '';
  renderImport();
  try {
    const d = await post('/api/import/preview', {
      text, filename: hasUrl ? '' : state.imp.filename,
      enhance: { enabled: !!e.enabled, token: e.token, userid: e.userid },
    });
    state.imp.task = d.data.task_id;
    pollImport(d.data.task_id);
  } catch (err) {
    state.imp.phase = 'input'; renderImport();
    toast(err.message || '解析失败', 'err');
  }
}

function pollImport(tid) {
  if (state.impTimer) clearInterval(state.impTimer);
  state.impTimer = setInterval(async () => {
    let d;
    try { d = await getJson('/api/import/status?id=' + encodeURIComponent(tid)); }
    catch (e) { return; }
    const s = d.data || {};
    state.imp.done = s.done || 0;
    state.imp.total = s.total || 0;
    if (s.state === 'running') { renderImportProgress(); return; }
    clearInterval(state.impTimer); state.impTimer = 0;
    if (s.state === 'done') {
      state.imp.result = s.result;
      state.imp.phase = 'result';
      const src = s.result.source || {};
      state.imp.name = src.name || (state.imp.filename
        ? state.imp.filename.replace(/\.[^.]+$/, '') : '') || '导入歌单';
      renderImport();
    } else if (s.state === 'cancelled') {
      state.imp.phase = 'input'; renderImport(); toast('已取消');
    } else {
      state.imp.phase = 'input'; renderImport();
      toast(s.error || '解析失败', 'err');
    }
  }, 600);
}

function impFind(tier, i) {
  const b = (state.imp.result && state.imp.result.buckets) || {};
  return (b[tier] || []).find((x) => String(x.index) === String(i)) || null;
}

async function commitImport() {
  const r = state.imp.result;
  if (!r) return;
  const items = [];
  let skipped = 0;
  for (const t of ['l1', 'l2', 'l3']) {
    for (const x of (r.buckets[t] || [])) {
      if (!x.chosen) continue;
      const it = x.item || (x.candidates && x.candidates[x.selected]) || null;
      if (it && it.platform && it.id) items.push(it);
      else skipped++;
    }
  }
  if (!items.length) { toast('没有勾选任何可导入的曲目', 'err'); return; }
  const name = String(($('#impName') || {}).value || state.imp.name || '导入歌单').trim();
  const target = ($('#impTarget') || {}).value || '';
  const src = r.source || {};
  try {
    const d = await post('/api/import/commit', {
      name, target_id: target, items,
      source: {
        mode: src.mode, platform: src.platform, label: src.label,
        filename: src.filename, name: src.name, partial: src.partial,
        note: src.note, rules_version: r.rules_version,
      },
    });
    const st = await getJson('/api/store');
    state.playlists = st.playlists || [];
    renderSide(); render();
    $('#importDrawer').hidden = true;
    go({ name: 'playlist', id: d.data.id });
    toast(`已导入 ${d.data.added} 首` + (skipped ? ` · ${skipped} 条未选定已跳过` : ''));
    impReset();
  } catch (err) {
    toast(err.message || '导入失败', 'err');
  }
}

async function onImportClick(e) {
  const el = e.target.closest('[data-imp]');
  if (!el) return;
  const act = el.dataset.imp;
  const i = el.dataset.i;
  switch (act) {
    case 'file': {
      const f = $('#impFile');
      if (f) f.click();
      break;
    }
    case 'start': await startImportPreview(); break;
    case 'cancel': {
      if (state.imp.task) { try { await post('/api/import/cancel', { id: state.imp.task }); } catch (e2) {} }
      if (state.impTimer) { clearInterval(state.impTimer); state.impTimer = 0; }
      state.imp.phase = 'input'; renderImport(); toast('已取消');
      break;
    }
    case 'reset': impReset(); renderImport(); break;
    case 'cand': {
      const x = impFind('l2', i);
      if (!x) break;
      x.selected = Number(el.dataset.c);
      x.item = x.candidates[x.selected] || null;
      renderImport();
      break;
    }
    case 'manual-search': {
      const inp = document.querySelector(`[data-imp-field="mq"][data-i="${i}"]`);
      const q = String((inp && inp.value) || (state.imp.manual[i] || {}).q || '').trim();
      if (!q) { toast('先输入要查找的歌名', 'err'); break; }
      try {
        const d = await rpc('search', { keyword: q, page: 1, size: 10 });
        state.imp.manual[i] = { q, list: (d.list || []).slice(0, 6) };
        renderImport();
      } catch (err) { toast(err.message || '搜索失败', 'err'); }
      break;
    }
    case 'manual-pick': {
      const x = impFind('l3', i);
      const m = state.imp.manual[i] || {};
      const c = (m.list || [])[Number(el.dataset.c)];
      if (!x || !c) break;
      x.item = c; x.chosen = true;
      renderImport();
      break;
    }
    case 'commit': await commitImport(); break;
    case 'health': {
      const box = $('#impHealth');
      if (box) box.textContent = '正在检查三家接口…';
      try {
        const d = await getJson('/api/providers/health');
        const u = d.data || {};
        if (box) box.innerHTML = (u.providers || []).map((p) =>
          `<div class="imp-ok">${p.ok ? '✓' : '!'} ${esc(p.label)} · ${p.tracks} 首 ${esc(p.error || '')}</div>`).join('')
          + `<div class="imp-ver">规则 ${esc(u.rules_version || '?')} · ${u.ok ? '全部正常' : '有通道降级（不影响本地文件导入）'}</div>`;
      } catch (err) { if (box) box.textContent = '检查失败：' + err.message; }
      break;
    }
    case 'update': {
      try {
        const d = await getJson('/api/update/check');
        const u = d.data || {};
        const rm = u.remote;
        toast(rm && rm.has_update ? '发现新规则版本，请更新客户端或替换 providers/ 规则文件'
          : (rm === 'not_configured' ? `已是本地最新 · 规则 ${u.rules_version}` : '远端版本检查暂不可用'));
      } catch (err) { toast(err.message || '检查失败', 'err'); }
      break;
    }
    default: break;
  }
}

function onImportChange(e) {
  const el = e.target.closest('[data-imp-field]');
  if (!el) return;
  const f = el.dataset.impField;
  if (f === 'file') {
    const file = el.files && el.files[0];
    if (!file) return;
    file.arrayBuffer().then((buf) => {
      let text = new TextDecoder('utf-8').decode(buf);
      // 酷狗旧版 .kgl 常见 GBK / windows-1252，UTF-8 解码会出现替换字符，换编码重读
      if ((text.match(/\uFFFD/g) || []).length > 0) {
        try { text = new TextDecoder('gb18030').decode(buf); } catch (e2) { /* 保留 UTF-8 结果 */ }
      }
      state.imp.filename = file.name;
      state.imp.fileText = text;
      const ta = $('#impText'); if (ta) ta.value = text;
      const fn = $('#impFileName');
      if (fn) fn.textContent = `${file.name} · ${text.length} 字`;
    }).catch(() => toast('文件读取失败', 'err'));
    return;
  }
  if (f === 'enhance') { state.imp.enhance.enabled = !!el.checked; return; }
  if (f === 'token') { state.imp.enhance.token = String(el.value || '').trim(); return; }
  if (f === 'userid') { state.imp.enhance.userid = String(el.value || '').trim(); return; }
  if (f === 'mq') {
    state.imp.manual[el.dataset.i] = Object.assign({}, state.imp.manual[el.dataset.i], { q: el.value });
    return;
  }
  if (f === 'pick') {
    const x = impFind(el.dataset.tier, el.dataset.i);
    if (!x) return;
    // L3 没有选定曲目时不许勾上，避免「勾了却导不进去」的假状态
    if (el.checked && el.dataset.tier === 'l3' && !x.item) {
      el.checked = false;
      toast('先手动查找并选中一首歌', 'err');
      return;
    }
    x.chosen = !!el.checked;
    if (x.chosen && !x.item && (x.candidates || []).length) x.item = x.candidates[x.selected || 0];
    renderImport();
  }
}

/* ------------------------------------------------------------------ 播放引擎 2.0
   单 <audio> 升级为「双元素 + Web Audio 图」，为 gapless / crossfade / EQ 服务。

   两条铁律：
   1. `audio()` 永远返回**当前活动的那个元素** —— 进度、音量、歌词、SMTC、
      续播那几百行代码一行都不用改，它们本来就是在读「正在响的那个」。
   2. 引擎关掉时退回 1.x 的原生单元素路径，行为完全一致。不是「旁路一下看看」，
      而是把元素整个重建 —— Web Audio 一旦接管就不可撤销，只有重建才真的回到旧路径。

   音频图（两元素各一个 Gain 做交叉，之后汇入同一条 EQ 链）：

       #audio   → srcA → gainA ┐
                               ├→ eq[0..9] → master → destination
       #audio-b → srcB → gainB ┘

   gapless 的实现不是「等 ended 再 play」（那有 200ms 启动空档），而是
   「提前把下一首启动、增益压到 0、到点开闸淡入」——声音在 A 结束的瞬间接上，
   静音空洞为 0。crossfade 就是把这个开闸过程拉长到 0.3~2s。 */
const EQ_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];
const EQ_PRESETS = {
  flat: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
  bass: [6, 5, 3.5, 1.5, 0, 0, 0, 0, 0, 0],
  vocal: [-2, -1.5, -0.5, 1, 2.5, 3.5, 3.5, 2, 0.5, 0],
  classical: [3, 2.5, 1.5, 0.5, 0, 0, 0.5, 1.5, 2.5, 2.5],
  pop: [-1.5, -1, 0.5, 1.5, 2.5, 2, 0.5, -0.5, 0, 0.5],
  rock: [4.5, 3.5, 2, -0.5, -1, -0.5, 1.5, 2.5, 3.5, 3.5],
};
const EQ_LABEL = {
  flat: '平直', bass: '低音增强', vocal: '人声',
  classical: '古典', pop: '流行', rock: '摇滚', custom: '自定义',
};
const ENG_LEAD = 0.16;        // gapless 开闸提前量：160ms 的双向淡入淡出
const PRELOAD_AT = 8;         // 剩余多少秒时把下一首塞进备用元素

const ENG = {
  on: false, ctx: null, master: null, eq: [], els: [], gains: [], srcs: [],
  slot: 0, pre: null, cross: ENG_LEAD, gapless: true, eqTimer: null,
};

const engActive = () => (ENG.on ? ENG.els[ENG.slot] : $('#audio'));
const engOther = () => (ENG.on ? ENG.els[1 - ENG.slot] : null);
const audio = () => engActive();
const engGainOf = (el) => {
  const i = ENG.els.indexOf(el);
  return i < 0 ? null : ENG.gains[i];
};

/** 引擎开关没显式设过时：桌面默认开，Android 默认关（P4.0 只做了桌面 spike） */
function engWant() {
  const v = state.settings && state.settings.engine_v2;
  if (v === false || v === 0 || v === 'false') return false;
  if (v === true || v === 1 || v === 'true') return true;
  return !IS_ANDROID;
}

/** 交叉时长：crossfade 设置优先，否则取 gapless 的 160ms；两者都关就是 0（回 ended 切） */
function engCrossSeconds() {
  const cf = Number((state.settings && state.settings.crossfade_ms) || 0);
  if (cf > 0) return Math.min(2000, Math.max(300, cf)) / 1000;
  return ENG.gapless ? ENG_LEAD : 0;
}

function engNewEl(id, preload) {
  const el = document.createElement('audio');
  el.id = id;
  el.preload = preload;
  el.setAttribute('playsinline', '');   // Android 不弹全屏播放器接管
  document.body.appendChild(el);
  return el;
}

function engVolume(v) {
  const val = Math.max(0, Math.min(1, Number(v) || 0));
  if (ENG.on) ENG.els.forEach((el) => { try { el.volume = val; } catch (e) { /* ignore */ } });
  else { const el = $('#audio'); if (el) el.volume = val; }
}

function engResume() {
  if (!ENG.on || !ENG.ctx) return;
  if (ENG.ctx.state === 'suspended') ENG.ctx.resume().catch(() => {});
}

function engBuild(elA, elB) {
  try {
    const CE = window.AudioContext || window.webkitAudioContext;
    if (!CE) return false;
    const ctx = new CE();
    const master = ctx.createGain();
    master.gain.value = 1;
    const eq = EQ_FREQS.map((f) => {
      const b = ctx.createBiquadFilter();
      b.type = 'peaking';
      b.frequency.value = f;
      b.Q.value = 1.0;
      b.gain.value = 0;
      return b;
    });
    for (let i = 1; i < eq.length; i += 1) eq[i - 1].connect(eq[i]);
    eq[eq.length - 1].connect(master);
    master.connect(ctx.destination);

    const els = [elA, elB];
    const gains = [];
    const srcs = [];
    for (let i = 0; i < els.length; i += 1) {
      const src = ctx.createMediaElementSource(els[i]);   // 每个元素只能接一次
      const g = ctx.createGain();
      g.gain.value = 1;
      src.connect(g);
      g.connect(eq[0]);
      srcs.push(src);
      gains.push(g);
    }
    Object.assign(ENG, { on: true, ctx, master, eq, els, gains, srcs, slot: 0 });
    return true;
  } catch (e) {
    return false;                          // 老内核 / Web Audio 被禁用：静默降级
  }
}

/** 实际生效的 10 段曲线（eq_gains 才是唯一真相，预设只是往里填数） */
function engCurve() {
  const g = (state.settings && state.settings.eq_gains) || [];
  return EQ_FREQS.map((_, i) => Math.max(-12, Math.min(12, Number(g[i]) || 0)));
}

function engApplyEq(immediate) {
  if (!ENG.on) return;
  const on = state.settings.eq_enabled === true;
  const curve = engCurve();
  const now = ENG.ctx.currentTime;
  ENG.eq.forEach((b, i) => {
    const v = on ? curve[i] : 0;
    try {
      b.gain.cancelScheduledValues(now);
      if (immediate) b.gain.setValueAtTime(v, now);
      else b.gain.setTargetAtTime(v, now, 0.03);   // 平滑过渡，切换预设不爆音
    } catch (e) {
      try { b.gain.value = v; } catch (e2) { /* ignore */ }
    }
  });
}

function engSetPreset(name) {
  const curve = (EQ_PRESETS[name] || EQ_PRESETS.flat).slice();
  state.settings.eq_preset = name;
  state.settings.eq_gains = curve;
  postSettings({ eq_preset: name, eq_gains: curve });
  engApplyEq();
}

/** 推子拖动：先落内存 + 立刻听感，落盘防抖 */
function engSetBand(i, db) {
  const cur = engCurve();
  cur[i] = Math.max(-12, Math.min(12, Number(db) || 0));
  state.settings.eq_gains = cur;
  state.settings.eq_preset = 'custom';
  engApplyEq();
  if (ENG.eqTimer) clearTimeout(ENG.eqTimer);
  ENG.eqTimer = setTimeout(() => {
    postSettings({ eq_gains: engCurve(), eq_preset: 'custom' });
  }, 350);
}

/** 预载失败也一样要走：拿不到音质候选（缺 minfo）就交给 ended 路径回源补全 */
function engCanChain() {
  if (!ENG.on || !ENG.gapless) return false;
  if (state.mode !== 'order') return false;      // 单曲循环 / 随机不做链式切换
  if (SLEEP.mode === 'track') return false;      // 播完就停，别偷偷预载下一首
  return state.playList.length > 1;
}

function engPreload() {
  const idx = nextIndexOf(1);
  if (idx < 0 || idx === state.playIndex) return;
  const item = state.playList[idx];
  if (!item || !item.id) return;
  const b = engOther();
  if (!b) return;
  // 刚淡出的元素还在收尾：这时候覆写 src 会把淡出腰斩成一段真的静音。
  // 等冷却过去（recycle 跑完）再来 —— 短音频可能等不到，那就交给 ended 路径。
  if (b.__coolUntil && Date.now() < b.__coolUntil) return;
  const cands = playCandidates(item);
  if (!cands.length) return;
  const q = cands[0];
  try {
    b.volume = engActive().volume;
    b.src = mediaSrc(item, q);
    b.load();
  } catch (e) { return; }
  ENG.pre = { index: idx, item, q, el: b, at: Date.now() };
}

function engAdopt(index, item, q) {
  state.playIndex = index;
  state.playing = item;
  state.currentQuality = q;
  if (item.platform === 'local' && !item.cover) {
    item.cover = '/api/local/cover?p=' + encodeURIComponent(item.id);
  }
  lastCountedKey = '';                 // 下一首重新计一次「听完」
  paintPlayer(item);
  renderPlaying();
  renderQueue();
  renderNp();
  touchRecent(item);
  const btn = $('#playBtn');
  if (btn) btn.innerHTML = icon('pause');
  setBuffering(false);
}

function engRecycle(el, g, delay) {
  setTimeout(() => {
    if (el === engActive()) return;    // 期间用户又切回来了，别动它
    // 已经被下一次预载征用了：只把增益复位，src 留着别清
    if (ENG.pre && ENG.pre.el === el) {
      try {
        const now = ENG.ctx.currentTime;
        g.gain.cancelScheduledValues(now);
        g.gain.setValueAtTime(1, now);
      } catch (e) { /* ignore */ }
      return;
    }
    try { el.pause(); el.removeAttribute('src'); el.load(); } catch (e) { /* ignore */ }
    delete el.__coolUntil;
    try {
      const now = ENG.ctx.currentTime;
      g.gain.cancelScheduledValues(now);
      g.gain.setValueAtTime(1, now);
    } catch (e) { /* ignore */ }
  }, Math.max(80, delay));
}

/** 手动起播前把两条增益拉回 1：上一轮交叉被打断时可能停在半路 */
function engGainReset() {
  if (!ENG.on) return;
  const now = ENG.ctx.currentTime;
  ENG.gains.forEach((g) => {
    try {
      g.gain.cancelScheduledValues(now);
      g.gain.setValueAtTime(1, now);
    } catch (e) { /* ignore */ }
  });
}

/** 开销闸：备用元素已经开播、增益压到 0，到点把两条 gain 反向拉过去 */
function engCross(remain) {
  const pre = ENG.pre;
  if (!pre || !ENG.on) return;
  const oldEl = engActive();
  const newEl = pre.el;
  if (newEl !== engOther()) { ENG.pre = null; return; }
  const gOld = engGainOf(oldEl);
  const gNew = engGainOf(newEl);
  if (!gOld || !gNew) { ENG.pre = null; return; }
  ENG.pre = null;

  // 交叉时长以设置为准，但不能超过当前曲剩下的时间（短音频 / 用户拖到末尾时）
  const left = Number.isFinite(remain) ? remain : ENG.cross;
  const sec = Math.max(0.06, Math.min(ENG.cross, Math.max(0.06, left)));
  const now = ENG.ctx.currentTime;
  try { newEl.currentTime = 0; } catch (e) { /* 元数据还没到，交给 play() 自己定位 */ }
  const p = newEl.play();
  if (p && p.catch) p.catch(() => {});   // 被自动播放策略拦下就回落到 ended 路径
  try {
    gNew.gain.cancelScheduledValues(now);
    gNew.gain.setValueAtTime(0, now);
    gNew.gain.linearRampToValueAtTime(1, now + sec);
    gOld.gain.cancelScheduledValues(now);
    gOld.gain.setValueAtTime(1, now);
    gOld.gain.linearRampToValueAtTime(0, now + sec);
  } catch (e) {
    try { gNew.gain.value = 1; gOld.gain.value = 0; } catch (e2) { /* ignore */ }
  }

  ENG.slot = 1 - ENG.slot;             // 先换槽：之后 audio() 读到的就是新曲
  engAdopt(pre.index, pre.item, pre.q);
  oldEl.__coolUntil = Date.now() + sec * 1000 + 250;   // 淡出期不许被预载抢走
  engRecycle(oldEl, gOld, sec * 1000 + 120);
  engResume();
}

/** timeupdate 里调：按剩余时长决定预载 / 开闸 */
function engTick() {
  if (!engCanChain()) return;
  const a = engActive();
  const d = a.duration || 0;
  if (!d || !isFinite(d)) return;
  const remain = d - a.currentTime;
  if (!ENG.pre) {
    // 下限只留 0.5s，不跟交叉时长绑：短音频（试听片段 / 铃声）缓冲慢一点就会
    // 错过预载窗口，那时宁可用一次更短的交叉，也不能掉回 200ms 的 ended 空档。
    if (remain <= PRELOAD_AT && remain > 0.5) engPreload();
    return;
  }
  // 预载刚下发时别急着开闸（给 load() 一点时间）；但已经到底了就不再等
  const waited = Date.now() - (ENG.pre.at || 0);
  if (remain <= ENG.cross && (waited >= 200 || remain < 0.25)) engCross(remain);
}

/** 手动换歌时把备用元素按住：否则预载残留会在新曲开始时冒出来 */
function engHaltOther() {
  if (!ENG.on) return;
  ENG.pre = null;
  const o = engOther();
  if (!o) return;
  try { o.pause(); } catch (e) { /* ignore */ }
  const g = engGainOf(o);
  if (!g) return;
  try {
    const now = ENG.ctx.currentTime;
    g.gain.cancelScheduledValues(now);
    g.gain.setValueAtTime(1, now);
  } catch (e) { /* ignore */ }
}

function engTeardown() {
  ENG.pre = null;
  if (ENG.eqTimer) { clearTimeout(ENG.eqTimer); ENG.eqTimer = null; }
  if (ENG.ctx) { try { ENG.ctx.close(); } catch (e) { /* ignore */ } }
  // 引擎没起来时 ENG.els 是空的，但 DOM 里仍有 #audio —— 一并收走，否则重建会撞 id
  const els = ENG.els.length
    ? ENG.els.slice()
    : Array.from(document.querySelectorAll('#audio, #audio-b'));
  els.forEach((el) => {
    try { el.pause(); el.removeAttribute('src'); el.remove(); } catch (e) { /* ignore */ }
  });
  Object.assign(ENG, { on: false, ctx: null, master: null, eq: [], els: [], gains: [], srcs: [], slot: 0 });
}

function engBoot() {
  if (ENG.on) return;              // 幂等：refreshStore 会被多次调用，图只搭一次
  const a = $('#audio');
  if (!a) return;
  ENG.gapless = state.settings.gapless !== false;
  ENG.cross = engCrossSeconds();
  if (!engWant()) { bindAudioEl(a); return; }
  const b = $('#audio-b') || engNewEl('audio-b', 'auto');
  if (!engBuild(a, b)) { bindAudioEl(a); return; }
  engVolume(Number(state.settings.volume ?? 80) / 100);
  bindAudioEl(b);
  bindAudioEl(a);
  engApplyEq(true);
}

/** 音频事件绑定：跟着元素走（引擎切换会整只重建），非活动元素的事件一律不理 */
function bindAudioEl(a) {
  if (!a || a.dataset.engBound === '1') return;
  a.dataset.engBound = '1';
  const mine = () => (ENG.on ? a === engActive() : a === $('#audio'));

  a.addEventListener('play', () => {
    if (!mine()) return;
    engResume();
    $('#playBtn').innerHTML = icon('pause');
    renderPlaying();
    mediaPushMeta(true);           // SMTC / Android 通知栏状态 → playing（即时，不吃节流）
    postLyricNow();
  });
  a.addEventListener('pause', () => {
    if (!mine()) return;
    $('#playBtn').innerHTML = icon('play');
    saveLastTrack(true);
    mediaPushMeta(true);
    postLyricNow();
  });
  a.addEventListener('timeupdate', () => {
    if (!mine()) return;
    paintSeek();
    updateLyricTick();
    postLyricNow();
    mediaPushTick(false);
    if (!a.paused) {
      const d = a.duration || 0;
      // 重听检测：计过数后又回到前 20% 段（单曲循环 / 拖回开头 / 重播），
      // 清掉计数标志，这次听完再计一次 —— 否则同一会话内永远只计 1 次，
      // 「当天听完 2 次进今日缓存」在循环播放下凑不齐。
      const k = state.playing ? songKey(state.playing) : '';
      if (lastCountedKey && lastCountedKey === k && d && a.currentTime < d * 0.2) {
        lastCountedKey = '';
      }
      if (d && a.currentTime / d >= 0.98 && !lastCountedKey) reportPlay(true);
      engTick();                   // 曲末预载 + 开闸，都是这里推进的
    }
  });
  a.addEventListener('ended', () => {
    if (!mine()) return;           // 交叉期间的旧元素由 engRecycle 收拾，不重复切歌
    reportPlay(true);
    if (SLEEP.mode === 'track') {  // 睡眠定时「播完这首就停」：计完数就地停下
      SLEEP.mode = 'off';
      syncSleepUI();
      toast('睡眠定时：已按约定停在曲末');
      return;
    }
    if (state.mode === 'one') {
      a.currentTime = 0;
      a.play().catch(() => {});
      return;
    }
    playNext(1);
  });
  a.addEventListener('error', () => {
    if (!mine()) return;
    if (a.src && !a.dataset.errShown) {
      a.dataset.errShown = '1';
      setBuffering(false);
      setTimeout(() => { delete a.dataset.errShown; }, 3000);
    }
  });
  a.addEventListener('seeking', () => { if (mine()) setBuffering(false); });
  a.addEventListener('waiting', () => { if (mine()) setBuffering(true); });
  a.addEventListener('playing', () => { if (mine()) setBuffering(false); });
  a.addEventListener('loadedmetadata', () => {
    if (!mine()) return;
    setBuffering(false); paintSeek(); mediaPushTick(true);
  });
  a.addEventListener('seeked', () => {
    if (!mine()) return;
    mediaPushTick(true); postLyricNow();
  });
}

/** 切换引擎开关：重建两个元素，彻底回到 / 离开 Web Audio 路径，并把当前曲目接着放 */
async function engRebuild() {
  const a0 = audio();
  const keep = (state.playing && a0) ? {
    list: state.playList.slice(),
    index: state.playIndex,
    pos: a0.currentTime || 0,
    playing: !a0.paused,
    source: state.queueSource,
  } : null;
  engTeardown();
  engNewEl('audio', 'metadata');
  engBoot();
  if (keep && keep.list.length && keep.index >= 0) {
    await playAt(keep.list, keep.index, {
      position: keep.pos, keepPaused: !keep.playing, source: keep.source,
    });
  }
}

function setBuffering(on) {
  const el = $('#buffering');
  if (el) el.hidden = !on;
}

/* ------------------------------------------------------------------ 系统媒体集成
   页面只维护一份「对外播放状态」，两端各走各的原生通道：
   · 桌面：navigator.mediaSession → Windows SMTC（任务栏媒体浮层 / 锁屏 /
     键盘媒体键 / 蓝牙耳机控制）。WebView2 是 Chromium 内核，原生支持。
   · Android：window.ElysiumAndroid.mediaUpdate → 原生 PlaybackService
     （前台服务 + MediaStyle 通知 + 线控），原生命令经 window.__elysiumMedia
     打回页面 —— Android WebView 的媒体会话归原生管，避免出现两套控制。 */
const MS_ON = ('mediaSession' in navigator)
  && document.documentElement.dataset.platform !== 'android';
const ANDROID_BR = !!(window.ElysiumAndroid && window.ElysiumAndroid.mediaUpdate);
let _msPosAt = 0;
let _andPosAt = 0;
let _msMetaKey = '';

function mediaPushMeta(force) {
  const it = state.playing;
  const a = audio();
  const key = it ? `${it.platform}:${it.id}:${it.name || ''}` : '';
  if (MS_ON && it && (force || key !== _msMetaKey)) {
    _msMetaKey = key;
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: it.name || '',
        artist: it.artist || '',
        album: it.album_name || '',
        artwork: it.cover ? [{ src: it.cover, sizes: '512x512' }] : [],
      });
    } catch (e) { /* 老内核没有 MediaMetadata 构造器就静默降级 */ }
  }
  if (MS_ON) {
    try { navigator.mediaSession.playbackState = a.paused ? 'paused' : 'playing'; } catch (e) {}
  }
  if (ANDROID_BR && it) {
    const now = Date.now();
    if (force || now - _andPosAt >= 3000) {
      _andPosAt = now;
      try {
        window.ElysiumAndroid.mediaUpdate(JSON.stringify({
          title: it.name || '', artist: it.artist || '', album: it.album_name || '',
          cover: it.cover || '', playing: !a.paused && !a.ended,
          position: Math.floor(a.currentTime || 0), duration: Math.floor(a.duration || 0),
        }));
      } catch (e) { /* 桥暂时不通（页面比原生先加载）下个 tick 再试 */ }
    }
  }
}

function mediaPushTick(force) {
  const a = audio();
  const d = a.duration;
  if (MS_ON && isFinite(d) && d > 0) {
    const now = Date.now();
    if (force || now - _msPosAt >= 5000) {
      _msPosAt = now;
      try {
        navigator.mediaSession.setPositionState({
          duration: d, position: Math.min(a.currentTime || 0, d),
          playbackRate: a.playbackRate || 1,
        });
      } catch (e) { /* position 越界等边界值，下个 tick 会修正 */ }
    }
  }
  if (force) mediaPushMeta(true);   // seek 后两端进度都立即校正
}

/* 原生（Android PlaybackService）→ 页面的命令入口；桌面端不会被调用 */
window.__elysiumMedia = (cmd, arg) => {
  const a = audio();
  switch (String(cmd || '')) {
    case 'toggle': if (a.paused) a.play().catch(() => {}); else a.pause(); break;
    case 'play': a.play().catch(() => {}); break;
    case 'pause': a.pause(); break;
    case 'next': playNext(1); break;
    case 'prev': playNext(-1); break;
    case 'seek': {
      if (a.duration) a.currentTime = Math.min(a.duration, Math.max(0, Number(arg) || 0));
      break;
    }
    case 'stop': a.pause(); try { a.currentTime = 0; } catch (e) { /* ignore */ } break;
    default: break;
  }
};

if (MS_ON) {
  const msAct = {
    play: () => audio().play().catch(() => {}),
    pause: () => audio().pause(),
    previoustrack: () => playNext(-1),
    nexttrack: () => playNext(1),
    seekbackward: (d) => {
      const a = audio();
      a.currentTime = Math.max(0, a.currentTime - ((d && d.seekOffset) || 10));
    },
    seekforward: (d) => {
      const a = audio();
      if (a.duration) a.currentTime = Math.min(a.duration, a.currentTime + ((d && d.seekOffset) || 10));
    },
    seekto: (d) => {
      const a = audio();
      if (d && (d.fastSeek || d.hasDuration !== false)) {
        try { a.currentTime = d.seekTime; } catch (e) { /* ignore */ }
      }
    },
    stop: () => {
      const a = audio();
      a.pause();
      try { a.currentTime = 0; } catch (e) { /* ignore */ }
    },
  };
  Object.keys(msAct).forEach((k) => {
    try { navigator.mediaSession.setActionHandler(k, msAct[k]); } catch (e) { /* 内核不认就算了 */ }
  });
}

/* ------------------------------------------------------------------ 睡眠定时
   两种口径：until = 到点就暂停（倒计时）；track = 播完当前这首就停。
   会话级设置，不落盘 —— 定时睡觉是每次的行为，不是偏好。 */
const SLEEP = { mode: 'off', at: 0, timer: null };

function setSleep(mode, minutes) {
  if (SLEEP.timer) { clearInterval(SLEEP.timer); SLEEP.timer = null; }
  if (mode !== 'until' && mode !== 'track') {
    SLEEP.mode = 'off'; SLEEP.at = 0;
    toast('睡眠定时已取消');
    syncSleepUI();
    return;
  }
  SLEEP.mode = mode;
  if (mode === 'track') {
    SLEEP.at = 0;
    toast('睡眠定时：播完当前这首就停');
  } else {
    SLEEP.at = Date.now() + minutes * 60000;
    toast(`睡眠定时：${minutes} 分钟后暂停播放`);
    SLEEP.timer = setInterval(() => {
      if (Date.now() >= SLEEP.at) fireSleep();
    }, 1000);
  }
  syncSleepUI();
}

function fireSleep() {
  if (SLEEP.timer) { clearInterval(SLEEP.timer); SLEEP.timer = null; }
  SLEEP.mode = 'off'; SLEEP.at = 0;
  const a = audio();
  if (a && !a.paused) a.pause();
  toast('睡眠时间到，已暂停播放');
  syncSleepUI();
}

function syncSleepUI() {
  const sel = $('#setSleep');
  if (sel) sel.value = SLEEP.mode;
}

/* 桌面歌词小窗的数据推送：行变化时把当前行推给本地中继（纯内存）。 */
let _lyricPostAt = 0;
function postLyricNow() {
  if (document.documentElement.dataset.platform === 'android') return;
  const now = Date.now();
  if (now - _lyricPostAt < 300) return;
  _lyricPostAt = now;
  const it = state.playing || {};
  const i = lyricIdx;
  const cur = (lyricLines && i >= 0 && lyricLines[i] && lyricLines[i].text) || '';
  const nxt = (lyricLines && i >= 0 && lyricLines[i + 1] && lyricLines[i + 1].text) || '';
  const tr = (lyricLines && i >= 0 && lyricLines[i] && lyricLines[i].trans) || '';
  post('/api/lyric/now', {
    title: it.name || '', artist: it.artist || '',
    line: cur, next: nxt, trans: tr, cover: it.cover || '',
    playing: !audio().paused,
  }).catch(() => {});
}

function paintPlayer(item) {
  const art = $('#plArt');
  art.innerHTML =
    (item.cover ? `<img src="${esc(item.cover)}" alt="" onerror="this.remove()">` : phImg('song')) +
    '<span class="spin" id="buffering" hidden></span>';
  $('#plTitle').textContent = item.name || '';
  $('#plSub').textContent = [item.artist, item.album_name].filter(Boolean).join(' · ');
  const fav = isFav('song', item.id, item.platform);
  const fb = $('#plFav');
  fb.innerHTML = icon(fav ? 'heart' : 'heart-o');
  fb.classList.toggle('on', fav);
  paintSpec();
  mediaPushMeta(true);            // SMTC 元数据 + Android 通知栏同步换曲
}

/* 迷你条：窗口缩成 430×138 只留播放条。窗口尺寸由 Python 侧改，
   前端只负责切布局 —— 两边必须一起动，否则会出现「界面缩了但窗口还大」。 */
function setMini(on) {
  const api = window.pywebview && window.pywebview.api;
  if (!api || typeof api.mini_mode !== 'function') {
    toast('迷你条只在桌面端可用', 'err');
    return;
  }
  document.documentElement.dataset.mini = on ? 'on' : 'off';
  const btn = $('#miniBtn');
  if (btn) {
    btn.innerHTML = icon(on ? 'restore' : 'mini');
    btn.title = on ? '还原窗口' : '迷你条';
  }
  try { api.mini_mode(!!on); } catch (e) { /* 窗口改不动就保持布局，至少不崩 */ }
}

function paintSpec() {
  const q = state.currentQuality;
  const el = $('#plSpec');
  if (!q) { el.textContent = '—'; return; }
  if (state.playing && state.playing.platform === 'work') { el.textContent = '作品 · WAV'; return; }
  el.textContent = (q.format === 'flac' ? 'FLAC' : 'MP3') + ' · ' + q.bitrate + 'k';
}

let lastCountedKey = '';
/** 开播即记入最近播放（与缓存计数无关，那个仍要听完才算） */
function touchRecent(item) {
  if (!item || !item.id) return;
  state.recent = [item].concat(state.recent.filter(
    (r) => !(String(r.id) === String(item.id) && r.platform === item.platform))).slice(0, 60);
  post('/api/store', { op: 'recent_add', payload: item }).catch(() => {});
}

function reportPlay(completed) {
  const item = state.playing;
  if (!item || !state.currentQuality) return;
  const key = songKey(item);          // 只认歌曲，切换音质不会重复计数
  if (completed && key === lastCountedKey) return;
  if (completed) lastCountedKey = key;
  post('/api/play', { item, quality: state.currentQuality, completed })
    .then((r) => {
      if (completed) refreshStatsQuiet();
      const t = r && r.data;
      if (!t || !t.triggered) return;
      if (t.triggered === 'long') toast(`「${item.name}」已升为长期缓存`);
      else toast(`「${item.name}」已存入今日缓存`);
    })
    .catch(() => {});
}

function maybeCountOnLeave() {
  const a = audio();
  const d = a.duration || 0;
  if (!d || !state.playing) return;
  if (a.currentTime / d >= 0.98) reportPlay(true);
}

async function startTrack(item, cands, i, position, opts = {}) {
  const q = cands[i];
  if (!q) {
    setBuffering(false);
    // 音质候选全试完仍失败：区分「这首歌彻底播不了」与「网络抖了一下」。
    // 只有彻底不可用才换下一首，否则网络卡顿会让人以为歌单被跳过。
    if (state.playList.length > 1 && !opts.networkIssue) {
      toast('这首播不了，已切到下一首', 'err');
      playNext(1);
    } else {
      toast('网络不稳，这首暂时放不出来 · 可点播放键重试', 'err');
    }
    return;
  }
  setBuffering(true);
  try {
    const a = audio();
    engResume();                      // Web Audio 上下文要用户手势才能跑
    engGainReset();                   // 新曲从满增益开始，别继承上一轮未走完的淡出
    a.src = mediaSrc(item, q);        // 同步设 src，保住用户手势
    a.dataset.media = a.src;
    engVolume((Number(state.settings.volume ?? 80)) / 100);
    if (position > 0) {
      a.addEventListener('loadedmetadata', () => {
        try { a.currentTime = position; } catch (e) { /* ignore */ }
      }, { once: true });
    }
    await a.play();
    state.currentQuality = q;
    if (!opts.keepCounted) lastCountedKey = '';
    paintSpec();
    if (opts.keepPaused) {          // 暂停时切音质，保持暂停态
      a.pause();
      $('#playBtn').innerHTML = icon('play');
    } else {
      $('#playBtn').innerHTML = icon('pause');
    }
    touchRecent(item);            // 开播即进「最近播放」，不必等听完
  } catch (e) {
    if (e && (e.name === 'NotAllowedError' || /not allowed|user gesture/i.test(e.message || ''))) {
      setBuffering(false);
      $('#playBtn').innerHTML = icon('play');
      toast('点一下播放键开始播放');
      return;
    }
    // 加载慢/连接被掐断 = 网络问题，不是这首歌的问题：
    // 同一首换一个音质再试，都不行就停在原地提示，绝不自动跳下一首。
    const netIssue = /network|load|abort|interrupted|timeout|stall|failed to load/i
      .test((e && e.message) || '') || (e && e.name === 'NotSupportedError');
    if (i + 1 < cands.length) {
      startTrack(item, cands, i + 1, position, { ...opts, networkIssue: netIssue });
      return;
    }
    setBuffering(false);
    $('#playBtn').innerHTML = icon('play');
    if (netIssue) {
      toast('网络不稳，这首暂时放不出来 · 可点播放键重试', 'err');
      return;                      // 停在当前曲，不切歌
    }
    toast('播放失败：' + (e.message || '未知错误'), 'err');
    if (state.playList.length > 1) playNext(1);
  }
}

const QUEUE_SRC = {
  search: '搜索结果', home: '首页', library: '收藏本',
  artist: '歌手页', album: '专辑', taste: '口味画像', local: '本地音乐',
};

async function playAt(list, index, opts = {}) {
  let item = list[index];
  if (!item) return;
  item = await ensureMinfo(item);       // 歌单条目缺 minfo：回源补全再播
  const cands = playCandidates(item);
  if (!cands.length) { toast('这首歌没有可用音质', 'err'); return; }
  state.resumePos = 0;             // 换歌/重播都用新的位置，旧的待续位置作废
  maybeCountOnLeave();
  engHaltOther();                  // 上一轮预载的下一首作废，别让它在新曲里冒头
  state.playList = list.slice();
  state.playIndex = index;
  state.playing = item;
  if (item.platform === 'local' && !item.cover) {   // 封面惰性取：只取真正开播的这首
    item.cover = '/api/local/cover?p=' + encodeURIComponent(item.id);
  }
  state.queueSource = opts.source || (
    list === (state.reco && state.reco.list) ? '为你推荐'
      : list === state.recent ? '最近播放'
        : (QUEUE_SRC[state.route.name] || '播放列表'));
  paintPlayer(item);
  renderPlaying();
  renderQueue();
  renderNp();
  await startTrack(item, cands, 0, opts.position || 0, { keepPaused: !!opts.keepPaused });
}

function nextIndexOf(step = 1) {
  const n = state.playList.length;
  if (!n) return -1;
  if (state.mode === 'shuffle') {
    if (n === 1) return 0;
    let i = Math.floor(Math.random() * (n - 1));
    if (i >= state.playIndex) i += 1;
    return i;
  }
  return (state.playIndex + step + n) % n;
}

function playNext(step) {
  if (!state.playList.length) return;
  if (state.mode === 'one' && step > 0) {
    const a = audio();
    a.currentTime = 0;
    a.play().catch(() => {});
    return;
  }
  playAt(state.playList, nextIndexOf(step), { source: state.queueSource });
}

function cycleMode() {
  const i = MODES.findIndex((m) => m.key === state.mode);
  const next = MODES[(i + 1) % MODES.length];
  state.mode = next.key;
  const btn = $('#modeBtn');
  btn.innerHTML = icon(next.icon);
  btn.title = next.label;
  btn.classList.toggle('on', next.key !== 'order');
  post('/api/store', { op: 'settings', payload: { mode: next.key } }).catch(() => {});
  toast(next.label);
}

/* ------------------------------------------------------------------ 续播 */
function saveLastTrack(force) {
  const a = audio();
  if (!state.playing || !state.currentQuality || !a.currentTime) return;
  if (!force && a.paused) return;
  const now = Date.now();
  if (!force && now - lastSaveAt < 5000) return;
  lastSaveAt = now;
  const lt = {
    item: state.playing,
    quality: state.currentQuality,
    position: Math.floor(a.currentTime),
    // 时长一起存：启动恢复播放条时没有音频 metadata 可读，
    // 有这个才能把进度条画在正确位置，而不是永远 00:00 / 空条。
    duration: Math.floor(a.duration || 0),
    at: now,
  };
  state.settings.last_track = lt;
  try { localStorage.setItem(LS_LAST, JSON.stringify(lt)); } catch (e) { /* ignore */ }
  post('/api/store', { op: 'settings', payload: { last_track: lt } }).catch(() => {});
}

function pickLastTrack() {
  const fromServer = state.settings.last_track || null;
  let fromLocal = null;
  try { fromLocal = JSON.parse(localStorage.getItem(LS_LAST) || 'null'); } catch (e) { fromLocal = null; }
  if (!fromLocal) return fromServer;
  if (!fromServer) return fromLocal;
  return (fromLocal.at || 0) >= (fromServer.at || 0) ? fromLocal : fromServer;
}

function restoreLast() {
  const lt = pickLastTrack();
  if (!lt || !lt.item || !lt.item.id) return false;
  const item = lt.item;
  state.playing = item;
  state.playList = [item];
  state.playIndex = 0;
  state.currentQuality = lt.quality || playCandidates(item)[0];
  paintPlayer(item);
  renderQueue();
  renderPlaying();
  /* 只摆播放条的样子，不预载音频。
     存下来的那条 media URL 带 time+sign 签名，隔一段时间启动必然过期：
     在这里 a.load() 一次就是一条 console 资源报错，而且 loadedmetadata
     永不触发、进度也恢复不了 —— 可用户已经看到「上次听到…按播放接着听」，
     说了能听、按下去才出问题。真正的取流留给 resume：
     playAt(..., {position}) 会重新签名并定位。 */
  const pos = Math.max(0, Math.floor(lt.position || 0));
  const dur = Math.max(0, Math.floor(lt.duration || 0));
  state.resumePos = pos;            // 真正起播时用它定位
  if (state.currentQuality) {
    if (pos) {
      $('#curTime').textContent = fmtTime(pos);
      if (dur > pos) {
        $('#allTime').textContent = fmtTime(dur);
        const r = Math.min(1, pos / dur);
        $('#seekFill').style.width = (r * 100).toFixed(2) + '%';
        $('#seekDot').style.left = (r * 100).toFixed(2) + '%';
      }
    }
    toast(`上次听到「${item.name}」，按播放接着听`);
  }
  return true;
}

function paintSeek() {
  const a = audio();
  const d = a.duration || 0;
  const r = d ? Math.min(1, a.currentTime / d) : 0;
  $('#curTime').textContent = fmtTime(a.currentTime);
  $('#allTime').textContent = fmtTime(d);
  $('#seekFill').style.width = (r * 100).toFixed(2) + '%';
  $('#seekDot').style.left = (r * 100).toFixed(2) + '%';
  if (a.buffered.length) {
    const end = a.buffered.end(a.buffered.length - 1);
    $('#seekBuf').style.width = (d ? Math.min(1, end / d) * 100 : 0).toFixed(2) + '%';
  }
}

/* ------------------------------------------------------------------ 渲染组件 */
const isPlaying = (it) => state.playing && String(state.playing.id) === String(it.id)
  && state.playing.platform === it.platform;

function eqHtml() {
  return '<div class="eq"><i></i><i></i><i></i><i></i></div>';
}

/* ------------------------------------------------------------------ 美术占位 */
/* 上游没给封面时才用；空状态按当前主题取对应插画，避免浅底上出现深色方块 */
function phImg(kind) {
  /* 空状态用 CSS 背景：队列/下载抽屉只在 boot 渲染一次，靠 DOM 切主题会漏掉 */
  if (kind === 'empty') return '<span class="eico-art"></span>';
  const file = kind === 'artist' ? 'cover_artist.webp'
    : kind === 'album' ? 'cover_album.webp'
      : 'cover_song.webp';
  return `<img class="ph" src="/art/${file}" alt="" loading="lazy">`;
}

function coverHtml(url, shape) {
  const inner = url
    ? `<img src="${esc(url)}" alt="" loading="lazy" onerror="this.remove()">`
    : phImg('song');
  return `<div class="tr-cover">${inner}</div>`;
}

/** 可下载的最高音质（FLAC 优先），用于规格列展示 */
function bestQuality(item) {
  const minfo = (item && item.minfo) || [];
  if (!minfo.length) return null;
  const flac = minfo.filter((m) => m.format === 'flac').sort((a, b) => _br(b) - _br(a));
  const mp3 = minfo.filter((m) => m.format === 'mp3').sort((a, b) => _br(b) - _br(a));
  return flac[0] || mp3[0] || minfo[0];
}

function rowHtml(it, i, opt = {}) {
  const on = isPlaying(it);
  const pr = PROBE.get(pk(it));
  const spec = qualityChip(it, i);
  const fav = isFav('song', it.id, it.platform);
  const loc = it.platform === 'local';
  const albumCell = opt.hideAlbum ? ''
    : `<div class="tr-album">${it.album_id
      ? `<button data-act="album" data-id="${esc(it.album_id)}" data-platform="${esc(it.platform)}" title="${esc(it.album_name || '')}">${esc(it.album_name || '—')}</button>`
      : esc(it.album_name || '—')}</div>`;
  const artistBtn = it.artist
    ? `<button data-act="artist" data-name="${esc(it.artist)}" data-platform="${esc(it.platform)}" data-aid="${esc(String(it.artist_id || '').trim())}">${esc(it.artist)}</button>`
    : '';
  return `
  <div class="tr${on ? ' on' : ''}${pr && !pr.valid && !pr.unresolved ? ' dead' : ''}" data-row="${i}" data-pk="${esc(pk(it))}" data-act="play" data-i="${i}">
    <div class="tr-n">
      ${on ? eqHtml() : `<span>${i + 1}</span>`}
      <button class="tr-play" data-act="play" data-i="${i}" title="播放">${icon('play')}</button>
    </div>
    ${coverHtml(it.cover)}
    <div class="tr-main">
      <div class="tr-name">${esc(it.name || '未知曲目')}</div>
      <div class="tr-sub">${artistBtn}</div>
    </div>
    <div class="tr-src">${srcBadge(it, i)}</div>
    ${albumCell}
    <div class="tr-spec">${spec}</div>
    <div class="tr-dur mono">${fmtTime(it.duration)}</div>
    <div class="tr-act">
      <button class="ic${fav ? ' on' : ''}" data-act="fav" data-kind="song" data-i="${i}" title="收藏">${icon(fav ? 'heart' : 'heart-o')}</button>
      ${loc ? '' : `<button class="ic" data-act="dl" data-i="${i}" title="下载">${icon('dl')}</button>`}
    </div>
  </div>`;
}

function theadHtml(opt = {}) {
  return `<div class="thead">
    <span class="th-n">#</span><span class="th-cover"></span><span class="th-main">曲目</span>
    <span class="th-src">来源</span>
    ${opt.hideAlbum ? '' : '<span class="th-album">专辑</span>'}
    <span class="th-spec">实测规格</span>
    <span class="th-dur">时长</span><span class="th-act"></span>
  </div>`;
}

function trackListHtml(items, opt = {}) {
  if (!items.length) {
    return emptyHtml('disc', '这里是空的', opt.empty || '换个关键词再试试');
  }
  setTimeout(() => probeRows(items), 30);
  return theadHtml(opt) + items.map((it, i) => rowHtml(it, i, opt)).join('');
}

/* 专辑卡的备选来源：'kuwo:123,wyy:456'，主来源打不开时按序兜底 */
const altsAttr = (a) => ((a && a.alts) || []).map((x) => `${x.platform}:${x.id}`).join(',');
const parseAlts = (s) => String(s || '').split(',').map((x) => {
  const i = x.indexOf(':');
  return i > 0 ? { platform: x.slice(0, i), id: x.slice(i + 1) } : null;
}).filter(Boolean);

function albumCardHtml(a) {
  return `<button class="card" data-act="album" data-id="${esc(a.id)}" data-platform="${esc(a.platform)}" data-alts="${esc(altsAttr(a))}">
    <div class="card-art">${a.cover ? `<img src="${esc(a.cover)}" alt="" loading="lazy" onerror="this.remove()">` : phImg('album')}
      <span class="cplay" data-act="album-play" data-id="${esc(a.id)}" data-platform="${esc(a.platform)}">${icon('play')}</span>
    </div>
    <div class="card-name">${esc(a.name || '未知专辑')}</div>
    <div class="card-sub">${esc(a.artist || '')}${a.count ? ` · <span class="mono">${a.count}</span>` : ''}</div>
  </button>`;
}

function playlistCardHtml(pl) {
  const n = (pl.items || []).length;
  return `<button class="card" data-act="playlist" data-id="${esc(pl.id)}">
    <div class="card-art pl-art"><svg><use href="#i-list"/></svg>
      ${n ? `<span class="cplay" data-act="pl-play" data-id="${esc(pl.id)}">${icon('play')}</span>` : ''}
    </div>
    <div class="card-name">${esc(pl.name)}</div>
    <div class="card-sub">${n ? `<span class="mono">${n}</span> 首` : '空歌单'}</div>
  </button>`;
}

function artistCardHtml(a, hideFav = false) {
  const fav = isFav('artist', a.name, a.platform);
  const aid = String(a.artist_id || '').trim();
  const inner = a.cover
    ? `<img src="${esc(a.cover)}" alt="" loading="lazy" onerror="this.remove()">`
    : phImg('artist');
  return `<button class="card round" data-act="artist" data-name="${esc(a.name)}" data-platform="${esc(a.platform)}" data-aid="${esc(aid)}">
    <div class="card-art">${inner}
      <span class="cplay" data-act="artist-play" data-name="${esc(a.name)}" data-platform="${esc(a.platform)}" data-aid="${esc(aid)}">${icon('play')}</span>
    </div>
    <div class="card-name">${esc(a.name)}</div>
    <div class="card-sub">${a.count ? `<span class="mono">${a.count}</span> 首` : '歌手'} · ${esc(srcOf(a).label)}${fav && !hideFav ? ' · 已收藏' : ''}</div>
  </button>`;
}

function initial(name) {
  const t = String(name || '').trim();
  if (!t) return '♪';
  return /[a-z0-9]/i.test(t[0]) ? t[0].toUpperCase() : t[0];
}

const losslessCount = (items) => (items || [])
  .filter((it) => (it.minfo || []).some((m) => m.format === 'flac')).length;

function songCardHtml(s) {
  return `<button class="card" data-act="card-play" data-id="${esc(s.id)}" data-platform="${esc(s.platform)}">
    <div class="card-art">${s.cover ? `<img src="${esc(s.cover)}" alt="" loading="lazy" onerror="this.remove()">` : phImg('song')}
      <span class="cplay">${icon('play')}</span>
    </div>
    <div class="card-name">${esc(s.name)}</div>
    <div class="card-sub">${esc(s.artist || '')}</div>
  </button>`;
}

function emptyHtml(ic, title, sub) {
  return `<div class="empty">
    <div class="eico">${phImg('empty')}</div>
    <b>${esc(title)}</b>
    <p>${esc(sub || '')}</p>
  </div>`;
}

/* ------------------------------------------------------------------ 视图 · 首页 */
const GREETS = ['夜深了', '早上好', '上午好', '中午好', '下午好', '傍晚好', '晚上好'];
function greeting() {
  const h = new Date().getHours();
  if (h < 5) return GREETS[0];
  if (h < 9) return GREETS[1];
  if (h < 11) return GREETS[2];
  if (h < 13) return GREETS[3];
  if (h < 17) return GREETS[4];
  if (h < 19) return GREETS[5];
  return GREETS[6];
}

/* ---------------------------------------------------- 本地推荐（信号层） */
/* 搬 Spotify 客户端能复现的部分：口味画像、显式反馈、seed 候选、预览后入列、
   可一键关闭。召回/排序在服务端，这里不做，也不连任何推荐服务。

   这个函数只负责「取回数据」，不负责画。原来它在请求前后各 render() 一次，
   等于每点一下整页重建两遍 —— 闪屏、滚动归零都是这么来的。
   画由调用方决定：首屏整页，换批只重画这一块货架。 */
async function loadRecommend(force, limit) {
  const want = limit || 12;
  if (state.settings.personalized === false) {
    state.reco = { list: [], seeds: [], disabled: true };
    state.recoLoading = false;
    state.recoError = false;
    return false;
  }
  if (!force && !limit && state.reco && state.reco.list && state.reco.list.length) return false;
  state.recoLoading = true;
  state.recoError = false;
  try {
    const [t, r] = await Promise.all([
      getJson('/api/taste'),
      getJson('/api/recommend?limit=' + want + (force ? '&refresh=1' : '')),
    ]);
    if (t && t.data) state.taste = t.data;
    if (r && r.ok === false) throw new Error(r.error || '推荐接口没有返回数据');
    state.reco = (r && r.data) || { list: [], seeds: [] };
  } catch (e) {
    state.recoError = true;
    if (!(state.reco && state.reco.list && state.reco.list.length)) {
      state.reco = { list: [], seeds: [], disabled: false };
    }
  }
  state.recoLoading = false;
  probeRows((state.reco && state.reco.list) || []);
  return true;
}

/* ---------------------------------------------------- 我的统计（纯只读） */
/* 数据全在后端聚合（/api/stats/summary），这里只取和画。
   听完一首统计就变了：refreshStatsQuiet 在首页时静默重取，只重画统计卡。 */
async function loadStats() {
  state.statsLoading = true;
  try {
    const r = await getJson('/api/stats/summary');
    state.stats = (r && r.data) || null;
  } catch (e) {
    state.stats = state.stats || null;      // 失败保留旧数据，不清空
  }
  state.statsLoading = false;
}

function refreshStatsQuiet() {
  if (!$('#shelfStats')) return;            // 不在首页就不取
  loadStats().then(() => paintStats());
}

function fmtHours(sec) {
  const h = (Number(sec) || 0) / 3600;
  return h >= 100 ? Math.round(h) + ' h' : (Math.round(h * 10) / 10) + ' h';
}

function statsShelfHtml() {
  const s = state.stats;
  if (!s && state.statsLoading) {
    return `<section class="shelf" id="shelfStats"><div class="shelf-head"><h2>我的统计</h2></div>
      <div class="stats-grid">${Array.from({ length: 4 }, () => '<div class="skel stats-skel"></div>').join('')}</div></section>`;
  }
  if (!s || s.empty) {
    return `<section class="shelf" id="shelfStats">
      <div class="shelf-head"><h2>我的统计</h2></div>
      <div class="stats-empty">听完的歌会记进统计 · 数据只在本地</div></section>`;
  }
  const maxN = Math.max(1, ...s.by_day.map((d) => d.n));
  const maxH = Math.max(1, ...s.hours);
  return `<section class="shelf" id="shelfStats">
    <div class="shelf-head"><h2>我的统计</h2></div>
    <div class="stats-grid">
      <div class="stat"><b class="mono">${s.plays_7d}</b><span>近 7 天收听</span></div>
      <div class="stat"><b class="mono">${fmtHours(s.total_seconds)}</b><span>累计时长</span></div>
      <div class="stat"><b class="mono">${s.listening_days}</b><span>累计天数</span></div>
      <div class="stat"><b class="mono">${s.favorites}</b><span>收藏</span></div>
    </div>
    <div class="stats-body">
      <div class="stats-block">
        <div class="stats-cap">近 30 天</div>
        <div class="stats-bars">${s.by_day.map((d) =>
          `<i style="height:${Math.max(3, Math.round((d.n / maxN) * 100))}%" title="${d.date} · ${d.n} 次"></i>`).join('')}</div>
      </div>
      <div class="stats-block">
        <div class="stats-cap">时段分布</div>
        <div class="stats-bars">${s.hours.map((n, h) =>
          `<i style="height:${Math.max(3, Math.round((n / maxH) * 100))}%" title="${String(h).padStart(2, '0')}:00 · ${n} 次"></i>`).join('')}</div>
      </div>
      ${s.top_artists.length ? `<div class="stats-block">
        <div class="stats-cap">最常听</div>
        <div class="stats-arts">${s.top_artists.slice(0, 8).map((a) =>
          `<button class="stats-art" data-act="artist" data-name="${esc(a.name)}" title="${esc(a.name)} · ${a.plays} 次">
            ${a.cover ? `<img src="${esc(a.cover)}" alt="" loading="lazy" onerror="this.remove()">` : ''}
            <span class="ell">${esc(a.name)}</span></button>`).join('')}</div>
      </div>` : ''}
    </div>
  </section>`;
}

function paintStats() {
  const host = $('#shelfStats');
  if (!host) return;
  const tpl = document.createElement('template');
  tpl.innerHTML = statsShelfHtml();
  const next = tpl.content.firstElementChild;
  if (!next) return;
  morphNode(host, next);
}

/* 推荐卡：整张卡点哪儿都能播（和「最近播放」一致），操作只留一套。
   之前是 div 不带 data-act（点不动）+ 右下角 cplay + 左下角三个圆钮，
   两套控件叠在一起，看起来像两种 UI。 */
function recoCardHtml(it, i) {
  const spec = qualityChip(it);
  return `<div class="card reco" role="button" tabindex="0" data-act="reco-play" data-i="${i}"
      data-pk="${esc(pk(it))}" data-pk-i="${i}" title="播放 ${esc(it.name || '')}">
    <div class="card-art">
      ${it.cover ? `<img src="${esc(it.cover)}" alt="" loading="lazy" onerror="this.remove()">` : phImg('song')}
      <div class="racts">
        <button data-act="reco-play" data-i="${i}" title="播放">${icon('play')}</button>
        <button data-act="reco-add" data-i="${i}" title="加入播放队列">${icon('queue')}</button>
        <button data-act="reco-like" data-i="${i}" title="喜欢，多推这类">${icon('heart-o')}</button>
        <button data-act="reco-dislike" data-i="${i}" title="不推荐这首">${icon('close')}</button>
      </div>
    </div>
    <div class="card-name">${esc(it.name || '')}</div>
    <div class="card-sub">${esc(it.artist || '')}</div>
    <div class="rmeta">${srcBadge(it)}<div class="rspec">${spec}</div></div>
  </div>`;
}

async function recoFeedback(verdict, idx) {
  const it = (state.reco.list || [])[idx];
  if (!it) return;
  try {
    await post('/api/feedback', {
      platform: it.platform, id: it.id, name: it.name, artist: it.artist, verdict,
    });
    toast(verdict === 'exclude' ? '已排除，不再推荐这首' : '记下了，会多推这类');
    // 就地抹掉这一张，别整页重画：反馈是「把这首拿掉」，不是「换一屏」
    state.reco.list.splice(idx, 1);
    paintReco();
  } catch (e) {
    toast('提交失败：' + e.message, 'err');
  }
}

/* 只重画「为你推荐」这一块。换成整页 render() 的话，
   下面的最近播放、我的歌手会跟着重建，封面全部重新解码 —— 满屏闪。 */
function paintReco() {
  const host = $('#shelfReco');
  if (!host) return;
  const tpl = document.createElement('template');
  tpl.innerHTML = recoShelfHtml();
  const next = tpl.content.firstElementChild;
  if (!next) return;
  morphNode(host, next);
  decorateShelves();
}

/* 「换一批」：按钮进忙碌态 → 取下一页 → 就地换卡片。
   后端把候选池留下来按窗口切，所以这一步基本是瞬时的；
   真正回上游时也不会整页空白，旧卡片留到新数据到为止。 */
/* 「正在换」的视觉过渡只在真的等久了才出现。
   换批在本机是纯切片（后端池子已经在了，一次 4~70ms 就回来），
   如果一按下就变灰转圈，用户看到的是页面「抖一下」——
   比什么都不做更像出了故障。140ms 大约是一次眨眼的量级，
   快过它就当无事发生，慢过它才让按钮转圈、内容变灰。 */
const SLOW_FEEDBACK_MS = 140;

function armSlowFeedback(pairs) {
  // pairs: [[元素, class], ...]，元素可能中途被换掉，回调里再确认一次
  return setTimeout(() => {
    pairs.forEach(([el, cls]) => {
      if (el && el.isConnected) el.classList.add(cls);
    });
  }, SLOW_FEEDBACK_MS);
}

function dropFeedback(pairs) {
  pairs.forEach(([el, cls]) => {
    if (el && el.isConnected) el.classList.remove(cls);
  });
}

async function refreshReco(btn) {
  if (state.recoLoading) return;
  const host = $('#shelfReco');
  const marks = [[btn, 'busy'], [host, 'swapping']];
  const arm = armSlowFeedback(marks);
  try {
    await loadRecommend(true, state.recoExpanded ? 26 : 12);
  } finally {
    clearTimeout(arm);
  }
  dropFeedback(marks);
  paintReco();
}

function recoShelfHtml() {
  const list = (state.reco && state.reco.list) || [];
  const seeds = (state.reco && state.reco.seeds) || [];
  const off = state.settings.personalized === false || (state.reco && state.reco.disabled);

  if (off) {
    return `<section class="shelf" id="shelfReco">
      <div class="shelf-head"><h2>为你推荐</h2>
        <button class="more" data-act="taste">去打开 ›</button></div>
      <div class="reco-off">个性化推荐已关闭 · 口味画像里可以重新打开</div></section>`;
  }
  if (!list.length && state.recoLoading) {
    return `<section class="shelf" id="shelfReco"><div class="shelf-head"><h2>为你推荐</h2></div>
      <div class="shelf-row">${Array.from({ length: 5 }, () => '<div class="skel reco-skel"></div>').join('')}</div></section>`;
  }
  if (!list.length) {
    // 分清楚「没信号」和「取失败」：前者给去处，后者才给重试。
    // 原来一律摆一个「重试」，没信号的人点了永远还是空 —— 那就是空壳。
    return `<section class="shelf" id="shelfReco">
      <div class="shelf-head"><h2>为你推荐</h2>
        ${state.recoError
          ? '<button class="more" data-act="reco-refresh">重试 ›</button>'
          : '<button class="more" data-act="taste">调口味 ›</button>'}</div>
      <div class="reco-off">${state.recoError
        ? '取推荐失败 · 音源这会儿可能连不上，稍后再试'
        : '还听不出你的口味 · 多听几首、收藏几个歌手，这里就有歌了'}</div></section>`;
  }
  return `<section class="shelf" id="shelfReco">
    <div class="shelf-head"><h2>为你推荐 <span class="cnt">${pad(list.length)}</span></h2>
      <div class="shelf-acts">
        <button class="more" data-act="taste">口味画像 ›</button>
        <button class="more" data-act="reco-expand">${state.recoExpanded ? '收起' : '显示全部'} ›</button>
        <button class="more" data-act="reco-refresh">换一批 ›</button>
      </div></div>
    ${seeds.length ? `<div class="shelf-sub">按 ${seeds.slice(0, 3).map((s) => esc(s)).join(' · ')} 的口味 · 来自你已有的音源</div>` : ''}
    <div class="shelf-row">${list.map(recoCardHtml).join('')}</div>
  </section>`;
}

/* --------------------------------------------------------- 全风格卡片歌单 */
/* SP 的「全风格」入口：一组风格卡片，每张卡是这个风格的真实曲目。
   卡片内容按用户口味加权 + 当日轮换生成（后端 /api/styles）。
   force=true 是「换一批歌」：后端按轮次换每张卡里取的歌，
   卡片集合一天内稳定 —— 所以换批只动卡片内部，不重新打上游。 */
async function loadStyles(force) {
  if (!force && state.styles && state.styles.cards && state.styles.cards.length) return false;
  state.styleLoading = true;
  state.styleError = false;
  try {
    const r = await getJson('/api/styles?cards=12&per=6' + (force ? '&refresh=1' : ''));
    if (r && r.ok === false) throw new Error(r.error || '风格歌单接口没有返回数据');
    state.styles = (r && r.data) || { cards: [] };
    if (!state.genreList && state.styles.genres) {
      const gs = state.styles.genres;
      const groups = [];
      gs.forEach((g) => { if (!groups.includes(g.group)) groups.push(g.group); });
      state.genreList = { groups, genres: gs };
    }
  } catch (e) {
    state.styleError = true;
    if (!(state.styles && state.styles.cards && state.styles.cards.length)) {
      state.styles = { cards: [] };
    }
  }
  state.styleLoading = false;
  return true;
}

function styleCardHtml(c) {
  const list = c.list || [];
  const art = list.find((x) => x.cover);
  const cover = art ? art.cover : '';
  return `<section class="stylecard" data-act="style-play" data-g="${esc(c.genre)}">
    <div class="stylecard-top">
      <span class="stylecard-art">${cover
        ? `<img src="${esc(cover)}" alt="" loading="lazy" onerror="this.remove()">` : phImg('album')}</span>
      <div class="stylecard-meta">
        <div class="stylecard-name">${esc(c.name)}</div>
        <div class="stylecard-en">${esc(c.en)} · ${esc(c.group)}</div>
        <div class="stylecard-sub">${list.length} 首</div>
      </div>
      <button class="stylecard-play" data-act="style-play" data-g="${esc(c.genre)}" title="播放这一组">${icon('play')}</button>
    </div>
    <div class="stylecard-list">${list.map((it) => `
      <div class="stylecard-row" data-act="style-one" data-g="${esc(c.genre)}" data-i="${list.indexOf(it)}">
        <span class="ell">${esc(it.name || '')}</span>
        <span class="ell sca-artist">${esc(it.artist || '')}</span>
      </div>`).join('')}</div>
  </section>`;
}

/* 卡片区单独抽出来：换批只替换这一块，标题和按钮原地不动。 */
function styleGridInner() {
  const cards = (state.styles && state.styles.cards) || [];
  if (state.styleLoading && !cards.length) {
    // 首次进这个页面要回上游按风格抓歌（几十个请求并发），冷启动通常几秒。
    // 骨架按最终网格密度铺 8 格，别只铺 4 格 —— 卡片到位时布局不会整块跳一下；
    // 再补一行说明，用户至少知道在等什么，而不是盯着灰块怀疑卡死。
    return `<div class="skel stylecard-skel"></div>`.repeat(8)
      + `<p class="grid-note">正在从音源按风格取歌 · 第一次会慢一些，之后就是秒开</p>`;
  }
  if (!cards.length) {
    return `<div class="reco-off style-empty">${state.styleError
      ? '取风格歌单失败 · 音源这会儿可能连不上，点上面的「换一批歌」再试'
      : '上游这次没返回可用的曲目 · 换一批试试，或者过会儿再来'}</div>`;
  }
  return cards.map(styleCardHtml).join('');
}

function viewStyles() {
  return `<div class="page">
    <div class="vhead taste-head">
      <div class="kicker">ALL STYLES · LOCAL ONLY</div>
      <h1>全风格歌单</h1>
      <p class="sub">每个卡片是一种风格 · 卡片按你的口味加权、每天轮换</p>
    </div>
    <div class="vhead-acts" style="margin-bottom:14px">
      <button class="btn" data-act="style-refresh">${icon('retry')}换一批歌</button>
      <button class="btn ghost" data-act="taste">口味画像 ›</button>
    </div>
    <div class="stylecards" id="styleGrid">${styleGridInner()}</div>
  </div>`;
}

function paintStyles() {
  const grid = $('#styleGrid');
  if (!grid) return;
  const tpl = document.createElement('template');
  tpl.innerHTML = styleGridInner();
  morphChildren(grid, tpl.content);
}

async function refreshStyles(btn) {
  if (state.styleLoading) return;
  const grid = $('#styleGrid');
  const marks = [[btn, 'busy'], [grid, 'swapping']];
  const arm = armSlowFeedback(marks);          // 同上：快就不闪，慢才转圈
  let ok = false;
  try {
    ok = await loadStyles(true);
  } finally {
    clearTimeout(arm);
  }
  dropFeedback(marks);
  paintStyles();
  if (ok && !state.styleError) toast('已换一批歌');
}

/* ------------------------------------------------------------- 口味画像页 */
async function loadTaste() {
  try {
    const [t, g] = await Promise.all([
      getJson('/api/taste'),
      state.genreList ? Promise.resolve({ data: state.genreList }) : getJson('/api/genres'),
    ]);
    state.taste = t.data || {};
    if (g && g.data) state.genreList = g.data;
    if (state.route.name === 'taste') render();
  } catch (e) { /* 静默，保留上一次画像 */ }
}

function tasteTags(arr, cls, label) {
  if (!arr || !arr.length) return '';
  return `<div class="taste-tags"><span class="kicker">${label}</span>${arr.map(
    (x) => `<span class="tag ${cls}">${esc(typeof x === 'string' ? x : (x.name || ''))}</span>`
  ).join('')}</div>`;
}

/* 风格选择器：固定分类多选，不让用户打字自述。
   分类来自后端 /api/genres（本地内置的中日+欧美主流风格表）。 */
function genrePickerHtml() {
  const gs = (state.genreList && state.genreList.genres) || [];
  if (!gs.length) return '<div class="reco-off">风格分类加载中…</div>';
  const picked = new Set(state.taste.genres || []);
  const groups = state.genreList.groups || [];
  const byGroup = new Map();
  gs.forEach((g) => {
    if (!byGroup.has(g.group)) byGroup.set(g.group, []);
    byGroup.get(g.group).push(g);
  });
  return groups.map((grp) => {
    const items = byGroup.get(grp) || [];
    if (!items.length) return '';
    return `<div class="gpick-grp">
      <div class="gpick-grp-t">${esc(grp)}</div>
      <div class="gpick-row">${items.map((g) => `
        <button class="gpick${picked.has(g.id) ? ' on' : ''}" data-act="gpick" data-g="${esc(g.id)}"
          title="${esc(g.name)} · ${esc(g.en)}">${esc(g.name)}</button>`).join('')}</div>
    </div>`;
  }).join('');
}

function viewTaste() {
  const t = state.taste || {};
  const arts = t.artists || [];
  // 条形按真实收听次数画；没听过但收藏过的歌手不占条，排在后面
  const max = Math.max(1, ...arts.map((a) => Number(a.plays) || 0));
  // 只列前 10 位：画像里能有几十个歌手，全铺出来这张卡会长到离谱，
  // 右边的风格选择器反而看不见了。剩下的给一行说明。
  const top = arts.slice(0, 10);
  // taste_profile 一定会带 listened —— 没有这个键说明还没取回来。
  // 不然首次进页面会先摆一屏「还没有口味信号」，再换成真数据。
  const loaded = 'listened' in t;
  const rows = !loaded
    ? Array.from({ length: 4 }, () => '<div class="skel taste-skel"></div>').join('')
    : top.length ? top.map((a, i) => `
    <div class="taste-artist">
      <span class="taste-rank mono">${pad(i + 1)}</span>
      <span class="taste-av round">${a.cover
        ? `<img src="${esc(a.cover)}" alt="" onerror="this.remove()">` : phImg('artist')}</span>
      <span class="taste-an ell">${esc(a.name || '')}</span>
      <span class="taste-bar"><i style="width:${Math.round((Number(a.plays) || 0) / max * 100)}%"></i></span>
      <span class="mono taste-score">${a.plays || 0}</span>
    </div>`).join('')
    : '<div class="reco-off">还没有口味信号 · 多听几首、收藏几个歌手就有了</div>';

  const picked = (t.genres || []).length;
  const more = Math.max(0, arts.length - top.length);

  return `<div class="page">
    <div class="vhead taste-head">
      <div class="kicker">TASTE PROFILE · LOCAL ONLY</div>
      <h1>口味画像</h1>
      <p class="sub">只依据本机的收藏、播放与反馈生成 · 不上传、不依赖任何账号</p>
    </div>
    <div class="taste-grid">
      <section class="taste-card">
        <div class="taste-head"><h2>常听歌手</h2>
          <span class="cnt">${pad(arts.length)}</span></div>
        <p class="rule-note">数字是听完的次数 · 换音质不重复计</p>
        ${rows}
        ${more ? `<p class="taste-more">还有 ${more} 位没列出来</p>` : ''}
        <div class="taste-stats">
          <span><b class="mono">${t.listened || 0}</b>次收听</span>
          <span><b class="mono">${t.songs || 0}</b>首曲目</span>
          <span><b class="mono">${t.favorites || 0}</b>项收藏</span>
        </div>
      </section>
      <section class="taste-card">
        <div class="taste-head"><h2>我喜欢的风格</h2>
          <span class="cnt">${pad(picked)}</span></div>
        <p class="rule-note">点一下就是选中 · 选中的风格会优先出现在「全风格歌单」</p>
        <div class="gpick-box" id="genrePicker">${genrePickerHtml()}</div>
        ${tasteTags(t.liked, 'good', '喜欢的')}
        ${tasteTags(t.excluded, 'bad', '已排除')}
      </section>
    </div>
    <section class="taste-card wide">
      <div class="taste-head"><h2>个性化推荐</h2></div>
      <label class="taste-switch">
        <input type="checkbox" class="switch" id="tasteEnabled" data-act="taste-toggle" ${t.enabled === false ? '' : 'checked'}/>
        <span>生成首页「为你推荐」<span class="tip">关掉后首页不出推荐，本地画像与反馈仍保留</span></span>
      </label>
      <div class="taste-acts">
        <button class="btn sm ghost" data-act="feedback-clear">清除全部反馈</button>
        <button class="btn sm" data-act="nav" data-view="home">返回首页</button>
      </div>
    </section>
  </div>`;
}

/* 首页快捷磁贴：只放侧栏里没有的入口，固定 4 格一行。
   原来的问题是「最近播放先占 2 格 + 功能入口补到 6 格」：
   一有播放记录，排最后的「口味画像」就被挤没了 —— 入口会自己消失。
   现在功能入口是固定集合，磁贴不随播放历史变化；最近播放下面有独立货架，
   不需要再在磁贴里重来一遍。搜索框在顶栏、收藏本和设置都在侧栏，也不用重复。 */
function shortcutTilesHtml() {
  const feats = [
    { label: '全风格', ic: 'folder', act: 'tile-styles' },
    { label: '口味画像', ic: 'mic', act: 'taste' },
    { label: '收藏本', ic: 'lib', act: 'tile-lib' },
  ];
  return `<div class="tiles">${feats.map((e) => `
    <button class="tile fn" data-act="${e.act}">
      <span class="tile-art">${icon(e.ic)}</span>
      <span class="tile-name ell">${e.label}</span>
      <span class="tile-ic">${icon('fwd')}</span>
    </button>`).join('')}</div>`;
}

function viewHome() {
  const arts = favsOf('artist');
  const albs = favsOf('album');
  const parts = [];

  parts.push(`<div class="vhead home-head">
    <div class="kicker">ELYSIUM</div>
    <h1>${greeting()}</h1>
    <p class="sub">${state.recent.length
      ? `最近听了 <span class="mono">${state.recent.length}</span> 首，收藏 <span class="mono">${state.favs.length}</span> 项`
      : '搜索一首歌，或者直接粘专辑链接'}</p>
  </div>`);

  parts.push(shortcutTilesHtml());

  const lt = pickLastTrack();
  if (lt && lt.item && lt.item.id) {
    const it = lt.item;
    const dur = Number(it.duration) || 0;
    const pos = Math.max(0, lt.position || 0);
    const pct = dur ? Math.min(100, (pos / dur) * 100) : 0;
    parts.push(`<div class="cont">
      <div class="cont-art">${it.cover
        ? `<img src="${esc(it.cover)}" alt="" onerror="this.remove()">` : phImg('song')}</div>
      <div class="cont-main">
        <div class="kicker">CONTINUE</div>
        <div class="cont-name">${esc(it.name || '')}</div>
        <div class="cont-sub">${esc(it.artist || '')}${dur
          ? ` · 听到 <span class="mono">${fmtTime(pos)}</span> / <span class="mono">${fmtTime(dur)}</span>` : ''}</div>
        <div class="cont-bar"><i style="width:${pct.toFixed(1)}%"></i></div>
      </div>
      <button class="cont-play" data-act="resume" title="继续播放">${icon('play')}</button>
    </div>`);
  }

  parts.push(recoShelfHtml());

  if (state.recent.length) {
    parts.push(`<section class="shelf">
      <div class="shelf-head"><h2>最近播放 <span class="cnt">${pad(state.recent.length)}</span></h2>
        <div class="shelf-acts">
          <button class="more" data-act="recent-expand">${state.recentExpanded ? '收起' : '全部'} ›</button>
          <button class="more" data-act="recent-clear">清除记录</button>
        </div></div>
      <div class="shelf-row">${state.recent.slice(0, state.recentExpanded ? 60 : 14).map(songCardHtml).join('')}</div>
    </section>`);
  }

  if (arts.length) {
    parts.push(`<section class="shelf">
      <div class="shelf-head"><h2>我的歌手 <span class="cnt">${pad(arts.length)}</span></h2>
        <button class="more" data-act="nav" data-view="library" data-tab="artist">全部 ›</button></div>
      <div class="shelf-row">${arts.slice(0, 14).map((a) => artistCardHtml({ ...a, count: a.count })).join('')}</div>
    </section>`);
  }

  if (albs.length) {
    parts.push(`<section class="shelf">
      <div class="shelf-head"><h2>我的专辑 <span class="cnt">${pad(albs.length)}</span></h2>
        <button class="more" data-act="nav" data-view="library" data-tab="album">全部 ›</button></div>
      <div class="shelf-row">${albs.slice(0, 14).map((a) => albumCardHtml(a)).join('')}</div>
    </section>`);
  }

  parts.push(statsShelfHtml());

  if (!state.recent.length && !arts.length && !albs.length) {
    parts.push(`<div class="welcome">
      <div class="wico">${icon('spark')}</div>
      <h2>把喜欢的听回来</h2>
      <p>酷我与网易云双音源，无损直取。收藏的歌手和专辑会归到左侧，点开就是他们的全部作品。</p>
      <div class="chips">
        ${['周杰伦', '陈奕迅', '邓紫棋', '五月天', '李荣浩', '毛不易']
          .map((k) => `<button data-act="quick" data-kw="${esc(k)}">${esc(k)}</button>`).join('')}
      </div>
    </div>`);
  } else {
    parts.push(`<section class="shelf">
      <div class="shelf-head"><h2>换点别的听</h2></div>
      <div class="chips" style="justify-content:flex-start">
        ${['周杰伦', '陈奕迅', '邓紫棋', '五月天', '林俊杰', '毛不易', '张学友', '李荣浩']
          .map((k) => `<button data-act="quick" data-kw="${esc(k)}">${esc(k)}</button>`).join('')}
      </div>
    </section>`);
  }

  return `<div class="page">${parts.join('')}</div>`;
}

/* ------------------------------------------------------------------ 视图 · 搜索 */
function viewSearch() {
  const { keyword } = state.q;
  const tab = state.tabs.search;
  const all = state.items;
  const items = all;
  const artists = groupArtists(items);
  const albums = groupAlbums(items);

  const head = `<div class="vhead vhead-row">
    <div>
      <div class="kicker">SEARCH</div>
      <h1>${keyword ? esc(keyword) : '搜索'}</h1>
      <p class="sub">${keyword
        ? `${items.length} 首${state.q.merged ? ' · 已合并重复曲目' : ''}`
        : '输入歌名、歌手、专辑，或粘贴链接'}</p>
    </div>
    <div class="vhead-acts">
      ${items.length ? `<button class="btn primary" data-act="play-all">${icon('play')}播放全部</button>
      <button class="btn" data-act="dl-all">${icon('dl')}全部下载</button>` : ''}
    </div>
  </div>`;

  if (!keyword) {
    return `<div class="page">${head}${emptyHtml('search', '还没有搜索', '试试「夜曲」「陈奕迅」，或贴一条专辑链接')}</div>`;
  }

  if (state.loading && !items.length) {
    return `<div class="page">${head}<div class="stabs">
      <button class="stab on">歌曲</button><button class="stab">歌手</button><button class="stab">专辑</button>
    </div>${Array.from({ length: 6 }, () => '<div class="skel"></div>').join('')}</div>`;
  }

  const tabs = `<div class="stabs">
    <button class="stab${tab === 'song' ? ' on' : ''}" data-act="stab" data-tab="song">歌曲 <em>${items.length}</em></button>
    <button class="stab${tab === 'artist' ? ' on' : ''}" data-act="stab" data-tab="artist">歌手 <em>${artists.length}</em></button>
    <button class="stab${tab === 'album' ? ' on' : ''}" data-act="stab" data-tab="album">专辑 <em>${albums.length}</em></button>
  </div>`;

  // 某个源对这个词一条都不给时，说明一下，别让人以为是客户端漏了
  const emptyNote = (state.q.empty || []).length
    ? `<p class="rule-note">${state.q.empty.map((p) => srcOf({ platform: p }).label).join('、')} 没有收录这个词的结果，以下来自其余来源</p>`
    : '';

  let body = '';
  if (tab === 'song') {
    body = trackListHtml(items, { empty: '换个关键词，或粘贴专辑 / 歌单链接' })
      + (items.length && state.q.more
        ? `<div style="text-align:center;padding:16px 0"><button class="btn sm" data-act="more">加载更多</button></div>`
        : '');
  } else if (tab === 'artist') {
    body = `<p class="rule-note">以下为本次结果中出现的<b>记名演唱者</b>，只按歌曲署名归类；仅为作词 / 作曲 / 编曲参与的不并入，也不按搜索词合并。</p>`
      + (artists.length
        ? `<div class="cards">${artists.map(artistCardHtml).join('')}</div>`
        : emptyHtml('mic', '没有识别出歌手', '试试直接搜歌手名字'));
  } else {
    body = albums.length
      ? `<div class="cards">${albums.map((a) => albumCardHtml(a)).join('')}</div>`
      : emptyHtml('disc', '没有识别出专辑', '当前结果里缺少专辑信息');
  }

  return `<div class="page">${head}${tabs}${emptyNote}${body}</div>`;
}

/* 折叠口径，与后端 fold_items 保持一致：曲名主干 + 主艺人 + 时长接近。 */
const FOLD_HINT = ['主题曲', '主题歌', '片头曲', '片尾曲', '插曲', '印象曲', '推广曲',
  '宣传曲', '原声', 'ost', 'live', '现场', '演唱会', '伴奏', 'instrumental',
  '翻唱', 'cover', 'remix', 'demo', '版', 'feat', '原唱', '演奏'];

function coreTitle(name) {
  const s = String(name == null ? '' : name).normalize('NFKC').trim()
    .replace(/（/g, '(').replace(/）/g, ')');
  const m = s.match(/^([\s\S]*?)\s*[-–—]\s*([\s\S]*)$/);
  if (m) {
    const head = m[1].trim();
    const tail = m[2].trim();
    const low = tail.toLowerCase();
    if (head && (tail.startsWith('《') || FOLD_HINT.some((h) => low.includes(h)))) return head;
  }
  return s;
}

const foldText = (s) => String(s == null ? '' : s).normalize('NFKC').toLowerCase()
  .replace(/[\s\u3000·・,，、。.!！?？'"“”‘’()（）[\]【】《》〈〉<>~\-_/&+*#:：;；|]+/g, '');

const mainArtist = (s) => foldText(String(s == null ? '' : s)
  .split(/\s*(?:\/|&|、|,|，|;|；|\bfeat\b|\bft\b|\bwith\b)\s*/i)[0]);

const albKey = (name, artist) => `${foldText(coreTitle(name))}::${mainArtist(artist)}`;

const foldKey = (it) => `${foldText(coreTitle(it && it.name))}::${mainArtist(it && it.artist)}`;
const durNear = (a, b) => !a || !b || Math.abs(a - b) <= 4;

/* 加载更多时把新一页并进已有结果：同一首只留一行，另一个源进 alts。
   后端只折叠「一页之内」，跨页的重复得在这里收口。 */
function expandFolded(items) {
  const out = [];
  (items || []).forEach((it) => {
    const copy = Object.assign({}, it);
    delete copy.alts;
    out.push(copy);
    (it.alts || []).forEach((a) => out.push(Object.assign({}, a)));
  });
  return out;
}

function foldRows(rows) {
  const groups = [];
  const index = new Map();
  rows.forEach((it) => {
    const k = foldKey(it);
    if (!index.has(k)) index.set(k, []);
    const bucket = index.get(k);
    let hit = null;
    for (const gi of bucket) {
      if (durNear(groups[gi].rep.duration, it.duration)) { hit = groups[gi]; break; }
    }
    if (hit) { hit.alts.push(it); return; }
    groups.push({ rep: it, alts: [] });
    bucket.push(groups.length - 1);
  });
  return groups.map(({ rep, alts }) => {
    let keep = rep;
    let rest = alts;
    const same = [rep].concat(alts).filter((x) => x.platform === 'kuwo');
    if (same.length && same[0] !== rep) {
      keep = same[0];
      rest = [rep].concat(alts).filter((x) => x !== same[0]);
    }
    if (!rest.length) return keep;
    const out = Object.assign({}, keep);
    out.alts = rest;
    return out;
  });
}

/* 行内换源：同一首在另一源也有 → 就地换掉这一行，不新增行。
   换下去的版本留在 alts 里，再点还能换回来。 */
function altswap(idx) {
  const arr = state.list;
  const cur = arr && arr[idx];
  if (!cur || !cur.alts || !cur.alts.length) return;
  const alts = cur.alts.slice();
  const next = alts.shift();
  const prev = {};
  ['platform', 'id', 'name', 'artist', 'artist_id', 'album_id', 'album_name',
    'duration', 'cover', 'pic_url', 'minfo', 'time', 'sign'].forEach((k) => { prev[k] = cur[k]; });
  const merged = Object.assign({}, next, { alts: alts.concat([prev]) });
  arr[idx] = merged;
  render();
  toast(`已切到 ${srcOf(merged).label}`);
}


/* 归类口径：仅以记名歌手归。
   上游搜索是关键词相关性召回，搜「周杰伦」会带出他作词作曲给别人唱的歌
   （蔡依林《布拉格广场》、李玟《刀马旦》）和综艺学员翻唱。这些歌的
   artist / artist_id 是真实演唱者，必须按它归到本人名下，绝不按搜索词归。 */
const artistKey = (it) => {
  const aid = String(it.artist_id || '').trim();
  return aid ? `${it.platform}:id:${aid}` : `${it.platform}:name:${String(it.artist || '').trim()}`;
};

function groupArtists(items) {
  const map = new Map();
  items.forEach((it) => {
    const name = String(it.artist || '').trim();
    if (!name) return;
    const key = artistKey(it);
    if (!map.has(key)) {
      map.set(key, {
        name, artist_id: String(it.artist_id || '').trim(),
        count: 0, cover: it.cover, platform: it.platform,
      });
    }
    map.get(key).count += 1;
  });
  return [...map.values()].sort((a, b) => b.count - a.count);
}

function groupAlbums(items) {
  const map = new Map();
  items.forEach((it) => {
    const id = String(it.album_id || '');
    const name = it.album_name || '';
    if (!id || !name) return;
    // 同一张专辑在两个源各有一个 id：按「专辑名 + 主艺人」归一，只留一张卡
    const key = albKey(name, it.artist);
    const hit = map.get(key);
    if (!hit) {
      map.set(key, {
        id, name, cover: it.cover, artist: it.artist,
        artist_id: String(it.artist_id || '').trim(),
        platform: it.platform, count: 1, alts: [],
      });
      return;
    }
    if (it.platform === hit.platform) { hit.count += 1; return; }
    if (it.platform === 'kuwo' && hit.platform !== 'kuwo') {
      hit.alts.push({ id: hit.id, platform: hit.platform });   // 旧的那个降为备选
      hit.id = id;
      hit.platform = 'kuwo';
      hit.cover = it.cover || hit.cover;
    } else {
      hit.alts.push({ id, platform: it.platform });
    }
  });
  return [...map.values()];
}

/* ------------------------------------------------------------------ 视图 · 歌手 */
/* 曲目一次抓全（后端双源深翻页），这里只按需分批画：
   几百行一次性塞进 DOM 会卡住主线程，实测规格也会被塞满队列。
   所以「歌曲 / 专辑」做成两个 tab，而不是专辑堆在曲目上面逼人往下滑。 */
function viewArtist() {
  const d = state.artist;
  if (!d) return `<div class="page">${emptyHtml('mic', '正在取这位歌手的全部作品', '两个来源一起翻页，歌多的会多等几秒')}</div>`;
  const all = d.list || [];
  const albums = d.albums || [];
  const tab = state.tabs.artist;
  const shown = Math.min(state.artistShown, all.length);
  const songs = all.slice(0, shown);
  const albumsShown = Math.min(state.artistAlbumShown, albums.length);
  const cover = (all[0] || {}).cover || (albums[0] || {}).cover || '';
  const fav = isFav('artist', d.name, d.platform);
  const plats = (d.platforms && d.platforms.length ? d.platforms : [d.platform]);

  const hero = `<div class="hero">
    <div class="hero-art round">${cover
      ? `<img src="${esc(cover)}" alt="" onerror="this.remove()">`
      : phImg('artist')}</div>
    <div class="hero-meta">
      <div class="kicker">ARTIST</div>
      <h1>${esc(d.name)}</h1>
      <div class="hero-spec">
        <span class="mono">${all.length}</span> 首
        <span class="sep"></span>
        <span class="mono">${albums.length}</span> 张专辑
        <span class="sep"></span>
        无损 <span class="mono">${losslessCount(all)}</span> 首
        <span class="sep"></span>
        ${plats.map((p) => srcOf({ platform: p }).label).join(' · ')}
        ${state.artistFilling ? '<span class="sep"></span><span class="mono" style="opacity:.6">正在补齐全部曲目…</span>' : ''}
      </div>
      <p class="rule-note">归类口径：仅以上游记名歌手为准，仅作词 / 作曲 / 编曲参与的不归入${d.reason ? ` · ${esc(d.reason)}` : ''}</p>
      <div class="hero-acts">
        <button class="btn primary" data-act="artist-play-all">${icon('play')}播放热门</button>
        <button class="btn${fav ? ' on' : ''}" data-act="fav-artist">${icon(fav ? 'heart' : 'heart-o')}${fav ? '已收藏' : '收藏歌手'}</button>
        <button class="btn" data-act="dl-artist">${icon('dl')}全部下载</button>
      </div>
    </div>
  </div>`;

  const tabs = `<div class="stabs">
    <button class="stab${tab === 'song' ? ' on' : ''}" data-act="atab" data-tab="song">歌曲 <em>${all.length}</em></button>
    <button class="stab${tab === 'album' ? ' on' : ''}" data-act="atab" data-tab="album">专辑 <em>${albums.length}</em></button>
  </div>`;

  let body = '';
  if (tab === 'album') {
    body = albums.length
      ? `<div class="cards">${albums.slice(0, albumsShown).map((a) => albumCardHtml(a)).join('')}</div>`
        + (albumsShown < albums.length
          ? `<div class="morewrap"><button class="btn sm" data-act="artist-more" data-kind="album">再展开 ${Math.min(ARTIST_ALBUM_PAGE, albums.length - albumsShown)} 张 ›</button></div>`
          : '')
      : emptyHtml('disc', '没有识别出专辑', d.reason || '上游没给这张专辑信息');
  } else {
    body = all.length
      ? trackListHtml(songs, { hideAlbum: false })
        + (shown < all.length
          ? `<div class="morewrap"><button class="btn sm" data-act="artist-more" data-kind="song">还有 ${all.length - shown} 首 · 继续展开 ›</button></div>`
          : '')
      : emptyHtml('mic', '没有记名到这位歌手的作品', d.reason || '两个来源都没收录');
  }

  return `<div class="page">${hero}${tabs}${body}</div>`;
}

/* ------------------------------------------------------------------ 视图 · 专辑 */
function viewAlbum() {
  const d = state.album;
  if (!d) return `<div class="page">${emptyHtml('disc', '加载中', '正在取专辑曲目')}</div>`;
  const info = d.info || {};
  const list = d.list || [];
  const cover = info.pic_url || (list[0] || {}).cover || '';
  const artist = info.artist || (list[0] || {}).artist || '';
  const fav = isFav('album', info.id, d.platform);
  const total = list.reduce((n, it) => n + (it.duration || 0), 0);

  const hero = `<div class="hero">
    <div class="hero-art">${cover ? `<img src="${esc(cover)}" alt="" onerror="this.remove()">` : phImg('album')}</div>
    <div class="hero-meta">
      <div class="kicker">ALBUM</div>
      <h1>${esc(info.name || '专辑')}</h1>
      <div class="hero-spec">
        ${artist ? `<button data-act="artist" data-name="${esc(artist)}" data-platform="${esc(d.platform)}" data-aid="${esc(String(info.artist_id || (list[0] || {}).artist_id || '').trim())}" style="color:var(--ink-2)">${esc(artist)}</button><span class="sep"></span>` : ''}
        <span class="mono">${list.length}</span> 首
        <span class="sep"></span>
        <span class="mono">${fmtTime(total)}</span>
        <span class="sep"></span>
        无损 <span class="mono">${losslessCount(list)}</span> 首
        <span class="sep"></span>
        ${d.platform === 'kuwo' ? '酷我' : '网易云'}
      </div>
      <div class="hero-acts">
        <button class="btn primary" data-act="play-all">${icon('play')}播放专辑</button>
        <button class="btn${fav ? ' on' : ''}" data-act="fav-album">${icon(fav ? 'heart' : 'heart-o')}${fav ? '已收藏' : '收藏专辑'}</button>
        <button class="btn" data-act="dl-all">${icon('dl')}整张下载</button>
      </div>
    </div>
  </div>`;

  const desc = info.description
    ? `<p class="sub" style="margin:-8px 0 20px;max-width:760px;color:var(--muted);font-size:12.5px;line-height:1.7">${esc(info.description).slice(0, 320)}</p>`
    : '';

  return `<div class="page">${hero}${desc}${trackListHtml(list, { hideAlbum: true, empty: '这张专辑没有曲目' })}</div>`;
}

/* ------------------------------------------------------------------ 视图 · 本地曲库 */
/* 最小可用版：只扫下载目录，最新下载的排最前。点行直放本地文件，
   收听计数照走 /api/play（platform=local 只计数、不进在线缓存）。 */
async function loadLocal(force) {
  const L = state.local;
  if (L.loading) return;
  L.loading = true;
  L.error = '';
  if (state.route.name === 'local') render();
  try {
    const r = await getJson('/api/local/list' + (force ? '?refresh=1' : ''));
    if (!(r && r.ok)) throw new Error((r && r.error) || '扫描失败');
    L.items = r.data.items || [];
    L.dir = r.data.dir || '';
    L.roots = r.data.roots || (r.data.dir ? [r.data.dir] : []);
    L.scanning = r.data.scanning || null;
    L.loaded = true;
  } catch (e) {
    L.loaded = true;
    L.error = e.message || '扫描失败';
  }
  L.loading = false;
  const c = $('#localCount');
  if (c) c.textContent = L.items.length || '';
  if (state.route.name === 'local') render();
  pollLocalScan();
}

/* 大曲库：后端先把文件名列表秒级吐回来，标签在后台补。
   这里轮询进度，只改头部那行字 —— 整页重绘会把人滚动到一半的列表弹回顶部。 */
let localPollTimer = null;
function pollLocalScan() {
  const L = state.local;
  if (localPollTimer) { clearTimeout(localPollTimer); localPollTimer = null; }
  const sc = L.scanning;
  if (!sc || !sc.running) return;
  localPollTimer = setTimeout(async () => {
    try {
      const r = await getJson('/api/local/list');
      if (r && r.ok) {
        L.items = r.data.items || [];
        L.roots = r.data.roots || L.roots;
        L.scanning = r.data.scanning || null;
        const c = $('#localCount');
        if (c) c.textContent = L.items.length || '';
        const sub = $('#localSub');
        if (sub && L.scanning && L.scanning.running) {
          sub.textContent = `正在读取标签 ${L.scanning.done} / ${L.scanning.total}…`;
        } else if (state.route.name === 'local') {
          render();
        }
      }
    } catch (e) { /* 下一轮再试 */ }
    pollLocalScan();
  }, 900);
}

function viewLocal() {
  const L = state.local;
  if (!L.loaded && !L.loading) loadLocal();
  const rootsN = (L.roots || []).length;
  const sc = L.scanning;
  const head = `<div class="vhead vhead-row">
    <div>
      <div class="kicker">LOCAL LIBRARY</div>
      <h1>本地音乐</h1>
      <p class="sub" id="localSub">${L.loading
        ? '正在扫描曲库…'
        : L.error
          ? `扫描失败：${esc(L.error)}`
          : sc && sc.running
            ? `正在读取标签 ${sc.done} / ${sc.total}…`
            : L.items.length
              ? `<span class="mono">${L.items.length}</span> 个本地文件 · ${rootsN > 1 ? rootsN + ' 个目录' : '下载目录'}`
              : rootsN > 1 ? '这些目录里还没有音频文件' : '下载目录里还没有音频文件'}</p>
    </div>
    <div class="vhead-acts">
      <button class="btn" data-act="local-refresh" title="重新扫描曲库目录">${icon('retry')}重新扫描</button>
      ${L.items.length ? `<button class="btn primary" data-act="local-playall">${icon('play')}播放全部</button>` : ''}
    </div>
  </div>`;
  let body;
  if (L.loading && !L.items.length) body = emptyHtml('disc', '正在扫描…', '读取曲库目录里的音频文件');
  else if (L.error && !L.items.length) body = emptyHtml('folder', '扫不到这个目录', '可以在「设置 → 本地曲库」里换一个目录再试');
  else if (!L.items.length) body = emptyHtml('folder', '这里是空的', '下载几首歌，或在设置里添加你的音乐目录');
  else body = trackListHtml(L.items, { empty: '' });
  return `<div class="page">${head}${body}</div>`;
}

/* ------------------------------------------------------------------ 视图 · 收藏库 */
function viewLibrary() {
  const tab = state.tabs.library;
  const songs = favsOf('song');
  const artists = favsOf('artist');
  const albums = favsOf('album');
  const total = songs.length + artists.length + albums.length;

  const head = `<div class="vhead vhead-row">
    <div>
      <div class="kicker">LIBRARY</div>
      <h1>收藏库</h1>
      <p class="sub">${total
        ? `<span class="mono">${songs.length}</span> 首歌曲 · <span class="mono">${artists.length}</span> 位歌手 · <span class="mono">${albums.length}</span> 张专辑`
        : '还没有收藏。点曲目右侧的心，或在歌手 / 专辑页收藏。'}</p>
    </div>
    <div class="vhead-acts">
      ${tab === 'song' && songs.length ? `<button class="btn primary" data-act="play-all">${icon('play')}播放全部</button>
      <button class="btn" data-act="dl-all">${icon('dl')}全部下载</button>` : ''}
    </div>
  </div>`;

  const tabs = `<div class="stabs">
    <button class="stab${tab === 'song' ? ' on' : ''}" data-act="ltab" data-tab="song">歌曲 <em>${songs.length}</em></button>
    <button class="stab${tab === 'artist' ? ' on' : ''}" data-act="ltab" data-tab="artist">歌手 <em>${artists.length}</em></button>
    <button class="stab${tab === 'album' ? ' on' : ''}" data-act="ltab" data-tab="album">专辑 <em>${albums.length}</em></button>
    <button class="stab${tab === 'playlist' ? ' on' : ''}" data-act="ltab" data-tab="playlist">歌单 <em>${state.playlists.length}</em></button>
  </div>`;

  let body = '';
  if (tab === 'song') {
    body = songs.length
      ? trackListHtml(songs, { empty: '' })
      : emptyHtml('heart-o', '还没有收藏歌曲', '在搜索结果里点心形图标');
  } else if (tab === 'artist') {
    body = artists.length
      ? `<div class="cards">${artists.map((a) => artistCardHtml({ ...a, count: a.count }, true)).join('')}</div>`
      : emptyHtml('mic', '还没有收藏歌手', '打开歌手页，点「收藏歌手」');
  } else if (tab === 'playlist') {
    body = state.playlists.length
      ? `<div class="cards">${state.playlists.map((p) => playlistCardHtml(p)).join('')}</div>`
      : emptyHtml('list', '还没有歌单', '点侧栏「我的歌单」旁的 + 新建一个');
  } else {
    body = albums.length
      ? `<div class="cards">${albums.map((a) => albumCardHtml(a)).join('')}</div>`
      : emptyHtml('disc', '还没有收藏专辑', '打开专辑页，点「收藏专辑」');
  }
  return `<div class="page">${head}${tabs}${body}</div>`;
}

/* ------------------------------------------------------------------ 视图 · 歌单 */
function viewPlaylist() {
  const pl = plById(state.route.id);
  if (!pl) {
    return `<div class="page">${emptyHtml('list', '歌单不存在', '它可能已经被删掉了')}</div>`;
  }
  const items = pl.items || [];
  const head = `<div class="vhead vhead-row">
    <div>
      <div class="kicker">PLAYLIST</div>
      <h1>${esc(pl.name)}</h1>
      <p class="sub">${items.length
        ? `<span class="mono">${items.length}</span> 首歌曲`
        : '还是空的。右键任意一首歌，选「加入歌单…」。'}</p>
    </div>
    <div class="vhead-acts">
      ${items.length ? `<button class="btn primary" data-act="play-all" data-src="playlist">${icon('play')}播放全部</button>
      <button class="btn" data-act="dl-all" data-src="playlist">${icon('dl')}全部下载</button>` : ''}
      <button class="btn ghost" data-act="pl-rename" data-id="${esc(pl.id)}">重命名</button>
      <button class="btn ghost danger" data-act="pl-delete" data-id="${esc(pl.id)}">删除歌单</button>
    </div>
  </div>`;
  const body = items.length
    ? trackListHtml(items, { empty: '' })
    : emptyHtml('list', '歌单是空的', '右键任意一首歌，选「加入歌单…」');
  return `<div class="page">${head}${body}</div>`;
}

/* ------------------------------------------------------------------ 路由 */
function currentViewHtml() {
  switch (state.route.name) {
    case 'search': return viewSearch();
    case 'artist': return viewArtist();
    case 'album': return viewAlbum();
    case 'library': return viewLibrary();
    case 'local': return viewLocal();
    case 'works': return viewWorks();
    case 'playlist': return viewPlaylist();
    case     'taste': return viewTaste();
    case 'styles': return viewStyles();
    default: return viewHome();
  }
}

/* --------------------------------------------------------- 增量更新渲染 */
/* 整页 innerHTML 重建的代价：每张封面重新解码（白一下）、.page 的入场动画
   重播（整屏闪一下）、滚动位置归零。用户的体感就是「点一下闪一下」。
   这里按节点做最小合并，只替换真正变了的节点，其它原样留着。 */
function morphChildren(dst, src) {
  const a = [...dst.childNodes];
  const b = [...src.childNodes];
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) morphNode(a[i], b[i]);
  for (let i = n; i < b.length; i++) dst.appendChild(b[i].cloneNode(true));
  for (let i = a.length - 1; i >= n; i--) dst.removeChild(a[i]);
}

function morphNode(a, b) {
  if (a.nodeType !== b.nodeType || a.nodeName !== b.nodeName) {
    a.replaceWith(b.cloneNode(true));
    return;
  }
  if (a.nodeType === 3 || a.nodeType === 8) {
    if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue;
    return;
  }
  if (a.nodeType !== 1) return;
  morphAttrs(a, b);
  // 封面地址没变就别碰它 —— 重设同一个 src 也会重新走一次解码，那就是闪的那一下
  morphChildren(a, b);
}

function morphAttrs(a, b) {
  for (let i = 0; i < b.attributes.length; i++) {
    const at = b.attributes[i];
    if (a.getAttribute(at.name) === at.value) continue;
    a.setAttribute(at.name, at.value);
    if (at.name === 'checked' && 'checked' in a) a.checked = true;
  }
  for (let i = a.attributes.length - 1; i >= 0; i--) {
    const name = a.attributes[i].name;
    if (b.hasAttribute(name)) continue;
    a.removeAttribute(name);
    if (name === 'checked' && 'checked' in a) a.checked = false;
  }
}

/* ------------------------------------------------------- 横向货架可滑动 */
/* 之前货架只有 overflow-x:auto 就完事了：滚轮不会横着走、鼠标拖不动，
   右边却永远切着半张封面 —— 看着能滑，试了滑不动，这才是最难受的地方。
   现在补上真正能用的三种手法：hover 箭头、按住拖动、触控板横扫。 */
function shelfRowOf(node) {
  const shelf = node.closest ? node.closest('.shelf') : null;
  return shelf ? shelf.querySelector('.shelf-row') : null;
}

function updateShelfNav(row) {
  const shelf = row.closest('.shelf');
  if (!shelf) return;
  const max = row.scrollWidth - row.clientWidth;
  const can = max > 2;
  shelf.classList.toggle('scrollable', can);
  const prev = shelf.querySelector('.shelf-nav.prev');
  const next = shelf.querySelector('.shelf-nav.next');
  if (prev) prev.hidden = !(can && row.scrollLeft > 2);
  if (next) next.hidden = !(can && row.scrollLeft < max - 2);
  if (can && prev && next) {
    // 箭头只覆盖货架本身那一条，别糊到标题上
    const top = row.offsetTop;
    const h = row.offsetHeight;
    [prev, next].forEach((b) => { b.style.top = top + 'px'; b.style.height = h + 'px'; });
  }
}

function enableDragScroll(row) {
  let down = false, startX = 0, startLeft = 0, moved = 0, draggedAt = 0;
  row.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || e.target.closest('button')) return;
    down = true; moved = 0; startX = e.clientX; startLeft = row.scrollLeft;
    row.classList.add('dragging');
    try { row.setPointerCapture(e.pointerId); } catch (err) { /* 老内核 */ }
  });
  row.addEventListener('pointermove', (e) => {
    if (!down) return;
    const dx = e.clientX - startX;
    if (Math.abs(dx) > Math.abs(moved)) moved = dx;
    if (Math.abs(dx) > 3) row.scrollLeft = startLeft - dx;
  });
  const end = (e) => {
    if (!down) return;
    down = false;
    row.classList.remove('dragging');
    if (Math.abs(moved) > 5) draggedAt = Date.now();
    try { row.releasePointerCapture(e.pointerId); } catch (err) { /* ignore */ }
  };
  row.addEventListener('pointerup', end);
  row.addEventListener('pointercancel', end);
  // 拖完松手那一下还会补一个 click，别让它把卡片当播放键
  row.addEventListener('click', (e) => {
    if (Date.now() - draggedAt < 260) { e.stopPropagation(); e.preventDefault(); }
  }, true);
  row.addEventListener('scroll', () => updateShelfNav(row), { passive: true });
}

function decorateShelves() {
  $$('#view .shelf').forEach((shelf) => {
    const row = shelf.querySelector('.shelf-row');
    if (!row) return;
    if (!shelf.querySelector('.shelf-nav')) {
      shelf.insertAdjacentHTML('beforeend',
        `<button class="shelf-nav prev" data-act="shelf-prev" title="上一组" hidden>${icon('back')}</button>
         <button class="shelf-nav next" data-act="shelf-next" title="下一组" hidden>${icon('fwd')}</button>`);
    }
    if (row.dataset.drag !== '1') {
      row.dataset.drag = '1';
      enableDragScroll(row);
      // 封面陆续解码会改变 scrollWidth，得跟着重算有没有更多
      row.querySelectorAll('img').forEach((img) => {
        if (!img.complete) img.addEventListener('load', () => updateShelfNav(row), { once: true });
      });
    }
    updateShelfNav(row);
  });
}

function patchView(enter) {
  const view = $('#view');
  const y = view.scrollTop;
  const tpl = document.createElement('template');
  tpl.innerHTML = currentViewHtml();
  morphChildren(view, tpl.content);
  view.scrollTop = y;                    // 增量更新不该动静态页面里的阅读位置
  if (enter) {                           // 只有真的换页才播一次入场
    view.classList.remove('enter');
    void view.offsetWidth;
    view.classList.add('enter');
  }
}

function render(enter) {
  patchView(enter);
  // 列表上下文：当前视图的曲目集合，供播放/下载使用
  if (state.route.name === 'artist') state.list = (state.artist && state.artist.list) || [];
  else if (state.route.name === 'album') state.list = (state.album && state.album.list) || [];
  else if (state.route.name === 'library') state.list = favsOf('song');
  else if (state.route.name === 'local') state.list = (state.local && state.local.items) || [];
  else if (state.route.name === 'playlist') {
    const pl = plById(state.route.id);
    state.list = (pl && pl.items) || [];
  }
  else if (state.route.name === 'search') state.list = state.items;
  decorateShelves();
  syncNav();
  renderPlaying();
}

function renderPlaying() {
  $$('#view .tr').forEach((row) => {
    const i = Number(row.dataset.row);
    const it = state.list[i];
    if (!it) return;
    const on = isPlaying(it);
    row.classList.toggle('on', on);
    const n = row.querySelector('.tr-n');
    if (n) {
      const num = n.querySelector('span');
      const eq = n.querySelector('.eq');
      if (on && !eq) {
        if (num) num.remove();
        n.insertAdjacentHTML('afterbegin', eqHtml());
      } else if (!on && eq) {
        eq.remove();
        n.insertAdjacentHTML('afterbegin', `<span>${i + 1}</span>`);
      }
    }
  });
}

function syncNav() {
  // 高亮只跟主分区（首页/搜索/收藏库/设置）；歌手 / 专辑 / 歌单等详情页
  // 不属于任何分区，全部熄灭 —— 从侧栏进来高亮残留在「搜索」会误导。
  $$('#snav .snav-i').forEach((b) => b.classList.toggle('on', b.dataset.nav === state.route.name));
  $('#backBtn').disabled = !state.stack.length;
}

function go(route, push = true) {
  if (push) state.stack.push(state.route);
  state.route = route;
  if (route.name !== 'search') { /* 保留搜索状态 */ }
  render(true);
  $('#view').scrollTop = 0;
}

function goBack() {
  if (!state.stack.length) return;
  const prev = state.stack.pop();
  state.route = prev;
  render(true);
}

/* ------------------------------------------------------------------ 侧栏收藏 */
function renderSide() {
  const arts = favsOf('artist');
  const albs = favsOf('album');
  $('#artCount').textContent = arts.length || '';
  $('#albCount').textContent = albs.length || '';
  $('#libCount').textContent = state.favs.length || '';
  $('#plCount').textContent = state.playlists.length || '';
  { const lc = $('#localCount');                 // 本地曲库计数：扫过了才显示数字
    if (lc) { lc.textContent = state.local.items.length || ''; lc.hidden = !state.local.loaded; } }
  { const wc = $('#worksCount');                 // 作品计数：进过作品页才显示
    if (wc) { wc.textContent = (state.works || []).length || ''; wc.hidden = !state.worksLoaded; } }

  $('#sidePlaylists').innerHTML = state.playlists.length
    ? state.playlists.slice(0, 20).map((p) => `<button class="side-item" data-act="playlist" data-id="${esc(p.id)}">
        <span class="si-art"><svg><use href="#i-list"/></svg></span>
        <span class="si-txt"><b>${esc(p.name)}</b></span>
        <em class="si-n">${(p.items || []).length}</em>
      </button>`).join('')
    : '<div class="side-empty">新建歌单，把歌装进来</div>';

  $('#sideArtists').innerHTML = arts.length
    ? arts.slice(0, 12).map((a) => `<button class="side-item" data-act="artist" data-name="${esc(a.name)}" data-platform="${esc(a.platform)}" data-aid="${esc(String(a.artist_id || '').trim())}">
        <span class="si-art round">${a.cover ? `<img src="${esc(a.cover)}" alt="">` : phImg('artist')}</span>
        <span class="si-txt"><b>${esc(a.name)}</b></span>
      </button>`).join('')
    : '<div class="side-empty">收藏的歌手会出现在这里</div>';

  $('#sideAlbums').innerHTML = albs.length
    ? albs.slice(0, 12).map((a) => `<button class="side-item" data-act="album" data-id="${esc(a.id)}" data-platform="${esc(a.platform)}" data-alts="${esc(altsAttr(a))}">
        <span class="si-art">${a.cover ? `<img src="${esc(a.cover)}" alt="">` : phImg('album')}</span>
        <span class="si-txt"><b>${esc(a.name)}</b></span>
      </button>`).join('')
    : '<div class="side-empty">收藏的专辑会出现在这里</div>';
}

/* ------------------------------------------------------------------ 队列 */
function queueHeadHtml() {
  return `<div class="qhead">
      <span class="mono">QUEUE · ${state.playList.length}${state.queueSource
        ? ` · 来自：${esc(state.queueSource)}` : ''}</span>
      <button class="btn sm ghost" data-act="qclear">清空</button>
    </div>`;
}

function queueRowsHtml() {
  return state.playList.map((it, i) => `
    <button class="qrow${i === state.playIndex ? ' on' : ''}" data-act="qplay" data-i="${i}"
            draggable="true" data-qi="${i}" title="拖动可调整顺序">
      <span class="qrow-n">${i === state.playIndex ? '▶' : pad(i + 1)}</span>
      <span class="qr-main">
        <span class="qr-name ell">${esc(it.name || '')}</span>
        <span class="qr-sub ell">${esc(it.artist || '')}</span>
      </span>
      <span class="ic qr-next" data-act="qnext" data-i="${i}" draggable="false" title="插到下一首">${icon('next')}</span>
      <span class="ic" data-act="qremove" data-i="${i}" draggable="false" title="移出队列">${icon('close')}</span>
    </button>`).join('');
}

function renderQueue() {
  const pane = $('#queuePane');
  if (pane) {
    pane.innerHTML = state.playList.length
      ? queueHeadHtml() + queueRowsHtml()
      : emptyHtml('queue', '队列是空的', '点任意一首歌开始播放');
  }
  renderNp();
}

/* ------------------------------------------------- 队列内拖拽重排（Stage C）
   范围：只改内存态 state.playList，不跨页、不落盘。
   关键约束：正在播放的那首不能被打断 —— 靠对象引用重新定位 playIndex，
   不重新 load 音频。 */
let _dragFrom = -1;              // 拖动源索引
let _dragTo = -1;                // 落点（插入到该索引之前）

function queueRowEls() {
  return $$('#queuePane .qrow');
}

function clearDragMarks() {
  $$('#queuePane .qrow').forEach((r) => r.classList.remove('drag-src', 'drop-above', 'drop-below'));
  const line = $('#queueDropLine');
  if (line) line.remove();
}

function onQueueDragStart(e) {
  const row = e.target.closest && e.target.closest('.qrow');
  if (!row) return;
  // 从按钮上起拖不算排序
  if (e.target.closest('[data-act="qnext"],[data-act="qremove"]')) {
    e.preventDefault();
    return;
  }
  _dragFrom = Number(row.dataset.qi);
  _dragTo = -1;
  row.classList.add('drag-src');
  if (e.dataTransfer) {
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', String(_dragFrom)); } catch (err) { /* IE */ }
  }
}

function onQueueDragOver(e) {
  if (_dragFrom < 0) return;
  e.preventDefault();
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
  const row = e.target.closest && e.target.closest('.qrow');
  queueRowEls().forEach((r) => r.classList.remove('drop-above', 'drop-below'));
  const line = $('#queueDropLine');
  if (line) line.remove();
  if (!row) { _dragTo = -1; return; }
  const r = row.getBoundingClientRect();
  const before = (e.clientY - r.top) < r.height / 2;
  const to = Number(row.dataset.qi);
  _dragTo = before ? to : to + 1;
  // 拖到自己身上（或紧邻自己）不显示指示线
  if (_dragTo === _dragFrom || _dragTo === _dragFrom + 1) {
    _dragTo = -1;
    return;
  }
  row.classList.add(before ? 'drop-above' : 'drop-below');
}

function onQueueDrop(e) {
  if (_dragFrom < 0) return;
  e.preventDefault();
  const from = _dragFrom;
  let to = _dragTo;
  clearDragMarks();
  _dragFrom = -1;
  _dragTo = -1;
  if (to < 0 || from === to) return;
  const list = state.playList;
  if (from < 0 || from >= list.length) return;
  // 正在播放的那首：用对象引用重新定位，绝不重载音频
  const playing = state.playing;
  const [moved] = list.splice(from, 1);
  let at = to;
  if (at > from) at -= 1;            // 移除源后目标索引左移
  at = Math.max(0, Math.min(at, list.length));
  list.splice(at, 0, moved);
  if (playing) {
    const ni = list.indexOf(playing);
    if (ni >= 0) state.playIndex = ni;
  }
  renderQueue();
}

function onQueueDragEnd() {
  clearDragMarks();
  _dragFrom = -1;
  _dragTo = -1;
}

/* ------------------------------------------------- Now Playing 右侧面板 */
function openNp(tab) {
  state.npTab = tab || state.npTab || 'lyric';
  $('#npDrawer').hidden = false;
  $('#panelDrawer').hidden = true;
  $('#settingsDrawer').hidden = true;
  closeQualityMenu();
  renderNp();
}

function renderNp() {
  const box = $('#npBody');
  if (!box || $('#npDrawer').hidden) return;
  const tab = state.npTab || 'lyric';
  $$('#npDrawer .dtab[data-tab]').forEach(
    (b) => b.classList.toggle('on', b.dataset.tab === tab));
  const it = state.playing;
  if (!it) { box.innerHTML = emptyHtml('note', '未在播放', '选一首歌开始'); return; }
  if (tab === 'queue') {
    box.innerHTML = state.playList.length
      ? queueHeadHtml() + queueRowsHtml()
      : emptyHtml('queue', '队列是空的', '点任意一首歌开始播放');
    return;
  }
  const q = state.currentQuality;
  box.innerHTML = `<div class="np-hero">
      <div class="np-art">${it.cover
        ? `<img src="${esc(it.cover)}" alt="" onerror="this.remove()">` : phImg('song')}</div>
      <div class="np-title">${esc(it.name || '')}</div>
      <div class="np-sub">${esc(it.artist || '')}${it.album_name ? ' · ' + esc(it.album_name) : ''}</div>
      <div class="np-badges">${srcBadge(it)}${q
        ? `<button class="chip pq" data-act="pq">${q.format === 'flac' ? 'FLAC' : 'MP3 ' + q.bitrate + 'k'} · 切换音质</button>` : ''}</div>
    </div>
    <div class="np-lyric" id="npLyricBody"><span class="muted">正在取歌词…</span></div>
    <div class="np-foot lrc-tools">
      <button class="btn ghost sm" data-act="lrc-off" data-d="-0.5" title="歌词提前半秒（歌词比人声慢时用）">−0.5s</button>
      <span class="mono" data-off>0ms</span>
      <button class="btn ghost sm" data-act="lrc-off" data-d="0.5" title="歌词延后半秒（歌词比人声快时用）">+0.5s</button>
      <button class="btn ghost sm" data-act="lrc-off" data-d="0" data-reset hidden>复位</button>
      <span class="lrc-gap"></span>
      <button class="btn ghost sm" data-act="karaoke">${icon('mic')}去K歌</button>
      <button class="btn ghost sm" data-act="lyric-full">放大歌词</button>
    </div>`;
  syncLrcOffUI();
  loadNpLyric(it);
}

/* ------------------------------------------------------------------ 歌词 · SP 式逐行 */
let lyricLines = [];      // [{t, text, trans?}] 按时间升序；空 = 无时间标签，降级纯文本
let lyricIdx = -1;
let lyricFollow = true;   // 是否跟随当前行滚动（用户手动滚动后置 false）
let lyricScrolling = false;// 正在做程序滚动，期间的 wheel 事件不算「用户滚动」
let lyricPending = false;  // 动画期间目标行变了，动画结束后补一次滚动
let LRC_OFF = 0;           // 歌词时间轴偏移（秒）：正=行延后出现，负=提前

function parseLrcText(raw) {
  const out = [];
  String(raw || '').split(/\r?\n/).forEach((ln) => {
    const tags = [...ln.matchAll(/\[(\d+):(\d+(?:\.\d+)?)\]/g)];
    if (!tags.length) return;
    const text = ln.replace(/\[[^\]]*\]/g, '').trim();
    if (!text) return;
    tags.forEach((mt) => out.push({ t: (+mt[1]) * 60 + (+mt[2]), text }));
  });
  return out.sort((x, y) => x.t - y.t);
}

/* 翻译 LRC 与主 LRC 按时间戳就近对齐（两轴都升序，游标单调前进）。
   上游带不带翻译不一定（wyy 多数带，kuwo 基本没有）——取不到就单语，不承诺。 */
function parseLrc(raw, trans) {
  const lines = parseLrcText(raw);
  const tl = parseLrcText(trans);
  if (lines.length && tl.length) {
    let j = 0;
    lines.forEach((l) => {
      while (j + 1 < tl.length && Math.abs(tl[j + 1].t - l.t) <= Math.abs(tl[j].t - l.t)) j++;
      if (tl[j] && Math.abs(tl[j].t - l.t) <= 0.6 && tl[j].text !== l.text) l.trans = tl[j].text;
    });
  }
  return lines;
}

function lyricIndexAt(sec) {
  let lo = 0, hi = lyricLines.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (lyricLines[mid].t + LRC_OFF <= sec + 0.25) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}

function renderLyricLines(el, big) {
  if (!lyricLines.length) {
    const plain = String(currentLyric || '').replace(/\[[^\]]*\]/g, '').trim();
    el.innerHTML = `<div class="lyric-plain">${esc(plain || '这首没有歌词')}</div>`;
    return;
  }
  lyricIdx = -1;
  lyricFollow = true;
  el.innerHTML = lyricLines.map((l, i) =>
    `<button class="lyric-line${big ? ' big' : ''}" data-act="lyric-seek" data-i="${i}" data-t="${l.t}">${esc(l.text)}${l.trans ? `<span class="lt">${esc(l.trans)}</span>` : ''}</button>`
  ).join('');
  updateLyricTick();
}

/* 偏移控件状态同步：np 面板与放大层各有一组 [data-off]/[data-reset]。 */
function syncLrcOffUI() {
  const ms = Math.round(Number(state.settings.lrc_offset_ms) || 0);
  $$('[data-off]').forEach((el) => { el.textContent = (ms > 0 ? '+' : '') + ms + 'ms'; });
  $$('[data-reset]').forEach((el) => { el.hidden = ms === 0; });
}

/* 把当前行滚到容器正中。
   用 getBoundingClientRect 直接量，不用 offsetTop：
   offsetTop 是相对 offsetParent 的，歌词行与滚动容器常常不在同一个
   定位上下文里，基准会跑到外层祖先，算出来的位置能差 50px 上下，
   表现就是「高亮歌词不在视口中间」。
   做法：先量出行与容器当前的像素差，加上要滚的量，得到目标 scrollTop。
   滚动用 smooth，但只在「行变了」时滚一次 —— 之前每次 timeupdate 都滚，
   浏览器会把连续 smooth 动画叠加，看起来就是歌词自己乱飞。 */
function scrollLyricTo(box, idx) {
  const cur = idx >= 0 ? box.children[idx] : null;
  if (!cur) return;
  const boxRect = box.getBoundingClientRect();
  const curRect = cur.getBoundingClientRect();
  // 行中心与容器中心的差：正数表示行在下方，需要往下滚
  const curCenter = curRect.top + curRect.height / 2;
  const boxCenter = boxRect.top + boxRect.height / 2;
  const delta = curCenter - boxCenter;
  const top = Math.max(0, box.scrollTop + delta);
  // 动画进行中不要打断它 —— smooth 被反复打断/叠加就会飞快乱窜。
  // 只记一个「待重算」标记，动画结束后按当前高亮行再算一次。
  if (lyricScrolling) { lyricPending = true; return; }
  if (Math.abs(box.scrollTop - top) < 2) return;
  lyricScrolling = true;
  box.scrollTo({ top, behavior: 'smooth' });
  setTimeout(() => {
    lyricScrolling = false;
    if (lyricPending && lyricFollow) {
      lyricPending = false;
      // 用当前真正的高亮行重算，而不是动画开始时那行
      scrollLyricTo(box, lyricIdx);
    } else {
      lyricPending = false;
    }
  }, 420);
}

function updateLyricTick() {
  if (!lyricLines.length) return;
  const a = audio();
  const idx = lyricIndexAt(a.currentTime || 0);
  if (idx === lyricIdx) return;      // 行没变就不动，避免反复触发滚动
  lyricIdx = idx;
  ['npLyricBody', 'lyricBody'].forEach((id) => {
    const box = document.getElementById(id);
    if (!box || box.hidden || !box.offsetParent) return;
    box.querySelectorAll('.lyric-line').forEach((b, i) => {
      b.classList.toggle('on', i === idx);
      b.classList.toggle('near', Math.abs(i - idx) === 1);
    });
    if (lyricFollow) scrollLyricTo(box, idx);
  });
}

async function loadNpLyric(it) {
  const box = $('#npLyricBody');
  if (!box) return;
  box.innerHTML = '<span class="muted">正在取歌词…</span>';
  lyricLines = []; lyricIdx = -1;
  try {
    const d = await rpc('getlyric', {
      platform: it.platform, id: it.id, time: it.time, sign: it.sign,
    });
    const raw = (d && (d.lyric || d.lrc || d.text || (typeof d === 'string' ? d : ''))) || '';
    const trans = (d && typeof d === 'object' && (d.tlyric || d.tLyric || d.trans)) || '';
    currentLyric = raw;
    if (!state.playing || pk(state.playing) !== pk(it)) return;
    lyricLines = parseLrc(raw, trans);
    renderLyricLines($('#npLyricBody'), false);
    if (!$('#lyricLayer').hidden) renderLyricLines($('#lyricBody'), true);
  } catch (e) {
    const el = $('#npLyricBody');
    if (el) el.innerHTML = `<span class="muted">取歌词失败：${esc(e.message)}</span>`;
  }
}

/* ------------------------------------------------------------------ 下载 */
const ACTIVE = ['queued', 'running'];
function stateText(t) {
  switch (t.state) {
    case 'queued': return '排队中';
    case 'running': return t.total
      ? `${fmtSize(t.got)} / ${fmtSize(t.total)} · ${fmtSpeed(t.speed)}${t.eta ? ' · 剩 ' + fmtTime(t.eta) : ''}`
      : '连接中';
    case 'done': return `${t.format ? t.format.toUpperCase() : ''}${t.bitrate ? ' ' + t.bitrate + 'k' : ''} · 完成`;
    case 'error': return t.error || '失败';
    case 'cancelled': return '已取消';
    case 'skipped': return '已跳过';
    default: return t.state;
  }
}

function renderTasks() {
  const pane = $('#dlPane');
  if (!pane) return;
  $('#dlCount').textContent = state.tasks.length;
  const active = state.tasks.filter((t) => ACTIVE.includes(t.state)).length;
  const badge = $('#dlBadge');
  if (active) { badge.hidden = false; badge.textContent = active; } else { badge.hidden = true; }

  if (!state.tasks.length) {
    pane.innerHTML = emptyHtml('dl', '没有下载任务', '在曲目右侧点下载图标');
    return;
  }
  pane.innerHTML = `<div class="qhead">
      <span class="mono">DOWNLOADS · ${state.tasks.length}</span>
      <button class="btn sm ghost" data-act="dl-clear">清理已完成</button>
    </div>` + state.tasks.map((t) => {
    const pct = t.total ? Math.min(100, (t.got / t.total) * 100) : (t.state === 'done' ? 100 : 0);
    const cls = t.state === 'error' ? ' err' : t.state === 'done' ? ' done' : '';
    const acts = ACTIVE.includes(t.state)
      ? `<button class="ic" data-act="dl-cancel" data-id="${t.id}" title="取消">${icon('close')}</button>`
      : t.state === 'error' || t.state === 'cancelled'
        ? `<button class="ic" data-act="dl-retry" data-id="${t.id}" title="重试">${icon('retry')}</button>
           <button class="ic" data-act="dl-remove" data-id="${t.id}" title="移除">${icon('trash')}</button>`
        : `<button class="ic" data-act="dl-remove" data-id="${t.id}" title="移除">${icon('trash')}</button>`;
    return `<div class="task${cls}">
      <div class="task-top">
        <div class="task-cover">${t.cover ? `<img src="${esc(t.cover)}" alt="">` : phImg('song')}</div>
        <div class="task-main">
          <div class="task-name">${esc(t.name)}</div>
          <div class="task-sub">${esc(stateText(t))}</div>
        </div>
        <div class="task-act">${acts}</div>
      </div>
      <div class="pbar"><i style="width:${pct.toFixed(1)}%"></i></div>
    </div>`;
  }).join('');
}

async function refreshTasks() {
  try {
    const d = await getJson('/api/downloads');
    if (d && d.data) {
      state.tasks = d.data.tasks || [];
      renderTasks();
    }
  } catch (e) { /* 静默 */ }
}

async function enqueue(items, quality) {
  if (!items || !items.length) return;
  try {
    await post('/api/downloads/add', { items, quality: quality || state.quality });
    toast(`已加入下载队列 · ${items.length} 首`);
    openPanel('dl');
    refreshTasks();
  } catch (e) {
    toast(e.message || '加入队列失败', 'err');
  }
}

/* ------------------------------------------------------------------ 音质菜单 */
function positionMenu(menu, anchor) {
  menu.hidden = false;
  const r = anchor.getBoundingClientRect();
  const w = menu.offsetWidth;
  const h = menu.offsetHeight;
  let left = Math.min(window.innerWidth - w - 10, r.right - w);
  let top = r.bottom + 6;
  if (top + h > window.innerHeight - 100) top = r.top - h - 6;
  menu.style.left = Math.max(8, left) + 'px';
  menu.style.top = Math.max(8, top) + 'px';
}

function openQualityMenu(anchor, items) {
  const menu = $('#qmenu');
  // 用第一首的可用音质做选项，其余按同规则降级
  const first = items[0];
  const minfo = (first.minfo || []).slice().sort((a, b) => _br(b) - _br(a));
  if (!minfo.length) { toast('这首没有可用音质', 'err'); return; }
  const opts = minfo.map((m) => `<button data-act="pick-q" data-q="${m.format}:${m.bitrate}">
      <span>${m.format === 'flac' ? '无损 FLAC' : 'MP3 ' + m.bitrate + 'k'}</span>
    </button>`).join('');
  menu.innerHTML = `<div class="qm-t">选择下载音质${items.length > 1 ? ` · ${items.length} 首` : ''}</div>${opts}`;
  positionMenu(menu, anchor);
  menu._items = items;
  menu._mode = 'download';
}

/* 播放中切音质：列出当前曲目的真实档位，切完保持进度与播放/暂停态 */
function openPlayQualityMenu(anchor) {
  const it = state.playing;
  if (!it) { toast('先播放一首歌'); return; }
  const menu = $('#qmenu');
  if (!menu.hidden && menu._mode === 'play') { closeQualityMenu(); return; }
  const list = playCandidates(it);
  if (!list.length) { toast('这首没有可用音质', 'err'); return; }
  const cur = state.currentQuality || {};
  const opts = list.map((q) => {
    const on = q.format === cur.format && String(q.bitrate) === String(cur.bitrate);
    const label = q.format === 'flac' ? '无损 FLAC' : 'MP3 ' + q.bitrate + 'k';
    return `<button data-act="pick-q" data-q="${q.format}:${q.bitrate}"${on ? ' class="on"' : ''}>
      <span>${label}${on ? ' · 当前' : ''}</span>
    </button>`;
  }).join('');
  menu.innerHTML = `<div class="qm-t">播放音质 · ${esc(it.name || '')}</div>${opts}`;
  positionMenu(menu, anchor);
  menu._items = null;
  menu._mode = 'play';
}

async function switchQuality(q) {
  const it = state.playing;
  if (!it) return;
  const a = audio();
  const pos = Number.isFinite(a.currentTime) ? a.currentTime : 0;
  const wasPaused = a.paused;
  const exact = (it.minfo || []).find(
    (m) => m.format === q.format && String(m.bitrate) === String(q.bitrate)) || q;
  const rest = playCandidates(it).filter(
    (m) => !(m.format === exact.format && String(m.bitrate) === String(exact.bitrate)));
  const counted = lastCountedKey;
  await startTrack(it, [exact, ...rest], 0, pos,
    { keepPaused: wasPaused, keepCounted: true });
  lastCountedKey = counted;
  let pref = 'auto';
  if (exact.format === 'flac') pref = 'best';
  else if (_br(exact) >= 320) pref = '320';
  else pref = '128';
  state.settings.play_quality = pref;
  post('/api/store', { op: 'settings', payload: { play_quality: pref } }).catch(() => {});
  const sel = $('#setPlayQuality');
  if (sel) sel.value = pref;
  paintSpec();
  toast(`已切到 ${exact.format === 'flac' ? '无损 FLAC' : 'MP3 ' + exact.bitrate + 'k'}`);
}

function closeQualityMenu() { $('#qmenu').hidden = true; }

/* ------------------------------------------------- 右键菜单（Stage D）
   范围（决策 4）：只新增一个动作「加入歌单…」，其余动作继续走 hover 按钮。
   自绘而非原生 contextmenu —— 原生样式不可控、无法加图标。 */
let _ctxTarget = null;           // {kind:'song'|'album'|'artist', ...}

function closeCtxMenu(keepTarget) {
  const m = $('#ctxmenu');
  if (m) m.hidden = true;
  // 面板切换到「选择歌单」时只是换个内容，不该清掉目标
  if (!keepTarget) _ctxTarget = null;
}

/* 按光标位置定位，贴近右/下边缘时向左/上翻转，不溢出视口 */
function positionCtxMenu(x, y) {
  const m = $('#ctxmenu');
  m.hidden = false;
  m._px = x;
  m._py = y;
  const w = m.offsetWidth;
  const h = m.offsetHeight;
  let left = x;
  let top = y;
  if (left + w > window.innerWidth - 8) left = x - w;
  if (top + h > window.innerHeight - 8) top = y - h;
  m.style.left = Math.max(8, Math.min(left, window.innerWidth - w - 8)) + 'px';
  m.style.top = Math.max(8, Math.min(top, window.innerHeight - h - 8)) + 'px';
}

function openCtxMenu(x, y, target) {
  const m = $('#ctxmenu');
  if (!m) return;
  _ctxTarget = target;
  let html = '';
  if (target.kind === 'song') {
    html = `<div class="qm-t">${esc(target.name || '')}</div>
      <button data-act="ctx-karaoke">${icon('mic')}K歌这一首</button>
      <button data-act="ctx-add-pl" data-kind="song">${icon('list')}加入歌单…</button>`;
  } else if (target.kind === 'album') {
    html = `<div class="qm-t">${esc(target.name || '')} · 专辑</div>
      <button data-act="ctx-add-pl" data-kind="album">${icon('list')}整张加入歌单…</button>`;
  } else if (target.kind === 'artist') {
    html = `<div class="qm-t">${esc(target.name || '')} · 歌手</div>
      <button data-act="ctx-add-pl" data-kind="artist">${icon('list')}全部加入歌单…</button>`;
  }
  m.innerHTML = html;
  positionCtxMenu(x, y);
}

/* 「加入歌单…」：列出所有歌单供选择 */
function openPlaylistPicker(target) {
  const m = $('#ctxmenu');
  if (!m) return;
  // 关键：这里要重新设回去 —— 上一步 closeCtxMenu() 已把 _ctxTarget 清空
  _ctxTarget = target;
  const pls = state.playlists;
  const rows = pls.length
    ? pls.map((p) => `<button data-act="pl-pick" data-id="${esc(p.id)}">
        <span>${esc(p.name)}</span><span class="mono">${(p.items || []).length}</span>
      </button>`).join('')
    : '<div class="qm-empty">还没有歌单</div>';
  m.innerHTML = `<div class="qm-t">加入歌单</div>${rows}
    <button data-act="pl-pick-new" class="qm-new">${icon('plus')}新建歌单…</button>`;
  positionCtxMenu(m._px || 40, m._py || 40);
}

/* 收集目标对应的曲目列表（单曲本身 / 专辑曲目 / 歌手曲目）。
   直接 rpc 取数：复用 openArtist/openAlbum 会把当前页面导航走 ——
   用户右键「加入歌单」后突然发现页面跳了，这是导航副作用不是他要的。 */
async function collectForAdd(target) {
  if (target.kind === 'song') {
    return target.item ? [target.item] : [];
  }
  if (target.kind === 'album') {
    const d = await rpc('album', { platform: target.platform || state.platform, id: target.id });
    return (d && d.list) || [];
  }
  if (target.kind === 'artist') {
    const d = await rpc('artist', {
      platform: target.platform || state.platform,
      name: target.name,
      artist_id: target.artist_id || '',
    });
    return (d && d.list) || [];
  }
  return [];
}

async function doAddToPlaylist(plid, target) {
  const songs = await collectForAdd(target);
  if (!songs.length) { toast('没找到可加入的曲目', 'err'); return; }
  const added = await plAddSongs(plid, songs);
  if (added) {
    const pl = plById(plid);
    toast(`已加入「${pl ? pl.name : '歌单'}」 · ${added} 首`);
  } else {
    toast('这些歌已在歌单里了');
  }
}

/* ------------------------------------------------------------------ 设置面板 */
/* ------------------------------------------------------------------ 多端同步
   与导入歌单同一套规矩：拉取只做 dry-run diff，逐项声明，勾了才落盘。
   本地已有的收藏 / 歌单永不因为「远端没有」而被删掉 —— 同步只做加法。 */
let SYNC_STATUS = null;
let SYNC_DIFF = null;

function fmtAgo(ts) {
  const t = Number(ts || 0);
  if (!t) return '从未';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - t));
  if (s < 45) return '刚刚';
  if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
  if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
  return Math.floor(s / 86400) + ' 天前';
}

async function paintSyncTip() {
  const el = $('#syncTip');
  if (!el) return;
  try {
    const d = await getJson('/api/sync/status');
    SYNC_STATUS = (d && d.data) || {};
    if (!SYNC_STATUS.configured) { el.textContent = '还没填服务器和账号'; return; }
    const parts = [];
    if (SYNC_STATUS.last_push) parts.push('上次上传 ' + fmtAgo(SYNC_STATUS.last_push));
    if (SYNC_STATUS.last_pull) parts.push('上次拉取 ' + fmtAgo(SYNC_STATUS.last_pull));
    const r = SYNC_STATUS.remote;
    if (r && r.counts) {
      parts.push(`远端：收藏 ${r.counts.favorites} / 歌单 ${r.counts.playlists}`);
    }
    const pend = SYNC_STATUS.pending;
    let badge = '';
    if (pend) {
      const n = (pend.favorites || 0) + (pend.playlists || 0)
        + (pend.conflicts || 0) + (pend.last_track ? 1 : 0);
      if (n) badge = `<b class="sync-badge">远端有 ${n} 项待合并</b>`;
    }
    el.innerHTML = badge + esc(parts.length ? parts.join(' · ') : '已配置，还没同步过');
  } catch (e) {
    el.textContent = '读取同步状态失败';
  }
}

function syncForm() {
  return {
    url: ($('#setDavUrl') || {}).value || '',
    user: ($('#setDavUser') || {}).value || '',
    pass: ($('#setDavPass') || {}).value || '',
    dir: ($('#setDavDir') || {}).value || 'Elysium',
    auto: !!($('#setDavAuto') || {}).checked,
  };
}

async function syncSave() {
  try {
    const r = await post('/api/sync/config', syncForm());
    state.settings.webdav = Object.assign({}, r.data || {}, { has_pass: true });
    toast('已保存，正在测试连接…');
    const t = await post('/api/sync/test', {});
    if (!t.ok) throw new Error(t.error || '连接失败');
    const d = t.data || {};
    toast(d.exists
      ? `连上了 · 远端已有数据包（${(d.remote && d.remote.counts && d.remote.counts.favorites) || 0} 项收藏）`
      : '连上了 · 远端还没有数据包，可以直接上传');
    fillSettings();
    paintSyncTip();
  } catch (e) {
    toast(e.message || '保存失败', 'err');
    paintSyncTip();
  }
}

async function syncPush() {
  const btn = $('[data-act="sync-push"]');
  if (btn) btn.disabled = true;
  try {
    const r = await post('/api/sync/push', {});
    if (!r.ok) throw new Error(r.error || '上传失败');
    const d = r.data || {};
    toast(`已上传：收藏 ${d.favorites} 项 · 歌单 ${d.playlists} 个 · ${fmtSize(d.bytes)}`);
    paintSyncTip();
  } catch (e) {
    toast(e.message || '上传失败', 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function syncPull() {
  try {
    toast('正在读取远端…');
    const r = await post('/api/sync/pull', {});
    if (!r.ok) throw new Error(r.error || '拉取失败');
    openSyncDrawer(r.data || {});
    paintSyncTip();
  } catch (e) {
    toast(e.message || '拉取失败', 'err');
  }
}

function openSyncDrawer(data) {
  SYNC_DIFF = data;
  $('#npDrawer').hidden = true;
  $('#panelDrawer').hidden = true;
  $('#settingsDrawer').hidden = true;
  $('#importDrawer').hidden = true;
  $('#syncDrawer').hidden = false;
  renderSync();
}

const SYNC_KIND = { song: '歌曲', album: '专辑', artist: '歌手' };

function renderSync() {
  const box = $('#syncBody');
  if (!box) return;
  const d = SYNC_DIFF;
  if (!d) { box.innerHTML = ''; return; }
  const rm = d.remote || {};
  const cnt = rm.counts || {};
  let html = `<div class="sync-head">
    <b>远端数据包</b>
    <p class="tip">来自 ${esc(rm.device || '未知设备')} · ${rm.pushed_at ? fmtAgo(rm.pushed_at) + '上传' : '时间未知'}
      · 收藏 ${cnt.favorites || 0} 项 / 歌单 ${cnt.playlists || 0} 个</p>
  </div>`;

  if (d.empty) {
    html += `<div class="sync-empty">两端一致，没有需要合并的内容</div>
      <div class="set-actions"><button class="btn sm" data-close="syncDrawer">关闭</button></div>`;
    box.innerHTML = html;
    return;
  }

  const favs = d.favorites || [];
  if (favs.length) {
    html += `<div class="sync-sec">
      <label class="sync-all"><input type="checkbox" id="syncFav" checked/>
        <b>收藏</b><em class="tip">远端多出 ${favs.length} 项</em></label>
      <div class="sync-list">${favs.slice(0, 40).map((f) => `<div class="sync-row plain">
        <span class="sr-kind">${SYNC_KIND[f.kind] || '歌曲'}</span>
        <span class="sr-name">${esc(f.name || '')}</span>
        <span class="sr-sub">${esc(f.artist || '')}</span>
      </div>`).join('')}
      ${favs.length > 40 ? `<div class="sync-row plain muted">…另有 ${favs.length - 40} 项</div>` : ''}</div>
    </div>`;
  }

  const p = d.playlists || {};
  const news = p.new || [];
  if (news.length) {
    html += `<div class="sync-sec"><b class="sync-title">新歌单</b>
      <div class="sync-list">${news.map((x) => `<label class="sync-row">
        <input type="checkbox" data-pl-new="${esc(x.id)}" checked/>
        <span class="sr-name">${esc(x.name)}</span>
        <span class="sr-sub">${x.count} 首</span></label>`).join('')}</div>
    </div>`;
  }

  const fills = p.fill || [];
  if (fills.length) {
    html += `<div class="sync-sec"><b class="sync-title">可补条目</b>
      <div class="sync-list">${fills.map((x) => `<label class="sync-row">
        <input type="checkbox" data-pl-fill="${esc(x.id)}" checked/>
        <span class="sr-name">${esc(x.name)}</span>
        <span class="sr-sub">远端多 ${x.add} 首（共 ${x.total}）</span></label>`).join('')}</div>
    </div>`;
  }

  const conf = p.conflicts || [];
  if (conf.length) {
    html += `<div class="sync-sec"><b class="sync-title">同名分歧</b>
      <p class="tip">同名但内容不同的歌单，需要你定一个</p>
      ${conf.map((x) => `<div class="sync-row">
        <span class="sr-name">${esc(x.name)}</span>
        <span class="sr-sub">本机 ${x.local.count} 首 / 远端 ${x.remote.count} 首</span>
        <select data-conflict="${esc(x.name)}">
          <option value="local">保留本机</option>
          <option value="remote">用远端覆盖</option>
          <option value="both">两个都留</option>
        </select></div>`).join('')}
    </div>`;
  }

  const lt = d.last_track;
  if (lt) {
    html += `<div class="sync-sec"><label class="sync-row">
      <input type="checkbox" id="syncLt" checked/>
      <span class="sr-name">播放进度</span>
      <span class="sr-sub">切到「${esc((lt.item && lt.item.name) || '')}」
        ${lt.position ? fmtTime(lt.position) : ''}（${fmtAgo(lt.at)}）</span></label>
    </div>`;
  }

  html += `<div class="set-actions" style="padding-top:16px">
    <button class="btn sm primary" data-act="sync-commit">应用所选</button>
    <button class="btn sm ghost" data-close="syncDrawer">取消</button>
  </div>
  <p class="tip" style="padding-top:8px">没勾的部分不会动；本地已有的收藏和歌单不会因为远端没有而被删掉。</p>`;
  box.innerHTML = html;
}

async function syncCommit() {
  const newIds = $$('#syncBody input[data-pl-new]:checked').map((x) => x.dataset.plNew);
  const fillIds = $$('#syncBody input[data-pl-fill]:checked').map((x) => x.dataset.plFill);
  const conflicts = {};
  $$('#syncBody select[data-conflict]').forEach((sel) => {
    if (sel.value && sel.value !== 'local') conflicts[sel.dataset.conflict] = sel.value;
  });
  const payload = {
    favorites: !!($('#syncFav') || {}).checked,
    playlist_new: newIds,
    playlist_fill: fillIds,
    conflicts,
    last_track: !!($('#syncLt') || {}).checked,
  };
  try {
    const r = await post('/api/sync/commit', payload);
    if (!r.ok) throw new Error(r.error || '合并失败');
    const c = r.data || {};
    const parts = [];
    if (c.favorites) parts.push(`收藏 +${c.favorites}`);
    if (c.playlists_new) parts.push(`歌单 +${c.playlists_new}`);
    if (c.playlist_items) parts.push(`曲目 +${c.playlist_items}`);
    if (c.conflicts) parts.push(`分歧处理 ${c.conflicts}`);
    if (c.last_track) parts.push('进度已切');
    $('#syncDrawer').hidden = true;
    await refreshStore();
    renderSide();
    render();
    toast(parts.length ? '已合并：' + parts.join(' · ') : '没有可合并的内容');
  } catch (e) {
    toast(e.message || '合并失败', 'err');
  }
}

/* 曲库目录清单：0 号（下载目录）只读，其余可移除。移除只解除关联，不动文件。 */
function libDirsHtml() {
  const s = state.settings || {};
  const main = s.download_dir || '';
  const extra = Array.isArray(s.local_dirs) ? s.local_dirs : [];
  let html = `<div class="lib-dir main">
    <span class="ld-tag">下载目录</span>
    <span class="ld-path mono" title="${esc(main)}">${esc(main)}</span>
  </div>`;
  extra.forEach((d, i) => {
    html += `<div class="lib-dir">
      <span class="ld-tag alt">曲库 ${i + 1}</span>
      <span class="ld-path mono" title="${esc(d)}">${esc(d)}</span>
      <button class="ic" data-act="lib-remove" data-i="${i}" title="移除（不会删除文件）">${icon('close')}</button>
    </div>`;
  });
  if (!extra.length) {
    html += '<div class="lib-dir none"><span class="ld-path">没有额外目录 · 目前只扫下载目录</span></div>';
  }
  return html;
}

function afterLibChange() {
  const box = $('#libDirs');
  if (box) box.innerHTML = libDirsHtml();
  state.local.loaded = false;
  state.local.items = [];
  state.local.scanning = null;
  loadLocal(true);
}

function eqBandsHtml() {
  const curve = engCurve();
  return EQ_FREQS.map((f, i) => `
    <div class="eq-band">
      <em class="mono" data-eqv="${i}">${curve[i] > 0 ? '+' : ''}${curve[i]}</em>
      <span class="eq-slot">
        <input type="range" min="-12" max="12" step="1" value="${curve[i]}" data-eq="${i}" aria-label="${f}Hz"/>
      </span>
      <span>${f >= 1000 ? (f / 1000) + 'k' : f}</span>
    </div>`).join('');
}

function viewSettings() {
  const s = state.settings;
  return `
  <div class="set-sec">
    <h4>播放与音质</h4>
    <div class="field">
      <div><label>在线播放音质</label><p class="tip">默认 MP3 优先起播快、省流量；播放条右侧规格可随时临时切换</p></div>
      <select id="setPlayQuality">
        <option value="auto">自动（MP3 优先）</option>
        <option value="best">无损优先（FLAC）</option>
        <option value="320">MP3 320k</option>
        <option value="128">MP3 128k</option>
      </select>
    </div>
    <div class="field">
      <div><label>下载音质</label><p class="tip">目标音质缺失时自动降级，不会中断</p></div>
      <select id="setQuality">
        <option value="best">最佳（优先无损）</option>
        <option value="320">MP3 320k</option>
        <option value="128">MP3 128k</option>
      </select>
    </div>
    <div class="field">
      <div><label>音量</label><p class="tip">也可以直接拖播放条右侧的滑杆</p></div>
      <input type="range" id="setVol" min="0" max="100" style="width:130px"/>
    </div>
    <div class="field">
      <div><label>睡眠定时</label><p class="tip">到点暂停播放；「播完当前这首」计完收听后停在曲末。本次会话有效，不保存</p></div>
      <select id="setSleep">
        <option value="off">不启用</option>
        <option value="track">播完当前这首就停</option>
        <option value="15">15 分钟</option>
        <option value="30">30 分钟</option>
        <option value="45">45 分钟</option>
        <option value="60">60 分钟</option>
        <option value="90">90 分钟</option>
      </select>
    </div>
  </div>

  <div class="set-sec">
    <h4>播放引擎</h4>
    <div class="field">
      <div><label>引擎 2.0</label><p class="tip">双缓冲无缝播放与均衡器都靠它；关掉即回到 1.x 的原生播放路径</p></div>
      <input type="checkbox" class="switch" id="setEngineV2"/>
    </div>
    <div class="field">
      <div><label>无缝播放</label><p class="tip">专辑顺序播放时曲末直接接上下一首，不留空档；单曲循环与随机不启用</p></div>
      <input type="checkbox" class="switch" id="setGapless"/>
    </div>
    <div class="field">
      <div><label>交叉淡入淡出</label><p class="tip">曲末让两首叠着过渡；设了就以它为准，否则用 160ms 的短交叉</p></div>
      <select id="setCrossfade">
        <option value="0">关闭（短交叉 160ms）</option>
        <option value="500">0.5 秒</option>
        <option value="1000">1 秒</option>
        <option value="2000">2 秒</option>
      </select>
    </div>
    <div class="field">
      <div><label>均衡器</label><p class="tip">10 段参数均衡，拖动即时生效，只作用于本机播放</p></div>
      <input type="checkbox" class="switch" id="setEqOn"/>
    </div>
    <div class="field">
      <div><label>预设</label><p class="tip">挑一个再微调；动过推子之后自动记为「自定义」</p></div>
      <select id="setEqPreset">
        ${Object.keys(EQ_PRESETS).map((k) => `<option value="${k}">${EQ_LABEL[k]}</option>`).join('')}
        <option value="custom">自定义</option>
      </select>
    </div>
    <div class="eq-bands" id="eqBands">${eqBandsHtml()}</div>
  </div>

  <div class="set-sec">
    <h4>多端同步</h4>
    <div class="field">
      <div><label>服务器</label><p class="tip">你自己的 WebDAV 地址，例如坚果云 https://dav.jianguoyun.com/dav</p></div>
      <input type="text" id="setDavUrl" placeholder="https://dav.jianguoyun.com/dav"/>
    </div>
    <div class="field">
      <div><label>账号</label><p class="tip">WebDAV 的登录邮箱 / 用户名</p></div>
      <input type="text" id="setDavUser" placeholder="you@example.com"/>
    </div>
    <div class="field">
      <div><label>应用密码</label><p class="tip">坚果云要填「应用密码」，不是登录密码</p></div>
      <input type="password" id="setDavPass" placeholder="未设置"/>
    </div>
    <div class="field">
      <div><label>远端目录</label><p class="tip">数据包放在这个目录下的 <span class="mono">elysium-sync.json</span>，没有会自动建</p></div>
      <input type="text" id="setDavDir" placeholder="Elysium"/>
    </div>
    <div class="field">
      <div><label>自动上传</label><p class="tip">本地改动后 5 分钟内最多推一次；拉取永远要你确认，不会自动改本地</p></div>
      <input type="checkbox" class="switch" id="setDavAuto"/>
    </div>
    <div class="set-actions">
      <button class="btn sm" data-act="sync-save">保存并测试</button>
      <button class="btn sm ghost" data-act="sync-push">立即上传</button>
      <button class="btn sm ghost" data-act="sync-pull">从远端拉取</button>
    </div>
    <p class="tip" id="syncTip" style="padding-top:7px">读取中…</p>
  </div>

  <div class="set-sec">
    <h4>界面</h4>
    <div class="field">
      <div><label>主题</label><p class="tip">深色取 Spotify 的密度，浅色取 Apple Music 的通透</p></div>
      <select id="setTheme">
        <option value="dark">深色</option>
        <option value="light">浅色</option>
        <option value="auto">跟随系统</option>
      </select>
    </div>
    ${document.documentElement.dataset.platform === 'android' ? '' : `
    <div class="field">
      <div><label>桌面歌词</label><p class="tip">开一个置顶小窗显示当前歌词行，整窗可拖动；数据只在本机回环里转</p></div>
      <input type="checkbox" class="switch" id="setLyricWin"/>
    </div>`}
  </div>

  <div class="set-sec">
    <h4>个性化推荐</h4>
    <div class="field">
      <div><label>总开关</label><p class="tip">关掉后首页不再生成推荐，本地口味数据仍保留</p></div>
      <input type="checkbox" class="switch" id="setPersonalized"/>
    </div>
    <div class="field" style="display:block">
      <div><label>我的口味</label>
        <p class="tip">写点什么，比如「偏安静的吉他，不要电子」——只留在本机，是你自己的备注</p></div>
      <textarea id="setTasteNote" rows="2" maxlength="280" placeholder="描述你喜欢的音乐…"></textarea>
    </div>
    <div class="field">
      <div><label>口味画像</label><p class="tip" id="tasteSummary">读取中…</p></div>
      <div class="field-acts">
        <button class="btn sm" data-act="taste">查看画像 ›</button>
        <button class="btn sm ghost" data-act="feedback-clear">清除反馈</button>
      </div>
    </div>
  </div>

  <div class="set-sec">
    <h4>下载</h4>
    <div class="field">
      <div><label>保存位置</label><p class="tip ell" id="dirTip">${esc(s.download_dir || '')}</p></div>
      <button class="btn sm" data-act="set-dir">更改</button>
    </div>
    <div class="field">
      <div><label>并发任务</label><p class="tip">同时下载的数量</p></div>
      <select id="setConc">
        <option value="1">1</option><option value="2">2</option>
        <option value="3">3</option><option value="4">4</option>
      </select>
    </div>
    <div class="field">
      <div><label>文件命名</label><p class="tip">可用 {name} {artist} {album} {bitrate}</p></div>
      <input type="text" id="setTpl" placeholder="{name} - {artist}"/>
    </div>
    <div class="field">
      <div><label>同名文件</label><p class="tip">已存在时的处理方式</p></div>
      <select id="setDup">
        <option value="rename">保留两份</option>
        <option value="overwrite">覆盖</option>
        <option value="skip">跳过</option>
      </select>
    </div>
    <div class="field">
      <div><label>写入标签</label><p class="tip">标题、歌手、专辑、封面、歌词写进音频</p></div>
      <input type="checkbox" class="switch" id="setTags"/>
    </div>
    <div class="field">
      <div><label>同时保存 .lrc</label><p class="tip">歌词单独存一份文件</p></div>
      <input type="checkbox" class="switch" id="setLrc"/>
    </div>
  </div>

  <div class="set-sec">
    <h4>试听缓存</h4>
    <div class="field" style="display:block">
      <div><label>自动缓存规则</label>
        <p class="tip">当天听完 2 次进今日缓存（跨天清）；7 天内听完 5 次以上升为长期缓存（3 天没听自动清）</p>
      </div>
    </div>
    <div class="field" style="display:block">
      <div class="meter"><i id="cacheBar" style="width:0%"></i></div>
      <p class="tip" id="cacheText">读取中…</p>
    </div>
    <div class="field">
      <div><label>缓存上限</label><p class="tip">超出后按最久未听淘汰</p></div>
      <select id="setCacheLimit">
        <option value="512">512 MB</option>
        <option value="1024">1 GB</option>
        <option value="2048">2 GB</option>
        <option value="4096">4 GB</option>
        <option value="8192">8 GB</option>
      </select>
    </div>
    <div class="field">
      <div><label>听满自动缓存</label><p class="tip">关掉后仍可试听，只是不落盘</p></div>
      <input type="checkbox" class="switch" id="setAutoCache"/>
    </div>
    <div class="set-actions">
      <button class="btn sm" data-act="cache-clear" data-tier="all">清空全部缓存</button>
      <button class="btn sm" data-act="cache-clear" data-tier="today">只清今日</button>
    </div>
  </div>

  ${document.documentElement.dataset.platform === 'android' ? '' : `
  <div class="set-sec">
    <h4>窗口</h4>
    <div class="field">
      <div><label>托盘图标</label><p class="tip">关掉后应用只活在任务栏；关掉后「收进托盘」也就不成立了。改完重启生效</p></div>
      <input type="checkbox" class="switch" id="setTray"/>
    </div>
    <div class="field">
      <div><label>点关闭按钮时</label><p class="tip">默认退出 —— 不惊喜。选「收进托盘」后歌不会断，从托盘图标再叫回来</p></div>
      <select id="setCloseBehavior">
        <option value="exit">退出应用</option>
        <option value="tray">收进托盘继续放</option>
      </select>
    </div>
    <div class="field">
      <div><label>迷你条</label><p class="tip">缩成 430×138 的小悬浮条，只留封面、切歌和进度；播放条最右侧那个小图标也能切</p></div>
      <button class="btn sm" data-act="mini-toggle">切换</button>
    </div>
  </div>`}

  <div class="set-sec">
    <h4>本地曲库</h4>
    <div class="field" style="display:block">
      <div><label>扫描目录</label>
        <p class="tip">默认只扫下载目录；这里可以再添几个你自己整理的目录。只扫你明确添加的地方，不做全盘扫描</p></div>
      <div id="libDirs" class="lib-dirs">${libDirsHtml()}</div>
      <div class="set-actions">
        <button class="btn sm" data-act="lib-add">添加目录</button>
      </div>
    </div>
  </div>

  <div class="set-sec">
    <h4>数据</h4>
    <div class="set-actions">
      <button class="btn sm" data-act="data-clear" data-what="recent_clear">清除播放记录</button>
      <button class="btn sm" data-act="data-clear" data-what="history_clear">清除搜索历史</button>
      <button class="btn sm" data-act="data-clear" data-what="downloads_clear">清除下载记录</button>
      <button class="btn sm" data-act="data-clear" data-what="favorites_clear">清空收藏</button>
    </div>
  </div>

  <div class="set-sec">
    <h4>更新</h4>
    <div class="field">
      <div><label>版本状态</label><p class="tip" id="updTip">${esc(updTipText())}</p></div>
      <div class="field-acts">
        <button class="btn sm" data-act="upd-check" id="updBtn">检查更新</button>
        <button class="btn sm ghost" data-act="upd-open" id="updOpen" hidden>发布页 ›</button>
        <button class="btn sm ghost" data-act="upd-dismiss" id="updDismiss" hidden>忽略此版本</button>
      </div>
    </div>
    <div id="updAssets"></div>
    <div class="field">
      <div><label>自动检查</label><p class="tip">启动后和每 6 小时各查一次；只提醒，绝不自动下载</p></div>
      <input type="checkbox" class="switch" id="setAutoUpdate"/>
    </div>
  </div>

  <div class="set-ver">ELYSIUM ${state.version ? 'v' + esc(state.version) : ''} · 本机运行</div>`;
}

function fillSettings() {
  const s = state.settings;
  const set = (id, v) => { const el = $(id); if (el) el.value = v; };
  set('#setQuality', s.quality || 'best');
  set('#setPlayQuality', s.play_quality || 'auto');
  set('#setTheme', s.theme || 'dark');
  set('#setConc', String(s.concurrency || 2));
  set('#setDup', s.dup_policy || 'rename');
  set('#setTpl', s.name_template || '{name} - {artist}');
  set('#setCacheLimit', String(s.cache_limit_mb || 2048));
  const vol = $('#setVol');
  if (vol) vol.value = s.volume ?? 80;
  const chk = (id, v) => { const el = $(id); if (el) el.checked = !!v; };
  chk('#setTags', s.write_tags !== false);
  chk('#setLrc', !!s.save_lyric);
  chk('#setAutoCache', s.auto_cache !== false);
  chk('#setPersonalized', s.personalized !== false);
  chk('#setAutoUpdate', s.auto_check_update !== false);
  // 引擎开关没存过值时显示「平台默认」（桌面开、Android 关）
  chk('#setEngineV2', engWant());
  chk('#setGapless', s.gapless !== false);
  chk('#setEqOn', s.eq_enabled === true);
  const cf = Number(s.crossfade_ms) || 0;
  set('#setCrossfade', String([500, 1000, 2000].indexOf(cf) >= 0 ? cf : 0));
  set('#setEqPreset', EQ_PRESETS[s.eq_preset] || s.eq_preset === 'custom' ? s.eq_preset : 'flat');
  // ↑ 预设名不在表里（老版本写坏的值）就退回平直，别让下拉框空着

  // ---- 多端同步（密码不回传，只告诉前端「有没有」）
  const wd = s.webdav || {};
  set('#setDavUrl', wd.url || '');
  set('#setDavUser', wd.user || '');
  set('#setDavDir', wd.dir || 'Elysium');
  const pw = $('#setDavPass');
  if (pw) {
    pw.value = '';
    pw.placeholder = wd.has_pass ? '已保存 · 留空不修改' : '未设置';
  }
  chk('#setDavAuto', wd.auto === true);
  paintSyncTip();

  // ---- 窗口
  chk('#setTray', s.tray !== false);
  set('#setCloseBehavior', s.close_behavior === 'tray' ? 'tray' : 'exit');
  const tn = $('#setTasteNote');
  if (tn) tn.value = (state.taste && state.taste.note) || '';
  getJson('/api/taste').then((d) => {
    const a = (d.data && d.data.artists) || [];
    const el = $('#tasteSummary');
    if (el) el.textContent = a.length
      ? `常听：${a.slice(0, 4).map((x) => x.name).join('、')} · 最近 ${d.data.listened} 首`
      : '还没有足够的收听信号';
  }).catch(() => {});
  refreshCache();
}

async function refreshCache() {
  try {
    const d = await getJson('/api/cache');
    const c = d.data || {};
    const size = c.size || 0;
    const limit = (c.limit || 2048) * 1024 * 1024;
    const bar = $('#cacheBar');
    if (bar) bar.style.width = Math.min(100, (size / limit) * 100).toFixed(1) + '%';
    const txt = $('#cacheText');
    if (txt) {
      const cap = c.limit >= 1024 ? (c.limit / 1024).toFixed(c.limit % 1024 ? 1 : 0) + ' GB' : c.limit + ' MB';
      txt.textContent = `今日 ${c.today_count || 0} 首 · 长期 ${c.long_count || 0} 首 · 共 ${fmtSize(size)} / ${cap}`
        + `　今天已听满的曲目 ${c.heard || 0} 首`;
    }
  } catch (e) { /* 静默 */ }
}

/* ------------------------------------------------------------------ 面板 */
function openPanel(tab) {
  $('#npDrawer').hidden = true;
  $('#settingsDrawer').hidden = true;
  $('#importDrawer').hidden = true;
  $('#panelDrawer').hidden = false;
  setPanelTab(tab || state.panelTab || 'queue');
}
function setPanelTab(tab) {
  state.panelTab = tab;
  $$('.dtab[data-pt]').forEach((b) => b.classList.toggle('on', b.dataset.pt === tab));
  $('#queuePane').hidden = tab !== 'queue';
  $('#dlPane').hidden = tab !== 'dl';
  if (tab === 'queue') renderQueue(); else refreshTasks();
}
function openSettings() {
  $('#npDrawer').hidden = true;
  $('#panelDrawer').hidden = true;
  $('#importDrawer').hidden = true;
  $('#settingsBody').innerHTML = viewSettings();
  $('#settingsDrawer').hidden = false;
  fillSettings();
  bindSettings();
}

/* ------------------------------------------------------------------ 设置绑定 */
function bindSettings() {
  const save = (k, v) => {
    state.settings[k] = v;
    postSettings({ [k]: v });
  };
  const on = (sel, ev, fn) => { const el = $(sel); if (el) el.addEventListener(ev, fn); };

  on('#setQuality', 'change', (e) => { state.quality = e.target.value; save('quality', e.target.value); });
  on('#setPlayQuality', 'change', (e) => { save('play_quality', e.target.value); toast('下次播放起生效，当前曲目可在播放条直接切'); });
  on('#setTheme', 'change', (e) => { save('theme', e.target.value); applyTheme(e.target.value); });
  on('#setConc', 'change', (e) => save('concurrency', Number(e.target.value)));
  on('#setDup', 'change', (e) => save('dup_policy', e.target.value));
  on('#setTpl', 'change', (e) => save('name_template', e.target.value || '{name} - {artist}'));
  on('#setCacheLimit', 'change', async (e) => { save('cache_limit_mb', Number(e.target.value)); refreshCache(); });
  on('#setTags', 'change', (e) => save('write_tags', e.target.checked));
  on('#setLrc', 'change', (e) => save('save_lyric', e.target.checked));
  on('#setAutoCache', 'change', (e) => save('auto_cache', e.target.checked));
  on('#setPersonalized', 'change', (e) => {
    save('personalized', e.target.checked);
    loadRecommend(true);
  });
  on('#setAutoUpdate', 'change', (e) => {
    save('auto_check_update', e.target.checked);
    paintUpdateBadge();
  });
  on('#setTasteNote', 'change', async (e) => {
    try {
      await post('/api/taste_note', { text: e.target.value });
      state.taste = state.taste || {};
      state.taste.note = e.target.value;
      toast('口味备注已保存');
    } catch (err) { toast('保存失败：' + err.message, 'err'); }
  });
  on('#setVol', 'input', (e) => {
    const v = Number(e.target.value);
    engVolume(v / 100);
    $('#vol').value = v;
    state.settings.volume = v;
  });
  on('#setVol', 'change', (e) => save('volume', Number(e.target.value)));
  on('#setSleep', 'change', (e) => {
    const v = e.target.value;
    if (v === 'off' || v === 'track') setSleep(v);
    else setSleep('until', Number(v));
  });
  on('#setLyricWin', 'change', (e) => {
    // pywebview 的 js_api 返回 Promise，普通调用也兼容
    const api = window.pywebview && window.pywebview.api;
    if (!api || typeof api.lyric_window !== 'function') {
      e.target.checked = false;
      toast('这个环境不支持桌面歌词小窗', 'err');
      return;
    }
    try { api.lyric_window(e.target.checked); } catch (err) { /* 静默 */ }
  });

  /* ---- 窗口 ---- */
  on('#setTray', 'change', (e) => {
    save('tray', e.target.checked);
    toast(e.target.checked ? '托盘图标会在下次启动时出现' : '下次启动不再建托盘图标');
  });
  on('#setCloseBehavior', 'change', (e) => {
    save('close_behavior', e.target.value);
    toast(e.target.value === 'tray'
      ? '以后点关闭会收进托盘，歌不会断'
      : '以后点关闭就是退出应用');
  });

  /* ---- 播放引擎 ---- */
  on('#setEngineV2', 'change', async (e) => {
    const want = e.target.checked;
    save('engine_v2', want);
    await engRebuild();          // 元素整只重建：Web Audio 接管不可撤销，只能新建
    toast(want ? '已启用引擎 2.0' : '已回到原生播放路径');
  });
  on('#setGapless', 'change', (e) => {
    save('gapless', e.target.checked);
    ENG.gapless = e.target.checked;
    ENG.cross = engCrossSeconds();
    if (!e.target.checked) engHaltOther();   // 关掉就把已预载的下一首撤了
  });
  on('#setCrossfade', 'change', (e) => {
    const ms = Number(e.target.value) || 0;
    save('crossfade_ms', ms);
    ENG.cross = engCrossSeconds();
    toast(ms ? `交叉淡入淡出 ${ms / 1000} 秒` : '已关闭长交叉（仍走 160ms 短交叉）');
  });
  on('#setEqOn', 'change', (e) => {
    save('eq_enabled', e.target.checked);
    engApplyEq();                // 平滑过渡，切换不当场爆音
    toast(e.target.checked ? '均衡器已开启' : '均衡器已关闭');
  });
  on('#setEqPreset', 'change', (e) => {
    engSetPreset(e.target.value);
    const box = $('#eqBands');
    if (box) box.innerHTML = eqBandsHtml();
  });
  $$('#eqBands input[data-eq]').forEach((inp) => {
    inp.addEventListener('input', (e) => {
      const i = Number(e.target.dataset.eq);
      const db = Number(e.target.value);
      engSetBand(i, db);
      const lab = $(`#eqBands [data-eqv="${i}"]`);
      if (lab) lab.textContent = (db > 0 ? '+' : '') + db;
      const sel = $('#setEqPreset');
      if (sel) sel.value = 'custom';
    });
  });
}

/* ------------------------------------------------------------------ 歌词 */
let currentLyric = '';
async function showLyric() {
  const item = state.playing;
  if (!item) { toast('还没有播放的歌曲'); return; }
  $('#lyricTitle').textContent = `${item.name} — ${item.artist || ''}`;
  $('#lyricBody').innerHTML = '<span class="muted">正在取歌词…</span>';
  $('#lyricLayer').hidden = false;
  lyricLines = []; lyricIdx = -1;
  try {
    const d = await rpc('getlyric', {
      platform: item.platform, id: item.id, time: item.time, sign: item.sign,
    });
    currentLyric = (d && (d.lyric || d.lrc || (typeof d === 'string' ? d : ''))) || '';
    if (!currentLyric && typeof d === 'object') {
      currentLyric = d.lyric || d.lrc || d.text || '';
    }
    const trans = (d && typeof d === 'object' && (d.tlyric || d.tLyric || d.trans)) || '';
    lyricLines = parseLrc(currentLyric, trans);
    renderLyricLines($('#lyricBody'), true);
  } catch (e) {
    $('#lyricBody').innerHTML = `<span class="muted">取歌词失败：${esc(e.message)}</span>`;
  }
}

async function saveLyric() {
  const item = state.playing;
  if (!item || !currentLyric) { toast('没有可保存的歌词'); return; }
  try {
    const d = await post('/api/save-lyric', {
      name: item.name, artist: item.artist, content: currentLyric,
    });
    toast('歌词已保存到 ' + (d.data ? d.data.path : '下载目录'));
  } catch (e) {
    toast(e.message || '保存失败', 'err');
  }
}

/* ------------------------------------------------------------------ 搜索 */
function parseLink(text) {
  const s = String(text || '').trim();
  if (!/^https?:\/\//i.test(s)) return null;
  let u;
  try { u = new URL(s); } catch (e) { return null; }
  const host = u.hostname.replace(/^www\./, '');
  const full = s;
  const grab = (re) => { const m = full.match(re); return m ? m[1] : null; };

  if (host.endsWith('music.163.com')) {
    const hash = u.hash || '';
    const qs = new URLSearchParams((hash.split('?')[1] || u.search.split('?')[1] || ''));
    const id = qs.get('id') || grab(/(\d{4,})/);
    if (/album/.test(full) && id) return { kind: 'album', platform: 'wyy', id };
    if (/playlist/.test(full) && id) return { kind: 'playlist', platform: 'wyy', id };
    if (/song/.test(full) && id) return { kind: 'song', platform: 'wyy', id };
    return id ? { kind: 'song', platform: 'wyy', id } : null;
  }
  if (host.endsWith('kuwo.cn')) {
    if (/album_detail/.test(full)) { const id = grab(/album_detail\/(\d+)/); return id ? { kind: 'album', platform: 'kuwo', id } : null; }
    if (/playlist/.test(full)) { const id = grab(/playlist[^/]*\/(\d+)/) || grab(/[?&]id=(\d+)/); return id ? { kind: 'playlist', platform: 'kuwo', id } : null; }
    const id = grab(/play_detail\/(\d+)/) || grab(/[?&]id=(\d+)/);
    return id ? { kind: 'song', platform: 'kuwo', id } : null;
  }
  return null;
}

/* 换源：用歌名+歌手在全部来源里找最接近的一条，顶掉失效的那条 */
async function switchSource(idx) {
  const it = state.list[idx];
  if (!it) return;
  const kw = [it.name, it.artist].filter(Boolean).join(' ').trim();
  if (!kw) { toast('缺少歌名信息，无法换源'); return; }
  toast(`正在为「${it.name || ''}」寻找其他来源…`);
  try {
    const d = await rpc('search', { keyword: kw, page: 1, size: 20 });
    const norm = (s) => String(s || '').toLowerCase().replace(/\s+/g, '');
    const target = norm(it.name);
    const cands = (d.list || []).filter(
      (x) => !(x.platform === it.platform && String(x.id) === String(it.id)));
    const scored = [];
    for (const c of cands) {
      const n = norm(c.name);
      if (!n) continue;
      const nameHit = n === target ? 0 : ((n.includes(target) || target.includes(n)) ? 1 : 9);
      if (nameHit > 1) continue;
      const dt = it.duration && c.duration ? Math.abs(c.duration - it.duration) : 20;
      scored.push({ c, s: nameHit * 100 + dt });
    }
    scored.sort((a, b) => a.s - b.s);
    const hit = scored[0] && scored[0].c;
    if (!hit) { toast('其他来源也没有这首歌', 'err'); return; }
    // 写回当前列表（搜索结果 / 歌手页 / 收藏都走同一条）
    const arr = state.list;
    arr[idx] = hit;
    render();
    toast(`已换到 ${srcOf(hit).label} · ${hit.name || ''}`);
    playAt(arr, idx);
  } catch (e) {
    toast('换源失败：' + e.message, 'err');
  }
}

async function doSearch(text, opts = {}) {
  const kw = String(text == null ? '' : text).trim();
  if (!kw) return;

  const link = parseLink(kw);
  if (link) { openLink(link); return; }

  state.q = { keyword: kw, page: 1, total: 0, merged: false, empty: [] };
  state.items = [];
  state.loading = true;
  state.route = { name: 'search' };
  render();

  try {
    const d = await rpc('search', { keyword: kw, page: 1, size: 20 });
    state.items = d.list || [];
    state.q.total = d.total || state.items.length;
    state.q.empty = d.empty || [];
    state.q.merged = state.items.some((it) => it.alts && it.alts.length);
    state.q.more = !!d.has_more;
    state.loading = false;
    render();
    if (!state.items.length) toast('两个来源都没搜到，换个关键词试试');
  } catch (e) {
    state.loading = false;
    render();
    toast('搜索失败：' + e.message, 'err');
  }
}

async function loadMore() {
  if (state.loading) return;
  state.loading = true;
  try {
    const page = ++state.q.page;
    const d = await rpc('search', { keyword: state.q.keyword, page, size: 20 });
    const add = d.list || [];
    // 跨页也可能撞上同一首（上一页从另一个源折进来的），展开后整体重折
    state.items = foldRows(expandFolded(state.items).concat(expandFolded(add)));
    state.q.total = state.items.length;
    state.q.more = !!d.has_more;
    state.q.merged = state.items.some((it) => it.alts && it.alts.length);
  } catch (e) {
    toast('加载失败：' + e.message, 'err');
  }
  state.loading = false;
  render();
}

async function openLink(link) {
  if (link.kind === 'album') { openAlbum(link.id, link.platform); return; }
  if (link.kind === 'playlist') { openPlaylist(link.id, link.platform); return; }
  // 单曲链接：搜出这首歌
  try {
    const d = await rpc('song', { platform: link.platform, id: link.id });
    const list = d.list || [];
    if (!list.length) { toast('没解析到这首歌'); return; }
    state.q = { keyword: list[0].name, page: 1, total: list.length };
    state.items = list;
    state.route = { name: 'search' };
    render();
  } catch (e) {
    toast('链接解析失败：' + e.message, 'err');
  }
}

/* 同一张专辑在两个源都有一份 id：默认那份打不开（上游空表）就换备选，
   这就是「重复专辑随机取一个能用的」。 */
async function openAlbum(id, platform, alts) {
  const plat = platform || state.platform;
  state.stack.push(state.route);
  state.route = { name: 'album' };
  state.album = null;
  render();
  $('#view').scrollTop = 0;

  const tries = [{ id, platform: plat }].concat(alts || []);
  let lastErr = null;
  for (const t of tries) {
    if (!t || !t.id) continue;
    try {
      const d = await rpc('album', { platform: t.platform || plat, id: t.id });
      if (d && (d.list || []).length) {
        state.album = { info: d.info || {}, list: d.list || [],
          platform: t.platform || plat, id: t.id };
        render();
        return;
      }
      lastErr = lastErr || new Error('这张专辑在这个来源是空的');
    } catch (e) {
      lastErr = e;
    }
  }
  if (lastErr) toast('专辑打开失败：' + lastErr.message, 'err');
  state.album = { info: {}, list: [], platform: plat, id };
  render();
}

async function openPlaylist(id, platform) {
  const plat = platform || state.platform;
  state.stack.push(state.route);
  state.route = { name: 'search' };
  state.loading = true;
  render();
  try {
    const d = await rpc('playlist', { platform: plat, id, page: 1, size: 100 });
    state.items = d.list || [];
    state.q = { keyword: (d.info && d.info.name) || '歌单', page: 1, total: d.total || state.items.length };
    state.loading = false;
    render();
  } catch (e) {
    state.loading = false;
    render();
    toast('歌单解析失败：' + e.message, 'err');
  }
}

async function openArtist(name, platform, aid) {
  const plat = platform || state.platform;
  const seq = ++state.artistSeq;          // 连点两位歌手时，旧请求的结果不许回填
  state.stack.push(state.route);
  state.route = { name: 'artist' };
  state.artist = null;
  state.artistFilling = false;
  state.tabs.artist = 'song';
  state.artistShown = ARTIST_PAGE;
  state.artistAlbumShown = ARTIST_ALBUM_PAGE;
  render();
  $('#view').scrollTop = 0;

  const base = { platform: plat, name };
  if (aid) base.artist_id = aid;          // 记名歌手 id，优先于按名字猜

  // 两段式：先拿第一页把页面点亮（两个源各一次请求），再后台补齐到上限。
  // 「总曲目」永远是补齐后的量，不是第一屏那几十条。
  try {
    const quick = await rpc('artist', Object.assign({ pages: 1 }, base));
    if (seq !== state.artistSeq) return;
    state.artist = quick;
    state.artistFilling = true;
    render();
  } catch (e) {
    if (seq !== state.artistSeq) return;
    toast('歌手页加载失败：' + e.message, 'err');
    state.artist = { name, list: [], albums: [], platform: plat, reason: '加载失败，稍后再试' };
    render();
    return;
  }

  try {
    const full = await rpc('artist', base);
    if (seq !== state.artistSeq) return;
    state.artist = full;
  } catch (e) {
    if (seq !== state.artistSeq) return;
    toast('补齐曲目失败，先显示已取到的部分', 'err');
  }
  state.artistFilling = false;
  render();
}

/* 滚动到底自动续画：歌手页一次抓到几百首，DOM 按屏续，
   省得用户手点「继续展开」。离底 320px 就预取下一批。 */
let _scrollRaf = 0;
function onViewScroll() {
  if (_scrollRaf) return;
  _scrollRaf = requestAnimationFrame(() => {
    _scrollRaf = 0;
    if (state.route.name !== 'artist' || !state.artist) return;
    const v = $('#view');
    if (!v) return;
    if (v.scrollHeight - v.scrollTop - v.clientHeight > 320) return;
    const d = state.artist;
    if (state.tabs.artist === 'album') {
      if (state.artistAlbumShown < (d.albums || []).length) {
        state.artistAlbumShown += ARTIST_ALBUM_PAGE;
        render();
      }
      return;
    }
    if (state.artistShown < (d.list || []).length) {
      state.artistShown += ARTIST_PAGE;
      render();
    }
  });
}

/* ------------------------------------------------------------------ 事件 */
function bind() {
  $('#view').addEventListener('scroll', onViewScroll, { passive: true });
  /* 队列拖拽重排（Stage C）：委托到 #queuePane，队列重渲染后依然有效 */
  const qpane = $('#queuePane');
  if (qpane) {
    qpane.addEventListener('dragstart', onQueueDragStart);
    qpane.addEventListener('dragover', onQueueDragOver);
    qpane.addEventListener('drop', onQueueDrop);
    qpane.addEventListener('dragend', onQueueDragEnd);
  }

  /* 顶栏 */
  $('#searchForm').addEventListener('submit', (e) => {
    e.preventDefault();
    doSearch($('#kw').value);
  });
  $('#kw').addEventListener('input', (e) => {
    $('#kwClear').hidden = !e.target.value;
  });
  $('#kwClear').addEventListener('click', () => {
    $('#kw').value = '';
    $('#kwClear').hidden = true;
    $('#kw').focus();
  });
  $('#themeBtn').addEventListener('click', () => {
    const cur = document.documentElement.dataset.theme;
    const next = cur === 'dark' ? 'light' : 'dark';
    state.settings.theme = next;
    applyTheme(next);
    postSettings({ theme: next });
    if ($('#settingsDrawer').hidden === false) fillSettings();
  });

  $('#panelBtn').addEventListener('click', () => openPanel());
  $('#panelBtn2').addEventListener('click', () => openNp('queue'));
  $('#plArt').addEventListener('click', () => openNp('lyric'));
  $('#settingsBtn').addEventListener('click', openSettings);
  /* 抽屉遮罩：任一抽屉打开即压暗主区；点遮罩全部关闭。
     用 MutationObserver 盯 hidden 属性，覆盖所有开关路径（互斥切换 / data-close / Escape），免得逐点插桩。 */
  const _drawers = ['#panelDrawer', '#npDrawer', '#settingsDrawer', '#importDrawer'].map((s) => $(s));
  const syncDrawerMask = () => { $('#drawerMask').hidden = !_drawers.some((d) => !d.hidden); };
  const drawerObs = new MutationObserver(syncDrawerMask);
  _drawers.forEach((d) => drawerObs.observe(d, { attributes: true, attributeFilter: ['hidden'] }));
  $('#drawerMask').addEventListener('click', () => _drawers.forEach((d) => { d.hidden = true; }));

  /* 输入浮层（askText） */
  $('#askOk').addEventListener('click', () => _askDone($('#askInput').value));
  $('#askCancel').addEventListener('click', () => _askDone(null));
  $('#askLayer').addEventListener('click', (e) => { if (e.target === e.currentTarget) _askDone(null); });
  $('#askInput').addEventListener('keydown', (e) => {
    e.stopPropagation();                       // 别让全局快捷键（Esc 收起抽屉等）抢走
    if (e.key === 'Enter') { e.preventDefault(); _askDone($('#askInput').value); }
    else if (e.key === 'Escape') { e.preventDefault(); _askDone(null); }
  });

  $('#backBtn').addEventListener('click', goBack);
  $('#plImport').addEventListener('click', openImport);
  $('#importBody').addEventListener('click', onImportClick);
  $('#importBody').addEventListener('change', onImportChange);
  $('#plAdd').addEventListener('click', async () => {
    const name = await askText('新歌单名称');
    if (name == null) return;
    const n = String(name).trim();
    if (!n) { toast('名称不能为空', 'err'); return; }
    plCreate(n).then((ok) => {
      if (!ok) return;
      const pl = state.playlists[state.playlists.length - 1];
      if (pl) go({ name: 'playlist', id: pl.id });
    });
  });
  $('#styleBtn').addEventListener('click', () => {
    $('#settingsDrawer').hidden = true;
    $('#npDrawer').hidden = true;
    go({ name: 'styles' });
    loadStyles(false);
  });

  $('#snav').addEventListener('click', (e) => {
    const b = e.target.closest('.snav-i');
    if (!b) return;
    const v = b.dataset.nav;
    if (v === 'settings') { openSettings(); return; }
    if (v === 'search') { go({ name: 'search' }); $('#kw').focus(); return; }
    go({ name: v });
  });

  /* 抽屉关闭 */
  document.addEventListener('click', (e) => {
    const c = e.target.closest('[data-close]');
    if (c) {
      const el = document.getElementById(c.dataset.close);
      if (el) el.hidden = true;
      return;
    }
    // 点规格按钮本身不在这里关，交给 openPlayQualityMenu 做开关切换
    if (!e.target.closest('#qmenu') && !e.target.closest('[data-act="pq"]')) closeQualityMenu();
  });

  $$('.dtab[data-pt]').forEach((b) => b.addEventListener('click', () => setPanelTab(b.dataset.pt)));

  /* 播放器 */
  $('#playBtn').addEventListener('click', () => {
    const a = audio();
    if (!a.src) {
      /* 还没有音频源，两种可能：
         ① 启动时恢复了「上次听到」——播放条上有歌，但故意没预载（签名会过期）；
         ② 点了一首歌但还没起播。
         两种都该接着播放条上这首来，而不是从当前列表第一首重头开始。
         原来是靠 restoreLast 预载 a.src 蒙混过去的，去掉预载后必须显式处理。 */
      if (state.playing) {
        const pos = state.resumePos || 0;
        state.resumePos = 0;
        playAt([state.playing], 0, { position: pos, source: '上次会话' });
        return;
      }
      const list = state.list.length ? state.list : state.items;
      if (list.length) playAt(list, 0);
      else if (state.recent.length) playAt(state.recent, 0);
      else toast('先搜一首歌');
      return;
    }
    if (a.paused) a.play().catch(() => {}); else a.pause();
  });
  $('#prevBtn').addEventListener('click', () => playNext(-1));
  $('#nextBtn').addEventListener('click', () => playNext(1));
  $('#modeBtn').addEventListener('click', cycleMode);
  $('#lyricBtn').addEventListener('click', () => openNp('lyric'));
  $('#miniBtn').addEventListener('click', () => setMini(document.documentElement.dataset.mini !== 'on'));
  $('#lyricSave').addEventListener('click', saveLyric);
  $('#plFav').addEventListener('click', () => {
    if (!state.playing) return;
    toggleFav('song', state.playing).then((on) => {
      $('#plFav').innerHTML = icon(on ? 'heart' : 'heart-o');
      $('#plFav').classList.toggle('on', on);
    });
  });

  const vol = $('#vol');
  vol.addEventListener('input', () => {
    engVolume(Number(vol.value) / 100);
    state.settings.volume = Number(vol.value);
    const sv = $('#setVol'); if (sv) sv.value = vol.value;
  });
  vol.addEventListener('change', () => {
    post('/api/store', { op: 'settings', payload: { volume: Number(vol.value) } }).catch(() => {});
  });
  $('#volBtn').addEventListener('click', () => {
    const a = audio();
    a.muted = !a.muted;
    $('#volBtn').innerHTML = icon(a.muted ? 'vol-x' : 'vol');
  });

  /* 进度条拖动 */
  const seek = $('#seekBar');
  const seekAt = (e) => {
    const a = audio();
    const d = a.duration || 0;
    if (!d) return;
    const r = seek.getBoundingClientRect();
    const x = Math.min(Math.max(0, e.clientX - r.left), r.width);
    a.currentTime = (x / r.width) * d;
    paintSeek();
  };
  seek.addEventListener('pointerdown', (e) => {
    seek.setPointerCapture(e.pointerId);
    seekAt(e);
    const move = (ev) => seekAt(ev);
    const up = () => {
      seek.removeEventListener('pointermove', move);
      seek.removeEventListener('pointerup', up);
      saveLastTrack(true);
    };
    seek.addEventListener('pointermove', move);
    seek.addEventListener('pointerup', up);
  });

  /* 媒体事件见 bindAudioEl()（引擎要重建元素，绑定跟着元素走） */

  /* 歌词容器：用户手动滚动后停止自动跟随，滚回当前行才恢复。
     程序自己触发的 smooth 滚动也会冒出 wheel 事件，所以用 lyricScrolling 排除掉，
     否则每次程序滚动都会被误判成「用户在滚」，随后彻底失去跟随。 */
  ['npLyricBody', 'lyricBody'].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    const mark = () => {
      if (lyricScrolling) return;
      lyricFollow = false;
    };
    ['wheel', 'touchmove', 'pointerdown'].forEach((ev2) =>
      el.addEventListener(ev2, mark, { passive: true }));
    // 滚回当前高亮行附近 → 恢复跟随
    el.addEventListener('scroll', () => {
      if (lyricScrolling || lyricFollow || lyricIdx < 0) return;
      const cur = el.children[lyricIdx];
      if (!cur) return;
      const boxRect = el.getBoundingClientRect();
      const curRect = cur.getBoundingClientRect();
      if (curRect.top <= boxRect.top + boxRect.height / 2
          && curRect.bottom >= boxRect.top + boxRect.height / 2) {
        lyricFollow = true;
      }
    }, { passive: true });
  });

  /* 键盘 */
  document.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      if (e.key === 'Escape') e.target.blur();
      return;
    }
    if (e.code === 'Space') { e.preventDefault(); $('#playBtn').click(); }
    else if (e.code === 'ArrowRight') { e.preventDefault(); const x = audio(); if (x.duration) x.currentTime = Math.min(x.duration, x.currentTime + 5); }
    else if (e.code === 'ArrowLeft') { e.preventDefault(); const x = audio(); x.currentTime = Math.max(0, x.currentTime - 5); }
    else if (e.code === 'ArrowUp') { e.preventDefault(); showVol(+5); }
    else if (e.code === 'ArrowDown') { e.preventDefault(); showVol(-5); }
    else if (e.code === 'KeyL') { e.preventDefault(); openNp('lyric'); }
    else if (e.code === 'KeyQ') { e.preventDefault(); openNp('queue'); }
    else if (e.code === 'KeyM') {
      e.preventDefault();
      const a = audio();
      a.muted = !a.muted;
      $('#volBtn').innerHTML = icon(a.muted ? 'vol-x' : 'vol');
      toast(a.muted ? '已静音' : '已取消静音');
    }
    else if (e.code === 'Slash') { e.preventDefault(); $('#kw').focus(); $('#kw').select(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') { e.preventDefault(); $('#kw').focus(); $('#kw').select(); }
    else if (e.key === 'Escape') { $('#npDrawer').hidden = true; closeQualityMenu(); closeCtxMenu(); }
  });

  window.addEventListener('pagehide', () => saveLastTrack(true));
  window.addEventListener('beforeunload', () => saveLastTrack(true));
  // 页面滚动时收起浮层（浮层是 fixed，滚动后会脱离锚点）
  window.addEventListener('scroll', () => { closeQualityMenu(); closeCtxMenu(); }, true);

  /* 下载轮询 */
  setInterval(() => {
    if (!$('#panelDrawer').hidden || state.tasks.some((t) => ACTIVE.includes(t.state))) refreshTasks();
  }, 1200);
}

function showVol(delta) {
  const v = Math.min(100, Math.max(0, Number($('#vol').value) + delta));
  $('#vol').value = v;
  engVolume(v / 100);
  state.settings.volume = v;
  toast('音量 ' + v);
  post('/api/store', { op: 'settings', payload: { volume: v } }).catch(() => {});
}

/* ------------------------------------------------------------------ 交互动作（事件委托） */
document.addEventListener('contextmenu', (e) => {
  // 右键落在可加歌单的载体上才弹菜单，否则放浏览器默认菜单
  const target = ctxTargetOf(e.target);
  if (!target) return;
  e.preventDefault();
  closeQualityMenu();
  openCtxMenu(e.clientX, e.clientY, target);
});

/* 判断右键落点属于哪类可加入歌单的对象 */
function ctxTargetOf(node) {
  if (!node || !node.closest) return null;
  // 曲目行（搜索结果 / 收藏 / 歌单 / 队列）
  const tr = node.closest('.tr');
  if (tr) {
    const i = Number(tr.dataset.row);
    const it = state.list[i];
    if (it) return { kind: 'song', name: it.name, item: it };
  }
  // 专辑卡
  const alb = node.closest('[data-act="album"]');
  if (alb && alb.dataset.id) {
    return { kind: 'album', id: alb.dataset.id, platform: alb.dataset.platform,
             name: (alb.querySelector('.card-name') || {}).textContent || '专辑' };
  }
  // 歌手卡
  const art = node.closest('[data-act="artist"]');
  if (art && art.dataset.name) {
    return { kind: 'artist', name: art.dataset.name, platform: art.dataset.platform,
             artist_id: art.dataset.aid || '' };
  }
  return null;
}

document.addEventListener('click', async (e) => {
  // 右键菜单：点外部关闭。放在这里（而不是 bind() 里）是因为本监听器
  // 注册得更早 —— 菜单按钮被点击后 openPlaylistPicker 会重建 innerHTML，
  // 旧按钮会脱离 DOM，此时 closest('#ctxmenu') 返回 null，
  // 若在别的监听器里判断就会误判成「点了外部」。
  if (!e.target.closest('#ctxmenu')) closeCtxMenu();

  const el = e.target.closest('[data-act]');
  if (!el) return;
  const act = el.dataset.act;
  const idx = el.dataset.i != null ? Number(el.dataset.i) : -1;

  // 卡片上的播放键：不要冒泡成打开页面
  if (act === 'album-play' || act === 'artist-play' || act === 'card-play') {
    e.stopPropagation();
  }

  switch (act) {
    case 'nav': {
      const v = el.dataset.view;
      if (v === 'library') state.tabs.library = el.dataset.tab || 'song';
      go({ name: v });
      break;
    }
    case 'quick': doSearch(el.dataset.kw); break;
    case 'resume': {
      const lt = pickLastTrack();
      if (!lt || !lt.item) { toast('没有上次播放记录'); break; }
      playAt([lt.item], 0, { position: lt.position || 0, source: '上次会话' });
      break;
    }
    case 'stab': state.tabs.search = el.dataset.tab; render(); break;
    case 'atab': {
      state.tabs.artist = el.dataset.tab || 'song';
      render();
      break;
    }
    case 'artist-more': {
      const kind = el.dataset.kind || 'song';
      if (kind === 'album') state.artistAlbumShown += ARTIST_ALBUM_PAGE;
      else state.artistShown += ARTIST_PAGE;
      render();
      break;
    }
    case 'altswap': altswap(Number(el.dataset.i)); break;
    case 'swap': switchSource(Number(el.dataset.i)); break;
    case 'reco-play': playAt((state.reco && state.reco.list) || [], Number(el.dataset.i), { source: '为你推荐' }); break;
    case 'reco-expand': {
      const want = state.recoExpanded ? 12 : 26;
      state.recoExpanded = !state.recoExpanded;
      await loadRecommend(false, want);
      paintReco();
      break;
    }
    case 'shelf-prev':
    case 'shelf-next': {
      const row = shelfRowOf(el);
      if (!row) break;
      const step = Math.max(160, Math.round(row.clientWidth * 0.82));
      row.scrollBy({ left: el.dataset.act === 'shelf-next' ? step : -step, behavior: 'smooth' });
      break;
    }
    case 'recent-expand': state.recentExpanded = !state.recentExpanded; render(); break;
    case 'tile-lib': go({ name: 'library' }); break;
    case 'tile-styles': {
      // 首次进页面就先把 loading 立起来，别先闪一个「没有歌单」的空态再换掉
      if (!((state.styles && state.styles.cards) || []).length) state.styleLoading = true;
      go({ name: 'styles' });
      if (await loadStyles(false)) paintStyles();
      break;
    }
    case 'mini-toggle':
      setMini(document.documentElement.dataset.mini !== 'on');
      break;
    case 'lib-add': {
      try {
        let path = '';
        if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_dir) {
          path = await window.pywebview.api.pick_dir();
        } else {
          path = await askText('输入曲库目录的完整路径', '') || '';
        }
        if (!path) break;
        const norm = (x) => String(x || '').replace(/[\/]+$/, '').toLowerCase();
        const cur = Array.isArray(state.settings.local_dirs) ? state.settings.local_dirs.slice() : [];
        if (norm(path) === norm(state.settings.download_dir || '')) {
          toast('这就是下载目录，不用再加');
          break;
        }
        if (cur.some((x) => norm(x) === norm(path))) {
          toast('这个目录已经在列表里了');
          break;
        }
        cur.push(path);
        await post('/api/store', { op: 'settings', payload: { local_dirs: cur } });
        state.settings.local_dirs = cur;
        afterLibChange();
        toast('已添加，正在重新扫描…');
      } catch (err) { toast(err.message, 'err'); }
      break;
    }
    case 'lib-remove': {
      const i = Number(el.dataset.i);
      const cur = (state.settings.local_dirs || []).slice();
      if (!cur[i]) break;
      cur.splice(i, 1);
      try {
        await post('/api/store', { op: 'settings', payload: { local_dirs: cur } });
        state.settings.local_dirs = cur;
        afterLibChange();
        toast('已移除（文件没有被删除）');
      } catch (err) { toast(err.message, 'err'); }
      break;
    }
    case 'sync-save':
      await syncSave();
      break;
    case 'sync-push':
      await syncPush();
      break;
    case 'sync-pull':
      await syncPull();
      break;
    case 'sync-commit':
      await syncCommit();
      break;
    case 'taste':
      $('#settingsDrawer').hidden = true;
      $('#npDrawer').hidden = true;
      go({ name: 'taste' });
      loadTaste();
      break;
    case 'style-refresh':
      await refreshStyles(el);
      break;
    case 'style-play': {
      const gid = el.dataset.g;
      const c = ((state.styles && state.styles.cards) || []).find((x) => x.genre === gid);
      if (!c || !c.list.length) break;
      playAt(c.list, 0, { source: '全风格 · ' + c.name });
      break;
    }
    case 'style-one': {
      const gid = el.dataset.g;
      const c = ((state.styles && state.styles.cards) || []).find((x) => x.genre === gid);
      if (!c) break;
      const i = Number(el.dataset.i);
      if (!c.list[i]) break;
      playAt(c.list, i, { source: '全风格 · ' + c.name });
      break;
    }
    case 'taste-save': {
      const txt = ($('#tasteNote') || {}).value || '';
      await post('/api/taste_note', { text: txt });
      toast('口味备注已保存');
      loadTaste();
      break;
    }
    case 'gpick': {
      const gid = el.dataset.g;
      if (!gid) break;
      const cur = new Set(state.taste.genres || []);
      if (cur.has(gid)) cur.delete(gid); else cur.add(gid);
      const ids = [...cur];
      // 先本地更新再落盘，点完立刻看到高亮，不等网络
      state.taste.genres = ids;
      el.classList.toggle('on', cur.has(gid));
      await post('/api/taste_genres', { ids });
      const cnt = el.closest('.taste-card')?.querySelector('.cnt');
      if (cnt) cnt.textContent = pad(ids.length);
      toast(ids.length ? `已选 ${ids.length} 个风格` : '已清空风格选择');
      break;
    }
    case 'taste-toggle': {
      const on = !!el.checked;
      state.settings.personalized = on;
      await post('/api/store', { op: 'settings', payload: { personalized: on } });
      toast(on ? '已开启个性化推荐' : '已关闭个性化推荐');
      // 重新取一次状态，首页那块货架才会跟着变（关掉时显示「已关闭」而不是留着旧卡）
      await loadRecommend(true);
      break;
    }
    case 'reco-add': {
      const it = ((state.reco && state.reco.list) || [])[Number(el.dataset.i)];
      if (it) { state.playList.push(it); renderQueue(); toast('已加入播放队列'); }
      break;
    }
    case 'reco-like': recoFeedback('like', Number(el.dataset.i)); break;
    case 'reco-dislike': recoFeedback('exclude', Number(el.dataset.i)); break;
    case 'reco-refresh': await refreshReco(el); break;
    case 'feedback-clear': {
      // 这是不可撤销的破坏性操作：清掉的是「喜欢/排除」的歌手反馈。
      // 跟「清空收藏」「删除歌单」同级，必须先问一句。
      // 注意后端只清 feedback，不动 taste_genres —— 手动选的风格是明确意图，
      // 不算反馈。文案必须跟这个行为一致，别让用户以为连风格一起清了。
      if (!confirm('清除全部反馈？\n\n会清掉你对歌手的「喜欢 / 排除」，'
                   + '口味画像需要重新积累。\n'
                   + '已选风格、收藏、歌单和收听记录都不受影响。')) break;
      try {
        await post('/api/store', { op: 'feedback_clear' });
        await loadRecommend(true);
        paintReco();
        if (state.route && state.route.name === 'taste') render();
        toast('反馈记录已清空');
      } catch (e) {
        toast('清除失败：' + e.message, 'err');
      }
      break;
    }
    case 'ltab': state.tabs.library = el.dataset.tab; render(); break;
    case 'more': loadMore(); break;

    /* ------------------------------------------------------ K 歌 / 作品 */
    case 'karaoke': karaokeStart(state.playing); break;
    case 'ctx-karaoke': {
      const t = _ctxTarget;
      closeCtxMenu();
      if (t && t.item) karaokeStart(t.item);
      break;
    }
    case 'k-begin': karaokeBegin(); break;
    case 'k-finish': karStop(); break;
    case 'k-save': karSave(); break;
    case 'k-discard':
      karClose();
      toast('已丢弃，没有保存');
      break;
    case 'k-close':
      // 唱到一半点关闭：先转回顾页（录音还在），别一把丢掉用户的真声
      if (KAR.phase === 'sing') karStop();
      else karClose();
      break;
    case 'w-play': {
      const i = (state.works || []).findIndex((w) => w.id === el.dataset.id);
      if (i >= 0) playAt(state.works.map(workAsTrack), i, { source: '我的作品' });
      break;
    }
    case 'w-playall':
      if ((state.works || []).length) playAt(state.works.map(workAsTrack), 0, { source: '我的作品' });
      break;
    case 'w-del': workDelete(el.dataset.id); break;

    case 'playlist': go({ name: 'playlist', id: el.dataset.id }); break;
    case 'ctx-add-pl': {
      const t = _ctxTarget;
      closeCtxMenu(true);                 // 保留 target：下一步还要用它选歌单
      if (t) openPlaylistPicker(t);
      break;
    }
    case 'pl-pick': {
      const t = _ctxTarget;
      const pid = el.dataset.id;
      closeCtxMenu();
      if (t) await doAddToPlaylist(pid, t);
      break;
    }
    case 'pl-pick-new': {
      const t = _ctxTarget;
      closeCtxMenu();
      const name = await askText('新歌单名称');
      if (name == null) return;
      const n = String(name).trim();
      if (!n) { toast('名称不能为空', 'err'); return; }
      if (!await plCreate(n)) return;
      const pl = state.playlists[state.playlists.length - 1];
      if (t && pl) await doAddToPlaylist(pl.id, t);
      break;
    }
    case 'pl-play': {
      const pl = plById(el.dataset.id);
      if (pl && (pl.items || []).length) playAt(pl.items, 0, { source: pl.name });
      break;
    }
    case 'pl-rename': {
      const pl = plById(el.dataset.id);
      if (!pl) break;
      const name = await askText('歌单名称', pl.name);
      if (name == null) break;
      const n = String(name).trim();
      if (!n) { toast('名称不能为空', 'err'); break; }
      plOp('playlist_rename', { id: pl.id, name: n });
      break;
    }
    case 'pl-delete': {
      const pl = plById(el.dataset.id);
      if (!pl) break;
      if (!confirm(`删除歌单「${pl.name}」？\n歌单里的歌不会被删除。`)) break;
      plOp('playlist_delete', { id: pl.id });
      if (state.route.name === 'playlist' && state.route.id === pl.id) go({ name: 'library' });
      break;
    }

    case 'play': {
      const list = state.list;
      if (list[idx]) playAt(list, idx);
      break;
    }
    case 'play-all': {
      const list = state.list;
      if (list.length) playAt(list, 0);
      break;
    }
    case 'local-refresh': loadLocal(true); break;
    case 'local-playall': {
      const list = state.local.items;
      if (list.length) playAt(list, 0, { source: '本地音乐' });
      break;
    }
    case 'card-play': {
      const it = state.recent.find((x) => String(x.id) === el.dataset.id && x.platform === el.dataset.platform);
      if (it) playAt([it], 0);
      break;
    }

    case 'album': openAlbum(el.dataset.id, el.dataset.platform, parseAlts(el.dataset.alts)); break;
    case 'artist': openArtist(el.dataset.name, el.dataset.platform, el.dataset.aid); break;
    case 'album-play': {
      const plat = el.dataset.platform;
      openAlbum(el.dataset.id, plat, parseAlts(el.dataset.alts)).then(() => {
        if (state.album && state.album.list.length) playAt(state.album.list, 0);
      });
      break;
    }
    case 'artist-play': {
      openArtist(el.dataset.name, el.dataset.platform, el.dataset.aid).then(() => {
        if (state.artist && state.artist.list.length) playAt(state.artist.list, 0);
      });
      break;
    }
    case 'artist-play-all': {
      if (state.artist && state.artist.list.length) playAt(state.artist.list, 0);
      break;
    }

    case 'fav': {
      const it = state.list[idx];
      if (!it) break;
      const on = await toggleFav('song', it);
      el.innerHTML = icon(on ? 'heart' : 'heart-o');
      el.classList.toggle('on', on);
      if (state.playing && String(state.playing.id) === String(it.id)) {
        $('#plFav').innerHTML = icon(on ? 'heart' : 'heart-o');
        $('#plFav').classList.toggle('on', on);
      }
      break;
    }
    case 'fav-album': {
      const d = state.album;
      if (!d) break;
      const info = d.info || {};
      const payload = {
        id: String(info.id || ''), platform: d.platform, name: info.name || '',
        artist: info.artist || '', cover: info.pic_url || '', year: info.year || '',
      };
      const on = await toggleFav('album', payload);
      toast(on ? '已收藏专辑' : '已取消收藏');
      break;
    }
    case 'fav-artist': {
      const d = state.artist;
      if (!d) break;
      const payload = {
        id: d.name, platform: d.platform, name: d.name,
        artist_id: d.artist_id || '',
        cover: (d.list[0] || {}).cover || '', count: d.list.length,
      };
      const on = await toggleFav('artist', payload);
      toast(on ? '已收藏歌手' : '已取消收藏');
      break;
    }

    case 'dl': {
      const it = state.list[idx];
      if (!it) break;
      openQualityMenu(el, [await ensureMinfo(it)]);
      break;
    }
    case 'dl-all': {
      const list = state.list;
      if (!list.length) break;
      // 菜单选项由首条的 minfo 生成；歌单条目没有 minfo，回源补一条，
      // 其余条目交给后端的合成降级档兜底
      const first = await ensureMinfo(list[0]);
      openQualityMenu(el, [first].concat(list.slice(1)));
      break;
    }
    case 'dl-artist': {
      if (state.artist && state.artist.list.length) openQualityMenu(el, state.artist.list);
      break;
    }
    case 'pick-q': {
      const [format, bitrate] = String(el.dataset.q).split(':');
      const menu = $('#qmenu');
      const mode = menu._mode;
      const items = menu._items || [];
      closeQualityMenu();
      if (mode === 'play') switchQuality({ format, bitrate });
      else enqueue(items, `${format}:${bitrate}`);
      break;
    }
    case 'pq': openPlayQualityMenu(el); break;
    case 'nptab': state.npTab = el.dataset.tab || 'lyric'; renderNp(); break;
    case 'lyric-full': showLyric(); break;
    case 'lrc-off': {
      /* ±0.5s 累进、复位归零；±10s 封顶防手滑。改完立即重算当前行并落设置。 */
      const isReset = el.dataset.d === '0';
      const cur = Math.round(Number(state.settings.lrc_offset_ms) || 0);
      const ms = isReset ? 0
        : Math.max(-10000, Math.min(10000, cur + Math.round(Number(el.dataset.d) * 1000)));
      state.settings.lrc_offset_ms = ms;
      LRC_OFF = ms / 1000;
      postSettings({ lrc_offset_ms: ms });
      syncLrcOffUI();
      updateLyricTick();
      break;
    }
    case 'lyric-seek': {
      const l = lyricLines[Number(el.dataset.i)];
      const a2 = audio();
      if (l && a2 && a2.src) {
        a2.currentTime = l.t;
        lyricFollow = true;          // 主动点行 = 想跟着这句看，恢复跟随
        updateLyricTick();
      }
      break;
    }

    case 'qplay': {
      maybeCountOnLeave();
      playAt(state.playList, idx, { source: state.queueSource });
      break;
    }
    case 'qnext': {
      e.stopPropagation();
      const from = idx;
      const cur = state.playIndex;
      if (from === cur || from === cur + 1) { toast('已经是下一首'); break; }
      const arr = state.playList.slice();
      const [it] = arr.splice(from, 1);
      const at = from < cur ? cur : cur + 1;
      arr.splice(at, 0, it);
      state.playList = arr;
      state.playIndex = from < cur ? cur - 1 : cur;
      renderQueue();
      toast('已插到下一首');
      break;
    }
    case 'qremove': {
      e.stopPropagation();
      if (idx < 0) break;
      state.playList.splice(idx, 1);
      if (idx < state.playIndex) state.playIndex -= 1;
      renderQueue();
      break;
    }
    case 'qclear': state.playList = []; state.playIndex = -1; renderQueue(); break;

    case 'dl-cancel': await post('/api/downloads/cancel', { id: el.dataset.id }); refreshTasks(); break;
    case 'dl-retry': await post('/api/downloads/retry', { id: el.dataset.id }); refreshTasks(); break;
    case 'dl-remove': await post('/api/downloads/remove', { id: el.dataset.id }); refreshTasks(); break;
    case 'dl-clear': {
      for (const t of state.tasks.filter((x) => x.state === 'done')) {
        await post('/api/downloads/remove', { id: t.id });
      }
      refreshTasks();
      break;
    }

    case 'upd-check': {
      const btn = $('#updBtn');
      if (btn) { btn.disabled = true; btn.textContent = '检查中…'; }
      try {
        await loadUpdateStatus(true);
        const u = state.update;
        if (u && u.has_update) toast(`发现新版本 ${u.latest}`);
        else if (u && u.latest && !u.error) toast('已是最新版本');
      } catch (err) {
        state.update = null;
        paintUpdateSection();
        const tip = $('#updTip');
        if (tip) tip.textContent = '检查失败：' + err.message;
      }
      if (btn) { btn.disabled = false; btn.textContent = '检查更新'; }
      break;
    }
    case 'upd-open':
    case 'upd-open-url': {
      openExternal(el.dataset.url);
      break;
    }
    case 'upd-dl': {
      try {
        const d = await post('/api/update/download', {
          url: el.dataset.url || '', sha256: el.dataset.sha || '',
        });
        state.updateDl = d.data || { state: 'idle' };
        paintUpdateAssets();
        pollUpdateDl();                      // 立刻接管轮询（页面刷新后也靠它恢复进度）
      } catch (err) { toast(err.message || '下载启动失败', 'err'); }
      break;
    }
    case 'upd-install': {
      const bridge = window.ElysiumAndroid;
      const path = el.dataset.path || '';
      if (bridge && bridge.installUpdate) {
        const ok = bridge.installUpdate(path);
        toast(ok ? '已调起系统安装器，请确认安装' : '调起安装器失败（文件不存在或权限不足）',
              ok ? '' : 'err');
      } else {
        toast('此版本不支持应用内安装，请用文件管理器手动安装 APK', 'err');
      }
      break;
    }
    case 'upd-copy': {
      const ok = await copyText(el.dataset.url || '');
      toast(ok ? '已复制直链，可粘贴到浏览器或下载器' : '复制失败，请手动选中链接复制', ok ? '' : 'err');
      break;
    }
    case 'upd-dismiss': {
      const u = state.update;
      if (!u || !u.latest) break;
      state.settings.update_dismissed = u.latest;
      await post('/api/store', { op: 'settings', payload: { update_dismissed: u.latest } });
      paintUpdateBadge();
      paintUpdateSection();
      toast(`已忽略 ${u.latest}，下个新版本会再提醒`);
      break;
    }
    case 'set-dir': {
      try {
        let path = '';
        if (window.pywebview && window.pywebview.api && window.pywebview.api.choose_dir) {
          path = await window.pywebview.api.choose_dir();
        } else {
          path = await askText('输入下载目录的完整路径', state.settings.download_dir || '') || '';
          if (path) {
            state.settings.download_dir = path;
            await post('/api/store', { op: 'settings', payload: { download_dir: path } });
          }
        }
        if (path) {
          state.settings.download_dir = path;
          const tip = $('#dirTip'); if (tip) tip.textContent = path;
          toast('保存位置已更新');
        }
      } catch (err) {
        toast('更改失败：' + err.message, 'err');
      }
      break;
    }
    case 'cache-clear': {
      try {
        await post('/api/cache/clear', { tier: el.dataset.tier || 'all' });
        toast('缓存已清理');
        refreshCache();
      } catch (err) { toast(err.message, 'err'); }
      break;
    }
    case 'data-clear': {
      const what = el.dataset.what;
      if (what === 'favorites_clear' && !confirm('确定清空所有收藏（歌曲、歌手、专辑）？')) break;
      try {
        await post('/api/store', { op: what, payload: {} });
        await refreshStore();
        renderSide();
        render();
        toast('已清除');
      } catch (err) { toast(err.message, 'err'); }
      break;
    }
    case 'recent-clear': {
      await post('/api/store', { op: 'recent_clear', payload: {} });
      state.recent = [];
      render();
      toast('播放记录已清除');
      break;
    }
    default: break;
  }
});

/* ------------------------------------------------------------------ 启动 */
async function refreshStore() {
  try {
    const d = await getJson('/api/store');
    state.settings = d.settings || {};
    LRC_OFF = (Number(state.settings.lrc_offset_ms) || 0) / 1000;
    state.version = d._version || '';    // 单一来源：别在前端再写死一份版本号
    state.favs = d.favorites || [];
    state.playlists = d.playlists || [];
    state.recent = d.recent || [];
    state.downloads = d.downloads || [];
    state.platform = state.settings.platform || 'kuwo';
    state.quality = state.settings.quality || 'best';
    state.mode = state.settings.mode || 'order';
    applyTheme(state.settings.theme || 'dark');
    $('#vol').value = state.settings.volume ?? 80;
    engVolume((Number(state.settings.volume ?? 80)) / 100);
    const m = MODES.find((x) => x.key === state.mode) || MODES[0];
    $('#modeBtn').innerHTML = icon(m.icon);
    $('#modeBtn').title = m.label;
    $('#modeBtn').classList.toggle('on', state.mode !== 'order');
    engBoot();                 // 设置到手才开始搭音频图（要读 engine_v2 / eq_* 决定取舍）
  } catch (e) {
    toast('读取本地数据失败：' + e.message, 'err');
  }
}

/* ------------------------------------------------------------------ K 歌（P8）
   任选曲库里的歌开口唱。三条边界一开始就定死：
   1. 音频图与播放器完全隔离：专用 <audio> + 专用 AudioContext，关层即
      ctx.close() —— 浏览器对 AudioContext 实例数有上限（约 6 个），不还就饿死。
   2. 录音只录 K 歌自己的混音总线（伴奏 + 人声），不碰播放器链路、不写
      听歌统计 —— 唱歌不是「听歌」，不进 plays。
   3. 消人声是经典中置相减（L−R），对「人声居中、两侧乐器」的混音有效，
      效果因歌而异 —— UI 如实说，且随时可切回原声跟唱，不假装是 AI 分离。 */
const KAR = {
  open: false, item: null, phase: '',     // '' | 'ready' | 'count' | 'sing' | 'review'
  ctx: null, el: null, cancel: null, orig: null, acc: null,
  micGain: null, wet: null, micLevel: 0, micStream: null,
  chunks: [], lines: [], sung: [], lastLine: -1, raf: 0,
  accVol: 0.9, micVol: 1.0, vocalOff: false,
  resume: null, t0: 0, saving: false, duration: 0,
};
const KAR_SING_THR = 0.035;   // 跟唱判定：行内 mic RMS 超过它算「唱了」（假麦正弦 ≈ 0.7）
/* getUserMedia 必须绑在 mediaDevices 上调用：摘成裸函数再调会 Illegal invocation。 */
const KAR_MON = () => (navigator.mediaDevices && navigator.mediaDevices.getUserMedia)
  ? navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices) : null;

function karLineAt(t) {
  const ls = KAR.lines;
  let lo = 0, hi = ls.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (ls[mid].t <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}

async function karaokeStart(item) {
  if (!item || !item.id) return;
  if (KAR.open) { toast('已经在 K 歌了'); return; }
  if (!KAR_MON()) {
    toast('这个窗口拿不到麦克风。用系统浏览器打开 ' + location.origin + ' 就能 K 歌', 'err');
    return;
  }
  // 播放器让位：暂停并记下位置，退出 K 歌回到原位（不自动续播，避免惊喜）
  const a = audio();
  if (state.playing && a.src) {
    KAR.resume = { position: a.currentTime || 0 };
    a.pause();
  } else KAR.resume = null;
  KAR.item = item;
  KAR.phase = 'ready';
  KAR.chunks = [];
  KAR.lastLine = -1;
  KAR.saving = false;
  // 歌词尽力取：拿不到（本地文件多数没有）就纯伴奏唱，不阻塞开唱
  try {
    const d = await rpc('getlyric', {
      platform: item.platform, id: item.id, time: item.time, sign: item.sign,
    });
    const raw = (d && (d.lyric || d.lrc)) || (typeof d === 'string' ? d : '');
    const trans = (d && (d.tlyric || d.tLyric || d.trans)) || '';
    KAR.lines = parseLrc(raw, trans);
  } catch (e) { KAR.lines = []; }
  KAR.sung = KAR.lines.map(() => false);
  KAR.open = true;
  $('#singLayer').hidden = false;
  karPaintReady();
  karBindOnce();
}

function karPaintReady() {
  const it = KAR.item || {};
  $('#kTitle').textContent = it.name || '';
  $('#kArtist').textContent = it.artist || '本地伴奏';
  const bg = $('#kBg');
  if (it.cover) { bg.style.backgroundImage = `url("${String(it.cover).replace(/"/g, '')}")`; }
  else bg.style.backgroundImage = 'none';
  $('#kStage').innerHTML = `
    <div class="k-ready">
      <div class="k-ready-t">准备好开嗓</div>
      <div class="k-ready-s">${KAR.lines.length
        ? `已就位 ${KAR.lines.length} 行歌词 · 建议戴耳机（避免伴奏串进麦克风）`
        : '这首歌没找到带时间轴的歌词 · 纯伴奏模式'}</div>
      <button class="btn primary lg" id="kBegin" data-act="k-begin">${icon('mic')}开始演唱</button>
      <div class="k-hint">全程录音只保存在本机，唱完由你决定保存或丢弃</div>
    </div>`;
  karPaintCtl(true);
}

/** 控制条只绑一次（oninput/onclick 赋值，重复 open 不叠加） */
function karBindOnce() {
  const acc = $('#kAccVol'), mic = $('#kMicVol'), voc = $('#kVocal');
  acc.oninput = () => {
    KAR.accVol = Number(acc.value) / 100;
    if (KAR.acc) KAR.acc.gain.setTargetAtTime(KAR.accVol, KAR.ctx.currentTime, 0.03);
  };
  mic.oninput = () => {
    KAR.micVol = Number(mic.value) / 100;
    if (KAR.micGain) KAR.micGain.gain.setTargetAtTime(KAR.micVol, KAR.ctx.currentTime, 0.03);
  };
  voc.onchange = () => {
    KAR.vocalOff = voc.checked;
    karSetVocal(!KAR.vocalOff);
    toast(voc.checked ? '已切回原声（跟原唱一起唱）' : '已减弱原声（中置消人声，效果因歌而异）');
  };
}

function karSetVocal(cancelOn) {
  if (!KAR.ctx) return;
  const now = KAR.ctx.currentTime;
  try {
    KAR.cancel.gain.setTargetAtTime(cancelOn ? 1 : 0, now, 0.05);
    KAR.orig.gain.setTargetAtTime(cancelOn ? 0 : 1, now, 0.05);
  } catch (e) {
    KAR.cancel.gain.value = cancelOn ? 1 : 0;
    KAR.orig.gain.value = cancelOn ? 0 : 1;
  }
}

/** 音频图 + 录音抽头。全程 try 包死：K 歌失败不能把整个播放器拖下水 */
function karBuild(stream) {
  try {
    const CE = window.AudioContext || window.webkitAudioContext;
    if (!CE) return false;
    const ctx = new CE();
    const el = document.createElement('audio');
    el.id = 'kAudio';
    el.preload = 'auto';
    el.setAttribute('playsinline', '');
    document.body.appendChild(el);
    el.src = mediaSrc(KAR.item, playCandidates(KAR.item)[0]);

    const src = ctx.createMediaElementSource(el);
    const split = ctx.createChannelSplitter(2);
    const l = ctx.createGain(); l.gain.value = 0.5;
    const r = ctx.createGain(); r.gain.value = -0.5;
    const cancel = ctx.createGain(); cancel.gain.value = 1;
    const orig = ctx.createGain(); orig.gain.value = 0;
    const acc = ctx.createGain(); acc.gain.value = KAR.accVol;
    src.connect(split);
    split.connect(l, 0); split.connect(r, 1);
    l.connect(cancel); r.connect(cancel);
    src.connect(orig);
    cancel.connect(acc); orig.connect(acc);

    const bus = ctx.createGain(); bus.gain.value = 1;
    acc.connect(bus);

    const micSrc = ctx.createMediaStreamSource(stream);
    const micGain = ctx.createGain(); micGain.gain.value = KAR.micVol;
    micSrc.connect(micGain);
    // 「房间感」：120ms 反馈延迟 + 低通，当湿润层，默认全干（0）
    const wet = ctx.createGain(); wet.gain.value = 0;
    const dly = ctx.createDelay(0.5); dly.delayTime.value = 0.12;
    const fb = ctx.createGain(); fb.gain.value = 0.25;
    const damp = ctx.createBiquadFilter(); damp.type = 'lowpass'; damp.frequency.value = 3200;
    micGain.connect(dly); dly.connect(damp); damp.connect(fb); fb.connect(dly);
    damp.connect(wet);
    micGain.connect(bus); wet.connect(bus);

    const rec = ctx.createScriptProcessor(4096, 2, 2);
    const sink = ctx.createGain(); sink.gain.value = 0;    // SP 必须连到 destination 才会跑
    bus.connect(rec); rec.connect(sink); sink.connect(ctx.destination);
    bus.connect(ctx.destination);                          // 监听：伴奏 + 自己的声音

    // 麦克风电平抽头：走与录音同构的 ScriptProcessor，读到的就是真正进
    // 混音总线的那一路 —— 电平条与成品录音同源，不会出现「看着有电平、
    // 存下来却是静音」。人声是断续的，加一点峰值保持让条子不闪成 0。
    const micTap = ctx.createScriptProcessor(1024, 1, 1);
    const micSink = ctx.createGain(); micSink.gain.value = 0;
    micSrc.connect(micTap); micTap.connect(micSink); micSink.connect(ctx.destination);
    micTap.onaudioprocess = (e) => {
      const d = e.inputBuffer.getChannelData(0);
      let s = 0;
      for (let i = 0; i < d.length; i += 1) s += d[i] * d[i];
      const rms = Math.sqrt(s / d.length);
      KAR.micLevel = Math.max(rms, (KAR.micLevel || 0) * 0.82);
    };

    rec.onaudioprocess = (e) => {
      if (KAR.phase !== 'sing') return;      // 倒数 / 回顾不进录音
      KAR.chunks.push([new Float32Array(e.inputBuffer.getChannelData(0)),
                       new Float32Array(e.inputBuffer.getChannelData(1))]);
    };

    Object.assign(KAR, { ctx, el, cancel, orig, acc, micGain, wet,
      micLevel: 0, micStream: stream });
    el.addEventListener('ended', () => { if (KAR.phase === 'sing') karStop(); });
    return true;
  } catch (e) {
    return false;
  }
}

async function karaokeBegin() {
  if (!KAR.open || KAR.phase === 'sing' || KAR.phase === 'count') return;
  let stream;
  try {
    stream = await KAR_MON()({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
    });
  } catch (e) {
    toast('拿不到麦克风：' + (e && (e.name || e.message) || '权限被拒绝'), 'err');
    return;
  }
  if (!karBuild(stream)) {
    toast('K 歌音频初始化失败', 'err');
    stream.getTracks().forEach((t) => t.stop());
    return;
  }
  karSetVocal(!KAR.vocalOff);
  KAR.phase = 'count';
  $('#kStage').innerHTML = '<div class="k-count" id="kCount">3</div>';
  karPaintCtl(false);
  try { await KAR.ctx.resume(); } catch (e) { /* ignore */ }
  for (const n of [3, 2, 1]) {
    const c = $('#kCount');
    if (!c) return;                        // 期间关了层
    c.textContent = n;
    await new Promise((res) => setTimeout(res, 700));
  }
  karGo();
}

function karGo() {
  if (!KAR.open || !KAR.el) return;
  KAR.phase = 'sing';
  KAR.t0 = Date.now();
  KAR.chunks = [];
  const p = KAR.el.play();
  if (p && p.catch) p.catch(() => { toast('伴奏没能播出来', 'err'); });
  karPaintCtl(false, true);
  cancelAnimationFrame(KAR.raf);
  KAR.raf = requestAnimationFrame(karLoop);
}

function karLoop() {
  if (!KAR.open) return;
  KAR.raf = requestAnimationFrame(karLoop);
  if (KAR.phase !== 'sing' || !KAR.el) return;
  const el = KAR.el;
  const pos = el.currentTime || 0;
  const dur = (isFinite(el.duration) && el.duration) || KAR.item.duration || 0;
  $('#kRecTime').textContent = fmtTime(pos) + (dur ? ' / ' + fmtTime(dur) : '');
  if (dur) $('#kProgFill').style.width = Math.min(100, (pos / dur) * 100).toFixed(2) + '%';

  // 电平条（麦克风）：与录音同构的 SP 抽头读到的 RMS
  const rms = KAR.micLevel || 0;
  const lvl = $('#kMicLvl');
  if (lvl) lvl.style.width = Math.min(100, rms * 220).toFixed(1) + '%';

  // 行进 + 跟唱判定
  const t = pos + LRC_OFF;
  const idx = KAR.lines.length ? karLineAt(t) : -1;
  if (idx !== KAR.lastLine) {
    KAR.lastLine = idx;
    karPaintLines(idx);
  }
  if (idx >= 0 && rms > KAR_SING_THR) KAR.sung[idx] = true;
}

function karPaintLines(idx) {
  const box = $('#kLines');
  if (!box) return;
  const ls = KAR.lines;
  if (!ls.length) { box.innerHTML = '<div class="k-nolrc">纯伴奏模式 · 尽情唱</div>'; return; }
  const row = (l, cls) => `<div class="k-line ${cls}">${esc((l && l.text) || '')}`
    + ((l && l.trans) ? `<span class="k-trans">${esc(l.trans)}</span>` : '') + '</div>';
  box.innerHTML = (ls[idx - 1] ? row(ls[idx - 1], 'dim') : '<div class="k-line dim"></div>')
    + (ls[idx] ? row(ls[idx], 'now') : '<div class="k-line now">&nbsp;</div>')
    + (ls[idx + 1] ? row(ls[idx + 1], 'dim') : '<div class="k-line dim"></div>');
}

function karStop() {
  if (KAR.phase !== 'sing') return;
  KAR.phase = 'review';
  try { KAR.el.pause(); } catch (e) { /* ignore */ }
  const sung = KAR.sung.filter(Boolean).length;
  const total = KAR.lines.filter((l, i, a) => !i || a[i].t - a[i - 1].t > 0.5).length || KAR.lines.length;
  const secs = Math.round((Date.now() - KAR.t0) / 1000);
  KAR.duration = secs;                 // 保存时用录制时长，不含回顾页停留
  KAR.blob = karWav(KAR.chunks, KAR.ctx ? KAR.ctx.sampleRate : 48000);
  KAR.chunks = [];
  $('#kStage').innerHTML = `
    <div class="k-review">
      <div class="k-score">${KAR.lines.length
        ? `跟唱 <b>${sung}</b> / ${total} 行` : '录音完成'}</div>
      <div class="k-ready-s">时长 ${fmtTime(secs)} · ${Math.round((KAR.blob.size || 0) / 1048576 * 10) / 10} MB · 只存在本机</div>
      <div class="k-review-acts">
        <button class="btn primary" data-act="k-save" ${KAR.blob ? '' : 'disabled'}>${icon('check')}保存作品</button>
        <button class="btn ghost" data-act="k-discard">${icon('trash')}丢弃</button>
      </div>
    </div>`;
  karPaintCtl(false);
}

/** 两声道交错 → 16bit PCM WAV（44 字节头） */
function karWav(chunks, rate) {
  const frames = chunks.reduce((n, c) => n + c[0].length, 0);
  if (!frames) return null;
  const buf = new ArrayBuffer(44 + frames * 4);
  const v = new DataView(buf);
  const ws = (o, s) => { for (let i = 0; i < s.length; i += 1) v.setUint8(o + i, s.charCodeAt(i)); };
  ws(0, 'RIFF'); v.setUint32(4, 36 + frames * 4, true); ws(8, 'WAVE'); ws(12, 'fmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 2, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 4, true);
  v.setUint16(32, 4, true); v.setUint16(34, 16, true);
  ws(36, 'data'); v.setUint32(40, frames * 4, true);
  let o = 44;
  for (const c of chunks) {
    const n = Math.min(c[0].length, c[1].length);
    for (let i = 0; i < n; i += 1) {
      const Lv = Math.max(-1, Math.min(1, c[0][i]));
      const Rv = Math.max(-1, Math.min(1, c[1][i]));
      v.setInt16(o, Lv < 0 ? Lv * 32768 : Lv * 32767, true); o += 2;
      v.setInt16(o, Rv < 0 ? Rv * 32768 : Rv * 32767, true); o += 2;
    }
  }
  return new Blob([buf], { type: 'audio/wav' });
}

async function karSave() {
  if (KAR.saving || !KAR.blob) return;
  KAR.saving = true;
  const it = KAR.item || {};
  const q = new URLSearchParams({
    name: it.name || '未命名作品', artist: it.artist || '',
    platform: it.platform || '', sid: String(it.id || ''),
    cover: it.cover || '', duration: String(KAR.duration || 0),
  });
  // 录音是二进制：post() 固定 JSON.stringify，会把 Blob 变成 "{}"，
  // 后端按 Content-Length 只读到 2 字节就拒了。这里发 raw octet-stream。
  try {
    const r = await fetch('/api/works/upload?' + q.toString(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: KAR.blob,
    });
    const d = await r.json().catch(() => ({}));
    if (d && d.ok && d.data) {
      state.works = [d.data].concat(state.works || []);
      state.worksLoaded = true;
      renderSide();
      toast('作品已保存，去「我的作品」听听');
      karClose();
    } else {
      toast((d && d.error) || '保存失败', 'err');
      KAR.saving = false;
    }
  } catch (e) {
    toast('保存失败：' + (e.message || e), 'err');
    KAR.saving = false;
  }
}

function karClose() {
  cancelAnimationFrame(KAR.raf);
  if (KAR.micStream) KAR.micStream.getTracks().forEach((t) => t.stop());
  if (KAR.el) {
    try { KAR.el.pause(); KAR.el.removeAttribute('src'); KAR.el.load(); } catch (e) { /* ignore */ }
    KAR.el.remove();
  }
  if (KAR.ctx) { try { KAR.ctx.close(); } catch (e) { /* ignore */ } }
  Object.assign(KAR, { open: false, phase: '', ctx: null, el: null, cancel: null,
    orig: null, acc: null, micGain: null, wet: null, micLevel: 0,
    micStream: null, chunks: [], blob: null, duration: 0, lastLine: -1 });
  $('#singLayer').hidden = true;
  // 还位：播放器回到进入前的位置（保持暂停，用户自己决定续不续）
  if (KAR.resume) {
    const a = audio();
    if (a.src) {
      try { a.currentTime = KAR.resume.position; } catch (e) { /* ignore */ }
    }
    KAR.resume = null;
  }
  $('#playBtn').innerHTML = icon('play');
}

/** 控制条的显隐与按钮文案（ready/count/sing/review 各态不同） */
function karPaintCtl(ready, singing) {
  const layer = $('#singLayer');
  if (layer) layer.classList.toggle('sing', !!singing);
  const fin = $('#kFinish');
  if (fin) fin.hidden = !singing;
  const back = $('#kBack');
  if (back) back.hidden = !!singing;
}

/* ------------------------------------------------------------------ 我的作品 */
async function loadWorks(force) {
  if (state.worksLoaded && !force) return;
  state.worksLoading = true;
  if (state.route.name === 'works') render();
  try {
    const d = await fetch('/api/works').then((r) => r.json());
    state.works = (d && d.data) || [];
    state.worksLoaded = true;
  } catch (e) {
    state.worksError = e.message;
  }
  state.worksLoading = false;
  if (state.route.name === 'works') render();
}

function viewWorks() {
  if (!state.worksLoaded && !state.worksLoading) loadWorks();
  const works = state.works || [];
  const head = `<div class="vhead vhead-row">
    <div>
      <div class="kicker">MY COVERS</div>
      <h1>我的作品</h1>
      <p class="sub">${works.length
        ? `<span class="mono">${works.length}</span> 个翻唱录音 · 只保存在本机`
        : '在这里收录你自己唱的歌'}</p>
    </div>
    <div class="vhead-acts">
      ${works.length ? `<button class="btn primary" data-act="w-playall">${icon('play')}播放全部</button>` : ''}
    </div>
  </div>`;
  let body;
  if (state.worksLoading && !works.length) body = emptyHtml('mic', '正在读取…', '从本机取作品列表');
  else if (!works.length) {
    body = emptyHtml('mic', '还没有作品',
      '播放任意一首歌 → 正在播放面板 → 「去K歌」，唱完保存就会出现在这里');
  } else {
    body = `<div class="cards">${works.map((w) => `
      <div class="card wcard">
        <div class="card-art">${w.cover
          ? `<img src="${esc(w.cover)}" alt="" onerror="this.remove()">` : phImg('song')}
          <button class="card-play" data-act="w-play" data-id="${esc(w.id)}" title="播放">${icon('play')}</button>
        </div>
        <div class="card-name ell">${esc(w.name || '未命名作品')}</div>
        <div class="card-sub ell">${esc(w.artist || '原唱未知')}</div>
        <div class="wcard-foot">
          <span class="mono">${fmtTime(w.duration || 0)} · ${new Date((w.created || 0) * 1000)
            .toLocaleDateString()}</span>
          <span class="wcard-acts">
            <a class="ic" href="/api/works/file?id=${esc(w.id)}" download="${esc((w.name || '作品') + '.wav')}"
               title="导出 WAV">${icon('dl')}</a>
            <button class="ic" data-act="w-del" data-id="${esc(w.id)}" title="删除">${icon('trash')}</button>
          </span>
        </div>
      </div>`).join('')}</div>`;
  }
  return `<div class="page">${head}${body}</div>`;
}

async function workDelete(id) {
  const w = (state.works || []).find((x) => x.id === id);
  if (!w) return;
  // 破坏性操作，先问一句（与「清空收藏」同级）
  if (!confirm(`删除作品「${w.name || '未命名'}」？\n\n录音文件会一并从磁盘删掉，不可恢复。`)) return;
  try {
    await post('/api/works/delete', { id });
    state.works = (state.works || []).filter((x) => x.id !== id);
    renderSide();
    render();
    toast('已删除');
  } catch (e) {
    toast('删除失败：' + (e.message || e), 'err');
  }
}

/** 作品 meta → 可播放条目（playCandidates/mediaSrc 对 platform 'work' 有短路） */
function workAsTrack(w) {
  return {
    platform: 'work', id: w.id, name: w.name || '未命名作品',
    artist: w.artist || '我的作品', cover: w.cover || '', duration: w.duration || 0,
  };
}

async function boot() {
  bind();
  await refreshStore();
  renderSide();
  // 首屏就把推荐置成 loading：不然会先摆一屏「还听不出你的口味」的空态，
  // 数据回来再换成卡片 —— 那是两次重画，用户看到的是闪一下加一次跳变。
  if (state.settings.personalized !== false) state.recoLoading = true;
  render();
  renderQueue();
  refreshTasks();
  restoreLast();
  loadRecommend().then(() => paintReco());   // 首页推荐：异步，不挡首屏
  loadStats().then(() => paintStats());      // 首页统计：异步，不挡首屏
  loadUpdateStatus();                        // 更新追踪：读后端缓存 + 每 30 分钟轮询一次
  setInterval(() => loadUpdateStatus(), 30 * 60 * 1000);
  pollUpdateDl();                            // 若有进行中的版本下载（如刷新页面），恢复进度条
  $('#kw').focus();
}

boot();
