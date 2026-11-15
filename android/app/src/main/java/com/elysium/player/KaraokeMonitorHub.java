package com.elysium.player;

import android.content.Context;
import android.util.Log;

import org.json.JSONObject;

/**
 * 原生直通的三级路由（beta9）：
 *
 *   1. Oboe/AAudio 全双工（首选，低延迟共享通路）；
 *   2. 旧 Java AudioRecord→AudioTrack（Oboe 库缺失/流打开失败时）；
 *   3. WebView 耳返（页面 watchdog：running=false / 双向 RMS 异常时回退）。
 *
 * 对 MainActivity/网页保持原 KaraokeMonitor 的五个方法 + JSON 契约不变，
 * 只在 status 里多给 engine/api/oboeError，方便页面显示当前走哪一级。
 */
public final class KaraokeMonitorHub {

    private static final String TAG = "ElysiumKaraoke";
    private static final KaraokeMonitorHub INSTANCE = new KaraokeMonitorHub();

    public static KaraokeMonitorHub get() {
        return INSTANCE;
    }

    private final OboeKaraokeMonitor oboe = OboeKaraokeMonitor.get();
    private final KaraokeMonitor legacy = KaraokeMonitor.get();

    private volatile boolean useOboe;
    private volatile boolean diagOboe;
    private volatile String oboeError = "";

    private KaraokeMonitorHub() {}

    public String start(Context ctx, String json) {
        CrashLog.log("hub.start begin");
        // 先把可能残留的旧引擎停掉；Oboe start 自己也会停旧流。
        legacy.stop();

        if (oboe.isAvailable()) {
            String r = oboe.start(ctx, json);
            if (isOk(r)) {
                useOboe = true;
                diagOboe = false;
                oboeError = "";
                CrashLog.log("hub.start oboe ok");
                return r;
            }
            oboeError = detailOf(r);
            CrashLog.log("hub.start oboe failed: " + r);
            Log.w(TAG, "Oboe start failed, fallback to Java engine: " + r);
        } else {
            oboeError = "oboe library unavailable";
            CrashLog.log("hub.start oboe unavailable");
        }

        useOboe = false;
        String r2 = legacy.start(ctx, json);
        CrashLog.log("hub.start legacy done ok=" + isOk(r2));
        return withOboeError(r2);
    }

    public String update(String json) {
        return useOboe ? oboe.update(json) : legacy.update(json);
    }

    public String status() {
        if (useOboe || diagOboe) return oboe.status();
        return withOboeError(legacy.status());
    }

    public void stop() {
        // beta9「唱完闪退」发生在收口路径：每步落面包屑，崩在哪一步一目了然。
        CrashLog.log("hub.stop begin");
        oboe.stop();
        CrashLog.log("hub.stop oboe done");
        legacy.stop();
        CrashLog.log("hub.stop legacy done");
        useOboe = false;
        diagOboe = false;
        CrashLog.log("hub.stop done");
    }

    public String startDiag(Context ctx, String json) {
        // 自检不能在 monitor 运行时开；Oboe/旧引擎都先停。
        stop();
        diagOboe = false;

        if (oboe.isAvailable()) {
            String r = oboe.startDiag(ctx, json);
            if (isOk(r)) {
                diagOboe = true;
                CrashLog.log("hub.startDiag oboe ok");
                return r;
            }
            oboeError = detailOf(r);
            CrashLog.log("hub.startDiag oboe failed: " + r);
            Log.w(TAG, "Oboe diag failed, fallback to Java diag: " + r);
        }
        String r = legacy.startDiag(ctx, json);
        CrashLog.log("hub.startDiag legacy ok=" + isOk(r));
        return r;
    }

    public void stopDiag() {
        oboe.stopDiag();
        legacy.stopDiag();
        diagOboe = false;
    }

    private static boolean isOk(String r) {
        try {
            return new JSONObject(r == null ? "{}" : r).optBoolean("ok", false);
        } catch (Exception e) {
            return false;
        }
    }

    private static String detailOf(String r) {
        try {
            JSONObject o = new JSONObject(r == null ? "{}" : r);
            String e = o.optString("error", "");
            String d = o.optString("detail", "");
            return (e + (d.isEmpty() ? "" : ": " + d)).trim();
        } catch (Exception e) {
            return r == null ? "" : r;
        }
    }

    /** 旧引擎 status 里带 engine=java；这里再补上 Oboe 失败原因，页面可显示/排查。 */
    private String withOboeError(String r) {
        if (oboeError == null || oboeError.isEmpty()) return r;
        try {
            JSONObject o = new JSONObject(r == null ? "{}" : r);
            o.put("oboeError", oboeError);
            return o.toString();
        } catch (Exception e) {
            return r;
        }
    }
}
