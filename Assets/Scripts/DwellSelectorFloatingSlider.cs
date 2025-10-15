using UnityEngine;
using UnityEngine.UI;
using UnityEngine.SceneManagement;
using RobotVoice; // for MqttIntentPublisher

public class DwellSelectorFloatingSlider : MonoBehaviour
{
    [Header("Refs")]
    public Canvas canvas;
    public RectTransform cursorRect;          // Cursor_Hand
    public RectTransform floatingSliderRect;  // Slider_Floating (RectTransform)
    public Slider floatingSlider;             // Slider_Floating (Slider)

    [System.Serializable]
    public struct CardEntry
    {
        public RectTransform cardRect;        // Card_BalloonPop, Card_Garden...
        public string sceneName;              // Scene to load on select
        public Image hoverHighlight;          // Optional overlay to enable on hover
    }
    public CardEntry[] cards;

    [Header("Behavior")]
    public float dwellSeconds = 1.4f;         // good starting point
    public Vector2 sliderOffset = new Vector2(48f, -48f); // relative to cursor
    [Tooltip("If enabled, dwell selection will publish a LAUNCH_GAME command to the message hub instead of loading a Unity scene.")]
    public bool openViaMessageHub = true;
    [Tooltip("MQTT publisher (message hub)")]
    public MqttIntentPublisher mqttPublisher;

    float _timer;
    int _hovered = -1;

    void Start()
    {
        if (floatingSlider != null)
        {
            floatingSlider.minValue = 0f;
            floatingSlider.maxValue = 1f;
            floatingSlider.value = 0f;
            floatingSlider.gameObject.SetActive(false);
        }
    }

    void Update()
    {
        if (canvas == null || cursorRect == null || floatingSliderRect == null || floatingSlider == null) return;

        // keep slider parked beside the cursor
        floatingSliderRect.anchoredPosition = cursorRect.anchoredPosition + sliderOffset;

        // find which card (if any) the cursor is over
        Vector2 sp = RectLocalToScreen(cursorRect, canvas);
        int newHover = -1;
        for (int i = 0; i < cards.Length; i++)
        {
            var c = cards[i].cardRect;
            if (c != null && RectTransformUtility.RectangleContainsScreenPoint(c, sp, canvas.worldCamera))
            {
                newHover = i; break;
            }
        }

        // hover changed → reset timer/visuals
        if (newHover != _hovered)
        {
            if (_hovered >= 0 && cards[_hovered].hoverHighlight) cards[_hovered].hoverHighlight.enabled = false;
            _hovered = newHover;
            _timer = 0f;
            floatingSlider.value = 0f;
        }

        if (_hovered >= 0)
        {
            if (cards[_hovered].hoverHighlight) cards[_hovered].hoverHighlight.enabled = true;
            if (!floatingSlider.gameObject.activeSelf) floatingSlider.gameObject.SetActive(true);

            _timer += Time.deltaTime;
            floatingSlider.value = Mathf.Clamp01(_timer / dwellSeconds);

            if (_timer >= dwellSeconds)
            {
                string value = cards[_hovered].sceneName;
                if (openViaMessageHub && mqttPublisher != null && !string.IsNullOrEmpty(value))
                {
                    _ = mqttPublisher.PublishLaunchIntentAsync(value, "ui_dwell");
                }
                else if (!string.IsNullOrEmpty(value))
                {
                    SceneManager.LoadScene(value);
                }
            }
        }
        else
        {
            if (floatingSlider.gameObject.activeSelf) floatingSlider.gameObject.SetActive(false);
        }
    }

    static Vector2 RectLocalToScreen(RectTransform rect, Canvas canvas)
    {
        // For Screen Space - Overlay, pass null camera; for others, pass canvas.worldCamera
        var cam = (canvas.renderMode == RenderMode.ScreenSpaceOverlay) ? null : canvas.worldCamera;
        // rect.position is already world space; convert directly to screen point
        return RectTransformUtility.WorldToScreenPoint(cam, rect.position);
    }

}
