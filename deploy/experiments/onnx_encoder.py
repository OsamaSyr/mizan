#!/usr/bin/env python3
"""
Prototype: bge-m3 through ONNX Runtime on CPU, as a drop-in for semantic.Encoder.

Experiment only — src/mizan/semantic.py is not modified. `export()` writes the
CLS-pooled encoder to ONNX (external-data format; the fp32 file is ~2.2 GB),
`quantize()` makes an int8 dynamic-quantized copy, and `OrtEncoder` exposes the
same `encode(texts, max_length, batch_size)` contract as semantic.Encoder so it
can be installed as `semantic._ENCODER` and exercised through the real code.
"""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from mizan import semantic as S  # noqa: E402


def export(out_path: str, opset: int = 17) -> float:
    import torch
    from transformers import AutoModel

    class ClsEncoder(torch.nn.Module):
        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, input_ids, attention_mask):
            return self.m(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state[:, 0]

    t0 = time.perf_counter()
    model = AutoModel.from_pretrained(S._model_dir(S.MODEL_ID, S.MODEL_REVISION),
                                      local_files_only=True).eval()
    ids = torch.ones(2, 16, dtype=torch.long)
    mask = torch.ones(2, 16, dtype=torch.long)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with torch.inference_mode():
        torch.onnx.export(ClsEncoder(model), (ids, mask), out_path, dynamo=False,
                          input_names=["input_ids", "attention_mask"], output_names=["cls"],
                          dynamic_axes={"input_ids": {0: "b", 1: "t"},
                                        "attention_mask": {0: "b", 1: "t"}, "cls": {0: "b"}},
                          opset_version=opset)
    return time.perf_counter() - t0


def quantize(src: str, dst: str) -> float:
    from onnxruntime.quantization import QuantType, quantize_dynamic
    t0 = time.perf_counter()
    quantize_dynamic(src, dst, weight_type=QuantType.QInt8, use_external_data_format=True)
    return time.perf_counter() - t0


class OrtEncoder:
    def __init__(self, path: str, threads: int | None = None):
        import onnxruntime as ort
        from transformers import AutoTokenizer
        t0 = time.perf_counter()
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if threads:
            so.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
        self.tok = AutoTokenizer.from_pretrained(S._model_dir(S.MODEL_ID, S.MODEL_REVISION),
                                                 local_files_only=True)
        self.device = "cpu"
        self.load_seconds = time.perf_counter() - t0
        self._lock = threading.Lock()

    def encode(self, texts, max_length=S.QUERY_MAX_TOKENS, batch_size=32):
        import numpy as np
        n = len(texts)
        out = np.zeros((n, S.EMBED_DIM), dtype=np.float32)
        if n == 0:
            return out
        order = sorted(range(n), key=lambda i: len(texts[i]))
        with self._lock:
            for b in range(0, n, batch_size):
                idx = order[b:b + batch_size]
                enc = self.tok([texts[i] or " " for i in idx], padding=True, truncation=True,
                               max_length=max_length, return_tensors="np")
                cls = self.sess.run(["cls"], {"input_ids": enc["input_ids"].astype(np.int64),
                                              "attention_mask": enc["attention_mask"].astype(np.int64)})[0]
                cls = cls / np.linalg.norm(cls, axis=1, keepdims=True)
                out[idx] = cls
        return out
