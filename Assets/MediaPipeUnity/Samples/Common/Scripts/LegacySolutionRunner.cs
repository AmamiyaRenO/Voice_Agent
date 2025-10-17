// Copyright (c) 2023 homuler
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
  public abstract class LegacySolutionRunner<TGraphRunner> : BaseRunner where TGraphRunner : GraphRunner
  {
    [SerializeField] protected Screen screen;
    [SerializeField] protected TGraphRunner graphRunner;

    private Coroutine _coroutine;

    public RunningMode runningMode;

    private bool _webCamReleasedForBackground;
    private Coroutine _webCamResumeCoroutine;

    public long timeoutMillisec
    {
      get => graphRunner.timeoutMillisec;
      set => graphRunner.timeoutMillisec = value;
    }

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
      graphRunner.Stop();
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
      annotationController.isMirrored = expectedToBeMirrored ^ imageSource.isHorizontallyFlipped ^ imageSource.isFrontFacing;
      annotationController.rotationAngle = imageSource.rotation.Reverse();
    }
  }
}
