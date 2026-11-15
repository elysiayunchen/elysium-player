package com.elysium.player;

import android.content.Context;
import android.util.Log;

import java.io.File;
import java.io.FileWriter;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * 崩溃 / 桥调用面包屑（beta10）。
 *
 * 背景：beta9 真机「唱完闪退」无法定位 —— 用户侧没有 adb，原生 tombstone 拿不到。
 * 这层把三类证据落到 getExternalFilesDir/crash-log.txt（应用外部目录）：
 *   1. 每个桥方法的进出 —— 哪个 JNI 调用前后断的；
 *   2. 未捕获 Java 异常（完整栈，链回系统默认 handler，崩溃对话框照常出现）；
 *   3. 原生侧 SIGSEGV/SIGABRT 等由 C++ 面包屑兜底（见 oboe_karaoke.cpp blogLine）。
 * 自检面板经 crashLog() 桥直接读尾部。任何写失败都静默 —— 诊断层绝不能反过来
 * 把应用搞崩。
 */
public final class CrashLog {

    private static volatile File file;
    private static final SimpleDateFormat FMT =
            new SimpleDateFormat("MM-dd HH:mm:ss.SSS", Locale.US);

    private CrashLog() {}

    public static void init(Context ctx) {
        try {
            File dir = ctx.getExternalFilesDir(null);
            if (dir != null) {
                file = new File(dir, "crash-log.txt");
            }
        } catch (Throwable ignored) {
        }
    }

    public static synchronized void log(String line) {
        if (file == null) {
            return;
        }
        try (FileWriter w = new FileWriter(file, true)) {
            w.write(FMT.format(new Date()));
            w.write(' ');
            w.write(line);
            w.write('\n');
        } catch (Exception ignored) {
        }
    }

    /** 读文件末尾 maxBytes，给自检面板展示；读不到给一句可读的说明。 */
    public static synchronized String tail(int maxBytes) {
        if (file == null || !file.exists() || file.length() == 0) {
            return "暂无记录";
        }
        try (RandomAccessFile raf = new RandomAccessFile(file, "r")) {
            long len = raf.length();
            int n = (int) Math.min(maxBytes, len);
            raf.seek(len - n);
            byte[] b = new byte[n];
            raf.readFully(b);
            return new String(b, StandardCharsets.UTF_8);
        } catch (Exception e) {
            return "read failed: " + e;
        }
    }

    /** 链回系统默认 handler：崩溃对话框照常出现，只是我们先留了一份栈。 */
    public static void install() {
        final Thread.UncaughtExceptionHandler prev =
                Thread.getDefaultUncaughtExceptionHandler();
        Thread.setDefaultUncaughtExceptionHandler((t, e) -> {
            log("UNCAUGHT thread=" + t.getName() + "\n" + Log.getStackTraceString(e));
            if (prev != null) {
                prev.uncaughtException(t, e);
            }
        });
    }
}
