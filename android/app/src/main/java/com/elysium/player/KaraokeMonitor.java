package com.elysium.player;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.media.AudioAttributes;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.Process;
import android.os.SystemClock;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * K 歌原生干声耳返（2026-11-14）。
 *
 * 背景：WebView 的 getUserMedia 采集栈在 Android 上有约 110ms 隐藏缓冲，
 * 加 64ms 输出缓冲，实测耳返 RTT 179ms。JS 图本身零缓冲，改不动；
 * 这里用 AudioRecord → AudioTrack 的低延迟通路只把「干声耳返」搬原生，
 * 录音 / 混响 / 音准 / 伴奏继续走 WebAudio（双采集，见 plans/NATIVE-MON-2026-11-14.md）。
 *
 * 约定：
 * - 纯 Java，不引 NDK/Oboe；API 26+ 的 AudioTrack 走 PERFORMANCE_MODE_LOW_LATENCY。
 * - 不申请音频焦点、不建 MediaSession —— 焦点仍归 WebView 的 <audio>（beta2–beta4 踩过
 *   两个焦点客户端互相暂停的坑）。
 * - 只在前台 Activity 期间运行；MainActivity.onStop 会停，不声明麦克风 FGS。
 * - 任何初始化失败都返回 {ok:false,...}，由页面回退 WebView 耳返，绝不把 K 歌搞哑。
 */
public final class KaraokeMonitor {

    private static final String TAG = "ElysiumKaraokeMon";
    private static final int[] RATES = {48000, 44100};
    private static final int CHUNK_FRAMES = 256;          // 5.3ms @48k
    private static final int BUFFER_CHUNKS = 4;
    private static final int BYTES_PER_SAMPLE = 2;
    private static final float LIM_KNEE = 0.7f;
    private static final float LIM_TOP = 0.97f;
    private static final int LIM_TABLE = 1024;
    private static final int DIAG_PULSES = 8;
    private static final double DIAG_PULSE_START = 0.9;
    private static final double DIAG_PULSE_GAP = 0.85;
    private static final double DIAG_PULSE_MS = 60;
    private static final double DIAG_TOTAL_SEC = 9.0;

    private static volatile KaraokeMonitor instance;

    public static KaraokeMonitor get() {
        KaraokeMonitor m = instance;
        if (m == null) {
            synchronized (KaraokeMonitor.class) {
                m = instance;
                if (m == null) {
                    m = new KaraokeMonitor();
                    instance = m;
                }
            }
        }
        return m;
    }

    private KaraokeMonitor() {}

    /* ---------------- 运行状态 ---------------- */

    private final Object lock = new Object();
    private volatile boolean running;
    private volatile Thread thread;
    private volatile AudioRecord record;
    private volatile AudioTrack track;
    private volatile Context appCtx;

    private volatile int sampleRate;
    private volatile int chunkFrames = CHUNK_FRAMES;
    private volatile int source;
    private volatile String sourceName = "";
    private volatile String performanceMode = "default";
    private volatile int recBufFrames;
    private volatile int trkBufFrames;
    private volatile float inRms;
    private volatile float outRms;
    private volatile long framesRead;
    private volatile long framesWritten;
    private volatile String lastError = "";

    private volatile float targetGain = 1f;
    private volatile float targetToneDb = 0f;
    private volatile boolean muted;
    private volatile String cachedRoute = "unknown";
    private volatile long routeAt;

    /* ---------------- 原生自检状态 ---------------- */

    private final Object diagLock = new Object();
    private volatile boolean diagRunning;
    private volatile boolean diagDone;
    private volatile Thread diagThread;
    private volatile String diagError = "";
    private volatile String diagRoute = "";
    private volatile double diagRtt = -1;
    private volatile double diagMin = -1;
    private volatile double diagMax = -1;
    private final List<Double> diagHits = new ArrayList<>();

    /* ---------------- 对外控制 ---------------- */

    /** 启动干声直通；返回 JSON（ok=false 时由页面回退 WebView 耳返）。 */
    public String start(Context ctx, String json) {
        JSONObject cfg = parse(json);
        Context app = ctx == null ? null : ctx.getApplicationContext();

        // 先停旧引擎；join 必须在 lock 外做 —— 音频线程退出时会回来拿 lock，
        // 持锁 join 会互相等 500ms（甚至误判超时）。
        Thread old;
        synchronized (lock) {
            old = thread;
            running = false;
            unblockLocked();
        }
        if (old != null && old != Thread.currentThread()) {
            try {
                old.join(500);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
        if (old != null && old.isAlive()) {
            // 旧线程没退出：不要释放它正在用的流，直接让页面回退 WebView 耳返。
            return err("busy", "previous monitor still stopping");
        }

        synchronized (lock) {
            releaseLocked();
            appCtx = app;
            if (app == null) return err("no_context", "no Context");
            if (app.checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                    != PackageManager.PERMISSION_GRANTED) {
                return err("permission", "RECORD_AUDIO not granted");
            }
            targetGain = clamp(cfg.optDouble("gain", 1.0), 0.0, 4.0).floatValue();
            targetToneDb = clamp(cfg.optDouble("toneDb", 0.0), -12.0, 12.0).floatValue();
            muted = cfg.optBoolean("muted", false);
            lastError = "";
            inRms = 0f;
            outRms = 0f;
            framesRead = 0;
            framesWritten = 0;

            Exception first = null;
            for (int rate : RATES) {
                try {
                    AudioRecord r = openRecord(app, rate);
                    AudioTrack t = openTrack(rate);
                    record = r;
                    track = t;
                    sampleRate = rate;
                    chunkFrames = CHUNK_FRAMES;
                    recBufFrames = safeBufFrames(r);
                    trkBufFrames = safeTrackBufFrames(t);
                    running = true;
                    Thread th = new Thread(this::loop, "ElysiumKaraokeMon");
                    thread = th;
                    th.start();
                    return statusLocked();
                } catch (Exception e) {
                    first = e;
                    releaseLocked();
                }
            }
            lastError = first == null ? "audio init failed" : String.valueOf(first.getMessage());
            return err("audio_init", lastError);
        }
    }

    /** 运行中改音量 / 音色 / 静音；失败返回 ok=false。 */
    public String update(String json) {
        JSONObject cfg = parse(json);
        synchronized (lock) {
            if (cfg.has("gain")) {
                targetGain = clamp(cfg.optDouble("gain", targetGain), 0.0, 4.0).floatValue();
            }
            if (cfg.has("toneDb")) {
                targetToneDb = clamp(cfg.optDouble("toneDb", targetToneDb), -12.0, 12.0).floatValue();
            }
            if (cfg.has("muted")) muted = cfg.optBoolean("muted", muted);
            return ok();
        }
    }

    /**
     * 停直通。快速返回：先让 read/write 解锁，再由清理线程 join/release，
     * 避免 onStop 在 UI 线程等音频线程。start() 会在锁外 join 旧线程再重建。
     */
    public void stop() {
        final Thread th;
        final AudioRecord oldR;
        final AudioTrack oldT;
        synchronized (lock) {
            if (!running && record == null && track == null) return;
            running = false;
            th = thread;
            oldR = record;
            oldT = track;
            unblockLocked();
        }
        if (th != null && th != Thread.currentThread()) {
            Thread cleaner = new Thread(() -> {
                try {
                    th.join(500);
                } catch (InterruptedException ignored) {
                    Thread.currentThread().interrupt();
                }
                releaseIfSame(oldR, oldT, th);
            }, "ElysiumKaraokeMonStop");
            cleaner.setDaemon(true);
            cleaner.start();
        } else {
            releaseIfSame(oldR, oldT, th);
        }
    }

    /** 页面轮询：运行状态 + RMS + 缓冲 + 路由 + 自检进度。 */
    public String status() {
        synchronized (lock) {
            return statusLocked();
        }
    }

    /** 原生脉冲自检：后台跑，结果从 status().diag 读。 */
    public String startDiag(Context ctx, String json) {
        Context app = ctx == null ? null : ctx.getApplicationContext();
        synchronized (lock) {
            if (running) return err("monitor_running", "stop monitor before diag");
        }
        synchronized (diagLock) {
            if (diagRunning) return ok();
            if (app == null) return err("no_context", "no Context");
            if (app.checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                    != PackageManager.PERMISSION_GRANTED) {
                return err("permission", "RECORD_AUDIO not granted");
            }
            diagRunning = true;
            diagDone = false;
            diagError = "";
            diagRoute = "";
            diagRtt = -1;
            diagMin = -1;
            diagMax = -1;
            diagHits.clear();
            appCtx = app;
            Thread th = new Thread(() -> diagLoop(app), "ElysiumKaraokeDiag");
            diagThread = th;
            th.setDaemon(true);
            th.start();
        }
        return ok();
    }

    /** 取消正在跑的自检（离前台 / 退出页面时调用）。 */
    public void stopDiag() {
        synchronized (diagLock) {
            diagRunning = false;
        }
    }

    /* ---------------- 主循环 ---------------- */

    private void loop() {
        Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO);
        AudioRecord r = record;
        AudioTrack t = track;
        final int rate = sampleRate;
        final int chunk = chunkFrames;
        if (r == null || t == null) {
            lastError = "audio stream missing";
            cleanupLoop(r, t);
            return;
        }
        short[] in = new short[chunk];
        short[] out = new short[chunk * 2];
        Biquad hpf = new Biquad();
        hpf.highpass(80.0, rate);
        Biquad tone = new Biquad();
        tone.peaking(2500.0, 0.9, rate, targetToneDb);
        float appliedTone = targetToneDb;
        float curGain = 0f;              // 从 0 ramp 起，避免起播咔哒
        float curMute = 0f;
        int readErrs = 0;
        try {
            t.play();
            r.startRecording();
            while (running) {
                int n = r.read(in, 0, chunk, AudioRecord.READ_BLOCKING);
                if (!running) break;
                if (n <= 0) {
                    // 麦克风被系统收回时 read 会持续报错；只认 DEAD_OBJECT 会空转。
                    readErrs += 1;
                    if (n == AudioRecord.ERROR_DEAD_OBJECT || readErrs > 50) {
                        lastError = "record read " + n;
                        break;
                    }
                    continue;
                }
                readErrs = 0;
                if (targetToneDb != appliedTone) {
                    tone.peaking(2500.0, 0.9, rate, targetToneDb);
                    appliedTone = targetToneDb;
                }
                float gTarget = targetGain;
                float mTarget = muted ? 0f : 1f;
                float gStep = (gTarget - curGain) / n;
                float mStep = (mTarget - curMute) / n;
                double sumIn = 0;
                double sumOut = 0;
                for (int i = 0; i < n; i++) {
                    float x = in[i] / 32768f;
                    sumIn += (double) x * x;
                    x = hpf.process(x);
                    curGain += gStep;
                    x *= curGain;
                    x = tone.process(x);
                    curMute += mStep;
                    x *= curMute;
                    x = limit(x);
                    sumOut += (double) x * x;
                    short s = (short) Math.max(-32768f, Math.min(32767f, x * 32767f));
                    out[i * 2] = s;
                    out[i * 2 + 1] = s;
                }
                framesRead += n;
                float blockIn = (float) Math.sqrt(sumIn / n);
                float blockOut = (float) Math.sqrt(sumOut / n);
                inRms = inRms * 0.7f + blockIn * 0.3f;
                outRms = outRms * 0.7f + blockOut * 0.3f;
                int want = n * 2;
                int off = 0;
                while (off < want && running) {
                    int w = t.write(out, off, want - off, AudioTrack.WRITE_BLOCKING);
                    if (w < 0) {
                        lastError = "track write " + w;
                        off = -1;
                        break;
                    }
                    if (w == 0) break;
                    off += w;
                    framesWritten += w / 2;
                }
                if (off < 0) break;
            }
        } catch (Exception e) {
            lastError = String.valueOf(e.getMessage());
            Log.w(TAG, "monitor loop stopped", e);
        } finally {
            cleanupLoop(r, t);
        }
    }

    private void cleanupLoop(AudioRecord r, AudioTrack t) {
        running = false;
        synchronized (lock) {
            if (record == r) {
                releaseRecord(r);
                record = null;
            }
            if (track == t) {
                releaseTrack(t);
                track = null;
            }
            if (thread == Thread.currentThread()) thread = null;
        }
    }

    /* ---------------- 自检：脉冲 RTT ---------------- */

    private void diagLoop(Context ctx) {
        Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO);
        AudioRecord r = null;
        AudioTrack t = null;
        try {
            int rate = 48000;
            r = openRecord(ctx, rate);
            t = openTrack(rate);
            final AudioRecord dr = r;
            final AudioTrack dt = t;
            final long startNs = System.nanoTime();
            final long[] pulseNs = new long[DIAG_PULSES];
            final int rateF = rate;
            final int chunk = CHUNK_FRAMES;

            Thread writer = new Thread(() -> {
                try {
                    dt.play();
                    int total = (int) (rateF * DIAG_TOTAL_SEC);
                    int pos = 0;
                    short[] buf = new short[chunk * 2];
                    while (pos < total && diagRunning) {
                        java.util.Arrays.fill(buf, (short) 0);
                        for (int p = 0; p < DIAG_PULSES; p++) {
                            int start = (int) ((DIAG_PULSE_START + p * DIAG_PULSE_GAP) * rateF);
                            int len = (int) (DIAG_PULSE_MS / 1000.0 * rateF);
                            if (start >= pos + chunk || start + len <= pos) continue;
                            if (start >= pos && start < pos + chunk) {
                                pulseNs[p] = System.nanoTime();
                            }
                            for (int i = 0; i < chunk; i++) {
                                int abs = pos + i;
                                if (abs < start || abs >= start + len) continue;
                                double ph = 2 * Math.PI * 1000.0 * (abs - start) / rateF;
                                double env = 1.0;
                                int edge = (int) (0.005 * rateF);
                                int into = abs - start;
                                int left = start + len - abs;
                                if (into < edge) env = into / (double) edge;
                                if (left < edge) env = Math.min(env, left / (double) edge);
                                short s = (short) (Math.sin(ph) * env * 0.8 * 32767);
                                buf[i * 2] = s;
                                buf[i * 2 + 1] = s;
                            }
                        }
                        dt.write(buf, 0, chunk * 2, AudioTrack.WRITE_BLOCKING);
                        pos += chunk;
                    }
                } catch (Exception e) {
                    synchronized (diagLock) {
                        diagError = "writer: " + e.getMessage();
                    }
                }
            }, "ElysiumDiagOut");

            Thread reader = new Thread(() -> {
                try {
                    dr.startRecording();
                    short[] buf = new short[chunk];
                    double noisePeak = 0;
                    List<Double> onsets = new ArrayList<>();
                    long deadline = startNs + (long) ((DIAG_TOTAL_SEC + 0.8) * 1e9);
                    while (diagRunning && System.nanoTime() < deadline) {
                        int n = dr.read(buf, 0, chunk, AudioRecord.READ_BLOCKING);
                        long now = System.nanoTime();
                        if (n <= 0) continue;
                        double elapsed = (now - startNs) / 1e6;
                        if (elapsed < 500) {
                            for (int i = 0; i < n; i++) {
                                noisePeak = Math.max(noisePeak, Math.abs(buf[i] / 32768.0));
                            }
                            continue;
                        }
                        double thr = Math.max(noisePeak * 4, 0.02);
                        for (int i = 0; i < n; i++) {
                            double a = Math.abs(buf[i] / 32768.0);
                            if (a > thr) {
                                double tMs = (now - startNs) / 1e6
                                        - (n / (double) rateF) * 1000.0
                                        + (i / (double) rateF) * 1000.0;
                                onsets.add(tMs);
                                break;
                            }
                        }
                    }
                    matchPulses(pulseNs, onsets, startNs);
                } catch (Exception e) {
                    synchronized (diagLock) {
                        diagError = "reader: " + e.getMessage();
                    }
                }
            }, "ElysiumDiagIn");

            writer.setDaemon(true);
            reader.setDaemon(true);
            writer.start();
            reader.start();
            writer.join((long) ((DIAG_TOTAL_SEC + 2) * 1000));
            reader.join((long) ((DIAG_TOTAL_SEC + 2) * 1000));
            synchronized (diagLock) {
                diagRoute = detectRoute(appCtx);
                diagRunning = false;
                diagDone = true;
            }
        } catch (Exception e) {
            synchronized (diagLock) {
                diagError = String.valueOf(e.getMessage());
                diagRunning = false;
                diagDone = true;
            }
            Log.w(TAG, "diag failed", e);
        } finally {
            diagRunning = false;
            releaseRecord(r);
            releaseTrack(t);
        }
    }

    private void matchPulses(long[] pulseNs, List<Double> onsets, long startNs) {
        List<Double> rtts = new ArrayList<>();
        boolean[] used = new boolean[onsets.size()];
        for (int p = 0; p < pulseNs.length; p++) {
            if (pulseNs[p] == 0) continue;
            double pMs = (pulseNs[p] - startNs) / 1e6;
            for (int i = 0; i < onsets.size(); i++) {
                if (used[i]) continue;
                double o = onsets.get(i);
                if (o >= pMs + 5 && o <= pMs + 600) {
                    used[i] = true;
                    rtts.add(o - pMs);
                    break;
                }
            }
        }
        synchronized (diagLock) {
            diagHits.clear();
            diagHits.addAll(rtts);
            if (!rtts.isEmpty()) {
                java.util.Collections.sort(rtts);
                double med = rtts.size() % 2 == 1
                        ? rtts.get(rtts.size() / 2)
                        : (rtts.get(rtts.size() / 2 - 1) + rtts.get(rtts.size() / 2)) / 2;
                diagRtt = med;
                diagMin = rtts.get(0);
                diagMax = rtts.get(rtts.size() - 1);
            }
        }
    }

    /* ---------------- 状态 JSON ---------------- */

    private String statusLocked() {
        try {
            JSONObject o = new JSONObject();
            o.put("ok", true);
            o.put("running", running);
            o.put("engine", "java");
            o.put("api", "java");
            o.put("inRms", round3(inRms));
            o.put("outRms", round3(outRms));
            o.put("underruns", underruns());
            o.put("framesRead", framesRead);
            o.put("framesWritten", framesWritten);
            o.put("route", detectRoute(appCtx));
            o.put("source", sourceName);
            o.put("sampleRate", sampleRate);
            o.put("chunkFrames", chunkFrames);
            o.put("recordBufFrames", recBufFrames);
            o.put("trackBufFrames", trkBufFrames);
            o.put("recordBufMs", bufMs(recBufFrames, sampleRate));
            o.put("trackBufMs", bufMs(trkBufFrames, sampleRate));
            o.put("performanceMode", performanceMode);
            o.put("lastError", lastError == null ? "" : lastError);
            o.put("diag", diagJson());
            return o.toString();
        } catch (Exception e) {
            return err("json", String.valueOf(e.getMessage()));
        }
    }

    private JSONObject diagJson() throws Exception {
        JSONObject d = new JSONObject();
        synchronized (diagLock) {
            d.put("running", diagRunning);
            d.put("done", diagDone);
            d.put("error", diagError == null ? "" : diagError);
            d.put("route", diagRoute);
            d.put("rttMs", diagRtt < 0 ? JSONObject.NULL : round1(diagRtt));
            d.put("minMs", diagMin < 0 ? JSONObject.NULL : round1(diagMin));
            d.put("maxMs", diagMax < 0 ? JSONObject.NULL : round1(diagMax));
            JSONArray hits = new JSONArray();
            for (double v : diagHits) hits.put(round1(v));
            d.put("hits", hits);
        }
        return d;
    }

    private long underruns() {
        AudioTrack t = track;
        if (t == null || Build.VERSION.SDK_INT < 24) return 0;
        try {
            return t.getUnderrunCount();
        } catch (Exception e) {
            return 0;
        }
    }

    private static double bufMs(int frames, int rate) {
        if (rate <= 0 || frames <= 0) return 0;
        return Math.round(frames * 10000.0 / rate) / 10.0;
    }

    private static double round3(float v) {
        return Math.round(v * 1000.0) / 1000.0;
    }

    private static double round1(double v) {
        return Math.round(v * 10.0) / 10.0;
    }

    /* ---------------- 流打开 / 释放 ---------------- */

    private AudioRecord openRecord(Context ctx, int rate) {
        int min = AudioRecord.getMinBufferSize(rate,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        int bytes = Math.max(min > 0 ? min : CHUNK_FRAMES * BYTES_PER_SAMPLE * 4,
                CHUNK_FRAMES * BYTES_PER_SAMPLE * BUFFER_CHUNKS);
        int[] sources = sourceCandidates(ctx);
        Exception last = null;
        for (int src : sources) {
            try {
                AudioRecord r = new AudioRecord.Builder()
                        .setAudioSource(src)
                        .setAudioFormat(new AudioFormat.Builder()
                                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                                .setSampleRate(rate)
                                .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                                .build())
                        .setBufferSizeInBytes(bytes)
                        .build();
                if (r.getState() != AudioRecord.STATE_INITIALIZED) {
                    int st = r.getState();
                    r.release();
                    throw new IllegalStateException("AudioRecord state " + st);
                }
                source = src;
                sourceName = sourceName(src);
                return r;
            } catch (Exception e) {
                last = e;
            }
        }
        throw new IllegalStateException("AudioRecord init failed: "
                + (last == null ? "no source" : last.getMessage()));
    }

    private int[] sourceCandidates(Context ctx) {
        boolean unprocessed = false;
        try {
            AudioManager am = (AudioManager) ctx.getSystemService(Context.AUDIO_SERVICE);
            unprocessed = am != null
                    && "true".equals(am.getProperty(
                    AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED));
        } catch (Exception ignored) {
        }
        if (unprocessed) {
            return new int[]{MediaRecorder.AudioSource.UNPROCESSED,
                    MediaRecorder.AudioSource.VOICE_RECOGNITION,
                    MediaRecorder.AudioSource.MIC};
        }
        return new int[]{MediaRecorder.AudioSource.VOICE_RECOGNITION,
                MediaRecorder.AudioSource.MIC};
    }

    private static String sourceName(int src) {
        switch (src) {
            case MediaRecorder.AudioSource.UNPROCESSED: return "unprocessed";
            case MediaRecorder.AudioSource.VOICE_RECOGNITION: return "voice_recognition";
            default: return "mic";
        }
    }

    private AudioTrack openTrack(int rate) {
        int min = AudioTrack.getMinBufferSize(rate,
                AudioFormat.CHANNEL_OUT_STEREO, AudioFormat.ENCODING_PCM_16BIT);
        int bytes = Math.max(min > 0 ? min : CHUNK_FRAMES * BYTES_PER_SAMPLE * 2 * 4,
                CHUNK_FRAMES * BYTES_PER_SAMPLE * 2 * BUFFER_CHUNKS);
        AudioTrack.Builder b = new AudioTrack.Builder()
                .setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                        .build())
                .setAudioFormat(new AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(rate)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO)
                        .build())
                .setTransferMode(AudioTrack.MODE_STREAM)
                .setBufferSizeInBytes(bytes);
        if (Build.VERSION.SDK_INT >= 26) {
            b.setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY);
            performanceMode = "low_latency";
        } else {
            performanceMode = "default";
        }
        AudioTrack t = b.build();
        if (t.getState() != AudioTrack.STATE_INITIALIZED) {
            t.release();
            throw new IllegalStateException("AudioTrack state " + t.getState());
        }
        return t;
    }

    private static int safeBufFrames(AudioRecord r) {
        try {
            return r.getBufferSizeInFrames();
        } catch (Exception e) {
            return 0;
        }
    }

    private static int safeTrackBufFrames(AudioTrack t) {
        try {
            return t.getBufferSizeInFrames();
        } catch (Exception e) {
            return 0;
        }
    }

    private void unblockLocked() {
        AudioRecord r = record;
        AudioTrack t = track;
        try {
            if (r != null) r.stop();
        } catch (Exception ignored) {
        }
        try {
            if (t != null) {
                t.pause();
                t.flush();
                t.stop();
            }
        } catch (Exception ignored) {
        }
    }

    private void releaseIfSame(AudioRecord r, AudioTrack t, Thread th) {
        synchronized (lock) {
            if (record == r) {
                releaseRecord(r);
                record = null;
            }
            if (track == t) {
                releaseTrack(t);
                track = null;
            }
            if (thread == th) thread = null;
            if (record == null && track == null) running = false;
        }
    }

    private void releaseLocked() {
        releaseRecord(record);
        record = null;
        releaseTrack(track);
        track = null;
        thread = null;
        running = false;
    }

    private void releaseRecord(AudioRecord r) {
        if (r == null) return;
        try {
            if (r.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) r.stop();
        } catch (Exception ignored) {
        }
        try {
            r.release();
        } catch (Exception ignored) {
        }
    }

    private void releaseTrack(AudioTrack t) {
        if (t == null) return;
        try {
            t.pause();
            t.flush();
            t.stop();
        } catch (Exception ignored) {
        }
        try {
            t.release();
        } catch (Exception ignored) {
        }
    }

    /* ---------------- 路由 ---------------- */

    private String detectRoute(Context ctx) {
        long now = SystemClock.elapsedRealtime();
        if (now - routeAt < 1000) return cachedRoute;
        routeAt = now;
        String route = "unknown";
        try {
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

    /* ---------------- 小工具 ---------------- */

    private static JSONObject parse(String json) {
        if (json == null || json.isEmpty()) return new JSONObject();
        try {
            return new JSONObject(json);
        } catch (Exception e) {
            return new JSONObject();
        }
    }

    private static Double clamp(double v, double lo, double hi) {
        if (Double.isNaN(v) || Double.isInfinite(v)) v = 0;
        return Math.max(lo, Math.min(hi, v));
    }

    private static String ok() {
        return "{\"ok\":true}";
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

    /* ---------------- DSP ---------------- */

    /** RBJ 二阶 biquad（Transposed Direct Form II），只处理单声道。 */
    private static final class Biquad {
        private double b0, b1, b2, a1, a2, z1, z2;

        void highpass(double f, double fs) {
            double w0 = 2 * Math.PI * f / fs;
            double cos = Math.cos(w0);
            double alpha = Math.sin(w0) / (2 * 0.70710678);
            double a0 = 1 + alpha;
            b0 = (1 + cos) / 2 / a0;
            b1 = -(1 + cos) / a0;
            b2 = (1 + cos) / 2 / a0;
            a1 = -2 * cos / a0;
            a2 = (1 - alpha) / a0;
        }

        void peaking(double f, double q, double fs, double db) {
            double A = Math.pow(10, db / 40);
            double w0 = 2 * Math.PI * f / fs;
            double cos = Math.cos(w0);
            double alpha = Math.sin(w0) / (2 * q);
            double a0 = 1 + alpha / A;
            b0 = (1 + alpha * A) / a0;
            b1 = -2 * cos / a0;
            b2 = (1 - alpha * A) / a0;
            a1 = -2 * cos / a0;
            a2 = (1 - alpha / A) / a0;
        }

        float process(float x) {
            double y = b0 * x + z1;
            z1 = b1 * x - a1 * y + z2;
            z2 = b2 * x - a2 * y;
            return (float) y;
        }
    }

    /** 与 web karLimCurve 同曲线：|x|≤0.7 线性，之后幂式压向 0.97；查表零过冲。 */
    private static final float[] LIM = buildLim();

    private static float[] buildLim() {
        float[] t = new float[LIM_TABLE + 1];
        float k = (1f - LIM_KNEE) / (LIM_TOP - LIM_KNEE);
        for (int i = 0; i <= LIM_TABLE; i++) {
            float a = (float) i / LIM_TABLE;
            if (a <= LIM_KNEE) {
                t[i] = a;
            } else {
                float u = (a - LIM_KNEE) / (1f - LIM_KNEE);
                t[i] = LIM_KNEE + (LIM_TOP - LIM_KNEE)
                        * (float) (1 - Math.pow(1 - u, k));
            }
        }
        return t;
    }

    private static float limit(float x) {
        float a = Math.min(1f, Math.abs(x));
        float p = a * LIM_TABLE;
        int i = (int) p;
        if (i >= LIM_TABLE) return x < 0 ? -LIM[LIM_TABLE] : LIM[LIM_TABLE];
        float y = LIM[i] + (LIM[i + 1] - LIM[i]) * (p - i);
        return x < 0 ? -y : y;
    }
}
