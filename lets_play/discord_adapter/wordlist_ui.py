from __future__ import annotations

import discord
from beacon import DestructiveConfirmationView

from lets_play.new_rules.phrase_bank import PhraseBankError
from lets_play.presentation import default_presentation
from lets_play.storage.guild_phrases import GuildPhraseBank


class AddPhraseModal(discord.ui.Modal):
    def __init__(self, bot, guild_id: int, view_instance, copy=None):
        self.copy = copy or getattr(bot, "presentation", None) or default_presentation()
        super().__init__(title=self.copy.text("wordlist.modal_title"))
        self.bot = bot
        self.guild_id = guild_id
        self.view_instance = view_instance

        self.phrase_input = discord.ui.TextInput(
            label=self.copy.text("wordlist.input_label"),
            placeholder=self.copy.text("wordlist.input_placeholder"),
            max_length=80,
            required=True,
        )
        self.add_item(self.phrase_input)

    async def on_submit(self, interaction: discord.Interaction):
        text = self.phrase_input.value.strip()
        try:
            async with self.bot.game_runtime.store.transaction() as unit:
                bank = await GuildPhraseBank(unit).add(self.guild_id, text)
            await self.view_instance.refresh_phrases(interaction, bank)
        except PhraseBankError as e:
            msg = self.copy.text("wordlist.error_add", e=e)
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            msg = self.copy.text("wordlist.error_generic", e=e)
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)


class WordlistConfigLayoutView(discord.ui.LayoutView):
    def __init__(self, bot, guild_id: int, phrases, presentation=None):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.phrases = phrases or ()
        self.copy = presentation or getattr(bot, "presentation", None) or default_presentation()
        settings = getattr(bot, "settings", None) if bot else None
        add_emoji_str = getattr(settings, "add_emoji", None) if settings else None
        cancel_emoji_str = getattr(settings, "cancel_emoji", None) if settings else None
        bin_emoji_str = getattr(settings, "bin_emoji", None) if settings else None
        self.add_emoji = self._parse_emoji(add_emoji_str)
        self.cancel_emoji = self._parse_emoji(cancel_emoji_str)
        self.bin_emoji = self._parse_emoji(bin_emoji_str)
        self.build_layout()

    def _parse_emoji(self, val):
        if not val:
            return None
        if isinstance(val, (discord.PartialEmoji, discord.Emoji)):
            return val
        if isinstance(val, str):
            try:
                return discord.PartialEmoji.from_str(val)
            except Exception:
                return val
        return val

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        if self.phrases:
            phrases_count_btn = discord.ui.Button(
                label=f"{len(self.phrases)}/15", style=discord.ButtonStyle.secondary, disabled=True
            )
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(self.copy.text("wordlist.title")),
                    accessory=phrases_count_btn,
                )
            )
        else:
            container.add_item(discord.ui.TextDisplay(self.copy.text("wordlist.title")))
        container.add_item(discord.ui.TextDisplay(self.copy.text("wordlist.description")))
        container.add_item(discord.ui.Separator())

        if self.phrases:
            for p in self.phrases:
                remove_btn = discord.ui.Button(
                    emoji=self.bin_emoji,
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"wordlist:remove:{p.phrase_id}",
                )
                remove_btn.callback = self.make_remove_callback(p.phrase_id)
                section = discord.ui.Section(
                    f"`{p.phrase_id}.` {p.text}",
                    accessory=remove_btn,
                )
                container.add_item(section)

            # Buttons
            container.add_item(discord.ui.Separator())
            add_btn = discord.ui.Button(
                label=self.copy.text("buttons.wordlist_add"),
                emoji=self.add_emoji,
                style=discord.ButtonStyle.primary,
                custom_id="wordlist:add",
                disabled=len(self.phrases) >= 15,
            )
            add_btn.callback = self.add_callback
            reset_btn = discord.ui.Button(
                label=self.copy.text("buttons.wordlist_reset"),
                emoji=self.bin_emoji,
                style=discord.ButtonStyle.danger,
                custom_id="wordlist:reset",
            )
            reset_btn.callback = self.reset_callback

            row = discord.ui.ActionRow()
            row.add_item(add_btn)
            row.add_item(reset_btn)
            container.add_item(row)
        else:
            add_btn = discord.ui.Button(
                label=self.copy.text("buttons.wordlist_add"),
                emoji=self.add_emoji,
                style=discord.ButtonStyle.primary,
                custom_id="wordlist:add",
            )
            add_btn.callback = self.add_callback
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(self.copy.text("wordlist.empty_description")),
                    accessory=add_btn,
                )
            )
            container.add_item(discord.ui.Separator())

        self.add_item(container)

    def make_remove_callback(self, phrase_id: str):
        async def remove_callback(interaction: discord.Interaction):
            try:
                async with self.bot.game_runtime.store.transaction() as unit:
                    bank = await GuildPhraseBank(unit).remove(self.guild_id, phrase_id)
                await self.refresh_phrases(interaction, bank)
            except Exception as e:
                msg = self.copy.text("wordlist.error_generic", e=e)
                if not interaction.response.is_done():
                    await interaction.response.send_message(msg, ephemeral=True)
                else:
                    await interaction.followup.send(msg, ephemeral=True)

        return remove_callback

    async def add_callback(self, interaction: discord.Interaction):
        modal = AddPhraseModal(self.bot, self.guild_id, self, copy=self.copy)
        await interaction.response.send_modal(modal)

    async def reset_callback(self, interaction: discord.Interaction):
        async def confirm_reset(i: discord.Interaction):
            try:
                async with self.bot.game_runtime.store.transaction() as unit:
                    bank = await GuildPhraseBank(unit).reset(self.guild_id)
                self.phrases = bank or ()
            except Exception as e:
                try:
                    msg = self.copy.text("wordlist.error_generic", e=e)
                    if not i.response.is_done():
                        await i.response.send_message(msg, ephemeral=True)
                    else:
                        await i.followup.send(msg, ephemeral=True)
                except Exception:
                    pass

        view = DestructiveConfirmationView(
            user=interaction.user,
            title_text=self.copy.text("wordlist.reset_title"),
            body_text=self.copy.text("wordlist.reset_body"),
            on_confirmation_callback=confirm_reset,
            parent_view=self,
            parent_view_refresh_function=self.build_layout,
        )
        await interaction.response.edit_message(view=view)

    async def refresh_phrases(self, interaction: discord.Interaction, new_phrases):
        self.phrases = new_phrases or ()
        self.build_layout()
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                await interaction.edit_original_response(view=self)
        except Exception:
            pass
