using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

namespace RobotVoice
{
    public sealed partial class UserTestControlPanel
    {
        private async Task HandleRuntimeConfigAsync(HttpListenerContext context)
        {
            var method = (context.Request.HttpMethod ?? string.Empty).Trim().ToUpperInvariant();
            if (method == "GET")
            {
                await WriteRuntimeConfigStatusAsync(context.Response, "runtime config").ConfigureAwait(false);
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
            var configPath = ResolveLauncherConfigPath();
            var load = LoadLauncherConfigRoot(configPath);
            if (!load.Success)
            {
                await WriteJsonAsync(context.Response, 500, "error", load.Error).ConfigureAwait(false);
                return;
            }

            var root = load.Root;
            var pythonObj = EnsureObjectNode(root, "python");
            var pathsObj = EnsureObjectNode(root, "paths");
            var openaiObj = EnsureObjectNode(root, "openai");
            var intentObj = EnsureObjectNode(root, "intent");
            var envObj = EnsureObjectNode(root, "env");
            var projectRoot = ResolveProjectRootPath();

            string value;
            if (TryReadOptionalString(requestObj, "asr_python", out value))
            {
                SetOrRemoveString(
                    pythonObj,
                    "asr",
                    NormalizePathOrCommandForConfig(value, projectRoot, allowCommandName: true));
            }
            if (TryReadOptionalString(requestObj, "tts_python", out value))
            {
                SetOrRemoveString(
                    pythonObj,
                    "tts",
                    NormalizePathOrCommandForConfig(value, projectRoot, allowCommandName: true));
            }
            if (TryReadOptionalString(requestObj, "intent_manifest_path", out value))
            {
                SetOrRemoveString(
                    pathsObj,
                    "intent_manifest",
                    NormalizePathOrCommandForConfig(value, projectRoot, allowCommandName: false));
            }
            if (TryReadOptionalString(requestObj, "game_manifest_path", out value))
            {
                SetOrRemoveString(
                    pathsObj,
                    "game_manifest",
                    NormalizePathOrCommandForConfig(value, projectRoot, allowCommandName: false));
            }
            if (TryReadOptionalString(requestObj, "openai_api_key", out value))
            {
                SetOrRemoveString(openaiObj, "api_key", value);
            }
            if (TryReadOptionalString(requestObj, "openai_transcribe_model", out value))
            {
                SetOrRemoveString(openaiObj, "transcribe_model", value);
            }
            if (TryReadOptionalString(requestObj, "openai_base_url", out value))
            {
                SetOrRemoveString(openaiObj, "base_url", value);
            }
            if (TryReadOptionalString(requestObj, "openai_transcribe_prompt", out value))
            {
                SetOrRemoveString(openaiObj, "transcribe_prompt", value);
            }
            if (TryReadOptionalString(requestObj, "ollama_model", out value))
            {
                SetOrRemoveString(envObj, "OLLAMA_MODEL", value);
            }
            if (TryReadOptionalString(requestObj, "conversation_pipeline_mode", out value))
            {
                SetOrRemoveString(envObj, "VOICE_PIPELINE_MODE", NormalizeConversationPipelineModeForConfig(value));
            }
            if (TryReadOptionalString(requestObj, "conversation_profile", out value))
            {
                SetOrRemoveString(envObj, "VOICE_CONVERSATION_PROFILE", NormalizeConversationProfileForConfig(value));
            }
            if (TryReadOptionalString(requestObj, "local_asr_mode", out value))
            {
                var normalizedAsr = NormalizeAsrMode(value);
                SetOrRemoveString(envObj, "VOICE_LOCAL_ASR_MODE", string.IsNullOrWhiteSpace(normalizedAsr) ? string.Empty : normalizedAsr);
            }
            if (TryReadOptionalString(requestObj, "cloud_asr_mode", out value))
            {
                var normalizedAsr = NormalizeAsrMode(value);
                SetOrRemoveString(envObj, "VOICE_CLOUD_ASR_MODE", string.IsNullOrWhiteSpace(normalizedAsr) ? string.Empty : normalizedAsr);
            }
            if (TryReadOptionalString(requestObj, "openai_response_model", out value))
            {
                SetOrRemoveString(envObj, "OPENAI_RESPONSE_MODEL", value);
            }
            if (TryReadOptionalString(requestObj, "launch_triggers", out value))
            {
                SetOrRemoveStringList(intentObj, "launch_triggers", ParsePhraseList(value));
            }
            if (TryReadOptionalString(requestObj, "exit_keywords", out value))
            {
                SetOrRemoveStringList(intentObj, "exit_keywords", ParsePhraseList(value));
            }
            bool boolValue;
            if (TryReadOptionalBool(requestObj, "use_llm_intent_classifier", out boolValue))
            {
                SetOrRemoveString(intentObj, "use_llm_classifier", boolValue ? "true" : "false");
            }
            if (TryReadOptionalBool(requestObj, "use_moonshine_intent_recognizer", out boolValue))
            {
                SetOrRemoveString(intentObj, "use_moonshine_intent_recognizer", boolValue ? "true" : "false");
            }

            // Clean up legacy flat keys when intent rules are stored in nested intent object.
            root.Remove("launch_triggers");
            root.Remove("exit_keywords");

            try
            {
                var parent = Path.GetDirectoryName(configPath);
                if (!string.IsNullOrWhiteSpace(parent))
                {
                    Directory.CreateDirectory(parent);
                }
                File.WriteAllText(configPath, root.ToString(2), Encoding.UTF8);
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(context.Response, 500, "error", $"failed to save launcher config: {ex.Message}").ConfigureAwait(false);
                return;
            }

            var liveApplyMessage = await ApplyConversationRuntimeChangesAsync(
                ReadEnvString(envObj, "VOICE_PIPELINE_MODE", "direct_unified"),
                ReadEnvString(envObj, "VOICE_CONVERSATION_PROFILE", "local"),
                ReadEnvString(envObj, "VOICE_LOCAL_ASR_MODE", "moonshine-medium"),
                ReadEnvString(envObj, "VOICE_CLOUD_ASR_MODE", "api"),
                (openaiObj["api_key"]?.Value ?? string.Empty).Trim(),
                (openaiObj["base_url"]?.Value ?? string.Empty).Trim(),
                (openaiObj["transcribe_model"]?.Value ?? string.Empty).Trim(),
                (openaiObj["transcribe_prompt"]?.Value ?? string.Empty).Trim(),
                ReadEnvString(envObj, "OPENAI_RESPONSE_MODEL", "gpt-4o-mini"),
                ReadEnvString(envObj, "OLLAMA_MODEL", VoiceAgentDefaults.DefaultVisionModel)).ConfigureAwait(false);

            await WriteRuntimeConfigStatusAsync(context.Response, liveApplyMessage).ConfigureAwait(false);
        }

        private async Task WriteRuntimeConfigStatusAsync(HttpListenerResponse response, string message)
        {
            var configPath = ResolveLauncherConfigPath();
            var defaultConfigPath = ResolveLauncherDefaultConfigPath();
            var load = LoadMergedLauncherConfigRoot(configPath, defaultConfigPath);
            if (!load.Success)
            {
                await WriteJsonAsync(response, 500, "error", load.Error).ConfigureAwait(false);
                return;
            }

            var root = load.Root;
            var pythonObj = EnsureObjectNode(root, "python");
            var pathsObj = EnsureObjectNode(root, "paths");
            var openaiObj = EnsureObjectNode(root, "openai");
            var intentObj = EnsureObjectNode(root, "intent");
            var envObj = EnsureObjectNode(root, "env");
            var projectRoot = ResolveProjectRootPath();
            var launchTriggers = ReadStringList(intentObj, "launch_triggers");
            if (launchTriggers.Count == 0)
            {
                launchTriggers = ReadStringList(root, "launch_triggers");
            }
            if (launchTriggers.Count == 0)
            {
                launchTriggers = GetDefaultLaunchTriggers();
            }
            var exitKeywords = ReadStringList(intentObj, "exit_keywords");
            if (exitKeywords.Count == 0)
            {
                exitKeywords = ReadStringList(root, "exit_keywords");
            }
            if (exitKeywords.Count == 0)
            {
                exitKeywords = GetDefaultExitKeywords();
            }
            var useLlmIntentClassifier = ReadOptionalBool(intentObj, "use_llm_classifier", false);
            var useMoonshineIntentRecognizer = ReadOptionalBool(
                intentObj,
                "use_moonshine_intent_recognizer",
                false);

            var openaiApiKey = (openaiObj["api_key"]?.Value ?? string.Empty).Trim();
            var payload = new JSONObject();
            payload["status"] = "ok";
            payload["message"] = message;
            payload["path"] = configPath;
            payload["default_path"] = defaultConfigPath;
            payload["asr_python"] = NormalizePathOrCommandForConfig(
                (pythonObj["asr"]?.Value ?? string.Empty).Trim(),
                projectRoot,
                allowCommandName: true);
            payload["tts_python"] = NormalizePathOrCommandForConfig(
                (pythonObj["tts"]?.Value ?? string.Empty).Trim(),
                projectRoot,
                allowCommandName: true);
            payload["intent_manifest_path"] = NormalizePathOrCommandForConfig(
                (pathsObj["intent_manifest"]?.Value ?? string.Empty).Trim(),
                projectRoot,
                allowCommandName: false);
            payload["game_manifest_path"] = NormalizePathOrCommandForConfig(
                (pathsObj["game_manifest"]?.Value ?? string.Empty).Trim(),
                projectRoot,
                allowCommandName: false);
            payload["openai_api_key"] = openaiApiKey;
            payload["openai_api_key_set"] = !string.IsNullOrWhiteSpace(openaiApiKey);
            payload["openai_transcribe_model"] = (openaiObj["transcribe_model"]?.Value ?? string.Empty).Trim();
            payload["openai_base_url"] = (openaiObj["base_url"]?.Value ?? string.Empty).Trim();
            payload["openai_transcribe_prompt"] = (openaiObj["transcribe_prompt"]?.Value ?? string.Empty).Trim();
            var ollamaModel = (envObj["OLLAMA_MODEL"]?.Value ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(ollamaModel))
            {
                ollamaModel = (Environment.GetEnvironmentVariable("OLLAMA_MODEL") ?? string.Empty).Trim();
            }
            if (string.IsNullOrWhiteSpace(ollamaModel))
            {
                ollamaModel = VoiceAgentDefaults.DefaultVisionModel;
            }
            payload["ollama_model"] = ollamaModel;
            payload["conversation_pipeline_mode"] = NormalizeConversationPipelineModeForConfig(
                ReadEnvString(envObj, "VOICE_PIPELINE_MODE", "direct_unified"));
            payload["conversation_profile"] = NormalizeConversationProfileForConfig(
                ReadEnvString(envObj, "VOICE_CONVERSATION_PROFILE", "local"));
            payload["local_asr_mode"] = ResolvePreferredConversationAsrMode(
                "local",
                ReadEnvString(envObj, "VOICE_LOCAL_ASR_MODE", "moonshine-medium"),
                ReadEnvString(envObj, "VOICE_CLOUD_ASR_MODE", "api"));
            payload["cloud_asr_mode"] = ResolvePreferredConversationAsrMode(
                "cloud",
                ReadEnvString(envObj, "VOICE_LOCAL_ASR_MODE", "moonshine-medium"),
                ReadEnvString(envObj, "VOICE_CLOUD_ASR_MODE", "api"));
            payload["openai_response_model"] = ReadEnvString(envObj, "OPENAI_RESPONSE_MODEL", "gpt-4o-mini");
            payload["launch_triggers"] = string.Join(", ", launchTriggers);
            payload["exit_keywords"] = string.Join(", ", exitKeywords);
            payload["use_llm_intent_classifier"] = useLlmIntentClassifier;
            payload["use_moonshine_intent_recognizer"] = useMoonshineIntentRecognizer;
            payload["effective_game_manifest_path"] = ResolveGameManifestPath();
            await WriteRawJsonAsync(response, 200, payload.ToString()).ConfigureAwait(false);
        }

        private async Task HandleRuntimePrereqAsync(HttpListenerContext context)
        {
            var method = (context.Request.HttpMethod ?? string.Empty).Trim().ToUpperInvariant();
            if (method != "GET")
            {
                context.Response.StatusCode = 405;
                context.Response.Headers["Allow"] = "GET,OPTIONS";
                await WriteJsonAsync(context.Response, 405, "error", "method not allowed").ConfigureAwait(false);
                return;
            }

            var payload = new JSONObject();
            payload["status"] = "ok";

            var configuredModel = ResolveConfiguredOllamaModel();
            var ollamaBase = ResolveOllamaBaseUrl();
            var ollamaExe = ResolveOllamaExecutablePath();

            var piperExe = (Environment.GetEnvironmentVariable("PIPER_EXECUTABLE") ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(piperExe))
            {
                piperExe = ResolveBundledPiperExecutablePath();
            }
            piperExe = ResolveAbsolutePathCandidate(piperExe);

            var piperModel = (Environment.GetEnvironmentVariable("PIPER_MODEL_PATH") ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(piperModel))
            {
                piperModel = ResolveBundledPiperModelPath();
            }
            piperModel = ResolveAbsolutePathCandidate(piperModel);

            var piperConfig = ResolveAbsolutePathCandidate((Environment.GetEnvironmentVariable("PIPER_CONFIG_PATH") ?? string.Empty).Trim());
            var piperExeReady = !string.IsNullOrWhiteSpace(piperExe) && File.Exists(piperExe);
            var piperModelReady = !string.IsNullOrWhiteSpace(piperModel) && File.Exists(piperModel);
            var piperReady = piperExeReady && piperModelReady;

            var ollamaProbe = await ProbeOllamaAsync(ollamaBase, configuredModel).ConfigureAwait(false);

            payload["piper_ready"] = piperReady;
            payload["piper_executable_path"] = piperExe;
            payload["piper_model_path"] = piperModel;
            payload["piper_config_path"] = piperConfig;
            payload["piper_executable_exists"] = piperExeReady;
            payload["piper_model_exists"] = piperModelReady;
            payload["ollama_base_url"] = ollamaBase;
            payload["ollama_executable_path"] = ollamaExe;
            payload["ollama_installed"] = !string.IsNullOrWhiteSpace(ollamaExe);
            payload["ollama_running"] = ollamaProbe.Reachable;
            payload["ollama_model"] = configuredModel;
            payload["ollama_model_available"] = ollamaProbe.ModelAvailable;
            payload["ollama_error"] = ollamaProbe.Error;
            payload["ollama_download_url"] = "https://ollama.com/download/windows";
            payload["ollama_install_command"] = "winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements";
            payload["ollama_pull_command"] = "ollama pull " + configuredModel;
            payload["needs_piper_setup"] = !piperReady;
            payload["needs_ollama_setup"] = !ollamaProbe.Reachable || !ollamaProbe.ModelAvailable;

            await WriteRawJsonAsync(context.Response, 200, payload.ToString()).ConfigureAwait(false);
        }

        private async Task HandleRuntimeOllamaAsync(HttpListenerContext context)
        {
            var method = (context.Request.HttpMethod ?? string.Empty).Trim().ToUpperInvariant();
            if (method != "POST")
            {
                context.Response.StatusCode = 405;
                context.Response.Headers["Allow"] = "POST,OPTIONS";
                await WriteJsonAsync(context.Response, 405, "error", "method not allowed").ConfigureAwait(false);
                return;
            }

            var request = ParseJsonBody<RuntimeActionRequest>(context.Request);
            var action = (request.action ?? string.Empty).Trim().ToLowerInvariant();
            var model = NormalizeOllamaModelName(request.model);
            if (string.IsNullOrWhiteSpace(model))
            {
                model = ResolveConfiguredOllamaModel();
            }

            string error;
            switch (action)
            {
                case "open_download":
                    if (!TryOpenUrl("https://ollama.com/download/windows", out error))
                    {
                        await WriteJsonAsync(context.Response, 500, "error", $"failed to open browser: {error}").ConfigureAwait(false);
                        return;
                    }
                    await WriteJsonAsync(context.Response, 200, "ok", "opened Ollama download page").ConfigureAwait(false);
                    return;

                case "install":
                    if (!TryStartPowerShellDetached(
                        "winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements",
                        elevate: true,
                        hidden: false,
                        out error))
                    {
                        await WriteJsonAsync(context.Response, 500, "error", $"failed to start Ollama install: {error}").ConfigureAwait(false);
                        return;
                    }
                    await WriteJsonAsync(
                        context.Response,
                        200,
                        "ok",
                        "started Ollama install in elevated PowerShell. Approve UAC prompt and wait for completion.")
                        .ConfigureAwait(false);
                    return;

                case "pull_model":
                    if (string.IsNullOrWhiteSpace(model))
                    {
                        await WriteJsonAsync(context.Response, 400, "error", "invalid model name").ConfigureAwait(false);
                        return;
                    }

                    var escapedModel = model.Replace("'", "''");
                    if (!TryStartPowerShellDetached(
                        "ollama pull '" + escapedModel + "'",
                        elevate: false,
                        hidden: false,
                        out error))
                    {
                        await WriteJsonAsync(context.Response, 500, "error", $"failed to start model pull: {error}").ConfigureAwait(false);
                        return;
                    }
                    await WriteJsonAsync(
                        context.Response,
                        200,
                        "ok",
                        $"started model pull: {model}. wait until the PowerShell window finishes.")
                        .ConfigureAwait(false);
                    return;

                default:
                    await WriteJsonAsync(context.Response, 400, "error", "unknown action").ConfigureAwait(false);
                    return;
            }
        }

        private async Task<(bool Reachable, bool ModelAvailable, string Error)> ProbeOllamaAsync(string baseUrl, string requiredModel)
        {
            var model = NormalizeOllamaModelName(requiredModel);
            if (string.IsNullOrWhiteSpace(model))
            {
                model = VoiceAgentDefaults.DefaultVisionModel;
            }

            var url = (baseUrl ?? string.Empty).Trim().TrimEnd('/');
            if (string.IsNullOrWhiteSpace(url))
            {
                url = VoiceAgentDefaults.OllamaBaseUrl;
            }
            url += "/api/tags";

            try
            {
                using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(2.5)))
                {
                    var response = await SharedHttpClient.GetAsync(url, cts.Token).ConfigureAwait(false);
                    var body = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                    if (!response.IsSuccessStatusCode)
                    {
                        return (false, false, $"http {(int)response.StatusCode}: {body}");
                    }

                    var parsed = JSONNode.Parse(body);
                    var models = parsed?["models"];
                    if (models == null || !models.IsArray)
                    {
                        return (true, false, "ollama /api/tags returned no models list");
                    }

                    var available = models.AsArray;
                    for (int i = 0; i < available.Count; i++)
                    {
                        var node = available[i];
                        var name = (node?["name"]?.Value ?? string.Empty).Trim();
                        if (OllamaModelNamesMatch(model, name))
                        {
                            return (true, true, string.Empty);
                        }
                    }

                    return (true, false, $"model not found: {model}");
                }
            }
            catch (TaskCanceledException)
            {
                return (false, false, "request timeout");
            }
            catch (Exception ex)
            {
                return (false, false, ex.Message);
            }
        }

        private string ResolveConfiguredOllamaModel()
        {
            var configPath = ResolveLauncherConfigPath();
            var defaultConfigPath = ResolveLauncherDefaultConfigPath();
            var load = LoadMergedLauncherConfigRoot(configPath, defaultConfigPath);
            if (load.Success && load.Root != null)
            {
                var envNode = load.Root["env"];
                if (envNode != null && envNode.IsObject)
                {
                    var model = (envNode["OLLAMA_MODEL"]?.Value ?? string.Empty).Trim();
                    if (!string.IsNullOrWhiteSpace(model))
                    {
                        return model;
                    }
                }
            }

            var fromEnv = (Environment.GetEnvironmentVariable("OLLAMA_MODEL") ?? string.Empty).Trim();
            if (!string.IsNullOrWhiteSpace(fromEnv))
            {
                return fromEnv;
            }

            return VoiceAgentDefaults.DefaultVisionModel;
        }

        private static string NormalizeOllamaModelName(string raw)
        {
            var text = (raw ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(text))
            {
                return string.Empty;
            }

            for (int i = 0; i < text.Length; i++)
            {
                var c = text[i];
                var ok = (c >= 'a' && c <= 'z')
                    || (c >= 'A' && c <= 'Z')
                    || (c >= '0' && c <= '9')
                    || c == '-' || c == '_' || c == '.' || c == ':' || c == '/';
                if (!ok)
                {
                    return string.Empty;
                }
            }
            return text;
        }

        private static bool OllamaModelNamesMatch(string expected, string candidate)
        {
            var exp = (expected ?? string.Empty).Trim();
            var got = (candidate ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(exp) || string.IsNullOrWhiteSpace(got))
            {
                return false;
            }
            if (string.Equals(exp, got, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }

            var expBase = exp.Split(':')[0].Trim();
            var gotBase = got.Split(':')[0].Trim();
            return !string.IsNullOrWhiteSpace(expBase)
                && !string.IsNullOrWhiteSpace(gotBase)
                && string.Equals(expBase, gotBase, StringComparison.OrdinalIgnoreCase);
        }

        private static string ResolveOllamaExecutablePath()
        {
            var envExe = ResolveAbsolutePathCandidate(Environment.GetEnvironmentVariable("OLLAMA_EXE"));
            if (!string.IsNullOrWhiteSpace(envExe) && File.Exists(envExe))
            {
                return envExe;
            }

            var fromPath = ResolveExecutableFromPath("ollama.exe");
            if (!string.IsNullOrWhiteSpace(fromPath))
            {
                return fromPath;
            }

            var candidates = new[]
            {
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Ollama", "ollama.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Ollama", "ollama.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), "Ollama", "ollama.exe"),
            };
            for (int i = 0; i < candidates.Length; i++)
            {
                var candidate = ResolveAbsolutePathCandidate(candidates[i]);
                if (!string.IsNullOrWhiteSpace(candidate) && File.Exists(candidate))
                {
                    return candidate;
                }
            }

            return string.Empty;
        }

        private static string ResolveExecutableFromPath(string executableName)
        {
            var exe = (executableName ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(exe))
            {
                return string.Empty;
            }

            var pathEnv = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
            var parts = pathEnv.Split(Path.PathSeparator);
            for (int i = 0; i < parts.Length; i++)
            {
                var dir = (parts[i] ?? string.Empty).Trim().Trim('"');
                if (string.IsNullOrWhiteSpace(dir))
                {
                    continue;
                }
                try
                {
                    var candidate = Path.Combine(dir, exe);
                    if (File.Exists(candidate))
                    {
                        return Path.GetFullPath(candidate);
                    }
                }
                catch
                {
                }
            }

            return string.Empty;
        }

        private static bool TryOpenUrl(string url, out string error)
        {
            error = string.Empty;
            try
            {
                var startInfo = new ProcessStartInfo
                {
                    FileName = url,
                    UseShellExecute = true,
                };
                Process.Start(startInfo);
                return true;
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }
        }

        private static bool TryStartPowerShellDetached(string script, bool elevate, bool hidden, out string error)
        {
            error = string.Empty;
            var source = (script ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(source))
            {
                error = "empty script";
                return false;
            }

            try
            {
                var encoded = Convert.ToBase64String(Encoding.Unicode.GetBytes(source));
                var args = "-NoProfile -ExecutionPolicy Bypass -EncodedCommand " + encoded;
                var startInfo = new ProcessStartInfo
                {
                    FileName = "powershell.exe",
                    Arguments = args,
                    UseShellExecute = elevate || !hidden,
                    WindowStyle = hidden ? ProcessWindowStyle.Hidden : ProcessWindowStyle.Normal,
                    CreateNoWindow = hidden && !elevate
                };
                if (elevate)
                {
                    startInfo.Verb = "runas";
                }

                Process.Start(startInfo);
                return true;
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }
        }

        private static string ResolveBundledPiperExecutablePath()
        {
            var root = ResolveProjectRootPath();
            var candidate = ResolveAbsolutePathCandidate(Path.Combine(root, "runtime", "piper", "piper.exe"));
            if (!string.IsNullOrWhiteSpace(candidate) && File.Exists(candidate))
            {
                return candidate;
            }
            return string.Empty;
        }

        private static string ResolveBundledPiperModelPath()
        {
            var root = ResolveProjectRootPath();
            var modelsDir = ResolveAbsolutePathCandidate(Path.Combine(root, "runtime", "piper", "models"));
            if (string.IsNullOrWhiteSpace(modelsDir) || !Directory.Exists(modelsDir))
            {
                return string.Empty;
            }

            var preferred = new[]
            {
                "en_US-lessac-medium.onnx",
                "en_US-amy-medium.onnx",
                "en_US-ryan-high.onnx",
            };
            for (int i = 0; i < preferred.Length; i++)
            {
                var candidate = ResolveAbsolutePathCandidate(Path.Combine(modelsDir, preferred[i]));
                if (!string.IsNullOrWhiteSpace(candidate) && File.Exists(candidate))
                {
                    return candidate;
                }
            }

            try
            {
                var first = Directory.GetFiles(modelsDir, "*.onnx", SearchOption.AllDirectories).FirstOrDefault();
                return ResolveAbsolutePathCandidate(first);
            }
            catch
            {
                return string.Empty;
            }
        }

        private static bool TryReadOptionalString(JSONObject obj, string key, out string value)
        {
            value = string.Empty;
            if (obj == null || string.IsNullOrWhiteSpace(key) || !obj.HasKey(key))
            {
                return false;
            }

            value = (obj[key]?.Value ?? string.Empty).Trim();
            return true;
        }

        private static bool TryReadOptionalBool(JSONObject obj, string key, out bool value)
        {
            value = false;
            if (obj == null || string.IsNullOrWhiteSpace(key) || !obj.HasKey(key))
            {
                return false;
            }

            var raw = (obj[key]?.Value ?? string.Empty).Trim();
            return TryParseBool(raw, out value);
        }

        private static JSONObject EnsureObjectNode(JSONObject root, string key)
        {
            if (root != null)
            {
                var current = root[key];
                if (current != null && current.IsObject)
                {
                    return current.AsObject;
                }
            }

            var created = new JSONObject();
            if (root != null)
            {
                root[key] = created;
            }
            return created;
        }

        private static void SetOrRemoveString(JSONObject obj, string key, string value)
        {
            if (obj == null || string.IsNullOrWhiteSpace(key))
            {
                return;
            }

            if (string.IsNullOrWhiteSpace(value))
            {
                obj.Remove(key);
                return;
            }

            obj[key] = value.Trim();
        }

        private static void SetOrRemoveStringList(JSONObject obj, string key, List<string> values)
        {
            if (obj == null || string.IsNullOrWhiteSpace(key))
            {
                return;
            }

            if (values == null || values.Count == 0)
            {
                obj.Remove(key);
                return;
            }

            var arr = new JSONArray();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            for (int i = 0; i < values.Count; i++)
            {
                var text = (values[i] ?? string.Empty).Trim();
                if (string.IsNullOrWhiteSpace(text))
                {
                    continue;
                }
                if (seen.Add(text))
                {
                    arr.Add(text);
                }
            }

            if (arr.Count == 0)
            {
                obj.Remove(key);
                return;
            }

            obj[key] = arr;
        }

        private static List<string> ReadStringList(JSONObject obj, string key)
        {
            var values = new List<string>();
            if (obj == null || string.IsNullOrWhiteSpace(key))
            {
                return values;
            }

            var node = obj[key];
            if (node != null && node.IsArray)
            {
                var arr = node.AsArray;
                var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                for (int i = 0; i < arr.Count; i++)
                {
                    var text = (arr[i]?.Value ?? string.Empty).Trim();
                    if (!string.IsNullOrWhiteSpace(text) && seen.Add(text))
                    {
                        values.Add(text);
                    }
                }
            }
            return values;
        }

        private static bool ReadOptionalBool(JSONObject obj, string key, bool fallback)
        {
            if (obj == null || string.IsNullOrWhiteSpace(key) || !obj.HasKey(key))
            {
                return fallback;
            }

            var raw = (obj[key]?.Value ?? string.Empty).Trim();
            bool value;
            return TryParseBool(raw, out value) ? value : fallback;
        }

        private static List<string> ParsePhraseList(string text)
        {
            var values = new List<string>();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            if (string.IsNullOrWhiteSpace(text))
            {
                return values;
            }

            var merged = text
                .Replace("\r\n", "\n")
                .Replace('\uFF0C', ',')
                .Replace('\uFF1B', ';')
                .Replace(';', ',')
                .Replace('\n', ',');
            var parts = merged.Split(',');
            for (int i = 0; i < parts.Length; i++)
            {
                var value = (parts[i] ?? string.Empty).Trim();
                if (!string.IsNullOrWhiteSpace(value) && seen.Add(value))
                {
                    values.Add(value);
                }
            }
            return values;
        }

        private static List<string> GetDefaultLaunchTriggers()
        {
            return new List<string>
            {
                "open",
                "start",
                "launch",
                "play",
                "begin",
                "load"
            };
        }

        private static List<string> GetDefaultExitKeywords()
        {
            return new List<string>
            {
                "back home",
                "go home",
                "return home",
                "back",
                "quit",
                "exit",
                "stop",
                "cancel",
                "close",
                "close game"
            };
        }

        private static string ResolveProjectRootPath()
        {
            try
            {
                var appRoot = Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
                var appScripts = Path.Combine(appRoot, "scripts");
                if (Directory.Exists(appScripts))
                {
                    return appRoot;
                }

                // Installed layout can be <install>\app\<Unity build> with scripts at <install>\scripts.
                var installRoot = Path.GetFullPath(Path.Combine(appRoot, ".."));
                var installScripts = Path.Combine(installRoot, "scripts");
                if (Directory.Exists(installScripts))
                {
                    return installRoot;
                }

                return appRoot;
            }
            catch
            {
                return Directory.GetCurrentDirectory();
            }
        }

        private static string ResolveUserStateDirectoryPath()
        {
            var fromEnv = ResolveAbsolutePathCandidate(Environment.GetEnvironmentVariable("VOICE_AGENT_STATE_DIR"));
            if (!string.IsNullOrWhiteSpace(fromEnv))
            {
                return fromEnv;
            }

            try
            {
                var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                if (!string.IsNullOrWhiteSpace(localAppData))
                {
                    return Path.GetFullPath(Path.Combine(localAppData, "VoiceAgent"));
                }
            }
            catch
            {
            }

            return Path.GetFullPath(Path.Combine(ResolveProjectRootPath(), "state"));
        }

        private static string ResolveUserLauncherConfigPathDefault()
        {
            return Path.GetFullPath(Path.Combine(ResolveUserStateDirectoryPath(), "local_services.user.json"));
        }

        private static string ResolveUserManifestPathDefault()
        {
            return Path.GetFullPath(Path.Combine(ResolveUserStateDirectoryPath(), "manifest.json"));
        }

        private static string EnsureUserManifestPathDefault()
        {
            var userManifestPath = ResolveUserManifestPathDefault();
            try
            {
                var parent = Path.GetDirectoryName(userManifestPath);
                if (!string.IsNullOrWhiteSpace(parent))
                {
                    Directory.CreateDirectory(parent);
                }

                if (!File.Exists(userManifestPath))
                {
                    var installedManifest = Path.Combine(ResolveProjectRootPath(), "scripts", "intent_service", "manifest.json");
                    if (File.Exists(installedManifest))
                    {
                        File.Copy(installedManifest, userManifestPath, false);
                    }
                    else
                    {
                        File.WriteAllText(userManifestPath, "{\"games\":[]}", Encoding.UTF8);
                    }
                }
            }
            catch
            {
                // Keep returning the target path even if seeding fails.
            }

            return userManifestPath;
        }

        private static bool IsPathWithinRoot(string candidatePath, string rootPath)
        {
            if (string.IsNullOrWhiteSpace(candidatePath) || string.IsNullOrWhiteSpace(rootPath))
            {
                return false;
            }

            try
            {
                var fullPath = Path.GetFullPath(candidatePath).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                var fullRoot = Path.GetFullPath(rootPath).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                if (string.Equals(fullPath, fullRoot, StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }

                return fullPath.StartsWith(fullRoot + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)
                    || fullPath.StartsWith(fullRoot + Path.AltDirectorySeparatorChar, StringComparison.OrdinalIgnoreCase);
            }
            catch
            {
                return false;
            }
        }

        private static bool ShouldPreferUserWritableManifestPath(string manifestPath)
        {
            if (Application.isEditor || string.IsNullOrWhiteSpace(manifestPath))
            {
                return false;
            }

            var installScriptsDir = Path.Combine(ResolveProjectRootPath(), "scripts");
            if (!Directory.Exists(installScriptsDir))
            {
                return false;
            }

            return IsPathWithinRoot(manifestPath, installScriptsDir);
        }

        private static string ResolveExistingFilePathCandidate(string raw, string baseDir = null)
        {
            var candidate = ResolveAbsolutePathCandidate(raw, baseDir);
            if (string.IsNullOrWhiteSpace(candidate))
            {
                return string.Empty;
            }

            try
            {
                if (File.Exists(candidate))
                {
                    return candidate;
                }

                // Legacy buggy value: <install>\app\scripts\... should be <install>\scripts\...
                var normalized = candidate.Replace('/', '\\');
                var marker = "\\app\\scripts\\";
                var idx = normalized.IndexOf(marker, StringComparison.OrdinalIgnoreCase);
                if (idx >= 0)
                {
                    var repaired = normalized.Remove(idx, "\\app".Length);
                    repaired = Path.GetFullPath(repaired);
                    if (File.Exists(repaired))
                    {
                        return repaired;
                    }
                }
            }
            catch
            {
            }

            return string.Empty;
        }

        private static bool LooksLikePathValue(string raw)
        {
            if (string.IsNullOrWhiteSpace(raw))
            {
                return false;
            }

            var text = raw.Trim();
            if (text.StartsWith(".", StringComparison.Ordinal))
            {
                return true;
            }
            return text.IndexOf('\\') >= 0
                || text.IndexOf('/') >= 0
                || text.IndexOf(':') >= 0;
        }

        private static string NormalizePathOrCommandForConfig(string raw, string baseDir, bool allowCommandName)
        {
            if (string.IsNullOrWhiteSpace(raw))
            {
                return string.Empty;
            }

            var trimmed = raw.Trim();
            if (allowCommandName && !LooksLikePathValue(trimmed))
            {
                return trimmed;
            }

            var expanded = Environment.ExpandEnvironmentVariables(trimmed);
            if (expanded.IndexOf('%') >= 0 && trimmed.IndexOf('%') >= 0)
            {
                // Keep unresolved %VAR% literals as-is.
                return trimmed;
            }

            try
            {
                if (Path.IsPathRooted(expanded))
                {
                    return Path.GetFullPath(expanded);
                }
                var root = string.IsNullOrWhiteSpace(baseDir) ? ResolveProjectRootPath() : baseDir;
                return Path.GetFullPath(Path.Combine(root, expanded));
            }
            catch
            {
                return trimmed;
            }
        }

        private static bool IsSinglePercentPlaceholder(string raw)
        {
            if (string.IsNullOrWhiteSpace(raw))
            {
                return false;
            }

            var text = raw.Trim();
            if (!(text.StartsWith("%", StringComparison.Ordinal) && text.EndsWith("%", StringComparison.Ordinal)))
            {
                return false;
            }
            if (text.Length < 3)
            {
                return false;
            }

            return text.IndexOf('%', 1) == text.Length - 1;
        }

        private Dictionary<string, string> LoadLauncherEnvOverrides()
        {
            var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            var configPath = ResolveLauncherConfigPath();
            var defaultConfigPath = ResolveLauncherDefaultConfigPath();
            var load = LoadMergedLauncherConfigRoot(configPath, defaultConfigPath);
            if (!load.Success || load.Root == null)
            {
                return result;
            }

            var envNode = load.Root["env"];
            if (envNode == null || !envNode.IsObject)
            {
                return result;
            }

            var envObj = envNode.AsObject;
            foreach (var pair in envObj.Linq)
            {
                var key = (pair.Key ?? string.Empty).Trim();
                var value = (pair.Value?.Value ?? string.Empty).Trim();
                if (!string.IsNullOrWhiteSpace(key) && !string.IsNullOrWhiteSpace(value))
                {
                    result[key] = value;
                }
            }
            return result;
        }

        private string ResolvePathFromConfigOrPlaceholder(
            string raw,
            string baseDir,
            Dictionary<string, string> launcherEnv,
            bool allowCommandName)
        {
            var text = (raw ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(text))
            {
                return string.Empty;
            }

            if (!IsSinglePercentPlaceholder(text))
            {
                return NormalizePathOrCommandForConfig(text, baseDir, allowCommandName);
            }

            var key = text.Substring(1, text.Length - 2).Trim();
            if (string.IsNullOrWhiteSpace(key))
            {
                return string.Empty;
            }

            string resolved;
            if (launcherEnv != null && launcherEnv.TryGetValue(key, out resolved) && !string.IsNullOrWhiteSpace(resolved))
            {
                return NormalizePathOrCommandForConfig(resolved, baseDir, allowCommandName);
            }

            resolved = Environment.GetEnvironmentVariable(key);
            if (!string.IsNullOrWhiteSpace(resolved))
            {
                return NormalizePathOrCommandForConfig(resolved, baseDir, allowCommandName);
            }

            var expanded = Environment.ExpandEnvironmentVariables(text);
            if (!string.Equals(expanded, text, StringComparison.Ordinal) && !string.IsNullOrWhiteSpace(expanded))
            {
                return NormalizePathOrCommandForConfig(expanded, baseDir, allowCommandName);
            }

            // Unresolved placeholder -> require explicit absolute path in UI.
            return string.Empty;
        }

        private string ResolveLauncherConfigPath()
        {
            var fromEnv = ResolveAbsolutePathCandidate(Environment.GetEnvironmentVariable("VOICE_AGENT_LAUNCHER_CONFIG"));
            if (!string.IsNullOrWhiteSpace(fromEnv))
            {
                return fromEnv;
            }

            if (!Application.isEditor)
            {
                return ResolveUserLauncherConfigPathDefault();
            }

            var cwdDefault = ResolveAbsolutePathCandidate(Path.Combine("scripts", "local_services.user.json"));
            if (!string.IsNullOrWhiteSpace(cwdDefault))
            {
                return cwdDefault;
            }

            return Path.GetFullPath(Path.Combine(Application.dataPath, "..", "scripts", "local_services.user.json"));
        }

        private string ResolveLauncherDefaultConfigPath()
        {
            var fromEnv = ResolveAbsolutePathCandidate(Environment.GetEnvironmentVariable("VOICE_AGENT_DEFAULT_CONFIG"));
            if (!string.IsNullOrWhiteSpace(fromEnv))
            {
                return fromEnv;
            }

            var cwdDefault = ResolveAbsolutePathCandidate(Path.Combine("scripts", "local_services.default.json"));
            if (!string.IsNullOrWhiteSpace(cwdDefault))
            {
                return cwdDefault;
            }

            return Path.GetFullPath(Path.Combine(Application.dataPath, "..", "scripts", "local_services.default.json"));
        }

        private static string ResolveAbsolutePathCandidate(string raw, string baseDir = null)
        {
            if (string.IsNullOrWhiteSpace(raw))
            {
                return string.Empty;
            }

            try
            {
                var expanded = Environment.ExpandEnvironmentVariables(raw.Trim());
                if (Path.IsPathRooted(expanded))
                {
                    return Path.GetFullPath(expanded);
                }
                var root = string.IsNullOrWhiteSpace(baseDir) ? ResolveProjectRootPath() : baseDir;
                return Path.GetFullPath(Path.Combine(root, expanded));
            }
            catch
            {
                return string.Empty;
            }
        }

        private static (bool Success, JSONObject Root, string Error) LoadLauncherConfigRoot(string path)
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

                if (rootObj["python"] == null || !rootObj["python"].IsObject)
                {
                    rootObj["python"] = new JSONObject();
                }
                if (rootObj["paths"] == null || !rootObj["paths"].IsObject)
                {
                    rootObj["paths"] = new JSONObject();
                }
                if (rootObj["openai"] == null || !rootObj["openai"].IsObject)
                {
                    rootObj["openai"] = new JSONObject();
                }
                if (rootObj["intent"] == null || !rootObj["intent"].IsObject)
                {
                    rootObj["intent"] = new JSONObject();
                }
                if (rootObj["env"] == null || !rootObj["env"].IsObject)
                {
                    rootObj["env"] = new JSONObject();
                }

                return (true, rootObj, string.Empty);
            }
            catch (Exception ex)
            {
                return (false, null, $"failed to load launcher config: {ex.Message}");
            }
        }

        private static JSONNode CloneJsonNode(JSONNode node)
        {
            if (node == null)
            {
                return null;
            }

            try
            {
                return JSONNode.Parse(node.ToString());
            }
            catch
            {
                return node;
            }
        }

        private static void MergeJsonObjectInto(JSONObject target, JSONObject source)
        {
            if (target == null || source == null)
            {
                return;
            }

            foreach (var pair in source.Linq)
            {
                var key = pair.Key;
                var value = pair.Value;
                if (string.IsNullOrWhiteSpace(key) || value == null)
                {
                    continue;
                }

                if (value.IsObject)
                {
                    var current = target[key];
                    JSONObject targetChild;
                    if (current != null && current.IsObject)
                    {
                        targetChild = current.AsObject;
                    }
                    else
                    {
                        targetChild = new JSONObject();
                        target[key] = targetChild;
                    }

                    MergeJsonObjectInto(targetChild, value.AsObject);
                    continue;
                }

                target[key] = CloneJsonNode(value);
            }
        }

        private static (bool Success, JSONObject Root, string Error) LoadMergedLauncherConfigRoot(string userPath, string defaultPath)
        {
            var baseLoad = LoadLauncherConfigRoot(defaultPath);
            if (!baseLoad.Success)
            {
                return baseLoad;
            }

            var userLoad = LoadLauncherConfigRoot(userPath);
            if (!userLoad.Success)
            {
                return userLoad;
            }

            var merged = new JSONObject();
            MergeJsonObjectInto(merged, baseLoad.Root);
            MergeJsonObjectInto(merged, userLoad.Root);

            if (merged["python"] == null || !merged["python"].IsObject)
            {
                merged["python"] = new JSONObject();
            }
            if (merged["paths"] == null || !merged["paths"].IsObject)
            {
                merged["paths"] = new JSONObject();
            }
            if (merged["openai"] == null || !merged["openai"].IsObject)
            {
                merged["openai"] = new JSONObject();
            }
            if (merged["intent"] == null || !merged["intent"].IsObject)
            {
                merged["intent"] = new JSONObject();
            }
            if (merged["env"] == null || !merged["env"].IsObject)
            {
                merged["env"] = new JSONObject();
            }

            return (true, merged, string.Empty);
        }

        private string ResolveGameManifestPath()
        {
            var userManifestPath = string.Empty;
            var configPath = ResolveLauncherConfigPath();
            var defaultConfigPath = ResolveLauncherDefaultConfigPath();
            var config = LoadMergedLauncherConfigRoot(configPath, defaultConfigPath);
            if (config.Success && config.Root != null)
            {
                var pathsNode = config.Root["paths"];
                if (pathsNode != null && pathsNode.IsObject)
                {
                    var paths = pathsNode.AsObject;
                    var gameFromConfig = ResolveExistingFilePathCandidate((paths["game_manifest"]?.Value ?? string.Empty).Trim());
                    if (!string.IsNullOrWhiteSpace(gameFromConfig))
                    {
                        if (ShouldPreferUserWritableManifestPath(gameFromConfig))
                        {
                            if (string.IsNullOrWhiteSpace(userManifestPath))
                            {
                                userManifestPath = EnsureUserManifestPathDefault();
                            }
                            return userManifestPath;
                        }
                        return gameFromConfig;
                    }
                    var intentFromConfig = ResolveExistingFilePathCandidate((paths["intent_manifest"]?.Value ?? string.Empty).Trim());
                    if (!string.IsNullOrWhiteSpace(intentFromConfig))
                    {
                        if (ShouldPreferUserWritableManifestPath(intentFromConfig))
                        {
                            if (string.IsNullOrWhiteSpace(userManifestPath))
                            {
                                userManifestPath = EnsureUserManifestPathDefault();
                            }
                            return userManifestPath;
                        }
                        return intentFromConfig;
                    }
                }
            }

            var primary = ResolveExistingFilePathCandidate(Environment.GetEnvironmentVariable("GAME_LAUNCHER_MANIFEST_PATH"));
            if (!string.IsNullOrWhiteSpace(primary))
            {
                if (ShouldPreferUserWritableManifestPath(primary))
                {
                    if (string.IsNullOrWhiteSpace(userManifestPath))
                    {
                        userManifestPath = EnsureUserManifestPathDefault();
                    }
                    return userManifestPath;
                }
                return primary;
            }

            var secondary = ResolveExistingFilePathCandidate(Environment.GetEnvironmentVariable("INTENT_MANIFEST_PATH"));
            if (!string.IsNullOrWhiteSpace(secondary))
            {
                if (ShouldPreferUserWritableManifestPath(secondary))
                {
                    if (string.IsNullOrWhiteSpace(userManifestPath))
                    {
                        userManifestPath = EnsureUserManifestPathDefault();
                    }
                    return userManifestPath;
                }
                return secondary;
            }

            if (!Application.isEditor)
            {
                if (string.IsNullOrWhiteSpace(userManifestPath))
                {
                    userManifestPath = EnsureUserManifestPathDefault();
                }
                return userManifestPath;
            }

            var cwdDefault = ResolveExistingFilePathCandidate(Path.Combine("scripts", "intent_service", "manifest.json"));
            if (!string.IsNullOrWhiteSpace(cwdDefault))
            {
                return cwdDefault;
            }

            return Path.GetFullPath(Path.Combine(ResolveProjectRootPath(), "scripts", "intent_service", "manifest.json"));
        }

        private static (bool Success, JSONObject Root, string Error) LoadGameManifestRoot(string path)
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

                if (rootObj["games"] == null || !rootObj["games"].IsArray)
                {
                    rootObj["games"] = new JSONArray();
                }

                return (true, rootObj, string.Empty);
            }
            catch (Exception ex)
            {
                return (false, null, $"failed to load manifest: {ex.Message}");
            }
        }

    }
}
