"""Generate abstract test footage without using any user media or cloud service.

Optional regeneration: uv run --with av --with numpy python -m scripts.generate_test_video
"""
from pathlib import Path

import av
import numpy as np


def main():
    path = Path(__file__).resolve().parents[1] / 'tests/fixtures/synthetic.mp4'
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)
    texture = rng.integers(0, 256, (180, 320, 3), dtype=np.uint8)
    with av.open(str(path), 'w', options={'movflags': '+faststart'}) as container:
        stream = container.add_stream('libx264', rate=24)
        stream.width, stream.height, stream.pix_fmt = 320, 180, 'yuv420p'
        stream.options = {'crf': '18', 'preset': 'fast'}
        for number in range(48):
            pixels = np.roll(texture, number * 7, axis=1)
            frame = av.VideoFrame.from_ndarray(pixels, format='rgb24')
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    print(f'Synthetic video: {path.name}, {path.stat().st_size} bytes')


if __name__ == '__main__':
    main()
