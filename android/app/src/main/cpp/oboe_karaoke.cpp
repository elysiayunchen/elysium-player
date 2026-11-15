/*
 * Elysium 原生直通耳返 · Oboe（AAudio）双流低延迟引擎（2026-11-14，beta9）
 *
 * 为什么换 Oboe：
 *   beta8 的纯 Java AudioRecord→AudioTrack 自检/实测暴露出两个天花板：
 *   - AudioTrack 的 minBuffer 在部分机型是 128ms，Java 层压不下去；
 *   - 采集侧也拿不到 fast capture（record minBuffer 200ms 量级）。
 *   这两项都不是 DSP 能解决的，必须让 Oboe 去选 AAudio 的 low-latency 共享通路。
 *
 * 为什么是两条流：
 *   Oboe 1.9.3 的 Direction 只有 Input/Output，没有 Duplex；用一条 input + 一条
 *   output，各自 LowLatency/Shared，中间用一个小 SPSC 环形缓冲衔接。输入回调做
 *   干声 DSP 入环，输出回调出环；环形缓冲默认 85ms 容量、8ms 预填，给时钟漂移
 *   留余量，正常占用在预填量级。
 *
 * 本引擎只做「干声耳返」：
 *   mic → HPF 80Hz → gain（块内 ramp）→ peaking 2.5k Q0.9 → 软限幅 → ring → output。
 *   录音 / 混响 / 音准 / 伴奏仍在 WebView；失败由 Java 层回退旧引擎 / WebView。
 *
 * 实时约束：
 *   - 回调里不分配、不加锁、不调 JNI、不打日志；
 *   - 参数走 atomic，DSP 状态只在输入回调线程；
 *   - 共享模式（不抢 Exclusive，WebView 的伴奏还要混音）。
 *
 * beta12→13（2026-11-16，P11.5 压延迟）：
 *   - beta11 真机自检 RTT 105ms，用户实测体感不可过（此前「可接受」的 gate
 *     判断草率收口）。约 75ms 藏在平台层（MTK/MIUI 把 MMAP 采集静默回退
 *     legacy 路径），本版先测量 + 低成本调优，数据再定结构重构：
 *   - 输入候选扩展为 (AAudio|OpenSL) × 5 preset（新增 VoiceCommunication，
 *     部分 MTK 上走 voip 采集 profile，period 更短），start 可带 prefer 列表；
 *   - 新增 sweep 自检：逐候选开流发 4 发脉冲量 RTT，一次跑完出全矩阵对比表；
 *   - 输入流开完把客户端缓冲收到 2×burst（Oboe 低延迟实践，减积压余量）；
 *   - 遥测拆分：inApi / inBufferCapacityFrames / inCalcLatencyMs /
 *     outCalcLatencyMs，不再让 latencyMs 一笔糊涂账。
 */

#include <oboe/Oboe.h>

#include <android/log.h>
#include <jni.h>
#include <signal.h>
#include <time.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, "ElysiumOboe", __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN, "ElysiumOboe", __VA_ARGS__)

namespace elysium {

constexpr int kMaxPulses = 8;
constexpr int kMaxOnsets = 64;
constexpr int kScratchSamples = 32768;
constexpr int kLimTable = 1024;
constexpr float kLimKnee = 0.7f;
constexpr float kLimTop = 0.97f;
constexpr double kPulseStartSec = 0.9;
constexpr double kPulseGapSec = 0.85;
constexpr double kPulseMs = 60.0;
constexpr double kDiagTotalSec = 9.0;
constexpr double kPi = 3.14159265358979323846;

// sweep 自检：每候选 4 发脉冲，间隔密一点压总时长（10 候选 ≈ 30s）。
constexpr int kSweepPulses = 4;
constexpr double kSweepLeadSec = 0.7;
constexpr double kSweepGapSec = 0.5;

// 输入候选：api 0=默认（API 27+ 即 AAudio）1=显式 AAudio 2=显式 OpenSL ES；
// preset 0..4 见 presetName。默认顺序（mPrefer 为空）与 beta11 完全一致。
struct Cand {
  int api;
  int preset;
};

// 与 web karLimCurve / Java 版同曲线：|x|≤0.7 线性，之后幂式压向 0.97。
static float gLim[kLimTable + 1];

static void buildLim() {
  float k = (1.0f - kLimKnee) / (kLimTop - kLimKnee);
  for (int i = 0; i <= kLimTable; i++) {
    float a = static_cast<float>(i) / kLimTable;
    if (a <= kLimKnee) {
      gLim[i] = a;
    } else {
      float u = (a - kLimKnee) / (1.0f - kLimKnee);
      gLim[i] = kLimKnee + (kLimTop - kLimKnee) *
          static_cast<float>(1.0 - std::pow(1.0 - u, k));
    }
  }
}

static inline float limitSample(float x) {
  float a = std::min(1.0f, std::fabs(x));
  float p = a * kLimTable;
  int i = static_cast<int>(p);
  if (i >= kLimTable) return x < 0 ? -gLim[kLimTable] : gLim[kLimTable];
  float y = gLim[i] + (gLim[i + 1] - gLim[i]) * (p - i);
  return x < 0 ? -y : y;
}

static inline int64_t nowNs() {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return static_cast<int64_t>(ts.tv_sec) * 1000000000LL + ts.tv_nsec;
}

static std::string jsonEsc(const std::string& s) {
  std::string o;
  o.reserve(s.size() + 8);
  for (char c : s) {
    if (c == '"' || c == '\\') {
      o += '\\';
      o += c;
    } else if (static_cast<unsigned char>(c) < 0x20) {
      o += ' ';
    } else {
      o += c;
    }
  }
  return o;
}

/* ---- 面包屑：Java 侧经 nativeSetLogPath 给出落盘路径，闪退时按最后一条
   定位到具体流操作（beta9「唱完闪退」无法定位的教训）。低频调用，fopen/fclose
   每条都落盘，进程死了证据也留得住。JNI 入口全部 try/catch，异常逃出 JNI
   会 std::terminate（同样表现为闪退）。 ---- */
static std::string gBlogPath;

static void blogRaw(const std::string& path, const std::string& line) {
  if (path.empty()) return;
  FILE* f = fopen(path.c_str(), "a");
  if (!f) return;
  fprintf(f, "[%lld] %s\n",
          static_cast<long long>(nowNs() / 1000000LL), line.c_str());
  fclose(f);
}

static void blogLine(const std::string& line) { blogRaw(gBlogPath, line); }

// RBJ 二阶 biquad（Transposed Direct Form II），与 web/Java 版一致；只处理单声道。
struct Biquad {
  double b0 = 0, b1 = 0, b2 = 0, a1 = 0, a2 = 0, z1 = 0, z2 = 0;

  void highpass(double f, double fs) {
    double w0 = 2 * kPi * f / fs;
    double cs = std::cos(w0);
    double alpha = std::sin(w0) / (2 * 0.70710678);
    double a0 = 1 + alpha;
    b0 = (1 + cs) / 2 / a0;
    b1 = -(1 + cs) / a0;
    b2 = (1 + cs) / 2 / a0;
    a1 = -2 * cs / a0;
    a2 = (1 - alpha) / a0;
  }

  void peaking(double f, double q, double fs, double db) {
    double amp = std::pow(10, db / 40);
    double w0 = 2 * kPi * f / fs;
    double cs = std::cos(w0);
    double alpha = std::sin(w0) / (2 * q);
    double a0 = 1 + alpha / amp;
    b0 = (1 + alpha * amp) / a0;
    b1 = -2 * cs / a0;
    b2 = (1 - alpha * amp) / a0;
    a1 = -2 * cs / a0;
    a2 = (1 - alpha / amp) / a0;
  }

  inline float process(float x) {
    double y = b0 * x + z1;
    z1 = b1 * x - a1 * y + z2;
    z2 = b2 * x - a2 * y;
    return static_cast<float>(y);
  }
};

class Engine : public oboe::AudioStreamDataCallback,
               public oboe::AudioStreamErrorCallback {
 public:
  Engine() {
    buildLim();
    resetPulseStateLocked(kMaxPulses, kPulseGapSec, kPulseStartSec, 0);
  }

  ~Engine() override { stop(); }

  int start(double gain, double toneDb, bool muted);
  void stop();
  void update(double gain, double toneDb, bool muted);
  std::string statusJson();
  int startDiag();
  void stopDiag();
  int startSweep();
  void stopSweep();
  void setPrefer(const std::string& json);
  void setLogPath(const std::string& p);
  std::string lastError() { return getError(); }

  oboe::DataCallbackResult onAudioReady(oboe::AudioStream* stream, void* audioData,
                                        int32_t numFrames) override;
  void onErrorAfterClose(oboe::AudioStream* stream, oboe::Result error) override;

 private:
  enum { kModeIdle = 0, kModeMonitor = 1, kModeDiag = 2 };

  bool openStreamsLocked(std::string* detail);
  bool openOutputLocked(std::string* errs);
  bool openInputLocked(const Cand& cand, int rate, std::string* errs);
  void closeStreamsLocked();
  void closeInputLocked();
  void resetDsp(int rate);
  void resetPulseStateLocked(int pulses, double gapSec, double leadSec, int64_t outBase);
  void finishDiagLocked();
  void diagLoop();
  void sweepLoop();
  std::string diagJsonLocked();
  std::string sweepJsonLocked();
  void setError(const std::string& e);
  std::string getError();

  oboe::DataCallbackResult onInputMonitor(oboe::AudioStream* stream, int16_t* data,
                                          int32_t numFrames, int channels);
  oboe::DataCallbackResult onOutputMonitor(oboe::AudioStream* stream, int16_t* data,
                                           int32_t numFrames, int channels);
  oboe::DataCallbackResult onDiagInput(oboe::AudioStream* stream, int16_t* data,
                                       int32_t numFrames, int channels);
  oboe::DataCallbackResult onDiagOutput(oboe::AudioStream* stream, int16_t* data,
                                        int32_t numFrames, int channels);

  static int apiToInt(oboe::AudioApi api) {
    if (api == oboe::AudioApi::AAudio) return 1;
    if (api == oboe::AudioApi::OpenSLES) return 2;
    return 0;
  }
  static const char* apiName(int api) {
    if (api == 1) return "aaudio";
    if (api == 2) return "opensl";
    return "unknown";
  }
  static const char* presetName(int p) {
    if (p == 0) return "voice_performance";
    if (p == 1) return "unprocessed";
    if (p == 2) return "voice_recognition";
    if (p == 4) return "voice_communication";
    return "generic";
  }
  static oboe::InputPreset presetValue(int p) {
    if (p == 0) return oboe::InputPreset::VoicePerformance;
    if (p == 1) return oboe::InputPreset::Unprocessed;
    if (p == 2) return oboe::InputPreset::VoiceRecognition;
    if (p == 4) return oboe::InputPreset::VoiceCommunication;
    return oboe::InputPreset::Generic;
  }
  // 候选标签："opensl/voice_communication"；api 0（默认）不写前缀。
  static std::string candLabel(const Cand& c) {
    if (c.api == 1) return std::string("aaudio/") + presetName(c.preset);
    if (c.api == 2) return std::string("opensl/") + presetName(c.preset);
    return presetName(c.preset);
  }
  static const char* modeName(int m) {
    if (m == kModeMonitor) return "monitor";
    if (m == kModeDiag) return "diag";
    return "idle";
  }

  std::mutex mLock;
  std::shared_ptr<oboe::AudioStream> mIn;
  std::shared_ptr<oboe::AudioStream> mOut;
  std::atomic<bool> mRunning{false};
  std::atomic<bool> mTearingDown{false};
  std::atomic<int> mMode{kModeIdle};

  std::atomic<int> mInRate{48000};
  std::atomic<int> mOutRate{48000};
  std::atomic<int> mInBurst{0};
  std::atomic<int> mOutBurst{0};
  std::atomic<int> mInBuf{0};
  std::atomic<int> mOutBuf{0};
  std::atomic<int> mOutCap{0};
  std::atomic<int> mInCap{0};
  std::atomic<int> mAudioApi{0};
  std::atomic<int> mInApi{0};
  std::atomic<int> mInputPreset{0};
  std::vector<Cand> mPrefer;      // start 的输入候选顺序；空 = 默认（与 beta11 一致）

  std::atomic<float> mGain{1.0f};
  std::atomic<float> mToneDb{0.0f};
  std::atomic<bool> mMuted{false};

  Biquad mHpf;
  Biquad mTone;
  float mCurGain = 0.0f;
  float mCurMute = 0.0f;
  float mAppliedTone = 0.0f;

  std::atomic<float> mInRms{0.0f};
  std::atomic<float> mOutRms{0.0f};
  std::atomic<uint64_t> mFramesRead{0};
  std::atomic<uint64_t> mFramesWritten{0};

  // 输入 → 输出的小 SPSC 环；容量取 2 的幂，正常占用≈prefill。
  std::vector<int16_t> mRing;
  uint32_t mRingMask = 0;
  int mRingCapacity = 0;
  int mPrefillFrames = 0;
  std::atomic<uint32_t> mWritePos{0};
  std::atomic<uint32_t> mReadPos{0};
  std::atomic<uint64_t> mRingUnderruns{0};
  std::atomic<uint64_t> mRingOverruns{0};

  // ---- 自检状态 ----
  std::atomic<bool> mDiagRunning{false};
  std::atomic<bool> mDiagDone{false};
  std::atomic<int> mDiagPulseCount{0};
  std::atomic<int> mDiagOnsetCount{0};
  std::atomic<int64_t> mDiagPulseNs[kMaxPulses];
  std::atomic<int64_t> mDiagOnsetNs[kMaxOnsets];
  std::atomic<int64_t> mDiagOutFrames{0};
  std::atomic<int> mDiagLockoutFrames{0};
  // 脉冲表参数（sweep 与普通 diag 共用一套回调）：写完才 start，回调只读。
  int mDiagActivePulses = kMaxPulses;
  double mDiagGapSec = kPulseGapSec;

  int64_t mDiagPulseStartFrame[kMaxPulses] = {0};
  int64_t mDiagPulseLenFrames = 0;
  int64_t mDiagEdgeFrames = 0;
  int mDiagCalibFrames = 0;
  double mDiagNoiseSumSq = 0.0;
  float mDiagThreshold = 0.02f;
  std::array<int16_t, kScratchSamples> mDiagScratch{};

  double mDiagRtt = -1.0;
  double mDiagMin = -1.0;
  double mDiagMax = -1.0;
  int mDiagHitCount = 0;
  double mDiagHits[kMaxPulses] = {0};
  double mDiagInLatencyMs = -1.0;
  double mDiagOutLatencyMs = -1.0;
  int mDiagXruns = 0;
  std::string mDiagError;

  std::thread mDiagThread;
  std::atomic<bool> mDiagThreadRunning{false};

  // ---- sweep 自检（beta12→13）：逐候选开输入流量 RTT，输出流全程复用 ----
  struct SweepRow {
    std::string label;
    double rtt = -1, mn = -1, mx = -1;
    int hits = 0;
    int inBurst = 0, inCap = 0;
    double inLat = -1, outLat = -1;
    int xruns = 0;
    bool ok = false;
    std::string err;
  };
  std::atomic<bool> mSweepRunning{false};
  std::atomic<bool> mSweepDone{false};
  std::atomic<int> mSweepIdx{-1};
  std::vector<Cand> mSweepList;
  std::vector<SweepRow> mSweepRows;   // mLock 保护
  std::string mSweepError;            // mLock 保护
  std::thread mSweepThread;

  mutable std::mutex mErrLock;
  std::string mLastError;
};

/* ---------------- 生命周期 ---------------- */

int Engine::start(double gain, double toneDb, bool muted) {
  stop();
  blogLine("start begin");

  std::lock_guard<std::mutex> lock(mLock);
  setError("");
  resetPulseStateLocked(kMaxPulses, kPulseGapSec, kPulseStartSec, 0);
  mGain.store(static_cast<float>(std::max(0.0, std::min(4.0, gain))));
  mToneDb.store(static_cast<float>(std::max(-12.0, std::min(12.0, toneDb))));
  mMuted.store(muted);

  std::string detail;
  mMode.store(kModeMonitor);
  if (!openStreamsLocked(&detail)) {
    mMode.store(kModeIdle);
    setError(detail);
    blogLine("start open failed: " + detail);
    return -1;
  }
  {
    char b[160];
    std::snprintf(b, sizeof(b),
                  "start streams ok rate=%d inBurst=%d outBurst=%d api=%s preset=%s",
                  mInRate.load(), mInBurst.load(), mOutBurst.load(),
                  apiName(mAudioApi.load()), presetName(mInputPreset.load()));
    blogLine(b);
  }

  const int outBurst = std::max(32, mOutBurst.load());
  const int inBurst = std::max(32, mInBurst.load());
  int cap = std::max(4096, 8 * std::max(outBurst, inBurst));
  int pow2 = 1;
  while (pow2 < cap) pow2 <<= 1;
  cap = pow2;
  mRing.assign(static_cast<size_t>(cap), 0);
  mRingCapacity = cap;
  mRingMask = static_cast<uint32_t>(cap - 1);
  mPrefillFrames = std::max(2 * 128, std::min(cap / 2, 2 * outBurst));
  mWritePos.store(0);
  mReadPos.store(0);
  mRingUnderruns.store(0);
  mRingOverruns.store(0);

  mTearingDown.store(false);
  mRunning.store(true);
  oboe::Result r = mIn->requestStart();
  if (r != oboe::Result::OK) {
    mRunning.store(false);
    mMode.store(kModeIdle);
    closeStreamsLocked();
    setError(std::string("input start: ") + oboe::convertToText(r));
    return -1;
  }

  // 先让输入填到预填量，再启动输出，避免一上来就 underrun。
  const int64_t deadline = nowNs() + 150LL * 1000000LL;
  while (mRunning.load() && nowNs() < deadline) {
    const int32_t level = static_cast<int32_t>(
        mWritePos.load(std::memory_order_acquire) - mReadPos.load(std::memory_order_relaxed));
    if (level >= mPrefillFrames) break;
    std::this_thread::sleep_for(std::chrono::microseconds(500));
  }

  r = mOut->requestStart();
  if (r != oboe::Result::OK) {
    mRunning.store(false);
    mMode.store(kModeIdle);
    closeStreamsLocked();
    setError(std::string("output start: ") + oboe::convertToText(r));
    return -1;
  }

  mInBurst.store(mIn->getFramesPerBurst());
  mInBuf.store(mIn->getBufferSizeInFrames());
  mOutBurst.store(mOut->getFramesPerBurst());
  mOutBuf.store(mOut->getBufferSizeInFrames());
  mOutCap.store(mOut->getBufferCapacityInFrames());
  return 0;
}

void Engine::stop() {
  std::thread dt, st;
  blogLine("stop begin");
  {
    std::lock_guard<std::mutex> lock(mLock);
    mDiagRunning.store(false);
    mSweepRunning.store(false);
    closeStreamsLocked();
    if (mDiagThread.joinable()) dt = std::move(mDiagThread);
    if (mSweepThread.joinable()) st = std::move(mSweepThread);
  }
  if (dt.joinable()) {
    dt.join();
    blogLine("stop diag thread joined");
  }
  if (st.joinable()) {
    st.join();
    blogLine("stop sweep thread joined");
  }
  blogLine("stop end");
}

int Engine::startDiag() {
  stopDiag();  // 先收掉上一轮自检线程/流；不动 monitor（下面会拒绝）
  blogLine("diag begin");

  std::lock_guard<std::mutex> lock(mLock);
  if (mRunning.load() || mSweepRunning.load()) return -2;

  setError("");
  mMode.store(kModeDiag);

  std::string detail;
  if (!openStreamsLocked(&detail)) {
    mMode.store(kModeIdle);
    setError(detail);
    return -1;
  }
  // 脉冲表在流开完、采样率确定后排（startDiag 用满 8 发、0.85s 间隔）。
  resetPulseStateLocked(kMaxPulses, kPulseGapSec, kPulseStartSec, 0);

  mTearingDown.store(false);
  mDiagRunning.store(true);
  mRunning.store(true);

  oboe::Result r = mIn->requestStart();
  if (r != oboe::Result::OK) {
    mDiagRunning.store(false);
    mRunning.store(false);
    mMode.store(kModeIdle);
    closeStreamsLocked();
    setError(std::string("input start: ") + oboe::convertToText(r));
    return -1;
  }
  r = mOut->requestStart();
  if (r != oboe::Result::OK) {
    mDiagRunning.store(false);
    mRunning.store(false);
    mMode.store(kModeIdle);
    closeStreamsLocked();
    setError(std::string("output start: ") + oboe::convertToText(r));
    return -1;
  }

  mInBurst.store(mIn->getFramesPerBurst());
  mInBuf.store(mIn->getBufferSizeInFrames());
  mInCap.store(mIn->getBufferCapacityInFrames());
  mOutBurst.store(mOut->getFramesPerBurst());
  mOutBuf.store(mOut->getBufferSizeInFrames());
  mOutCap.store(mOut->getBufferCapacityInFrames());

  mDiagThreadRunning.store(true);
  mDiagThread = std::thread([this] { diagLoop(); });
  blogLine("diag started");
  return 0;
}

// 自检计时线程：输入/输出回调负责检测/发声，这里只负责到点停流、匹配结果。
void Engine::stopDiag() {
  std::thread dt, st;
  {
    std::lock_guard<std::mutex> lock(mLock);
    mDiagRunning.store(false);
    mSweepRunning.store(false);
    if (mMode.load() == kModeDiag) closeStreamsLocked();
    if (mDiagThread.joinable()) dt = std::move(mDiagThread);
    if (mSweepThread.joinable()) st = std::move(mSweepThread);
  }
  if (dt.joinable()) dt.join();
  if (st.joinable()) st.join();
}

/* ---------------- sweep 自检（beta12→13） ----------------
   逐候选（AAudio/OpenSL × 5 preset）开输入流，各发 4 发脉冲量 RTT；
   输出流开一条全程复用，候选间只收/开输入。结果从 status().sweep 轮询。 */

int Engine::startSweep() {
  stop();   // monitor / 旧 sweep / 旧 diag 一并收掉
  blogLine("sweep begin");

  std::lock_guard<std::mutex> lock(mLock);
  setError("");
  resetPulseStateLocked(kMaxPulses, kPulseGapSec, kPulseStartSec, 0);
  mSweepRows.clear();
  mSweepError.clear();
  mSweepIdx.store(-1);
  // 全矩阵：显式 AAudio 与 OpenSL 各 × 5 preset。api 0（默认）与显式 AAudio
  // 在 API 27+ 等价，不重复测。
  mSweepList = {{1, 0}, {1, 1}, {1, 2}, {1, 3}, {1, 4},
                {2, 0}, {2, 1}, {2, 2}, {2, 3}, {2, 4}};
  mMode.store(kModeDiag);          // 复用 diag 回调派发
  mSweepDone.store(false);
  mSweepRunning.store(true);
  mSweepThread = std::thread([this] { sweepLoop(); });
  blogLine("sweep started");
  return 0;
}

void Engine::stopSweep() {
  mSweepRunning.store(false);
  std::thread st;
  {
    std::lock_guard<std::mutex> lock(mLock);
    mDiagRunning.store(false);
    if (mMode.load() == kModeDiag) closeStreamsLocked();
    if (mSweepThread.joinable()) st = std::move(mSweepThread);
  }
  if (st.joinable()) st.join();
}

void Engine::sweepLoop() {
  blogLine("sweep thread begin");
  const std::vector<Cand> list = mSweepList;

  {
    // 输出流只开一次、start 一次，跨候选复用（采样率/脉冲基准一致）。
    // 候选间隙 mDiagRunning=false → 回调走闲置清零，不发声。
    std::lock_guard<std::mutex> lock(mLock);
    std::string errs;
    if (!openOutputLocked(&errs)) {
      mSweepError = errs.empty() ? "no oboe output" : errs;
      mSweepRunning.store(false);
      mSweepDone.store(true);
      blogLine("sweep output open failed");
      return;
    }
    int rate = mOut->getSampleRate();
    if (rate <= 0) rate = 48000;
    mOutRate.store(rate);
    mOutBurst.store(mOut->getFramesPerBurst());
    mOutBuf.store(mOut->getBufferSizeInFrames());
    mOutCap.store(mOut->getBufferCapacityInFrames());
    mAudioApi.store(apiToInt(mOut->getAudioApi()));
    mTearingDown.store(false);
    mMode.store(kModeDiag);
    mRunning.store(true);
    oboe::Result r = mOut->requestStart();
    if (r != oboe::Result::OK) {
      mRunning.store(false);
      mSweepError = std::string("output start: ") + oboe::convertToText(r);
      mSweepRunning.store(false);
      mSweepDone.store(true);
      closeStreamsLocked();
      blogLine("sweep output start failed");
      return;
    }
  }

  for (size_t i = 0; i < list.size(); i++) {
    if (!mSweepRunning.load(std::memory_order_relaxed)) break;
    mSweepIdx.store(static_cast<int>(i), std::memory_order_relaxed);
    SweepRow row;
    row.label = candLabel(list[i]);

    {
      std::lock_guard<std::mutex> lock(mLock);
      if (!mSweepRunning.load(std::memory_order_relaxed)) break;
      std::string errs;
      if (!openInputLocked(list[i], mOutRate.load(), &errs)) {
        row.err = errs.empty() ? "open failed" : errs;
        mSweepRows.push_back(row);
        blogLine("sweep " + row.label + " open failed");
        continue;
      }
      // 输出回调从启动到现在累计的帧数就是本候选的脉冲基准。
      resetPulseStateLocked(kSweepPulses, kSweepGapSec, kSweepLeadSec,
                            mDiagOutFrames.load());
      mTearingDown.store(false);
      mDiagRunning.store(true);
      mRunning.store(true);
      oboe::Result r = mIn->requestStart();
      if (r != oboe::Result::OK) {
        row.err = std::string("input start: ") + oboe::convertToText(r);
        closeInputLocked();
        mSweepRows.push_back(row);
        blogLine("sweep " + row.label + " start failed");
        continue;
      }
    }

    // 跑满本候选：lead + (n-1)×gap + 脉宽 + 尾巴。
    const double total = kSweepLeadSec + (kSweepPulses - 1) * kSweepGapSec
        + kPulseMs / 1000.0 + 0.45;
    const int64_t deadline = nowNs() + static_cast<int64_t>(total * 1e9);
    while (mDiagRunning.load(std::memory_order_relaxed) && nowNs() < deadline) {
      std::this_thread::sleep_for(std::chrono::milliseconds(40));
    }

    {
      std::lock_guard<std::mutex> lock(mLock);
      if (mIn) {
        auto l = mIn->calculateLatencyMillis();
        if (l) mDiagInLatencyMs = l.value();
        auto x = mIn->getXRunCount();
        if (x) mDiagXruns += x.value();
      }
      if (mOut) {
        auto l = mOut->calculateLatencyMillis();
        if (l) mDiagOutLatencyMs = l.value();
        auto x = mOut->getXRunCount();
        if (x) mDiagXruns += x.value();
      }
      row.inBurst = mInBurst.load();
      row.inCap = mInCap.load();
      row.inLat = mDiagInLatencyMs;
      row.outLat = mDiagOutLatencyMs;
      row.xruns = mDiagXruns;
      closeInputLocked();
      finishDiagLocked();
      row.rtt = mDiagRtt;
      row.mn = mDiagMin;
      row.mx = mDiagMax;
      row.hits = mDiagHitCount;
      row.ok = mDiagHitCount >= 2 && mDiagRtt > 0;
      if (mSweepRunning.load(std::memory_order_relaxed)) mSweepRows.push_back(row);
    }
    {
      char b[160];
      std::snprintf(b, sizeof(b), "sweep %s rtt=%.1f hits=%d xruns=%d",
                    row.label.c_str(), row.rtt, row.hits, row.xruns);
      blogLine(b);
    }
  }

  {
    std::lock_guard<std::mutex> lock(mLock);
    if (mMode.load() == kModeDiag) closeStreamsLocked();   // 收输出（输入已逐个关）
    mDiagRunning.store(false);
    mMode.store(kModeIdle);
  }
  if (mSweepRunning.exchange(false)) mSweepDone.store(true);
  mSweepIdx.store(-1);
  blogLine("sweep thread end");
}

void Engine::update(double gain, double toneDb, bool muted) {
  mGain.store(static_cast<float>(std::max(0.0, std::min(4.0, gain))));
  mToneDb.store(static_cast<float>(std::max(-12.0, std::min(12.0, toneDb))));
  mMuted.store(muted);
}

/* ---------------- 流打开 ---------------- */

// 输出流（mono → stereo），开成一条就停；采样率以它为准，输入按同速率开。
bool Engine::openOutputLocked(std::string* errs) {
  const int32_t outChannels[] = {1, 2};
  for (int c = 0; c < 2; c++) {
    oboe::AudioStreamBuilder b;
    b.setDirection(oboe::Direction::Output);
    b.setPerformanceMode(oboe::PerformanceMode::LowLatency);
    b.setSharingMode(oboe::SharingMode::Shared);
    b.setFormat(oboe::AudioFormat::I16);
    b.setChannelCount(outChannels[c]);
    b.setUsage(oboe::Usage::Media);
    b.setContentType(oboe::ContentType::Music);
    b.setDataCallback(this);
    b.setErrorCallback(this);

    std::shared_ptr<oboe::AudioStream> s;
    oboe::Result r = b.openStream(s);
    if (r != oboe::Result::OK || !s) {
      *errs += "out/";
      *errs += (c == 0 ? "mono" : "stereo");
      *errs += ": ";
      *errs += oboe::convertToText(r);
      *errs += "; ";
      continue;
    }
    if (s->getFormat() != oboe::AudioFormat::I16) {
      *errs += "out format != I16; ";
      s->close();
      continue;
    }
    mOut = s;
    mOutBurst.store(s->getFramesPerBurst());
    mOutBuf.store(s->getBufferSizeInFrames());
    mOutCap.store(s->getBufferCapacityInFrames());
    mAudioApi.store(apiToInt(s->getAudioApi()));
    return true;
  }
  return false;
}

// 输入流：按候选（api × preset）开，mono → stereo；开成即记遥测并收缓冲。
bool Engine::openInputLocked(const Cand& cand, int rate, std::string* errs) {
  const int32_t inChannels[] = {1, 2};
  for (int c = 0; c < 2; c++) {
    oboe::AudioStreamBuilder b;
    b.setDirection(oboe::Direction::Input);
    b.setPerformanceMode(oboe::PerformanceMode::LowLatency);
    b.setSharingMode(oboe::SharingMode::Shared);
    b.setFormat(oboe::AudioFormat::I16);
    b.setSampleRate(rate);
    b.setChannelCount(inChannels[c]);
    b.setInputPreset(presetValue(cand.preset));
    if (cand.api == 1) b.setAudioApi(oboe::AudioApi::AAudio);
    else if (cand.api == 2) b.setAudioApi(oboe::AudioApi::OpenSLES);
    b.setDataCallback(this);
    b.setErrorCallback(this);

    std::shared_ptr<oboe::AudioStream> s;
    oboe::Result r = b.openStream(s);
    if (r != oboe::Result::OK || !s) {
      *errs += "in/";
      *errs += presetName(cand.preset);
      *errs += "/";
      *errs += (c == 0 ? "mono" : "stereo");
      *errs += ": ";
      *errs += oboe::convertToText(r);
      *errs += "; ";
      continue;
    }
    if (s->getFormat() != oboe::AudioFormat::I16) {
      *errs += "in format != I16; ";
      s->close();
      continue;
    }
    int inRate = s->getSampleRate();
    if (inRate <= 0) inRate = rate;
    if (inRate != rate) {
      *errs += "in rate mismatch: ";
      *errs += std::to_string(inRate);
      *errs += " vs ";
      *errs += std::to_string(rate);
      *errs += "; ";
      s->close();
      continue;
    }
    // 输入客户端缓冲收到 2×burst（Oboe 低延迟实践）：legacy 路径上积压在
    // 客户端缓冲里的帧全算延迟。不支持/失败就保持默认，只影响余量不致命。
    const int32_t burst = s->getFramesPerBurst();
    const int32_t cap = s->getBufferCapacityInFrames();
    if (burst > 0 && cap > 0 && cap > 2 * burst) {
      auto rr = s->setBufferSizeInFrames(std::min(cap, 2 * burst));
      if (rr != oboe::Result::OK) {
        blogLine(std::string("in trim buffer failed: ") + oboe::convertToText(rr));
      }
    }
    mIn = s;
    mInputPreset.store(cand.preset);
    mInApi.store(apiToInt(s->getAudioApi()));
    mInRate.store(inRate);
    mInBurst.store(s->getFramesPerBurst());
    mInBuf.store(s->getBufferSizeInFrames());
    mInCap.store(s->getBufferCapacityInFrames());
    return true;
  }
  return false;
}

bool Engine::openStreamsLocked(std::string* detail) {
  std::string errs;

  // 输出先开：拿到设备原生采样率，输入按同一速率开，环里的帧率才一致。
  if (!openOutputLocked(&errs)) {
    *detail = errs.empty() ? "no oboe output" : errs;
    return false;
  }
  int rate = mOut->getSampleRate();
  if (rate <= 0) rate = 48000;
  mOutRate.store(rate);

  // 输入候选顺序：mPrefer（页面/自检给的最优序）优先；为空保持 beta11 的
  // VoicePerformance → Unprocessed → VoiceRecognition → Generic。
  std::vector<Cand> list = mPrefer;
  if (list.empty()) {
    list = {{0, 0}, {0, 1}, {0, 2}, {0, 3}};
  }
  for (const Cand& cand : list) {
    if (openInputLocked(cand, rate, &errs)) {
      resetDsp(mInRate.load());
      return true;
    }
  }
  closeStreamsLocked();
  *detail = errs.empty() ? "no oboe input" : errs;
  return false;
}

void Engine::closeStreamsLocked() {
  // Android 15 legacy 输入路径的 close 不等在飞回调结束（beta10 真机 SIGSEGV
  // 实证：唱完收口时 mIn->close 解映射 AudioRecord 缓冲，在飞回调还在上面
  // memset）。所以：先树收口旗（在飞回调过检查点后一律不碰 data）→ 双流
  // requestStop（不阻塞）→ 留 120ms 宽限期让在飞回调退出（缓冲还映射着）
  // → 再逐条 close。mTearingDown 由 start/startDiag 复位。
  mTearingDown.store(true, std::memory_order_release);
  mRunning.store(false);
  mMode.store(kModeIdle);
  if (mOut) mOut->requestStop();
  if (mIn) mIn->requestStop();
  blogLine("stop: requests sent, grace 120ms");
  std::this_thread::sleep_for(std::chrono::milliseconds(120));
  blogLine("stop: grace done");
  if (mOut) {
    auto r = mOut->close();
    blogLine(std::string("stop: out closed r=") + oboe::convertToText(r));
    mOut.reset();
  }
  if (mIn) {
    auto r = mIn->close();
    blogLine(std::string("stop: in closed r=") + oboe::convertToText(r));
    mIn.reset();
  }
  blogLine("stop: streams released");
}

// sweep 专用的输入单边收口：输出流跨候选复用不动，只收输入。
// Android 15 legacy 输入路径的 close 竞态协议与 closeStreamsLocked 相同。
void Engine::closeInputLocked() {
  mTearingDown.store(true, std::memory_order_release);
  mRunning.store(false);
  mDiagRunning.store(false);
  if (mIn) {
    mIn->requestStop();
    blogLine("sweep: in stop, grace 120ms");
    std::this_thread::sleep_for(std::chrono::milliseconds(120));
    auto r = mIn->close();
    blogLine(std::string("sweep: in closed r=") + oboe::convertToText(r));
    mIn.reset();
  }
}

void Engine::resetDsp(int rate) {
  mHpf = Biquad();
  mTone = Biquad();
  mHpf.highpass(80.0, rate);
  mTone.peaking(2500.0, 0.9, rate, mToneDb.load());
  mAppliedTone = mToneDb.load();
  mCurGain = 0.0f;   // 从 0 ramp 起，避免起播咔哒
  mCurMute = 0.0f;
  mInRms.store(0.0f);
  mOutRms.store(0.0f);
  mFramesRead.store(0);
  mFramesWritten.store(0);
}

/* ---------------- 音频回调 ---------------- */

oboe::DataCallbackResult Engine::onAudioReady(oboe::AudioStream* stream,
                                              void* audioData, int32_t numFrames) {
  auto* data = static_cast<int16_t*>(audioData);
  const int channels = std::max(1, stream == nullptr ? 1 : stream->getChannelCount());
  const bool isOutput = (stream != nullptr && stream == mOut.get());

  // 收口期/闲置：输入绝不碰 data（Android 15 legacy 输入路径的 close 不等在飞
  // 回调结束，缓冲可能已解映射，beta10 真机 SIGSEGV 实证）；输出填零——sweep
  // 候选间隙输出流还在跑，不填零会把上一块残帧反复外放成爆音。输出缓冲在
  // 回调期间必然有效，写零安全。
  if (mTearingDown.load(std::memory_order_relaxed) ||
      !mRunning.load(std::memory_order_relaxed)) {
    if (isOutput && data != nullptr && numFrames > 0) {
      std::memset(data, 0, static_cast<size_t>(numFrames) * channels * sizeof(int16_t));
    }
    return oboe::DataCallbackResult::Continue;
  }

  const int mode = mMode.load(std::memory_order_relaxed);

  if (mode == kModeMonitor) {
    return isOutput
        ? onOutputMonitor(stream, data, numFrames, channels)
        : onInputMonitor(stream, data, numFrames, channels);
  }
  if (mode == kModeDiag && mDiagRunning.load(std::memory_order_relaxed)) {
    return isOutput
        ? onDiagOutput(stream, data, numFrames, channels)
        : onDiagInput(stream, data, numFrames, channels);
  }

  // 闲置（sweep 候选间隙等）：输出清零防上一块残帧复播成爆音；输入不碰 data。
  if (isOutput && data != nullptr && numFrames > 0) {
    std::memset(data, 0, static_cast<size_t>(numFrames) * channels * sizeof(int16_t));
  }
  return oboe::DataCallbackResult::Continue;
}

oboe::DataCallbackResult Engine::onInputMonitor(oboe::AudioStream* /*stream*/, int16_t* data,
                                                int32_t numFrames, int channels) {
  const int rate = mInRate.load(std::memory_order_relaxed);
  float gTarget = mGain.load(std::memory_order_relaxed);
  float mTarget = mMuted.load(std::memory_order_relaxed) ? 0.0f : 1.0f;
  float toneTarget = mToneDb.load(std::memory_order_relaxed);
  if (toneTarget != mAppliedTone) {
    mTone.peaking(2500.0, 0.9, rate, toneTarget);
    mAppliedTone = toneTarget;
  }

  uint32_t wp = mWritePos.load(std::memory_order_relaxed);
  uint32_t rp = mReadPos.load(std::memory_order_acquire);
  const int used = static_cast<int32_t>(wp - rp);
  const int n = std::min(numFrames, mRingCapacity);
  if (n <= 0) return oboe::DataCallbackResult::Continue;
  const bool canWrite = (used + n <= mRingCapacity);
  if (!canWrite) {
    mRingOverruns.fetch_add(static_cast<uint64_t>(n), std::memory_order_relaxed);
  }

  const float gStep = (gTarget - mCurGain) / static_cast<float>(n);
  const float mStep = (mTarget - mCurMute) / static_cast<float>(n);
  double sumIn = 0;
  double sumOut = 0;
  const uint32_t mask = mRingMask;

  for (int i = 0; i < n; i++) {
    float x = 0.0f;
    for (int c = 0; c < channels; c++) x += data[i * channels + c];
    x /= (32768.0f * channels);
    sumIn += static_cast<double>(x) * x;
    x = mHpf.process(x);
    mCurGain += gStep;
    x *= mCurGain;
    x = mTone.process(x);
    mCurMute += mStep;
    x *= mCurMute;
    x = limitSample(x);
    sumOut += static_cast<double>(x) * x;
    auto s = static_cast<int16_t>(std::max(-32768.0f, std::min(32767.0f, x * 32767.0f)));
    if (canWrite) mRing[(wp + i) & mask] = s;
  }
  if (canWrite) mWritePos.store(wp + static_cast<uint32_t>(n), std::memory_order_release);

  const float blockIn = static_cast<float>(std::sqrt(sumIn / static_cast<double>(n)));
  const float blockOut = static_cast<float>(std::sqrt(sumOut / static_cast<double>(n)));
  mInRms.store(mInRms.load(std::memory_order_relaxed) * 0.7f + blockIn * 0.3f);
  mOutRms.store(mOutRms.load(std::memory_order_relaxed) * 0.7f + blockOut * 0.3f);
  mFramesRead.fetch_add(static_cast<uint64_t>(n), std::memory_order_relaxed);
  return oboe::DataCallbackResult::Continue;
}

oboe::DataCallbackResult Engine::onOutputMonitor(oboe::AudioStream* /*stream*/, int16_t* data,
                                                 int32_t numFrames, int channels) {
  uint32_t rp = mReadPos.load(std::memory_order_relaxed);
  uint32_t wp = mWritePos.load(std::memory_order_acquire);
  const int avail = static_cast<int32_t>(wp - rp);
  const int n = std::min(numFrames, mRingCapacity);
  const uint32_t mask = mRingMask;

  if (avail >= n) {
    for (int i = 0; i < n; i++) {
      const int16_t s = mRing[(rp + i) & mask];
      for (int c = 0; c < channels; c++) data[i * channels + c] = s;
    }
    if (n < numFrames) {
      std::memset(data + n * channels, 0,
                  static_cast<size_t>(numFrames - n) * channels * sizeof(int16_t));
    }
    mReadPos.store(rp + static_cast<uint32_t>(n), std::memory_order_release);
  } else {
    std::memset(data, 0, static_cast<size_t>(numFrames) * channels * sizeof(int16_t));
    mRingUnderruns.fetch_add(static_cast<uint64_t>(numFrames), std::memory_order_relaxed);
  }
  mFramesWritten.fetch_add(static_cast<uint64_t>(numFrames), std::memory_order_relaxed);
  return oboe::DataCallbackResult::Continue;
}

oboe::DataCallbackResult Engine::onDiagInput(oboe::AudioStream* /*stream*/, int16_t* data,
                                             int32_t numFrames, int channels) {
  const int rate = std::max(1, mInRate.load(std::memory_order_relaxed));
  const int64_t now = nowNs();
  const int total = numFrames * channels;
  if (total > 0 && total <= kScratchSamples) {
    std::memcpy(mDiagScratch.data(), data, static_cast<size_t>(total) * sizeof(int16_t));
  }

  // 前 500ms 用 RMS 估本底，避免启动爆音把峰值阈值顶到测不到脉冲。
  if (mDiagCalibFrames < rate / 2) {
    if (total > 0 && total <= kScratchSamples) {
      for (int i = 0; i < numFrames; i++) {
        double x = 0.0;
        for (int c = 0; c < channels; c++) {
          x += mDiagScratch[static_cast<size_t>(i) * channels + c];
        }
        x /= (32768.0 * channels);
        mDiagNoiseSumSq += x * x;
      }
    }
    mDiagCalibFrames += numFrames;
    if (mDiagCalibFrames >= rate / 2) {
      double frames = std::max(1, mDiagCalibFrames);
      double rms = std::sqrt(mDiagNoiseSumSq / frames);
      mDiagThreshold = static_cast<float>(std::max(rms * 4.0, 0.02));
    }
    return oboe::DataCallbackResult::Continue;
  }

  int lockout = mDiagLockoutFrames.load(std::memory_order_relaxed);
  if (lockout > 0) {
    mDiagLockoutFrames.store(std::max(0, lockout - numFrames), std::memory_order_relaxed);
    return oboe::DataCallbackResult::Continue;
  }

  if (total > 0 && total <= kScratchSamples) {
    for (int i = 0; i < numFrames; i++) {
      double x = 0.0;
      for (int c = 0; c < channels; c++) {
        x += mDiagScratch[static_cast<size_t>(i) * channels + c];
      }
      double a = std::fabs(x / (32768.0 * channels));
      if (a > mDiagThreshold) {
        int n = mDiagOnsetCount.fetch_add(1, std::memory_order_relaxed);
        if (n < kMaxOnsets) {
          const int64_t onset = now -
              static_cast<int64_t>(((numFrames - i) / static_cast<double>(rate)) * 1e9);
          mDiagOnsetNs[n].store(onset, std::memory_order_relaxed);
        }
        mDiagLockoutFrames.store(static_cast<int>(0.3 * rate), std::memory_order_relaxed);
        break;
      }
    }
  }
  return oboe::DataCallbackResult::Continue;
}

oboe::DataCallbackResult Engine::onDiagOutput(oboe::AudioStream* /*stream*/, int16_t* data,
                                              int32_t numFrames, int channels) {
  const int rate = std::max(1, mOutRate.load(std::memory_order_relaxed));
  const int64_t now = nowNs();
  const int64_t outBase = mDiagOutFrames.load(std::memory_order_relaxed);
  std::memset(data, 0, static_cast<size_t>(numFrames) * channels * sizeof(int16_t));

  for (int i = 0; i < numFrames; i++) {
    const int64_t abs = outBase + i;
    float sample = 0.0f;
    for (int p = 0; p < mDiagActivePulses; p++) {
      const int64_t start = mDiagPulseStartFrame[p];
      const int64_t end = start + mDiagPulseLenFrames;
      if (abs < start || abs >= end) continue;
      const int64_t into = abs - start;
      const int64_t left = end - abs;
      double env = 1.0;
      if (into < mDiagEdgeFrames) env = static_cast<double>(into) / mDiagEdgeFrames;
      if (left < mDiagEdgeFrames) env = std::min(env, static_cast<double>(left) / mDiagEdgeFrames);
      const double phase = 2 * kPi * 1000.0 * static_cast<double>(into) / rate;
      sample += static_cast<float>(std::sin(phase) * env * 0.8);
      if (abs == start) {
        auto ns = static_cast<int64_t>((i / static_cast<double>(rate)) * 1e9);
        mDiagPulseNs[p].store(now + ns, std::memory_order_relaxed);
        mDiagPulseCount.fetch_add(1, std::memory_order_relaxed);
      }
    }
    auto s = static_cast<int16_t>(std::max(-1.0f, std::min(1.0f, sample)) * 32767.0f);
    for (int c = 0; c < channels; c++) data[i * channels + c] = s;
  }
  mDiagOutFrames.store(outBase + numFrames, std::memory_order_relaxed);
  return oboe::DataCallbackResult::Continue;
}

/* ---------------- 自检结果 ---------------- */

void Engine::resetPulseStateLocked(int pulses, double gapSec, double leadSec,
                                   int64_t outBase) {
  mDiagRunning.store(false);
  mDiagDone.store(false);
  mDiagPulseCount.store(0);
  mDiagOnsetCount.store(0);
  mDiagActivePulses = std::max(1, std::min(kMaxPulses, pulses));
  mDiagGapSec = gapSec;
  for (int i = 0; i < kMaxPulses; i++) {
    mDiagPulseNs[i].store(0);
    mDiagHits[i] = 0;
    mDiagPulseStartFrame[i] = 0;
  }
  for (int i = 0; i < kMaxOnsets; i++) mDiagOnsetNs[i].store(0);
  mDiagOutFrames.store(outBase);
  mDiagLockoutFrames.store(0);
  mDiagPulseLenFrames = 0;
  mDiagEdgeFrames = 0;
  mDiagCalibFrames = 0;
  mDiagNoiseSumSq = 0;
  mDiagThreshold = 0.02f;
  mDiagRtt = -1;
  mDiagMin = -1;
  mDiagMax = -1;
  mDiagHitCount = 0;
  mDiagInLatencyMs = -1;
  mDiagOutLatencyMs = -1;
  mDiagXruns = 0;
  mDiagError.clear();

  // 脉冲表：outBase 是输出回调已发的帧数（sweep 跨候选连续），普通 diag 为 0。
  const int rate = std::max(1, mOutRate.load());
  mDiagPulseLenFrames = static_cast<int64_t>(kPulseMs / 1000.0 * rate);
  mDiagEdgeFrames = static_cast<int64_t>(0.005 * rate);
  for (int p = 0; p < mDiagActivePulses; p++) {
    mDiagPulseStartFrame[p] = outBase +
        static_cast<int64_t>((leadSec + p * gapSec) * rate);
  }
}

void Engine::finishDiagLocked() {
  const int pulseCount = std::min(mDiagPulseCount.load(), kMaxPulses);
  const int onsetCount = std::min(mDiagOnsetCount.load(), kMaxOnsets);
  bool used[kMaxOnsets] = {false};
  double hits[kMaxPulses] = {0};
  int hitCount = 0;

  for (int p = 0; p < pulseCount; p++) {
    const int64_t pn = mDiagPulseNs[p].load();
    if (pn <= 0) continue;
    for (int i = 0; i < onsetCount; i++) {
      if (used[i]) continue;
      const int64_t on = mDiagOnsetNs[i].load();
      const double dMs = static_cast<double>(on - pn) / 1e6;
      if (dMs >= 5.0 && dMs <= 600.0) {
        used[i] = true;
        hits[hitCount++] = dMs;
        break;
      }
    }
  }

  mDiagHitCount = hitCount;
  mDiagRtt = -1;
  mDiagMin = -1;
  mDiagMax = -1;
  for (int i = 0; i < hitCount; i++) mDiagHits[i] = hits[i];
  if (hitCount > 0) {
    std::sort(hits, hits + hitCount);
    mDiagRtt = (hitCount % 2 == 1)
        ? hits[hitCount / 2]
        : (hits[hitCount / 2 - 1] + hits[hitCount / 2]) / 2.0;
    mDiagMin = hits[0];
    mDiagMax = hits[hitCount - 1];
  }
}

void Engine::diagLoop() {
  blogLine("diag thread begin");
  const int64_t deadline = nowNs() +
      static_cast<int64_t>((kDiagTotalSec + 0.8) * 1e9);
  bool timedOut = false;
  while (mDiagRunning.load(std::memory_order_relaxed)) {
    if (nowNs() >= deadline) {
      timedOut = true;
      break;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }

  std::lock_guard<std::mutex> lock(mLock);
  if (mIn) {
    auto l = mIn->calculateLatencyMillis();
    if (l) mDiagInLatencyMs = l.value();
    auto x = mIn->getXRunCount();
    if (x) mDiagXruns += x.value();
  }
  if (mOut) {
    auto l = mOut->calculateLatencyMillis();
    if (l) mDiagOutLatencyMs = l.value();
    auto x = mOut->getXRunCount();
    if (x) mDiagXruns += x.value();
  }
  closeStreamsLocked();
  mDiagRunning.store(false);
  finishDiagLocked();
  {
    char b[128];
    std::snprintf(b, sizeof(b), "diag finish hits=%d rtt=%.1f timedOut=%d",
                  mDiagHitCount, mDiagRtt, timedOut ? 1 : 0);
    blogLine(b);
  }

  const std::string err = getError();
  if (!err.empty()) mDiagError = err;
  else if (!timedOut) mDiagError = "stopped";

  mDiagDone.store(true);
  mDiagThreadRunning.store(false);
}

/* ---------------- 错误 ---------------- */

void Engine::onErrorAfterClose(oboe::AudioStream* /*stream*/, oboe::Result error) {
  const std::string msg = std::string("stream error: ") + oboe::convertToText(error);
  blogLine("onErrorAfterClose " + msg);
  setError(msg);
  mRunning.store(false);
  mDiagRunning.store(false);
}

/* ---- prefer 列表：["opensl/voice_communication", "voice_performance", ...] ----
   无 JSON 库，扫引号取串；"api/preset" 或裸 "preset"，非法项忽略。
   全部非法/空数组 = 清空（回默认顺序）。必须在 start 前调用。 */
void Engine::setPrefer(const std::string& json) {
  std::vector<Cand> list;
  size_t i = 0;
  while (list.size() < 16 && (i = json.find('"', i)) != std::string::npos) {
    const size_t j = json.find('"', i + 1);
    if (j == std::string::npos) break;
    const std::string tok = json.substr(i + 1, j - i - 1);
    i = j + 1;
    int api = 0;
    std::string name = tok;
    const size_t slash = tok.find('/');
    if (slash != std::string::npos) {
      const std::string a = tok.substr(0, slash);
      name = tok.substr(slash + 1);
      if (a == "aaudio") api = 1;
      else if (a == "opensl") api = 2;
      else continue;
    }
    int preset = -1;
    for (int p = 0; p < 5; p++) {
      if (name == presetName(p)) { preset = p; break; }
    }
    if (preset >= 0) list.push_back({api, preset});
  }
  std::lock_guard<std::mutex> lock(mLock);
  mPrefer = list;
  blogLine("prefer set n=" + std::to_string(list.size()));
}

void Engine::setError(const std::string& e) {
  std::lock_guard<std::mutex> lock(mErrLock);
  mLastError = e;
}

void Engine::setLogPath(const std::string& p) {
  gBlogPath = p;
  blogLine("native log path set");
}

std::string Engine::getError() {
  std::lock_guard<std::mutex> lock(mErrLock);
  return mLastError;
}

/* ---------------- 状态 JSON ---------------- */

std::string Engine::diagJsonLocked() {
  const int rate = std::max(1, mOutRate.load());
  const int outBurst = mOutBurst.load();
  const int outBuf = mOutBuf.load();
  const int inBurst = mInBurst.load();
  const bool done = mDiagDone.load();
  const bool running = mDiagRunning.load();

  char inLatBuf[24], outLatBuf[24], rttBuf[24], minBuf[24], maxBuf[24];
  if (mDiagInLatencyMs >= 0) std::snprintf(inLatBuf, sizeof(inLatBuf), "%.1f", mDiagInLatencyMs);
  else std::snprintf(inLatBuf, sizeof(inLatBuf), "null");
  if (mDiagOutLatencyMs >= 0) std::snprintf(outLatBuf, sizeof(outLatBuf), "%.1f", mDiagOutLatencyMs);
  else std::snprintf(outLatBuf, sizeof(outLatBuf), "null");
  if (mDiagRtt >= 0) std::snprintf(rttBuf, sizeof(rttBuf), "%.1f", mDiagRtt);
  else std::snprintf(rttBuf, sizeof(rttBuf), "null");
  if (mDiagMin >= 0) std::snprintf(minBuf, sizeof(minBuf), "%.1f", mDiagMin);
  else std::snprintf(minBuf, sizeof(minBuf), "null");
  if (mDiagMax >= 0) std::snprintf(maxBuf, sizeof(maxBuf), "%.1f", mDiagMax);
  else std::snprintf(maxBuf, sizeof(maxBuf), "null");

  char head[768];
  std::snprintf(head, sizeof(head),
                "\"running\":%s,\"done\":%s,\"error\":\"%s\","
                "\"engine\":\"oboe\",\"api\":\"%s\",\"inApi\":\"%s\","
                "\"sampleRate\":%d,\"framesPerBurst\":%d,\"bufferFrames\":%d,"
                "\"inputBufferFrames\":%d,\"inputBufferCapacityFrames\":%d,"
                "\"inputLatencyMs\":%s,\"outputLatencyMs\":%s,\"xruns\":%d,"
                "\"pulseCount\":%d,\"onsetCount\":%d,"
                "\"rttMs\":%s,\"minMs\":%s,\"maxMs\":%s,\"hits\":[",
                running ? "true" : "false",
                done ? "true" : "false",
                jsonEsc(mDiagError).c_str(),
                apiName(mAudioApi.load()),
                apiName(mInApi.load()),
                rate, outBurst, outBuf, inBurst, mInCap.load(),
                inLatBuf, outLatBuf, mDiagXruns,
                mDiagPulseCount.load(), mDiagOnsetCount.load(),
                rttBuf, minBuf, maxBuf);

  std::string out(head);
  for (int i = 0; i < mDiagHitCount; i++) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%s%.1f", i ? "," : "", mDiagHits[i]);
    out += buf;
  }
  out += "]}";
  return out;
}

/* sweep 结果 JSON：mLock 内调用。失败候选 rtt 为 null，靠 ok/error 区分。 */
std::string Engine::sweepJsonLocked() {
  const bool running = mSweepRunning.load();
  const bool done = mSweepDone.load();
  const int idx = mSweepIdx.load();
  std::string cur = (idx >= 0 && idx < static_cast<int>(mSweepList.size()))
      ? candLabel(mSweepList[static_cast<size_t>(idx)]) : "";

  std::string out = "{\"running\":";
  out += running ? "true" : "false";
  out += ",\"done\":";
  out += done ? "true" : "false";
  out += ",\"error\":\"" + jsonEsc(mSweepError) + "\"";
  out += ",\"current\":\"" + jsonEsc(cur) + "\",\"results\":[";
  for (size_t i = 0; i < mSweepRows.size(); i++) {
    const SweepRow& r = mSweepRows[i];
    char rtt[24], mn[24], mx[24], inLat[24], outLat[24];
    if (r.ok && r.rtt >= 0) std::snprintf(rtt, sizeof(rtt), "%.1f", r.rtt);
    else std::snprintf(rtt, sizeof(rtt), "null");
    if (r.mn >= 0) std::snprintf(mn, sizeof(mn), "%.1f", r.mn);
    else std::snprintf(mn, sizeof(mn), "null");
    if (r.mx >= 0) std::snprintf(mx, sizeof(mx), "%.1f", r.mx);
    else std::snprintf(mx, sizeof(mx), "null");
    if (r.inLat >= 0) std::snprintf(inLat, sizeof(inLat), "%.1f", r.inLat);
    else std::snprintf(inLat, sizeof(inLat), "null");
    if (r.outLat >= 0) std::snprintf(outLat, sizeof(outLat), "%.1f", r.outLat);
    else std::snprintf(outLat, sizeof(outLat), "null");

    char b[384];
    std::snprintf(b, sizeof(b),
                  "%s{\"label\":\"%s\",\"ok\":%s,\"rttMs\":%s,\"minMs\":%s,"
                  "\"maxMs\":%s,\"hits\":%d,\"inBurst\":%d,\"inCap\":%d,"
                  "\"inLatencyMs\":%s,\"outLatencyMs\":%s,\"xruns\":%d,"
                  "\"error\":\"%s\"}",
                  i ? "," : "", jsonEsc(r.label).c_str(), r.ok ? "true" : "false",
                  rtt, mn, mx, r.hits, r.inBurst, r.inCap, inLat, outLat,
                  r.xruns, jsonEsc(r.err).c_str());
    out += b;
  }
  out += "]}";
  return out;
}

std::string Engine::statusJson() {
  std::lock_guard<std::mutex> lock(mLock);

  int outXruns = 0, inXruns = 0;
  double inLat = -1, outLat = -1;
  if (mIn) {
    auto x = mIn->getXRunCount();
    if (x) inXruns = x.value();
    auto l = mIn->calculateLatencyMillis();
    if (l) inLat = l.value();
    mInBurst.store(mIn->getFramesPerBurst());
    mInBuf.store(mIn->getBufferSizeInFrames());
    mInCap.store(mIn->getBufferCapacityInFrames());
    mInApi.store(apiToInt(mIn->getAudioApi()));
  }
  if (mOut) {
    auto x = mOut->getXRunCount();
    if (x) outXruns = x.value();
    auto l = mOut->calculateLatencyMillis();
    if (l) outLat = l.value();
    mOutBurst.store(mOut->getFramesPerBurst());
    mOutBuf.store(mOut->getBufferSizeInFrames());
    mOutCap.store(mOut->getBufferCapacityInFrames());
  }

  const int rate = std::max(1, mOutRate.load());
  const int inBurst = mInBurst.load();
  const int outBurst = mOutBurst.load();
  const int outBuf = mOutBuf.load();
  const int recMs = static_cast<int>(inBurst * 1000.0 / rate + 0.5);
  const int trkMs = static_cast<int>(outBuf * 1000.0 / rate + 0.5);
  const uint32_t wp = mWritePos.load(std::memory_order_acquire);
  const uint32_t rp = mReadPos.load(std::memory_order_relaxed);
  const int ringFrames = static_cast<int32_t>(wp - rp);
  const int ringMs = static_cast<int>(ringFrames * 1000.0 / rate + 0.5);
  const int underruns = outXruns + static_cast<int>(mRingUnderruns.load());
  const int overruns = inXruns + static_cast<int>(mRingOverruns.load());
  const std::string err = getError();

  char latBuf[24], inLatBuf[24], outLatBuf[24];
  const double totalLat = (inLat >= 0 || outLat >= 0)
      ? (std::max(0.0, inLat) + std::max(0.0, outLat) + ringFrames * 1000.0 / rate)
      : -1.0;
  if (totalLat >= 0) std::snprintf(latBuf, sizeof(latBuf), "%.1f", totalLat);
  else std::snprintf(latBuf, sizeof(latBuf), "null");
  // 平台侧实时遥测拆分：链路归因要看清输入腿/输出腿各占多少（P11.5）。
  if (inLat >= 0) std::snprintf(inLatBuf, sizeof(inLatBuf), "%.1f", inLat);
  else std::snprintf(inLatBuf, sizeof(inLatBuf), "null");
  if (outLat >= 0) std::snprintf(outLatBuf, sizeof(outLatBuf), "%.1f", outLat);
  else std::snprintf(outLatBuf, sizeof(outLatBuf), "null");

  char head[2048];
  std::snprintf(head, sizeof(head),
                "{\"ok\":true,\"engine\":\"oboe\",\"api\":\"%s\",\"inApi\":\"%s\","
                "\"running\":%s,\"mode\":\"%s\","
                "\"inRms\":%.4f,\"outRms\":%.4f,\"underruns\":%d,\"overruns\":%d,"
                "\"framesRead\":%llu,\"framesWritten\":%llu,"
                "\"source\":\"%s\",\"sampleRate\":%d,\"chunkFrames\":%d,"
                "\"recordBufFrames\":%d,\"trackBufFrames\":%d,"
                "\"recordBufMs\":%d,\"trackBufMs\":%d,"
                "\"bufferCapacityFrames\":%d,\"framesPerBurst\":%d,"
                "\"inputBufferCapacityFrames\":%d,"
                "\"ringFrames\":%d,\"ringMs\":%d,\"latencyMs\":%s,"
                "\"inCalcLatencyMs\":%s,\"outCalcLatencyMs\":%s,"
                "\"performanceMode\":\"low_latency\","
                "\"lastError\":\"%s\",\"diag\":{",
                apiName(mAudioApi.load()),
                apiName(mInApi.load()),
                mRunning.load() ? "true" : "false",
                modeName(mMode.load()),
                mInRms.load(), mOutRms.load(), underruns, overruns,
                static_cast<unsigned long long>(mFramesRead.load()),
                static_cast<unsigned long long>(mFramesWritten.load()),
                presetName(mInputPreset.load()),
                rate, outBurst, inBurst, outBuf, recMs, trkMs,
                mOutCap.load(), outBurst, mInCap.load(),
                ringFrames, ringMs, latBuf, inLatBuf, outLatBuf,
                jsonEsc(err).c_str());

  return std::string(head) + diagJsonLocked() + ",\"sweep\":" + sweepJsonLocked() + "}";
}

}  // namespace elysium

/* ---------------- JNI ---------------- */

using elysium::Engine;
using elysium::blogLine;

static Engine gEngine;

extern "C" {

// JNI 入口全部 try/catch：异常逃出 JNI 边界会 std::terminate，同样表现为闪退。
// 捕获后落面包屑并回错误码，让页面走回退而不是跟着崩。

JNIEXPORT jint JNICALL
Java_com_elysium_player_OboeKaraokeMonitor_nativeStart(JNIEnv* /*env*/, jobject /*thiz*/,
                                                       jdouble gain, jdouble toneDb,
                                                       jboolean muted) {
  try {
    return gEngine.start(gain, toneDb, muted != JNI_FALSE);
  } catch (const std::exception& e) {
    blogLine(std::string("jni start exception: ") + e.what());
  } catch (...) {
    blogLine("jni start exception (unknown)");
  }
  return -3;
}

JNIEXPORT void JNICALL
Java_com_elysium_player_OboeKaraokeMonitor_nativeStop(JNIEnv* /*env*/, jobject /*thiz*/) {
  try {
    gEngine.stop();
  } catch (const std::exception& e) {
    blogLine(std::string("jni stop exception: ") + e.what());
  } catch (...) {
    blogLine("jni stop exception (unknown)");
  }
}

JNIEXPORT void JNICALL
Java_com_elysium_player_OboeKaraokeMonitor_nativeSetParams(JNIEnv* /*env*/, jobject /*thiz*/,
                                                           jdouble gain, jdouble toneDb,
                                                           jboolean muted) {
  try {
    gEngine.update(gain, toneDb, muted != JNI_FALSE);
  } catch (...) {
    blogLine("jni setParams exception");
  }
}

JNIEXPORT jstring JNICALL
Java_com_elysium_player_OboeKaraokeMonitor_nativeStatusJson(JNIEnv* env, jobject /*thiz*/) {
  try {
    return env->NewStringUTF(gEngine.statusJson().c_str());
  } catch (const std::exception& e) {
    blogLine(std::string("jni status exception: ") + e.what());
  } catch (...) {
    blogLine("jni status exception (unknown)");
  }
  return env->NewStringUTF("{\"ok\":false,\"error\":\"status_exception\"}");
}

JNIEXPORT jstring JNICALL
Java_com_elysium_player_OboeKaraokeMonitor_nativeLastError(JNIEnv* env, jobject /*thiz*/) {
  try {
    return env->NewStringUTF(gEngine.lastError().c_str());
  } catch (...) {
    return env->NewStringUTF("lastError exception");
  }
}

JNIEXPORT jint JNICALL
Java_com_elysium_player_OboeKaraokeMonitor_nativeStartDiag(JNIEnv* /*env*/, jobject /*thiz*/) {
  try {
    return gEngine.startDiag();
  } catch (const std::exception& e) {
    blogLine(std::string("jni startDiag exception: ") + e.what());
  } catch (...) {
    blogLine("jni startDiag exception (unknown)");
  }
  return -3;
}

JNIEXPORT jint JNICALL
Java_com_elysium_player_OboeKaraokeMonitor_nativeStartSweep(JNIEnv* /*env*/, jobject /*thiz*/) {
  try {
    return gEngine.startSweep();
  } catch (const std::exception& e) {
    blogLine(std::string("jni startSweep exception: ") + e.what());
  } catch (...) {
    blogLine("jni startSweep exception (unknown)");
  }
  return -3;
}

JNIEXPORT void JNICALL
Java_com_elysium_player_OboeKaraokeMonitor_nativeSetPrefer(JNIEnv* env, jobject /*thiz*/,
                                                           jstring json) {
  try {
    const char* p = env->GetStringUTFChars(json, nullptr);
    if (p) {
      gEngine.setPrefer(p);
      env->ReleaseStringUTFChars(json, p);
    }
  } catch (...) {
    blogLine("jni setPrefer exception");
  }
}

JNIEXPORT void JNICALL
Java_com_elysium_player_OboeKaraokeMonitor_nativeStopDiag(JNIEnv* /*env*/, jobject /*thiz*/) {
  try {
    gEngine.stopDiag();
  } catch (...) {
    blogLine("jni stopDiag exception");
  }
}

JNIEXPORT void JNICALL
Java_com_elysium_player_OboeKaraokeMonitor_nativeSetLogPath(JNIEnv* env, jobject /*thiz*/,
                                                            jstring path) {
  try {
    const char* p = env->GetStringUTFChars(path, nullptr);
    if (p) {
      gEngine.setLogPath(p);
      env->ReleaseStringUTFChars(path, p);
    }
  } catch (...) {
  }
}

}  // extern "C"
