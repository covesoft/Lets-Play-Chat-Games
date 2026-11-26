from __future__ import annotations

import asyncio

import discord

from lets_play.discord_adapter.beacon_helper_views import DestructiveConfirmationView

from lets_play.new_rules.phrase_bank import PhraseBankError
from lets_play.storage.guild_phrases import GuildPhraseBank


class AddPhraseModal(discord.ui.Modal, title="Add Don't Forget Phrase"):
    phrase_input = discord.ui.TextInput(
        label="Phrase or GIF URL",
        placeholder="Enter 1-5 words or 1 GIF URL (max 80 chars)",
        max_length=80,
        required=True,
    )

    def __init__(self, bot, guild_id: int, view_instance):
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id
        self.view_instance = view_instance

    async def on_submit(self, interaction: discord.Interaction):
        text = self.phrase_input.value.strip()
        try:
            async with self.bot.game_runtime.store.transaction() as unit:
                bank = await GuildPhraseBank(unit).add(self.guild_id, text)
            await self.view_instance.refresh_phrases(interaction, bank)
        except PhraseBankError as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Cannot add phrase: {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"Cannot add phrase: {e}", ephemeral=True)
        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"An error occurred: {e}", ephemeral=True)


class WordlistConfigLayoutView(discord.ui.LayoutView):
    def __init__(self, bot, guild_id: int, phrases):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.phrases = phrases or ()
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        if self.phrases:
            phrases_count_btn = discord.ui.Button(
                label=f"{len(self.phrases)}/15", style=discord.ButtonStyle.secondary, disabled=True
            )
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay("## Don't Forget Wordlist Management"),
                    accessory=phrases_count_btn,
                )
            )
        else:
            container.add_item(discord.ui.TextDisplay("## Don't Forget Wordlist Management"))
        container.add_item(
            discord.ui.TextDisplay(
                "Players must include one of these phrases during Don't Forget card activations."
            )
        )
        container.add_item(discord.ui.Separator())

        if self.phrases:
            for p in self.phrases:
                remove_btn = discord.ui.Button(
                    label="Remove",
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
                label="Add Phrase", style=discord.ButtonStyle.primary, custom_id="wordlist:add", disabled=len(self.phrases) >= 15
            )
            add_btn.callback = self.add_callback
            reset_btn = discord.ui.Button(
                label="Delete All Custom Phrases",
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
                label="Add Phrase", style=discord.ButtonStyle.primary, custom_id="wordlist:add"
            )
            add_btn.callback = self.add_callback
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(
                        "Nothing here yet. Add your own custom beautiful phrase!"
                    ),
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
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        f"An error occurred: {e}", ephemeral=True
                    )
                else:
                    await interaction.followup.send(f"An error occurred: {e}", ephemeral=True)

        return remove_callback

    async def add_callback(self, interaction: discord.Interaction):
        modal = AddPhraseModal(self.bot, self.guild_id, self)
        await interaction.response.send_modal(modal)

    async def reset_callback(self, interaction: discord.Interaction):
        async def confirm_reset(i: discord.Interaction):
            try:
                async with self.bot.game_runtime.store.transaction() as unit:
                    bank = await GuildPhraseBank(unit).reset(self.guild_id)
                await self.refresh_phrases(i, bank)
            except Exception as e:
                try:
                    if not i.response.is_done():
                        await i.response.send_message(
                            f"An error occurred: {e}", ephemeral=True
                        )
                    else:
                        await i.followup.send(f"An error occurred: {e}", ephemeral=True)
                except Exception:
                    pass

        view = DestructiveConfirmationView(
            user=interaction.user,
            title_text="Delete All Custom Phrases",
            body_text="Are you sure you want to delete all custom phrases and reset the wordlist to system defaults?",
            on_confirmation_callback=confirm_reset,
        )
        await interaction.response.edit_message(view=view)

    async def refresh_phrases(self, interaction: discord.Interaction, new_phrases):
        self.phrases = new_phrases or ()
        self.build_layout()
        await asyncio.sleep(1.5)
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                await interaction.edit_original_response(view=self)
        except Exception:
            pass