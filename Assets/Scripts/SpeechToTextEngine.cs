using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

/// <summary>
/// Options passed to an <see cref="ISpeechToTextEngine"/> implementation during initialisation.
/// </summary>
public sealed class SpeechToTextEngineConfiguration
{
        /// <summary>
        /// The absolute path to the model data used by the engine. Implementations decide how the path is consumed.
        /// </summary>
        public string ModelPath { get; }

        /// <summary>
        /// Optional key phrases that should be prioritised by the engine. May be empty.
        /// </summary>
        public IReadOnlyList<string> KeyPhrases { get; }

        /// <summary>
        /// Maximum number of alternative transcriptions the engine should keep track of.
        /// </summary>
        public int MaxAlternatives { get; }

        /// <summary>
        /// Audio sample rate, in hertz, expected by the engine.
        /// </summary>
        public float SampleRate { get; }

        public SpeechToTextEngineConfiguration(string modelPath, IReadOnlyList<string> keyPhrases, int maxAlternatives, float sampleRate)
        {
                if (string.IsNullOrEmpty(modelPath))
                        throw new ArgumentException("Model path must be a valid, non-empty string.", nameof(modelPath));

                ModelPath = modelPath;
                KeyPhrases = keyPhrases ?? Array.Empty<string>();
                MaxAlternatives = Mathf.Max(1, maxAlternatives);
                SampleRate = Mathf.Max(1f, sampleRate);
        }
}

/// <summary>
/// Provides a common contract for speech-to-text engines so that different providers can be plugged into the controller.
/// </summary>
public interface ISpeechToTextEngine : IDisposable
{
        /// <summary>
        /// Human readable name for the engine. Used for logging and debugging.
        /// </summary>
        string EngineName { get; }

        /// <summary>
        /// Performs any heavy initialisation required before audio can be processed.
        /// </summary>
        Task InitialiseAsync(SpeechToTextEngineConfiguration configuration, CancellationToken cancellationToken = default);

        /// <summary>
        /// Attempts to transcribe the provided audio buffer.
        /// </summary>
        /// <param name="samples">Audio samples captured from the microphone.</param>
        /// <param name="result">Full transcription if the engine produced a final result.</param>
        /// <returns>True if a final transcription is ready, otherwise false.</returns>
        bool TryRecognise(short[] samples, out string result);
}

/// <summary>
/// Convenience base class that binds the <see cref="ISpeechToTextEngine"/> interface to a <see cref="MonoBehaviour"/>.
/// </summary>
public abstract class SpeechToTextEngineBase : MonoBehaviour, ISpeechToTextEngine
{
        public abstract string EngineName { get; }

        public abstract Task InitialiseAsync(SpeechToTextEngineConfiguration configuration, CancellationToken cancellationToken = default);

        public abstract bool TryRecognise(short[] samples, out string result);

        public virtual void Dispose()
        {
        }
}
