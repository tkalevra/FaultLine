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
