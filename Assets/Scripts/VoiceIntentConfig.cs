using System;
using UnityEngine;

namespace RobotVoice
{
    [Serializable]
    public class VoiceIntentConfig
    {
        public string WakeWord = string.Empty;
        public string[] LaunchKeywords = Array.Empty<string>();
        public string[] ExitKeywords = Array.Empty<string>();
        public SynonymOverride[] SynonymOverrides = Array.Empty<SynonymOverride>();

        public static VoiceIntentConfig LoadFromJson(string json)
        {
            if (string.IsNullOrWhiteSpace(json))
            {
                return null;
            }

            try
            {
                return JsonUtility.FromJson<VoiceIntentConfig>(json);
            }
            catch
            {
                return null;
            }
        }

        public string ResolveGameName(string raw)
        {
            if (string.IsNullOrWhiteSpace(raw))
            {
                return string.Empty;
            }

            var text = raw.Trim();

            if (SynonymOverrides != null)
            {
                for (int i = 0; i < SynonymOverrides.Length; i++)
                {
                    var ov = SynonymOverrides[i];
                    if (ov != null && ov.Matches(text))
                    {
                        return ov.Canonical;
                    }
                }
            }

            return text;
        }
    }
}


