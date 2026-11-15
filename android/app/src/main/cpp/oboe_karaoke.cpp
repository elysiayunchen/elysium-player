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
    resetDiagStateLocked();
  }

  ~Engine() override { stop(); }

  int start(double gain, double toneDb, bool muted);
  void stop();
  void update(double gain, double toneDb, bool muted);
  std::string statusJson();
  int startDiag();
  void stopDiag();
  void setLogPath(const std::string& p);
  std::string lastError() { return getError(); }

  oboe::DataCallbackResult onAudioReady(oboe::AudioStream* stream, void* audioData,
                                        int32_t numFrames) override;
  void onErrorAfterClose(oboe::AudioStream* stream, oboe::Result error) override;

 private:
  enum { kModeIdle = 0, kModeMonitor = 1, kModeDiag = 2 };

  bool openStreamsLocked(std::string* detail);
  void closeStreamsLocked();
  void resetDsp(int rate);
  void resetDiagStateLocked();
  void finishDiagLocked();
  void diagLoop();
  std::string diagJsonLocked();
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
    return "generic";
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
  std::atomic<int> mAudioApi{0};
  std::atomic<int> mInputPreset{0};

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

  mutable std::mutex mErrLock;
  std::string mLastError;
};

/* ---------------- 生命周期 ---------------- */

int Engine::start(double gain, double toneDb, bool muted) {
  stop();
  blogLine("start begin");

  std::lock_guard<std::mutex> lock(mLock);
  setError("");
  resetDiagStateLocked();
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
  std::thread dt;
  blogLine("stop begin");
  {
    std::lock_guard<std::mutex> lock(mLock);
    mDiagRunning.store(false);
    closeStreamsLocked();
    if (mDiagThread.joinable()) dt = std::move(mDiagThread);
  }
  if (dt.joinable()) {
    dt.join();
    blogLine("stop diag thread joined");
  }
  blogLine("stop end");
}

int Engine::startDiag() {
  stopDiag();  // 先收掉上一轮自检线程/流；不动 monitor（下面会拒绝）
  blogLine("diag begin");

  std::lock_guard<std::mutex> lock(mLock);
  if (mRunning.load()) return -2;

  setError("");
  resetDiagStateLocked();
  mMode.store(kModeDiag);

  std::string detail;
  if (!openStreamsLocked(&detail)) {
    mMode.store(kModeIdle);
    setError(detail);
    return -1;
  }

  const int outRate = std::max(1, mOutRate.load());
  for (int p = 0; p < kMaxPulses; p++) {
    mDiagPulseStartFrame[p] = static_cast<int64_t>(
        (kPulseStartSec + p * kPulseGapSec) * outRate);
  }
  mDiagPulseLenFrames = static_cast<int64_t>(kPulseMs / 1000.0 * outRate);
  mDiagEdgeFrames = static_cast<int64_t>(0.005 * outRate);

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
  std::thread dt;
  {
    std::lock_guard<std::mutex> lock(mLock);
    mDiagRunning.store(false);
    if (mMode.load() == kModeDiag) closeStreamsLocked();
    if (mDiagThread.joinable()) dt = std::move(mDiagThread);
  }
  if (dt.joinable()) dt.join();
}

void Engine::update(double gain, double toneDb, bool muted) {
  mGain.store(static_cast<float>(std::max(0.0, std::min(4.0, gain))));
  mToneDb.store(static_cast<float>(std::max(-12.0, std::min(12.0, toneDb))));
  mMuted.store(muted);
}

/* ---------------- 流打开 ---------------- */

bool Engine::openStreamsLocked(std::string* detail) {
  std::string errs;

  // 输出先开：拿到设备原生采样率，输入按同一速率开，环里的帧率才一致。
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
      errs += "out/";
      errs += (c == 0 ? "mono" : "stereo");
      errs += ": ";
      errs += oboe::convertToText(r);
      errs += "; ";
      continue;
    }
    if (s->getFormat() != oboe::AudioFormat::I16) {
      errs += "out format != I16; ";
      s->close();
      continue;
    }
    mOut = s;
    mOutBurst.store(s->getFramesPerBurst());
    mOutBuf.store(s->getBufferSizeInFrames());
    mOutCap.store(s->getBufferCapacityInFrames());
    mAudioApi.store(apiToInt(s->getAudioApi()));
    break;
  }
  if (!mOut) {
    *detail = errs.empty() ? "no oboe output" : errs;
    return false;
  }

  int rate = mOut->getSampleRate();
  if (rate <= 0) rate = 48000;
  mOutRate.store(rate);

  // 输入：VoicePerformance（K 歌专用，API 29+）→ Unprocessed → VoiceRecognition → Generic。
  const oboe::InputPreset presets[] = {
      oboe::InputPreset::VoicePerformance,
      oboe::InputPreset::Unprocessed,
      oboe::InputPreset::VoiceRecognition,
      oboe::InputPreset::Generic};
  const int32_t inChannels[] = {1, 2};
  for (int p = 0; p < 4; p++) {
    for (int c = 0; c < 2; c++) {
      oboe::AudioStreamBuilder b;
      b.setDirection(oboe::Direction::Input);
      b.setPerformanceMode(oboe::PerformanceMode::LowLatency);
      b.setSharingMode(oboe::SharingMode::Shared);
      b.setFormat(oboe::AudioFormat::I16);
      b.setSampleRate(rate);
      b.setChannelCount(inChannels[c]);
      b.setInputPreset(presets[p]);
      b.setDataCallback(this);
      b.setErrorCallback(this);

      std::shared_ptr<oboe::AudioStream> s;
      oboe::Result r = b.openStream(s);
      if (r != oboe::Result::OK || !s) {
        errs += "in/";
        errs += presetName(p);
        errs += "/";
        errs += (c == 0 ? "mono" : "stereo");
        errs += ": ";
        errs += oboe::convertToText(r);
        errs += "; ";
        continue;
      }
      if (s->getFormat() != oboe::AudioFormat::I16) {
        errs += "in format != I16; ";
        s->close();
        continue;
      }
      int inRate = s->getSampleRate();
      if (inRate <= 0) inRate = rate;
      if (inRate != rate) {
        errs += "in rate mismatch: ";
        errs += std::to_string(inRate);
        errs += " vs ";
        errs += std::to_string(rate);
        errs += "; ";
        s->close();
        continue;
      }
      mIn = s;
      mInputPreset.store(p);
      mInRate.store(inRate);
      mInBurst.store(s->getFramesPerBurst());
      mInBuf.store(s->getBufferSizeInFrames());
      break;
    }
    if (mIn) break;
  }
  if (!mIn) {
    closeStreamsLocked();
    *detail = errs.empty() ? "no oboe input" : errs;
    return false;
  }

  resetDsp(mInRate.load());
  return true;
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

  // 收口期：绝不碰 data。Android 15 legacy 输入路径的 close 不等在飞回调结束
  // （beta10 真机 SIGSEGV 实证），缓冲可能已解映射，这里碰一下就是闪退。
  if (mTearingDown.load(std::memory_order_relaxed) ||
      !mRunning.load(std::memory_order_relaxed)) {
    return oboe::DataCallbackResult::Continue;
  }

  const bool isOutput = (stream != nullptr && stream == mOut.get());
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

  // 闲置/收口期：不碰 data（同上，缓冲可能已解映射）。
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
    for (int p = 0; p < kMaxPulses; p++) {
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

void Engine::resetDiagStateLocked() {
  mDiagRunning.store(false);
  mDiagDone.store(false);
  mDiagPulseCount.store(0);
  mDiagOnsetCount.store(0);
  for (int i = 0; i < kMaxPulses; i++) {
    mDiagPulseNs[i].store(0);
    mDiagHits[i] = 0;
  }
  for (int i = 0; i < kMaxOnsets; i++) mDiagOnsetNs[i].store(0);
  mDiagOutFrames.store(0);
  mDiagLockoutFrames.store(0);
  for (int i = 0; i < kMaxPulses; i++) mDiagPulseStartFrame[i] = 0;
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

  char head[640];
  std::snprintf(head, sizeof(head),
                "\"running\":%s,\"done\":%s,\"error\":\"%s\","
                "\"engine\":\"oboe\",\"api\":\"%s\","
                "\"sampleRate\":%d,\"framesPerBurst\":%d,\"bufferFrames\":%d,"
                "\"inputBufferFrames\":%d,"
                "\"inputLatencyMs\":%s,\"outputLatencyMs\":%s,\"xruns\":%d,"
                "\"pulseCount\":%d,\"onsetCount\":%d,"
                "\"rttMs\":%s,\"minMs\":%s,\"maxMs\":%s,\"hits\":[",
                running ? "true" : "false",
                done ? "true" : "false",
                jsonEsc(mDiagError).c_str(),
                apiName(mAudioApi.load()),
                rate, outBurst, outBuf, inBurst,
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

  char latBuf[24];
  const double totalLat = (inLat >= 0 || outLat >= 0)
      ? (std::max(0.0, inLat) + std::max(0.0, outLat) + ringFrames * 1000.0 / rate)
      : -1.0;
  if (totalLat >= 0) std::snprintf(latBuf, sizeof(latBuf), "%.1f", totalLat);
  else std::snprintf(latBuf, sizeof(latBuf), "null");

  char head[1792];
  std::snprintf(head, sizeof(head),
                "{\"ok\":true,\"engine\":\"oboe\",\"api\":\"%s\","
                "\"running\":%s,\"mode\":\"%s\","
                "\"inRms\":%.4f,\"outRms\":%.4f,\"underruns\":%d,\"overruns\":%d,"
                "\"framesRead\":%llu,\"framesWritten\":%llu,"
                "\"source\":\"%s\",\"sampleRate\":%d,\"chunkFrames\":%d,"
                "\"recordBufFrames\":%d,\"trackBufFrames\":%d,"
                "\"recordBufMs\":%d,\"trackBufMs\":%d,"
                "\"bufferCapacityFrames\":%d,\"framesPerBurst\":%d,"
                "\"ringFrames\":%d,\"ringMs\":%d,\"latencyMs\":%s,"
                "\"performanceMode\":\"low_latency\","
                "\"lastError\":\"%s\",\"diag\":{",
                apiName(mAudioApi.load()),
                mRunning.load() ? "true" : "false",
                modeName(mMode.load()),
                mInRms.load(), mOutRms.load(), underruns, overruns,
                static_cast<unsigned long long>(mFramesRead.load()),
                static_cast<unsigned long long>(mFramesWritten.load()),
                presetName(mInputPreset.load()),
                rate, outBurst, inBurst, outBuf, recMs, trkMs,
                mOutCap.load(), outBurst,
                ringFrames, ringMs, latBuf,
                jsonEsc(err).c_str());

  return std::string(head) + diagJsonLocked() + "}";
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
