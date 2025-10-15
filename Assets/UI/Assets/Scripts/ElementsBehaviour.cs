using UnityEngine;
using UnityEngine.UI;

namespace Assets.Scripts
{
    public class ElementsBehaviour : MonoBehaviour
    {
        public GameObject CanvasGroup;
        public GameObject HiddenPanel;
        public GameObject Mask;
        public Canvas CanvasDark;
        private Animator _anim;
        private Animator _anim2;
        private bool _slideRight = true;
        public Dropdown ExampleDd;
        public GameObject Caption;
        public Image[] AnimatedIcons = new Image[6];
        public bool[] AnimIcons = new bool[6];
        private bool _captionSet;
        public GameObject PaddleImage;

        private void Start()
        {
            Caption.SetActive(false);
            _anim = CanvasGroup.GetComponent<Animator>();
            _anim.enabled = false;
            _anim2 = HiddenPanel.GetComponent<Animator>();
            _anim2.enabled = false;
            Mask.GetComponent<Mask>().enabled = false;
            ExampleDd.value = 0;
            PaddleImage.SetActive(false);
        }

        private void Update()
        {
            AnimateImages();
            if (Input.GetKeyDown(KeyCode.Escape))
            {
                BackToStart();
                PaddleImage.SetActive(false);
            }
        }

        public void SetCaptionPosition(GameObject btn)
        {
            if (!_captionSet)
            {
                Caption.transform.position = new Vector3(btn.transform.position.x - 40, btn.transform.position.y + 40, btn.transform.position.z);
                _captionSet = true;
            }
        }

        public void ResetCaption()
        {
            _captionSet = false;
        }

        public void BackToStart()
        {
            _anim.SetInteger("AnimState", 5);
            _anim.Play("BackToStart");
        }
        
        public void RotateLeft()
        {
            Mask.GetComponent<Mask>().enabled = true;
            _anim.enabled = true;
            _anim.SetInteger("AnimState", 0);
            _anim.Play("RotateLeft");
        }

        public void RotateUp()
        {
            CanvasDark.sortingOrder = 1;
            _anim.SetInteger("AnimState", 1);
            _anim.Play("RotateUp");
        }

        public void RotateDown()
        {
            _anim.SetInteger("AnimState", 2);
            _anim.Play("RotateDown");
        }

        public void RotateRight()
        {
            CanvasDark.sortingOrder = 0;
            _anim.SetInteger("AnimState", 3);
            _anim.Play("RotateRight");
        }

        public void RotateToWorld()
        {
            PaddleImage.SetActive(true);
            Mask.GetComponent<Mask>().enabled = true;
            _anim.enabled = true;
            _anim.SetInteger("AnimState", 4);
            _anim.Play("RotateToWorld");
        }

        public void Slide()
        {
            _anim2.enabled = true;
            Mask.GetComponent<Mask>().enabled = false;
            if (_slideRight)
            {
                _anim2.SetInteger("SlideState", 0);
                _anim2.Play("Slide");
            }
            else
            {
                _anim2.SetInteger("SlideState", 1);
                _anim2.Play("SlideBack");    
            }
            
            _slideRight = !_slideRight;
        }

        public void AnimIconImage(int anim)
        {
            AnimIcons[anim] = !AnimIcons[anim];
        }

        private void AnimateImages()
        {
            for (var i = 0; i < AnimatedIcons.Length; i++)
            {
                if (AnimIcons[i])
                {
                    AnimatedIcons[i].fillAmount += 0.01f;

                    if (AnimatedIcons[i].fillAmount == 1)
                        AnimatedIcons[i].fillAmount = 0;
                }
                else
                    AnimatedIcons[i].fillAmount = 0;
            }
        }

        public void Close()
        {
            Application.Quit();
        }
    }
}
