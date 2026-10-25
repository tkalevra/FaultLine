"""Machine-value SHAPE oracle (round-7 RED, live-proven on the nvidia gate).

THE WHY: the P70 restate (my P70 laptop has wifi mac aa:bb:cc:11:22:33) parsed
mac as a PROPN appositive on wifi, so the name-type binding detector read
the FIELD LABEL (wifi mac) as a type-with-a-name and the naming chain minted
(mac, instance_of, wifi) + (wifi, also_known_as, mac) -- two junk Person entities --
while the machine VALUE they label sat right next to them. A token whose surface is
a MACHINE VALUE (MAC address / IPv4+CIDR / email / URL) is a VALUE, never a name
and never the name of a naming bind: THE HARD LINE value side, recognized by its
own punctuation SHAPE -- deterministic, subject-agnostic, NO word list.

The classes here are the same ones the per-tenant scalar_atomic extraction patterns
declare (compound.py / _detect_atomic_values); this module is the in-code floor the
detectors can consult WITHOUT a DB cursor (the deriver runs pattern-free by design).
Fail-open polarity: an unmatched surface is simply NOT a value shape -- callers use
this to REFUSE name/Person treatment, never to refuse capture.

Deterministic, offline, zero LLM.
"""
import re

_MAC_RE = re.compile(
    r"[0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){2,7}")
_IPV4_RE = re.compile(
    r"(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?")
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(
    r"https?://\S+", re.IGNORECASE)

_VALUE_RES = (_MAC_RE, _IPV4_RE, _EMAIL_RE, _URL_RE)


def is_value_shape(text) -> bool:
    """True iff text (a token surface or a span) IS a machine value by SHAPE:
    MAC address, IPv4/CIDR, email, or URL. Bare numbers are deliberately NOT a
    value shape (a cardinal is an age/count, not an identifier -- "my son Alex, 19"
    must keep binding). Deterministic; never raises."""
    if not text:
        return False
    t = str(text).strip()
    if not t:
        return False
    for rx in _VALUE_RES:
        m = rx.search(t)
        if m and m.group(0) == t:
            return True
    return False


def span_adjoins_value_shape(text, char_start, char_end, window=48) -> bool:
    """True iff the text IMMEDIATELY following the span [char_start, char_end)
    (skipping punctuation/whitespace, within window chars) opens with a machine
    value shape. This is the field-label oracle: a label span ("wifi mac",
    "hostname:") sits DIRECTLY before the value it names, with no verb between."""
    try:
        tail = str(text)[int(char_end):int(char_end) + int(window)]
    except Exception:  # noqa: BLE001 -- fail-open: not a value adjacency
        return False
    tail = tail.lstrip()
    if not tail:
        return False
    stripped = tail.lstrip(',:;-()[]| ')
    if not stripped:
        return False
    for rx in _VALUE_RES:
        if rx.match(stripped):
            return True
    return False