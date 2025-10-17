// Copyright (c) 2021 homuler
//
// Use of this source code is governed by an MIT-style
// license that can be found in the LICENSE file or at
// https://opensource.org/licenses/MIT.

using System;
using System.Collections;
using UnityEngine;

using Mediapipe.Unity;

namespace Mediapipe.Unity.Sample
{
  public abstract class VisionTaskApiRunner<TTask> : BaseRunner where TTask : Tasks.Vision.Core.BaseVisionTaskApi
  {
    [SerializeField] protected Screen screen;

    private Coroutine _coroutine;
    protected TTask taskApi;

    public RunningMode runningMode;

    private bool _webCamReleasedForBackground;
    private Coroutine _webCamResumeCoroutine;

    public override void Play()
    {
      if (_coroutine != null)
      {
        Stop();
      }
      base.Play();
      _coroutine = StartCoroutine(Run());
    }

    public override void Pause()
    {
      base.Pause();
      ImageSourceProvider.ImageSource.Pause();
    }

    public override void Resume()
    {
      base.Resume();
      var _ = StartCoroutine(ImageSourceProvider.ImageSource.Resume());
    }

    public override void Stop()
    {
      base.Stop();
      StopCoroutine(_coroutine);
      ImageSourceProvider.ImageSource.Stop();
      taskApi?.Close();
      taskApi = null;
    }

    private void OnDisable()
    {
      if (_webCamResumeCoroutine != null)
      {
        StopCoroutine(_webCamResumeCoroutine);
        _webCamResumeCoroutine = null;
      }
      _webCamReleasedForBackground = false;
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
      if (!isActiveAndEnabled)
      {
        return;
      }

      if (bootstrap == null || !bootstrap.isFinished)
      {
        return;
      }

      var imageSource = ImageSourceProvider.ImageSource;
      if (imageSource is not WebCamSource webCamSource)
      {
        return;
      }

      if (!isVisible)
      {
        if (!_webCamReleasedForBackground && webCamSource.isPrepared)
        {
          _webCamReleasedForBackground = true;
          isPaused = true;
          webCamSource.Stop();
        }
        return;
      }

      if (!_webCamReleasedForBackground)
      {
        return;
      }

      if (_webCamResumeCoroutine != null)
      {
        return;
      }

      _webCamResumeCoroutine = StartCoroutine(ResumeWebCamAfterFocus(webCamSource));
    }

    private IEnumerator ResumeWebCamAfterFocus(WebCamSource webCamSource)
    {
      const float retryDelaySeconds = 0.5f;
https://github.com/AmamiyaRenO/Voice_Agent/pull/52/conflict?name=Assets%252FMediaPipeUnity%252FSamples%252FCommon%252FScripts%252FVisionTaskApiRunner.cs&ancestor_oid=1bd08d8b256cb371189019091dea6f78d7b9b6ee&base_oid=ba1fc7ddf1a13d1632533e3f3fd64bf386d31a4c&head_oid=af51ee761c81a7042b792bbd493b91dfc8a06803
      while (_webCamReleasedForBackground && isActiveAndEnabled)
      {
        var resumed = false;

        try
        {
          yield return webCamSource.Play();
          resumed = true;
        }
        catch (Exception ex)
        {
          Debug.LogWarning($"[{TAG}] Failed to reacquire WebCam after focus change: {ex.Message}");
        }

        if (resumed)
        {
          _webCamReleasedForBackground = false;
          isPaused = false;
          break;
        }

        yield return new WaitForSeconds(retryDelaySeconds);
      }

      _webCamResumeCoroutine = null;
    }

    protected abstract IEnumerator Run();

    protected static void SetupAnnotationController<T>(AnnotationController<T> annotationController, ImageSource imageSource, bool expectedToBeMirrored = false) where T : HierarchicalAnnotation
    {
      annotationController.isMirrored = expectedToBeMirrored;
      annotationController.imageSize = new Vector2Int(imageSource.textureWidth, imageSource.textureHeight);
    }
  }
}
