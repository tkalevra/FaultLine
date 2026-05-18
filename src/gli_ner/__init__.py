from .extractor import ExtractionService


class GliNERModel:
    """Stub GliNER model for unit testing."""
    def predict(self, text, top_k=5):
        return []


def extract_entities(text: str, model_class=None) -> list[dict]:
    """
    Extract entities using GliNER.
    CONTRACT: Returns list of dicts {'entity': str, 'label': str}.
    Raises ValueError if model_class is None.
    """
    if model_class is None:
        raise ValueError("Model class cannot be None")
    service = ExtractionService(model=model_class)
    raw = service.extract([text])
    return [{"entity": e["entity"], "label": e["label"]} for e in raw]
