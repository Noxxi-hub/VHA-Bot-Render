# ════════════════════════════════════════════════
#  tsprachen.py  •  VHA Übersetzer-Bot
#  Globale Sprachen + Raumsprachen per Button
#  PT + EN immer aktiv (fix)
#  JA, ZH, KO, ES, IT, RU, AR, TR, PL, NL zuschaltbar
#  DE + FR absichtlich ausgelassen → Haupt-Bot
# ════════════════════════════════════════════════

import discord
from discord.ext import commands
import sqlite3
import json
import os
import logging

log = logging.getLogger("VHATranslator.Sprachen")

# Backend automatisch wählen: wenn MONGODB_URI gesetzt ist (z.B. auf Render),
# wird MongoDB genutzt. Sonst läuft es wie bisher über die lokale SQLite-DB
# (z.B. auf deinem eigenen Server).
USE_MONGO = bool(os.getenv("MONGODB_URI"))

if USE_MONGO:
    from mongo_client import get_db as _get_mongo_db
    log.info("💾 Sprachen-Backend: MongoDB")
else:
    log.info("💾 Sprachen-Backend: SQLite (lokal)")

LOGO_URL = (
    "https://cdn.discordapp.com/attachments/1484252260614537247/"
    "1484253018533662740/Picsart_26-03-18_13-55-24-994.png"
    "?ex=69bd8dd7&is=69bc3c57&hm=de6fea399dd30f97d2a14e1515c9e7f91d81d0d9ea111f13e0757d42eb12a0e5&"
)

# Pfad zur gemeinsamen SQLite-Datenbank (nur genutzt wenn USE_MONGO=False)
_DB_PATH = "/home/botdata/botdata.sqlite"

def _get_db() -> sqlite3.Connection:
    """Gibt eine SQLite-Verbindung zurück."""
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

# Sprachen die immer aktiv sind (können nicht abgeschaltet werden)
FIXED_LANGS = {"PT", "EN"}

# Zuschaltbare Sprachen
OPTIONAL_LANGS = {
    "JA": {"flag": "🇯🇵", "name": "日本語"},
    "ZH": {"flag": "🇨🇳", "name": "中文"},
    "KO": {"flag": "🇰🇷", "name": "한국어"},
    "ES": {"flag": "🇪🇸", "name": "Español"},
    "RU": {"flag": "🇷🇺", "name": "Русский"},
    "TR": {"flag": "🇹🇷", "name": "Türkçe"},
}

ALLOWED_ROLES = {"R5", "R4", "DEV"}


# ────────────────────────────────────────────────
# Sprachen-Speicher — SQLite (lokal) oder MongoDB (Render)
# ────────────────────────────────────────────────

def get_active_langs() -> set:
    """Globale aktive Sprachen (inkl. FIXED_LANGS)."""
    default = {"PT", "EN"}
    if USE_MONGO:
        try:
            col = _get_mongo_db()["tsprachen"]
            doc = col.find_one({"_id": "settings"})
            if not doc or not doc.get("active"):
                col.update_one({"_id": "settings"}, {"$set": {"active": list(default)}}, upsert=True)
                return default
            active = set(doc["active"])
            active.update(FIXED_LANGS)
            return active
        except Exception as e:
            log.error(f"Fehler beim Laden der Sprachen (MongoDB): {e}")
            return default
    else:
        try:
            conn = _get_db()
            row = conn.execute("SELECT active FROM tsprachen WHERE _id = 'settings'").fetchone()
            if not row or not row[0]:
                conn.execute("INSERT OR REPLACE INTO tsprachen (_id, active) VALUES ('settings', ?)",
                             (json.dumps(list(default)),))
                conn.commit()
                conn.close()
                return default
            active = set(json.loads(row[0]))
            active.update(FIXED_LANGS)
            conn.close()
            return active
        except Exception as e:
            log.error(f"Fehler beim Laden der Sprachen (SQLite): {e}")
            return default


def set_active_langs(langs: set):
    """Speichert globale Sprachen."""
    langs.update(FIXED_LANGS)
    if USE_MONGO:
        try:
            col = _get_mongo_db()["tsprachen"]
            col.update_one({"_id": "settings"}, {"$set": {"active": list(langs)}}, upsert=True)
        except Exception as e:
            log.error(f"Fehler beim Speichern der Sprachen (MongoDB): {e}")
    else:
        try:
            conn = _get_db()
            conn.execute("INSERT OR REPLACE INTO tsprachen (_id, active) VALUES ('settings', ?)",
                         (json.dumps(list(langs)),))
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"Fehler beim Speichern der Sprachen (SQLite): {e}")


# HARDCODED Räume — überschreiben die DB immer
_HARDCODED_ROOMS = {
    1498224449529577595: {"FR", "EN"},
}

def get_room_langs(channel_id: int) -> set | None:
    """
    Raumsprachen für einen Kanal.
    Gibt None zurück wenn keine eigenen Einstellungen → globale nutzen.
    Gibt leeres set zurück wenn Kanal deaktiviert.
    """
    if channel_id in _HARDCODED_ROOMS:
        return _HARDCODED_ROOMS[channel_id]

    if USE_MONGO:
        try:
            col = _get_mongo_db()["tsprachen_rooms"]
            doc = col.find_one({"_id": str(channel_id)})
            if not doc:
                return None
            if doc.get("disabled"):
                return set()
            langs = set(doc.get("langs", []))
            return langs if langs else None
        except Exception as e:
            log.error(f"Fehler beim Laden der Raumsprachen (MongoDB): {e}")
            return None
    else:
        try:
            conn = _get_db()
            row = conn.execute("SELECT langs, disabled FROM tsprachen_rooms WHERE _id = ?",
                               (str(channel_id),)).fetchone()
            conn.close()
            if not row:
                return None
            if row[1]:  # disabled
                return set()
            langs = set(json.loads(row[0])) if row[0] else set()
            return langs if langs else None
        except Exception as e:
            log.error(f"Fehler beim Laden der Raumsprachen (SQLite): {e}")
            return None


def set_room_langs(channel_id: int, langs: set | None, disabled: bool = False):
    """Speichert Raumsprachen."""
    if USE_MONGO:
        try:
            col = _get_mongo_db()["tsprachen_rooms"]
            if langs is None and not disabled:
                col.delete_one({"_id": str(channel_id)})
            else:
                col.update_one(
                    {"_id": str(channel_id)},
                    {"$set": {"langs": list(langs) if langs else [], "disabled": bool(disabled)}},
                    upsert=True
                )
        except Exception as e:
            log.error(f"Fehler beim Speichern der Raumsprachen (MongoDB): {e}")
    else:
        try:
            conn = _get_db()
            if langs is None and not disabled:
                conn.execute("DELETE FROM tsprachen_rooms WHERE _id = ?", (str(channel_id),))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO tsprachen_rooms (_id, langs, disabled) VALUES (?, ?, ?)",
                    (str(channel_id), json.dumps(list(langs)) if langs else "[]", int(disabled))
                )
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"Fehler beim Speichern der Raumsprachen (SQLite): {e}")


BOT_OWNER_ID = 1464651603654086748

def has_permission(member: discord.Member) -> bool:
    if member.id == BOT_OWNER_ID:
        return True
    if member.guild_permissions.administrator:
        return True
    member_roles = {r.name.upper() for r in member.roles}
    return bool(member_roles & ALLOWED_ROLES)


# ────────────────────────────────────────────────
# Globale Sprachen — Button View
# ────────────────────────────────────────────────

class GlobalSprachenView(discord.ui.View):
    def __init__(self, author: discord.Member):
        super().__init__(timeout=120)
        self.author = author
        self._update_buttons()

    def _update_buttons(self):
        self.clear_items()
        active = get_active_langs()

        for code, info in OPTIONAL_LANGS.items():
            is_active = code in active
            btn = discord.ui.Button(
                label=f"{info['flag']} {info['name']}",
                style=discord.ButtonStyle.success if is_active else discord.ButtonStyle.secondary,
                emoji="✅" if is_active else "❌",
                custom_id=f"tlang_{code}"
            )
            btn.callback = self._make_callback(code)
            self.add_item(btn)

    def _make_callback(self, code: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.author.id:
                await interaction.response.send_message(
                    "❌ Nur derjenige der den Befehl ausgeführt hat kann Änderungen vornehmen.",
                    ephemeral=True
                )
                return

            try:
                active = set(get_active_langs())
                active.update(FIXED_LANGS)
            except Exception:
                active = set(FIXED_LANGS)

            if code in active:
                active.discard(code)
                action = "deaktiviert"
            else:
                active.add(code)
                action = "aktiviert"

            try:
                set_active_langs(active)
            except Exception as e:
                await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)
                return

            info = OPTIONAL_LANGS[code]
            self._update_buttons()
            embed = self._make_embed()
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(
                f"{info['flag']} **{info['name']}** {action}!",
                ephemeral=True
            )

        return callback

    def _make_embed(self) -> discord.Embed:
        active = get_active_langs()
        embed = discord.Embed(
            title="🌐 Übersetzer-Bot • Globale Sprachen",
            color=0x2ECC71
        )
        embed.set_author(name="VHA Übersetzer-Bot", icon_url=LOGO_URL)

        embed.add_field(
            name="🔒 Immer aktiv",
            value="🇧🇷 Português • 🇬🇧 English",
            inline=False
        )

        status_lines = []
        for code, info in OPTIONAL_LANGS.items():
            status = "✅ Aktiv" if code in active else "❌ Inaktiv"
            status_lines.append(f"{info['flag']} {info['name']}: **{status}**")

        embed.add_field(
            name="🔄 Ein/Ausschaltbar",
            value="\n".join(status_lines),
            inline=False
        )

        embed.set_footer(
            text="Klicke auf einen Button um eine Sprache ein/auszuschalten",
            icon_url=LOGO_URL
        )
        return embed


# ────────────────────────────────────────────────
# Raumsprachen — Button View
# ────────────────────────────────────────────────

class RaumSprachenView(discord.ui.View):
    def __init__(self, author: discord.Member, channel_id: int, channel_name: str, current: set):
        super().__init__(timeout=120)
        self.author = author
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.current = current  # State im Memory — kein DB-Call beim Button-Klick
        self._update_buttons()

    def _update_buttons(self):
        self.clear_items()
        all_langs = {
            "DE": {"flag": "🇩🇪", "name": "Deutsch"},
            "FR": {"flag": "🇫🇷", "name": "Français"},
            "PT": {"flag": "🇧🇷", "name": "Português"},
            "EN": {"flag": "🇬🇧", "name": "English"},
            **OPTIONAL_LANGS
        }
        for code, info in all_langs.items():
            is_active = code in self.current
            btn = discord.ui.Button(
                label=f"{info['flag']} {info['name']}",
                style=discord.ButtonStyle.success if is_active else discord.ButtonStyle.secondary,
                emoji="✅" if is_active else "❌",
                custom_id=f"troom_{self.channel_id}_{code}"
            )
            btn.callback = self._make_callback(code)
            self.add_item(btn)

        reset_btn = discord.ui.Button(
            label="📡 Globale Einstellungen",
            style=discord.ButtonStyle.primary,
            custom_id=f"troom_{self.channel_id}_reset",
            row=4
        )
        reset_btn.callback = self._reset_callback
        self.add_item(reset_btn)

        off_btn = discord.ui.Button(
            label="🚫 Alle aus",
            style=discord.ButtonStyle.danger,
            custom_id=f"troom_{self.channel_id}_off",
            row=4
        )
        off_btn.callback = self._off_callback
        self.add_item(off_btn)

    def _make_callback(self, code: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.author.id:
                await interaction.response.send_message(
                    "❌ Nur derjenige der den Befehl ausgeführt hat kann Änderungen vornehmen.",
                    ephemeral=True
                )
                return

            # State im Memory updaten — kein DB-Call nötig
            if code in self.current:
                self.current.discard(code)
                action = "deaktiviert"
            else:
                self.current.add(code)
                action = "aktiviert"

            # In DB speichern
            set_room_langs(self.channel_id, self.current.copy(), disabled=False)

            self._update_buttons()
            embed = self._make_embed()
            await interaction.response.edit_message(embed=embed, view=self)

            all_langs = {"DE": {"flag": "🇩🇪", "name": "Deutsch"}, "FR": {"flag": "🇫🇷", "name": "Français"}, "PT": {"flag": "🇧🇷", "name": "Português"}, "EN": {"flag": "🇬🇧", "name": "English"}, **OPTIONAL_LANGS}
            info = all_langs.get(code, {"flag": "🌐", "name": code})
            await interaction.followup.send(
                f"{info['flag']} **{info['name']}** in #{self.channel_name} {action}!",
                ephemeral=True
            )
        return callback

    async def _reset_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "❌ Nur derjenige der den Befehl ausgeführt hat kann Änderungen vornehmen.",
                ephemeral=True
            )
            return
        # DB-Eintrag löschen → globale Einstellungen
        set_room_langs(self.channel_id, None)
        self.current = get_active_langs().copy()
        self._update_buttons()
        embed = self._make_embed()
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            f"📡 #{self.channel_name} nutzt jetzt wieder die **globalen Einstellungen**.",
            ephemeral=True
        )

    async def _off_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "❌ Nur derjenige der den Befehl ausgeführt hat kann Änderungen vornehmen.",
                ephemeral=True
            )
            return
        set_room_langs(self.channel_id, set(), disabled=True)
        self.current = set()
        self._update_buttons()
        embed = self._make_embed()
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            f"🚫 Übersetzung in #{self.channel_name} **deaktiviert**.",
            ephemeral=True
        )

    def _make_embed(self) -> discord.Embed:
        room_setting = get_room_langs(self.channel_id)
        if room_setting is None:
            status_text = "📡 Nutzt globale Einstellungen"
            color = 0x3498DB
        elif len(self.current) == 0:
            status_text = "🚫 Übersetzung deaktiviert"
            color = 0xED4245
        else:
            status_text = "⚙️ Eigene Einstellungen aktiv"
            color = 0x2ECC71

        embed = discord.Embed(
            title=f"⚙️ Raumsprachen • #{self.channel_name}",
            color=color
        )
        embed.set_author(name="VHA Übersetzer-Bot", icon_url=LOGO_URL)
        embed.add_field(name="Status", value=status_text, inline=False)

        all_langs = {
            "DE": {"flag": "🇩🇪", "name": "Deutsch"},
            "FR": {"flag": "🇫🇷", "name": "Français"},
            "PT": {"flag": "🇧🇷", "name": "Português"},
            "EN": {"flag": "🇬🇧", "name": "English"},
            **OPTIONAL_LANGS
        }
        status_lines = []
        for code, info in all_langs.items():
            status = "✅ Aktiv" if code in self.current else "❌ Inaktiv"
            status_lines.append(f"{info['flag']} {info['name']}: **{status}**")

        embed.add_field(
            name="🔄 Sprachen für diesen Kanal",
            value="\n".join(status_lines),
            inline=False
        )
        embed.set_footer(
            text="📡 Globale Einstellungen = Reset • 🚫 Alle aus = Übersetzung deaktivieren",
            icon_url=LOGO_URL
        )
        return embed



# ────────────────────────────────────────────────
# Cog
# ────────────────────────────────────────────────

class TSprachenCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="sprachen", aliases=["languages", "idiomas", "lang"])
    async def cmd_sprachen(self, ctx):
        """Globale Sprachen des Übersetzer-Bots per Button verwalten."""
        if not has_permission(ctx.author):
            await ctx.send("❌ Keine Berechtigung.", delete_after=5)
            return
        try:
            async for msg in ctx.channel.history(limit=20):
                if msg.author == ctx.guild.me and msg.embeds:
                    if "Übersetzer-Bot • Globale Sprachen" in (msg.embeds[0].title or ""):
                        await msg.delete()
        except Exception:
            pass
        view = GlobalSprachenView(ctx.author)
        embed = view._make_embed()
        await ctx.send(embed=embed, view=view)

    @commands.command(name="kanalid", aliases=["channelid"])
    async def cmd_kanalid(self, ctx):
        """Alle Text-, Voice- und Forumkanäle mit ID als DM."""
        if not has_permission(ctx.author):
            await ctx.send("❌ Keine Berechtigung.", delete_after=5)
            return

        lines = []
        for category, channels in ctx.guild.by_category():
            cat_name = category.name if category else "Ohne Kategorie"
            # TextChannel, ForumChannel, VoiceChannel, StageChannel anzeigen
            relevant = [c for c in channels if isinstance(c, (
                discord.TextChannel,
                discord.ForumChannel,
                discord.VoiceChannel,
                discord.StageChannel,
            ))]
            if not relevant:
                continue
            lines.append(f"**{cat_name}**")
            for ch in relevant:
                if isinstance(ch, discord.ForumChannel):
                    ch_type = "📋 Forum"
                elif isinstance(ch, discord.VoiceChannel):
                    ch_type = "🔊 Voice"
                elif isinstance(ch, discord.StageChannel):
                    ch_type = "🎙️ Stage"
                else:
                    ch_type = "#"
                lines.append(f"• {ch_type} {ch.name} — `{ch.id}`")

        chunks = []
        current = []
        length = 0
        for line in lines:
            if length + len(line) > 1800:
                chunks.append("\n".join(current))
                current = [line]
                length = len(line)
            else:
                current.append(line)
                length += len(line)
        if current:
            chunks.append("\n".join(current))

        for i, chunk in enumerate(chunks):
            embed = discord.Embed(
                title=f"📋 Kanal-IDs • {ctx.guild.name}" + (f" ({i+1}/{len(chunks)})" if len(chunks) > 1 else ""),
                description=chunk,
                color=0x5865F2
            )
            embed.set_footer(text="🔊 Voice-IDs für !traumsprachen [ID] verwenden")
            await ctx.author.send(embed=embed)

        await ctx.send("📬 Kanal-IDs als Direktnachricht geschickt!", delete_after=8)


async def setup(bot):
    await bot.add_cog(TSprachenCog(bot))
