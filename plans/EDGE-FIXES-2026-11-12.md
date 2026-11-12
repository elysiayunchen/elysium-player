# 边缘测试问题清单（2026-11-12）

来源：`verify_edge.py`（91 例，74 PASS / 8 FAIL / 8 WARN / 1 SKIP），完整证据见 `edge_report.md`。
本文件记录待修项与修法；修完在每条后面标 `[fixed]` 并注明验证结果。

## FAIL（真实缺陷）

| ID | 优先级 | 问题 | 位置 | 修法 |
| --- | --- | --- | --- | --- |
| E-01 | P0 | `data.json` 为 JSON 字符串（非 dict 的 truthy 值）时启动即崩，应用打不开 | `main.py:253` `(raw or {}).items()` | 解析后先 `isinstance(raw, dict)`，否则回落默认值并把原文件另存 `.bak` |
| E-02 | P1 | `parse_text` 单行超长触发正则病态回溯：20KB=1.3s / 100KB=33s / 1MB>120s，导入预览永久卡住一个 worker | `importer.py:286` `(.+?\.(?:mp3\|…))\b` | 单行超过 2048 字符直接跳过（正常条目 < 300，Windows 路径上限 260） |
| E-03 | P1 | 脏 settings（`cache_limit_mb="abc"` / `cache_min_plays=null`）让 `/api/cache` 抛未捕获 ValueError，连接直接断 | `main.py:3852-3854` | 抽 `_int_setting()` 做类型兜底；写入侧同时钳制范围 |
| E-04 | P2 | Range 数字 5000 位触发 `int()` 4300 位限制，连接无响应 | `main.py:3600` | 转 int 前限制位数（>18 位按越界处理） |
| E-05 | P2 | 空 body / 非法 JSON / 二进制被当成功处理：回 `ok:true` 并写盘一次 | `main.py:3913`、`Store.apply` 末尾 | `_json_body` 保证返回 dict；`/api/store` 校验 op 缺失/未知返回 400 |
| E-06 | P2 | m3u8 每首歌解析成两条，其中一条名字带目录（`/xx/晴天`） | `importer.py:262-290` | `#EXTINF` 只作待配对元数据，与下一行文件路径合并；剥目录前缀 |
| E-07 | P3 | IPv6 回环 Host `[::1]:port` 被 403 | `main.py:3552` | 先剥 `[...]` 再剥端口 |
| E-08 | P3 | E-01 的写入侧连带项 | 同 E-01 | 同 E-01 |

## WARN（顺带修，低风险）

| ID | 问题 | 位置 | 修法 |
| --- | --- | --- | --- |
| W-01 | 静态资源 `startswith(webroot)` 前缀匹配，可读 `webX/` 兄弟目录 | `main.py:3680` | 改成 `target == webroot or target.startswith(webroot + os.sep)` |
| W-02 | 客户端 RST 时 `_send` 抛 `ConnectionResetError`，一次并发测试刷 100 条 traceback | `main.py:3557` `_send` | 写响应包 try，捕获连接类异常 |
| W-03 | 5000 位版本号触发 `int()` 限制 | `main.py:3249` `_ver_tuple` | 数字段超过 9 位截断 |
| W-04 | Windows 保留名（`CON`/`PRN`/`AUX`/`NUL`）未过滤，写盘会失败 | `main.py:1746` `safe_name` | 命中保留名前缀加 `_` |
| W-05 | `sync_engine.diff` 假设远端是 dict，脏包抛 AttributeError（靠上层兜底） | `sync_engine.py:306` | 非 dict 抛 `DavError`，给出可读提示 |
| W-06 | 设置无范围校验（`cache_limit_mb=-5`、`volume=99` 照收） | `main.py:297` `apply("settings")` | 数值键白名单 + 类型转换 + 钳制 |

## 暂不修（记录在案）

- **junction 目录被扫进曲库列表**：`local_scan` 会遍历 junction，列表能看到目录外文件（播放仍被 `local_resolve` 的 realpath 拦住）。风险仅限本机信息暴露，改动要动扫描热路径，先留着。
- **无 JSON body 上限**：只对 `/api/store` 加 4MB 上限；`/api/works/upload` 需要 200MB raw body，不做全局限制。
- **损坏 data.json 的 `.bak` 只保留一份**：够用，不做版本化。

## 验收

```bash
python verify_edge.py          # 91 例，目标 FAIL=0
python verify_import.py        # 单元层 U1~U8，确认 m3u8 改动没有回退
```

## 修复结果（2026-11-12 21:48）

| ID | 状态 | 实际改动 |
| --- | --- | --- |
| E-01 | fixed | `Store._load` 解析后校验 `isinstance(raw, dict)`，非 dict 走 `_quarantine()` 备份 `.bak` 再回落默认值；顺手把读取编码改成 `utf-8-sig` |
| E-02 | fixed | `parse_text` 加 `_MAX_LINE=2048`，超长行直接跳过（CSV / 逐行两条路径都加） |
| E-03 | fixed | 新增 `_int_setting()`，`/api/cache` 的四个整数项与 `plays` 遍历都改走它；`plays` 项加 dict 校验 |
| E-04 | fixed | 新增 `_range_num()`，>18 位按越界处理；`_serve_cached` 三处 `int()` 全替换 |
| E-05 | fixed | `_json_body` 保证返回 dict（非对象/非法长度一律 `{}`）；`/api/store` 校验 `op in STORE_OPS`，缺失/未知回 400；`Store.apply` 内部同样拒绝未知 op；单次 body 限 4MB |
| E-06 | fixed | `parse_text` 重写逐行循环：`#EXTINF` 只作待配对元数据，与下一行文件路径合成一条；新增 `_path_stem` / `_looks_path` / `_split_stem`，剥目录只对「像路径」的行生效（`AC/DC` 这类歌名不被砍） |
| E-07 | fixed | `_local_request` 先剥 `[...]` 再剥端口 |
| E-08 | fixed | 同 E-01 |
| W-01 | fixed | `_static` 改成 `target == webroot or target.startswith(webroot + os.sep)` |
| W-02 | fixed | `_send` 包 try，吞掉 BrokenPipe / ConnectionReset / OSError |
| W-03 | fixed | `_ver_tuple` 数字段截到 9 位 |
| W-04 | fixed | `safe_name` 命中 Windows 保留名（CON/PRN/AUX/NUL/COM1-9/LPT1-9）加 `_` 前缀 |
| W-05 | fixed | `sync_engine.diff` 非 dict 抛 `DavError`；新增 `_dicts()` 过滤混进列表的 null/字符串，`_fav_key` / `_item_key` 加 isinstance 兜底 |
| W-06 | fixed | 新增 `_clean_settings()`：数值键转 int 并钳制、`local_dirs` 必须数组、`eq_gains` 截 10 项、非 dict payload 拒绝 |

**额外发现（W-06 验证时）**：`data.json` 带 UTF-8 BOM 时 `json.load(encoding="utf-8")` 会抛
`Unexpected UTF-8 BOM`，整份数据被当损坏回落默认值 —— 收藏会静默消失。已改成 `utf-8-sig`，
并在 `verify_edge.py` 加了「BOM 文件内容被保留」断言。

**验收结果**

- `python verify_edge.py` → 92 例：**91 PASS / 0 FAIL / 1 WARN**（仅剩 junction 扫描那条，按计划不修）。
  用例数从 91 变 92，因为 F 组新增了 BOM 内容保留断言。
- `python verify_import.py --no-ui` → **31/31 通过**（U1~U13、A1~A18）。
