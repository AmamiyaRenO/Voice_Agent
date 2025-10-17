using System.Collections;
using UnityEngine;
using UnityEngine.UI;

namespace Mediapipe.Unity.Tutorial
{
  public class FaceLandmarkerRunner : MonoBehaviour
  {
    [SerializeField] private RawImage screen;
    [SerializeField] private int width;
    [SerializeField] private int height;
    [SerializeField] private int fps;

    private WebCamTexture webCamTexture;
    private bool _resumeRequested;
    private Coroutine _resumeCoroutine;

    private IEnumerator Start()
    {
      if (WebCamTexture.devices.Length == 0)
      {
        throw new System.Exception("Web Camera devices are not found");
      }
      var webCamDevice = WebCamTexture.devices[0];
      webCamTexture = new WebCamTexture(webCamDevice.name, width, height, fps);
      webCamTexture.Play();

      // NOTE: On macOS, the contents of webCamTexture may not be readable immediately, so wait until it is readable
      yield return new WaitUntil(() => webCamTexture.width > 16);

      screen.rectTransform.sizeDelta = new Vector2(width, height);
      screen.texture = webCamTexture;
    }

    private void OnDestroy()
    {
      if (_resumeCoroutine != null)
      {
        StopCoroutine(_resumeCoroutine);
        _resumeCoroutine = null;
      }

      if (webCamTexture != null)
      {
        webCamTexture.Stop();
      }
    }

    private void OnApplicationFocus(bool hasFocus)
    {
      HandleApplicationVisibilityChange(hasFocus);
    }

    private void OnApplicationPause(bool pauseStatus)
    {
      HandleApplicationVisibilityChange(!pauseStatus);
    }

    private void HandleApplicationVisibilityChange(bool isVisible)
    {
      if (webCamTexture == null)
      {
        return;
      }

      if (!isVisible)
      {
        if (webCamTexture.isPlaying)
        {
          _resumeRequested = true;
          webCamTexture.Stop();
        }
        return;
      }

      if (!_resumeRequested)
      {
        return;
      }

      if (_resumeCoroutine != null)
      {
        StopCoroutine(_resumeCoroutine);
      }
      _resumeCoroutine = StartCoroutine(ResumeAfterFocus());
    }

    private IEnumerator ResumeAfterFocus()
    {
      webCamTexture.Play();
      yield return new WaitUntil(() => webCamTexture.width > 16);
      _resumeRequested = false;
      _resumeCoroutine = null;
    }
  }
}
