using System;
using System.Collections.Generic;
using System.Net.Http;
using System.Text;
using System.Threading.Tasks;
using UnityEngine;

namespace RobotVoice
{
    public sealed partial class UserTestControlPanel
    {
        private static string NormalizeConversationPipelineModeForConfig(string value)
        {
            var normalized = (value ?? string.Empty).Trim().ToLowerInvariant();
            switch (normalized)
            {
                case "legacy":
                case "mqtt":
                case "legacy_mqtt":
                    return "legacy_mqtt";
                default:
                    return "direct_unified";
            }
        }

        private static string NormalizeConversationProfileForConfig(string value)
        {
            var normalized = (value ?? string.Empty).Trim().ToLowerInvariant();
            switch (normalized)
            {
                case "cloud":
                case "openai":
                case "online":
                    return "cloud";
                default:
                    return "local";
            }
        }

        private static string ReadEnvString(JSONObject envObj, string key, string fallback)
        {
            if (envObj != null)
            {
                var value = (envObj[key]?.Value ?? string.Empty).Trim();
                if (!string.IsNullOrWhiteSpace(value))
                {
                    return value;
                }
            }
            return fallback;
        }

        private static string ResolvePreferredConversationAsrMode(string profile, string localAsrMode, string cloudAsrMode)
        {
            var normalizedProfile = NormalizeConversationProfileForConfig(profile);
            var preferred = normalizedProfile == "cloud" ? cloudAsrMode : localAsrMode;
            var normalized = NormalizeAsrMode(preferred);
            if (!string.IsNullOrWhiteSpace(normalized))
            {
                return normalized;
            }
            return normalizedProfile == "cloud" ? "api" : "moonshine-medium";
        }

        private void ApplySavedConversationRuntimeConfig()
        {
            if (voiceLauncher == null)
            {
                return;
            }

            try
            {
                var configPath = ResolveLauncherConfigPath();
                var defaultConfigPath = ResolveLauncherDefaultConfigPath();
                var load = LoadMergedLauncherConfigRoot(configPath, defaultConfigPath);
                if (!load.Success)
                {
                    return;
                }

                var envObj = EnsureObjectNode(load.Root, "env");
                var pipelineMode = NormalizeConversationPipelineModeForConfig(
                    ReadEnvString(envObj, "VOICE_PIPELINE_MODE", "direct_unified"));
                var profile = NormalizeConversationProfileForConfig(
                    ReadEnvString(envObj, "VOICE_CONVERSATION_PROFILE", "local"));
                voiceLauncher.ApplyConversationRuntimeConfigForTester(pipelineMode, profile);
            }
            catch (Exception ex)
            {
                Debug.LogWarning($"[UserTestPanel] Failed to apply saved conversation config: {ex.Message}");
            }
        }

        private async Task<string> ApplyConversationRuntimeChangesAsync(
            string pipelineMode,
            string profile,
            string localAsrMode,
            string cloudAsrMode,
            string openaiApiKey,
            string openaiBaseUrl,
            string openaiTranscribeModel,
            string openaiTranscribePrompt,
            string openaiResponseModel,
            string ollamaModel)
        {
            var normalizedPipeline = NormalizeConversationPipelineModeForConfig(pipelineMode);
            var normalizedProfile = NormalizeConversationProfileForConfig(profile);
            var normalizedLocalAsr = NormalizeAsrMode(localAsrMode);
            if (string.IsNullOrWhiteSpace(normalizedLocalAsr))
            {
                normalizedLocalAsr = "moonshine-medium";
            }
            var normalizedCloudAsr = NormalizeAsrMode(cloudAsrMode);
            if (string.IsNullOrWhiteSpace(normalizedCloudAsr))
            {
                normalizedCloudAsr = "api";
            }

            if (voiceLauncher != null)
            {
                await RunOnMainThreadAsync(() =>
                {
                    voiceLauncher.ApplyConversationRuntimeConfigForTester(normalizedPipeline, normalizedProfile);
                    return true;
                }).ConfigureAwait(false);
            }

            var notes = new List<string>();
            var conversationResult = await SetConversationServiceConfigAsync(
                normalizedPipeline,
                normalizedProfile,
                normalizedLocalAsr,
                normalizedCloudAsr,
                openaiApiKey,
                openaiBaseUrl,
                openaiTranscribeModel,
                openaiTranscribePrompt,
                openaiResponseModel,
                ollamaModel).ConfigureAwait(false);
            if (!conversationResult.Success)
            {
                notes.Add(conversationResult.Error);
            }

            var preferredAsr = ResolvePreferredConversationAsrMode(
                normalizedProfile,
                normalizedLocalAsr,
                normalizedCloudAsr);
            var asrResult = await SetAsrModeAsync(preferredAsr).ConfigureAwait(false);
            if (!asrResult.Success)
            {
                notes.Add(asrResult.Error);
            }

            if (notes.Count == 0)
            {
                return "saved and applied to runtime.";
            }

            return "saved; live apply partial: " + string.Join(" | ", notes);
        }

        private async Task<(bool Success, string Error)> SetConversationServiceConfigAsync(
            string pipelineMode,
            string profile,
            string localAsrMode,
            string cloudAsrMode,
            string openaiApiKey,
            string openaiBaseUrl,
            string openaiTranscribeModel,
            string openaiTranscribePrompt,
            string openaiResponseModel,
            string ollamaModel)
        {
            try
            {
                var apiKey = string.IsNullOrWhiteSpace(openaiApiKey) ? string.Empty : openaiApiKey.Trim();
                var baseUrl = string.IsNullOrWhiteSpace(openaiBaseUrl) ? string.Empty : openaiBaseUrl.Trim();
                var transcribeModel = string.IsNullOrWhiteSpace(openaiTranscribeModel) ? string.Empty : openaiTranscribeModel.Trim();
                var transcribePrompt = string.IsNullOrWhiteSpace(openaiTranscribePrompt) ? string.Empty : openaiTranscribePrompt.Trim();

                var payload = new StringBuilder(384)
                    .Append("{\"pipeline_mode\":\"").Append(EscapeJson(pipelineMode)).Append('"')
                    .Append(",\"profile\":\"").Append(EscapeJson(profile)).Append('"')
                    .Append(",\"local_asr_mode\":\"").Append(EscapeJson(localAsrMode)).Append('"')
                    .Append(",\"cloud_asr_mode\":\"").Append(EscapeJson(cloudAsrMode)).Append('"')
                    .Append(",\"cloud_response_provider\":\"openai\"")
                    .Append(",\"openai_api_key\":\"").Append(EscapeJson(apiKey)).Append('"')
                    .Append(",\"openai_base_url\":\"").Append(EscapeJson(baseUrl)).Append('"')
                    .Append(",\"openai_transcribe_model\":\"").Append(EscapeJson(transcribeModel)).Append('"')
                    .Append(",\"openai_transcribe_prompt\":\"").Append(EscapeJson(transcribePrompt)).Append('"');

                var openaiModel = string.IsNullOrWhiteSpace(openaiResponseModel) ? string.Empty : openaiResponseModel.Trim();
                if (!string.IsNullOrWhiteSpace(openaiModel))
                {
                    payload.Append(",\"openai_response_model\":\"")
                        .Append(EscapeJson(openaiModel))
                        .Append('"');
                }

                var localModel = string.IsNullOrWhiteSpace(ollamaModel) ? string.Empty : ollamaModel.Trim();
                if (!string.IsNullOrWhiteSpace(localModel))
                {
                    payload.Append(",\"local_response_model\":\"")
                        .Append(EscapeJson(localModel))
                        .Append('"');
                }

                payload.Append('}');
                using (var content = new StringContent(payload.ToString(), Encoding.UTF8, "application/json"))
                {
                    var response = await SharedHttpClient.PostAsync(
                        ResolveAsrServiceBaseUrl() + "/conversation/config",
                        content).ConfigureAwait(false);
                    var body = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                    if (!response.IsSuccessStatusCode)
                    {
                        return (false, string.IsNullOrWhiteSpace(body)
                            ? $"conversation config HTTP {(int)response.StatusCode}"
                            : body.Trim());
                    }
                }

                return (true, string.Empty);
            }
            catch (Exception ex)
            {
                return (false, $"failed to set conversation config: {ex.Message}");
            }
        }
    }
}
