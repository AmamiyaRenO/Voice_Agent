using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using Ionic.Zip;
using Unity.Profiling;
using UnityEngine;
using UnityEngine.Networking;

public class VoskSpeechToText : MonoBehaviour
{
        [Tooltip("Location of the model, relative to the Streaming Assets folder.")]
        public string ModelPath = "vosk-model-small-ru-0.22.zip";

        [Tooltip("The source of the microphone input.")]
        public VoiceProcessor VoiceProcessor;
	[Tooltip("The Max number of alternatives that will be processed.")]
	public int MaxAlternatives = 3;

        [Tooltip("How long should we record before restarting?")]
        public float MaxRecordLength = 5;

        [Tooltip("Should the recognizer start when the application is launched?")]
        public bool AutoStart = true;

        [Tooltip("The phrases that will be detected. If left empty, all words will be detected.")]
        public List<string> KeyPhrases = new List<string>();

        [Tooltip("Sample rate used for microphone capture and passed to the speech engine.")]
        public int SampleRate = 16000;

        [Tooltip("Speech-to-text engine implementation. Defaults to the built-in Vosk engine when left empty.")]
        [SerializeField]
        private SpeechToTextEngineBase speechEngine;

        //Resolved engine instance used for recognition.
        private SpeechToTextEngineBase _engineInstance;

        //Flag to indicate the engine has completed initialisation.
        private bool _engineReady;

	//Called when the the state of the controller changes.
	public Action<string> OnStatusUpdated;

        //Called after the user is done speaking and the speech engine processes the audio.
        public Action<string> OnTranscriptionResult;

	//The absolute path to the decompressed model folder.
	private string _decompressedModelPath;

	//Flag that is used to wait for the model file to decompress successfully.
	private bool _isDecompressing;

	//Flag that is used to wait for the the script to start successfully.
	private bool _isInitializing;

        //Flag that is used to check if the speech engine was started.
        private bool _didInit;

        //Threading Logic

        // Flag to signal we are ending
        private bool _running;

	//Thread safe queue of microphone data.
	private readonly ConcurrentQueue<short[]> _threadedBufferQueue = new ConcurrentQueue<short[]>();

        //Thread safe queue of resuts
        private readonly ConcurrentQueue<string> _threadedResultQueue = new ConcurrentQueue<string>();

        private SpeechToTextEngineBase Engine
        {
                get
                {
                        if (_engineInstance == null)
                        {
                                _engineInstance = ResolveEngineInstance();
                        }

                        return _engineInstance;
                }
        }

        public ISpeechToTextEngine CurrentEngine => Engine;

        private SpeechToTextEngineBase ResolveEngineInstance()
        {
                if (speechEngine != null)
                {
                        return speechEngine;
                }

                var existing = GetComponent<SpeechToTextEngineBase>();
                if (existing != null)
                {
                        speechEngine = existing;
                        return existing;
                }

                speechEngine = gameObject.AddComponent<VoskSpeechToTextEngine>();
                return speechEngine;
        }

        private int GetSampleRate()
        {
                return Mathf.Max(1, SampleRate);
        }
        void Awake()
        {
                if (VoiceProcessor == null)
                {
                        VoiceProcessor = GetComponent<VoiceProcessor>();
                        if (VoiceProcessor == null)
                        {
                                VoiceProcessor = gameObject.AddComponent<VoiceProcessor>();
                        }
                }

                ResolveEngineInstance();
        }




        static readonly ProfilerMarker speechEngineProcessMarker = new ProfilerMarker("SpeechEngine.TryRecognise");

	//If Auto start is enabled, starts vosk speech to text.
	void Start()
	{
		//KeyPhrases = new List<string> { "forward", "backward", "left", "right" };
                if (AutoStart)
                {
                        StartSpeechRecognition();
                }
        }

        /// <summary>
        /// Start speech recognition using the configured engine.
        /// </summary>
        /// <param name="keyPhrases">A list of keywords/phrases. Keywords need to exist in the model's dictionary.</param>
        /// <param name="modelPath">The path to the model folder relative to StreamingAssets. If the path has a .zip ending, it will be decompressed into the application data persistent folder.</param>
        /// <param name="startMicrophone">Should the microphone start recording immediately after initialisation?</param>
        /// <param name="maxAlternatives">The maximum number of alternative phrases detected</param>
        public void StartSpeechRecognition(List<string> keyPhrases = null, string modelPath = default, bool startMicrophone = false, int maxAlternatives = 3)
        {
                StartRecognitionInternal(keyPhrases, modelPath, startMicrophone, maxAlternatives);
        }

        /// <summary>
        /// Backwards-compatible entry point for legacy scripts.
        /// </summary>
        public void StartVoskStt(List<string> keyPhrases = null, string modelPath = default, bool startMicrophone = false, int maxAlternatives = 3)
        {
                StartRecognitionInternal(keyPhrases, modelPath, startMicrophone, maxAlternatives);
        }

        private void StartRecognitionInternal(List<string> keyPhrases, string modelPath, bool startMicrophone, int maxAlternatives)
        {
                if (_isInitializing)
                {
                        Debug.LogError("Initializing in progress!");
                        return;
                }
                if (_didInit)
                {
                        Debug.LogError("Speech engine has already been initialized!");
                        return;
                }

                if (!string.IsNullOrEmpty(modelPath))
                {
                        ModelPath = modelPath;
                }

                if (keyPhrases != null)
                {
                        KeyPhrases = keyPhrases;
                }

                MaxAlternatives = maxAlternatives;
                StartCoroutine(DoStartVoskStt(startMicrophone));
        }

	//Decompress model, load settings, start Vosk and optionally start the microphone
        private IEnumerator DoStartVoskStt(bool startMicrophone)
        {
                _isInitializing = true;
                _engineReady = false;

                yield return WaitForMicrophoneInput();

                yield return Decompress();

                var engine = Engine;
                OnStatusUpdated?.Invoke($"Loading model ({engine.EngineName}) from: " + _decompressedModelPath);

                int sampleRate = GetSampleRate();

                SpeechToTextEngineConfiguration configuration = new SpeechToTextEngineConfiguration(
                        _decompressedModelPath,
                        KeyPhrases,
                        MaxAlternatives,
                        sampleRate);

                Task initialiseTask;
                try
                {
                        initialiseTask = engine.InitialiseAsync(configuration);
                }
                catch (Exception exception)
                {
                        Debug.LogError($"Failed to initialise speech engine {engine.EngineName}: {exception}");
                        OnStatusUpdated?.Invoke($"Failed to initialise {engine.EngineName}.");
                        _isInitializing = false;
                        yield break;
                }

                while (!initialiseTask.IsCompleted)
                {
                        yield return null;
                }

                if (initialiseTask.IsFaulted || initialiseTask.IsCanceled)
                {
                        Exception ex = initialiseTask.Exception?.GetBaseException();
                        if (ex == null && initialiseTask.IsCanceled)
                        {
                                ex = new OperationCanceledException("Speech engine initialisation was cancelled.");
                        }

                        Debug.LogError($"Failed to initialise speech engine {engine.EngineName}: {ex}");
                        OnStatusUpdated?.Invoke($"Failed to initialise {engine.EngineName}.");
                        _isInitializing = false;
                        yield break;
                }

                _engineReady = true;

                OnStatusUpdated?.Invoke($"Initialized ({engine.EngineName})");
                VoiceProcessor.OnFrameCaptured += VoiceProcessorOnOnFrameCaptured;
                VoiceProcessor.OnRecordingStop += VoiceProcessorOnOnRecordingStop;

                if (startMicrophone)
                        VoiceProcessor.StartRecording(sampleRate);

                _isInitializing = false;
                _didInit = true;

                ToggleRecording();
        }

	//Decompress the model zip file or return the location of the decompressed files.
	private IEnumerator Decompress()
	{
		if (!Path.HasExtension(ModelPath)
			|| Directory.Exists(
				Path.Combine(Application.persistentDataPath, Path.GetFileNameWithoutExtension(ModelPath))))
		{
			OnStatusUpdated?.Invoke("Using existing decompressed model.");
			_decompressedModelPath =
				Path.Combine(Application.persistentDataPath, Path.GetFileNameWithoutExtension(ModelPath));
			Debug.Log(_decompressedModelPath);

			yield break;
		}

		OnStatusUpdated?.Invoke("Decompressing model...");
		string dataPath = Path.Combine(Application.streamingAssetsPath, ModelPath);

		Stream dataStream;
		// Read data from the streaming assets path. You cannot access the streaming assets directly on Android.
		if (dataPath.Contains("://"))
		{
			UnityWebRequest www = UnityWebRequest.Get(dataPath);
			www.SendWebRequest();
			while (!www.isDone)
			{
				yield return null;
			}
			dataStream = new MemoryStream(www.downloadHandler.data);
		}
		// Read the file directly on valid platforms.
		else
		{
			dataStream = File.OpenRead(dataPath);
		}

		//Read the Zip File
		var zipFile = ZipFile.Read(dataStream);

		//Listen for the zip file to complete extraction
		zipFile.ExtractProgress += ZipFileOnExtractProgress;

		//Update status text
		OnStatusUpdated?.Invoke("Reading Zip file");

		//Start Extraction
		zipFile.ExtractAll(Application.persistentDataPath);

		//Wait until it's complete
		while (_isDecompressing == false)
		{
			yield return null;
		}
		//Override path given in ZipFileOnExtractProgress to prevent crash
		_decompressedModelPath = Path.Combine(Application.persistentDataPath, Path.GetFileNameWithoutExtension(ModelPath));

		//Update status text
		OnStatusUpdated?.Invoke("Decompressing complete!");
		//Wait a second in case we need to initialize another object.
		yield return new WaitForSeconds(1);
		//Dispose the zipfile reader.
		zipFile.Dispose();
	}

	///The function that is called when the zip file extraction process is updated.
	private void ZipFileOnExtractProgress(object sender, ExtractProgressEventArgs e)
	{
		if (e.EventType == ZipProgressEventType.Extracting_AfterExtractAll)
		{
			_isDecompressing = true;
			_decompressedModelPath = e.ExtractLocation;
		}
	}

	//Wait until microphones are initialized
	private IEnumerator WaitForMicrophoneInput()
	{
		while (Microphone.devices.Length <= 0)
			yield return null;
	}

	//Can be called from a script or a GUI button to start detection.
        public void ToggleRecording()
        {
                Debug.Log("Toogle Recording");
                if (!VoiceProcessor.IsRecording)
                {
                        if (!_engineReady)
                        {
                                Debug.LogWarning("Speech engine is not initialised; call StartSpeechRecognition() first.");
                                return;
                        }

                        Debug.Log("Start Recording");
                        _running = true;
                        int sampleRate = GetSampleRate();
                        VoiceProcessor.StartRecording(sampleRate);
                        Task.Run(ThreadedWork).ConfigureAwait(false);
                }
                else
                {
                        Debug.Log("Stop Recording");
                        _running = false;
                        VoiceProcessor.StopRecording();
                }
        }

	//Calls the On Phrase Recognized event on the Unity Thread
	void Update()
	{
		if (_threadedResultQueue.TryDequeue(out string voiceResult))
		{
		    OnTranscriptionResult?.Invoke(voiceResult);
		}
	}

	//Callback from the voice processor when new audio is detected
	private void VoiceProcessorOnOnFrameCaptured(short[] samples)
	{	
                _threadedBufferQueue.Enqueue(samples);
	}

	//Callback from the voice processor when recording stops
	private void VoiceProcessorOnOnRecordingStop()
	{
                Debug.Log("Stopped");
	}

        //Feeds the audio logic into the configured speech engine
        private async Task ThreadedWork()
        {
                if (!_engineReady)
                {
                        Debug.LogWarning("Speech engine is not ready. Did you call StartSpeechRecognition()?");
                        return;
                }

                var engine = Engine;

                while (_running)
                {
                        if (_threadedBufferQueue.TryDequeue(out short[] voiceResult))
                        {
                                using (speechEngineProcessMarker.Auto())
                                {
                                        try
                                        {
                                                if (engine.TryRecognise(voiceResult, out string result))
                                                {
                                                        _threadedResultQueue.Enqueue(result);
                                                }
                                        }
                                        catch (Exception exception)
                                        {
                                                Debug.LogError($"Speech engine {engine.EngineName} threw an exception: {exception}");
                                        }
                                }
                        }
                        else
                        {
                                // Wait for some data
                                await Task.Delay(100).ConfigureAwait(false);
                        }
                }
        }

        private void OnDestroy()
        {
                _running = false;
                _engineReady = false;
                _didInit = false;
                _isInitializing = false;

                if (VoiceProcessor != null)
                {
                        VoiceProcessor.OnFrameCaptured -= VoiceProcessorOnOnFrameCaptured;
                        VoiceProcessor.OnRecordingStop -= VoiceProcessorOnOnRecordingStop;

                        if (VoiceProcessor.IsRecording)
                        {
                                VoiceProcessor.StopRecording();
                        }
                }

                if (_engineInstance != null)
                {
                        _engineInstance.Dispose();
                        _engineInstance = null;
                }
        }



}
