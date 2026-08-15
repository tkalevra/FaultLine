"""English deriver helper for the Spanish parity tests (runs under en_core_web_sm)."""
import json, os, sys
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, _REPO)

# silence structlog so the JSON is the ONLY stdout line
import logging
logging.disable(logging.CRITICAL)
try:
    import structlog
    # route ALL structlog output to a null handler so the JSON is the only stdout line
    import logging
    class _NullHandler(logging.Handler):
        def emit(self, record):
            pass
    logging.getLogger().addHandler(_NullHandler())
    logging.getLogger().setLevel(logging.CRITICAL + 1)
except Exception:
    pass

import datetime
from src.extraction import linguistics as m

sentence = sys.argv[1]
facts = m.derive_sentence_facts(sentence, datetime.date(2023, 6, 1), None)
out = [
    {
        'subject': getattr(f, 'subject', ''),
        'rel_type': getattr(f, 'rel_type', ''),
        'object': getattr(f, 'object', ''),
    }
    for f in (facts or [])
]
# write the JSON to a result file (stdout carries structlog noise), then print the path
import tempfile
_f = os.path.join(tempfile.gettempdir(), f"es_parity_{os.getpid()}.json")
with open(_f, "w") as _fh:
    _fh.write(json.dumps(out))
print(_f)