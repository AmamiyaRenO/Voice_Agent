using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using SimpleJSON;
using UnityEngine;
using Vosk;

/// <summary>
/// Default speech-to-text engine using the bundled Vosk bindings.
/// Additional engines can be created by inheriting from <see cref="SpeechToTextEngineBase"/>.
/// </summary>
[DisallowMultipleComponent]
public class VoskSpeechToTextEngine : SpeechToTextEngineBase
{
        private readonly object _syncRoot = new object();
        private Model _model;
        private VoskRecognizer _recognizer;

        public override string EngineName => "Vosk";

        public override async Task InitialiseAsync(SpeechToTextEngineConfiguration configuration, CancellationToken cancellationToken = default)
        {
                if (configuration == null)
                        throw new ArgumentNullException(nameof(configuration));

                await Task.Run(() =>
                {
                        cancellationToken.ThrowIfCancellationRequested();

                        lock (_syncRoot)
                        {
                                DisposeInternal();

                                _model = new Model(configuration.ModelPath);

                                string grammar = BuildGrammar(configuration.KeyPhrases);
                                if (string.IsNullOrEmpty(grammar))
                                {
                                        _recognizer = new VoskRecognizer(_model, configuration.SampleRate);
                                }
                                else
                                {
                                        _recognizer = new VoskRecognizer(_model, configuration.SampleRate, grammar);
                                }

                                _recognizer.SetMaxAlternatives(configuration.MaxAlternatives);
                        }
                }, cancellationToken).ConfigureAwait(false);
        }

        public override bool TryRecognise(short[] samples, out string result)
        {
                result = null;
                if (samples == null)
                        return false;

                lock (_syncRoot)
                {
                        if (_recognizer == null)
                                return false;

                        if (_recognizer.AcceptWaveform(samples, samples.Length))
                        {
                                result = _recognizer.Result();
                                return true;
                        }
                }

                return false;
        }

        public override void Dispose()
        {
                lock (_syncRoot)
                {
                        DisposeInternal();
                }
        }

        private static string BuildGrammar(IReadOnlyList<string> keyPhrases)
        {
                if (keyPhrases == null || keyPhrases.Count == 0)
                        return string.Empty;

                JSONArray keywords = new JSONArray();
                foreach (var keyphrase in keyPhrases)
                {
                        if (string.IsNullOrWhiteSpace(keyphrase))
                                continue;

                        keywords.Add(new JSONString(keyphrase.ToLowerInvariant()));
                }

                if (keywords.Count == 0)
                        return string.Empty;

                keywords.Add(new JSONString("[unk]"));
                return keywords.ToString();
        }

        private void DisposeInternal()
        {
                _recognizer?.Dispose();
                _recognizer = null;

                _model?.Dispose();
                _model = null;
        }
}
