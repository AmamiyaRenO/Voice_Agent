using System;
using System.Collections.Generic;

namespace RobotVoice
{
    public enum ConversationRole
    {
        User,
        Coach,
        Wizard,
        System,
    }

    public readonly struct ConversationLogEntry
    {
        public readonly DateTime TimestampUtc;
        public readonly ConversationRole Role;
        public readonly string Speaker;
        public readonly string Message;
        public readonly string Metadata;
        public readonly string Source;

        public ConversationLogEntry(DateTime timestampUtc, ConversationRole role, string speaker, string message, string metadata, string source)
        {
            TimestampUtc = timestampUtc;
            Role = role;
            Speaker = speaker ?? string.Empty;
            Message = message ?? string.Empty;
            Metadata = metadata ?? string.Empty;
            Source = source ?? string.Empty;
        }
    }

    public static class ConversationLog
    {
        private const int MaxEntries = 200;
        private static readonly List<ConversationLogEntry> Entries = new List<ConversationLogEntry>(64);
        private static readonly object SyncRoot = new object();

        public static void AddEntry(ConversationRole role, string message, string speaker = null, string metadata = null, string source = null)
        {
            if (string.IsNullOrWhiteSpace(message))
            {
                return;
            }

            var trimmed = message.Trim();
            var speakerName = string.IsNullOrWhiteSpace(speaker)
                ? GetDefaultSpeaker(role)
                : speaker.Trim();

            var entry = new ConversationLogEntry(
                DateTime.UtcNow,
                role,
                speakerName,
                trimmed,
                metadata ?? string.Empty,
                source ?? string.Empty);

            lock (SyncRoot)
            {
                Entries.Add(entry);
                if (Entries.Count > MaxEntries)
                {
                    var overflow = Entries.Count - MaxEntries;
                    Entries.RemoveRange(0, overflow);
                }
            }
        }

        public static ConversationLogEntry[] GetSnapshot()
        {
            lock (SyncRoot)
            {
                return Entries.ToArray();
            }
        }

        private static string GetDefaultSpeaker(ConversationRole role)
        {
            switch (role)
            {
                case ConversationRole.Coach:
                    return "RACHEL";
                case ConversationRole.Wizard:
                    return "Wizard Override";
                case ConversationRole.System:
                    return "System";
                default:
                    return "User";
            }
        }
    }
}
