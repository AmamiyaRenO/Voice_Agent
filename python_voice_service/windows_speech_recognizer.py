"""Windows Speech Recognition demo.

Run this script on Windows to continuously print recognized speech using the
built-in Speech API (SAPI). It listens on the default microphone and prints
partial and final results in real time.

Requirements:
    - Python 3.9+
    - pywin32 (``pip install pywin32``)

Usage:
    python windows_speech_recognizer.py

Press Ctrl+C to stop the recognizer.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

try:
    import pythoncom
    import win32com.client
except ImportError:  # pragma: no cover - Only occurs on non-Windows setups.
    pythoncom = None  # type: ignore
    win32com = None  # type: ignore


IS_WINDOWS = sys.platform.startswith("win32")


def ensure_windows() -> None:
    if not IS_WINDOWS:
        raise SystemExit(
            "This demo only works on Windows because it relies on the SAPI COM API."
        )
    if pythoncom is None or win32com is None:
        raise SystemExit(
            "pywin32 is required. Install it with 'pip install pywin32' before running."
        )


@dataclass
class RecognitionResult:
    """Container for results forwarded from the SAPI callbacks."""

    text: str
    is_final: bool
    confidence: Optional[float] = None


class _SpeechEvents:
    """Event sink passed to DispatchWithEvents to receive callbacks."""

    def __init__(self, handler: Optional["WindowsSpeechRecognizer"] = None) -> None:
        # ``win32com.client.WithEvents`` instantiates the sink without arguments, so we
        # allow ``handler`` to be provided later.
        self._handler = handler

    def OnRecognition(self, _stream_number, _stream_position, _recognition_type, result):
        phrase = win32com.client.Dispatch(result)
        text = phrase.PhraseInfo.GetText()
        try:
            confidence = phrase.PhraseInfo.Elements.Item(0).EngineConfidence
        except Exception:  # pragma: no cover - EngineConfidence can be missing.
            confidence = None
        if self._handler is not None:
            self._handler.enqueue_result(
                RecognitionResult(text=text, is_final=True, confidence=confidence)
            )

    def OnHypothesis(self, _stream_number, _stream_position, result):
        phrase = win32com.client.Dispatch(result)
        text = phrase.PhraseInfo.GetText()
        if self._handler is not None:
            self._handler.enqueue_result(RecognitionResult(text=text, is_final=False))


class WindowsSpeechRecognizer:
    """Thin wrapper around the Windows Speech API that prints results."""

    def __init__(self) -> None:
        ensure_windows()
        self._results: "queue.Queue[RecognitionResult]" = queue.Queue()
        self._recognizer = None
        self._event_sink = None

    def start(self) -> None:
        pythoncom.CoInitialize()
        # ``SpInprocRecognizer`` gives us full control over the audio input and
        # avoids depending on the system-wide shared recognizer configuration,
        # which can leave the capture device unset on some Windows installs.
        recognizer = win32com.client.Dispatch("SAPI.SpInprocRecognizer")

        # Bind the recognizer to the default multimedia microphone so that SAPI
        # actually opens the capture device even when the shared recognizer is
        # disabled. ``SpMMAudioIn`` exposes ``GetDescription`` for logging.
        try:
            audio_in = win32com.client.Dispatch("SAPI.SpMMAudioIn")
            recognizer.AudioInputStream = audio_in
            print(
                "[WindowsSpeechRecognizer] Using input device: "
                f"{audio_in.GetDescription()}"
            )
        except Exception as exc:
            print(
                "[WindowsSpeechRecognizer] Falling back to recognizer default "
                f"audio input ({exc})."
            )

        context = recognizer.CreateRecoContext()
        recognizer.State = 1  # 1 == Active
        grammar = context.CreateGrammar()
        # Dictation grammars must be explicitly loaded before activation.
        grammar.DictationLoad()
        grammar.DictationSetState(1)  # 1 == SGDSActive

        # Subscribe to recognition events.
        context.EventInterests = 0x7FFF  # Receive all events for debugging.
        self._event_sink = win32com.client.WithEvents(context, _SpeechEvents)
        self._event_sink._handler = self

        self._recognizer = recognizer
        print("[WindowsSpeechRecognizer] Listening... Press Ctrl+C to stop.")

    def pump_events(self) -> None:
        # Pump COM events on this thread.
        while True:
            pythoncom.PumpWaitingMessages()
            time.sleep(0.01)

    def enqueue_result(self, result: RecognitionResult) -> None:
        self._results.put(result)

    def print_results_loop(self) -> None:
        while True:
            result = self._results.get()
            tag = "FINAL" if result.is_final else "PARTIAL"
            if result.confidence is not None:
                print(f"[{tag}] {result.text} (confidence: {result.confidence:.2f})")
            else:
                print(f"[{tag}] {result.text}")


if __name__ == "__main__":
    ensure_windows()

    recognizer = WindowsSpeechRecognizer()
    recognizer.start()

    # Start background thread to print results as they arrive.
    printer_thread = threading.Thread(target=recognizer.print_results_loop, daemon=True)
    printer_thread.start()

    try:
        recognizer.pump_events()
    except KeyboardInterrupt:
        print("\n[WindowsSpeechRecognizer] Stopped by user.")
    finally:
        pythoncom.CoUninitialize()
