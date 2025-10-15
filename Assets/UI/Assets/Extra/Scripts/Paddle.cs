using UnityEngine;
using System.Collections;

public class Paddle : MonoBehaviour {
    
	private void Update ()
	{
        var temp = Input.mousePosition;
        temp.z = 532f; 
        transform.position = Camera.main.ScreenToWorldPoint(temp);
	}
}
