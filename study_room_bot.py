import discord
from discord.ext import commands
from discord import app_commands
import asyncio
from datetime import datetime, timedelta
import sqlite3
import matplotlib.pyplot as plt
import io
import pandas as pd
import os

# --- Configuration ---
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN") 
if not BOT_TOKEN:
    print("FATAL ERROR: DISCORD_BOT_TOKEN environment variable not set. The bot cannot start.")
    exit()

DB_NAME = "study_data.db"

intents = discord.Intents.default()
intents.members = True 
intents.message_content = True 
intents.voice_states = True 

# --- Bot Class ---
class StudyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        self.db_conn = None
        self.db_cursor = None
        self.active_rooms = {}
        self.owner_to_channel = {}
        self.setup_db()

    def setup_db(self):
        try:
            self.db_conn = sqlite3.connect(DB_NAME)
            self.db_cursor = self.db_conn.cursor()
            self.db_cursor.execute('''
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    partner_id INTEGER,
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    duration_seconds INTEGER,
                    topic TEXT
                )
            ''')
            self.db_conn.commit()
            print("SQLite database setup complete.")
        except Exception as e:
            print(f"Error setting up database: {e}")

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        await self.tree.sync()
        print("Slash commands synced.")

    # --- Internal Helpers (unchanged) ---
    async def _log_session(self, owner_id: int, partner_id: int, start_time: datetime, duration_seconds: int, topic: str):
        try:
            end_time = start_time + timedelta(seconds=duration_seconds)
            self.db_cursor.execute('''
                INSERT INTO sessions (user_id, partner_id, start_time, end_time, duration_seconds, topic)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                owner_id, 
                partner_id, 
                start_time.isoformat(), 
                end_time.isoformat(), 
                duration_seconds,
                topic
            ))
            self.db_conn.commit()
        except Exception as e:
            print(f"Error logging session to DB: {e}")

    async def _perform_cleanup(self, channel_id: int, reason: str, duration_seconds: int = 0, topic: str = "N/A"):
        room_data = self.active_rooms.pop(channel_id, None)
        if not room_data:
            return
        owner_id = room_data['owner_id']
        partner_id = room_data['partner_id']
        start_time = room_data['start_time']
        if duration_seconds == 0:
            duration_seconds = int((datetime.now() - start_time).total_seconds())
        await self._log_session(owner_id, partner_id, start_time, duration_seconds, topic)
        self.owner_to_channel.pop(owner_id, None)
        guild = self.get_channel(channel_id).guild
        if not guild: return
        channel = self.get_channel(channel_id)
        role = guild.get_role(room_data['role_id'])
        if 'timer_task' in room_data:
            room_data['timer_task'].cancel()
        if role:
            try:
                await role.delete(reason=f"Cleanup: {reason}")
            except Exception as e:
                print(f"Failed to delete role {role.id}: {e}")
        if channel:
            try:
                await channel.delete(reason=f"Cleanup: {reason}")
            except Exception as e:
                print(f"Failed to delete channel {channel_id}: {e}")
        print(f"Cleanup complete for channel {channel_id}. Reason: {reason} (Duration: {duration_seconds}s)")

    async def cleanup_room_timer(self, channel_id: int, duration_minutes: int, topic: str):
        try:
            await asyncio.sleep(duration_minutes * 60)
            await self._perform_cleanup(
                channel_id, 
                f"Timed session expired ({duration_minutes} minutes).",
                duration_seconds=duration_minutes * 60,
                topic=topic
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Error in cleanup timer: {e}")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if before.channel and before.channel.id in self.active_rooms and after.channel != before.channel:
            human_members = [m for m in before.channel.members if not m.bot]
            if not human_members:
                room_data = self.active_rooms.get(before.channel.id, {})
                await self._perform_cleanup(
                    before.channel.id, 
                    f"Study room auto-deleted because it became empty after {member.name} left.",
                    topic=room_data.get('topic', 'N/A')
                )

# --- Cog for Slash Commands ---
class StudyCog(commands.Cog):
    def __init__(self, bot: StudyBot):
        self.bot = bot

    @app_commands.command(name="bookroom", description="Book a private study room with a partner for a specified duration.")
    @app_commands.describe(
        topic="A short name for the study session (e.g., 'Calculus-Review')",
        partner="The study partner to invite (mention their @username)",
        duration_minutes="The length of the session in minutes (1 to 360 max)"
    )
    async def bookroom(self, interaction: discord.Interaction, topic: str, partner: discord.Member, duration_minutes: app_commands.Range[int, 1, 360]):
        await self.bot.bookroom(interaction, topic, partner, duration_minutes)

    @app_commands.command(name="studystats", description="See your total study hours recorded by the bot.")
    async def studystats(self, interaction: discord.Interaction):
        await self.bot.studystats(interaction)

    @app_commands.command(name="weeklyreport", description="Generates and uploads a graph of study hours for the past week.")
    async def weeklyreport(self, interaction: discord.Interaction):
        await self.bot.weeklyreport(interaction)

# --- Add bot methods for calling commands from Cog ---
# These are the original implementations copied from your class for simplicity
# (bookroom, studystats, weeklyreport methods inside StudyBot remain unchanged from your main code)

# --- Bot Setup ---
bot = StudyBot()
bot.add_cog(StudyCog(bot))

if BOT_TOKEN:
    try:
        bot.run(BOT_TOKEN)
    except discord.errors.LoginFailure:
        print("\nERROR: Invalid bot token. Please check the DISCORD_BOT_TOKEN environment variable.")
