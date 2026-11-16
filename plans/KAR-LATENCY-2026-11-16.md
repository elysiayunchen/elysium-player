# K 歌耳返延迟「压到物理地板」（P11.5）设计与实现清单（2026-11-16）

> 起因：beta11 收口时把「自检 RTT 105ms、体感可接受」写进了 gate，用户实测推翻——
> **有线耳机下耳返延迟不可过**，此前判断草率。beta11 的 105ms 是外放数字
> （MIUI SmartPA 扬声器保护 DSP 还在抬），有线耳机路径的真实耳返整链 Lv 从未测过。
> 流缓冲已极简（3/11ms + 环 16ms），约 75ms 藏在平台层：MTK/MIUI 把 MMAP 采集
> 静默回退 legacy AudioRecord 路径（logcat 实证）。
>
> 原则：**数据先行**——每个结论都要有真机读数；**回归安全**——默认行为与
> beta11 完全一致，新通路只在用户/自检显式选择时启用；**失败回退**——
> prefer 失效/引擎失败退回默认顺序与 WebView 耳返。

## 0. 定理：监听侧补偿无效（本课题的边界）

设歌手跟着「听到的拍子」唱：拍 τ 被听到于 `t0(τ)+Lb+Da`（Lb=伴奏输出延迟，
Da=监听侧伴奏延迟），他在此刻发出拍 τ 的人声，人声被听到于
`t0(τ)+Lb+Da+Lv+Dv`（Lv=耳返整链，Dv=监侧人声延迟）。此刻同时听到的伴奏是
`τ+Lv+Dv`——**人声相对伴奏的听感偏移恒等于 Lv+Dv，与 Da 无关**。
推论：延迟伴奏救不了偏移，延迟人声只会更糟，唯一出路是降 Lv 本身。
（beta11 的 D-3「不做人声补偿」升级为定论，且扩展到伴奏侧。）

## 1. 已确立的事实链（beta7→11）

| 版本 | 通路 | 自检 RTT | 关键读数 |
| --- | --- | --- | --- |
| beta7 | WebView getUserMedia→JS 图 | 179ms | 采集隐藏缓冲 ~110ms + 输出 64ms |
| beta8 | 纯 Java AudioRecord→AudioTrack | 217.7ms | minBuffer 天花板 200/128ms |
| beta11 | Oboe 双流 LowLatency/Shared | **105ms**（外放） | 缓冲 3/11ms · 环 16ms · voice_performance · aaudio；logcat：输入落 legacy AudioRecord 路径（MMAP 未启用） |

105ms 拆账：流缓冲 3+11ms + 环 16ms ≈ 30ms，**~75ms 在平台层**
（legacy 采集 HAL 周期 + AudioFlinger 递送 + SmartPA）。

## 2. 方案：三段式，每段用数据定形下一段

### Phase 1 — beta13（versionCode 36）「测量 + 低成本调优」包

| ID | 项 | 做法 |
| --- | --- | --- |
| A-1 | 输入候选矩阵 | (AAudio\|OpenSL) × {VoicePerformance, VoiceCommunication, Unprocessed, VoiceRecognition, Generic}；`start` 带 `prefer` 数组排候选顺序；**mPrefer 为空时与 beta11 逐字节一致**（默认 api、原 4 preset 顺序、mono→stereo） |
| A-2 | sweep 自检 | `startKaraokeMonitorDiag('{"sweep":true}')`：输出流开一条全程复用，逐候选开输入各发 4 发脉冲（lead 0.7s / gap 0.5s，单候选 ~2.7s，10 候选 ~30s）；结果经 `status().sweep` 轮询：{running, done, error, current, results[{label, rttMs, hits, inBurst, inCap, in/outLatencyMs, xruns, ok}]} |
| A-3 | 输入缓冲收口 | 开完流 `setBufferSizeInFrames(min(cap, 2×burst))`（Oboe 低延迟实践，减客户端积压余量）；不支持则保持默认（AAudio input 可能 ErrorUnimplemented，面包屑记录，非致命） |
| A-4 | 遥测拆分 | status 增 `inApi` / `inputBufferCapacityFrames` / `inCalcLatencyMs` / `outCalcLatencyMs`；diag 增 `inApi` / `inputBufferCapacityFrames`——105ms 的糊涂账从此能归因到腿 |
| A-5 | 输出闲置清零 | onAudioReady 收口/闲置分支对输出 memset 0：sweep 候选间隙输出流还在跑，不清零会反复外放残帧爆音（输入侧仍绝对不碰 data，Android 15 SIGSEGV 协议不变） |
| A-6 | prefer 落库 | 设置键 `karaoke_native_prefer`（白名单：api/preset 标签或裸 preset，空串=清除）；`karaoke_solo_capture`（bool 硬校验） |
| A-7 | sweep 面板 | 设置→网络自检→「输入通路扫描」：进度（当前候选）→ 结果表（★最优）→「应用最优」写 `karaoke_native_prefer`；有偏好时显示「当前应用」+「清除偏好」 |
| A-8 | 单采集实验 | 调音台「单采集实验」开关（默认关）：开则下一场 K 歌 `getUserMedia` 整个不申请（复用 noMic 降级，reason 写明实验），只有原生一路采集——**单变量鉴别「双采集并发把输入压到 legacy/抬高低延迟余量」**；看门狗 badWeb 分支退场（micLevel 恒 0 必误判） |
| A-9 | 读数拆分 | 调音台读数：`源/inApi`（inApi≠输出 api 才显示）、`链 in/out ms`（calculateLatencyMillis 拆腿）；单采集时加「单采集实验」标 |

已知取舍：VoiceCommunication preset 可能带硬件 AEC/NS 染色——扫描表标注
「通话（可能带 AEC/NS 染色）」，应用最优后必须人耳复核；OpenSL 输入在部分
机型粒度更粗，以 sweep 数字为准。

**真机 gate（待回填）**

- [ ] dumpsys media.audio_policy：输入 profile 有无 MMAP/fast flag（决定 Phase 2 Exclusive）
- [ ] 有线耳机戴测 sweep 全表（10 候选 × RTT 中位）：____
- [ ] solo on/off 自检 RTT 对比（双采集惩罚有无）：____
- [ ] 常规自检 RTT（beta11 基线 105ms 外放 / 有线待测）：____

### Phase 2 — beta14 结构重构（按 Phase 1 数据定形）

| ID | 项 | 做法 |
| --- | --- | --- |
| B-1 | 单采集 | Oboe 输入独占采集：C++ 第二 SPSC 环 → JNI pump 线程 → base64 PCM chunk（20Hz，~6KB/条）→ `evaluateJavascript('window.__elysiumKarPcm(...)')`（复用 dispatchMedia 注入先例）；JS 端 ScriptProcessor 源节点拉队列替换 micSrc，下游 micTap/效果链/录音总线全不动 |
| B-2 | MMAP/Exclusive | 若 dumpsys/sweep 证明存在 MMAP-capable source：单采集下尝试 Exclusive 输入（无并发客户端） |
| B-3 | 音准对齐补偿 | karpACF 窗口对齐补 ΔC（diag 输入腿延迟）——顺带修掉现存 ~110ms 系统性打分偏移（音准打分从 bipolar 变公平） |
| B-4 | 看门狗改写 | 双 RMS 抢麦判定退役，改 relay 新鲜度（>300ms 断流回退双采集）+ 原生 running/underrun；getUserMedia 全链保留为回退 |

### Phase 3 — 收口

- 地板结论（RTT + MMAP 有无 + 最优源）进 ROADMAP P11.5 与调音台文案；
- 若地板 >60ms 且 MMAP 缺失：如实标注「此机平台地板」，建议 USB-C 数字耳机/
  USB 声卡（绕 SoC 音频路径，通常 20-40ms）；
- memory 落库：修正 beta11「体感可接受」的错误 gate 判断。

## 3. 不做的事

- 监听侧任何延迟补偿（定理见 §0）；
- 伴奏/混音搬原生（对 Lv 无贡献，共模）；
- TWS 蓝牙优化（物理 150ms+ 无解，只做警示文案）；
- 桌面端（WebView 链 30-60ms 已达标）。

## 4. 验收

```bash
python verify_karaoke.py    # B14/B15 + N10-N13 增组，46→51 全绿
python verify_pitch.py && python verify_diag.py && python verify_gapless.py
python verify_mobile_play.py all && python verify_ui.py && python check_release.py
```

真机：有线耳机戴测 sweep 表 → 应用最优 → 常规自检 RTT → solo on/off 对比 →
完整 K 歌回归（录音/音准/混响/保存 WAV/返回键/切后台）。

## 落地记录

- 2026-11-16 代码落地：oboe_karaoke.cpp（候选矩阵/sweep/缓冲收口/遥测拆分/
  输出闲置清零）、OboeKaraokeMonitor.java（prefer/sweep 透传）、web/app.js
  （扫描面板/solo/读数拆分/prefer 透传）、index.html（kSoloRow）、main.py
  （两键白名单）、verify_karaoke.py（B14/B15 + N10-N13）。
- 版本号：未提交工作区已把 beta12/versionCode 35 占给端口复用修复线，
  本线顺延 beta13/versionCode 36（结构重构 beta14/37）。
- 回归：verify_karaoke **48/48**（含 N12 修正——3 秒测试曲会在看门狗观察窗内
  唱完触发 ended→karStop，先等 sing 再暂停 `<audio>`；N13 假桥 statusResp 需带
  engine:'oboe' 才走进 oboe 分支）、verify_pitch 20/20、verify_diag 10/10、
  verify_gapless 16/16、verify_edge 92 PASS。
- 构建机同步（教训两条）：① 构建树 /workspace/elysium-repo 的 git HEAD 停在
  beta6，`git diff` 对比全失真（对 origin/main 也一样——远端 main 也停在
  beta6，94d2225 从未推送），文件级 md5 对账 + patch 都不可靠；② `git checkout
  FETCH_HEAD -- .` 在 FETCH_HEAD=beta6 时会把 box 的 beta7-12 工作区覆盖掉
  （未跟踪的 cpp/Java 引擎文件幸存）。**正确姿势**：Windows 临时分支
  `build/beta13-sync` 提交工作区快照推上去（repo 本就 public、*.apk 已
  gitignore），box fetch 后 checkout 对齐，gradle.properties 用 box 本地变体
  覆盖回去。此后每轮同步都走这条分支通道。
- **beta13 出包**（box assembleRelease --offline，CMake 首跑暴露
  `convertToText(ResultWithValue<int>)` 无实例化 → 改取 `.error()`）：
  `Elysium-2.3.0-beta13.apk`，versionCode 36，29,295,689 字节，
  md5 `f22991a4acef9140165c81cfe382cee1`，
  sha256 `e1bbfaeb9403233aa3cf7d741a8c7caccda81ab22a0f09b073d919e707ef9770`。
  aapt 核验版本 ✓；包内 app.imy 含 `karaoke_native_prefer`/`_KAR_NATIVE_PREFER_OK`
  /`diagSweepBtn`/`kSoloRow` ✓；双 ABI `libelysium_oboe.so` ✓。
  交付：box 快隧道出不去（UDP DNS 不通），走 adb 通道装机。
- 同步分支 build/beta13-sync（681df7c）比 box 树落后 cpp 的 3 行
  convertToText 修复（box 上直接改的）——beta14 同步时重新推快照覆盖。
- **2026-11-17 历史重写（filter-repo，用户拍板强推）**：历史 blob 与 commit
  消息里的开发机工作区路径（check_release.py 规则表同款）全部替换为
  `<dev-root>`——涉 verify_diag 旧版 / verify_mobile_play 旧版 /
  NATIVE-MON 旧版共 4 个 blob + 825ccdc（重写后 4c8c2f7）1 条消息。
  v1.0.0–v1.6.0 四个 tag 不含泄漏祖先，SHA 原封未动；main 677bf02 →
  **ccb80e0**、build/beta13-sync 0ad3cb4 → **6f53c81**（box 的 beta14
  快照已随之改写）；本地 .git 75MB → 22MB（旧对象已剪）；全量
  check_release（含历史）复核通过，CI 恢复全量扫描（不再 --no-history）。
  旧历史备份 bundle 在开发机工作区根（Elysium-backup-pre-rewrite-20261116）。
  **box 构建树注意**：远端 SHA 已全变、旧 SHA 一律失效，fetch 后按分支名
  checkout 对齐即可（本分支通道约定不变）。
