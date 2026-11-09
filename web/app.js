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
  const all = (items || []).filter((it) => it && it.id && it.platform);
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
  return qualityRank(item);
}

const songKey = (it) => `${it.platform}_${it.id}`;

/* 歌单条目落盘时只存了展示字段（无 minfo/time/sign），直接播/下会报
   「没有可用音质」。回源取一次完整条目补全，再写回原数组，下次不必重取。 */
async function ensureMinfo(item) {
  if (!item || !item.platform || !item.id) return item;
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

/* ------------------------------------------------------------------ 播放 */
const audio = () => $('#audio');

function setBuffering(on) {
  const el = $('#buffering');
  if (el) el.hidden = !on;
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
}

function paintSpec() {
  const q = state.currentQuality;
  const el = $('#plSpec');
  if (!q) { el.textContent = '—'; return; }
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
    a.src = mediaSrc(item, q);        // 同步设 src，保住用户手势
    a.dataset.media = a.src;
    a.volume = (Number(state.settings.volume ?? 80)) / 100;
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
  artist: '歌手页', album: '专辑', taste: '口味画像',
};

async function playAt(list, index, opts = {}) {
  let item = list[index];
  if (!item) return;
  item = await ensureMinfo(item);       // 歌单条目缺 minfo：回源补全再播
  const cands = playCandidates(item);
  if (!cands.length) { toast('这首歌没有可用音质', 'err'); return; }
  state.resumePos = 0;             // 换歌/重播都用新的位置，旧的待续位置作废
  maybeCountOnLeave();
  state.playList = list.slice();
  state.playIndex = index;
  state.playing = item;
  state.queueSource = opts.source || (
    list === (state.reco && state.reco.list) ? '为你推荐'
      : list === state.recent ? '最近播放'
        : (QUEUE_SRC[state.route.name] || '播放列表'));
  paintPlayer(item);
  renderPlaying();
  renderQueue();
  renderNp();
  await startTrack(item, cands, 0, opts.position || 0);
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
      <button class="ic" data-act="dl" data-i="${i}" title="下载">${icon('dl')}</button>
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
    <div class="np-foot"><button class="btn ghost sm" data-act="lyric-full">放大歌词</button></div>`;
  loadNpLyric(it);
}

/* ------------------------------------------------------------------ 歌词 · SP 式逐行 */
let lyricLines = [];      // [{t, text}] 按时间升序；空 = 无时间标签，降级纯文本
let lyricIdx = -1;
let lyricFollow = true;   // 是否跟随当前行滚动（用户手动滚动后置 false）
let lyricScrolling = false;// 正在做程序滚动，期间的 wheel 事件不算「用户滚动」
let lyricPending = false;  // 动画期间目标行变了，动画结束后补一次滚动

function parseLrc(raw) {
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

function lyricIndexAt(sec) {
  let lo = 0, hi = lyricLines.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (lyricLines[mid].t <= sec + 0.25) { ans = mid; lo = mid + 1; } else hi = mid - 1;
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
    `<button class="lyric-line${big ? ' big' : ''}" data-act="lyric-seek" data-i="${i}" data-t="${l.t}">${esc(l.text)}</button>`
  ).join('');
  updateLyricTick();
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
    currentLyric = raw;
    if (!state.playing || pk(state.playing) !== pk(it)) return;
    lyricLines = parseLrc(raw);
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

  <div class="set-sec">
    <h4>数据</h4>
    <div class="set-actions">
      <button class="btn sm" data-act="data-clear" data-what="recent_clear">清除播放记录</button>
      <button class="btn sm" data-act="data-clear" data-what="history_clear">清除搜索历史</button>
      <button class="btn sm" data-act="data-clear" data-what="downloads_clear">清除下载记录</button>
      <button class="btn sm" data-act="data-clear" data-what="favorites_clear">清空收藏</button>
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
    audio().volume = v / 100;
    $('#vol').value = v;
    state.settings.volume = v;
  });
  on('#setVol', 'change', (e) => save('volume', Number(e.target.value)));
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
    lyricLines = parseLrc(currentLyric);
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
    audio().volume = Number(vol.value) / 100;
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

  /* 媒体事件 */
  const a = audio();
  a.addEventListener('play', () => { $('#playBtn').innerHTML = icon('pause'); renderPlaying(); });
  a.addEventListener('pause', () => { $('#playBtn').innerHTML = icon('play'); saveLastTrack(true); });
  a.addEventListener('timeupdate', () => {
    paintSeek();
    updateLyricTick();
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
    }
  });
  a.addEventListener('ended', () => {
    reportPlay(true);
    if (state.mode === 'one') {
      a.currentTime = 0;
      a.play().catch(() => {});
      return;
    }
    playNext(1);
  });
  a.addEventListener('error', () => {
    if (a.src && !a.dataset.errShown) {
      a.dataset.errShown = '1';
      setBuffering(false);
      setTimeout(() => { delete a.dataset.errShown; }, 3000);
    }
  });
  a.addEventListener('seeking', () => setBuffering(false));
  a.addEventListener('waiting', () => setBuffering(true));
  a.addEventListener('playing', () => setBuffering(false));
  a.addEventListener('loadedmetadata', () => { setBuffering(false); paintSeek(); });

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
  audio().volume = v / 100;
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
    audio().volume = (Number(state.settings.volume ?? 80)) / 100;
    const m = MODES.find((x) => x.key === state.mode) || MODES[0];
    $('#modeBtn').innerHTML = icon(m.icon);
    $('#modeBtn').title = m.label;
    $('#modeBtn').classList.toggle('on', state.mode !== 'order');
  } catch (e) {
    toast('读取本地数据失败：' + e.message, 'err');
  }
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
  $('#kw').focus();
}

boot();
