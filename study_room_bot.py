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
GUILD_ID = 123456789012345678  # Replace with your server ID for testing

if not BOT_TOKEN:
    print("FATAL ERROR: DISCORD_BOT_TOKEN environment variable not set. The bot cannot start.")
    exit()

DB_NAME = "study_data.db"

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True

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
        guild = discord.Object(id=GUILD_ID)
        await self.tree.sync(guild=guild)
        print(f"Slash commands synced for guild {GUILD_ID}")
        # Uncomment the next lines when ready for global sync
        # await self.tree.sync()
        # print("Slash commands synced globally")

    # --- Session logging & cleanup functions ---
    async def _log_session(self, owner_id, partner_id, start_time, duration_seconds, topic):
        end_time = start_time + timedelta(seconds=duration_seconds)
        self.db_cursor.execute('''
            INSERT INTO sessions (user_id, partner_id, start_time, end_time, duration_seconds, topic)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (owner_id, partner_id, start_time.isoformat(), end_time.isoformat(), duration_seconds, topic))
        self.db_conn.commit()

    async def _perform_cleanup(self, channel_id, reason, duration_seconds=0, topic="N/A"):
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
            try: await role.delete(reason=f"Cleanup: {reason}")
            except: pass
        if channel:
            try: await channel.delete(reason=f"Cleanup: {reason}")
            except: pass
        print(f"Cleanup complete for channel {channel_id}. Reason: {reason} (Duration: {duration_seconds}s)")

    async def cleanup_room_timer(self, channel_id, duration_minutes, topic):
        try:
            await asyncio.sleep(duration_minutes * 60)
            await self._perform_cleanup(channel_id, f"Timed session expired ({duration_minutes} minutes).",
                                        duration_seconds=duration_minutes * 60, topic=topic)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Error in cleanup timer: {e}")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if before.channel and before.channel.id in self.active_rooms and after.channel != before.channel:
            human_members = [m for m in before.channel.members if not m.bot]
            if not human_members:
                room_data = self.active_rooms.get(before.channel.id, {})
                await self._perform_cleanup(before.channel.id,
                                            f"Study room auto-deleted because it became empty after {member.name} left.",
                                            topic=room_data.get('topic', 'N/A'))

    # --- Slash Commands ---
    @app_commands.command(name="bookroom", description="Book a private study room with a partner for a specified duration.")
    @app_commands.describe(topic="A short name for the session", partner="Partner to invite", duration_minutes="Length in minutes (1-360)")
    async def bookroom(self, interaction: discord.Interaction, topic: str, partner: discord.Member, duration_minutes: app_commands.Range[int, 1, 360]):
        await interaction.response.defer(thinking=True)
        if interaction.user.id in self.owner_to_channel:
            await interaction.followup.send(f"You already have an active study room.")
            return
        new_role = None
        new_channel = None
        start_time = datetime.now()
        try:
            role_name = f"Study-{interaction.user.name}-Access"
            new_role = await interaction.guild.create_role(name=role_name, mentionable=False)
            await interaction.user.add_roles(new_role)
            await partner.add_roles(new_role)
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=False),
                new_role: discord.PermissionOverwrite(connect=True, view_channel=True),
                interaction.guild.me: discord.PermissionOverwrite(connect=True, view_channel=True)
            }
            channel_name = f"🗣️study-{topic.lower().replace(' ','-')[:15]}"
            new_channel = await interaction.guild.create_voice_channel(name=channel_name, overwrites=overwrites)
            cleanup_task = self.loop.create_task(self.cleanup_room_timer(new_channel.id, duration_minutes, topic))
            self.active_rooms[new_channel.id] = {'owner_id': interaction.user.id,
                                                 'partner_id': partner.id,
                                                 'role_id': new_role.id,
                                                 'timer_task': cleanup_task,
                                                 'start_time': start_time,
                                                 'topic': topic}
            self.owner_to_channel[interaction.user.id] = new_channel.id
            end_time = start_time + timedelta(minutes=duration_minutes)
            await interaction.followup.send(f"Room booked: <#{new_channel.id}> for {duration_minutes} minutes with {partner.mention}.")
        except Exception as e:
            if new_role:
                try: await new_role.delete()
                except: pass
            if new_channel:
                try: await new_channel.delete()
                except: pass
            await interaction.followup.send(f"Error creating room: {e}")

    @app_commands.command(name="studystats", description="See your total study hours recorded by the bot.")
    async def studystats(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        user_id = interaction.user.id
        self.db_cursor.execute('SELECT SUM(duration_seconds) FROM sessions WHERE user_id=? OR partner_id=?', (user_id,user_id))
        total_seconds = self.db_cursor.fetchone()[0]
        if not total_seconds:
            await interaction.followup.send("No study sessions logged yet.")
            return
        total_hours = total_seconds / 3600
        total_minutes = (total_seconds % 3600) // 60
        await interaction.followup.send(f"Total study time: {total_hours:.1f}h {total_minutes}m.")

    @app_commands.command(name="weeklyreport", description="Generates a graph of study hours for the past week.")
    async def weeklyreport(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        one_week_ago = datetime.now() - timedelta(days=7)
        self.db_cursor.execute('SELECT user_id, start_time, duration_seconds FROM sessions WHERE start_time >= ?', (one_week_ago.isoformat(),))
        rows = self.db_cursor.fetchall()
        if not rows:
            await interaction.followup.send("No sessions logged in the last 7 days.")
            return
        data = []
        for user_id, start_time_str, duration_seconds in rows:
            start_time = datetime.fromisoformat(start_time_str)
            data.append({'user_id': user_id, 'day': start_time.strftime('%A'), 'duration_hours': duration_seconds/3600})
        df = pd.DataFrame(data)
        weekly_summary = df.groupby(['day','user_id'])['duration_hours'].sum().reset_index()
        user_names = {uid:(interaction.guild.get_member(uid).display_name if interaction.guild.get_member(uid) else f"User {uid}") for uid in weekly_summary['user_id'].unique()}
        weekly_summary['name'] = weekly_summary['user_id'].map(user_names)
        days_order = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
        fig, ax = plt.subplots(figsize=(10,6))
        plot_data = weekly_summary.pivot(index='day',columns='name',values='duration_hours').fillna(0).reindex(days_order, fill_value=0)
        plot_data.plot(kind='bar', ax=ax, rot=45, colormap='viridis')
        ax.set_title('Study Time Last 7 Days'); ax.set_xlabel('Day'); ax.set_ylabel('Hours')
        plt.tight_layout()
        buffer = io.BytesIO(); plt.savefig(buffer, format='png'); buffer.seek(0); plt.close(fig)
        await interaction.followup.send(file=discord.File(buffer, filename="weekly_report.png"))

bot = StudyBot()
bot.run(BOT_TOKEN)
