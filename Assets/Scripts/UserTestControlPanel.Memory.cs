using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Text;
using System.Threading.Tasks;

namespace RobotVoice
{
    public sealed partial class UserTestControlPanel
    {
        private async Task HandleMemoryAsync(HttpListenerContext context)
        {
            var method = (context.Request.HttpMethod ?? string.Empty).Trim().ToUpperInvariant();
            if (method == "GET")
            {
                var selectedUserIdFromQuery = (context.Request.QueryString["user_id"] ?? string.Empty).Trim();
                await WriteMemoryStatusAsync(context.Response, "memory loaded", selectedUserIdFromQuery).ConfigureAwait(false);
                return;
            }

            if (method != "POST")
            {
                context.Response.StatusCode = 405;
                context.Response.Headers["Allow"] = "GET,POST,OPTIONS";
                await WriteJsonAsync(context.Response, 405, "error", "method not allowed").ConfigureAwait(false);
                return;
            }

            var body = ReadRequestBody(context.Request);
            JSONNode requestNode;
            try
            {
                requestNode = string.IsNullOrWhiteSpace(body) ? new JSONObject() : JSONNode.Parse(body);
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(context.Response, 400, "error", $"invalid json body: {ex.Message}").ConfigureAwait(false);
                return;
            }

            if (requestNode == null || !requestNode.IsObject)
            {
                await WriteJsonAsync(context.Response, 400, "error", "json object body is required").ConfigureAwait(false);
                return;
            }

            var requestObj = requestNode.AsObject;
            var action = (requestObj["action"]?.Value ?? string.Empty).Trim().ToLowerInvariant();
            if (string.IsNullOrWhiteSpace(action))
            {
                await WriteJsonAsync(context.Response, 400, "error", "action is required").ConfigureAwait(false);
                return;
            }

            var memoryPath = ResolveDialogUserMemoryPath();
            var load = LoadDialogMemoryRoot(memoryPath);
            if (!load.Success)
            {
                await WriteJsonAsync(context.Response, 500, "error", load.Error).ConfigureAwait(false);
                return;
            }

            var root = load.Root;
            var profilesObj = EnsureObjectNode(root, "profiles");
            var identityMapObj = EnsureObjectNode(root, "identity_map");
            var nowTs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
            var selectedUserId = (requestObj["user_id"]?.Value ?? string.Empty).Trim();

            switch (action)
            {
                case "update_user_raw":
                {
                    if (string.IsNullOrWhiteSpace(selectedUserId))
                    {
                        await WriteJsonAsync(context.Response, 400, "error", "user_id is required").ConfigureAwait(false);
                        return;
                    }

                    var profileNode = requestObj["profile"];
                    if (profileNode == null || !profileNode.IsObject)
                    {
                        await WriteJsonAsync(context.Response, 400, "error", "profile object is required").ConfigureAwait(false);
                        return;
                    }

                    var existingProfile = profilesObj[selectedUserId] != null && profilesObj[selectedUserId].IsObject
                        ? profilesObj[selectedUserId].AsObject
                        : null;

                    profilesObj[selectedUserId] = NormalizeMemoryProfile(
                        selectedUserId,
                        profileNode.AsObject,
                        existingProfile,
                        nowTs);

                    EnsureNextUserIndex(root, selectedUserId);

                    var save = SaveDialogMemoryRoot(memoryPath, root);
                    if (!save.Success)
                    {
                        await WriteJsonAsync(context.Response, 500, "error", save.Error).ConfigureAwait(false);
                        return;
                    }

                    await WriteMemoryStatusAsync(context.Response, "memory user updated", selectedUserId).ConfigureAwait(false);
                    return;
                }
                case "delete_user":
                {
                    if (string.IsNullOrWhiteSpace(selectedUserId))
                    {
                        await WriteJsonAsync(context.Response, 400, "error", "user_id is required").ConfigureAwait(false);
                        return;
                    }

                    profilesObj.Remove(selectedUserId);

                    var keysToRemove = new List<string>();
                    foreach (var pair in identityMapObj.Linq)
                    {
                        var key = (pair.Key ?? string.Empty).Trim();
                        var node = pair.Value;
                        if (string.IsNullOrWhiteSpace(key) || node == null || !node.IsObject)
                        {
                            continue;
                        }

                        var mappedUser = (node["user_id"]?.Value ?? string.Empty).Trim();
                        if (string.Equals(mappedUser, selectedUserId, StringComparison.OrdinalIgnoreCase))
                        {
                            keysToRemove.Add(key);
                        }
                    }

                    for (int i = 0; i < keysToRemove.Count; i++)
                    {
                        identityMapObj.Remove(keysToRemove[i]);
                    }

                    var save = SaveDialogMemoryRoot(memoryPath, root);
                    if (!save.Success)
                    {
                        await WriteJsonAsync(context.Response, 500, "error", save.Error).ConfigureAwait(false);
                        return;
                    }

                    await WriteMemoryStatusAsync(context.Response, "memory user deleted", string.Empty).ConfigureAwait(false);
                    return;
                }
                default:
                    await WriteJsonAsync(context.Response, 400, "error", "unknown action").ConfigureAwait(false);
                    return;
            }
        }

        private async Task WriteMemoryStatusAsync(HttpListenerResponse response, string message, string selectedUserId)
        {
            var memoryPath = ResolveDialogUserMemoryPath();
            var load = LoadDialogMemoryRoot(memoryPath);
            if (!load.Success)
            {
                await WriteJsonAsync(response, 500, "error", load.Error).ConfigureAwait(false);
                return;
            }

            var root = load.Root;
            var profilesObj = EnsureObjectNode(root, "profiles");
            var identityMapObj = EnsureObjectNode(root, "identity_map");

            var users = new JSONArray();
            var userIds = new List<string>();
            foreach (var pair in profilesObj.Linq)
            {
                var userId = (pair.Key ?? string.Empty).Trim();
                if (!string.IsNullOrWhiteSpace(userId))
                {
                    userIds.Add(userId);
                }
            }
            userIds.Sort(StringComparer.OrdinalIgnoreCase);

            for (int i = 0; i < userIds.Count; i++)
            {
                var userId = userIds[i];
                var profile = profilesObj[userId] != null && profilesObj[userId].IsObject
                    ? profilesObj[userId].AsObject
                    : new JSONObject();
                users.Add(BuildMemoryUserSummary(userId, profile, identityMapObj));
            }

            JSONObject selectedProfile = null;
            var selectedIdentityKeys = new List<string>();
            if (!string.IsNullOrWhiteSpace(selectedUserId) && profilesObj[selectedUserId] != null && profilesObj[selectedUserId].IsObject)
            {
                selectedProfile = NormalizeMemoryProfile(
                    selectedUserId,
                    profilesObj[selectedUserId].AsObject,
                    profilesObj[selectedUserId].AsObject,
                    DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0);
                selectedIdentityKeys = BuildIdentityKeysForUser(identityMapObj, selectedUserId);
            }

            var payload = new JSONObject();
            payload["status"] = "ok";
            payload["message"] = message;
            payload["path"] = memoryPath;
            payload["user_count"] = users.Count;
            payload["users"] = users;
            payload["selected_user_id"] = string.IsNullOrWhiteSpace(selectedUserId) ? string.Empty : selectedUserId;
            payload["selected_profile"] = selectedProfile != null ? selectedProfile : new JSONObject();
            var selectedIdentityArray = new JSONArray();
            for (int i = 0; i < selectedIdentityKeys.Count; i++)
            {
                selectedIdentityArray.Add(selectedIdentityKeys[i]);
            }
            payload["selected_identity_keys"] = selectedIdentityArray;

            response.Headers["Cache-Control"] = "no-store, no-cache, must-revalidate";
            response.Headers["Pragma"] = "no-cache";
            response.Headers["Expires"] = "0";
            await WriteRawJsonAsync(response, 200, payload.ToString()).ConfigureAwait(false);
        }

        private string ResolveDialogUserMemoryPath()
        {
            var directEnv = ResolveAbsolutePathCandidate((Environment.GetEnvironmentVariable("DIALOG_USER_MEMORY_PATH") ?? string.Empty).Trim());
            if (!string.IsNullOrWhiteSpace(directEnv))
            {
                return directEnv;
            }

            var configPath = ResolveLauncherConfigPath();
            var defaultConfigPath = ResolveLauncherDefaultConfigPath();
            var load = LoadMergedLauncherConfigRoot(configPath, defaultConfigPath);
            if (load.Success && load.Root != null)
            {
                var envObj = EnsureObjectNode(load.Root, "env");
                var raw = (envObj["DIALOG_USER_MEMORY_PATH"]?.Value ?? string.Empty).Trim();
                if (!string.IsNullOrWhiteSpace(raw))
                {
                    var launcherEnv = LoadLauncherEnvOverrides();
                    var resolved = ResolvePathFromConfigOrPlaceholder(raw, ResolveProjectRootPath(), launcherEnv, allowCommandName: false);
                    if (!string.IsNullOrWhiteSpace(resolved))
                    {
                        return resolved;
                    }
                }
            }

            return Path.GetFullPath(Path.Combine(ResolveProjectRootPath(), "scripts", "dialog_service", "user_memory.json"));
        }

        private static (bool Success, JSONObject Root, string Error) LoadDialogMemoryRoot(string path)
        {
            try
            {
                JSONObject rootObj = null;
                if (File.Exists(path))
                {
                    var raw = File.ReadAllText(path, Encoding.UTF8);
                    if (!string.IsNullOrWhiteSpace(raw))
                    {
                        var parsed = JSONNode.Parse(raw);
                        if (parsed != null && parsed.IsObject)
                        {
                            rootObj = parsed.AsObject;
                        }
                    }
                }

                if (rootObj == null)
                {
                    rootObj = new JSONObject();
                }

                EnsureDialogMemoryShape(rootObj);
                return (true, rootObj, string.Empty);
            }
            catch (Exception ex)
            {
                return (false, null, $"failed to load dialog memory: {ex.Message}");
            }
        }

        private static (bool Success, string Error) SaveDialogMemoryRoot(string path, JSONObject root)
        {
            try
            {
                var parent = Path.GetDirectoryName(path);
                if (!string.IsNullOrWhiteSpace(parent))
                {
                    Directory.CreateDirectory(parent);
                }

                var payload = (root ?? new JSONObject()).ToString(2);
                var tempPath = path + ".tmp";
                File.WriteAllText(tempPath, payload, new UTF8Encoding(false));

                if (File.Exists(path))
                {
                    try
                    {
                        File.Replace(tempPath, path, null, true);
                    }
                    catch
                    {
                        File.Copy(tempPath, path, true);
                        File.Delete(tempPath);
                    }
                }
                else
                {
                    File.Move(tempPath, path);
                }
                return (true, string.Empty);
            }
            catch (Exception ex)
            {
                return (false, $"failed to save dialog memory: {ex.Message}");
            }
        }

        private static void EnsureDialogMemoryShape(JSONObject root)
        {
            if (root == null)
            {
                return;
            }

            if (root["identity_map"] == null || !root["identity_map"].IsObject)
            {
                root["identity_map"] = new JSONObject();
            }
            if (root["profiles"] == null || !root["profiles"].IsObject)
            {
                root["profiles"] = new JSONObject();
            }

            var currentNext = ParseMemoryInt(root["next_user_index"], 1);
            if (currentNext < 1)
            {
                currentNext = 1;
            }
            root["next_user_index"] = currentNext;
        }

        private static int ParseMemoryInt(JSONNode node, int fallback)
        {
            if (node == null)
            {
                return fallback;
            }
            var raw = (node.Value ?? string.Empty).Trim();
            int value;
            return int.TryParse(raw, NumberStyles.Integer, CultureInfo.InvariantCulture, out value) ? value : fallback;
        }

        private static double ParseMemoryDouble(JSONNode node, double fallback)
        {
            if (node == null)
            {
                return fallback;
            }
            var raw = (node.Value ?? string.Empty).Trim();
            double value;
            return double.TryParse(raw, NumberStyles.Float | NumberStyles.AllowThousands, CultureInfo.InvariantCulture, out value)
                ? value
                : fallback;
        }

        private static List<string> ParseMemoryStringList(JSONNode node, int maxItems, int maxCharsPerItem)
        {
            var result = new List<string>();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            if (node == null)
            {
                return result;
            }

            if (node.IsArray)
            {
                var arr = node.AsArray;
                for (int i = 0; i < arr.Count; i++)
                {
                    var text = (arr[i]?.Value ?? string.Empty).Trim();
                    if (string.IsNullOrWhiteSpace(text))
                    {
                        continue;
                    }
                    if (text.Length > maxCharsPerItem)
                    {
                        text = text.Substring(0, maxCharsPerItem).Trim();
                    }
                    if (!string.IsNullOrWhiteSpace(text) && seen.Add(text))
                    {
                        result.Add(text);
                    }
                    if (result.Count >= maxItems)
                    {
                        break;
                    }
                }
                return result;
            }

            var rawText = (node.Value ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(rawText))
            {
                return result;
            }

            var merged = rawText
                .Replace("\r\n", "\n")
                .Replace('\uFF0C', ',')
                .Replace('\uFF1B', ';')
                .Replace(';', '\n')
                .Replace(',', '\n');
            var parts = merged.Split('\n');
            for (int i = 0; i < parts.Length; i++)
            {
                var text = (parts[i] ?? string.Empty).Trim();
                if (string.IsNullOrWhiteSpace(text))
                {
                    continue;
                }
                if (text.Length > maxCharsPerItem)
                {
                    text = text.Substring(0, maxCharsPerItem).Trim();
                }
                if (!string.IsNullOrWhiteSpace(text) && seen.Add(text))
                {
                    result.Add(text);
                }
                if (result.Count >= maxItems)
                {
                    break;
                }
            }
            return result;
        }

        private static JSONArray ToJsonArray(List<string> items)
        {
            var arr = new JSONArray();
            if (items == null)
            {
                return arr;
            }
            for (int i = 0; i < items.Count; i++)
            {
                var text = (items[i] ?? string.Empty).Trim();
                if (!string.IsNullOrWhiteSpace(text))
                {
                    arr.Add(text);
                }
            }
            return arr;
        }

        private static JSONObject NormalizeMemoryProfile(string userId, JSONObject source, JSONObject existing, double nowTs)
        {
            var output = new JSONObject();
            var src = source ?? new JSONObject();
            var prev = existing ?? new JSONObject();

            var displayNameNode = PickMemoryNode(src, prev, "display_name");
            var displayName = (displayNameNode?.Value ?? userId ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(displayName))
            {
                displayName = userId;
            }
            output["display_name"] = displayName;

            var nameNode = PickMemoryNode(src, prev, "name");
            output["name"] = (nameNode?.Value ?? string.Empty).Trim();

            output["likes"] = ToJsonArray(ParseMemoryStringList(PickMemoryNode(src, prev, "likes"), 24, 80));
            output["dislikes"] = ToJsonArray(ParseMemoryStringList(PickMemoryNode(src, prev, "dislikes"), 24, 80));
            output["goals"] = ToJsonArray(ParseMemoryStringList(PickMemoryNode(src, prev, "goals"), 24, 120));
            output["recent_notes"] = ToJsonArray(ParseMemoryStringList(PickMemoryNode(src, prev, "recent_notes"), 64, 180));

            var memoryItems = PickMemoryNode(src, prev, "memory_items");
            if (memoryItems != null && memoryItems.IsArray)
            {
                output["memory_items"] = CloneJsonNode(memoryItems) ?? new JSONArray();
            }
            else
            {
                output["memory_items"] = new JSONArray();
            }

            var firstSeen = ParseMemoryDouble(PickMemoryNode(src, prev, "first_seen_ts"), nowTs);
            if (firstSeen <= 0)
            {
                firstSeen = nowTs;
            }
            output["first_seen_ts"] = firstSeen;

            var lastSeen = ParseMemoryDouble(PickMemoryNode(src, prev, "last_seen_ts"), nowTs);
            if (lastSeen <= 0)
            {
                lastSeen = nowTs;
            }
            output["last_seen_ts"] = lastSeen;

            var utteranceCount = ParseMemoryInt(PickMemoryNode(src, prev, "utterance_count"), 0);
            if (utteranceCount < 0)
            {
                utteranceCount = 0;
            }
            output["utterance_count"] = utteranceCount;
            return output;
        }

        private static JSONNode PickMemoryNode(JSONObject source, JSONObject fallback, string key)
        {
            if (source != null && !string.IsNullOrWhiteSpace(key) && source.HasKey(key))
            {
                return source[key];
            }
            if (fallback != null && !string.IsNullOrWhiteSpace(key) && fallback.HasKey(key))
            {
                return fallback[key];
            }
            return null;
        }

        private static List<string> BuildIdentityKeysForUser(JSONObject identityMapObj, string userId)
        {
            var keys = new List<string>();
            if (identityMapObj == null || string.IsNullOrWhiteSpace(userId))
            {
                return keys;
            }

            foreach (var pair in identityMapObj.Linq)
            {
                var key = (pair.Key ?? string.Empty).Trim();
                var node = pair.Value;
                if (string.IsNullOrWhiteSpace(key) || node == null || !node.IsObject)
                {
                    continue;
                }

                var mappedUser = (node["user_id"]?.Value ?? string.Empty).Trim();
                if (string.Equals(mappedUser, userId, StringComparison.OrdinalIgnoreCase))
                {
                    keys.Add(key);
                }
            }

            keys.Sort(StringComparer.OrdinalIgnoreCase);
            return keys;
        }

        private static int CountArrayItems(JSONObject profile, string key)
        {
            if (profile == null || string.IsNullOrWhiteSpace(key))
            {
                return 0;
            }
            var node = profile[key];
            if (node == null || !node.IsArray)
            {
                return 0;
            }
            return node.AsArray.Count;
        }

        private static string UnixSecondsToIso8601(double value)
        {
            if (value <= 0)
            {
                return string.Empty;
            }
            try
            {
                var ticks = (long)Math.Round(value * 1000.0);
                var dt = DateTimeOffset.FromUnixTimeMilliseconds(ticks).UtcDateTime;
                return dt.ToString("o", CultureInfo.InvariantCulture);
            }
            catch
            {
                return string.Empty;
            }
        }

        private static void EnsureNextUserIndex(JSONObject root, string userId)
        {
            if (root == null || string.IsNullOrWhiteSpace(userId))
            {
                return;
            }

            var text = userId.Trim();
            if (!text.StartsWith("user_", StringComparison.OrdinalIgnoreCase))
            {
                return;
            }

            var suffix = text.Substring(5);
            int parsed;
            if (!int.TryParse(suffix, NumberStyles.Integer, CultureInfo.InvariantCulture, out parsed))
            {
                return;
            }

            var current = ParseMemoryInt(root["next_user_index"], 1);
            var expected = parsed + 1;
            if (expected > current)
            {
                root["next_user_index"] = expected;
            }
        }

        private static JSONObject BuildMemoryUserSummary(string userId, JSONObject profile, JSONObject identityMapObj)
        {
            var summary = new JSONObject();
            summary["user_id"] = userId;
            var displayName = (profile["display_name"]?.Value ?? userId).Trim();
            summary["display_name"] = string.IsNullOrWhiteSpace(displayName) ? userId : displayName;
            summary["name"] = (profile["name"]?.Value ?? string.Empty).Trim();

            var utteranceCount = ParseMemoryInt(profile["utterance_count"], 0);
            if (utteranceCount < 0)
            {
                utteranceCount = 0;
            }
            summary["utterance_count"] = utteranceCount;

            var lastSeenTs = ParseMemoryDouble(profile["last_seen_ts"], 0.0);
            summary["last_seen_ts"] = lastSeenTs;
            summary["last_seen_iso"] = UnixSecondsToIso8601(lastSeenTs);

            summary["likes_count"] = CountArrayItems(profile, "likes");
            summary["dislikes_count"] = CountArrayItems(profile, "dislikes");
            summary["goals_count"] = CountArrayItems(profile, "goals");
            summary["recent_notes_count"] = CountArrayItems(profile, "recent_notes");
            summary["memory_items_count"] = CountArrayItems(profile, "memory_items");

            var identityKeys = BuildIdentityKeysForUser(identityMapObj, userId);
            var identityArray = new JSONArray();
            var sampleCount = 0;
            for (int i = 0; i < identityKeys.Count; i++)
            {
                var key = identityKeys[i];
                identityArray.Add(key);
                var record = identityMapObj[key];
                var c = ParseMemoryInt(record?["sample_count"], 0);
                if (c > 0)
                {
                    sampleCount += c;
                }
            }
            summary["identity_count"] = identityKeys.Count;
            summary["sample_count"] = sampleCount;
            summary["identity_keys"] = identityArray;

            return summary;
        }

    }
}
