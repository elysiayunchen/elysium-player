package com.elysium.player;

import android.content.Context;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.os.Build;
import android.os.SystemClock;
import android.util.Log;

import org.json.JSONObject;

import java.io.File;

/**
 * Oboe（AAudio）全双工干声耳返桥（beta9）。
 *
 * native 引擎见 src/main/cpp/oboe_karaoke.cpp：Duplex + LowLatency + Shared，
 * 只做 mic → HPF/gain/tone/limiter → output；录音/混响/音准/伴奏仍在 WebView。
 *
 * 这层只负责：
 * - 加载 libelysium_oboe.so，失败时 isAvailable()=false，由 Hub 回退旧 Java 引擎；
 * - 把 gain/toneDb/muted 合并后传给 native（update 只改 patch 里的字段）；
 * - 给 status JSON 补 route（原生层不碰 Android Java 路由 API）。
 */
public final class OboeKaraokeMonitor {

    private static final String TAG = "ElysiumOboeMon";
    private static final boolean LIB_OK;

    static {
        boolean ok = false;
        try {
            System.loadLibrary("elysium_oboe");
            ok = true;
        } catch (Throwable t) {
            Log.w(TAG, "native library not loaded", t);
        }
        LIB_OK = ok;
    }

    private static volatile OboeKaraokeMonitor instance;

    public static OboeKaraokeMonitor get() {
        OboeKaraokeMonitor m = instance;
        if (m == null) {
            synchronized (OboeKaraokeMonitor.class) {
                m = instance;
                if (m == null) {
                    m = new OboeKaraokeMonitor();
                    instance = m;
                }
            }
        }
        return m;
    }

    private volatile Context appCtx;
    private volatile String cachedRoute = "unknown";
    private volatile long routeAt;

    // update 是 patch 语义：只改 JSON 里出现的字段，其余保持。
    private volatile double lastGain = 1.0;
    private volatile double lastToneDb = 0.0;
    private volatile boolean lastMuted = false;

    private OboeKaraokeMonitor() {}

    public boolean isAvailable() {
        return LIB_OK;
    }

    /** 原生面包屑落盘路径（应用外部目录），闪退时按最后一条定位到具体流操作；fail-open。 */
    public void setLogDir(Context ctx) {
        if (!LIB_OK || ctx == null) return;
        try {
            File d = ctx.getExternalFilesDir(null);
            if (d != null) {
                nativeSetLogPath(new File(d, "native-log.txt").getAbsolutePath());
            }
        } catch (Throwable ignored) {
        }
    }

    /** 启动干声直通；失败返回 {ok:false,error,detail}，由 Hub 回退旧引擎。 */
    public String start(Context ctx, String json) {
        if (!LIB_OK) return err("oboe_unavailable", "native library not loaded");
        if (ctx != null) appCtx = ctx.getApplicationContext();
        setLogDir(ctx);
        JSONObject cfg = parse(json);
        lastGain = clamp(cfg.optDouble("gain", lastGain), 0, 4);
        lastToneDb = clamp(cfg.optDouble("toneDb", lastToneDb), -12, 12);
        lastMuted = cfg.optBoolean("muted", lastMuted);
        int r = nativeStart(lastGain, lastToneDb, lastMuted);
        if (r != 0) return err("oboe_start", nativeLastError());
        return status();
    }

    /** 运行中改参数：未出现的字段沿用上次值。 */
    public String update(String json) {
        if (!LIB_OK) return err("oboe_unavailable", "native library not loaded");
        JSONObject cfg = parse(json);
        if (cfg.has("gain")) lastGain = clamp(cfg.optDouble("gain", lastGain), 0, 4);
        if (cfg.has("toneDb")) lastToneDb = clamp(cfg.optDouble("toneDb", lastToneDb), -12, 12);
        if (cfg.has("muted")) lastMuted = cfg.optBoolean("muted", lastMuted);
        nativeSetParams(lastGain, lastToneDb, lastMuted);
        return "{\"ok\":true}";
    }

    public String status() {
        if (!LIB_OK) return err("oboe_unavailable", "native library not loaded");
        try {
            JSONObject o = new JSONObject(nativeStatusJson());
            String route = detectRoute(appCtx);
            o.put("route", route);
            JSONObject d = o.optJSONObject("diag");
            if (d != null) d.put("route", route);
            return o.toString();
        } catch (Exception e) {
            return err("oboe_status", String.valueOf(e.getMessage()));
        }
    }

    public void stop() {
        if (LIB_OK) nativeStop();
    }

    public String startDiag(Context ctx, String json) {
        if (!LIB_OK) return err("oboe_unavailable", "native library not loaded");
        if (ctx != null) appCtx = ctx.getApplicationContext();
        setLogDir(ctx);
        int r = nativeStartDiag();
        if (r != 0) return err("oboe_diag", nativeLastError());
        return "{\"ok\":true,\"running\":true}";
    }

    public void stopDiag() {
        if (LIB_OK) nativeStopDiag();
    }

    /** 与旧引擎同一套路由判定；输出设备列表做 1s 缓存。 */
    private String detectRoute(Context ctx) {
        long now = SystemClock.elapsedRealtime();
        if (now - routeAt < 1000) return cachedRoute;
        routeAt = now;
        String route = "unknown";
        try {
            if (ctx == null) {
                cachedRoute = route;
                return route;
            }
            AudioManager am = (AudioManager) ctx.getSystemService(Context.AUDIO_SERVICE);
            AudioDeviceInfo[] devs = am == null ? null
                    : am.getDevices(AudioManager.GET_DEVICES_OUTPUTS);
            boolean wired = false, usb = false, ble = false, bt = false, speaker = false;
            if (devs != null) {
                for (AudioDeviceInfo d : devs) {
                    int type = d.getType();
                    if (type == AudioDeviceInfo.TYPE_WIRED_HEADSET
                            || type == AudioDeviceInfo.TYPE_WIRED_HEADPHONES) wired = true;
                    else if (type == AudioDeviceInfo.TYPE_USB_HEADSET
                            || type == AudioDeviceInfo.TYPE_USB_DEVICE) usb = true;
                    else if (Build.VERSION.SDK_INT >= 31
                            && type == AudioDeviceInfo.TYPE_BLE_HEADSET) ble = true;
                    else if (type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP
                            || type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO) bt = true;
                    else if (type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER) speaker = true;
                }
            }
            if (wired) route = "wired_headset";
            else if (usb) route = "usb_headset";
            else if (ble) route = "ble_headset";
            else if (bt) route = "bluetooth";
            else if (speaker) route = "speaker";
        } catch (Exception ignored) {
        }
        cachedRoute = route;
        return route;
    }

    private static JSONObject parse(String json) {
        if (json == null || json.isEmpty()) return new JSONObject();
        try {
            return new JSONObject(json);
        } catch (Exception e) {
            return new JSONObject();
        }
    }

    private static double clamp(double v, double lo, double hi) {
        if (Double.isNaN(v) || Double.isInfinite(v)) v = 0;
        return Math.max(lo, Math.min(hi, v));
    }

    private static String err(String code, String detail) {
        try {
            JSONObject o = new JSONObject();
            o.put("ok", false);
            o.put("error", code == null ? "error" : code);
            o.put("detail", detail == null ? "" : detail);
            return o.toString();
        } catch (Exception e) {
            return "{\"ok\":false,\"error\":\"error\"}";
        }
    }

    private native int nativeStart(double gain, double toneDb, boolean muted);
    private native void nativeStop();
    private native void nativeSetParams(double gain, double toneDb, boolean muted);
    private native String nativeStatusJson();
    private native String nativeLastError();
    private native int nativeStartDiag();
    private native void nativeStopDiag();
    private native void nativeSetLogPath(String path);
}
