using System;
using System.Runtime.InteropServices;

namespace RobotVoice.Audio
{
    internal static class ApmNative
    {
        private const string DllName = "webrtc_apm_unity";

        [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
        public static extern IntPtr apm_create(int sample_rate_hz, int render_channels, int capture_channels);

        [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
        public static extern void apm_destroy(IntPtr handle);

        [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
        public static extern int apm_set_stream_delay_ms(IntPtr handle, int delay_ms);

        [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
        public static extern int apm_process_reverse_stream(IntPtr handle, float[] render_interleaved, int samples_per_channel);

        [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
        public static extern int apm_process_stream(
            IntPtr handle,
            float[] capture_interleaved,
            int samples_per_channel,
            float[] out_interleaved);
    }
}

