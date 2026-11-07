"""scripts/download_models.py: every model the default setup downloads is pinned in
backend/models.sha256, and a file that does not match is reported or discarded. No network:
downloads here read file:// URLs in a temporary directory."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "download_models.py"


@pytest.fixture
def dm(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("download_models", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    good = b"weights"
    (tmp_path / "vad").mkdir()
    (tmp_path / "vad" / "silero_vad_v6.onnx").write_bytes(good)
    manifest = tmp_path / "models.sha256"
    manifest.write_text(f"{hashlib.sha256(good).hexdigest()}  vad/silero_vad_v6.onnx\n"
                        f"{hashlib.sha256(b'face').hexdigest()}  blaze_face_short_range.tflite\n")
    monkeypatch.setattr(mod, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(mod, "MANIFEST", manifest)
    return mod


def test_every_default_download_is_pinned_in_the_manifest():
    spec = importlib.util.spec_from_file_location("download_models", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    pinned = mod.expected()
    defaults = [*mod.MEDIAPIPE, "vad/silero_vad_v6.onnx", "whisper/base.en", *mod.KOKORO, f"llm/{mod.DEFAULT_LLM}", "whispercpp/ggml-base.en.bin"]
    for target in defaults:
        assert any(rel == target or rel.startswith(target + "/") for rel in pinned), target
    assert all(len(h) == 64 for h in pinned.values())
    assert set(mod.FASTER_WHISPER) == {"base.en"} and all(len(rev) == 40 for _, rev in mod.LLMS.values())
    assert all("/latest/" not in url for url in mod.MEDIAPIPE.values())


def test_check_reports_matching_changed_and_missing_files(dm, tmp_path):
    assert dm.check("vad/silero_vad_v6.onnx") == (1, [])
    (tmp_path / "vad" / "silero_vad_v6.onnx").write_bytes(b"weightz")
    assert dm.check("vad/silero_vad_v6.onnx") == (0, ["vad/silero_vad_v6.onnx: sha256 differs from models.sha256"])
    (tmp_path / "vad" / "silero_vad_v6.onnx").unlink()
    assert dm.check("vad/silero_vad_v6.onnx") == (0, ["vad/silero_vad_v6.onnx: missing"])
    assert dm.check("whisper/small.en") == (0, [])  # not pinned: reported as unverified, not as a failure


def test_a_download_with_the_wrong_bytes_is_discarded(dm, tmp_path):
    wrong = tmp_path / "wrong.bin"
    wrong.write_bytes(b"not the face model")
    with pytest.raises(RuntimeError, match="download discarded"):
        dm.download("blaze_face_short_range.tflite", wrong.as_uri(), force=True)
    assert not (tmp_path / "blaze_face_short_range.tflite").exists()
    assert not list(tmp_path.glob("*.part"))
    right = tmp_path / "right.bin"
    right.write_bytes(b"face")
    dm.download("blaze_face_short_range.tflite", right.as_uri(), force=True)
    assert (tmp_path / "blaze_face_short_range.tflite").read_bytes() == b"face"


def test_verify_exits_nonzero_when_a_selected_file_is_missing(dm, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["download_models.py", "--verify", "--no-kokoro", "--llm", "none", "--whisper", "none", "--whisper-cpp", "none"])
    assert dm.main() == 1  # hand_landmarker.task and the face model are not on disk
    err = capsys.readouterr().err
    assert "blaze_face_short_range.tflite: missing" in err and "silero" not in err
