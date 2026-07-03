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

    [it branch] MULTILINGUAL default. GLiNER purity (Pitfall 11) is unchanged — the labels
    stay concise zero-shot type names; only the underlying weights become multilingual so
    Italian (and any language) entity typing works. Env-overridable via GLINER_MODEL. NOTE:
    audit for OTHER GLiNER load sites before relying on this (see DEV/DESIGN-italian-lang.md)."""
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
