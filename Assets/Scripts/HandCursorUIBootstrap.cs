using UnityEngine;
using UnityEngine.UI;
using Mediapipe.Unity.Sample.PoseLandmarkDetection;

public class HandCursorUIBootstrap : MonoBehaviour
{
	[Header("Optional Refs")]
	public PoseLandmarkerRunner poseRunner;

	[Header("Cursor Visual")]
	public Sprite cursorSprite;
	public Vector2 cursorSize = new Vector2(64, 64);

	void Awake()
	{
		// Ensure a Canvas exists
		var canvas = FindObjectOfType<Canvas>();
		if (canvas == null)
		{
			var go = new GameObject("Canvas", typeof(Canvas), typeof(CanvasScaler), typeof(GraphicRaycaster));
			canvas = go.GetComponent<Canvas>();
			canvas.renderMode = RenderMode.ScreenSpaceOverlay;
			var scaler = go.GetComponent<CanvasScaler>();
			scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
			scaler.referenceResolution = new Vector2(1920, 1080);
		}

		// Create cursor if missing
		var cursor = canvas.transform.Find("Cursor_Hand") as RectTransform;
		if (cursor == null)
		{
			var cg = new GameObject("Cursor_Hand", typeof(RectTransform), typeof(Image));
			cursor = cg.GetComponent<RectTransform>();
			cursor.SetParent(canvas.transform, false);
			var img = cg.GetComponent<Image>();
			img.raycastTarget = false;
			// Auto-load cursor sprite from Resources if not assigned
			if (cursorSprite == null)
			{
				var tex = Resources.Load<Texture2D>("Cursor/hand");
				if (tex != null)
				{
					cursorSprite = Sprite.Create(tex, new Rect(0, 0, tex.width, tex.height), new Vector2(0.5f, 0.5f), 100f);
				}
			}
			if (cursorSprite != null) img.sprite = cursorSprite;
			cursor.sizeDelta = cursorSize;
		}

		// Ensure HandCursorFromRunner present
		var tracker = FindObjectOfType<HandCursorFromRunner>();
		if (tracker == null)
		{
			tracker = gameObject.AddComponent<HandCursorFromRunner>();
		}

		if (poseRunner == null)
		{
			poseRunner = FindObjectOfType<PoseLandmarkerRunner>();
		}

		tracker.runner = poseRunner;
		tracker.canvas = canvas;
		tracker.cursorRect = cursor;
	}
}


