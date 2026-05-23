import sys
import os
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from gli_ner import extract_entities


def test_project_structure():
    assert os.path.exists("src/gli_ner/__init__.py")
    assert os.path.exists("src/gli_ner/extractor.py")


def test_extract_gliner_valid():
    model_mock = MagicMock()
    model_mock.predict.return_value = [
        {"entity": "system", "label": "Entity"},
    ]
    result = extract_entities("The system failed.", model_mock)
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["entity"] == "system"
    assert result[0]["label"] == "Entity"


def test_extract_gliner_invalid_model():
    with pytest.raises(ValueError):
        extract_entities("text", None)


def test_extract_gliner_missing_text():
    model_mock = MagicMock()
    model_mock.predict.return_value = []
    result = extract_entities("", model_mock)
    assert isinstance(result, list)
