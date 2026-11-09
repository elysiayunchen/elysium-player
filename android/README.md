# Elysium Android（Chaquopy）

Android 壳工程：[Chaquopy](https://chaquo.com/chaquopy/) 在应用内跑 Python 后端
（`main.serve_android`，监听 `127.0.0.1:18810`），原生 `MainActivity` 用 WebView 加载，
外链（发布页等）自动交给系统浏览器。桌面端 `main.py` 一行不改复用。

## 构建

要求：JDK 17+、Android SDK（platform 34 / build-tools 34）、Gradle 8.4+。

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
