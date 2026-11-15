# Elysium Android（Chaquopy）

Android 壳工程：[Chaquopy](https://chaquo.com/chaquopy/) 在应用内跑 Python 后端
（`main.serve_android`，监听 `127.0.0.1:18810`），原生 `MainActivity` 用 WebView 加载，
外链（发布页等）自动交给系统浏览器。桌面端 `main.py` 一行不改复用。

## 构建

要求：JDK 17+、Android SDK（platform 34 / build-tools 34）、Gradle 8.4+；
K 歌原生耳返需要 **NDK 26.1.10909125 + CMake 3.22.1**（`ndkVersion` /
`externalNativeBuild` 已写进 `app/build.gradle`）。

```bash
export ANDROID_HOME=$HOME/android          # 指向含 platforms;android-34 的 SDK
gradle assembleDebug                       # 产物 app/build/outputs/apk/debug/app-debug.apk
```

Python 源码从仓库根同步进来（打包内容 = 当前发布代码）：

```bash
mkdir -p app/src/main/python app/src/main/res/drawable
cp ../main.py ../importer.py app/src/main/python/
cp -r ../web ../providers app/src/main/python/
cp ../app.png app/src/main/res/drawable/appicon.png
```

- Python 目标 3.10，`pip install mutagen`（写标签用，纯 Python）由 Chaquopy 构建时自动完成
- `ndk.abiFilters`: arm64-v8a + armeabi-v7a
- **固定签名**：`elysium.keystore`（alias `elysium`，密码 `elysium2026`）随仓库走，debug / release
  同一把钥匙 —— 应用内更新靠覆盖安装，签名必须跨构建一致。仓库公开，密钥也就公开，
  这是个人侧载应用的取舍（签名连续性 > 密钥保密性）；换钥 = 用户全体卸载重装，别轻易动
- **应用内更新**：下载好的 APK 在应用私有目录（`files/Elysium/`），前端经
  `window.ElysiumAndroid.installUpdate(path)`（JavascriptInterface + FileProvider）调起
  系统安装器；Android 8+ 首次会引导授予「安装未知应用」权限
- **后台播放**（`PlaybackService`，v1.6.0 起）：前台服务 + MediaStyle 通知保住锁屏 /
  切后台的播放，通知按钮 / 耳机线控 / 锁屏控件经 `MainActivity.dispatchMedia(cmd)` 打回
  页面的 `window.__elysiumMedia(cmd)`。数据流单向：页面在换曲、播放暂停、seek 时把状态
  JSON 经 `ElysiumAndroid.mediaUpdate(json)` 推给服务，`<audio>` 始终是播放唯一真相源。
  服务还管音频焦点（被抢暂停、瞬时丢失后续播）、拔耳机暂停（`AUDIO_BECOMING_NOISY`）、
  播放期 PARTIAL_WAKE_LOCK；API 33+ 通知权限运行时申请，拒绝只是没有通知、不影响播放
- 出站走 HTTP 代理的环境，给 `gradle.properties` 加 `systemProp.https.proxyHost/Port`
  （JVM 不读 `http_proxy` 环境变量）
- **Oboe 原生耳返**：`libelysium_oboe.so` 由 CMake 编译（`app/src/main/cpp`），依赖
  `com.google.oboe:oboe:1.9.3`（Google Maven prefab）；`buildFeatures.prefab true` +
  `-DANDROID_STL=c++_shared`。改 C++ 后 `assembleRelease` 会同时编
  arm64-v8a / armeabi-v7a，并打进 `liboboe.so` / `libc++_shared.so`。

## K 歌原生直通耳返（v2.3.0-beta9）

WebView 的 `getUserMedia` 采集栈在 Android 上约 110ms 隐藏缓冲，加 64ms 输出缓冲，
beta7 真机实测耳返 RTT 179ms。beta8 的纯 Java `AudioRecord → AudioTrack` 在真机上
遇到 minBuffer 天花板（采集 200ms / 输出 128ms，自检 217.7ms 是满缓冲上界，
接耳机同样有可感延迟）。beta9 起干声耳返改走 **Oboe 1.9.3**：
输入 / 输出各一条 `LowLatency/Shared` 流 + SPSC 环形缓冲（`cpp/oboe_karaoke.cpp`），
输入回调做干声 DSP 入环，输出回调出环；录音 / 混响 / 音准 / 伴奏仍由 WebAudio 负责
（**双采集**）。

- Oboe 1.9.3 的 `Direction` 没有 Duplex，所以是 input + output 两条流；环容量
  ≥4096 帧（约 85ms），预填 2×输出 burst（约 8ms），正常占用≈预填量级。采集 preset
  优先级 `VoicePerformance`（API 29+，AAudio 的 K 歌专用 preset）→ `Unprocessed` →
  `VoiceRecognition` → `Generic`。
- **共享模式**，不抢 Exclusive（WebView 伴奏还要混音）；不申请音频焦点、不建
  MediaSession；仅前台 Activity 期间运行（`onStop` 停，页面 `visibilitychange` 回来再拉起）。
- **不做人声补偿**：伴奏输出延迟对「人声 vs 伴奏」是共模，相对偏移只由采集缓冲决定；
  加补偿或延迟整轨只会让偏移更大。
- **三层回退**：`KaraokeMonitorHub` Oboe → beta8 Java 引擎 → WebView 耳返；页面
  `status.engine` 显示 `oboe`/`java`，Oboe 失败原因进 `oboeError` + logcat，失败不把
  K 歌搞哑。
- 页面经 `window.ElysiumAndroid` 的五个方法控制（契约不变）：`startKaraokeMonitor(json)` /
  `updateKaraokeMonitor(json)` / `karaokeMonitorStatus()` / `stopKaraokeMonitor()` /
  `startKaraokeMonitorDiag(json)`。设置键 `karaoke_native_mon`（bool，默认开）。
- 调音台「原生直通」显示引擎 / 采集输出缓冲；设置→网络自检「原生耳返自检」外放
  8 发 1kHz/60ms 脉冲，输入/输出回调各自打时间戳，回报 RTT +
  `inputLatencyMs`/`outputLatencyMs`/`xruns`。
- 真机验证建议有线耳机（蓝牙 A2DP 延迟不可控），自检时拔耳机、媒体音量最大、
  环境安静；目标 **30–60ms**，gate ≤90ms。长唱 3–5 分钟观察 `ringMs` 与
  `underruns/overruns` 是否漂移。
