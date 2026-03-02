using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

namespace RobotVoice
{
    public sealed partial class UserTestControlPanel
    {
        private async Task HandleGameAsync(HttpListenerContext context)
        {
            var request = ParseJsonBody<GameRequest>(context.Request);
            var action = (request.action ?? string.Empty).Trim().ToLowerInvariant();
            if (voiceLauncher == null)
            {
                await WriteJsonAsync(context.Response, 503, "error", "VoiceGameLauncher not assigned").ConfigureAwait(false);
                return;
            }

            switch (action)
            {
                case "launch":
                case "open":
                    var gameName = (request.name ?? string.Empty).Trim();
                    if (string.IsNullOrEmpty(gameName))
                    {
                        await WriteJsonAsync(context.Response, 400, "error", "game name required").ConfigureAwait(false);
                        return;
                    }
					PostToMainThread(() => voiceLauncher.TriggerLaunchForTester(gameName));
                    await WriteJsonAsync(context.Response, 200, "ok", $"launching {gameName}").ConfigureAwait(false);
                    return;
                case "exit":
                case "close":
					PostToMainThread(() => voiceLauncher.TriggerExitForTester());
                    await WriteJsonAsync(context.Response, 200, "ok", "exit intent sent").ConfigureAwait(false);
                    return;
                default:
                    await WriteJsonAsync(context.Response, 400, "error", "unknown game action").ConfigureAwait(false);
                    return;
            }
        }

        private async Task HandleGameManifestAsync(HttpListenerContext context)
        {
            var method = (context.Request.HttpMethod ?? string.Empty).Trim().ToUpperInvariant();
            if (method == "GET")
            {
                await WriteGameManifestStatusAsync(context.Response).ConfigureAwait(false);
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
                requestNode = JSONNode.Parse(body);
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(context.Response, 400, "error", $"invalid json body: {ex.Message}").ConfigureAwait(false);
                return;
            }

            var gamesNode = requestNode?["games"];
            if (gamesNode == null || !gamesNode.IsArray)
            {
                await WriteJsonAsync(context.Response, 400, "error", "games array is required").ConfigureAwait(false);
                return;
            }

            var manifestPath = ResolveGameManifestPath();
            var load = LoadGameManifestRoot(manifestPath);
            if (!load.Success)
            {
                await WriteJsonAsync(context.Response, 500, "error", load.Error).ConfigureAwait(false);
                return;
            }
            var manifestBaseDir = Path.GetDirectoryName(manifestPath);
            if (string.IsNullOrWhiteSpace(manifestBaseDir))
            {
                manifestBaseDir = ResolveProjectRootPath();
            }
            var launcherEnv = LoadLauncherEnvOverrides();

            var existingRoot = load.Root;
            var existingById = new Dictionary<string, JSONObject>(StringComparer.OrdinalIgnoreCase);
            var oldGames = existingRoot["games"];
            if (oldGames != null && oldGames.IsArray)
            {
                var oldArray = oldGames.AsArray;
                for (int i = 0; i < oldArray.Count; i++)
                {
                    var oldNode = oldArray[i];
                    if (oldNode == null || !oldNode.IsObject)
                    {
                        continue;
                    }

                    var oldObj = oldNode.AsObject;
                    var oldId = NormalizeGameId((oldObj["id"]?.Value ?? string.Empty).Trim());
                    if (string.IsNullOrWhiteSpace(oldId))
                    {
                        continue;
                    }
                    existingById[oldId] = oldObj;
                }
            }

            var nextGames = new JSONArray();
            var seenIds = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var reqArray = gamesNode.AsArray;
            for (int i = 0; i < reqArray.Count; i++)
            {
                var row = reqArray[i];
                if (row == null || !row.IsObject)
                {
                    continue;
                }

                var rowObj = row.AsObject;
                var rawId = (rowObj["id"]?.Value ?? string.Empty).Trim();
                var rawName = (rowObj["name"]?.Value ?? string.Empty).Trim();
                var id = NormalizeGameId(string.IsNullOrWhiteSpace(rawId) ? rawName : rawId);
                if (string.IsNullOrWhiteSpace(id))
                {
                    await WriteJsonAsync(context.Response, 400, "error", $"invalid game id at index {i}").ConfigureAwait(false);
                    return;
                }
                if (!seenIds.Add(id))
                {
                    await WriteJsonAsync(context.Response, 400, "error", $"duplicate game id: {id}").ConfigureAwait(false);
                    return;
                }

                var name = string.IsNullOrWhiteSpace(rawName) ? id : rawName;
                var rawExecInput = (rowObj["exec"]?.Value ?? string.Empty).Trim();
                var rawWorkdirInput = (rowObj["workdir"]?.Value ?? string.Empty).Trim();
                var exec = ResolvePathFromConfigOrPlaceholder(
                    rawExecInput,
                    manifestBaseDir,
                    launcherEnv,
                    allowCommandName: true);
                var workdir = ResolvePathFromConfigOrPlaceholder(
                    rawWorkdirInput,
                    manifestBaseDir,
                    launcherEnv,
                    allowCommandName: false);
                if (!string.IsNullOrWhiteSpace(rawExecInput) && string.IsNullOrWhiteSpace(exec))
                {
                    await WriteJsonAsync(
                        context.Response,
                        400,
                        "error",
                        $"game '{id}' executable path is unresolved. Please provide an absolute path.")
                        .ConfigureAwait(false);
                    return;
                }
                if (!string.IsNullOrWhiteSpace(rawWorkdirInput) && string.IsNullOrWhiteSpace(workdir))
                {
                    await WriteJsonAsync(
                        context.Response,
                        400,
                        "error",
                        $"game '{id}' workdir is unresolved. Please provide an absolute path.")
                        .ConfigureAwait(false);
                    return;
                }
                var keywords = ParseKeywordList(rowObj);

                JSONObject target;
                if (!existingById.TryGetValue(id, out target))
                {
                    target = new JSONObject();
                }

                target["id"] = id;
                target["name"] = name;
                target["exec"] = exec;
                target["workdir"] = workdir;

                var synonyms = new JSONArray();
                foreach (var keyword in keywords)
                {
                    synonyms.Add(keyword);
                }
                target["synonyms"] = synonyms;

                if (target["args"] == null || !target["args"].IsArray)
                {
                    target["args"] = new JSONArray();
                }
                if (target["env"] == null || !target["env"].IsObject)
                {
                    target["env"] = new JSONObject();
                }

                nextGames.Add(target);
            }

            existingRoot["games"] = nextGames;

            try
            {
                var parent = Path.GetDirectoryName(manifestPath);
                if (!string.IsNullOrWhiteSpace(parent))
                {
                    Directory.CreateDirectory(parent);
                }
                File.WriteAllText(manifestPath, existingRoot.ToString(2), Encoding.UTF8);
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(context.Response, 500, "error", $"failed to save manifest: {ex.Message}").ConfigureAwait(false);
                return;
            }

            await WriteRawJsonAsync(
                context.Response,
                200,
                "{\"status\":\"ok\",\"message\":\"saved. restart intent_service and game_launcher to apply immediately.\"}")
                .ConfigureAwait(false);
        }

        private async Task HandleFilePickAsync(HttpListenerContext context)
        {
            var method = (context.Request.HttpMethod ?? string.Empty).Trim().ToUpperInvariant();
            if (method != "POST")
            {
                context.Response.StatusCode = 405;
                context.Response.Headers["Allow"] = "POST,OPTIONS";
                await WriteJsonAsync(context.Response, 405, "error", "method not allowed").ConfigureAwait(false);
                return;
            }

            var request = ParseJsonBody<FilePickRequest>(context.Request);
            var title = string.IsNullOrWhiteSpace(request.title) ? "Select File" : request.title.Trim();
            var filter = string.IsNullOrWhiteSpace(request.filter)
                ? "Executable Files (*.exe)|*.exe|All Files (*.*)|*.*"
                : request.filter.Trim();
            var projectRoot = ResolveProjectRootPath();
            var initialDir = NormalizePathOrCommandForConfig(request.initial_dir, projectRoot, allowCommandName: false);
            var initialFile = NormalizePathOrCommandForConfig(request.initial_filename, projectRoot, allowCommandName: false);
            var pick = await Task.Run(() => ShowHostOpenFileDialog(title, filter, initialDir, initialFile)).ConfigureAwait(false);
            if (!pick.Success)
            {
                await WriteJsonAsync(context.Response, 500, "error", pick.Error).ConfigureAwait(false);
                return;
            }

            var payload = new JSONObject();
            payload["status"] = "ok";
            payload["cancelled"] = pick.Cancelled;
            payload["path"] = pick.Path;
            payload["directory"] = string.IsNullOrWhiteSpace(pick.Path)
                ? string.Empty
                : (Path.GetDirectoryName(pick.Path) ?? string.Empty);
            await WriteRawJsonAsync(context.Response, 200, payload.ToString()).ConfigureAwait(false);
        }

        private static (bool Success, bool Cancelled, string Path, string Error) ShowHostOpenFileDialog(
            string title,
            string filter,
            string initialDir,
            string initialFile)
        {
            var done = new ManualResetEvent(false);
            var success = false;
            var cancelled = true;
            var selectedPath = string.Empty;
            var error = string.Empty;

            void Work()
            {
                try
                {
                    var formsAssembly = Assembly.Load("System.Windows.Forms");
                    var openFileDialogType = formsAssembly.GetType("System.Windows.Forms.OpenFileDialog", throwOnError: false);
                    if (openFileDialogType == null)
                    {
                        error = "native file dialog not available on this runtime";
                        return;
                    }

                    var dialog = Activator.CreateInstance(openFileDialogType);
                    if (dialog == null)
                    {
                        error = "failed to create file dialog instance";
                        return;
                    }

                    SetReflectedProperty(openFileDialogType, dialog, "Title", title);
                    SetReflectedProperty(openFileDialogType, dialog, "Filter", filter);
                    SetReflectedProperty(openFileDialogType, dialog, "CheckFileExists", true);
                    SetReflectedProperty(openFileDialogType, dialog, "Multiselect", false);
                    SetReflectedProperty(openFileDialogType, dialog, "RestoreDirectory", true);
                    if (!string.IsNullOrWhiteSpace(initialDir) && Directory.Exists(initialDir))
                    {
                        SetReflectedProperty(openFileDialogType, dialog, "InitialDirectory", initialDir);
                    }
                    if (!string.IsNullOrWhiteSpace(initialFile))
                    {
                        SetReflectedProperty(openFileDialogType, dialog, "FileName", initialFile);
                    }

                    var showDialogMethod = openFileDialogType.GetMethod("ShowDialog", Type.EmptyTypes);
                    if (showDialogMethod == null)
                    {
                        error = "file dialog ShowDialog method not found";
                        return;
                    }

                    var result = showDialogMethod.Invoke(dialog, null);
                    var code = Convert.ToInt32(result, CultureInfo.InvariantCulture);
                    // System.Windows.Forms.DialogResult.OK == 1
                    if (code != 1)
                    {
                        success = true;
                        cancelled = true;
                        return;
                    }

                    var fileNameObj = openFileDialogType.GetProperty("FileName")?.GetValue(dialog, null);
                    var filePathRaw = fileNameObj as string;
                    var filePath = string.IsNullOrWhiteSpace(filePathRaw) ? string.Empty : filePathRaw.Trim();
                    if (string.IsNullOrWhiteSpace(filePath))
                    {
                        success = true;
                        cancelled = true;
                        return;
                    }

                    selectedPath = Path.GetFullPath(filePath);
                    success = true;
                    cancelled = false;
                }
                catch (Exception ex)
                {
                    error = ex.Message;
                }
                finally
                {
                    done.Set();
                }
            }

            try
            {
                var thread = new Thread(Work);
                thread.SetApartmentState(ApartmentState.STA);
                thread.IsBackground = true;
                thread.Start();
                done.WaitOne();
            }
            catch (Exception ex)
            {
                return (false, false, string.Empty, ex.Message);
            }
            finally
            {
                done.Dispose();
            }

            if (!string.IsNullOrWhiteSpace(error))
            {
                return (false, false, string.Empty, error);
            }
            return (success, cancelled, selectedPath, string.Empty);
        }

        private static void SetReflectedProperty(Type targetType, object instance, string name, object value)
        {
            if (targetType == null || instance == null || string.IsNullOrWhiteSpace(name))
            {
                return;
            }

            var prop = targetType.GetProperty(name);
            if (prop == null || !prop.CanWrite)
            {
                return;
            }

            try
            {
                prop.SetValue(instance, value, null);
            }
            catch
            {
                // Ignore unsupported property assignments.
            }
        }

        private async Task WriteGameManifestStatusAsync(HttpListenerResponse response)
        {
            var manifestPath = ResolveGameManifestPath();
            var load = LoadGameManifestRoot(manifestPath);
            if (!load.Success)
            {
                await WriteJsonAsync(response, 500, "error", load.Error).ConfigureAwait(false);
                return;
            }
            var manifestBaseDir = Path.GetDirectoryName(manifestPath);
            if (string.IsNullOrWhiteSpace(manifestBaseDir))
            {
                manifestBaseDir = ResolveProjectRootPath();
            }
            var launcherEnv = LoadLauncherEnvOverrides();

            var root = load.Root;
            var outGames = new JSONArray();
            var unresolvedCount = 0;
            var gamesNode = root["games"];
            if (gamesNode != null && gamesNode.IsArray)
            {
                var gamesArray = gamesNode.AsArray;
                for (int i = 0; i < gamesArray.Count; i++)
                {
                    var node = gamesArray[i];
                    if (node == null || !node.IsObject)
                    {
                        continue;
                    }

                    var obj = node.AsObject;
                    var id = (obj["id"]?.Value ?? string.Empty).Trim();
                    if (string.IsNullOrWhiteSpace(id))
                    {
                        continue;
                    }

                    var item = new JSONObject();
                    item["id"] = id;
                    item["name"] = (obj["name"]?.Value ?? string.Empty).Trim();
                    var rawExec = (obj["exec"]?.Value ?? string.Empty).Trim();
                    var rawWorkdir = (obj["workdir"]?.Value ?? string.Empty).Trim();
                    var resolvedExec = ResolvePathFromConfigOrPlaceholder(
                        rawExec,
                        manifestBaseDir,
                        launcherEnv,
                        allowCommandName: true);
                    var resolvedWorkdir = ResolvePathFromConfigOrPlaceholder(
                        rawWorkdir,
                        manifestBaseDir,
                        launcherEnv,
                        allowCommandName: false);
                    if (!string.IsNullOrWhiteSpace(rawExec) && string.IsNullOrWhiteSpace(resolvedExec))
                    {
                        unresolvedCount++;
                    }
                    if (!string.IsNullOrWhiteSpace(rawWorkdir) && string.IsNullOrWhiteSpace(resolvedWorkdir))
                    {
                        unresolvedCount++;
                    }
                    item["exec"] = resolvedExec;
                    item["workdir"] = resolvedWorkdir;

                    var keywords = new JSONArray();
                    var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                    var synonymsNode = obj["synonyms"];
                    if (synonymsNode != null && synonymsNode.IsArray)
                    {
                        var synonyms = synonymsNode.AsArray;
                        for (int j = 0; j < synonyms.Count; j++)
                        {
                            var text = (synonyms[j]?.Value ?? string.Empty).Trim();
                            if (!string.IsNullOrWhiteSpace(text) && seen.Add(text))
                            {
                                keywords.Add(text);
                            }
                        }
                    }
                    item["keywords"] = keywords;
                    outGames.Add(item);
                }
            }

            var payload = new JSONObject();
            payload["status"] = "ok";
            payload["path"] = manifestPath;
            payload["unresolved_count"] = unresolvedCount;
            payload["games"] = outGames;
            await WriteRawJsonAsync(response, 200, payload.ToString()).ConfigureAwait(false);
        }

        private static string ReadRequestBody(HttpListenerRequest request)
        {
            using (var reader = new StreamReader(request.InputStream, request.ContentEncoding ?? Encoding.UTF8))
            {
                return reader.ReadToEnd();
            }
        }

        private static string NormalizeGameId(string raw)
        {
            if (string.IsNullOrWhiteSpace(raw))
            {
                return string.Empty;
            }

            var text = raw.Trim();
            var sb = new StringBuilder(text.Length);
            var wroteUnderscore = false;
            for (int i = 0; i < text.Length; i++)
            {
                var ch = char.ToLowerInvariant(text[i]);
                if ((ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9'))
                {
                    sb.Append(ch);
                    wroteUnderscore = false;
                    continue;
                }

                if (ch == '_' || ch == '-' || char.IsWhiteSpace(ch))
                {
                    if (!wroteUnderscore && sb.Length > 0)
                    {
                        sb.Append('_');
                        wroteUnderscore = true;
                    }
                }
            }

            var normalized = sb.ToString().Trim('_');
            return normalized;
        }

        private static List<string> ParseKeywordList(JSONObject rowObj)
        {
            var result = new List<string>();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

            var keywordsNode = rowObj["keywords"];
            if (keywordsNode != null && keywordsNode.IsArray)
            {
                var arr = keywordsNode.AsArray;
                for (int i = 0; i < arr.Count; i++)
                {
                    var value = (arr[i]?.Value ?? string.Empty).Trim();
                    if (!string.IsNullOrWhiteSpace(value) && seen.Add(value))
                    {
                        result.Add(value);
                    }
                }
            }

            var keywordsText = (rowObj["keywords_text"]?.Value ?? string.Empty).Trim();
            if (!string.IsNullOrWhiteSpace(keywordsText))
            {
                var parts = keywordsText.Split(',');
                for (int i = 0; i < parts.Length; i++)
                {
                    var value = (parts[i] ?? string.Empty).Trim();
                    if (!string.IsNullOrWhiteSpace(value) && seen.Add(value))
                    {
                        result.Add(value);
                    }
                }
            }

            return result;
        }

    }
}
