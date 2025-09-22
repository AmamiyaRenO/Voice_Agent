using System;

namespace RobotVoice
{
    [Serializable]
    public class SynonymOverride
    {
        public string Canonical = string.Empty;
        public string[] Variants = Array.Empty<string>();

        public bool Matches(string text)
        {
            if (string.IsNullOrWhiteSpace(text))
            {
                return false;
            }

            var candidate = text.Trim();
            if (!string.IsNullOrWhiteSpace(Canonical) &&
                candidate.Equals(Canonical, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }

            if (Variants == null || Variants.Length == 0)
            {
                return false;
            }

            for (int i = 0; i < Variants.Length; i++)
            {
                var v = Variants[i];
                if (!string.IsNullOrWhiteSpace(v) &&
                    candidate.Equals(v.Trim(), StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }

            return false;
        }
    }
}


