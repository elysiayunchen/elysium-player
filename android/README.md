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

Python 源码从仓库根同步进来（打包内容 = v1.5.0 发布代码）：

```bash
mkdir -p app/src/main/python app/src/main/res/drawable
cp ../main.py ../importer.py app/src/main/python/
cp -r ../web ../providers app/src/main/python/
cp ../app.png app/src/main/res/drawable/appicon.png
```

- Python 目标 3.10，`pip install mutagen`（写标签用，纯 Python）由 Chaquopy 构建时自动完成
- `ndk.abiFilters`: arm64-v8a + armeabi-v7a
- debug 签名（`~/.android/debug.keystore` 自动生成）；换机器重建会换签名，升级需先卸载
- 出站走 HTTP 代理的环境，给 `gradle.properties` 加 `systemProp.https.proxyHost/Port`
  （JVM 不读 `http_proxy` 环境变量）
