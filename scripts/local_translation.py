"""Serve the demo's optional local language detection and translation models."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path

import ctranslate2
import fasttext
import sentencepiece


LANGUAGES = {
    "ru": "rus_Cyrl", "be": "bel_Cyrl", "az": "azj_Latn", "kk": "kaz_Cyrl",
    "ky": "kir_Cyrl", "tk": "tuk_Latn", "uz": "uzn_Latn", "hy": "hye_Armn",
    "tg": "tgk_Cyrl", "hi": "hin_Deva",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--detector", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18191)
    args = parser.parse_args()
    detector = fasttext.load_model(str(args.detector))
    tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(args.model / "sentencepiece.bpe.model"))
    translator = ctranslate2.Translator(str(args.model), compute_type="int8", intra_threads=4)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond(200 if self.path == "/health" else 404, {"status": "ready"})

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 180_000:
                    raise ValueError("invalid request size")
                body = json.loads(self.rfile.read(size))
                if self.path == "/detect":
                    labels, scores = detector.predict(" ".join(body["text"].split()), k=5)
                    matches = {label.removeprefix("__label__"): float(score)
                               for label, score in zip(labels, scores)}
                    self.respond(200, {"languages": matches})
                elif self.path == "/translate":
                    source, target = (LANGUAGES[body[key]] for key in ("source", "target"))
                    rows = body["texts"]
                    if not isinstance(rows, list) or not rows or not all(isinstance(row, str) and row.strip() for row in rows):
                        raise ValueError("invalid text list")
                    tokens = [[source, *tokenizer.encode_as_pieces(text), "</s>"] for text in rows]
                    results = translator.translate_batch(tokens, target_prefix=[[target]] * len(tokens),
                                                         beam_size=2, max_input_length=0, max_batch_size=1024,
                                                         batch_type="tokens", no_repeat_ngram_size=6)
                    self.respond(200, {"texts": [tokenizer.decode_pieces(row.hypotheses[0][1:]) for row in results]})
                else:
                    self.respond(404, {"error": "unknown route"})
            except (ValueError, KeyError, TypeError, AttributeError):
                self.respond(400, {"error": "invalid request"})
            except RuntimeError:
                self.respond(503, {"error": "local model failed"})

        def respond(self, status, body):
            result = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Local translation ready on http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
