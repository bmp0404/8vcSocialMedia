"""X's weighted character count, the number X compares with 280.

The rules are twitter-text's config v3 (github.com/twitter/twitter-text, config/v3.json): code points
in four ranges count 1, every other code point counts 2, a URL counts 23 whatever its length, and
an emoji counts 2 however many code points it spans. Text is NFC-normalized first, as twitter-text
does. Ported from Aarav's weighted_length (aarav/self-improving, harness/actions/core.py:25), which
counted emoji per code point, so a skin tone or a joined family counted 4 to 10.

Not modelled: twitter-text also counts a bare domain such as example.com as a URL. Only
http(s):// links count 23 here.
"""

import re
import unicodedata

MAX_WEIGHTED_CHARS = 280
URL_WEIGHT = 23
LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
LINK = re.compile(r"https?://\S+")
# twitter-text leaves trailing punctuation out of a link, so "see https://x.com/a." ends with a period.
TRAILING = ".,;:!?)]}'\""
ZWJ, VS15, VS16, KEYCAP = 0x200D, 0xFE0E, 0xFE0F, 0x20E3


def _light(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in LIGHT_RANGES)


def _emoji(cp: int) -> bool:
    # Outside the light ranges an emoji and a lone symbol both count 2; the difference is only in sequences.
    return 0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or cp in (0x2B50, 0x2B55, 0x2934, 0x2935, 0x3030,
                                                                        0x303D, 0x3297, 0x3299)


def _modifier(cp: int) -> bool:
    return cp in (VS15, VS16, KEYCAP) or 0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F  # tags (flags)


def _emoji_end(cps: list[int], i: int) -> int:
    """Index just past the emoji sequence starting at i, or i when no emoji starts there."""
    cp = cps[i]
    if 0x1F1E6 <= cp <= 0x1F1FF:  # a flag is two regional indicators
        return i + 2 if i + 1 < len(cps) and 0x1F1E6 <= cps[i + 1] <= 0x1F1FF else i + 1
    # A keycap (1️⃣) or ©️/®️ is a light character that becomes an emoji only with VS16 or the keycap mark.
    if chr(cp) in "0123456789#*\u00a9\u00ae" and i + 1 < len(cps) and cps[i + 1] in (VS16, KEYCAP):
        j = i + 1
    elif _emoji(cp):
        j = i + 1
    else:
        return i
    while j < len(cps):
        if _modifier(cps[j]):
            j += 1
        elif cps[j] == ZWJ and j + 1 < len(cps) and _emoji(cps[j + 1]):
            j += 2
        else:
            break
    return j


def _count(text: str) -> int:
    cps, n, i = [ord(c) for c in text], 0, 0
    while i < len(cps):
        end = _emoji_end(cps, i)
        if end > i:
            n, i = n + 2, end
        else:
            n, i = n + (1 if _light(cps[i]) else 2), i + 1
    return n


def overflow_index(text: str, cap: int) -> int:
    """The first character index whose prefix weighs more than `cap`, or len(text) when none does."""
    return next((i for i in range(len(text)) if weighted_length(text[: i + 1]) > cap), len(text))


def weighted_length(text: str) -> int:
    text = unicodedata.normalize("NFC", text)
    n, last = 0, 0
    for m in LINK.finditer(text):
        link = m.group(0).rstrip(TRAILING)
        n += _count(text[last:m.start()]) + URL_WEIGHT
        last = m.start() + len(link)
    return n + _count(text[last:])
