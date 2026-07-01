# ════════════════════════════════════════════════
#  traumsprachen.py  •  VHA Übersetzer-Bot
#  Raumsprachen per Button steuern — SQLite
#  Befehl: !traumsprachen
# ════════════════════════════════════════════════

import discord
from discord.ext import commands
import logging

from tsprachen import get_room_langs, set_room_langs, get_active_langs, has_permission, LOGO_URL

log = logging.getLogger("VHATranslator.Traumsprachen")

ALL_ROOM_LANGS = {
    "DE": {"flag": "🇩🇪", "name": "Deutsch"},
    "FR": {"flag": "🇫🇷", "name": "Français"},
    "PT": {"flag": "🇧🇷", "name": "Português"},
    "EN": {"flag": "🇬🇧", "name": "English"},
    "JA": {"flag": "🇯🇵", "name": "日本語"},
    "TR": {"flag": "🇹🇷", "name": "Türkçe"},
}


class TraumsprachenView(discord.ui.View):
    def __init__(self, author: discord.Member, channel_id: int, channel_name: str, current_langs: set, enabled: bool):
        super().__init__(timeout=120)
        self.author = author
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.selected_langs = set(current_langs)
        self.enabled = enabled
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        for code, info in ALL_ROOM_LANGS.items():
            is_sel = code in self.selected_langs
            btn = discord.ui.Button(
                label=f"{info['flag']} {info['name']}",
                style=discord.ButtonStyle.success if is_sel else discord.ButtonStyle.secondary,
                emoji="✅" if is_sel else "❌",
                custom_id=f"trl_{self.channel_id}_{code}"
            )
            btn.callback = self._make_callback(code)
            self.add_item(btn)

        toggle_label = "🔔 An" if not self.enabled else "🔕 Aus"
        toggle_btn = discord.ui.Button(
            label=toggle_label,
            style=discord.ButtonStyle.primary if not self.enabled else discord.ButtonStyle.danger,
            custom_id=f"trl_{self.channel_id}_toggle",
            row=1
        )
        toggle_btn.callback = self._toggle_callback
        self.add_item(toggle_btn)

        save_btn = discord.ui.Button(
            label="✅ Speichern",
            style=discord.ButtonStyle.success,
            custom_id=f"trl_{self.channel_id}_save",
            row=1
        )
        save_btn.callback = self._save_callback
        self.add_item(save_btn)

        del_btn = discord.ui.Button(
            label="🗑️ Reset",
            style=discord.ButtonStyle.danger,
            custom_id=f"trl_{self.channel_id}_reset",
            row=1
        )
        del_btn.callback = self._reset_callback
        self.add_item(del_btn)

    def _make_callback(self, code: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.author.id:
                await interaction.response.send_message(
                    "❌ Nur derjenige der den Befehl ausgeführt hat kann Änderungen vornehmen.",
                    ephemeral=True
                )
                return
            if code in self.selected_langs:
                self.selected_langs.discard(code)
            else:
                self.selected_langs.add(code)
            self._build_buttons()
            await interaction.response.edit_message(view=self)
        return callback

    async def _toggle_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "❌ Nur derjenige der den Befehl ausgeführt hat kann Änderungen vornehmen.",
                ephemeral=True
            )
            return
        self.enabled = not self.enabled
        self._build_buttons()
        await interaction.response.edit_message(view=self)

    async def _save_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "❌ Nur derjenige der den Befehl ausgeführt hat kann Änderungen vornehmen.",
                ephemeral=True
            )
            return
        set_room_langs(self.channel_id, self.selected_langs.copy(), disabled=not self.enabled)
        await interaction.response.send_message("✅ Raumsprachen gespeichert!", ephemeral=True)

    async def _reset_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "❌ Nur derjenige der den Befehl ausgeführt hat kann Änderungen vornehmen.",
                ephemeral=True
            )
            return
        set_room_langs(self.channel_id, None)
        await interaction.response.send_message("🗑️ Raumsprachen zurückgesetzt (nutzt jetzt globale Einstellung).", ephemeral=True)


class TraumsprachenCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="traumsprachen", aliases=["traumsprache", "raum"])
    async def cmd_traumsprachen(self, ctx, channel_id: str = None):
        """Raumsprachen des Übersetzer-Bots per Button verwalten."""
        if not has_permission(ctx.author):
            await ctx.send("❌ Keine Berechtigung.", delete_after=5)
            return

        if channel_id:
            try:
                cid = int(channel_id.replace("<#", "").replace(">", ""))
            except ValueError:
                await ctx.send("❌ Ungültige Kanal-ID.")
                return
        else:
            cid = ctx.channel.id

        ch = self.bot.get_channel(cid) or ctx.guild.get_channel(cid)
        ch_name = ch.name if ch else str(cid)

        # Altes Menü löschen
        try:
            async for msg in ctx.channel.history(limit=20):
                if msg.author == ctx.guild.me and msg.embeds:
                    if f"Raumsprachen • #{ch_name}" in (msg.embeds[0].title or ""):
                        await msg.delete()
        except Exception:
            pass

        # State laden — SQLite via tsprachen.py
        room = get_room_langs(cid)
        if room is None:
            current = get_active_langs().copy()
            enabled = True
        elif len(room) == 0:
            current = set()
            enabled = False
        else:
            current = room.copy()
            enabled = True

        embed = discord.Embed(
            title=f"🌐 Raumsprachen • #{ch_name}",
            color=0x5865F2
        )
        embed.set_author(name="VHA Übersetzer-Bot", icon_url=LOGO_URL)
        embed.add_field(name="Status", value="🔔 Aktiv" if enabled else "🔕 Deaktiviert", inline=True)
        lang_str = ", ".join(
            [f"{ALL_ROOM_LANGS[c]['flag']} {ALL_ROOM_LANGS[c]['name']}" for c in current if c in ALL_ROOM_LANGS]
        ) or "Keine (nutzt globale)"
        embed.add_field(name="Sprachen", value=lang_str, inline=True)
        embed.set_footer(
            text="Buttons klicken: Sprache ein/aus • 🔔/🔕: An-Aus • ✅: Speichern • 🗑️: Reset"
        )

        view = TraumsprachenView(ctx.author, cid, ch_name, current, enabled)
        await ctx.send(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(TraumsprachenCog(bot))
