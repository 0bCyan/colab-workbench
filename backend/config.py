import os
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("WORKBENCH_DATA", ROOT / ".data")).expanduser().resolve()
COMFY_COMMIT = "4d64a8bc072b81c9b47321789b4d83fb7348ae2a"
MODEL_REVISION = "54472221d27c695fca9621b0c3ed52f06c4af707"
REMOTE_ROOT = "/content/colab-workbench"
MODEL_FILES = [
    "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "vae/minimax_h3_video_vae_int8_convrot.safetensors",
    "vae/minimax_h3_audio_vae_fp32.safetensors",
]


def profiles(remembered=()):
    """Discover launchers without opening or copying credential contents."""
    numbers = {int(match[1]) for name in remembered if (match := re.fullmatch(r"colab([1-9][0-9]*)", name))}
    credentials = Path.home() / ".local/share/colab-accounts/credentials"
    roots = dict.fromkeys([Path.home() / ".local/bin", *(Path(path) for path in os.get_exec_path() if path)])
    searches = [(root, r"colab([1-9][0-9]*)") for root in roots]
    searches.append((credentials, r"([1-9][0-9]*)\.json"))
    for root, pattern in searches:
        try:
            for entry in root.iterdir():
                match = re.fullmatch(pattern, entry.name)
                if match and entry.is_file():
                    numbers.add(int(match[1]))
        except OSError:
            continue
    result = []
    for number in sorted(numbers):
        name = f"colab{number}"
        launcher = shutil.which(name) or str(Path.home() / ".local/bin" / name)
        result.append({
            "id": name, "label": f"账号 {number}", "launcher": launcher,
            "installed": Path(launcher).is_file() and os.access(launcher, os.X_OK),
            "credential_present": (credentials / f"{number}.json").is_file(),
        })
    return result


def default_identity():
    override = os.environ.get("WORKBENCH_SSH_IDENTITY")
    if override:
        return str(Path(override).expanduser())
    for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        path = Path.home() / ".ssh" / name
        if path.is_file():
            return str(path)
    return ""
