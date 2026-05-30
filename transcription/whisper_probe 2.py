"""
Whisper-compatibility probe.

This script is run as a *subprocess* by `whisper_engine` at startup. Its
only job is to attempt loading the Whisper model. If it succeeds the
parent knows transcription is safe. If it segfaults / hits an exit /
times out, the parent disables transcription cleanly — without bringing
the whole Personal AI OS down.

Exit codes:
   0  : Whisper loaded successfully
   1  : Python-level exception during load (printed to stderr)
   *  : OS-level crash (segfault) — interpreted as "unavailable"
"""
from __future__ import annotations

import os
import sys
import traceback


def main() -> int:
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    model_name = sys.argv[1] if len(sys.argv) > 1 else "base"
    device = sys.argv[2] if len(sys.argv) > 2 else "cpu"
    compute_type = sys.argv[3] if len(sys.argv) > 3 else "int8"
    try:
        from faster_whisper import WhisperModel
        # Constructing the model is what actually loads ctranslate2 + weights.
        WhisperModel(model_name, device=device, compute_type=compute_type)
    except BaseException:
        traceback.print_exc(file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
