package com.elysium.player;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import androidx.core.content.FileProvider;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;

/**
 * Elysium Android 壳：Chaquopy 后台线程跑 Python 本地服务（main.serve_android），
 * WebView 加载 http://127.0.0.1:18810/。外链（发布页等）交给系统浏览器，
 * 127.0.0.1 之外的地址一律不进 WebView —— 应用是单页 SPA，没有内部导航历史。
 */
public class MainActivity extends Activity {

    private static final int PORT = 18810;
    private WebView web;

    /** 前端通过 window.ElysiumAndroid 调用：外链走系统浏览器、应用内更新调起安装器。 */
    private class UpdateBridge {

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
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
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
        // 只加载 127.0.0.1 的本地页面（见 shouldOverrideUrlLoading），桥不会暴露给外站
        web.addJavascriptInterface(new UpdateBridge(), "ElysiumAndroid");
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
