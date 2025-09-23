This folder is reserved for the official MQTTnet library. The project already contains a
first-party MQTT client capable of connecting, publishing intents (QoS 0/1) and handling
keep-alive traffic, so you do not need any external DLLs to enable the MQTT intent publisher.

If you prefer to use the full MQTTnet feature set (managed clients, subscriptions, TLS, etc.)
download the Unity-compatible assemblies from the official project
(https://github.com/dotnet/MQTTnet) and place them in this folder. Unity will import them on the
next refresh. Recommended assemblies from the release package:
- MQTTnet.dll
- MQTTnet.Extensions.ManagedClient.dll (optional)

After adding the DLLs, review the plugin import settings for each target platform to ensure they
match your build requirements before swapping the publisher over to MQTTnet.
