package com.elysium.player;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
import android.view.Window;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import androidx.core.content.FileProvider;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsControllerCompat;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import org.json.JSONObject;

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
    private static final int REQ_RECORD_AUDIO = 7102;
    private static WeakReference<MainActivity> ref = new WeakReference<>(null);
    private WebView web;
    /** 等系统权限对话框返回的麦克风请求（null = 没有在等）。 */
    private PermissionRequest pendingMic;

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

        /** 前端「去系统设置」：麦克风被拒后直接打开本应用的权限页。 */
        @JavascriptInterface
        public boolean openAppSettings() {
            try {
                Intent it = new Intent(
                        android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.parse("package:" + getPackageName()));
                it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(it);
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

        /* —— K 歌原生直通耳返（2026-11-14，见 KaraokeMonitor / KaraokeMonitorHub）——
           页面只在 Android + 本桥可用时启用；所有调用都返回 JSON 字符串，
           失败由页面回退 WebView 耳返。引擎只在前台 Activity 期间运行，
           onStop 会停，不声明麦克风 FGS。
           beta10：每个桥方法套 try/catch —— 桥线程抛异常会直接杀进程（闪退），
           一律转成 {ok:false,...} 并落面包屑（见 CrashLog），自检面板可读。 */
        @JavascriptInterface
        public String startKaraokeMonitor(String json) {
            CrashLog.log("bridge startKaraokeMonitor begin");
            try {
                String r = KaraokeMonitorHub.get().start(MainActivity.this, json);
                CrashLog.log("bridge startKaraokeMonitor ok");
                return r;
            } catch (Throwable t) {
                CrashLog.log("bridge startKaraokeMonitor throw\n" + Log.getStackTraceString(t));
                return errJson("bridge_throw", t);
            }
        }

        @JavascriptInterface
        public String updateKaraokeMonitor(String json) {
            try {
                return KaraokeMonitorHub.get().update(json);
            } catch (Throwable t) {
                CrashLog.log("bridge updateKaraokeMonitor throw\n" + Log.getStackTraceString(t));
                return errJson("bridge_throw", t);
            }
        }

        @JavascriptInterface
        public String karaokeMonitorStatus() {
            try {
                return KaraokeMonitorHub.get().status();
            } catch (Throwable t) {
                CrashLog.log("bridge karaokeMonitorStatus throw\n" + Log.getStackTraceString(t));
                return errJson("bridge_throw", t);
            }
        }

        @JavascriptInterface
        public void stopKaraokeMonitor() {
            CrashLog.log("bridge stopKaraokeMonitor begin");
            try {
                KaraokeMonitorHub.get().stop();
                CrashLog.log("bridge stopKaraokeMonitor ok");
            } catch (Throwable t) {
                CrashLog.log("bridge stopKaraokeMonitor throw\n" + Log.getStackTraceString(t));
            }
        }

        @JavascriptInterface
        public String startKaraokeMonitorDiag(String json) {
            CrashLog.log("bridge startKaraokeMonitorDiag begin");
            try {
                String r = KaraokeMonitorHub.get().startDiag(MainActivity.this, json);
                CrashLog.log("bridge startKaraokeMonitorDiag ok");
                return r;
            } catch (Throwable t) {
                CrashLog.log("bridge startKaraokeMonitorDiag throw\n" + Log.getStackTraceString(t));
                return errJson("bridge_throw", t);
            }
        }

        @JavascriptInterface
        public void stopKaraokeMonitorDiag() {
            try {
                KaraokeMonitorHub.get().stopDiag();
            } catch (Throwable t) {
                CrashLog.log("bridge stopKaraokeMonitorDiag throw\n" + Log.getStackTraceString(t));
            }
        }

        /** 自检面板读崩溃/桥面包屑尾部（beta10）。 */
        @JavascriptInterface
        public String crashLog() {
            return CrashLog.tail(12000);
        }

        /** 页面 → 原生：主题切换时同步系统栏图标明暗（浅色页配深色图标，
            否则 edge-to-edge 下白底白图标看不见）。深浅色以外的值一律按深色。 */
        @JavascriptInterface
        public void themeUpdate(String theme) {
            final boolean dark = !"light".equals(theme);
            runOnUiThread(() -> {
                Window w = getWindow();
                WindowInsetsControllerCompat c =
                        WindowCompat.getInsetsController(w, w.getDecorView());
                c.setAppearanceLightStatusBars(!dark);
                c.setAppearanceLightNavigationBars(!dark);
            });
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
        // 崩溃/桥面包屑：beta9「唱完闪退」无法定位（用户侧没有 adb），beta10 起
        // 证据落应用外部目录 crash-log.txt，自检面板经 crashLog() 桥读取。
        CrashLog.init(getApplicationContext());
        CrashLog.install();
        CrashLog.log("app create version=" + Build.VERSION.SDK_INT);
        // edge-to-edge：窗口画进状态栏/导航栏后面，页面用 env(safe-area-inset-*)
        // 自留安全区（index.html 的 viewport-fit=cover 与 style.css 的 padding
        // 早就按这个约定写好了，但原生侧一直没开 —— 手势条区域落在窗口外，
        // MIUI 给它铺系统白底，深色模式下播放条下面一条白断层，beta7 真机实测）。
        // 透明后露出来的是应用的深色页面，图标明暗随页面主题经 themeUpdate 同步。
        Window w = getWindow();
        WindowCompat.setDecorFitsSystemWindows(w, false);
        w.setStatusBarColor(Color.TRANSPARENT);   // API 35+ 弃用，但 29-34 仍生效
        w.setNavigationBarColor(Color.TRANSPARENT);
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
        // 本地服务端一直发 Cache-Control: no-store，但 WebView 的 HTTP 缓存以历史
        // 响应头为准：旧版本若发过可缓存的头，升级 APK 后旧条目照样按旧头的新鲜度
        // 命中，把旧版 UI 压在盘上（beta1 用户实测）。回环地址没有网络成本，
        // 索性每次都向服务端取最新资源。
        web.getSettings().setCacheMode(android.webkit.WebSettings.LOAD_NO_CACHE);
        /* K 歌麦克风：WebView 的 getUserMedia 必须先由宿主接管权限请求，再向系统申请
           RECORD_AUDIO。不接管时默认实现一律拒绝 —— 页面只能退到听唱模式（无耳返）。 */
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                boolean audio = false;
                for (String res : request.getResources()) {
                    if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(res)) {
                        audio = true;
                    }
                }
                if (!audio || !isLocalOrigin(request.getOrigin())) {
                    request.deny();
                    return;
                }
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                        == PackageManager.PERMISSION_GRANTED) {
                    request.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                    return;
                }
                if (pendingMic != null) {
                    try {
                        pendingMic.deny();
                    } catch (Exception ignored) {
                    }
                }
                pendingMic = request;
                try {
                    requestPermissions(
                            new String[]{Manifest.permission.RECORD_AUDIO}, REQ_RECORD_AUDIO);
                } catch (Exception e) {
                    pendingMic = null;
                    request.deny();
                }
            }

            @Override
            public void onPermissionRequestCanceled(PermissionRequest request) {
                if (pendingMic == request) {
                    pendingMic = null;
                }
            }
        });
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

    /** 桥方法 catch 到 Throwable 时的统一返回体；让页面走回退而不是跟着崩。 */
    private static String errJson(String code, Throwable t) {
        try {
            JSONObject o = new JSONObject();
            o.put("ok", false);
            o.put("error", code);
            o.put("detail", String.valueOf(t));
            return o.toString();
        } catch (Exception e) {
            return "{\"ok\":false,\"error\":\"" + code + "\"}";
        }
    }

    /** WebView 只加载 127.0.0.1（见 shouldOverrideUrlLoading）；权限也只给这个源。 */
    private static boolean isLocalOrigin(Uri u) {
        if (u == null) {
            return false;
        }
        String scheme = u.getScheme() == null ? "" : u.getScheme();
        String host = u.getHost() == null ? "" : u.getHost();
        return ("http".equals(scheme) || "https".equals(scheme))
                && (host.equals("127.0.0.1") || host.equals("localhost") || host.equals("::1"));
    }

    /**
     * 返回键 / 侧边返回手势：应用是 SPA，返回语义归页面管 ——
     * 先问 window.__elysiumBack()（关浮层 → 路由退一级，见 app.js），
     * 消化了就停手；已在根视图时播放中退后台保播放（音频在 WebView 里，
     * finish 会掐断），空闲才真正退出。enableOnBackInvokedCallback 未开
     * （targetSdk 34 默认关），系统 back 一律走这里。
     */
    @Override
    public void onBackPressed() {
        if (web == null) {
            super.onBackPressed();
            return;
        }
        try {
            web.evaluateJavascript(
                    "window.__elysiumBack ? window.__elysiumBack() : false",
                    value -> {
                        if ("true".equals(value)) {
                            return;
                        }
                        if (PlaybackService.lastPlaying) {
                            moveTaskToBack(true);
                        } else {
                            finish();
                        }
                    });
        } catch (Exception e) {
            // WebView 已销毁（页面没了）：没有可退的层级，按退出处理
            finish();
        }
    }

    /** 系统麦克风权限对话框回来：答应就放行页面里挂起的 getUserMedia，拒绝就明确告知。 */
    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions,
                                           int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != REQ_RECORD_AUDIO) {
            return;
        }
        PermissionRequest req = pendingMic;
        pendingMic = null;
        if (req == null) {
            return;
        }
        boolean ok = grantResults.length > 0
                && grantResults[0] == PackageManager.PERMISSION_GRANTED;
        try {
            if (ok) {
                req.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
            } else {
                req.deny();
            }
        } catch (Exception ignored) {
        }
    }

    @Override
    protected void onStop() {
        // 麦克风只能在前台 Activity 使用（Android 9+）；离前台立刻停直通，
        // 页面 visibilitychange 会在回到前台且仍在演唱时重新拉起。
        CrashLog.log("activity onStop begin");
        KaraokeMonitorHub.get().stop();
        KaraokeMonitorHub.get().stopDiag();
        CrashLog.log("activity onStop done");
        super.onStop();
    }

    @Override
    protected void onDestroy() {
        if (pendingMic != null) {
            try {
                pendingMic.deny();
            } catch (Exception ignored) {
            }
            pendingMic = null;
        }
        KaraokeMonitorHub.get().stop();
        KaraokeMonitorHub.get().stopDiag();
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
