#!/bin/sh
set -eu
umask 077
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/colab-accounts/credentials" "$HOME/.ssh" "$WORKBENCH_DATA"
chmod 700 "$HOME/.ssh"
if [ ! -f "$WORKBENCH_SSH_IDENTITY" ]; then
    ssh-keygen -q -t ed25519 -N '' -C workbench-container -f "$WORKBENCH_SSH_IDENTITY"
fi
for credential in "$HOME"/.local/share/colab-accounts/credentials/*.json; do
    [ -f "$credential" ] || continue
    account="$(basename "$credential" .json)"
    case "$account" in ''|*[!0-9]*|0|0*) continue ;; esac
    ln -sf /app/container/colab-account.py "$HOME/.local/bin/colab$account"
done
exec "$@"
