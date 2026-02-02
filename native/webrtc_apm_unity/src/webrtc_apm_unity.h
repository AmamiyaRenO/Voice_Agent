#pragma once

#include <cstddef>

#ifdef _WIN32
  #define WEBRTC_APM_UNITY_EXPORT __declspec(dllexport)
#else
  #define WEBRTC_APM_UNITY_EXPORT
#endif

extern "C" {

// Opaque handle managed by the DLL.
WEBRTC_APM_UNITY_EXPORT void* apm_create(int sample_rate_hz, int render_channels, int capture_channels);
WEBRTC_APM_UNITY_EXPORT void apm_destroy(void* handle);

// Stream delay (ms) helps align render reference to capture.
WEBRTC_APM_UNITY_EXPORT int apm_set_stream_delay_ms(void* handle, int delay_ms);

// Feed the render reference (speaker output) into AEC.
// - render_interleaved: interleaved float samples in [-1, 1]
// - samples_per_channel: number of samples per channel in this block (typically 10ms worth)
WEBRTC_APM_UNITY_EXPORT int apm_process_reverse_stream(
    void* handle,
    const float* render_interleaved,
    int samples_per_channel);

// Process capture (microphone) through AEC.
// - capture_interleaved: interleaved float samples in [-1, 1]
// - out_interleaved: output buffer (same layout/length as capture_interleaved)
WEBRTC_APM_UNITY_EXPORT int apm_process_stream(
    void* handle,
    const float* capture_interleaved,
    int samples_per_channel,
    float* out_interleaved);

}

