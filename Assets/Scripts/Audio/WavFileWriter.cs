using System;
using System.IO;
using System.Text;

namespace RobotVoice.Audio
{
    /// <summary>
    /// Minimal WAV writer for PCM16 interleaved audio.
    /// Writes a placeholder header then back-patches sizes on Dispose.
    /// </summary>
    internal sealed class WavFileWriter : IDisposable
    {
        private readonly FileStream _stream;
        private readonly int _sampleRate;
        private readonly short _channels;
        private long _dataBytes;

        public string Path { get; }

        public WavFileWriter(string path, int sampleRate, int channels)
        {
            if (string.IsNullOrWhiteSpace(path)) throw new ArgumentException("path is empty", nameof(path));
            if (sampleRate <= 0) throw new ArgumentOutOfRangeException(nameof(sampleRate));
            if (channels <= 0 || channels > short.MaxValue) throw new ArgumentOutOfRangeException(nameof(channels));

            Path = path;
            _sampleRate = sampleRate;
            _channels = (short)channels;

            Directory.CreateDirectory(System.IO.Path.GetDirectoryName(path) ?? ".");
            _stream = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.Read);
            WriteHeaderPlaceholder();
        }

        public void WritePcm16(short[] samples)
        {
            if (samples == null || samples.Length == 0) return;

            var bytes = new byte[samples.Length * 2];
            Buffer.BlockCopy(samples, 0, bytes, 0, bytes.Length);
            _stream.Write(bytes, 0, bytes.Length);
            _dataBytes += bytes.Length;

            // Keep the header up to date so the file has a non-zero duration even if Unity
            // is terminated or the writer isn't disposed cleanly (common during Editor play/stop).
            try
            {
                var endPos = _stream.Position;
                FinalizeHeader();
                _stream.Seek(endPos, SeekOrigin.Begin);
                _stream.Flush();
            }
            catch
            {
                // ignore streaming header update failures
            }
        }

        private void WriteHeaderPlaceholder()
        {
            // RIFF header (44 bytes)
            // ChunkID "RIFF"
            _stream.Write(Encoding.ASCII.GetBytes("RIFF"), 0, 4);
            // ChunkSize (placeholder)
            _stream.Write(BitConverter.GetBytes(0), 0, 4);
            // Format "WAVE"
            _stream.Write(Encoding.ASCII.GetBytes("WAVE"), 0, 4);

            // Subchunk1ID "fmt "
            _stream.Write(Encoding.ASCII.GetBytes("fmt "), 0, 4);
            // Subchunk1Size 16 for PCM
            _stream.Write(BitConverter.GetBytes(16), 0, 4);
            // AudioFormat 1 (PCM)
            _stream.Write(BitConverter.GetBytes((short)1), 0, 2);
            // NumChannels
            _stream.Write(BitConverter.GetBytes(_channels), 0, 2);
            // SampleRate
            _stream.Write(BitConverter.GetBytes(_sampleRate), 0, 4);
            // ByteRate = SampleRate * NumChannels * BitsPerSample/8
            int byteRate = _sampleRate * _channels * 2;
            _stream.Write(BitConverter.GetBytes(byteRate), 0, 4);
            // BlockAlign = NumChannels * BitsPerSample/8
            short blockAlign = (short)(_channels * 2);
            _stream.Write(BitConverter.GetBytes(blockAlign), 0, 2);
            // BitsPerSample
            _stream.Write(BitConverter.GetBytes((short)16), 0, 2);

            // Subchunk2ID "data"
            _stream.Write(Encoding.ASCII.GetBytes("data"), 0, 4);
            // Subchunk2Size (placeholder)
            _stream.Write(BitConverter.GetBytes(0), 0, 4);
        }

        private void FinalizeHeader()
        {
            // ChunkSize = 36 + Subchunk2Size
            var riffSize = (int)Math.Min(int.MaxValue, 36 + _dataBytes);
            var dataSize = (int)Math.Min(int.MaxValue, _dataBytes);

            _stream.Seek(4, SeekOrigin.Begin);
            _stream.Write(BitConverter.GetBytes(riffSize), 0, 4);

            _stream.Seek(40, SeekOrigin.Begin);
            _stream.Write(BitConverter.GetBytes(dataSize), 0, 4);
        }

        public void Dispose()
        {
            try
            {
                FinalizeHeader();
            }
            catch
            {
                // ignore header patch failures
            }
            finally
            {
                _stream?.Dispose();
            }
        }
    }
}

