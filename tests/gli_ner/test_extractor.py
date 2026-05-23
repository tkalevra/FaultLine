import pytest
from unittest.mock import MagicMock


@pytest.fixture
def mock_model():
    model = MagicMock()
    model.predict.return_value = [
        {"entity": "system", "label": "Entity"},
        {"entity": "failed", "label": "Event"},
    ]
    return model


def test_extract_entities_success(mock_model):
    from src.gli_ner import extractor

    result = extractor.extract("The system failed.", model=mock_model)

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["entity"] == "system"
    assert result[0]["label"] == "Entity"
    assert result[1]["entity"] == "failed"


def test_extract_entities_invalid_model():
    from src.gli_ner import extractor

    with pytest.raises(ValueError):
        extractor.extract("Test text.", model=None)
