import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from keepserver import keep_alive
keep_alive()

# ---------- Config ----------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN manquant dans .env")

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True

BOT_PREFIX = "!"
DB_PATH = "players.db"

TEST_GUILD_ID = 1282628230108418048
LEAD_ROLE_ID = 1282641140750880779

class MyBot(commands.Bot):
    async def setup_hook(self):
        guild = discord.Object(id=TEST_GUILD_ID)

        print("DEBUG globals before copy:", [c.name for c in self.tree.get_commands()])

        self.tree.copy_global_to(guild=guild)

        synced = await self.tree.sync(guild=guild)
        print(f"🌐 {len(synced)} cmd sync -> {[c.name for c in synced]}")

bot = MyBot(command_prefix=BOT_PREFIX, intents=INTENTS, help_command=None)
keep_alive()

# ---------- DB ----------
CREATE_TABLE_PLAYERS = """
CREATE TABLE IF NOT EXISTS players (
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    added_at_utc    TEXT    NOT NULL,
    trial_end_utc   TEXT    NOT NULL,
    notes           TEXT    DEFAULT '',
    notified_done   INTEGER NOT NULL DEFAULT 0,
    notified_at_utc TEXT    DEFAULT NULL,
    PRIMARY KEY (guild_id, user_id)
);
"""

CREATE_TABLE_SETTINGS = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id          INTEGER PRIMARY KEY,
    trial_channel_id  INTEGER
);
"""

CREATE_TABLE_NOTES = """
CREATE TABLE IF NOT EXISTS player_notes (
    guild_id               INTEGER NOT NULL,
    user_id                INTEGER NOT NULL,
    characters_level       TEXT    DEFAULT '', -- "Combien de perso/LVL"
    prev_guild_alliance    TEXT    DEFAULT '', -- "Ancienne guilde/alliance"
    optimized              TEXT    DEFAULT '', -- "Opti ou pas"
    content_preference     TEXT    DEFAULT '', -- "Préférence pvp ou pvm"
    objectives             TEXT    DEFAULT '', -- "Objectif/projets"
    age                    TEXT    DEFAULT '', -- optionnel
    contribution           TEXT    DEFAULT '', -- optionnel "Ce que tu amèneras..."
    updated_at_utc         TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
"""

CREATE_TABLE_PLAYERS_EXTERNAL = """
CREATE TABLE IF NOT EXISTS players_external (
    guild_id        INTEGER NOT NULL,
    name            TEXT    NOT NULL,  -- affichage
    name_key        TEXT    NOT NULL,  -- normalisé pour l'unicité (lower/strip)
    added_at_utc    TEXT    NOT NULL,
    trial_end_utc   TEXT    NOT NULL,
    notified_done   INTEGER NOT NULL DEFAULT 0,
    notified_at_utc TEXT    DEFAULT NULL,
    PRIMARY KEY (guild_id, name_key)
);
"""

CREATE_TABLE_NOTES_EXTERNAL = """
CREATE TABLE IF NOT EXISTS player_notes_external (
    guild_id            INTEGER NOT NULL,
    name_key            TEXT    NOT NULL, -- normalisé
    name                TEXT    NOT NULL, -- affichage original
    characters_level    TEXT    DEFAULT '',
    prev_guild_alliance TEXT    DEFAULT '',
    optimized           TEXT    DEFAULT '',
    content_preference  TEXT    DEFAULT '',
    objectives          TEXT    DEFAULT '',
    age                 TEXT    DEFAULT '',
    contribution        TEXT    DEFAULT '',
    updated_at_utc      TEXT    NOT NULL,
    PRIMARY KEY (guild_id, name_key)
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_PLAYERS)
        await db.execute(CREATE_TABLE_SETTINGS)
        await db.execute(CREATE_TABLE_NOTES)
        await db.execute(CREATE_TABLE_PLAYERS_EXTERNAL)
        await db.execute(CREATE_TABLE_NOTES_EXTERNAL)

        # Sécurité: ajouter colonnes manquantes si anciens schémas
        cols = {}
        async with db.execute("PRAGMA table_info(players)") as cur:
            for _, name, *_ in await cur.fetchall():
                cols[name] = True
        if "notified_done" not in cols:
            await db.execute("ALTER TABLE players ADD COLUMN notified_done INTEGER NOT NULL DEFAULT 0;")
        if "notified_at_utc" not in cols:
            await db.execute("ALTER TABLE players ADD COLUMN notified_at_utc TEXT DEFAULT NULL;")
        await db.commit()

# ---------- DB Ops ----------
async def set_trial_channel(guild_id: int, channel_id: Optional[int]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO guild_settings (guild_id, trial_channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET trial_channel_id=excluded.trial_channel_id",
            (guild_id, channel_id),
        )
        await db.commit()

async def get_trial_channel_id(guild_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT trial_channel_id FROM guild_settings WHERE guild_id=?", (guild_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] else None

async def add_player_to_db(guild_id: int, user_id: int) -> tuple[bool, str]:
    now_utc = datetime.now(timezone.utc)
    trial_end = now_utc + timedelta(days=14)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM players WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        ) as cur:
            if await cur.fetchone():
                return (False, "Ce membre est déjà dans la liste.")
        await db.execute(
            "INSERT INTO players (guild_id, user_id, added_at_utc, trial_end_utc) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, now_utc.isoformat(), trial_end.isoformat()),
        )
        await db.commit()
    return (True, "Membre ajouté avec succès.")

async def get_player_row(guild_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT added_at_utc, trial_end_utc, notes, notified_done, notified_at_utc "
            "FROM players WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ) as cur:
            return await cur.fetchone()

async def remove_player_from_db(guild_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM players WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        changes = db.total_changes
        await db.commit()
    return changes > 0

async def fetch_due_trials(guild_id: int):
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, added_at_utc, trial_end_utc FROM players "
            "WHERE guild_id=? AND notified_done=0 AND trial_end_utc <= ?",
            (guild_id, now_iso),
        ) as cur:
            return await cur.fetchall()

async def mark_notified(guild_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE players SET notified_done=1, notified_at_utc=? WHERE guild_id=? AND user_id=?",
            (datetime.now(timezone.utc).isoformat(), guild_id, user_id),
        )
        await db.commit()

async def fetch_all_discord_players(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, added_at_utc, trial_end_utc FROM players WHERE guild_id=? ORDER BY added_at_utc ASC",
            (guild_id,),
        ) as cur:
            return await cur.fetchall()

async def fetch_all_external_players(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name, added_at_utc, trial_end_utc FROM players_external WHERE guild_id=? ORDER BY added_at_utc ASC",
            (guild_id,),
        ) as cur:
            return await cur.fetchall()
        
async def upsert_notes_external(
    guild_id: int,
    name_key: str,
    name_display: str,
    characters_level: str,
    prev_guild_alliance: str,
    optimized: str,
    content_preference: str,
    objectives: str,
    age: str,
    contribution: str,
):
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO player_notes_external (guild_id, name_key, name, characters_level, prev_guild_alliance, optimized, "
            "content_preference, objectives, age, contribution, updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, name_key) DO UPDATE SET "
            "name=excluded.name, "
            "characters_level=excluded.characters_level, "
            "prev_guild_alliance=excluded.prev_guild_alliance, "
            "optimized=excluded.optimized, "
            "content_preference=excluded.content_preference, "
            "objectives=excluded.objectives, "
            "age=excluded.age, "
            "contribution=excluded.contribution, "
            "updated_at_utc=excluded.updated_at_utc",
            (
                guild_id, name_key, name_display,
                characters_level, prev_guild_alliance, optimized,
                content_preference, objectives, age, contribution,
                now_iso,
            ),
        )
        await db.commit()

async def update_optional_notes_external(guild_id: int, name_key: str, age: str, contribution: str):
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE player_notes_external SET age=?, contribution=?, updated_at_utc=? "
            "WHERE guild_id=? AND name_key=?",
            (age, contribution, now_iso, guild_id, name_key),
        )
        await db.commit()

async def get_notes_external(guild_id: int, name_key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name, characters_level, prev_guild_alliance, optimized, content_preference, "
            "objectives, age, contribution, updated_at_utc "
            "FROM player_notes_external WHERE guild_id=? AND name_key=?",
            (guild_id, name_key),
        ) as cur:
            return await cur.fetchone()

async def delete_notes_external(guild_id: int, name_key: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM player_notes_external WHERE guild_id=? AND name_key=?",
            (guild_id, name_key),
        )
        changes = db.total_changes
        await db.commit()
    return changes

# ---- Notes DB ----
async def upsert_notes(
    guild_id: int,
    user_id: int,
    characters_level: str,
    prev_guild_alliance: str,
    optimized: str,
    content_preference: str,
    objectives: str,
    age: str,
    contribution: str,
):
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO player_notes (guild_id, user_id, characters_level, prev_guild_alliance, optimized, "
            "content_preference, objectives, age, contribution, updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "characters_level=excluded.characters_level, "
            "prev_guild_alliance=excluded.prev_guild_alliance, "
            "optimized=excluded.optimized, "
            "content_preference=excluded.content_preference, "
            "objectives=excluded.objectives, "
            "age=excluded.age, "
            "contribution=excluded.contribution, "
            "updated_at_utc=excluded.updated_at_utc",
            (
                guild_id, user_id,
                characters_level, prev_guild_alliance, optimized,
                content_preference, objectives, age, contribution,
                now_iso,
            ),
        )
        await db.commit()

async def update_optional_notes(guild_id: int, user_id: int, age: str, contribution: str):
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE player_notes SET age=?, contribution=?, updated_at_utc=? "
            "WHERE guild_id=? AND user_id=?",
            (age, contribution, now_iso, guild_id, user_id),
        )
        await db.commit()

async def get_notes(guild_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT characters_level, prev_guild_alliance, optimized, content_preference, "
            "objectives, age, contribution, updated_at_utc "
            "FROM player_notes WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ) as cur:
            return await cur.fetchone()
        
async def add_player_by_name(guild_id: int, name: str) -> tuple[bool, str]:
    norm = _normalize_name(name)
    if not norm:
        return (False, "Nom invalide (vide).")
    now_utc = datetime.now(timezone.utc)
    trial_end = now_utc + timedelta(days=14)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM players_external WHERE guild_id=? AND name_key=?",
            (guild_id, norm),
        ) as cur:
            if await cur.fetchone():
                return (False, "Ce nom existe déjà dans la liste. Choisis un autre nom.")
        await db.execute(
            "INSERT INTO players_external (guild_id, name, name_key, added_at_utc, trial_end_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, name.strip(), norm, now_utc.isoformat(), trial_end.isoformat()),
        )
        await db.commit()
    return (True, "Entrée ajoutée avec succès.")

async def fetch_due_trials_external(guild_id: int):
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name, added_at_utc, trial_end_utc FROM players_external "
            "WHERE guild_id=? AND notified_done=0 AND trial_end_utc <= ?",
            (guild_id, now_iso),
        ) as cur:
            return await cur.fetchall()

async def mark_notified_external(guild_id: int, name_key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE players_external SET notified_done=1, notified_at_utc=? "
            "WHERE guild_id=? AND name_key=?",
            (datetime.now(timezone.utc).isoformat(), guild_id, name_key),
        )
        await db.commit()

# ---------- Utils ----------
def fmt_user(user: discord.abc.User):
    return f"{user.mention} ({user.name})"

def humanize_timedelta(delta: timedelta) -> str:
    total_seconds = int(abs(delta.total_seconds()))
    minutes, _ = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days: parts.append(f"{days}j")
    if hours: parts.append(f"{hours}h")
    if minutes and not days: parts.append(f"{minutes}m")
    if not parts: parts.append("0m")
    return " ".join(parts)

def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _normalize_name(name: str) -> str:
    return " ".join(name.strip().split()).lower()

def _status_and_delta(added_iso: str, end_iso: str) -> tuple[str, str]:
    added = parse_iso(added_iso)
    end = parse_iso(end_iso)
    now = datetime.now(timezone.utc)
    if end > now:
        status = "🟡 En essai"
        remaining = humanize_timedelta(end - now)
        delta = f"reste {remaining}"
    else:
        status = "✅ Terminé"
        ended_for = humanize_timedelta(now - end)
        delta = f"terminé depuis {ended_for}"
    since = humanize_timedelta(now - added)
    return status, f"ajouté il y a {since}, {delta}"

def _chunk(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

def lead_only():
    async def predicate(inter: discord.Interaction) -> bool:
        if inter.guild is None or not isinstance(inter.user, discord.Member):
            return False
        return any(r.id == LEAD_ROLE_ID for r in inter.user.roles)
    return app_commands.check(predicate)

# ---------- UI: Modal & View ----------
class PlayerNotesModal(discord.ui.Modal, title="Notes du membre"):
    def __init__(self, target_member_id: int):
        super().__init__(timeout=300)
        self.target_member_id = target_member_id

        self.characters_level = discord.ui.TextInput(
            label="Combien de perso / LVL",
            placeholder="Ex: 3 persos / 200, 199, 180...",
            style=discord.TextStyle.short,
            required=True,
            max_length=200,
        )
        self.prev_guild_alliance = discord.ui.TextInput(
            label="Ancienne guilde / alliance",
            placeholder="Ex: Guilde X / Alliance Y",
            style=discord.TextStyle.short,
            required=False,
            max_length=200,
        )
        self.optimized = discord.ui.TextInput(
            label="Opti ou pas",
            placeholder="Ex: Opti PvP, opti PvM, en cours...",
            style=discord.TextStyle.short,
            required=False,
            max_length=200,
        )
        self.content_preference = discord.ui.TextInput(
            label="Préférence de contenu (PvP ou PvM)",
            placeholder="Ex: Koli, AvA, donjons, succès, farm...",
            style=discord.TextStyle.short,
            required=True,
            max_length=200,
        )
        self.objectives = discord.ui.TextInput(
            label="Objectifs / projets à venir",
            placeholder="Ex: Monter team, AvA régulier, succès...",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000,
        )

        self.add_item(self.characters_level)
        self.add_item(self.prev_guild_alliance)
        self.add_item(self.optimized)
        self.add_item(self.content_preference)
        self.add_item(self.objectives)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Erreur: serveur introuvable.", ephemeral=True)
            return

        # On upsert les champs principaux, on laisse les optionnels vides pour l’instant
        await upsert_notes(
            guild.id,
            self.target_member_id,
            self.characters_level.value.strip(),
            self.prev_guild_alliance.value.strip(),
            self.optimized.value.strip(),
            self.content_preference.value.strip(),
            self.objectives.value.strip(),
            age="",  # optionnel non renseigné
            contribution="",  # optionnel non renseigné
        )

        # Réponse + bouton pour compléter les infos optionnelles
        view = OptionalNotesCTAView(self.target_member_id)
        await interaction.response.send_message(
            "✅ Notes enregistrées.\n"
            "Souhaites-tu ajouter les **informations optionnelles** (Âge, Ce que tu amèneras) ?",
            view=view, ephemeral=True
        )

class OptionalNotesCTAView(discord.ui.View):
    def __init__(self, target_member_id: int):
        super().__init__(timeout=180)
        self.target_member_id = target_member_id

    @discord.ui.button(label="Ajouter infos optionnelles", style=discord.ButtonStyle.secondary)
    async def open_optional_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check permission (même règle que la saisie principale)
        perms = interaction.user.guild_permissions if interaction.user and interaction.guild else None
        if not perms or not perms.manage_guild:
            await interaction.response.send_message(
                "⛔ Tu dois avoir la permission **Gérer le serveur** pour ajouter ces infos.", ephemeral=True
            )
            return
        await interaction.response.send_modal(OptionalNotesModal(self.target_member_id))


class OptionalNotesModal(discord.ui.Modal, title="Infos optionnelles"):
    def __init__(self, target_member_id: int):
        super().__init__(timeout=300)
        self.target_member_id = target_member_id

        self.age = discord.ui.TextInput(
            label="Âge (optionnel)",
            placeholder="Ex: 23",
            style=discord.TextStyle.short,
            required=False,
            max_length=10,
        )
        self.contribution = discord.ui.TextInput(
            label="Ce que tu amèneras à la guilde (optionnel)",
            placeholder="Ex: Organisation d’events, shotcaller, crafts, coaching...",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
        )

        self.add_item(self.age)
        self.add_item(self.contribution)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Erreur: serveur introuvable.", ephemeral=True)
            return

        await update_optional_notes(
            guild.id,
            self.target_member_id,
            self.age.value.strip(),
            self.contribution.value.strip(),
        )
        await interaction.response.send_message("✅ Infos optionnelles enregistrées.", ephemeral=True)

class NotesView(discord.ui.View):
    def __init__(self, target_member: discord.Member):
        super().__init__(timeout=300)
        self.target_member = target_member

    @discord.ui.button(label="Remplir le formulaire de notes", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        perms = interaction.user.guild_permissions if interaction.user and interaction.guild else None
        if not perms or not perms.manage_guild:
            await interaction.response.send_message(
                "⛔ Tu dois avoir la permission **Gérer le serveur** pour remplir ce formulaire.", ephemeral=True
            )
            return
        await interaction.response.send_modal(PlayerNotesModal(self.target_member.id))

class ListPaginator(discord.ui.View):
    def __init__(self, pages: list[discord.Embed]):
        super().__init__(timeout=300)
        self.pages = pages
        self.index = 0
        # Désactiver Prev au début si 1re page
        self._sync_buttons_state()

    def _sync_buttons_state(self):
        # active/désactive en fonction de la page
        self.prev_button.disabled = (self.index == 0)
        self.next_button.disabled = (self.index >= len(self.pages) - 1)

    async def send(self, ctx: commands.Context):
        if not self.pages:
            await ctx.reply("Aucun inscrit dans la liste pour ce serveur.")
            return
        msg = await ctx.reply(embed=self.pages[self.index], view=self)
        self.message = msg

    @discord.ui.button(label="◀ Précédent", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != interaction.message.interaction_user if hasattr(interaction.message, "interaction_user") else False:
            # On ne restreint pas : tout le monde peut paginer (ou ajoute un check si tu veux)
            pass
        if self.index > 0:
            self.index -= 1
            self._sync_buttons_state()
            await interaction.response.edit_message(embed=self.pages[self.index], view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Suivant ▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index < len(self.pages) - 1:
            self.index += 1
            self._sync_buttons_state()
            await interaction.response.edit_message(embed=self.pages[self.index], view=self)
        else:
            await interaction.response.defer()

class PlayerNotesModalExternal(discord.ui.Modal, title="Notes (sans mention)"):
    def __init__(self, guild_id: int, name_display: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.name_display = name_display.strip()
        self.name_key = _normalize_name(self.name_display)

        self.characters_level = discord.ui.TextInput(
            label="Combien de perso / LVL",
            placeholder="Ex: 3 persos / 200, 199, 180...",
            style=discord.TextStyle.short,
            required=True, max_length=200,
        )
        self.prev_guild_alliance = discord.ui.TextInput(
            label="Ancienne guilde / alliance",
            placeholder="Ex: Guilde X / Alliance Y",
            style=discord.TextStyle.short,
            required=False, max_length=200,
        )
        self.optimized = discord.ui.TextInput(
            label="Opti ou pas",
            placeholder="Ex: Opti PvP, opti PvM, en cours...",
            style=discord.TextStyle.short,
            required=False, max_length=200,
        )
        self.content_preference = discord.ui.TextInput(
            label="Préférence de contenu (PvP ou PvM)",
            placeholder="Ex: Koli, AvA, donjons...",
            style=discord.TextStyle.short,
            required=True, max_length=200,
        )
        self.objectives = discord.ui.TextInput(
            label="Objectifs / projets à venir",
            placeholder="Ex: Monter team, AvA régulier, succès...",
            style=discord.TextStyle.paragraph,
            required=True, max_length=1000,
        )

        self.add_item(self.characters_level)
        self.add_item(self.prev_guild_alliance)
        self.add_item(self.optimized)
        self.add_item(self.content_preference)
        self.add_item(self.objectives)

    async def on_submit(self, interaction: discord.Interaction):
        # Optionnel: vérifier que le nom existe dans players_external
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT 1 FROM players_external WHERE guild_id=? AND name_key=?",
                (self.guild_id, self.name_key),
            ) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(
                        "❌ Ce nom n'est pas dans la liste. Ajoute-le d'abord avec `/enter name:<nom>`.", ephemeral=True
                    )
                    return

        await upsert_notes_external(
            self.guild_id,
            self.name_key,
            self.name_display,
            self.characters_level.value.strip(),
            self.prev_guild_alliance.value.strip(),
            self.optimized.value.strip(),
            self.content_preference.value.strip(),
            self.objectives.value.strip(),
            age="",
            contribution="",
        )
        view = OptionalNotesCTAViewExternal(self.guild_id, self.name_display)
        await interaction.response.send_message(
            f"✅ Notes enregistrées pour **{self.name_display}**.\n"
            "Ajouter les **infos optionnelles** (Âge, Apport) ?",
            view=view, ephemeral=True
        )

class OptionalNotesCTAViewExternal(discord.ui.View):
    def __init__(self, guild_id: int, name_display: str):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.name_display = name_display.strip()

    @discord.ui.button(label="Ajouter infos optionnelles", style=discord.ButtonStyle.secondary)
    async def open_optional(self, interaction: discord.Interaction, button: discord.ui.Button):
        perms = interaction.user.guild_permissions if interaction.user and interaction.guild else None
        if not perms or not perms.manage_guild:
            await interaction.response.send_message(
                "⛔ Permission **Gérer le serveur** requise.", ephemeral=True
            )
            return
        await interaction.response.send_modal(OptionalNotesModalExternal(self.guild_id, self.name_display))

class OptionalNotesModalExternal(discord.ui.Modal, title="Infos optionnelles (sans mention)"):
    def __init__(self, guild_id: int, name_display: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.name_display = name_display.strip()
        self.name_key = _normalize_name(self.name_display)

        self.age = discord.ui.TextInput(
            label="Âge (optionnel)",
            placeholder="Ex: 23", style=discord.TextStyle.short,
            required=False, max_length=10,
        )
        self.contribution = discord.ui.TextInput(
            label="Apport à la guilde (optionnel)",
            placeholder="Ex: orga events, crafts, coaching...",
            style=discord.TextStyle.paragraph,
            required=False, max_length=1000,
        )
        self.add_item(self.age)
        self.add_item(self.contribution)

    async def on_submit(self, interaction: discord.Interaction):
        await update_optional_notes_external(
            self.guild_id, self.name_key,
            self.age.value.strip(), self.contribution.value.strip()
        )
        await interaction.response.send_message("✅ Infos optionnelles enregistrées.", ephemeral=True)

class NotesViewExternal(discord.ui.View):
    def __init__(self, guild_id: int, name_display: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.name_display = name_display.strip()

    @discord.ui.button(label="Remplir le formulaire de notes", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        perms = interaction.user.guild_permissions if interaction.user and interaction.guild else None
        if not perms or not perms.manage_guild:
            await interaction.response.send_message(
                "⛔ Permission **Gérer le serveur** requise.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            PlayerNotesModalExternal(self.guild_id, self.name_display)
        )

# ---------- Events ----------
@bot.event
async def on_ready():
    await init_db()
    print(f"Connecté en tant que {bot.user} (id={bot.user.id})")
    if not trial_checker.is_running():
        trial_checker.start()
    print("Prêt.")

# ---------- Commandes with slashs ----------
@bot.tree.command(name="checkpseudo", description="Génère le lien du profil Ankama à partir d'un pseudo (ex: pseudo#9999)")
async def check_pseudo(interaction: discord.Interaction, pseudo: str):
    pseudo = pseudo.strip()
    if not pseudo or "#" not in pseudo:
        await interaction.response.send_message(
            "❌ Format invalide. Utilise la commande ainsi : `/checkpseudo pseudo#9999`",
            ephemeral=True
        )
        return

    safe_pseudo = pseudo.replace("#", "-")
    url = f"https://account.ankama.com/fr/profil-ankama/{safe_pseudo}"
    await interaction.response.send_message(f"🔗 Profil Ankama : <{url}>")

@lead_only()
@bot.tree.command(name="sync", description="Resynchronise les commandes slash (owner only)")
async def sync_cmd(interaction: discord.Interaction):
    app_owner = (await bot.application_info()).owner
    if interaction.user.id != app_owner.id:
        await interaction.response.send_message("⛔ Réservé au propriétaire de l'application.", ephemeral=True)
        return
    if interaction.guild:
        await bot.tree.sync(guild=interaction.guild)
        await interaction.response.send_message(f"✅ Sync effectuée pour la guilde {interaction.guild.id}", ephemeral=True)
    else:
        await bot.tree.sync()
        await interaction.response.send_message("✅ Sync globale déclenchée.", ephemeral=True)

@lead_only()
@bot.tree.command(name="enter", description="Ajoute un joueur (mention ou nom texte).")
@app_commands.describe(member="Membre à ajouter (optionnel)", name="Nom texte si pas de mention (optionnel)")
async def enter_unified(interaction: discord.Interaction, member: discord.Member | None = None, name: str | None = None):
    if interaction.guild is None:
        await interaction.response.send_message("❌ À utiliser dans un serveur.", ephemeral=True)
        return

    if member is not None:
        created, msg = await add_player_to_db(interaction.guild.id, member.id)
        text = f"✅ {fmt_user(member)} ajouté (14j)." if created else f"⚠️ {msg}"
        await interaction.response.send_message(text)
        return

    if name and name.strip():
        created, msg = await add_player_by_name(interaction.guild.id, name)
        text = f"✅ **{name.strip()}** ajouté (14j)." if created else f"⚠️ {msg}"
        await interaction.response.send_message(text)
        return

    await interaction.response.send_message("❌ Usage : `/enter member:@membre` ou `/enter name:<nom>`", ephemeral=True)

@lead_only()
@bot.tree.command(name="check", description="Vérifie la période d’essai d’un membre Discord.")
@app_commands.describe(member="Membre à vérifier")
async def check_member(interaction: discord.Interaction, member: discord.Member):
    row = await get_player_row(interaction.guild.id, member.id) if interaction.guild else None
    if not row:
        await interaction.response.send_message(f"ℹ️ {fmt_user(member)} n'est **pas** dans la liste.")
        return

    added_at_utc = parse_iso(row[0])
    trial_end_utc = parse_iso(row[1])
    now_utc = datetime.now(timezone.utc)

    since = now_utc - added_at_utc
    remaining = trial_end_utc - now_utc
    status = "✅ **Période d’essai terminée**" if remaining.total_seconds() <= 0 else "🟡 **En période d’essai**"
    txt_since = humanize_timedelta(since)

    if remaining.total_seconds() > 0:
        txt_remaining = humanize_timedelta(remaining)
        await interaction.response.send_message(
            f"👤 {fmt_user(member)}\n"
            f"- Ajouté il y a **{txt_since}** (UTC: {added_at_utc.strftime('%Y-%m-%d %H:%M')})\n"
            f"- Fin d’essai dans **{txt_remaining}** (UTC: {trial_end_utc.strftime('%Y-%m-%d %H:%M')})\n"
            f"- Statut: {status}"
        )
    else:
        ended_for = humanize_timedelta(-remaining)
        await interaction.response.send_message(
            f"👤 {fmt_user(member)}\n"
            f"- Ajouté il y a **{txt_since}** (UTC: {added_at_utc.strftime('%Y-%m-%d %H:%M')})\n"
            f"- Période d’essai terminée depuis **{ended_for}** (UTC: {trial_end_utc.strftime('%Y-%m-%d %H:%M')})\n"
            f"- Statut: {status}"
        )

@lead_only()
@bot.tree.command(name="leave", description="Retire un membre Discord de la liste.")
@app_commands.describe(member="Membre à retirer")
async def leave_member(interaction: discord.Interaction, member: discord.Member):
    removed = await remove_player_from_db(interaction.guild.id, member.id)
    if removed:
        await interaction.response.send_message(f"🗑️ {fmt_user(member)} a été retiré de la liste.")
    else:
        await interaction.response.send_message(f"ℹ️ {fmt_user(member)} n’était pas dans la liste.")

@lead_only()
@bot.tree.command(name="list", description="Affiche la liste complète (Discord + noms).")
async def list_all(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Cette commande doit être utilisée dans un serveur.", ephemeral=True)
        return

    disc_rows = await fetch_all_discord_players(guild.id)
    ext_rows  = await fetch_all_external_players(guild.id)

    en_essai_lines, termines_lines = [], []

    for user_id, added_iso, end_iso in disc_rows:
        member = guild.get_member(user_id)
        display = member.mention if member else f"<@{user_id}>"
        status, delta = _status_and_delta(added_iso, end_iso)
        line = f"{display} — {status} — {delta}"
        (en_essai_lines if "En essai" in status else termines_lines).append(line)

    for name, added_iso, end_iso in ext_rows:
        display = f"**{name}**"
        status, delta = _status_and_delta(added_iso, end_iso)
        line = f"{display} — {status} — {delta}"
        (en_essai_lines if "En essai" in status else termines_lines).append(line)

    if not en_essai_lines and not termines_lines:
        await interaction.response.send_message("Aucun inscrit dans la liste pour ce serveur.")
        return

    def build_pages():
        full_lines = []
        if en_essai_lines:
            full_lines.append("**🟡 En période d’essai**")
            full_lines.extend(f"- {l}" for l in en_essai_lines)
        if termines_lines:
            if full_lines: full_lines.append("")
            full_lines.append("**✅ Période d’essai terminée**")
            full_lines.extend(f"- {l}" for l in termines_lines)

        chunks = list(_chunk(full_lines, 20))
        pages = []
        total = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            embed = discord.Embed(
                title=f"📋 Liste complète — {guild.name}",
                description="\n".join(chunk),
                color=discord.Color.teal(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text=f"Page {i}/{total}")
            pages.append(embed)
        return pages

    pages = build_pages()
    view = ListPaginator(pages)
    await interaction.response.send_message(embed=pages[0], view=view)

@lead_only()
@bot.tree.command(name="note", description="Ouvre le formulaire de notes (mention ou nom).")
@app_commands.describe(member="Membre (optionnel)", name="Nom texte (optionnel)")
async def note_form(interaction: discord.Interaction, member: discord.Member | None = None, name: str | None = None):
    if interaction.guild is None:
        await interaction.response.send_message("❌ À utiliser dans un serveur.", ephemeral=True)
        return

    if member:
        view = NotesView(member)
        await interaction.response.send_message(
            f"📝 Formulaire de notes pour {fmt_user(member)} — clique ci-dessous.",
            view=view, ephemeral=True
        )
        return

    if name and name.strip():
        name_key = _normalize_name(name)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT 1 FROM players_external WHERE guild_id=? AND name_key=?",
                (interaction.guild.id, name_key),
            ) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(
                        "❌ Ce nom n'est pas dans la liste. Ajoute-le d'abord avec `/enter name:<nom>`",
                        ephemeral=True
                    )
                    return
        view = NotesViewExternal(interaction.guild.id, name.strip())
        await interaction.response.send_message(
            f"📝 Formulaire de notes pour **{name.strip()}** — clique ci-dessous.",
            view=view, ephemeral=True
        )
        return

    await interaction.response.send_message("❌ Usage : `/note member:@membre` ou `/note name:<nom>`", ephemeral=True)

@lead_only()
@bot.tree.command(name="notes", description="Affiche les notes (mention ou nom).")
@app_commands.describe(member="Membre (optionnel)", name="Nom texte (optionnel)")
async def notes_show(interaction: discord.Interaction, member: discord.Member | None = None, name: str | None = None):
    def val(x): return x if (x and str(x).strip()) else "—"

    if member:
        row = await get_notes(interaction.guild.id, member.id)
        if not row:
            await interaction.response.send_message(f"ℹ️ Aucune note trouvée pour {fmt_user(member)}.")
            return
        (characters_level, prev_guild_alliance, optimized, content_preference,
         objectives, age, contribution, updated_at_iso) = row
        updated = parse_iso(updated_at_iso).strftime("%Y-%m-%d %H:%M UTC")
        embed = discord.Embed(
            title=f"Notes — {member.display_name}",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Combien de perso / LVL", value=val(characters_level), inline=False)
        embed.add_field(name="Ancienne guilde / alliance", value=val(prev_guild_alliance), inline=False)
        embed.add_field(name="Opti ou pas", value=val(optimized), inline=False)
        embed.add_field(name="Préférence PvP / PvM", value=val(content_preference), inline=False)
        embed.add_field(name="Objectifs / projets", value=val(objectives), inline=False)
        embed.add_field(name="Âge (optionnel)", value=val(age), inline=True)
        embed.add_field(name="Apport à la guilde (optionnel)", value=val(contribution), inline=False)
        embed.set_footer(text=f"Dernière mise à jour: {updated}")
        await interaction.response.send_message(embed=embed)
        return

    if name and name.strip():
        name_display = name.strip()
        name_key = _normalize_name(name_display)
        row = await get_notes_external(interaction.guild.id, name_key)
        if not row:
            await interaction.response.send_message(f"ℹ️ Aucune note trouvée pour **{name_display}**.")
            return
        (stored_name, characters_level, prev_guild_alliance, optimized, content_preference,
         objectives, age, contribution, updated_at_iso) = row
        updated = parse_iso(updated_at_iso).strftime("%Y-%m-%d %H:%M UTC")
        embed = discord.Embed(
            title=f"Notes — {stored_name}",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Combien de perso / LVL", value=val(characters_level), inline=False)
        embed.add_field(name="Ancienne guilde / alliance", value=val(prev_guild_alliance), inline=False)
        embed.add_field(name="Opti ou pas", value=val(optimized), inline=False)
        embed.add_field(name="Préférence PvP / PvM", value=val(content_preference), inline=False)
        embed.add_field(name="Objectifs / projets", value=val(objectives), inline=False)
        embed.add_field(name="Âge (optionnel)", value=val(age), inline=True)
        embed.add_field(name="Apport à la guilde (optionnel)", value=val(contribution), inline=False)
        embed.set_footer(text=f"Dernière mise à jour: {updated}")
        await interaction.response.send_message(embed=embed)
        return

    await interaction.response.send_message("❌ Usage : `/notes member:@membre` ou `/notes name:<nom>`", ephemeral=True)

@lead_only()
@bot.tree.command(name="delnotes", description="Supprime les notes (mention ou nom).")
@app_commands.describe(member="Membre (optionnel)", name="Nom texte (optionnel)")
async def delnotes(interaction: discord.Interaction, member: discord.Member | None = None, name: str | None = None):
    if member:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM player_notes WHERE guild_id=? AND user_id=?",
                (interaction.guild.id, member.id),
            )
            changes = db.total_changes
            await db.commit()
        if changes > 0:
            await interaction.response.send_message(f"🗑️ Notes supprimées pour {fmt_user(member)}.")
        else:
            await interaction.response.send_message(f"ℹ️ Aucune note à supprimer pour {fmt_user(member)}.")
        return

    if name and name.strip():
        name_key = _normalize_name(name)
        changes = await delete_notes_external(interaction.guild.id, name_key)
        if changes > 0:
            await interaction.response.send_message(f"🗑️ Notes supprimées pour **{name.strip()}**.")
        else:
            await interaction.response.send_message(f"ℹ️ Aucune note à supprimer pour **{name.strip()}**.")
        return

    await interaction.response.send_message("❌ Usage : `/delnotes member:@membre` ou `/delnotes name:<nom>`", ephemeral=True)

@lead_only()
@bot.tree.command(name="settrialchannel", description="Définit le salon des rappels J+14.")
@app_commands.describe(channel="Salon des rappels (laisser vide pour le salon courant)")
async def set_trial_channel_cmd(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if interaction.guild is None:
        await interaction.response.send_message("❌ À utiliser dans un serveur.", ephemeral=True)
        return
    target = channel or interaction.channel
    await set_trial_channel(interaction.guild.id, target.id)
    await interaction.response.send_message(f"🛠️ Salon des rappels défini sur {target.mention}")

@bot.tree.command(name="gettrialchannel", description="Affiche le salon des rappels J+14.")
async def get_trial_channel_cmd(interaction: discord.Interaction):
    channel_id = await get_trial_channel_id(interaction.guild.id)
    if channel_id:
        ch = interaction.guild.get_channel(channel_id)
        await interaction.response.send_message(f"Salon des rappels: {ch.mention if ch else f'#{channel_id} (introuvable)'}")
    else:
        await interaction.response.send_message("Aucun salon de rappel configuré. Utilise `/settrialchannel`.")

async def autocomplete_external_names(interaction: discord.Interaction, current: str):
    # Retourne une liste de app_commands.Choice[str]
    guild_id = interaction.guild_id
    results = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name FROM players_external WHERE guild_id=? AND name LIKE ? LIMIT 25",
            (guild_id, f"%{current}%")
        ) as cur:
            rows = await cur.fetchall()
            results = [app_commands.Choice(name=r[0], value=r[0]) for r in rows]
    return results

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    import traceback
    print("❌ Erreur slash command :")
    traceback.print_exception(type(error), error, error.__traceback__)
    try:
        await interaction.response.send_message("❌ Une erreur est survenue.", ephemeral=True)
    except discord.InteractionResponded:
        await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        # évite "interaction already responded"
        if interaction.response.is_done():
            await interaction.followup.send("⛔ Commande réservée aux **Leads**.", ephemeral=True)
        else:
            await interaction.response.send_message("⛔ Commande réservée aux **Leads**.", ephemeral=True)

# ---------- Rappels J+14 ----------
@tasks.loop(minutes=5.0)
async def trial_checker():
    for guild in bot.guilds:
        channel_id = await get_trial_channel_id(guild.id)
        if not channel_id:
            continue
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            continue
        perms_ok = channel.permissions_for(guild.me).send_messages if guild.me else False
        if not perms_ok:
            continue

        try:
            # 1) Membres Discord (user_id)
            due_rows = await fetch_due_trials(guild.id)
            if due_rows:
                for user_id, added_iso, trial_end_iso in due_rows:
                    member = guild.get_member(user_id)
                    mention = member.mention if member else f"<@{user_id}>"
                    added_at = parse_iso(added_iso)
                    await channel.send(
                        f"🔔 {mention} n'est plus en période d’essai "
                        f"(14 jours écoulés depuis {added_at.strftime('%Y-%m-%d')})."
                    )
                    await mark_notified(guild.id, user_id)

            # 2) Entrées par NOM (sans compte Discord)
            due_ext = await fetch_due_trials_external(guild.id)
            if due_ext:
                for name, added_iso, trial_end_iso in due_ext:
                    added_at = parse_iso(added_iso)
                    await channel.send(
                        f"🔔 **{name}** n'est plus en période d’essai "
                        f"(14 jours écoulés depuis {added_at.strftime('%Y-%m-%d')})."
                    )
                    # Marquer notifié via name_key normalisé
                    await mark_notified_external(guild.id, _normalize_name(name))

        except Exception as e:
            print(f"[trial_checker] Erreur sur guild {guild.id}: {e}")

@trial_checker.before_loop
async def before_trial_checker():
    await bot.wait_until_ready()

# ---------- Run ----------
if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)


