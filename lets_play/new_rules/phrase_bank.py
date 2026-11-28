"""Validate operator-supplied phrases before they become guild defaults."""

from hashlib import sha256
from pathlib import Path

import regex

from lets_play.new_rules.hands import PhraseChoice
from lets_play.new_rules.parsing import parse_message
from lets_play.new_rules.profanity import ProfanityList, load_profanity_list
from lets_play.new_rules.text_rules import RuleConfigurationError, gif_phrase_url

MAX_GUILD_PHRASES = 15
MAX_PHRASE_WORDS = 5
MAX_PHRASE_CHARACTERS = 80
MAX_PHRASE_FILE_BYTES = 16 * 1024
_PLAIN_PHRASE = regex.compile(r"[\p{L}\p{M}\p{N}\p{Pd} '’.,!?…]+")


class PhraseBankError(ValueError):
    """Safe diagnostics may identify a line, without its wording or path."""


def parse_phrase_bank(
    text: str, *, suitability: ProfanityList | None = None
) -> tuple[PhraseChoice, ...]:
    revision = sha256(text.encode("utf-8")).hexdigest()
    phrases = []
    seen = set()
    for number, line in enumerate(text.splitlines(), 1):
        phrase = line.strip()
        if not phrase:
            continue
        if len(phrases) == MAX_GUILD_PHRASES:
            raise PhraseBankError("The file can contain up to 15 phrases.")
        if len(phrase) > MAX_PHRASE_CHARACTERS:
            raise PhraseBankError(
                f"Line {number} must be a plain phrase or GIF URL up to 80 characters."
            )
        view = parse_message(phrase)
        if not gif_phrase_url(view) and (
            not _PLAIN_PHRASE.fullmatch(phrase)
            or not 1 <= len(view.words) <= MAX_PHRASE_WORDS
            or view.formatting
            or view.shared_links
            or view.objects
        ):
            raise PhraseBankError(
                f"Line {number} must be a plain phrase of one to five words or one GIF URL."
            )
        if suitability is not None and suitability.matches(view):
            raise PhraseBankError(f"Line {number} does not pass the compulsory-phrase policy.")
        try:
            choice = PhraseChoice(str(len(phrases) + 1), phrase, revision)
        except RuleConfigurationError:
            raise PhraseBankError(f"Line {number} must be a plain phrase.") from None
        if choice.key in seen:
            raise PhraseBankError(f"Line {number} duplicates an earlier phrase.")
        seen.add(choice.key)
        phrases.append(choice)
    return tuple(phrases)


def load_guild_phrases(
    path: Path | None, suitability_path: Path | None
) -> tuple[PhraseChoice, ...] | None:
    if path is None:
        return None
    suitability = load_profanity_list(suitability_path)
    if suitability_path is not None and suitability is None:
        raise PhraseBankError("Check CHATGAMES_PHRASE_SUITABILITY_LIST_PATH.")
    try:
        with path.open("rb") as source:
            data = source.read(MAX_PHRASE_FILE_BYTES + 1)
        if len(data) <= MAX_PHRASE_FILE_BYTES:
            text = data.decode("utf-8-sig")
        else:
            raise PhraseBankError("The phrase file exceeds 16 KiB.")
    except OSError, UnicodeError:
        raise PhraseBankError("The phrase file must be readable UTF-8.") from None
    return parse_phrase_bank(text, suitability=suitability)
