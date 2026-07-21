using System;
using System.Reflection;
using NUnit.Framework;

namespace RobotVoice.Tests
{
    public sealed class RachelPanelAssetTests
    {
        private static MethodInfo ResolveMethod()
        {
            var panelType = Type.GetType("RobotVoice.UserTestControlPanel, Assembly-CSharp", throwOnError: true);
            return panelType.GetMethod("TryResolvePanelAssetPath", BindingFlags.NonPublic | BindingFlags.Static);
        }

        private static bool TryResolve(string name, out string path, out string contentType)
        {
            var arguments = new object[] { name, null, null };
            var resolved = (bool)ResolveMethod().Invoke(null, arguments);
            path = arguments[1] as string;
            contentType = arguments[2] as string;
            return resolved;
        }

        [TestCase("app.css", "text/css; charset=utf-8")]
        [TestCase("shell.js", "application/javascript; charset=utf-8")]
        [TestCase("theme.js", "application/javascript; charset=utf-8")]
        [TestCase("rachel-device.png", "image/png")]
        [TestCase("sdk-manifest.json", "application/json; charset=utf-8")]
        public void KnownPanelAssetResolvesWithExpectedContentType(string name, string expectedContentType)
        {
            Assert.That(TryResolve(name, out var path, out var contentType), Is.True);
            Assert.That(path, Does.EndWith(name).IgnoreCase);
            Assert.That(contentType, Is.EqualTo(expectedContentType));
        }

        [TestCase("../app.css")]
        [TestCase("..\\app.css")]
        [TestCase("nested/app.css")]
        [TestCase("missing.js")]
        [TestCase("payload.exe")]
        public void UnsafeOrUnknownPanelAssetIsRejected(string name)
        {
            Assert.That(TryResolve(name, out _, out _), Is.False);
        }
    }
}
