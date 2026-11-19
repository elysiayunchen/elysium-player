# 上游站点池（P1+P2）—— 2026-11-18

> 起因：2026-11-17/18 两轮上游实测（`_tmp_upstream_probe/` 七轮探针）确认主站
> `flac.music.hi.cn` 是社区单机（阿里云单 IP、无 CF、无备援），gd 备用站只覆盖
> 网易且全代码仅两处接了兜底；「周杰伦」被主站艺人级过滤后 kuwo 无任何兜底
> （wyy 靠 gd 兜住，kuwo 直接空手）。本批把「单一上游」改成「站点池」：
> 池内自动故障转移 + 网络级熔断 + 健康巡检 + 路由表 manifest 热更新，
> 外加平台公开搜索接口直连层当末位兜底。P3（BYOA 用户 cookie）留待下批。

## 一、落地内容

### S1 站点池骨架（main.py 上游区重写）
- `STATIONS_DEFAULT`：有序四站 `hi → gd → dne（网易直连）→ dkw（酷我直连）`。
  池可被两处覆盖：环境变量 `ELYSIUM_STATIONS`（整表替换，测试/诊断逃生口）>
  providers/manifest.json `stations`（仅 kind:"hi" 镜像，替换 hi 层）。
- 错误分类：`UpstreamNetError`（连接/DNS/超时/HTTP 非 200 → 计熔断连败）与
  `UpstreamBizError`（code!=0 / 非 JSON → 站点健康，不参与熔断）。
  `str(e)` 文案不变，`_refresh_sign` / `DownloadManager._resolve` 的「过期」
  字符串判断不受影响。
- 熔断：连败 `POOL_TRIP_AFTER`(默认2) 次即跳过该站 `POOL_COOLDOWN`(默认300s)，
  到期半开放行试探，成功即复位。`upstream()` 在同一次调用内按池序自动换站；
  `upstream_keepalive()`（歌手页深翻页）同款。连接池与线程本地长连接按 host
  分桶，换站后旧连接自然废弃。
- 条目站标：`upstream()` 内部换站后经 `_served_by` 传递事实站点，
  `search_any` 据此打 `station` 字段（旧版硬编码 "hi"）。

### S2 直连搜索层（P2）
- `_direct_netease_search`：`/api/search/get/web`（平台网页在用的公开端点，
  免登录、支持深翻页，实测 ~270ms）；`_direct_kuwo_search`：`search.kuwo.cn/r.s`
  老接口（单引号 JSON，`ast` 解析，`MUSICRID` 剥 `MUSIC_` 前缀，`&nbsp;` unescape）。
- 只搜不放：条目无 time/sign，直链解析走既有 `_resolve_play_url` 重搜换签 /
  gd 直链老路（gd 直链不需要主站签名，直连层搜到的网易条目可正常播）。
- 直连层 60s 内存缓存（复用 `cached()`），防深翻页打疼平台。
- `search_any` 重写为逐层遍历：hi → gd（仅 wyy）→ 直连层（按 platform 各自
  可服务），某层给到非空即收；熔断中的站跳过。「周杰伦」kuwo 侧从直连层兜住。

### S3 健康巡检 + 路由表热应用
- `pool_tracker` 守护线程（serve()/serve_android 共用，与 update_tracker 并肩）：
  启动 8s 后拉一次远端路由表 + 逐站探测，之后每 600s 一轮。
- `pool_probe_once`：逐站搜一个词（hi 站走 `_up_call` 绕开转移逻辑才能探出
  各站死活）；`pool_refresh_once`：沿 manifest 的 update_url+mirrors 链取远端
  `stations`，严格校验（≤schema、https、回环 stub 例外、仅 kind:"hi"）后
  **只在内存整表替换 hi 层**——主站死了改仓库 manifest 一行 JSON + purge 即生效，
  不用发版。gd/direct 层恒为代码内置，远端不可触碰。
- notify-only 边界：规则与应用更新仍只比对版本号绝不替换（verify_import 的
  providers 指纹测试原样通过）；**唯一例外**是站点路由表——主站死了靠提示
  让用户自救不成立，且路由表是非可执行配置。

### S4 状态出口
- RPC 新增 `stations`（池快照 + 活动站 + 远端应用记录）、`pool_refresh`、
  `pool_probe`（手动触发，诊断/验收用）。
- `/api/diag` 六跳 → **七跳**：新增「4/7 站点池」（快照 + 活动 + 熔断中名单 +
  远端路由表应用时间）；verify_diag 断言同步 6→7。

### S5 杂项
- 删除死常量 `API_URL`（全仓无引用）；`STATION_NAME` 并入池 name 字段。
- UA Chrome/126 → 133（2026-11-17 报告指出的弱指纹老化）。
- `_valid_hi_entry` 允许回环 http（127.0.0.1/localhost）：测试与诊断 stub 专用，
  远端条目仍强制 https。

## 二、查证后排除（记录防复读）
- gd 支持 kuwo 源？——实测 url/search 均 400（musicdl 源码里那条线也已失效），
  不硬凑；kuwo 侧兜底只能靠直连搜索层。
- 酷我官方 mobi 通道当播放直链？——实测非 VIP 歌返回 mgg 加密无损、VIP 歌只给
  6kbps AAC 试听（`_tmp_upstream_probe/probe8-10`），解密属最高风险档，本批不做。
- 远端路由表走 notify-only？——驳回：主站死了靠提示让用户自救不成立，
  见 S3 的例外说明与安全护栏（schema 校验 + 仅 hi 镜像 + 内存应用）。

## 三、本轮未做（超范围 / 待决定）
- P3 BYOA：网易 cookie 直链（复用 kugou enhance 模式）——下批。
- UI 站点池状态面板——RPC 数据已备好（`stations`），前端下批。
- 熔断参数暴露到设置页——先用环境变量（`ELYSIUM_POOL_TRIP` / `ELYSIUM_POOL_COOLDOWN`）。

## 四、回归

| 套件 | 结果 |
| --- | --- |
| verify_upstream_pool（新） | 16/16 通过（全 stub 离线，CI core job 已收） |
| verify_import（notify-only 指纹） | 41/41 通过 |
| verify_diag（七跳断言已同步） | 10/10 通过（含真网七跳，4/7 站点池跳 OK） |
| verify_gapless | 16/16 通过 |
| check_release | 通过（工作区 + 全历史扫描） |
| 真网烟测（_tmp_upstream_probe/smoke_pool.py） | 「晴天」双平台走主站；「周杰伦」kuwo 从酷我直连层出 20 条、wyy 从 gd 出 20 条；死站演练：同请求内熔断 + 直连接管，第二次请求零等待 |

## 五、改动面
- `main.py`：上游区重写（站点池/熔断/直连层/巡检），search_any、run_diag、
  RPC_TABLE、serve() 线程；+约 330 行。
- `importer.py`：DEFAULT_MANIFEST 白名单 +`stations` 键；+4 行。
- `providers/manifest.json`：+`stations: []` 与 `_note` 更新。
- `verify_upstream_pool.py`：新增验收套件（本地 stub 伪装三类上游 + 控制
  端点，16 断言）。
- `verify_diag.py`：六跳 → 七跳断言同步（HTTP 断言、UI 行数断言、探针等待
  阈值三处）。
- `_tmp_upstream_probe/`：实测探针 probe1-9 与真网烟测 smoke_pool.py（未入库）。
- `.github/workflows/verify.yml`：core job 收录 verify_upstream_pool。
- `README.md`：功能/搜索加站点池条目；声明与边界补路由表说明一句。
- 打包面不变（无新运行时依赖，`ast`/`html` 均标准库）。
