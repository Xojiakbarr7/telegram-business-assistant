"""Language detection for the Telegram Business Assistant Bot."""

from __future__ import annotations

import re


# Uzbek Cyrillic specific characters
UZBEK_CYRILLIC_CHARS = set("ўқғҳ")

# Uzbek Cyrillic words (common)
UZBEK_CYRILLIC_WORDS = {
    "салом", "рахмат", "яхши", "нима", "қалесан", "йўқ", "бугун",
    "эртага", "керак", "ассалому", "аллайкум", "ваъалойкум", "хаёри",
    "как", "дела", "не", "делаешь", "сегодня", "завтра", "нужен",
    "помоги", "спасибо", "хорошо", "плохо", "да", "нет", "может",
}

# Uzbek Latin words (common)
UZBEK_LATIN_WORDS = {
    "salom", "rahmat", "yaxshi", "nima", "qalesan", "yoq", "bugun",
    "ertaga", "kerak", "assalomu", "aleykum", "vaaleykum", "hayri",
    "nima", "gap", "yaxshimisiz", "qanday", "ishlar", "qalay",
    "xop", "bopti", "zor", "hozir", "keyin", "men", "sen", "siz",
    "misan", "misiz", "mizmi", "yapsan", "yapman", "yapti",
    "yapmiz", "asizmi", "ganman", "dingmi", "gapirmayman",
    "qayerda", "nimadir", "ha", "yoq", "bilmadim", "topdim",
}

# Uzbek suffixes that indicate Uzbek language
UZBEK_SUFFIXES = {
    "misan", "misiz", "mizmi", "yapsan", "yapman", "yapti",
    "yapmiz", "asizmi", "ganman", "dingmi",
}

# Apostrophe variants
APOSTROPHES = {"'", "ʻ", "’", "‘", "`"}

# Language hints for tests
LANGUAGE_HINTS = {
    "ru": {
        "patterns": [r"[Ѐ-ӿ]"],
        "words": {"привет", "здравствуйте", "спасибо", "хорошо", "как", "дела"},
    },
    "en": {
        "patterns": [r"[A-Za-z]"],
        "words": {"hello", "hi", "thanks", "good", "how", "are", "you"},
    },
    "uz": {
        "patterns": [r"[ўқғҳ]", r"[a-z]'[a-z]"],
        "words": {"salom", "rahmat", "yaxshi", "qalesan", "bugun"},
    },
}


def _has_cyrillic(text: str) -> bool:
    """Check if text contains Cyrillic characters."""
    return bool(re.search(r"[Ѐ-ӿ]", text))


def _has_latin(text: str) -> bool:
    """Check if text contains Latin characters."""
    return bool(re.search(r"[A-Za-z]", text))


def _count_cyrillic_vs_latin(text: str) -> tuple[int, int]:
    """Count Cyrillic vs Latin letters."""
    cyrillic = len(re.findall(r"[Ѐ-ӿ]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return cyrillic, latin


def _contains_uzbek_cyrillic(text: str) -> bool:
    """Check if text contains Uzbek-specific Cyrillic characters or words."""
    text_lower = text.lower()
    # Check for specific Uzbek Cyrillic characters
    if any(c in text_lower for c in UZBEK_CYRILLIC_CHARS):
        return True
    # Check for Uzbek Cyrillic words
    words = re.findall(r"[Ѐ-ӿ]+", text_lower)
    return any(word in UZBEK_CYRILLIC_WORDS for word in words)


def _contains_uzbek_latin(text: str) -> bool:
    """Check if text contains Uzbek-specific Latin patterns."""
    text_lower = text.lower()
    # Check for apostrophe in words (o'g' etc.)
    words = re.findall(r"[a-zA-Z'ʻ’‘`]+", text_lower)
    for word in words:
        # Remove apostrophes and check
        clean = word.translate(str.maketrans("", "", "ʻ’‘`"))
        if clean in UZBEK_LATIN_WORDS:
            return True
        # Check for Uzbek suffixes
        for suffix in UZBEK_SUFFIXES:
            if clean.endswith(suffix):
                return True
    return False


def _has_apostrophe_words(text: str) -> bool:
    """Check if text has words with apostrophes that might be Uzbek."""
    words = re.findall(r"[a-zA-Z][a-zA-Z'ʻ’‘`]*[a-zA-Z]", text.lower())
    for word in words:
        if any(apo in word for apo in APOSTROPHES):
            clean = word.translate(str.maketrans("", "", "ʻ’‘`"))
            if clean in UZBEK_LATIN_WORDS:
                return True
            for suffix in UZBEK_SUFFIXES:
                if clean.endswith(suffix):
                    return True
    return False


def detect_language(text: str) -> str | None:
    """
    Detect language of text. Returns 'uz', 'ru', 'en', or None.

    Rules:
    - If no letters at all (only emoji, numbers, punctuation): return None
    - If Cyrillic dominant:
        - If Uzbek-specific chars (ўқғҳ) or Uzbek Cyrillic words found: 'uz'
        - Else: 'ru'
    - If Latin dominant:
        - If apostrophe in words + Uzbek words/suffixes: 'uz'
        - If Uzbek words found: 'uz'
        - Else: 'en'
    """
    if not text or not text.strip():
        return None

    # Check if there are any letters at all
    if not _has_cyrillic(text) and not _has_latin(text):
        return None

    cyrillic_count, latin_count = _count_cyrillic_vs_latin(text)

    if cyrillic_count > latin_count:
        # Cyrillic dominant
        if _contains_uzbek_cyrillic(text):
            return "uz"
        return "ru"
    elif latin_count > cyrillic_count:
        # Latin dominant
        if _has_apostrophe_words(text) or _contains_uzbek_latin(text):
            return "uz"
        return "en"
    else:
        # Equal or no clear dominance - check for specific markers
        if _contains_uzbek_cyrillic(text) or _has_apostrophe_words(text) or _contains_uzbek_latin(text):
            return "uz"
        if _has_cyrillic(text):
            return "ru"
        return "en"