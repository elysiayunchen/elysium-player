package com.elysium.player;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.media.session.MediaSession;
import android.media.MediaMetadata;
import android.media.session.PlaybackState;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;

import androidx.core.app.NotificationCompat;
import androidx.media.app.NotificationCompat.MediaStyle;

import org.json.JSONObject;

import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 前台媒体服务：锁屏 / 切后台时保住播放进程，通知栏给系统级的播放控制。
 *
 * 数据流向是单向的：WebView 页面（app.js）在换曲、播放 / 暂停、拖动进度时
 * 经 JavascriptInterface 把状态 JSON 推过来（mediaUpdate）；服务只负责把它
 * 变成 MediaSession + 通知。用户的控制操作（通知按钮 / 耳机线控 / 锁屏控件）
 * 经 MainActivity.dispatchMedia(cmd) 打回页面，由页面统一改 <audio> 状态 ——
 * 播放事实上的唯一真相源始终是页面，服务不碰音频流。
 */
public class PlaybackService extends Service {

    public static final String ACTION_PUSH = "com.elysium.player.media.PUSH";
    public static final String ACTION_TOGGLE = "com.elysium.player.media.TOGGLE";
    public static final String ACTION_NEXT = "com.elysium.player.media.NEXT";
    public static final String ACTION_PREV = "com.elysium.player.media.PREV";
    public static final String ACTION_PAUSE = "com.elysium.player.media.PAUSE";
    private static final String EXTRA_JSON = "json";
    private static final String CHANNEL_ID = "playback";
    private static final int NOTIF_ID = 42;
    private static final String COVER_UA = "Mozilla/5.0 (Linux; Android) Elysium";

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService io = Executors.newSingleThreadExecutor();

    private MediaSession session;
    private AudioManager audioManager;
    private AudioFocusRequest focusReq;
    private PowerManager.WakeLock wake;
    private BroadcastReceiver noisy;
    private boolean focusHeld = false;
    private boolean resumeOnFocus = false;
    private JSONObject meta = new JSONObject();
    private Bitmap coverBmp;
    private String coverLoaded = "";

    /** JS 桥入口：起服务并投递状态 JSON（可从任意线程调用）。 */
    public static void push(Context c, String json) {
        Intent it = new Intent(c, PlaybackService.class).setAction(ACTION_PUSH);
        if (json != null) {
            it.putExtra(EXTRA_JSON, json);
        }
        // Android 12+ 对后台起 FGS 有豁免校验：锁屏自动连播等边缘场景可能被拒，
        // 降级 startService 再不行就丢弃这次推送 —— 下一首歌的状态会再推。
        if (Build.VERSION.SDK_INT >= 26) {
            try {
                c.startForegroundService(it);
                return;
            } catch (Exception ignored) {
                // 落到 startService 兜底
            }
        }
        try {
            c.startService(it);
        } catch (Exception ignored) {
        }
    }

    /** 页面明确收尾（暂无曲目）时撤掉通知与服务。 */
    public static void hide(Context c) {
        c.stopService(new Intent(c, PlaybackService.class));
    }

    @Override
    public void onCreate() {
        super.onCreate();
        audioManager = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        session = new MediaSession(this, "Elysium");
        session.setActive(true);
        session.setCallback(new MediaSession.Callback() {
            @Override public void onPlay() { MainActivity.dispatchMedia("play"); }
            @Override public void onPause() { MainActivity.dispatchMedia("pause"); }
            @Override public void onSkipToNext() { MainActivity.dispatchMedia("next"); }
            @Override public void onSkipToPrevious() { MainActivity.dispatchMedia("prev"); }
            @Override public void onStop() { MainActivity.dispatchMedia("stop"); }
            @Override public void onSeekTo(long pos) {
                MainActivity.dispatchMedia("seek:" + pos);
            }
        });

        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CHANNEL_ID) == null) {
            NotificationChannel ch = new NotificationChannel(CHANNEL_ID, "播放控制",
                    NotificationManager.IMPORTANCE_LOW);
            ch.setShowBadge(false);
            nm.createNotificationChannel(ch);
        }

        // 拔耳机 → 暂停（外放突然响起是事故，不是功能）
        noisy = new BroadcastReceiver() {
            @Override public void onReceive(Context context, Intent intent) {
                MainActivity.dispatchMedia("pause");
            }
        };
        registerReceiver(noisy, new IntentFilter(AudioManager.ACTION_AUDIO_BECOMING_NOISY));
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? null : intent.getAction();
        if (ACTION_TOGGLE.equals(action)) {
            MainActivity.dispatchMedia("toggle");
        } else if (ACTION_NEXT.equals(action)) {
            MainActivity.dispatchMedia("next");
        } else if (ACTION_PREV.equals(action)) {
            MainActivity.dispatchMedia("prev");
        } else if (ACTION_PAUSE.equals(action)) {
            MainActivity.dispatchMedia("pause");
        } else if (ACTION_PUSH.equals(action)) {
            try {
                String j = intent.getStringExtra(EXTRA_JSON);
                applyState(j == null ? new JSONObject() : new JSONObject(j));
            } catch (Exception e) {
                android.util.Log.w("Elysium", "mediaUpdate 解析失败", e);
            }
        }
        return START_NOT_STICKY;      // 状态由页面全量推送，重启后等下一个 push 即可
    }

    /** 收到页面推来的最新状态：更新焦点、MediaSession、通知。 */
    private void applyState(JSONObject j) {
        meta = j;
        boolean playing = j.optBoolean("playing", false);
        updateFocus(playing);
        updateWake(playing);
        updateSession(playing);
        startForegroundTyped(buildNotification(playing));
        loadCoverAsync(j.optString("cover", ""));
    }

    private void updateFocus(boolean playing) {
        if (audioManager == null) {
            return;
        }
        if (playing && !focusHeld) {
            if (focusReq == null) {
                focusReq = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                        .setAudioAttributes(new AudioAttributes.Builder()
                                .setUsage(AudioAttributes.USAGE_MEDIA)
                                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                                .build())
                        .setOnAudioFocusChangeListener(this::onFocusChange)
                        .build();
            }
            focusHeld = audioManager.requestAudioFocus(focusReq)
                    == AudioManager.AUDIOFOCUS_REQUEST_GRANTED;
        } else if (!playing && focusHeld) {
            audioManager.abandonAudioFocusRequest(focusReq);
            focusHeld = false;
        }
    }

    private void onFocusChange(int change) {
        switch (change) {
            case AudioManager.AUDIOFOCUS_LOSS_TRANSIENT:
            case AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK:
                resumeOnFocus = true;
                MainActivity.dispatchMedia("pause");
                break;
            case AudioManager.AUDIOFOCUS_LOSS:
                resumeOnFocus = false;
                focusHeld = false;
                MainActivity.dispatchMedia("pause");
                break;
            case AudioManager.AUDIOFOCUS_GAIN:
                if (resumeOnFocus) {
                    resumeOnFocus = false;
                    MainActivity.dispatchMedia("play");
                }
                break;
            default:
                break;
        }
    }

    /** 流媒体播放时兜住 CPU，避免熄屏后网络停摆（暂停即还）。 */
    private void updateWake(boolean playing) {
        if (playing && wake == null) {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "Elysium:playback");
            wake.setReferenceCounted(false);
        }
        try {
            if (playing) {
                wake.acquire(4 * 3600 * 1000L);
            } else if (wake != null && wake.isHeld()) {
                wake.release();
            }
        } catch (Exception ignored) {
        }
    }

    private void updateSession(boolean playing) {
        if (session == null) {
            return;
        }
        long dur = meta.optLong("duration", 0L) * 1000L;
        long pos = meta.optLong("position", 0L) * 1000L;
        MediaMetadata.Builder mb = new MediaMetadata.Builder()
                .putString(MediaMetadata.METADATA_KEY_TITLE,
                        meta.optString("title", "Elysium"))
                .putString(MediaMetadata.METADATA_KEY_ARTIST,
                        meta.optString("artist", ""))
                .putString(MediaMetadata.METADATA_KEY_ALBUM,
                        meta.optString("album", ""))
                .putLong(MediaMetadata.METADATA_KEY_DURATION, dur);
        if (coverBmp != null) {
            mb.putBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART, coverBmp);
        }
        session.setMetadata(mb.build());
        long acts = PlaybackState.ACTION_PLAY | PlaybackState.ACTION_PAUSE
                | PlaybackState.ACTION_PLAY_PAUSE | PlaybackState.ACTION_SKIP_TO_NEXT
                | PlaybackState.ACTION_SKIP_TO_PREVIOUS | PlaybackState.ACTION_SEEK_TO
                | PlaybackState.ACTION_STOP;
        session.setPlaybackState(new PlaybackState.Builder()
                .setActions(acts)
                .setState(playing ? PlaybackState.STATE_PLAYING : PlaybackState.STATE_PAUSED,
                        pos, playing ? 1f : 0f)
                .build());
    }

    private PendingIntent control(String action) {
        Intent it = new Intent(this, getClass()).setAction(action);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) {
            flags |= PendingIntent.FLAG_IMMUTABLE;
        }
        return PendingIntent.getService(this, action.hashCode(), it, flags);
    }

    private Notification buildNotification(boolean playing) {
        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        int openFlags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) {
            openFlags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent pOpen = PendingIntent.getActivity(this, 1, open, openFlags);

        NotificationCompat.Builder b = new NotificationCompat.Builder(this, CHANNEL_ID)
                .setSmallIcon(R.drawable.appicon)
                .setContentIntent(pOpen)
                .setContentTitle(meta.optString("title", "Elysium"))
                .setContentText(meta.optString("artist", ""))
                .setOngoing(playing)
                .setOnlyAlertOnce(true)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                .addAction(new NotificationCompat.Action(
                        R.drawable.appicon, "上一首", control(ACTION_PREV)))
                .addAction(new NotificationCompat.Action(
                        R.drawable.appicon, playing ? "暂停" : "播放",
                        control(playing ? ACTION_PAUSE : ACTION_TOGGLE)))
                .addAction(new NotificationCompat.Action(
                        R.drawable.appicon, "下一首", control(ACTION_NEXT)))
                .setStyle(new MediaStyle()
                        .setMediaSession(session == null ? null : session.getSessionToken())
                        .setShowActionsInCompactView(0, 1, 2));

        // 划掉通知 = 明确不要播了：暂停播放并收摊（避免「无声播放还占着进程」）
        b.setDeleteIntent(control(ACTION_PAUSE));
        if (coverBmp != null) {
            b.setLargeIcon(coverBmp);
        }
        return b.build();
    }

    private void startForegroundTyped(Notification n) {
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
        } else {
            startForeground(NOTIF_ID, n);
        }
    }

    /** 封面异步加载：只有 URL 变了才拉，下完刷新一次通知。 */
    private void loadCoverAsync(String url) {
        if (url == null || url.isEmpty() || url.equals(coverLoaded)) {
            return;
        }
        coverLoaded = url;
        io.execute(() -> {
            Bitmap bmp = null;
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                c.setConnectTimeout(6000);
                c.setReadTimeout(8000);
                c.setRequestProperty("User-Agent", COVER_UA);
                try (InputStream in = c.getInputStream()) {
                    byte[] buf = new byte[1 << 20];
                    int n, total = 0;
                    while ((n = in.read(buf, total, buf.length - total)) > 0
                            && total < (1 << 21)) {
                        total += n;
                        if (total >= buf.length) break;
                    }
                    bmp = BitmapFactory.decodeByteArray(buf, 0, total);
                } finally {
                    c.disconnect();
                }
            } catch (Exception e) {
                bmp = null;
            }
            final Bitmap f = bmp;
            main.post(() -> {
                coverBmp = f;
                if (f == null) {
                    return;                    // 封面拿不到就保持文字通知，不重试
                }
                updateSession(meta.optBoolean("playing", false));
                NotificationManager nm =
                        (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
                nm.notify(NOTIF_ID, buildNotification(meta.optBoolean("playing", false)));
            });
        });
    }

    @Override
    public void onDestroy() {
        if (noisy != null) {
            try {
                unregisterReceiver(noisy);
            } catch (Exception ignored) {
            }
            noisy = null;
        }
        if (focusReq != null && audioManager != null) {
            audioManager.abandonAudioFocusRequest(focusReq);
            focusHeld = false;
        }
        updateWake(false);
        if (session != null) {
            try {
                session.release();
            } catch (Exception ignored) {
            }
            session = null;
        }
        stopForeground(STOP_FOREGROUND_REMOVE);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
