package com.elysium.player;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import androidx.core.content.FileProvider;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.lang.ref.WeakReference;

/**
 * Elysium Android 壳：Chaquopy 后台线程跑 Python 本地服务（main.serve_android），
 * WebView 加载 http://127.0.0.1:18810/。外链（发布页等）交给系统浏览器，
 * 127.0.0.1 之外的地址一律不进 WebView —— 应用是单页 SPA，没有内部导航历史。
 *
 * 媒体控制链路：页面 mediaUpdate() → PlaybackService（前台服务 + 通知栏）；
 * 通知 / 线控 / 锁屏控件 → dispatchMedia(cmd) → 页面 window.__elysiumMedia。
 * 播放的唯一真相源是页面 <audio>，原生侧只做中转，不碰音频流。
 */
public class MainActivity extends Activity {

    private static final int PORT = 18810;
    private static WeakReference<MainActivity> ref = new WeakReference<>(null);
    private WebView web;

    /** 让 PlaybackService / 通知按钮把命令打回页面（任意线程可调）。 */
    static void dispatchMedia(String cmd) {
        final MainActivity act = ref.get();
        if (act == null || act.web == null) {
            return;
        }
        final String c = cmd == null ? "" : cmd.replace("'", "");
        act.runOnUiThread(() -> act.web.evaluateJavascript(
                "window.__elysiumMedia && window.__elysiumMedia('"
                        + (c.startsWith("seek:") ? "seek','" + c.substring(5) : c) + "');",
                null));
    }

    /** 前端通过 window.ElysiumAndroid 调用：外链、应用内更新、媒体状态推送。 */
    private class AppBridge {

        @JavascriptInterface
        public boolean openUrl(String url) {
            String u = url == null ? "" : url;
            if (!u.startsWith("http://") && !u.startsWith("https://")) {
                return false;
            }
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(u)));
                return true;
            } catch (Exception e) {
                return false;
            }
        }

        /** 前端「安装更新」：把下载好的 APK 经 FileProvider 交给系统安装器。 */
        @JavascriptInterface
        public boolean installUpdate(String path) {
            try {
                File f = new File(path == null ? "" : path);
                if (!f.isFile()) {
                    return false;
                }
                Uri uri = FileProvider.getUriForFile(
                        MainActivity.this, "com.elysium.player.provider", f);
                Intent it = new Intent(Intent.ACTION_VIEW);
                it.setDataAndType(uri, "application/vnd.android.package-archive");
                it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION);
                startActivity(it);
                return true;
            } catch (Exception e) {
                return false;
            }
        }

        /** 页面 → 原生：播放状态 JSON（换曲 / 播放暂停 / 拖动，约 3s 节流）。 */
        @JavascriptInterface
        public void mediaUpdate(String json) {
            ensureNotifPermission();
            PlaybackService.push(MainActivity.this, json);
        }
    }

    /** 通知栏控制需要 POST_NOTIFICATIONS（API 33+ 运行时权限）；拒绝也只是没通知，不拦播放。 */
    private void ensureNotifPermission() {
        if (Build.VERSION.SDK_INT < 33) {
            return;
        }
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                == PackageManager.PERMISSION_GRANTED) {
            return;
        }
        try {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 7101);
        } catch (Exception ignored) {
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        ref = new WeakReference<>(this);
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(getApplicationContext()));
        }
        // serve_android 会阻塞（服务循环），必须放后台线程
        new Thread(() ->
                Python.getInstance().getModule("main").callAttr("serve_android")).start();

        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#08080b"));
        web.getSettings().setJavaScriptEnabled(true);
        web.getSettings().setDomStorageEnabled(true);
        web.getSettings().setMediaPlaybackRequiresUserGesture(false);
        // 只加载 127.0.0.1 的本地页面（见 shouldOverrideUrlLoading），桥不会暴露给外站
        web.addJavascriptInterface(new AppBridge(), "ElysiumAndroid");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri u = request.getUrl();
                String host = u.getHost() == null ? "" : u.getHost();
                if (host.equals("127.0.0.1") || host.equals("localhost") || host.equals("::1")) {
                    return false;
                }
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, u));
                } catch (Exception ignored) {
                }
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                // window-chrome.js 的宿主注入约定：ELYSIUM_PLATFORM 优于 UA 探测
                view.evaluateJavascript("window.ELYSIUM_PLATFORM='android';", null);
            }
        });
        setContentView(web);
        waitAndLoad();
    }

    @Override
    protected void onDestroy() {
        // 壳退出：撤掉前台服务与通知，别留一个点不动的控制面板。
        // 旋转等配置变化走 configChanges 不重建；真重建（isFinishing=false）时保留服务。
        if (isFinishing()) {
            PlaybackService.hide(this);
        }
        if (ref.get() == this) {
            ref = new WeakReference<>(null);
        }
        super.onDestroy();
    }

    /** 轮询本地端口，服务起来才 loadUrl，避免白屏 404。 */
    private void waitAndLoad() {
        final String url = "http://127.0.0.1:" + PORT + "/";
        new Thread(() -> {
            for (int i = 0; i < 150; i++) {
                try (java.net.Socket s = new java.net.Socket("127.0.0.1", PORT)) {
                    runOnUiThread(() -> web.loadUrl(url));
                    return;
                } catch (Exception e) {
                    try {
                        Thread.sleep(200);
                    } catch (InterruptedException ie) {
                        return;
                    }
                }
            }
        }).start();
    }
}
