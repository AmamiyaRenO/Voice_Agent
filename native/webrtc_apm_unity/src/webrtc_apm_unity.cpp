#include "webrtc_apm_unity.h"

#include <algorithm>
#include <memory>
#include <vector>

// WebRTC AudioProcessing (APM) headers (expected to be provided by the dependency).
#include "webrtc/common.h"
#include "webrtc/modules/audio_processing/include/audio_processing.h"

namespace {

struct ApmHandle {
  int sample_rate_hz = 48000;
  int render_channels = 2;
  int capture_channels = 1;
  int samples_per_channel = 0;

  webrtc::StreamConfig render_config;
  webrtc::StreamConfig capture_config;

  // planar buffers: [channel][sample]
  std::vector<std::vector<float>> render_in;
  std::vector<std::vector<float>> render_out;
  std::vector<std::vector<float>> capture_in;
  std::vector<std::vector<float>> capture_out;

  std::vector<const float*> render_in_ptrs;
  std::vector<float*> render_out_ptrs;
  std::vector<const float*> capture_in_ptrs;
  std::vector<float*> capture_out_ptrs;

  webrtc::AudioProcessing* apm = nullptr;

  ApmHandle(int sr, int rc, int cc)
      : sample_rate_hz(sr),
        render_channels(std::max(1, rc)),
        capture_channels(std::max(1, cc)),
        render_config(sample_rate_hz, render_channels),
        capture_config(sample_rate_hz, capture_channels) {}

  void EnsureFrame(int spc) {
    if (spc <= 0) return;
    const int expected = static_cast<int>(capture_config.num_frames());
    if (spc != expected) return;
    if (samples_per_channel == spc) return;
    samples_per_channel = spc;

    render_in.assign(render_channels, std::vector<float>(spc, 0.0f));
    render_out.assign(render_channels, std::vector<float>(spc, 0.0f));
    capture_in.assign(capture_channels, std::vector<float>(spc, 0.0f));
    capture_out.assign(capture_channels, std::vector<float>(spc, 0.0f));

    render_in_ptrs.resize(render_channels);
    render_out_ptrs.resize(render_channels);
    for (int ch = 0; ch < render_channels; ch++) {
      render_in_ptrs[ch] = render_in[ch].data();
      render_out_ptrs[ch] = render_out[ch].data();
    }

    capture_in_ptrs.resize(capture_channels);
    capture_out_ptrs.resize(capture_channels);
    for (int ch = 0; ch < capture_channels; ch++) {
      capture_in_ptrs[ch] = capture_in[ch].data();
      capture_out_ptrs[ch] = capture_out[ch].data();
    }
  }
};

static void Deinterleave(const float* interleaved, int channels, int spc, std::vector<std::vector<float>>& planar) {
  if (!interleaved || channels <= 0 || spc <= 0) return;
  for (int ch = 0; ch < channels; ch++) {
    auto& dst = planar[ch];
    for (int i = 0; i < spc; i++) {
      dst[i] = interleaved[i * channels + ch];
    }
  }
}

static void Interleave(const std::vector<std::vector<float>>& planar, int channels, int spc, float* out_interleaved) {
  if (!out_interleaved || channels <= 0 || spc <= 0) return;
  for (int i = 0; i < spc; i++) {
    for (int ch = 0; ch < channels; ch++) {
      out_interleaved[i * channels + ch] = planar[ch][i];
    }
  }
}

}  // namespace

extern "C" {

void* apm_create(int sample_rate_hz, int render_channels, int capture_channels) {
  const int sr = sample_rate_hz > 0 ? sample_rate_hz : 48000;

  auto handle = std::make_unique<ApmHandle>(sr, render_channels, capture_channels);

  handle->apm = webrtc::AudioProcessing::Create();
  if (!handle->apm) {
    return nullptr;
  }

  // Enable components (client-side defaults).
  handle->apm->high_pass_filter()->Enable(true);
  handle->apm->noise_suppression()->Enable(true);
  handle->apm->noise_suppression()->set_level(webrtc::NoiseSuppression::kHigh);

  // Best practice: enable drift compensation when capture/render clocks can differ.
  // This is common on consumer Windows devices and helps AEC stability.
  handle->apm->echo_cancellation()->enable_drift_compensation(true);
  handle->apm->echo_cancellation()->set_suppression_level(webrtc::EchoCancellation::kHighSuppression);
  handle->apm->echo_cancellation()->Enable(true);

  // Best practice (WebRTC): enable ExtendedFilter + DelayAgnostic via SetExtraOptions
  // to improve robustness when delay reporting is imperfect.
  // See comments in audio_processing.h around ExtendedFilter and DelayAgnostic.
  try {
    webrtc::Config extra;
    extra.Set<webrtc::ExtendedFilter>(new webrtc::ExtendedFilter(true));
    extra.Set<webrtc::DelayAgnostic>(new webrtc::DelayAgnostic(true));
    handle->apm->SetExtraOptions(extra);
  } catch (...) {
    // ignore; older builds may not support these options at runtime
  }

  // Initialize with matching 10ms stream configs.
  webrtc::ProcessingConfig pc;
  pc.streams[webrtc::ProcessingConfig::kInputStream] = handle->capture_config;
  pc.streams[webrtc::ProcessingConfig::kOutputStream] = handle->capture_config;
  pc.streams[webrtc::ProcessingConfig::kReverseInputStream] = handle->render_config;
  pc.streams[webrtc::ProcessingConfig::kReverseOutputStream] = handle->render_config;
  handle->apm->Initialize(pc);

  return handle.release();
}

void apm_destroy(void* handle) {
  auto* h = reinterpret_cast<ApmHandle*>(handle);
  if (h && h->apm) {
    delete h->apm;
    h->apm = nullptr;
  }
  delete h;
}

int apm_set_stream_delay_ms(void* handle, int delay_ms) {
  auto* h = reinterpret_cast<ApmHandle*>(handle);
  if (!h || !h->apm) return -1;
  return h->apm->set_stream_delay_ms(std::max(0, delay_ms));
}

int apm_process_reverse_stream(void* handle, const float* render_interleaved, int samples_per_channel) {
  auto* h = reinterpret_cast<ApmHandle*>(handle);
  if (!h || !h->apm) return -1;
  if (!render_interleaved || samples_per_channel <= 0) return -2;
  if (samples_per_channel != static_cast<int>(h->render_config.num_frames())) return -3;

  h->EnsureFrame(samples_per_channel);
  Deinterleave(render_interleaved, h->render_channels, samples_per_channel, h->render_in);

  const int rc = h->apm->ProcessReverseStream(
      h->render_in_ptrs.data(),
      h->render_config,
      h->render_config,
      h->render_out_ptrs.data());

  return rc;
}

int apm_process_stream(void* handle, const float* capture_interleaved, int samples_per_channel, float* out_interleaved) {
  auto* h = reinterpret_cast<ApmHandle*>(handle);
  if (!h || !h->apm) return -1;
  if (!capture_interleaved || !out_interleaved || samples_per_channel <= 0) return -2;
  if (samples_per_channel != static_cast<int>(h->capture_config.num_frames())) return -3;

  h->EnsureFrame(samples_per_channel);
  Deinterleave(capture_interleaved, h->capture_channels, samples_per_channel, h->capture_in);

  const int rc = h->apm->ProcessStream(
      h->capture_in_ptrs.data(),
      h->capture_config,
      h->capture_config,
      h->capture_out_ptrs.data());

  if (rc != 0) {
    return rc;
  }

  Interleave(h->capture_out, h->capture_channels, samples_per_channel, out_interleaved);
  return 0;
}

}  // extern "C"

