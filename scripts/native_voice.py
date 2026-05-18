#!/usr/bin/env python3
"""
Hermes Native Voice — 原生语音模式
===================================
唤醒词 -> STT -> 思考 -> TTS 全管线

对标: OpenHuman "native voice - STT in, TTS out, mascot lip-sync"

管线:
  1. 唤醒词检测 (Porcupine 引擎，高精度低误触发)
  2. 语音录制 (PyAudio)
  3. STT 转录 (MLX Whisper 本地，支持流式)
  4. LLM 思考 (本地 oMLX Qwen3.5-4B / FinnA 云端降级)
  5. TTS 合成 (macOS say / edge-tts)
  6. 播放 + Mascot 同步

模式:
  wake:       唤醒词触发 (Porcupine)
  wake-stream:唤醒词 + 流式STT (边说边显示)
  ptt:        按键触发
  test-stt:   测试 STT
  test-tts:   测试 TTS

用法:
  python3 native_voice.py wake
  python3 native_voice.py wake-stream
  python3 native_voice.py ptt
  python3 native_voice.py test-stt
  python3 native_voice.py test-tts
"""

import os
import struct
import subprocess
import sys
import time
import wave
from datetime import datetime
from pathlib import Path
from collections import deque

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
VOICE_DIR = HERMES_HOME / "voice"
AUDIO_DIR = VOICE_DIR / "recordings"

WAKE_WORD = os.environ.get("HERMES_WAKE_WORD", "hermes")
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_DURATION = 0.03  # 30ms
PORCUPINE_CHUNK = 512  # samples per porcupine frame
ENERGY_THRESHOLD = 500

# LLM: oMLX local first, FinnA cloud fallback
OMLX_BASE = "http://localhost:4560/v1"
FINNA_KEY = os.environ.get("FINNA_API_KEY", "app-BqyKsTO4Om3JGoPCTkJX080J")
FINNA_BASE = "https://www.finna.com.cn/v1"


# ---- Wake Word: Porcupine Engine ----

def init_porcupine():
    """Initialize Porcupine wake word engine. Returns (porcupine, keyword_paths)."""
    try:
        import pvporcupine
    except ImportError:
        print("  ⚠️ pvporcupine not installed. Falling back to energy threshold.")
        print("     pip install pvporcupine")
        return None, None

    try:
        # Try built-in "computer" keyword as default, or custom if available
        keyword_paths = [pvporcupine.KEYWORD_PATHS.get("computer")]
        if keyword_paths[0] is None:
            # try "jarvis"
            keyword_paths = [pvporcupine.KEYWORD_PATHS.get("jarvis")]

        if keyword_paths[0] is None:
            # List available built-in keywords
            available = [k for k, v in pvporcupine.KEYWORD_PATHS.items() if v is not None]
            print(f"  Available keywords: {available}")
            return None, None

        porcupine = pvporcupine.create(
            keyword_paths=keyword_paths,
            sensitivities=[0.7],
        )
        print("  🔑 Porcupine wake word engine ready")
        return porcupine, keyword_paths
    except Exception as e:
        print(f"  ⚠️ Porcupine init failed: {e}")
        return None, None


def porcupine_wake_loop():
    """Wake word detection with Porcupine + streaming STT."""
    print("\n  🦻 Porcupine Wake Word Mode - say 'hermes' or 'computer'")
    print("     Press Ctrl+C to stop\n")

    try:
        import pvporcupine
        import pyaudio
    except ImportError as e:
        print(f"  ❌ Missing dependency: {e}")
        return

    deps = check_deps()
    for d in deps:
        print(f"     {d}")
    print()

    porcupine, keyword_paths = init_porcupine()
    if porcupine is None:
        print("  ⚠️ Falling back to energy-threshold wake word")
        return energy_wake_loop()

    p = pyaudio.PyAudio()
    stream = p.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=PORCUPINE_CHUNK,
    )

    print("  ✅ Listening for wake word...\n")

    try:
        while True:
            pcm = stream.read(PORCUPINE_CHUNK, exception_on_overflow=False)
            pcm_unpacked = struct.unpack_from("h" * PORCUPINE_CHUNK, pcm)

            keyword_index = porcupine.process(pcm_unpacked)
            if keyword_index >= 0:
                print("  🔔 Wake word detected!")
                # Record and process
                frames = []
                silence_frames = 0
                max_silence = int(2.0 / (PORCUPINE_CHUNK / SAMPLE_RATE))  # 2s silence = stop

                for _ in range(int(15 / (PORCUPINE_CHUNK / SAMPLE_RATE))):
                    pcm = stream.read(PORCUPINE_CHUNK, exception_on_overflow=False)
                    frames.append(pcm)

                    # Simple energy check for silence
                    energy = sum(abs(x) for x in struct.unpack_from("h" * PORCUPINE_CHUNK, pcm)) / PORCUPINE_CHUNK
                    if energy < 200:
                        silence_frames += 1
                    else:
                        silence_frames = 0

                    if silence_frames > max_silence and len(frames) > int(1.5 / (PORCUPINE_CHUNK / SAMPLE_RATE)):
                        break

                # Save audio
                AUDIO_DIR.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                wav_path = str(AUDIO_DIR / f"wake_{ts}.wav")

                wf = wave.open(wav_path, 'wb')
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(b''.join(frames))
                wf.close()

                text = transcribe_audio(wav_path)
                print(f"     📝 {text}")

                if text and text.strip() and "⚠️" not in text:
                    print("     🤔 Thinking...")
                    response = think(text)
                    speak(response)
                    print("  ✅ Listening for wake word...\n")

    except KeyboardInterrupt:
        print("\n  👋")
    finally:
        porcupine.delete()
        stream.stop_stream()
        stream.close()
        p.terminate()


def porcupine_stream_loop():
    """Wake word + streaming STT: transcribe in chunks for real-time feedback."""
    print("\n  🦻 Wake Word + Streaming STT Mode")
    print("     Press Ctrl+C to stop\n")

    try:
        import pvporcupine
        import pyaudio
    except ImportError as e:
        print(f"  ❌ Missing dependency: {e}")
        return

    porcupine, keyword_paths = init_porcupine()
    if porcupine is None:
        print("  ⚠️ Falling back to energy-threshold")
        return energy_wake_loop()

    p = pyaudio.PyAudio()
    stream = p.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=PORCUPINE_CHUNK,
    )

    print("  ✅ Listening for wake word...\n")

    try:
        while True:
            pcm = stream.read(PORCUPINE_CHUNK, exception_on_overflow=False)
            pcm_unpacked = struct.unpack_from("h" * PORCUPINE_CHUNK, pcm)

            keyword_index = porcupine.process(pcm_unpacked)
            if keyword_index >= 0:
                print("  🔔 Wake word detected! Speak now...")

                frames = []
                silence_frames = 0
                max_silence = int(2.0 / (PORCUPINE_CHUNK / SAMPLE_RATE))
                chunk_count = 0
                STREAM_WHISPER_INTERVAL = int(1.5 / (PORCUPINE_CHUNK / SAMPLE_RATE))  # transcribe every 1.5s

                full_text = ""

                while True:
                    pcm = stream.read(PORCUPINE_CHUNK, exception_on_overflow=False)
                    frames.append(pcm)
                    chunk_count += 1

                    energy = sum(abs(x) for x in struct.unpack_from("h" * PORCUPINE_CHUNK, pcm)) / PORCUPINE_CHUNK
                    if energy < 200:
                        silence_frames += 1
                    else:
                        silence_frames = 0

                    # Streaming transcription every 1.5s
                    if chunk_count % STREAM_WHISPER_INTERVAL == 0 and len(frames) > int(1.0 / (PORCUPINE_CHUNK / SAMPLE_RATE)):
                        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
                        partial_path = str(AUDIO_DIR / "stream_partial.wav")
                        wf = wave.open(partial_path, 'wb')
                        wf.setnchannels(CHANNELS)
                        wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
                        wf.setframerate(SAMPLE_RATE)
                        wf.writeframes(b''.join(frames))
                        wf.close()

                        partial = transcribe_audio(partial_path)
                        if partial and "⚠️" not in partial and partial != full_text:
                            new_text = partial[len(full_text):].strip()
                            if new_text:
                                sys.stdout.write(f"     📝 {new_text}")
                                sys.stdout.flush()
                                full_text = partial

                    if silence_frames > max_silence and len(frames) > int(1.5 / (PORCUPINE_CHUNK / SAMPLE_RATE)):
                        print()
                        break

                if full_text.strip():
                    print(f"     📝 Final: {full_text}")
                    print("     🤔 Thinking...")
                    response = think(full_text)
                    speak(response)

                print("  ✅ Listening for wake word...\n")

    except KeyboardInterrupt:
        print("\n  👋")
    finally:
        porcupine.delete()
        stream.stop_stream()
        stream.close()
        p.terminate()


def energy_wake_loop():
    """Fallback: energy-threshold based wake word detection."""
    print("  🦻 Energy Wake Mode (fallback)")
    try:
        import pyaudio
    except ImportError:
        print("  ❌ pyaudio required")
        return

    p = pyaudio.PyAudio()
    stream = p.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=int(SAMPLE_RATE * CHUNK_DURATION),
    )

    energy_history = deque(maxlen=15)
    listening = False

    try:
        while True:
            data = stream.read(int(SAMPLE_RATE * CHUNK_DURATION))
            energy = sum(abs(x) for x in struct.unpack(f"{len(data)//2}h", data)) / (len(data)//2)
            energy_history.append(energy)
            avg = sum(energy_history) / len(energy_history) if energy_history else 0

            if avg > ENERGY_THRESHOLD and not listening:
                listening = True
            if listening and avg < ENERGY_THRESHOLD * 0.5:
                listening = False
                print("\n  🎤 Detected speech...")

                frames = [data]
                for _ in range(int(4 / CHUNK_DURATION)):
                    frames.append(stream.read(int(SAMPLE_RATE * CHUNK_DURATION)))

                AUDIO_DIR.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                wav_path = str(AUDIO_DIR / f"wake_{ts}.wav")

                wf = wave.open(wav_path, 'wb')
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(b''.join(frames))
                wf.close()

                text = transcribe_audio(wav_path)
                print(f"     📝 {text}")
                if text.strip() and "⚠️" not in text:
                    response = think(text)
                    speak(response)

            time.sleep(0.01)
    except KeyboardInterrupt:
        print("\n  👋")


# ---- Audio I/O ----

def check_deps():
    deps = []
    try:
        import pyaudio
        deps.append("pyaudio ✅")
    except ImportError:
        deps.append("pyaudio ❌")
    try:
        import pvporcupine
        deps.append("pvporcupine ✅")
    except ImportError:
        deps.append("pvporcupine ❌ (optional)")
    r = subprocess.run(["which", "mlx_whisper"], capture_output=True)
    deps.append(f"mlx_whisper {'✅' if r.returncode == 0 else '❌'}")
    for tool in ["say", "edge-tts"]:
        r = subprocess.run(["which", tool], capture_output=True)
        if r.returncode == 0:
            deps.append(f"{tool} ✅")
            break
    else:
        deps.append("TTS ❌")
    return deps


def record_audio(duration=5.0, output_path=None):
    if output_path is None:
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(AUDIO_DIR / f"rec_{ts}.wav")
    try:
        import pyaudio
    except ImportError:
        subprocess.run(f"sox -d -r {SAMPLE_RATE} -c {CHANNELS} {output_path} trim 0 {duration}", shell=True, timeout=int(duration)+5)
        return output_path

    p = pyaudio.PyAudio()
    stream = p.open(format=pyaudio.paInt16, channels=CHANNELS, rate=SAMPLE_RATE,
                    input=True, frames_per_buffer=int(SAMPLE_RATE*CHUNK_DURATION))
    frames = []
    for _ in range(int(duration/CHUNK_DURATION)):
        try:
            frames.append(stream.read(int(SAMPLE_RATE*CHUNK_DURATION)))
        except Exception:
            break
    stream.stop_stream(); stream.close(); p.terminate()
    wf = wave.open(output_path, 'wb')
    wf.setnchannels(CHANNELS); wf.setsampwidth(p.get_sample_size(pyaudio.paInt16)); wf.setframerate(SAMPLE_RATE)
    wf.writeframes(b''.join(frames)); wf.close()
    return output_path


def transcribe_audio(audio_path):
    # MLX Whisper (turbo model, fastest)
    try:
        result = subprocess.run(
            ["mlx_whisper", audio_path, "--model", "mlx-community/whisper-large-v3-turbo"],
            capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    # Fallback: openai whisper
    try:
        result = subprocess.run(
            ["whisper", audio_path, "--model", "tiny", "--language", "zh", "--output_format", "txt"],
            capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return "⚠️ STT unavailable"


def think(prompt):
    """LLM: oMLX local first, FinnA cloud fallback."""
    try:
        import requests
    except ImportError:
        return "⚠️ requests not available"

    # Try local oMLX first
    try:
        resp = requests.post(
            f"{OMLX_BASE}/chat/completions",
            headers={"Authorization": "Bearer local", "Content-Type": "application/json"},
            json={
                "model": "qwen3.5-4b-mlx",
                "messages": [
                    {"role": "system", "content": "你是 Hermes，老于的 AI 助手。用中文回复，简短直接，不超过3句。"},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 200, "temperature": 0.7, "stream": False,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
    except Exception:
        pass

    # Fallback to FinnA
    try:
        resp = requests.post(
            f"{FINNA_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {FINNA_KEY}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-v4-flash",
                "messages": [
                    {"role": "system", "content": "你是 Hermes，老于的 AI 助手。回答简短直接，用中文，不超过3句。"},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 200, "stream": False,
                "extra_body": {"enable_thinking": False},
            },
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"⚠️ {e}"

    return "⚠️ LLM unavailable"


def speak(text):
    print(f"🔊 {text}")
    if sys.platform == "darwin":
        try:
            subprocess.run(["say", "-v", "Tingting", text], timeout=30)
            return
        except Exception:
            pass
    try:
        subprocess.run(["edge-tts", "--voice", "zh-CN-XiaoxiaoNeural",
                        "--text", text, "--write-media", "/tmp/hermes_tts.mp3"], timeout=30)
        player = "afplay" if sys.platform == "darwin" else "mpv"
        subprocess.run([player, "/tmp/hermes_tts.mp3"], timeout=30)
    except Exception:
        pass


def ptt_mode():
    print("🎤 Push-to-Talk Mode - press Enter to record, Ctrl+C to exit\n")
    for d in check_deps():
        print(f"   {d}")
    try:
        while True:
            input("\n   Press Enter to speak...")
            wav_path = record_audio(5.0)
            text = transcribe_audio(wav_path)
            print(f"   📝 {text}")
            if text.strip() and "⚠️" not in text:
                response = think(text)
                speak(response)
    except KeyboardInterrupt:
        print("\n👋")


def cmd_test_stt():
    print("🧪 Testing STT...")
    wav_path = record_audio(3.0, str(AUDIO_DIR/"test.wav"))
    print(f"   Result: {transcribe_audio(wav_path)}")


def cmd_test_tts():
    speak("你好，我是 Hermes。语音测试成功。")


def main():
    VOICE_DIR.mkdir(parents=True, exist_ok=True)

    if len(sys.argv) < 2:
        print("Hermes Native Voice")
        print("\nModes:")
        print("  wake          Porcupine wake word")
        print("  wake-stream   Wake word + streaming STT")
        print("  ptt           Push-to-talk")
        print("  test-stt      Test STT")
        print("  test-tts      Test TTS")
        return

    cmd = sys.argv[1]
    if cmd == "wake":
        porcupine_wake_loop()
    elif cmd == "wake-stream":
        porcupine_stream_loop()
    elif cmd == "ptt":
        ptt_mode()
    elif cmd == "test-stt":
        cmd_test_stt()
    elif cmd == "test-tts":
        cmd_test_tts()
    else:
        print(f"Unknown: {cmd}")


if __name__ == "__main__":
    main()