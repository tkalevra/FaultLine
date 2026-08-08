class GLiNERAdapter:
    """Adapts the real GLiNER model to the predict(text, top_k) interface used by ExtractionService."""

    DEFAULT_LABELS = ["Person", "Organization", "Location", "Event", "Product", "Concept"]

    def __init__(self, model, labels=None):
        self.model = model
        self.labels = labels or self.DEFAULT_LABELS

    def predict(self, text: str, top_k: int = 5) -> list[dict]:
        entities = self.model.predict_entities(text, self.labels, threshold=0.5)
        return [
            {"entity": e["text"], "label": e["label"], "score": e.get("score", 1.0)}
            for e in entities[:top_k]
        ]


def load_default_model(labels=None) -> "GLiNERAdapter":
    """Load the real GLiNER model from HuggingFace and wrap it in GLiNERAdapter.

    [es branch] MULTILINGUAL default. GLiNER purity (Pitfall 11) is unchanged — the labels
    stay concise zero-shot type names; only the underlying weights become multilingual so
    Spanish (and any language) entity typing works. Env-overridable via GLINER_MODEL.

    ⚠️ DEAD CODE — READ THIS BEFORE TRUSTING IT. The audit this docstring used to ask for has
    now been done, and the answer was: **this function has NO CALLERS anywhere in the repo.**
    Its multilingual default was therefore never reached, and every entity typed by the running
    system went through English-only weights while this docstring claimed otherwise.

    The load site that ACTUALLY runs is the FastAPI startup hook in ``src/api/main.py``
    (``GLiNER2.from_pretrained``, env var ``GLINER_MODEL``, baked in the Dockerfile because the
    runtime image sets ``HF_HUB_OFFLINE=1``). It is a different library too: ``pyproject.toml``
    declares ``gliner2``, not ``gliner``, so the import below would raise ImportError if this
    were ever called. Change the model THERE; changing it here changes nothing."""
    import os
    from gliner import GLiNER
    _model = os.environ.get("GLINER_MODEL", "urchade/gliner_multi-v2.1")
    base = GLiNER.from_pretrained(_model)
    return GLiNERAdapter(base, labels=labels)


class ExtractionService:
    def __init__(self, model):
        if model is None:
            raise ValueError("model cannot be None — provide a GliNER model instance")
        self.model = model

    def extract(self, texts: list[str], top_n: int = 5) -> list[dict]:
        results = []
        for text in texts:
            predictions = self.model.predict(text, top_k=top_n)
            for pred in predictions:
                results.append({
                    "entity": pred.get("entity"),
                    "label": pred.get("label"),
                    "score": pred.get("score", 0.0),
                    "text": text,
                })
        return results


def extract(texts, model=None, top_n: int = 5) -> list[dict]:
    if model is None:
        raise ValueError("model cannot be None")
    if isinstance(texts, str):
        texts = [texts]
    service = ExtractionService(model=model)
    return service.extract(texts, top_n=top_n)
