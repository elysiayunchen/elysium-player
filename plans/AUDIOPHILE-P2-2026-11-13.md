# 发烧客模式 Phase 2 实现清单（2026-11-13）

> Phase 1（后端地基）已落地：FLAC STREAMINFO 解析 + probe `hires` + `loudness_set` + 响度设置契约，
> verify_audiophile 38/38（见 `audiophile_report.md`）。
> 本清单 = Phase 2「高级调音」：参数 EQ + 自动前级 + AutoEq 导入（A~D 组），尾部挂 2b 响度应用与 Hi-Res 徽标。
> 原则：**老用户零变化**（图形 EQ 原路径一字不动）、**不给假情报**（非法数据拒绝、徽标宁缺勿滥）、
> 控件带标签读数（对齐 K 歌调音台）、K 歌图（karBuild）完全不动。

## A. 设置契约（后端先行，main.py `_clean_settings` / `DEFAULT_STORE`）

| ID | 键 | 契约 | 校验 |
| --- | --- | --- | --- |
| A-1 | `eq_mode` | `'graphic'` \| `'parametric'`，默认 graphic | 白名单外丢弃 |
| A-2 | `eq_bands` | ≤10 段 `[{type,f,q,db}]`，type: peaking/lowshelf/highshelf | f∈[20,20000]、q∈[0.1,10]、db∈[-12,12]，**isfinite 硬墙**（NaN 落 data.json = 整库报废，同 loudness/pitch 铁律）；单段非法丢单段，非数组整表丢弃 |
| A-3 | `eq_preamp` | 前级增益 dB，默认 0 | 钳 [-12,12]，**保留一位小数**（AutoEq 解析值 -6.5 这类小数，走 `_SETTING_NUM` 的 int 口径会被截断；2026-11-13 起为独立分支） |
| A-4 | `eq_auto_preamp` | 自动前级补偿开关，默认 true | 非 bool 丢弃 |
| A-5 | `eq_headphone` | AutoEq 导入的耳机型号名（仅显示用） | strip + 截断 80 字符 |

老 `eq_gains` / `eq_preset` / `eq_enabled` 原语义原路径不动——A 组全是并列新键，图形模式用户零变化。

## B. 引擎图改造（web/app.js，等前端 UI 优化收尾）

| ID | 项 | 位置 | 做法 |
| --- | --- | --- | --- |
| B-1 | 前级节点 | `engBuild` app.js:1110 | 图变成 `src→gain→preamp→eq[0..9]→master→destination`；**图不重建**，运行时只改值 |
| B-2 | 参数模式 | `engBuild`/`engApplyEq` | 10 个 BiquadFilter 节点复用：参数模式按 `eq_bands` 重配 type/frequency/Q/gain；不足 10 段的余下节点补 identity（peaking + gain 0）；图形模式照旧 `EQ_FREQS` + Q=1 |
| B-3 | 自动前级 | `engApplyEq` | `preamp = (eq_auto_preamp ? -max(0, maxBandDb) : 0) + eq_preamp`，`setTargetAtTime` 平滑防爆音 |
| B-4 | 段数增减 | `engApplyEq` | 固定 10 节点不动（加/删段只是重配），不做图的动态增删 |
| B-5 | 响度预留 | `engApplyEq` | 2b 的响度增益落 master，与 preamp 相加后**上限钳 0dB**（防叠加削波）；本期只留变量位与注释 |

## C. AutoEq 导入（C-1 后端已做，C-2/C-3 前端）

| ID | 项 | 做法 |
| --- | --- | --- |
| C-1 | 文本解析器 | **后端已落地（2026-11-13）**：`parse_autoeq_apo(text)` + POST `/api/eq/parse`。APO/parametric 格式：`Preamp: -3.5 dB` + `Filter N: ON PK/LS/HS Fc 105 Hz Gain 6.5 dB Q 0.71`，逐行正则；PK→peaking、LS→lowshelf、HS→highshelf；≤10 段（超出截断并报 `truncated` 数）；**一条都解不出来 = 400 报错不落盘**。解析放后端的原因：段表校验口径只写 main.py 一处（`_clean_bands` 与 settings `eq_bands` 同源），不在 JS 里复刻第二遍。前端 C-2 只 POST 文本拿 `{preamp, bands, total, dropped, truncated}` |
| C-2 | 入口 UI | 均衡器区「导入 AutoEq」：粘贴框 + 型号输入（填 `eq_headphone`）+ 导入；数据来源 autoeq.app（CC-BY-4.0 署名放设置页脚注） |
| C-3 | 导入后 | `eq_mode=parametric`、`eq_preamp=解析值`、`eq_preset='autoeq'`（`EQ_LABEL` 增 autoeq:导入）；顺手 `eqpreset_set` 存一份进预设库，换耳机一键切回 |
| C-4 | 预设库存取（后端已落地） | store op `eqpreset_set` / `eqpreset_del`：`{name: {bands, preamp, at}}`，name strip 截 80、段表走 `_clean_bands` 同源校验（NaN 硬墙）、preamp 钳 [-12,12] 保留一位小数、空名或空段表不写；`EQPRESET_MAX 50` 超限按 at 淘汰最旧（与 LOUDNESS_MAX 同策略）；列表随 `load` 快照带出，WebDAV 同步免费搭车 |

## D. UI（设置抽屉均衡器区升级，遵 UI 审美硬标准）

| ID | 项 | 做法 |
| --- | --- | --- |
| D-1 | 模式切换 | 图形（现状 10 推子）/ 参数 segmented 两态 |
| D-2 | 参数段行 | 每段：类型 chips（峰/低架/高架）+ 频率/Q/增益三推子**带标签读数**（对齐 K 歌调音台的读数样式）；「加一段」≤10、单段删除 |
| D-3 | 前级读数 | 「前级 -3.5 dB · 自动补偿 2.5」标签读数，不裸摆数字 |
| D-4 | ≤480 布局 | 参数段行三控件纵向堆叠或两行排（参考移动端布局事实六坑清单）；复用 `eq-slot` 18px 槽样式 |
| D-5 | 拖动防抖 | 沿用 `engSetBand` 的 350ms 防抖 + 即时听感 |

## E. Phase 2b（随后，简列）

- **响度扫描**：K 加权双级 biquad（ITU-R BS.1770 系数，前端自写 ~30 行）+ OfflineAudioContext 解码算 integrated LUFS → `postStore('loudness_set')`（后端地基已就位）
- **响度应用**：`loudness_mode=track` 时 master 预置 `target − lufs`（与 preamp 合算钳 0dB）
- **Hi-Res 徽标**：播放条/详情读 probe `out.hires`，显示 96kHz/24bit；无数据不显示（不给假情报）
- 再往后：BS2B crossfeed / butterchurn 频谱 / ABX 盲听（另立清单，不塞进本期）

## 验收

```bash
# 后端（A 组 + C 组半边，均已落地）：
python verify_audiophile.py        # H 组 A-1~A-5 + I 解析器 / J 预设库 / K HTTP 端到端，全绿
python verify_edge.py              # E 组（store 脏数据）不回归
python verify_import.py --no-ui    # 31/31；verify_sync 26/26（eqpresets 随同步搭车）
# 前端（B/C/D 组，UI 收尾后）：verify_engine 风格 selenium
#   - 参数模式拖推子 → biquad type/frequency/Q/gain 实测变化
#   - 自动前级数值：bands [6,0,...] → preamp -6
#   - AutoEq 导入 UI：POST /api/eq/parse 好/坏文本 + eqpreset_set 存档
#   - 老用户回归：eq_mode 缺省 graphic + eq_gains 照旧（verify_np / verify_ui 不破坏）
python verify_np.py && python verify_ui.py
```

出包：每阶段 APK + 桌面 exe 测试包 + 包内容核验 + android-UA 冒烟（惯例，不许攒）。

## 分工与顺序

1. **后端 A 组契约 + H 组验证**（已落地 856987a）+ **C 组半边：C-1 解析器 + C-4 预设库**（已落地，
   I/J/K 组验证；不碰 web/*——那是前端 WIP，提交时按文件归组）
2. **前端 B/C-2/C-3/D**（UI 优化收尾后动工；C-2/C-3 只 POST 文本 + 存预设，不再自己解析）
3. **E 组 2b**（响度/徽标，B 组落地后）

## 落地记录

- 2026-11-13 后端 A 组：`DEFAULT_STORE` 五键 + `_clean_settings` 四分支（eq_mode 白名单 / eq_bands
  isfinite 硬墙 + 逐段校验 + 截 10 段 / eq_auto_preamp 只收 bool / eq_headphone 截 80）+
  `_SETTING_NUM` 增 `eq_preamp` 钳 [-12,12]；verify_audiophile 增 H 组 16 例，**54/54 全绿**
  （含老 eq_gains 键回归，详见 `audiophile_report.md`）。
- 2026-11-13 后端 C 组半边（解析器 + 预设库）：`_clean_bands(v, cap)` 从 `_clean_settings` 抽出
  （settings 与 AutoEq 解析一套口径；语义升级为「先收合法段再截断」，前面段非法不占后面段名额）；
  `parse_autoeq_apo` + POST `/api/eq/parse`（纯解析不落盘，400 带可展示错误文案）；`eqpresets`
  预设库 + `eqpreset_set/eqpreset_del`（C-4，上限 50 按 at 淘汰，`_prune_by_at` 与 loudness 共用）；
  `eq_preamp` 迁出 `_SETTING_NUM` 改一位小数（AutoEq 解析值 -6.5 不再被截成 -6）；verify_audiophile
  增 I/J/K 三组 41 例，**95/95 全绿**；回归 edge 91 PASS/0 FAIL/1 WARN、import 31/31、sync 26/26
  基线不变。
- 2026-11-13 前端 B/C-2/C-3/D + 2b 徽标：引擎图 `src→gain→loud→preamp→eq[0..9]→master`（B-1，
  节点名 preamp——`ENG.pre` 已被预载信息占用）；`engApplyEq` 双模式重配（B-2/B-4，10 节点复用，
  不足 10 段落 identity，图形模式照旧）；自动前级 `−max(0, maxBandDb) + eq_preamp` 钳 [-24,12]
  setTargetAtTime 平滑（B-3，响度增益在各槽 louds 分层不相加，注释说明 B-5）；C-2/C-3 导入 UI
  （型号 + APO 粘贴框 + 解析按钮，POST `/api/eq/parse` 不在 JS 复刻口径；成功落 parametric +
  eqpreset_set 存档案库，失败 err toast 段表不动）；D 组设置抽屉 EQ 独立分区（模式 segmented
  对齐 K 歌 .k-seg 语汇；参数段卡片 = 类型 chips + 频率/Q/增益三横推子带读数，频率/Q 对数轴
  0-1000 映射三/两个十倍频程；前级步进 ±0.5 读数「手动 dB · 补偿 −x.x」（负号 U+2212 对齐
  响度/变调读数排版）；档案库一键切回 + 删档案；值改动 350ms 防抖、结构改动即时落盘）；Hi-Res
  徽标读 probe STREAMINFO 实测（>48kHz 或 >16bit 才标，44.1k/16 与无数据都不显示），播放条
  没测过的歌后台补一发探测；refreshStore 增 `state.eqpresets`，同步合并后 `engApplyEq(true)`+
  响度跟一次（修 eq_* 同步回来不热生效的旧缺口）；EQ_LABEL 增 autoeq:导入。新增 verify_eq.py
  （A 老用户回归 / B 参数引擎实测含对数轴与防抖 / C 导入好坏文本与档案库 / D 徽标 / E 收尾，
  **38/38 全绿**；顺带抓出「段增益拖动不刷新前级读数」一缺陷）；回归 verify_np 27/27、
  verify_ui 20/20、import 31/31、sync 26/26、audiophile 95/95、edge 91 PASS/1 WARN 基线不变。
- 2026-11-13 三路评审收口（前端代码 / 后端契约 / 交互设计，并行子代理）：
  **前端 2 P1** —— ①参数段防抖闭包捕获旧数组，拖动 350ms 内做结构改动会把改动前段表
  写回服务端（改「定时器到点现读内存」+ 结构改动/应用档案/teardown 撤防抖，verify_eq 增
  B19 交错钉子）；②自动补偿/手动前级在图形模式同样生效但控件全在参数面板（老用户无感
  掉音量，违背「图形模式零变化」）—— 前级收窄为参数模式专属（engApplyPreamp active 闸，
  图形恒 0dB，verify_eq 增 A6 钉子）。**后端 1 P2 语义修正** —— eq_headphone 空串从
  「丢弃保留旧值」改为「显式清空落盘」（否则前端清空型号重启后旧型号复活回 UI），
  verify_audiophile 增 round-trip 断言 + /api/eq/parse 超 1MB、/api/store 超 4MB 两个 413
  用例 → 98/98。**导入链路加固** —— eqImportSubmit 校验响应形状（ok!==true / 空段表一律
  报错，绝不把用户当前调音清空）、eqpreset_set 存档改 await 不吞错、删档案走 confirm
  二次确认 + 失败 toast。**交互打磨** —— ≤480 触点抬到 ≥28-30px（seg/chips/步进钮/删钮/
  推子命中区，np-seek 范式）、`set-actions` 弹性规则收窄到 `#eqParamPane`（旧规则会把
  数据区的清空类危险按钮一起等分拉宽）、删档案挪到档案库行内 + danger 色、「加一段」升
  primary、前级 tip 改「手动 + 补偿相加才是链路实际增益」+ 读数补「实际」段、eq-pre-val
  去定宽、Hi-Res 徽标单位写全 kHz（与码率 k 撞量纲）、≤480 np-spec 加省略防挤标题、
  导入盒展开 scrollIntoView + 成功自动收盒清文本、loudness up 按钮 ＋ 全角统一、`.set-seg`
  并入 `.k-seg` 基类防漂移、「段表」黑话退役、reduced-motion 注释同步 probepulse、
  loudness 内存与落盘同口径钳 [-70,0]、AbortController 缺失守卫、probeForHires 按探测音质
  记 TTL（MP3 探过切 FLAC 能重探）、推荐卡探测徽标错峰（qualityChip 拆 delay/swap 语义）。
  np-seek 轨道 3px→3.5px 对齐统一后的 --seek-h。**verify_eq 40/40**；回归 verify_np 27/27、
  verify_ui 20/20、import 31/31、sync 26/26、audiophile 98/98、edge 91 PASS/1 WARN 不变。
