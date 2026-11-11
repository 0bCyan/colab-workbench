# Synthetic media

`synthetic.mp4` is a deterministic animation of a generated noise texture: 320 × 180,
24 fps, 48 frames. It contains no user artwork, prompts, account information, or
cloud output. The local Colab simulator serves this file to exercise downloads
and browser playback without credentials or paid machines.

To regenerate it, run from the repository root:

```sh
uv run --with av --with numpy python -m scripts.generate_test_video
```

The fixture and generator are covered by the repository's MIT license. PyAV and
NumPy are only needed to regenerate it, not to run the normal tests.
