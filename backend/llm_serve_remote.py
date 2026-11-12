"""Repository-owned Strata launcher; the verified cloud archive stays unchanged."""
import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path[:0] = [str(root / 'runtime'), str(root / 'runtime/tools')]
    import strata_tokenizer as ST
    from serve import server as server
    from serve.frontend import ChatTemplate

    tokenizer_root = root / 'pack/tokenizer'
    vocab = json.loads((tokenizer_root / 'vocab.json').read_text())
    tokens = [None] * len(vocab)
    for token, index in vocab.items():
        tokens[index] = token
    tokenizer_config = json.loads((tokenizer_root / 'tokenizer.json').read_text())
    tokenizer = ST.Tokenizer(
        tokens, (tokenizer_root / 'merges.txt').read_text().split('\n'),
        json.loads((tokenizer_root / 'token_type.json').read_text()),
        tokenizer_config.get('pre') or 'qwen35', tokenizer_config.get('special_ids') or None,
    )
    profile = json.loads((root / 'preset.json').read_text())
    flags = []
    for name, value in profile['engine_options'].items():
        flags.append('--' + name)
        if value is not True:
            flags.append(str(value).replace('${ROOT}', str(root)))
    environment = {**os.environ, **profile['environment']}
    environment['LD_LIBRARY_PATH'] = str(root / 'engine') + ':' + environment.get('LD_LIBRARY_PATH', '')
    (root / 'logs').mkdir(exist_ok=True)
    engine = server.StrataEngine(str(root / 'engine/strata'), flags, cwd=str(root),
                                 log=str(root / 'logs/engine.log'), env=environment)
    service = server.Service(engine, tokenizer, ChatTemplate(root / 'chat_template.jinja'),
                             model_name=profile['served_model'], fit_max_tokens=False)
    service.model_arch = 'qwen4exp'
    service.answer_room = False
    service.tool_call_recovery = True
    service.slot_save_path = server.slot_save_dir(str(root / 'sessions'))
    httpd = server.serve(service, '127.0.0.1', args.port)
    print(json.dumps({'ready': True, 'model': profile['served_model'], 'engine': engine.info}), flush=True)

    def stop(*_args):
        # HTTPServer.shutdown must be called from a thread other than serve_forever.
        httpd.shutdown()
        engine.close()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        time.sleep(1)


if __name__ == '__main__':
    main()
