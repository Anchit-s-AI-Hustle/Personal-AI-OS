"""End-to-end test: pick a real WAV chunk, transcribe via Groq Whisper."""
from pathlib import Path

from transcription import get_whisper_engine
from utils.logger import setup_logging

setup_logging()

audio = Path("data/audio_chunks/session-20260508T210351Z_chunk_0014.wav").resolve()
print(f"Audio: {audio}  ({audio.stat().st_size / 1024:.0f} KB)")

eng = get_whisper_engine()
print(f"Engine: {type(eng).__name__}")
print()
print("Transcribing (this calls Groq)...")
result = eng.transcribe_file(audio)
print()
print(f"  language : {result.language}")
print(f"  duration : {result.duration}")
print(f"  segments : {len(result.segments)}")
print()
print(f"  text     : {result.text!r}")
