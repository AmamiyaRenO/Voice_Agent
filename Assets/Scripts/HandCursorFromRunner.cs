using System.Collections;
using System.Collections.Generic;
using System.Reflection;
using UnityEngine;
using UnityEngine.UI;

public class HandCursorFromRunner : MonoBehaviour
{
	[Header("Refs")]
	public MonoBehaviour runner;
	public Canvas canvas;                   // Screen Space - Overlay recommended
	public RectTransform cursorRect;        // Cursor_Hand rect

	[Header("Which Wrist")]
	public bool useRightWrist = true;       // true = right (index 16), false = left (index 15)
	public bool mirrorX = true;             // selfie view feel

	[Header("Smoothing")]
	[Range(0.01f, 0.3f)] public float smoothTime = 0.08f;
	[Range(0f, 1f)] public float visibilityGate = 0.5f;
	[Range(0f, 1f)] public float lowConfLerp = 0.08f;
	public float maxSpeedPixelsPerSec = 2400f;

	const int LEFT_WRIST_INDEX = 15;
	const int RIGHT_WRIST_INDEX = 16;

	FieldInfo _landmarksField;
	PropertyInfo _latestResultProperty;
	FieldInfo _poseLandmarksField;
	bool _bound;
	bool _initialized;
	Vector2 _filtered;
	Vector2 _vel; // used by SmoothDamp

	// Simple moving-average stabilizer to reduce jitter
	[Range(1, 8)] public int historyWindow = 4;
	Vector2[] _history = new Vector2[8];
	int _histCount = 0;
	int _histIndex = 0;

	void Reset()
	{
		if (!canvas) canvas = FindObjectOfType<Canvas>();
		if (!runner) runner = FindPoseRunner();
		if (!cursorRect && canvas) cursorRect = canvas.transform.Find("Cursor_Hand") as RectTransform;
	}

	void Awake()
	{
		if (!runner) runner = FindPoseRunner();
		StartCoroutine(BindWhenReady());
	}

	IEnumerator BindWhenReady()
	{
		while (!_bound)
		{
			if (runner != null)
			{
				var poseLandmarks = TryGetPoseLandmarks(runner, out var firstList);
				if (poseLandmarks != null && poseLandmarks.Count > 0 && firstList != null)
				{
					_landmarksField = firstList.GetType()
						.GetField("landmarks", BindingFlags.Instance | BindingFlags.Public);

					if (_landmarksField != null)
					{
						_bound = true;
						yield break;
					}
				}
			}
			yield return null;
		}
	}

	void Update()
	{
		if (!_bound || runner == null || cursorRect == null || canvas == null) return;

		var poseLandmarks = TryGetPoseLandmarks(runner, out var firstList);
		if (poseLandmarks == null || poseLandmarks.Count == 0 || firstList == null) return;

		var raw = _landmarksField.GetValue(firstList) as System.Collections.IList;
		if (raw == null) return;

		int idx = useRightWrist ? RIGHT_WRIST_INDEX : LEFT_WRIST_INDEX;
		if (raw.Count <= idx) return;

		var lm = raw[idx];
		float x = GetField(lm, "x", 0.5f);
		float y = GetField(lm, "y", 0.5f);
		float v = GetField(lm, "visibility", 1f);

		if (mirrorX) x = 1f - x;

		// Convert normalized (0..1, MediaPipe top=0) to screen pixel space
		float px = x * Screen.width;
		float py = (1f - y) * Screen.height;

		Vector2 screen = new Vector2(px, py);

		// Map to canvas local space
		RectTransform canvasRect = canvas.transform as RectTransform;
		RectTransformUtility.ScreenPointToLocalPointInRectangle(canvasRect, screen, canvas.worldCamera, out var localTarget);

		// Moving-average smoothing on target point
		if (historyWindow > _history.Length) historyWindow = _history.Length;
		_history[_histIndex] = localTarget;
		_histIndex = (_histIndex + 1) % historyWindow;
		_histCount = Mathf.Min(_histCount + 1, historyWindow);
		Vector2 avgTarget = Vector2.zero;
		for (int i = 0; i < _histCount; i++) avgTarget += _history[i];
		avgTarget /= Mathf.Max(1, _histCount);

		// Seed
		if (!_initialized)
		{
			_filtered = avgTarget;
			cursorRect.anchoredPosition = _filtered;
			_initialized = true;
			return;
		}

		// Confidence-aware smoothing
		Vector2 next;
		if (v >= visibilityGate)
		{
			// SmoothDamp in canvas space with a speed cap
			float maxStep = maxSpeedPixelsPerSec * Time.deltaTime;
			next = Vector2.SmoothDamp(_filtered, avgTarget, ref _vel, smoothTime, Mathf.Infinity, Time.deltaTime);
			var delta = next - _filtered;
			if (delta.magnitude > maxStep) next = _filtered + delta.normalized * maxStep;
		}
		else
		{
			next = Vector2.Lerp(_filtered, avgTarget, lowConfLerp);
		}

		_filtered = next;
		cursorRect.anchoredPosition = _filtered;
	}

	static float GetField(object obj, string fieldName, float fallback)
	{
		if (obj == null) return fallback;
		var f = obj.GetType().GetField(fieldName, BindingFlags.Instance | BindingFlags.Public);
		if (f == null) return fallback;
		try { return (float)f.GetValue(obj); } catch { return fallback; }
	}

	static MonoBehaviour FindPoseRunner()
	{
		foreach (var behaviour in FindObjectsOfType<MonoBehaviour>())
		{
			if (behaviour == null) continue;
			if (behaviour.GetType().Name == "PoseLandmarkerRunner")
			{
				return behaviour;
			}
		}
		return null;
	}

	IList TryGetPoseLandmarks(MonoBehaviour source, out object firstList)
	{
		firstList = null;
		if (source == null) return null;

		var sourceType = source.GetType();
		if (_latestResultProperty == null || _latestResultProperty.DeclaringType != sourceType)
		{
			_latestResultProperty = sourceType.GetProperty("LatestResult", BindingFlags.Instance | BindingFlags.Public);
		}
		if (_latestResultProperty == null) return null;

		object result;
		try
		{
			result = _latestResultProperty.GetValue(source);
		}
		catch
		{
			return null;
		}
		if (result == null) return null;

		var resultType = result.GetType();
		if (_poseLandmarksField == null || _poseLandmarksField.DeclaringType != resultType)
		{
			_poseLandmarksField = resultType.GetField("poseLandmarks", BindingFlags.Instance | BindingFlags.Public);
		}
		var poseLandmarks = _poseLandmarksField?.GetValue(result) as IList;
		if (poseLandmarks == null || poseLandmarks.Count == 0) return poseLandmarks;
		firstList = poseLandmarks[0];
		return poseLandmarks;
	}
}


