"""Interaction views and components copied from the Beacon Framework because it requires Python 3.14 but the project uses 3.15"""

import inspect
from collections.abc import Callable, Coroutine
from typing import Any

import discord


class PrivateView(discord.ui.View):
    """Base view that only accepts interactions from one authorized user."""

    def __init__(self, user: discord.User | discord.Member, *args, **kwargs):
        """Initialize a private interaction view bound to a specific user.

        Args:
            user: User that is allowed to interact with this flow.
            *args: Additional positional arguments forwarded to the parent implementation.
            **kwargs: Additional keyword arguments forwarded to the underlying API.
        """
        super().__init__(*args, **kwargs)
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction):
        """Reject interactions from users other than the authorized one.

        Args:
            interaction: Interaction context received from Discord.

        Returns:
            bool: True when the check passes.
        """
        if interaction.user.id != self.user.id:
            watermark = getattr(
                interaction.client,
                "FRAMEWORK_WATERMARK",
                "\n-# Powered by [Beacon Framework](<https://beacon.dopaminestudios.in/>).",
            )
            await interaction.response.send_message(
                f"This isn't for you! This menu was originally opened by {self.user.mention}. To use it, you will have to run the original command yourself, I don't make the rules.{watermark}",
                ephemeral=True,
            )
            return False
        return True


class PrivateLayoutView(discord.ui.LayoutView):
    """Layout-view variant that enforces single-user interaction ownership."""

    def __init__(self, user: discord.User | discord.Member, *args, **kwargs):
        """Initialize a private layout view bound to a specific user.

        Args:
            user (discord.User | discord.Member): User that is allowed to interact with this flow.
            *args: Additional positional arguments forwarded to the parent implementation.
            **kwargs: Additional keyword arguments forwarded to the underlying API.
        """
        super().__init__(*args, **kwargs)
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction):
        """Reject interactions from users other than the authorized one.

        Args:
            interaction: Interaction context received from Discord.

        Returns:
            bool: True when the check passes.
        """
        if interaction.user.id != self.user.id:
            watermark = getattr(
                interaction.client,
                "FRAMEWORK_WATERMARK",
                "\n-# Powered by [Beacon Framework](<https://beacon.dopaminestudios.in/>).",
            )
            await interaction.response.send_message(
                f"This isn't for you! This menu was originally opened by {self.user.mention}. To use it, you will have to run the original command yourself, I don't make the rules.{watermark}",
                ephemeral=True,
            )
            return False
        return True


class ConfirmationView(PrivateLayoutView):
    def __init__(
        self,
        user: discord.User | discord.Member,
        timeout: float = 30.0,
        title_text: str = "Pending Confirmation",
        body_text: str = "Click Confirm to confirm the action.",
        on_confirmation_callback: Callable[[discord.Interaction], Any | Coroutine[Any, Any, Any]]
        | None = None,
        *args,
        **kwargs,
    ):
        """A custom Confirmation View that executes a custom callback that you define and passes the arguments you provide into it.

        Args:
            user: The discord user or member object of the user who originally triggered the interaction or command, so that only they can use this confirmation view.
            timeout: The timeout for the discord view.
            title_text: The big title text shown at the top of the confirmation view.
            body_text: The body text of the confirmation view.
            on_confirmation_callback: A synchronous or asynchronous function to execute when "Confirm" is successfully pressed. Use functools.partial if it requires any extra arguments other than interaction.
            *args: Positional arguments forwarded to the underlying implementation.
            **kwargs: Additional keyword arguments forwarded to the underlying implementation.
        """
        super().__init__(user, *args, timeout=timeout, **kwargs)
        self.value = None
        self.colour = None
        self.title_text = title_text
        self.body_text = body_text
        self.message: discord.Message | None = None
        self.on_confirmation_callback = on_confirmation_callback
        self.args = args
        self.kwargs = kwargs
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container(accent_colour=self.colour)
        container.add_item(discord.ui.TextDisplay(f"### {self.title_text}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.body_text))

        is_disabled = self.value is not None
        action_row = discord.ui.ActionRow()
        cancel = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.secondary, disabled=is_disabled
        )
        confirm = discord.ui.Button(
            label="Confirm", style=discord.ButtonStyle.green, disabled=is_disabled
        )

        cancel.callback = self.cancel_callback
        confirm.callback = self.confirm_callback

        action_row.add_item(cancel)
        action_row.add_item(confirm)
        container.add_item(discord.ui.Separator())
        container.add_item(action_row)

        self.add_item(container)

    async def update_view(
        self, interaction: discord.Interaction | None, title: str, colour: discord.Colour
    ):
        self.title_text = title
        self.body_text = f"~~{self.body_text}~~"
        self.colour = colour
        self.build_layout()

        if interaction:
            if interaction.response.is_done():
                await interaction.edit_original_response(view=self)
            else:
                await interaction.response.edit_message(view=self)
        elif self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

        self.stop()

    async def cancel_callback(self, interaction: discord.Interaction):
        self.value = False
        await self.update_view(interaction, "Action Canceled", discord.Color(0xDF5046))

    async def confirm_callback(self, interaction: discord.Interaction):
        self.value = True
        await self.update_view(interaction, "Action Confirmed", discord.Color.green())
        if self.on_confirmation_callback:
            if self.on_confirmation_callback:
                if inspect.iscoroutinefunction(self.on_confirmation_callback):
                    await self.on_confirmation_callback(interaction)
                else:
                    self.on_confirmation_callback(interaction)

    async def on_timeout(self) -> None:
        if self.value is None:
            self.value = False
            await self.update_view(None, "Timed Out", discord.Color(0xDF5046))


class DestructiveConfirmationView(PrivateLayoutView):
    def __init__(
        self,
        user: discord.User | discord.Member,
        timeout: float = 30.0,
        title_text: str = "Pending Confirmation",
        body_text: str = "Click Confirm to confirm the action.",
        on_confirmation_callback: Callable[[discord.Interaction], Any | Coroutine[Any, Any, Any]]
        | None = None,
        *args,
        **kwargs,
    ):
        """Like Beacon's normal Confirmation View, but the button is red. A custom Confirmation View that executes a custom callback that you define and passes the arguments you provide into it.

        Args:
            user: The discord user or member object of the user who originally triggered the interaction or command, so that only they can use this confirmation view.
            timeout: The timeout for the discord view.
            title_text: The big title text shown at the top of the confirmation view.
            body_text: The body text of the confirmation view.
            on_confirmation_callback: A synchronous or asynchronous function to execute when "Confirm" is successfully pressed. Use functools.partial if it requires any extra arguments other than interaction.
            *args: Positional arguments forwarded to the underlying implementation.
            **kwargs: Additional keyword arguments forwarded to the underlying implementation.
        """
        super().__init__(user, timeout=timeout)
        self.value = None
        self.colour = None
        self.title_text = title_text
        self.body_text = body_text
        self.message: discord.Message | None = None
        self.on_confirmation_callback = on_confirmation_callback
        self.args = args
        self.kwargs = kwargs
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container(accent_colour=self.colour)
        container.add_item(discord.ui.TextDisplay(f"### {self.title_text}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.body_text))

        is_disabled = self.value is not None
        action_row = discord.ui.ActionRow()
        cancel = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.secondary, disabled=is_disabled
        )
        confirm = discord.ui.Button(
            label="Confirm", style=discord.ButtonStyle.danger, disabled=is_disabled
        )

        cancel.callback = self.cancel_callback
        confirm.callback = self.confirm_callback

        action_row.add_item(cancel)
        action_row.add_item(confirm)
        container.add_item(discord.ui.Separator())
        container.add_item(action_row)

        self.add_item(container)

    async def update_view(
        self, interaction: discord.Interaction | None, title: str, colour: discord.Colour
    ):
        self.title_text = title
        self.body_text = f"~~{self.body_text}~~"
        self.colour = colour
        self.build_layout()

        if interaction:
            if interaction.response.is_done():
                await interaction.edit_original_response(view=self)
            else:
                await interaction.response.edit_message(view=self)
        elif self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

        self.stop()

    async def cancel_callback(self, interaction: discord.Interaction):
        self.value = False
        await self.update_view(interaction, "Action Canceled", discord.Color(0xDF5046))

    async def confirm_callback(self, interaction: discord.Interaction):
        self.value = True
        await self.update_view(interaction, "Action Confirmed", discord.Color.green())
        if self.on_confirmation_callback:
            if self.on_confirmation_callback:
                if inspect.iscoroutinefunction(self.on_confirmation_callback):
                    await self.on_confirmation_callback(interaction)
                else:
                    self.on_confirmation_callback(interaction)

    async def on_timeout(self) -> None:
        if self.value is None:
            self.value = False
            await self.update_view(None, "Timed Out", discord.Color(0xDF5046))
