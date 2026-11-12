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
| A-3 | `eq_preamp` | 前级增益 dB，默认 0 | `_SETTING_NUM` 钳 [-12,12] |
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

## C. AutoEq 导入（前端）

| ID | 项 | 做法 |
| --- | --- | --- |
| C-1 | 文本解析器 | APO/parametric 格式：`Preamp: -3.5 dB` + `Filter N: ON PK/LS/HS Fc 105 Hz Gain 6.5 dB Q 0.71`，逐行正则；PK→peaking、LS→lowshelf、HS→highshelf；≤10 段（超出的截断并提示）；**一条都解不出来 = 报错不落盘** |
| C-2 | 入口 UI | 均衡器区「导入 AutoEq」：粘贴框 + 型号输入（填 `eq_headphone`）+ 导入；数据来源 autoeq.app（CC-BY-4.0 署名放设置页脚注） |
| C-3 | 导入后 | `eq_mode=parametric`、`eq_preamp=解析值`、`eq_preset='autoeq'`（`EQ_LABEL` 增 autoeq:导入） |

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
# 后端（A 组，立即）：
python verify_audiophile.py        # 增 H 组：A-1~A-5 全部校验路径 + 老键 eq_gains 回归，全绿
python verify_edge.py              # E 组（store 脏数据）不回归
# 前端（B/C/D 组，UI 收尾后）：verify_engine 风格 selenium
#   - 参数模式拖推子 → biquad type/frequency/Q/gain 实测变化
#   - 自动前级数值：bands [6,0,...] → preamp -6
#   - AutoEq 解析：好文本 / 垃圾 / 超 10 段 / preamp 行
#   - 老用户回归：eq_mode 缺省 graphic + eq_gains 照旧（verify_np / verify_ui 不破坏）
python verify_np.py && python verify_ui.py
```

出包：每阶段 APK + 桌面 exe 测试包 + 包内容核验 + android-UA 冒烟（惯例，不许攒）。

## 分工与顺序

1. **后端 A 组契约 + H 组验证**（后端侧做，不碰 web/*——那是前端 WIP，提交时按文件归组）
2. **前端 B/C/D**（UI 优化收尾后动工）
3. **E 组 2b**（响度/徽标，B 组落地后）

## 落地记录

- 2026-11-13 后端 A 组：`DEFAULT_STORE` 五键 + `_clean_settings` 四分支（eq_mode 白名单 / eq_bands
  isfinite 硬墙 + 逐段校验 + 截 10 段 / eq_auto_preamp 只收 bool / eq_headphone 截 80）+
  `_SETTING_NUM` 增 `eq_preamp` 钳 [-12,12]；verify_audiophile 增 H 组 16 例，**54/54 全绿**
  （含老 eq_gains 键回归，详见 `audiophile_report.md`）。
