# K 歌原生直通耳返（Android）设计与实现清单（2026-11-14）

> 起因：beta7 真机 `diagMicRtt` 实测耳返 RTT **179ms**（178–179，48k 无重采样，
> base 5ms / output 64ms，采集回调 p50 5ms）。JS 图全程 sample 级直通、零缓冲，
> 瓶颈在 WebView 平台栈：约 110ms 采集隐藏缓冲 + 64ms 输出缓冲。
> 按预定规则（>150ms 开原生直通）立项。
>
> 原则：**只动 Android K 歌耳返**——录音、混响、音准、伴奏、桌面链路一律不动；
> **不给假情报**——数字以真机自检为准；**失败自动回退**——双采集抢麦/启动失败
> 一律退回 WebView 耳返，不把 K 歌搞哑；**不引 NDK、不加 FGS**——纯 Java，
> 仅前台 Activity 期间运行。

## 研究：全民K歌是怎么做的（公开资料）

公开资料没有源码级实现，能确认的架构事实：

- 全民K歌是**原生 App**：麦克风采集、伴奏解码播放、混音、耳返都在同一个原生
  音频引擎/同一条输出管线里，不走 WebView/WebAudio。
- **分层选路**：按机型探测能力，优先厂商私有低延迟通路，其次 AAudio/OpenSL，
  最后 AudioFlinger 软件混音；公开拆解给的量级是 AAudio 10–30ms、
  AudioFlinger 150–300ms（[CSDN 拆解](https://blog.csdn.net/weixin_29294597/article/details/159450140)）。
- 宣传的 **<40ms 是联合定制**：vivo TWS 3e 33ms、华为 FreeBuds <40ms
  （[中华网稿](http://hea.china.com/article/20240102/012024_1465925.html)）；
  普通蓝牙 A2DP 仍是 100ms+。这不是通用 Android 装个 App 就能复制的数字。
- **关键差异**：它的伴奏也在原生，人声与伴奏同一时钟/同一输出管线，不存在
  「人声走原生、伴奏走 WebView」的跨栈问题。Elysium 的伴奏在 WebView
  （output 64ms），要完全对齐 33ms 得把伴奏与混音也搬原生——另一个大工程，
  不在本期。

**结论**：本期只把「干声耳返」搬原生；伴奏/湿声/录音继续 WebAudio。这是
在现有架构下收益最大、回归面最小的切法。

## 时序推导：为什么**不做**人声补偿

设：伴奏输出延迟 `Lb`（WebView 64ms），人声耳返延迟 `Lv`（原生约 30–60ms）。

歌手听到拍点的时间是 `t拍 + Lb`，他跟着听到的拍子唱；自己的声音在
`t拍 + Lb + Lv` 被听到。此刻伴奏播到的位置是 `t拍 + Lv`——与 `Lb` 无关。
也就是说，**伴奏的输出延迟对「人声 vs 伴奏」是共模的**（歌手跟着听到的拍子唱，
伴奏的 `Lb` 在参考里已经含了一次），真正决定偏移的是人声耳返整条链 `Lv`。
WebView 下 `Lv = 179ms`：其中采集缓冲约 110ms 是主项，输出 64ms 也在里面；
原生把采集压到 5–10ms、输出压到约 20ms，`Lv` 就到 30–60ms 量级。

原生把采集缓冲从约 110ms 压到 5–10ms 后：

- 绝对耳返（自己声音的往返）从 179ms → 约 30–60ms（采集 + 原生输出 + 调度）；
- 相对偏移（人声与伴奏的错位）也等于这条人声耳返链，从 179ms → 约 30–60ms。

给「人声」加补偿只会把 `Lv` 做大、偏移变大；给「整轨」加延迟只是把整首歌
往后推，偏移不变。两条都不做——原生输出尽量快。

## 关键决策

| ID | 决策 | 理由 |
| --- | --- | --- |
| D-1 | **双采集**：WebView `getUserMedia` 继续供录音/混响/音准，原生另开一路 `AudioRecord` 只做干声耳返 | 录音链路零改动；单采集需把原生 PCM 回灌 WebAudio，桥接吞吐/时钟/录音对齐全要重做 |
| D-2 | **Oboe 1.9.3 双流**（beta9）：输入/输出各一条 `LowLatency/Shared` 流 + SPSC 环；**原纯 Java 方案保留为第二级回退** | beta8 真机 minBuffer 200/128ms，纯 Java 压不下去（自检 217.7ms、接耳机仍有延迟），按预案上 Oboe；目标 30–60ms |
| D-3 | **不做人声补偿、不延迟整轨** | 见上节时序推导；加了只会放大偏移 |
| D-4 | **默认开**（Android + 桥可用），调音台可关；失败/抢麦自动回退 | 用户装包即见效；有开关、有回退 |
| D-5 | **无 FGS、无新权限**；仅前台 Activity 期间运行，`onStop` 停 | targetSdk 34 麦克风 FGS 限制多；K 歌本来就前台；`RECORD_AUDIO` 已有 |
| D-6 | 原生**只做干声**；混响/录音/音准/伴奏一律不动 | 保持录音与效果链路零回归 |

## A. 原生引擎（新增 `android/.../KaraokeMonitor.java`）

| ID | 项 | 契约 |
| --- | --- | --- |
| A-1 | 采集源 | `UNPROCESSED`（`AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED` 为 true 时）→ `VOICE_RECOGNITION` → `MIC`；48k mono PCM16，初始化失败回退 44.1k；`status` 回报实际 source/rate |
| A-2 | 缓冲/线程 | chunk 256 帧（5.3ms@48k）；record/track buffer = `max(minBuffer, 4×chunkBytes)`；AudioTrack API26+ 用 `PERFORMANCE_MODE_LOW_LATENCY`，`USAGE_MEDIA/CONTENT_TYPE_MUSIC`；专用线程 `THREAD_PRIORITY_URGENT_AUDIO`；循环内零分配 |
| A-3 | DSP | HPF 80Hz（2 阶 RBJ）→ gain（5.3ms 块内 ramp）→ peaking 2.5k Q0.9（toneDb −6..+6）→ 软限幅（与 web `karLimCurve` 同曲线 knee .7 / top .97，2048 点查表）；mute 也走 ramp，防咔哒 |
| A-4 | 统计 | `inRms/outRms/underruns/framesRead/framesWritten/route/source/buffers/lastError`；`status()` 返回 JSON；route 1s 刷新（`AudioManager.getDevices`），供 JS 看门狗与读数 |
| A-5 | 生命周期 | `start/update/stop/status` 同步；start 失败返回 `{ok:false,error}`；stop join ≤500ms 并释放；**不申请音频焦点、不建 MediaSession**（沿用 PlaybackService 的既有约定） |
| A-6 | 原生自检 | `startDiag` 后台约 9s：8×1kHz/60ms 脉冲经 AudioTrack 出、AudioRecord 收，onset 匹配算 RTT；`status` 带 diag 进度；用于真机验收对比 179ms |

JSON 契约：

```jsonc
// startKaraokeMonitor 请求
{"gain":1.0,"toneDb":0,"muted":false}
// 成功
{"ok":true,"source":"unprocessed","sampleRate":48000,"chunkFrames":256,
 "recordBufFrames":1024,"trackBufFrames":1024,"recordBufMs":21.3,"trackBufMs":21.3,
 "performanceMode":"low_latency","route":"wired_headset"}
// 失败
{"ok":false,"error":"mic_busy","detail":"AudioRecord init failed"}

// updateKaraokeMonitor 请求 / 响应
{"gain":1.0,"toneDb":2,"muted":false} -> {"ok":true}

// karaokeMonitorStatus
{"ok":true,"running":true,"inRms":0.012,"outRms":0.011,"underruns":0,
 "framesRead":123456,"framesWritten":123456,"route":"wired_headset",
 "source":"unprocessed","sampleRate":48000,"recordBufFrames":1024,
 "trackBufFrames":1024,"lastError":""}

// startKaraokeMonitorDiag -> {"ok":true,"running":true}
// status 内 diag: {running,done,hits:[ms...],rttMs,minMs,maxMs,route,noise}
```

## B. 原生桥（`MainActivity.java`）

| ID | 项 | 做法 |
| --- | --- | --- |
| B-1 | 新桥方法 | `startKaraokeMonitor(String):String`、`updateKaraokeMonitor(String):String`、`karaokeMonitorStatus():String`、`stopKaraokeMonitor():void`、`startKaraokeMonitorDiag(String):String` |
| B-2 | 生命周期 | `onStop`/`onDestroy` 停引擎；页面 `visibilitychange` 也停/重启；不新增 Service/Intent |
| B-3 | 既有改动 | MainActivity 的 edge-to-edge / onBackPressed 已在 535bb7e 落库；本桥只在其后追加，不覆盖 |

## C. Web 接入（`web/app.js` / `index.html` / `style.css`）

| ID | 项 | 做法 |
| --- | --- | --- |
| C-1 | 能力探测 | `IS_ANDROID && window.ElysiumAndroid?.startKaraokeMonitor`；设置键 `karaoke_native_mon` 默认 true |
| C-2 | 启动 | `karBuild(stream)` 成功后、倒数前；config `{gain,toneDb,muted}`；ok → `KAR.native.on=true`、`echoGate` ramp→0、监听分支 −3dB headroom；失败保留 WebView 耳返并提示 |
| C-3 | 参数镜像 | micVol→`update.gain`；tone→`update.toneDb`；耳返开关→`update.muted`；web 原链路照旧更新（录音/混响不受影响） |
| C-4 | 看门狗 | `karLoop` 每 1s 轮询 status：原生 `inRms≈0` 且 web `micLevel>0.02` 连续 3 次→原生被占用回退；web `micLevel≈0` 且原生 `inRms>0.01` 连续 3 次→原生抢麦回退；`running=false/error`→回退；回退=stop + `echoGate` 恢复 + toast，本场不重试 |
| C-5 | 生命周期 | `karStop` 停原生（回顾页不再监听）、`karClose` 停；`visibilitychange` hidden 停、visible 且 count/sing 重启；`beforeunload` 停 |
| C-6 | UI | 调音台耳返行下新增「原生直通」开关 + 读数（采集/输出缓冲 ms、回退原因、蓝牙提示）；复用 `.km-echo/.km-val/.mono`，≤480 两行布局 |
| C-7 | 返回键 | `window.__elysiumBack` 已直接读 `KAR.open`（工作区 beta7 WIP 已修，不再依赖 `window.KAR`）；N9 覆盖「返回键关 K 歌并停原生」 |
| C-8 | 诊断面板 | 新增「原生耳返自检」按钮（Android+桥），轮询 diag 结果，显示 RTT/hits/route，与 web `diagMicRtt` 并列对比 |

## D. 设置/后端

| ID | 键 | 契约 | 校验 |
| --- | --- | --- | --- |
| D-1 | `karaoke_native_mon` | bool，默认 true；仅 Android 且桥可用时显示控件 | 非 bool 按 true 处理；未知键走既有 `_json_safe` 透传，无需新白名单 |
| D-2 | 落盘/同步 | 随 settings 走 `/api/store` 与 WebDAV 快照 | `verify_karaoke` 增 round-trip 脏数据例 |

## E. 诊断与回退

| ID | 项 | 做法 |
| --- | --- | --- |
| E-1 | 读数 | `原生直通 · 48k · 采集 21ms · 输出 21ms`；回退后 `原生直通不可用 · 已回退 WebView 耳返`；route=bluetooth 时追加「蓝牙延迟不可控，建议有线耳机」 |
| E-2 | 失败原因 | start 返回 `{ok:false,error,detail}` 原样进 toast/读数；本场记 `failed`，不再自动重试 |
| E-3 | 抢麦判定 | C-4 的双向 RMS 看门狗；误报由「连续 3 次 + 对向阈值」压住 |
| E-4 | 原生自检 | 设置→网络自检→「原生耳返自检」；外放、拔耳机、安静环境；9s 出 RTT 中位/区间/hits/route |
| E-5 | 外放/耳返 | 沿用现有「外放小心啸叫」提示；原生软限幅 + 监听分支 −3dB；不硬禁外放 |

## F. 验收

```bash
C:/Users/19645/miniconda3/python.exe verify_karaoke.py   # 增 B13 + N1–N9，32→42 全绿
python verify_pitch.py        # P5 耳返开关只动监听，不回归
python verify_ui.py
python verify_mobile_play.py all
python verify_diag.py
python verify_gapless.py
python check_release.py
```

N 组（CDP 注入 fake `window.ElysiumAndroid` + 现有 fake mic）：

| ID | 断言 |
| --- | --- |
| N1 | 桌面/无桥不显示原生行，原耳返行为不变 |
| N2 | Android + fake bridge 显示原生行且默认开 |
| N3 | begin 调 start；参数含 gain/toneDb/muted；`KAR.echoGate.gain≈0` |
| N4 | micVol/tone 变更调 update，参数正确 |
| N5 | echo off → `update.muted=true`，echoGate 保持 0；echo on → muted=false |
| N6 | start 返回失败 → echoGate 恢复 1、toast、无异常 |
| N7 | status `running=false` / 双向 RMS 异常 → 3 次轮询内回退 |
| N8 | `karStop`/`karClose` 调 stop |
| N9 | `__elysiumBack` 在 K 歌中返回 true 并清理 |

真机 gate（用户配合装包）：

- 外放跑「原生耳返自检」：RTT **≤90ms**（目标 30–60ms），记录 hits/route/buffers，
  与 web `diagMicRtt` 的 179ms 对比。
- 有线耳机 K 歌：原生开关生效、无双重人声、echo/micVol/tone 实时、录音/音准正常、
  保存 WAV 非静音。
- 抢麦验证：任一链路 RMS 异常能自动回退并提示。
- 蓝牙：读数提示「延迟不可控，建议有线」，不作为通过条件。

## 分工与顺序

1. 设计文档（本文件）。
2. 原生引擎 + 桥（先出 debug 包验证 start/status/自检）。
3. Web 接入 + UI + 看门狗/回退。
4. 原生自检 + `verify_karaoke` N 组。
5. 回归 + beta8 APK/桌面 exe + 真机 gate。
6. ROADMAP/README/memory 收口。

## beta9：Oboe / AAudio 双流（2026-11-14）

beta8 真机自检：**217.7ms（208–225，8/8 命中，speaker）**，但 `status` 里
`recordBufMs=200`、`trackBufMs=128` 暴露纯 Java 的 minBuffer 天花板。自检 writer
从 0s 就把 128ms AudioTrack 缓冲灌满，所以 217.7 是**满缓冲上界**，不是 monitor
稳态；接有线耳机同样能感到延迟。按 D-2 预案直接上 Oboe，不再在 Java 层修修补补。

**架构**
- Oboe 1.9.3 的 `Direction` 只有 `Input/Output`，没有 Duplex：改成**两条
  `LowLatency/Shared` 流 + SPSC 环形缓冲**（容量取 2 的幂，默认 ≥4096 帧
  ≈85ms，预填 2×输出 burst ≈8ms，正常占用≈预填量级）。
- 输入回调做干声 DSP（HPF 80Hz → gain → 2.5k peaking → 软限幅）入环；输出回调
  出环写设备。录音/混响/音准/伴奏仍在 WebView。
- 采集 preset 优先级：`VoicePerformance`（AAudio 的 K 歌专用 preset，API 29+）
  → `Unprocessed` → `VoiceRecognition` → `Generic`；输出 `USAGE_MEDIA` +
  `CONTENT_TYPE_MUSIC`。全部共享模式，**不抢 Exclusive**（WebView 伴奏还要混音），
  不申请音频焦点、不建 MediaSession。
- **三层回退** `KaraokeMonitorHub`：Oboe → beta8 Java 引擎 → WebView 耳返。
  页面 `status.engine` 显示 `oboe`/`java`；Oboe 失败原因进 `oboeError` + logcat。
- 原生自检改成输入/输出两个回调各自打时间戳：输出回调发 8 发 1kHz/60ms 脉冲，
  输入回调做 RMS 本底 + onset 检测；RTT 同时回报 `inputLatencyMs` /
  `outputLatencyMs` / `xruns`，不再受“自检灌满输出缓冲”影响。
- 状态新增 `ringMs` / `ringFrames` / `overruns` / `latencyMs` / `source`
  （`voice_performance` 等），页面调音台显示引擎、缓冲；诊断面板显示引擎/API/
  输入输出延迟。

**构建**
- NDK `26.1.10909125` + CMake `3.22.1` + Oboe AAR `com.google.oboe:oboe:1.9.3`
  （Google Maven prefab）；`buildFeatures { prefab true }`；
  `arguments "-DANDROID_STL=c++_shared"`；`abiFilters` 仍 arm64-v8a + armeabi-v7a。
- 产物含 `libelysium_oboe.so`、`liboboe.so`、`libc++_shared.so`；JNI 符号
  `Java_com_elysium_player_OboeKaraokeMonitor_native*` 已核验。

**回归 / 出包**
- `verify_karaoke 42/42`、`verify_pitch 20/20`、`verify_diag 10/10`、
  `verify_gapless 16/16`、`verify_mobile_play 2/2`；`verify_ui 17/20` 的
  T0/T1 搜索用例是上游无结果（与本改动无关，`verify_karaoke` 的 U 组全绿）。
- `Elysium-2.3.0-beta9.apk`：versionCode 32，29,230,153 字节（约 27.9 MiB），
  md5 `e85eb12d0d473616d59bee5db75dc51a`，
  sha256 `a8809b6a2512e0fc54852f609bbd9ad00941fb3efd59eae0d8977f553af1e4f1`。
- `check_release.py` 仍报历史 3 条工作区路径泄漏（beta8 就存在，需
  `git filter-repo` 另案处理；当前工作区新增文件已清白）。

**待真机 gate**
- Oboe 自检 RTT（目标 30–60ms，gate ≤90ms），`source` 记录实际 preset；
- 有线耳机 K 歌：无双重人声、echo/micVol/tone 实时、录音/音准正常；
- 双采集抢麦看门狗、蓝牙提示、Oboe 失败时 `回退 Java` 文案与回退链路；
- 长唱 3–5 分钟观察 `ringMs` 是否漂移、`underruns/overruns` 是否为 0。

## beta11：唱完闪退修复（2026-11-15）

beta10 真机反馈：耳返缓冲可接受（Oboe 双流生效）；**唱完闪退**。

**根因（beta10 面包屑 + adb logcat 崩溃栈实证）**：Android 15 的 legacy AAudio
输入路径（AudioRecord 承载）上，`close()` 释放 AudioRecord 缓冲**不等在飞回调
结束**——收口时还在执行的输入回调醒来后往已解映射的缓冲 memset（128 帧 ×2 字节
= 正好一个输入 burst）→ SIGSEGV 闪退。面包屑止于 `stop: in stop/close begin`，
崩溃栈顶 `__memset_aarch64 ← Engine::onAudioReady`，完全咬合。

**修复（beta11，versionCode 34）**：
1. `mTearingDown` 收口旗：置位后音频回调**一律不碰 data** 直接 Continue
   （删掉收口期两处 memset——正常运行的欠载分支保留）；
2. `closeStreamsLocked` 改为：双流 `requestStop`（不阻塞）→ **120ms 宽限期**
   （在飞回调在缓冲还映射着时退出）→ 再逐条 close；
3. start/startDiag 复位旗。诊断层不动。

**验证**：真机（Redmi Note 12 Turbo / Android 15 / MIUI，OS3.0.3.0）唱完不再
闪退；`adb pull` 装机 APK 回本地仓库校验 md5 与 box 构建一致
（1df995a34a13a274384664629394b2d0，29,250,629 字节）。回归 verify_karaoke 42/42。

**真机读数（beta11，Oboe fast path 确认生效）**：`缓冲 3/11ms · 环 16ms ·
voice_performance · aaudio`（对比 beta8 时代 minBuffer 200/128ms）。已知小尾巴：
开唱瞬间 xrun 1024/0（4 次输出欠载，~21ms 累计）——备选微调：输出回调粒度
`setFramesPerDataCallback(128)` 对齐输入 burst（beta12 做过未真机验证，已回退，
待并入下次迭代），不再单独开版。

**Gate 结果（beta11）**：原生自检 RTT **105ms**（>90ms gate，<179ms web 基线）。
logcat 实证：我们的两条流都正确请求 LOW_LATENCY（perfMode=12），但输入实际落在
legacy AudioRecord 路径（MediaTek/MIUI 对 MMAP 采集静默回退；logcat 里
"MMAP not used" 那条是 WebView getUserMedia 的，perfMode=10 才是真没请求的）。
105ms ≈ L_out + L_in_deliver，为该平台 legacy 路径地板；体感用户已确认可接受。
30–60ms 目标需要平台 MMAP 支持，另立课题不再追。

**调试基建（beta10 起）**：CrashLog（桥 try/catch + 未捕获 Java 异常）、原生
面包屑 native-log.txt、自检面板「崩溃日志」按钮；adb USB 直连（平台工具在
<dev-root>\.tools\platform-tools）。测试循环：我发包 → 手机浏览器一键
下载（gofile 直链 curl 已死，只能浏览器）→ adb pull/install → adb 驱动复现 →
拉崩溃栈。MIUI 的「USB 调试（安全设置）」重插线会重置，输入注入不可依赖；
读界面树（uiautomator dump）不受影响。

- 2026-11-14 立项：beta7 实测 179ms 触发；设计评审通过，按本清单动工。
- 2026-11-14 代码落地：
  - `android/.../KaraokeMonitor.java`（纯 Java，采集/HPF/增益/音色/软限幅/统计/脉冲自检）；
  - `MainActivity` 五个桥方法 + `onStop`/`onDestroy` 停引擎；
  - `web/app.js` 原生会话、参数镜像、双向 RMS 看门狗、失败回退；
  - `index.html`/`style.css` 调音台「原生直通」开关 + 读数；诊断面板「原生耳返自检」；
  - `verify_karaoke.py` 增 B13 + N1–N9，42/42 全绿；`verify_pitch` 20/20、
    `verify_ui` 20/20、`verify_mobile_play` 2/2、`verify_diag` 10/10、
    `verify_gapless` 16/16（回归中的一次 19/20、15/16 为并行/时序抖动，单跑复绿）。
  - 顺手把 `verify_diag.py` / `verify_mobile_play.py` 的硬编码工作区路径改成
    `os.path.dirname(__file__)`；`check_release.py` 工作区已清白，历史提交里的
    3 条旧泄漏仍需 `git filter-repo` 另案处理。
- 2026-11-14 出包：`Elysium-2.3.0-beta8.apk`（versionCode 31，26,198,659 字节，
  md5 `ec63f343517855505391466d9ff9405c`，sha256 `e9c6bcaac628f19b123edb318a03043d148461b709dae6c64188e51049c8bd57`），`aapt` 核验版本/权限，`classes.dex`
  含 `KaraokeMonitor` 与五个桥方法，`app.imy` 含 `karaoke_native_mon` 与原生文案。
  桌面 `dist/Elysium.exe` 同步重建，`--serve` 冒烟能从包内取到新 app.js。
- 真机 gate 待用户装包：外放「原生耳返自检」RTT ≤90ms（目标 30–60ms）；
  有线耳机 K 歌无双重人声、echo/micVol/tone 实时、录音/音准正常；双采集抢麦回退提示。
