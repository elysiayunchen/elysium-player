# 发烧客模式 Phase 3 实现清单（2026-11-14）

> Phase 2 已全链路收口（v2.3.0-beta1 三路评审，verify_eq 40/40；响度扫描/应用也在 main），
> 无半成品。本清单 = P2 尾部明写「另立清单」的三件挂账：**BS2B crossfeed / ABX 盲听 /
> butterchurn 频谱**（2026-11-12 调研卡已核实许可证与实现路径）。
> 原则沿用 P2：**老用户零变化**（crossfeed 默认关，图形/参数 EQ 原路径一字不动）、
> **不给假情报**（盲听结论按二项分布如实报数、WebGL2 不支持就不显示入口、
> crossfeed 不冒充"空间音效"）、控件带标签读数、K 歌图（karBuild）完全不动。

## 优先级结论（为什么是这个顺序）

| 序 | 项 | 理由 |
| --- | --- | --- |
| 1 | **crossfeed** | 三件里唯一改善每一分钟听感的；纯 Web Audio 自写移植，**零外部依赖**，与全仓风格零冲突；实现面最小（引擎图插一级 + 两个设置键 + 一个开关）。调研卡：splitter + lowpass(~700Hz) + lowshelf + gain + merger，无需 worklet，参数参考 EasyEffects Bauer 档。也是竞品 EchoMusic「空间音效」卖点在 WebView 架构下能诚实落地的部分 |
| 2 | **ABX 盲听** | 自写两按钮小游戏（webMUSHRA Apache-2.0 但太重，不引）；它需要"有东西可盲听"，crossfeed 落地后对象才完整——放第二顺位顺理成章。给全部发烧客功能一个"你真的听得出来吗"的诚实验证器 |
| 3 | **butterchurn 频谱** | 三件里唯一要**引外部 JS 库**（全仓目前零 vendor）+ 依赖 WebGL2（Android 低端机风险）+ 纯装饰不改善听感。**spike 先行，gate 决定去留**，不硬塞本期 |

竞品对照（调研卡）：EchoMusic 发烧卖点 = 10 段 EQ 带前级补偿 + LUFS 响度标准化 + 空间音效，
前两样 Phase 2 已齐，crossfeed 补第三样的诚实版。WebView 架构做不到的（bit-perfect/独占输出、
DSD、SoX 级升频、HRTF 高阶 Ambisonic）照旧不承诺。

## A. 设置契约（后端先行，main.py `_clean_settings` / `DEFAULT_STORE`）

| ID | 键 | 契约 | 校验 |
| --- | --- | --- | --- |
| A-1 | `xfeed_mode` | `'off'` \| `'on'`，默认 off | 白名单外丢弃（与 eq_mode 同口径） |
| A-2 | `xfeed_level` | 档位 `'weak'` \| `'medium'` \| `'strong'`，默认 medium | 白名单外丢弃；不收连续数值（档位是听感调校过的组合参数，散值用户调不准） |

- 不落盘的：ABX 结果只存 session 内存（小游戏性质，累计战绩落盘是伪需求，不做）。
- WebDAV 同步免费搭车（settings 键随快照走，Phase 5 已验证过机制）。
- isfinite/硬墙铁律照旧；本组键少，verify_audiophile 增 H' 组 round-trip + 脏数据例。

## B. 引擎图 crossfeed（web/app.js，仅耳机听感，纯自写）

| ID | 项 | 位置 | 做法 |
| --- | --- | --- | --- |
| B-1 | 图拓扑 | `engBuild` app.js:1136 | `eq[9] → xfIn`，xfIn 后接 splitter/merger 串音网络，`xfOut → master`；**图不重建**，crossfeed 节点常驻，off = 串音路径 gain 0 + 直通 gain 1（旁路等价直通，与 EQ 关闭"置 0 不拆图"同一哲学） |
| B-2 | 串音网络 | `engBuild` | `ChannelSplitter` → 每声道：直通 gain + 对侧经 lowpass(fcut) → crossfeed gain → 混入；`ChannelMerger` 合回 stereo。档位三档 = fcut/feed/直通衰减 三参数的组合表，数值参考 bs2b 默认 profile 与 EasyEffects 公开默认，**动工时听感定档**，表值写进代码注释并页脚致谢（算法思想 Bauer/bs2b，实现为自写 Web Audio，不复制 LGPL 代码） |
| B-3 | 切换平滑 | `engApplyXfeed` | 档位/开关切换走 `setTargetAtTime`，防爆音（对齐前级 B-3 做法） |
| B-4 | UI 文案边界 | 设置抽屉 | 如实写「耳机串音补偿：模拟音箱自然串音，减轻头中效应」；标注"戴耳机开启"；**不出现"空间音效/环绕声"字样**（HRTF 做不到，不冒充） |
| B-5 | UI 控件 | 设置抽屉 EQ 分区旁 | 新分区「耳机串音补偿」：off/on segmented + 档位三选 chips + 读数标签（当前档 + fcut/feed 数值，对齐 K 歌调音台读数样式）；≤480 复用 `.k-seg` 基类与 ≥28px 触点 |
| B-6 | 边界 | — | 只动播放器图（engBuild）；karBuild 不插；单声道源 L==R 时低频段两路相加会抬电平——**不做 L−R 检测**（bs2b 原版也不做），默认 off 即保护，风险写进注释 |

## C. ABX 盲听（自写，全屏层，主引擎图真链路对比）

| ID | 项 | 做法 |
| --- | --- | --- |
| C-1 | 对象注册表 | 动态生成：已落地的引擎开关逐一登记 `{id, label, apply(bool)}`——EQ 开/关、crossfeed 开/关、响度归一 track/off。**对比同一 audio 元素的真链路即时切换**（setTargetAtTime 即时生效），不是双文件——本机只有一份文件，伪 A/B 不做 |
| C-2 | 流程 UI | 独立全屏层（复用 K 歌全屏层 CSS 语汇）：选曲 → 选对比对象 → 循环「听 A / 听 B / 听 X → 判 X=A 还是 X=B」→ 实时计分；随时退出，退出不伤主播放（恢复原开关状态） |
| C-3 | 判分 | ≥15 轮出结论：二项分布 P(X≥k \| n,0.5) 算 p 值，读数如实展示「15 轮对 12，随机猜对概率 p=0.018」；p<0.05 提示「这轮听出差异了」，否则「与瞎猜不可区分（本轮样本）」——结论措辞**不给假情报**，不替用户下"听不出"的绝对结论 |
| C-4 | 入口 | 设置抽屉发烧客分区尾部「盲听验证」按钮；进入前若当前曲目未加载则提示先播放 |

## D. butterchurn spike（gate 决定去留，过了才立实施细目）

| ID | 项 | gate 标准 |
| --- | --- | --- |
| D-1 | 包体实测 | 下载 butterchurn.min.js + butterchurn-presets（base/full 两 build 分开量）；presets full 数 MB 级——进包超 ~2MB 就只收 base 或子集；量完报数再决策 |
| D-2 | WebGL2 | WebView2（桌面）大概率过；Android WebView 真机实测；不支持则**入口不显示**（不给假情报），且 spike 直接判不过 |
| D-3 | 零依赖决策点 | 这是全仓第一个 vendor 的外部 JS 库（MIT，头保留版权声明 + 设置页脚注）。若决定不破例：**Plan B = 自写 2D 频谱**（AnalyserNode FFT + 2D canvas，几十行零依赖，"频谱可视化"与"MilkDrop 特效"拆开，Plan B 随时可做） |
| D-4 | 附带 | 频谱 tap 接 master（AnalyserNode 常驻零成本）；全屏可视化层复用 C 组全屏层语汇 |

## E. 验收

```bash
# 后端（A 组）：
python verify_audiophile.py        # 增 H' 组 xfeed 键 round-trip/脏数据/白名单，全绿
python verify_edge.py              # 不回归
python verify_sync.py              # xfeed 键随 WebDAV 快照搭车不回归
# 前端（B/C 组）：
python verify_xfeed.py             # 新增：图拓扑断言（splitter/merger 常驻）/ off 旁路等价
#   （OfflineAudioContext 离线渲染：左喂 1kHz（>fcut）右声道≈0；左喂 200Hz 右声道有串音）
#   / 档位切换 setTargetAtTime 平滑 / ≤480 触点
python verify_abx.py               # 新增：对象注册表按落地功能生成 / 15 轮流程 / p 值数学
#   （固定 n,k 断言二项分布数值）/ 中途退出恢复原开关状态 / 退出不伤主播放
python verify_np.py && python verify_ui.py && python verify_eq.py   # 回归基线
# D 组 spike：产出体积/兼容性报告，过了立细目，不过记账关闭
```

出包：每阶段 APK + 桌面 exe 测试包 + 包内容核验 + android-UA 冒烟（惯例，不许攒）。

## 分工与顺序

1. **后端 A 组契约 + H' 验证**（半天；不碰 web/*）
2. **B 组 crossfeed** 引擎 + UI + verify_xfeed（可出第一个测试包真机听感定档）
3. **C 组 ABX** + verify_abx（依赖 2 的 crossfeed 进注册表；1 落地后即可并行开发，只是注册表先只有 EQ/响度两个对象）
4. **D 组 butterchurn spike**（可与 1–2 并行跑，gate 报告出来再决定立细目还是记账关掉）

## 落地记录

- （未动工；本清单 2026-11-14 立项，等评审）
