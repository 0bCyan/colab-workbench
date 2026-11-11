"""Decode the real smoke-test video and audio; writes a local verification report.

Run with: uv run --with av --with pillow --with numpy python -m scripts.verify_video
"""
import json

import av
from PIL import Image

from backend.config import DATA, ROOT


def main():
    result = json.loads((DATA / "real-smoke-result.json").read_text())
    job = result["job"]
    path = DATA / "outputs" / job["id"] / job["outputs"][0]["filename"]
    frames, count = [], 0
    with av.open(str(path)) as container:
        for frame in container.decode(video=0):
            if count in (0, 40, 80, 123):
                frames.append(frame.to_image())
            count += 1
    samples, nonzero, rate, channels = 0, False, None, None
    with av.open(str(path)) as container:
        for frame in container.decode(audio=0):
            samples += frame.samples
            nonzero = nonzero or bool(frame.to_ndarray().any())
            rate = frame.sample_rate
            channels = len(frame.layout.channels)
    info = {
        "video": str(path), "frames": count, "width": frames[0].width, "height": frames[0].height,
        "audio_samples": samples, "audio_sample_rate": rate, "audio_channels": channels,
        "nonzero_audio": nonzero, "generation_seconds": job["ended_at"] - job["started_at"],
        "setup_seconds": job["started_at"] - job["created_at"], "total_seconds": result["total_seconds"],
        "bytes": path.stat().st_size, "machine_phase_after": result["machine"]["phase"],
    }
    (ROOT / "validation-result.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
    canvas = Image.new("RGB", (1344, 768), "#18211b")
    for index, frame in enumerate(frames):
        frame.thumbnail((672, 384))
        canvas.paste(frame, ((index % 2) * 672, (index // 2) * 384))
    (ROOT / "screenshots").mkdir(exist_ok=True)
    canvas.save(ROOT / "screenshots/real-generation-frames.jpg", quality=90)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    assert count == 124 and nonzero and channels == 2


if __name__ == "__main__":
    main()
