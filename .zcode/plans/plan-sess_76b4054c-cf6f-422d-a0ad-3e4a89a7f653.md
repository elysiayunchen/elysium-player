# K 歌翻唱（P8 / v1.9.0）实施计划

## 背景与结论

用户想加「全民K歌」式功能：任选曲库里的歌开口唱、保存作品。**结论：可行**。Elysium 的底子恰好凑齐三大件：

- 任意曲目同源可播：`mediaSrc()`（web/app.js:396）本地走 `/api/local/file`、在线走 `/api/media` 代理，**都在同源**，K 歌专用音频元素直接用
- 歌词现成：`parseLrc()` 双语解析（app.js:3528）+ `lrc_offset_ms` 校准（app.js:3566）+ 歌词拉取链路（app.js:4574-4600）
- Web Audio 图有先例：引擎 2.0 的 `ENG` 模块（app.js:1015-1330）证明了 MediaElementSource 接管、gain ramp 防爆音、降级重建的完整套路

分四步走：**Step 0 收尾同步 → K0 麦克风 spike → K1 桌面 MVP → K2 Android**。

两个关键风险已定位：
- 桌面是 pywebview/WebView2，其 `getUserMedia` 是否放行未知（WebView2 默认可能拒绝未处理的 PermissionRequested）→ K0 验证；备好降级：检测不到麦克风时给「在系统浏览器中打开 K 歌」（同一个 localhost 页面，Chrome 里权限正常）
- Android 壳（MainActivity.java，186 行）**没有 WebChromeClient**，getUserMedia 必被拒 → K2 补 `onPermissionRequest` + RECORD_AUDIO 运行时权限（仿现有 `ensureNotifPermission` 样式）

## Step 0：P5 同步收尾（工作区有完整但未提交的实现）

sync_engine.py（603 行，函数齐全）+ main.py 端点 + 前端 syncDrawer 都在但未提交。先跑 `python verify_sync.py`；通过 → 单独提交 `feat(sync)`，让 K 歌的 diff 独立干净；不过 → 修复后再提交（K 歌开分支不受阻塞）。

## K0 spike：麦克风链路可行性（仿 P4.0，半小时级）

用 Chrome 的 `--use-fake-ui-for-media-stream --use-fake-device-for-media-stream`（假麦克风发正弦波）自动化验证：getUserMedia → AnalyserNode 有电平 → ScriptProcessor 录 3s → 拼 WAV 回放有声。WebView2 手动验一次。结论写进 ROADMAP（照 P4.0 格式）。

## K1 MVP（桌面，本次交付核心）

### 后端 main.py（约 +150 行）

- 作品目录：`data_dir()/works/`（与 data.json 同级，不进用户音乐目录）
- 端点（照 `/api/sync/*` 的分组注释风格）：
  - `GET /api/works` — 列表（data.json 的 `works` 集合）
  - `POST /api/works/upload` — raw body 是 WAV 字节，meta 走 query（name/artist/platform/sid/cover/duration）；分块读 `rfile` 写盘；大小上限 200MB；id 服务端生成（`[a-z0-9-]` 白名单校验，防路径穿越，复用 local file 的思路）
  - `GET /api/works/file?id=` — 服 WAV，复用 `_serve_cached`（main.py:3298）的 Range 逻辑；`AUDIO_MIME`（main.py:1716）补 `wav`
  - `POST /api/works/delete` — 删文件 + 删 meta
- `Store.apply`（main.py:222）加 `work_add` / `work_remove` 两个 op（照 `download_add` 样式），works 结构：`{id, key, name, artist, platform, cover, created, duration, file, size}`
- **口径**：works 不进 WebDAV 同步包（本地文件跨机无意义，与「本地曲目不同步」同口径）；export 不带 works 文件

### 前端 web/（约 +700 行：app.js +500 / style.css +120 / index.html +30）

**KARAOKE 模块**（独立于 ENG，不动引擎那几百行）：
- 专用 `<audio>` 元素（`engNewEl` 风格动态建）+ 专用 AudioContext（用完 `close()`，避免撑爆 Chrome 的 6 ctx 上限）
- 音频图：`kSrc →〔消人声开关：ChannelSplitter → L(+0.5)+R(−0.5) → merger，或直连〕→ kAcc → kBus`；`mic → kMicGain →（轻混响 dry/wet）→ kBus`；`kBus → destination`（监听）+ `kBus → ScriptProcessor`（录混合 bus，Int16 分块）
- 消人声默认开、可关（跟原唱一起唱）；切换用 gain ramp 防爆音（借鉴 `engApplyEq` 的 setTargetAtTime）。UI 用词诚实：「减弱原声（消中置人声）」，效果因歌而异
- 流程：进 K 歌 → 若在播则暂停并记位置（退出恢复）→ 3-2-1 倒数 → 播伴奏 + 录混合输出 → 曲终/手动停 → 拼 WAV 上传 → 「已保存，去作品库」或丢弃
- K 歌期间不写 mediaSession（SMTC 不乱跳）；不计数 plays
- **完成度点亮（不是打分）**：每行歌词时间窗内 mic RMS 超阈值记「唱了」，结束显示「跟唱 42/58 行」。无旋律参考就不做音准打分，不做假打分

**K 歌全屏层**（`singLayer`，照 lyricLayer 的层叠样式）：模糊封面背景、当前行大字 + 上下行小字居中滚动（parseLrc + offset）、REC 红点计时、伴奏/人声音量条、消人声开关、完成度徽标。

**入口**：正在播放抽屉（npDrawer）加「去K歌」；歌曲行 ctxmenu（app.js:3806 一带）加「K歌」。无麦克风（feature-detect 失败）时入口点了引导「在系统浏览器打开」。

**我的作品页**：侧栏 snav（index.html:76-82）加 `works` 项（`i-mic` 图标现成）+ renderSide 计数；卡片列表（封面/歌名/原唱/日期/时长）+ 播放/删除/导出（`a[download]`）。播放走正常队列与 SMTC：work 造 `{platform:'work', id, name, artist, cover}` 伪条目，`mediaSrc` 加分支、`ensureMinfo`/`playCandidates`（app.js:354/362）对 platform 'work' 短路。

## K2 Android（第二批，独立验收）

- Manifest 加 `RECORD_AUDIO`；MainActivity 加 WebChromeClient：`onPermissionRequest` 收到 audio 请求 → 自检/申请运行时权限（照 `ensureNotifPermission`，MainActivity.java:98-110 样式）→ grant；拒绝只影响 K 歌不拦播放
- 真机验证；APK 构建照既有流程（box 构建、固定签名）

## 验收

新增 `verify_karaoke.py`（仿 verify_gapless.py：selenium + Chrome + 假麦克风 flags）：
- W0-W3 后端：空列表引导态 / 上传→落盘+列表 / `/api/works/file` Range 206 / 删除后文件消失
- W4-W6 K 歌链路：进 K 歌监听有输出、消人声切换不断流（峰值连续）、假麦克风录 3s → WAV 头合法且时长≈3s 非全零、完成度 >0 行
- W7-W9 集成：作品能进队列正常播放、data.json 只增 works meta、退出 K 歌恢复正常播放位置
- 回归：verify_local 17/17、verify_gapless 16/16（引擎没动，必须原样过）

## 发布与文档

- 分支 `feat/karaoke`（照 P4 惯例独立分支），版本 v1.9.0；`plans/ROADMAP.md` 补 P8 立项（含「不做音准打分、works 不同步」的边界声明）
- 发版照既有流程，本计划不动签名/资产校验

## 改动文件清单

main.py、web/app.js、web/index.html、web/style.css、verify_karaoke.py（新）、plans/ROADMAP.md；K2 再动 android/app/src/main/AndroidManifest.xml 与 MainActivity.java。