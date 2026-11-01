"""Language registry: every ISO 639-3 language, with honest support tiers.

Any language with an ISO 639 code (about 7,900) can be selected. What changes per
language is how much the engine trusts automatic checks, which decides how much
human review is mandatory before output can be labelled Verified.

Tiers (see the architecture doc, section 9):
  A  high-resource: sample review
  B  medium: sample review, stricter
  C  Nigerian major languages (Hausa, Yoruba, Igbo): full native post-edit at first
  C2 covered by one major engine (Fulfulde, Kanuri, Tiv): full native review
  D  no dedicated engine (Pidgin and minority languages): native translator decides
  U  unmeasured: treated like D until a gold-set benchmark says otherwise
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

import langcodes
import pycountry

TIER_A = {
    "en", "fr", "de", "es", "pt", "it", "nl", "zh", "ja", "ko", "ru", "ar", "pl", "tr",
    "sv", "da", "nb", "no", "fi", "cs", "el", "he", "hu", "ro", "uk", "id", "vi", "th", "hi",
}
TIER_B = {"sw", "bn", "ur", "fa", "ta", "te", "mr", "ms", "tl", "fil", "af", "zu", "xh", "am", "bg", "hr", "sk", "sl", "sr", "lt", "lv", "et", "ca"}
TIER_C = {"yo", "ha", "ig"}
TIER_C2 = {"ff", "fuv", "kr", "knc", "tiv"}
TIER_D_NIGERIA = {"pcm", "efi", "ibb", "anw", "bin", "urh", "nup", "idu", "ijc", "ijo", "ann"}

RTL_SCRIPTS = {"Arab", "Hebr", "Thaa", "Syrc", "Nkoo", "Adlm", "Mand", "Samr"}

# Unicode character-name prefixes per ISO 15924 script, for the script gate.
SCRIPT_NAME_PREFIXES: dict[str, tuple[str, ...]] = {
    "Latn": ("LATIN",),
    "Cyrl": ("CYRILLIC",),
    "Grek": ("GREEK",),
    "Arab": ("ARABIC",),
    "Hebr": ("HEBREW",),
    "Deva": ("DEVANAGARI",),
    "Beng": ("BENGALI",),
    "Taml": ("TAMIL",),
    "Telu": ("TELUGU",),
    "Gujr": ("GUJARATI",),
    "Guru": ("GURMUKHI",),
    "Knda": ("KANNADA",),
    "Mlym": ("MALAYALAM",),
    "Thai": ("THAI",),
    "Laoo": ("LAO",),
    "Khmr": ("KHMER",),
    "Mymr": ("MYANMAR",),
    "Ethi": ("ETHIOPIC",),
    "Geor": ("GEORGIAN",),
    "Armn": ("ARMENIAN",),
    "Hang": ("HANGUL",),
    "Kore": ("HANGUL", "CJK"),
    "Hans": ("CJK",),
    "Hant": ("CJK",),
    "Jpan": ("CJK", "HIRAGANA", "KATAKANA"),
    "Sinh": ("SINHALA",),
    "Tibt": ("TIBETAN",),
    "Adlm": ("ADLAM",),
    "Nkoo": ("NKO",),
    "Tfng": ("TIFINAGH",),
}


@dataclass(frozen=True)
class DiacriticProfile:
    """Minimum share of words that should carry language-required marks."""

    marks: frozenset[str] = field(default_factory=frozenset)  # combining marks (NFD)
    letters: frozenset[str] = field(default_factory=frozenset)  # precomposed letters
    min_density: float = 0.0
    min_words: int = 10
    severity: str = "major"


DIACRITICS: dict[str, DiacriticProfile] = {
    # Yoruba: tone marks (acute, grave) and underdots (e, o, s with dot below).
    "yo": DiacriticProfile(
        marks=frozenset({"̀", "́", "̣"}),
        letters=frozenset("ẹọṣẸỌṢ"),
        min_density=0.15,
    ),
    # Igbo: dotted vowels and n with dot above; tone marks are optional in print.
    "ig": DiacriticProfile(
        marks=frozenset({"̣", "̇", "̀", "́"}),
        letters=frozenset("ịọụṅỊỌỤṄ"),
        min_density=0.05,
    ),
    # Hausa: hooked letters are standard in publishing but can be absent in short text.
    "ha": DiacriticProfile(
        letters=frozenset("ɓɗƙƴƁƊƘƳ"),
        min_density=0.01,
        min_words=150,
        severity="minor",
    ),
}


@dataclass(frozen=True)
class LanguageInfo:
    code: str
    name: str
    script: str | None
    rtl: bool
    tier: str
    diacritics: DiacriticProfile | None

    @property
    def review_policy(self) -> str:
        """'sample' = statistical release sample; 'full' = every chunk human-approved."""
        return "sample" if self.tier in {"A", "B"} else "full"

    @property
    def verified_possible_without_full_review(self) -> bool:
        return self.review_policy == "sample"


def _base(code: str) -> str:
    return code.split("-")[0].lower()


def _tier(code: str) -> str:
    base = _base(code)
    if base in TIER_A:
        return "A"
    if base in TIER_B:
        return "B"
    if base in TIER_C:
        return "C"
    if base in TIER_C2:
        return "C2"
    if base in TIER_D_NIGERIA:
        return "D"
    return "U"


class UnknownLanguage(ValueError):
    pass


@lru_cache(maxsize=4096)
def get_language(code: str) -> LanguageInfo:
    code = (code or "").strip()
    if not code:
        raise UnknownLanguage("Empty language code")
    try:
        lang = langcodes.Language.get(code)
    except (ValueError, langcodes.tag_parser.LanguageTagError) as exc:  # type: ignore[attr-defined]
        raise UnknownLanguage(f"Not a valid language tag: {code!r}") from exc
    if not lang.language:
        raise UnknownLanguage(f"Not a valid language tag: {code!r}")
    base = lang.language
    known = (
        pycountry.languages.get(alpha_2=base) if len(base) == 2 else pycountry.languages.get(alpha_3=base)
    )
    if known is None and base not in TIER_D_NIGERIA | TIER_C2:
        raise UnknownLanguage(f"Unknown language code: {code!r}")
    try:
        script = lang.script or lang.maximize().script
    except Exception:  # pragma: no cover - langcodes data edge cases
        script = None
    try:
        name = lang.display_name()
    except Exception:  # pragma: no cover
        name = getattr(known, "name", code)
    return LanguageInfo(
        code=lang.to_tag(),
        name=name,
        script=script,
        rtl=script in RTL_SCRIPTS,
        tier=_tier(code),
        diacritics=DIACRITICS.get(base),
    )


@lru_cache
def all_languages() -> list[tuple[str, str]]:
    """(code, name) for every ISO 639-3 individual language, for the UI picker."""
    out: dict[str, str] = {}
    for lang in pycountry.languages:
        code = getattr(lang, "alpha_2", None) or lang.alpha_3
        out[code] = lang.name
    return sorted(out.items(), key=lambda kv: kv[1])


def char_script_matches(ch: str, script: str) -> bool | None:
    prefixes = SCRIPT_NAME_PREFIXES.get(script)
    if not prefixes:
        return None
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return False
    return name.startswith(prefixes)
