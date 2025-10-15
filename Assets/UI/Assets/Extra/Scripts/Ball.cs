using UnityEngine;

public class Ball : MonoBehaviour
{
    private Vector3 startingPosition;
    public GameObject Sparkles;
    private bool _effects;
    private float _effectsTimer = 3.0f;

	private void Start ()
	{
	    startingPosition = transform.localPosition;
	    GetComponent<Rigidbody>().velocity = new Vector3(400, 400, 400);
        Sparkles.SetActive(false);
    }

    private void Update()
    {
        if (transform.localPosition.x < -780)
        {
            transform.localPosition = startingPosition;
            GetComponent<Rigidbody>().velocity = new Vector3(500, 500, 500);
        }

        if (_effects)
        {
            Sparkles.SetActive(true);
            _effectsTimer -= Time.deltaTime;
            if (_effectsTimer <= 0)
            {
                Sparkles.SetActive(false);
                _effects = false;
            }
        }
    }

    private void OnCollisionExit(Collision cl)
    {
        if (cl.gameObject.name == "Paddle")
        {
            Sparkles.SetActive(false);
            _effects = true;
            _effectsTimer = 3.0f;
        }
    }
}
