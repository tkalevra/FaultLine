import json


def build_audit_context(entities_list, source_span):
    """
    CONTRACT: Bundles entity pairs with source sentence span.
    Returns dict: {'context': [...], 'metadata': {...}}
    """
    return {"context": entities_list, "metadata": {"source": source_span}}
