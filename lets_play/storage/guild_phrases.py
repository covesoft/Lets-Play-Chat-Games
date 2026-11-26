"""Guild-owned phrase settings, independent of session offers and usage."""

from lets_play.new_rules.hands import PhraseChoice
from lets_play.new_rules.phrase_bank import parse_phrase_bank
from lets_play.storage.game_store import _execute, _row, _unit_mutating
from lets_play.storage.opening_codec import decode_config, encode_config


class GuildPhraseBank:
    def __init__(self, unit):
        self.unit = unit

    async def get(self, guild_id):
        self.unit._check()
        row = await _row(
            self.unit._conn,
            "SELECT bank_json FROM nr_guild_phrase_banks WHERE guild_id=?",
            (guild_id,),
        )
        return decode_config(row[0])[0] if row is not None else None

    @_unit_mutating
    async def seed(self, guild_id):
        """Copy current defaults once. An explicitly empty saved bank stays empty."""
        existing = await self.get(guild_id)
        if existing is not None:
            return existing
        defaults = self.unit.guild_phrase_defaults
        if defaults:
            await self.replace(guild_id, defaults)
            return defaults
        return None

    @_unit_mutating
    async def replace(self, guild_id, phrases):
        if type(guild_id) is not int or not 0 < guild_id <= (1 << 63) - 1:
            raise ValueError("A positive SQLite-compatible guild ID is required.")
        await _execute(
            self.unit._conn,
            """INSERT INTO nr_guild_phrase_banks(guild_id,bank_json) VALUES (?,?)
            ON CONFLICT(guild_id) DO UPDATE SET bank_json=excluded.bank_json""",
            (guild_id, encode_config(phrases, frozenset())),
        )

    @_unit_mutating
    async def add(self, guild_id, phrase_text: str):
        current = await self.seed(guild_id) or ()
        texts = [p.text for p in current] + [phrase_text]
        new_bank = parse_phrase_bank("\n".join(texts), suitability=self.unit.profanity)
        await self.replace(guild_id, new_bank)
        return new_bank

    @_unit_mutating
    async def remove(self, guild_id, phrase_id: str):
        current = await self.seed(guild_id) or ()
        new_bank = tuple(p for p in current if p.phrase_id != phrase_id)
        reindexed = tuple(
            PhraseChoice(str(i + 1), p.text, p.revision) for i, p in enumerate(new_bank)
        )
        await self.replace(guild_id, reindexed)
        return reindexed

    @_unit_mutating
    async def reset(self, guild_id):
        defaults = self.unit.guild_phrase_defaults or ()
        await self.replace(guild_id, defaults)
        return defaults
