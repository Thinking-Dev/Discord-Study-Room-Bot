import discord
from discord.ext import commands
from discord import app_commands
import asyncio
from datetime import datetime, timedelta
import sqlite3
import matplotlib.pyplot as plt
import io
import pandas as pd
import os # Import the os module


# --- Configuration ---
# SECURE: Load BOT_TOKEN from the environment variable provided by the hosting service.
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN") 


# Check if the token is available
if not BOT_TOKEN:
    print("FATAL ERROR: DISCORD_BOT_TOKEN environment variable not set. The bot cannot start.")
    exit()


DB_NAME = "study_data.db"


# Define intents (necessary for modern Discord bots)
intents = discord.Intents.default()
intents.members = True 
intents.message_content = True 
intents.voice_states = True 


# Use a custom class to hold our bot logic and track active rooms
class StudyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        # Database connection and cursor
        self.db_conn = None
        self.db_cursor = None
        
        # Tracks active rooms by the channel ID, storing all necessary data.
        # Format: {channel_id: {'owner_id': int, 'role_id': int, 'timer_task': asyncio.Task, 'start_time': datetime}}
        self.active_rooms = {}
        # Reverse lookup for quick checking if a user owns a room.
        # Format: {owner_id: channel_id}
        self.owner_to_channel = {}
        
        self.setup_db()


    def setup_db(self):
        """Initializes the SQLite database and creates the sessions table."""
        try:
            # We connect to the DB and use isolation_level=None for autocommit
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
        """Called when the bot successfully connects to Discord."""
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        await self.tree.sync()  # Sync all slash commands
        print("Slash commands synced.")


    async def _log_session(self, owner_id: int, partner_id: int, start_time: datetime, duration_seconds: int, topic: str):
        """Logs a completed session to the database."""
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
        """
        Handles the actual deletion of the role, channel, removes tracking, and logs data.
        """
        room_data = self.active_rooms.pop(channel_id, None)
        if not room_data:
            return


        # Log the session duration
        owner_id = room_data['owner_id']
        partner_id = room_data['partner_id']
        start_time = room_data['start_time']
        
        # If duration_seconds is 0, calculate actual study time
        if duration_seconds == 0:
            duration_seconds = int((datetime.now() - start_time).total_seconds())


        await self._log_session(owner_id, partner_id, start_time, duration_seconds, topic)




        # Remove from reverse lookup
        self.owner_to_channel.pop(owner_id, None)


        guild = self.get_channel(channel_id).guild
        if not guild: return
        
        channel = self.get_channel(channel_id)
        role = guild.get_role(room_data['role_id'])
        
        # 1. Cancel the timer task if it's still running
        if 'timer_task' in room_data:
            room_data['timer_task'].cancel()
        
        # 2. Delete the role (revokes access) and channel
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
        """
        Asynchronous task to manage the room timer.
        """
        try:
            # Wait for the full duration
            await asyncio.sleep(duration_minutes * 60)


            # Final Cleanup, passing the full requested duration
            await self._perform_cleanup(
                channel_id, 
                f"Timed session expired ({duration_minutes} minutes).",
                duration_seconds=duration_minutes * 60,
                topic=topic
            )
        except asyncio.CancelledError:
            # This happens if the voice state update listener calls cleanup early
            pass
        except Exception as e:
            print(f"Error in cleanup timer: {e}")




    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """
        Checks if a tracked voice channel becomes empty and auto-deletes the room.
        """
        # Only check when leaving a channel that is a tracked active room
        if before.channel and before.channel.id in self.active_rooms and after.channel != before.channel:
            
            # Count non-bot members remaining in the channel
            human_members = [m for m in before.channel.members if not m.bot]
            
            if not human_members:
                # Channel is now empty. Trigger immediate cleanup.
                room_data = self.active_rooms.get(before.channel.id, {})
                
                await self._perform_cleanup(
                    before.channel.id, 
                    f"Study room auto-deleted because it became empty after {member.name} left.",
                    topic=room_data.get('topic', 'N/A')
                )


    @app_commands.command(name="bookroom", description="Book a private study room with a partner for a specified duration.")
    @app_commands.describe(
        topic="A short name for the study session (e.g., 'Calculus-Review')",
        partner="The study partner to invite (mention their @username)",
        duration_minutes="The length of the session in minutes (1 to 360 max)"
    )
    async def bookroom(self, interaction: discord.Interaction, topic: str, partner: discord.Member, duration_minutes: app_commands.Range[int, 1, 360]):
        """
        Handles the /bookroom command to create a private study voice channel and temporary role.
        """
        await interaction.response.defer(thinking=True) 


        # 1. Check if the user already has an active room
        if interaction.user.id in self.owner_to_channel:
            channel_id = self.owner_to_channel[interaction.user.id]
            await interaction.followup.send(
                f"You already have an active study room: <#{channel_id}>. Please wait for that session to end or manually cancel it."
            )
            return


        # 2. Start Room Setup
        new_role = None
        new_voice_channel = None
        start_time = datetime.now()
        
        try:
            # Create the temporary access Role (the "group")
            role_name = f"Study-{interaction.user.name}-Access"
            new_role = await interaction.guild.create_role(name=role_name, reason="Temporary study room access role.", mentionable=False)


            # Add users to the new Role
            await interaction.user.add_roles(new_role, reason="Granting study room access.")
            await partner.add_roles(new_role, reason="Granting study room access.")


            # Define Overwrites
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=False), 
                new_role: discord.PermissionOverwrite(connect=True, view_channel=True), 
                interaction.guild.me: discord.PermissionOverwrite(connect=True, view_channel=True) 
            }


            # Create the private VOICE channel
            sanitized_topic = topic.lower().replace(' ', '-')
            channel_name = f"🗣️study-{sanitized_topic[:15]}"
            
            new_voice_channel = await interaction.guild.create_voice_channel(
                name=channel_name,
                overwrites=overwrites,
                reason=f"Study room booked by {interaction.user.name} for {duration_minutes} minutes."
            )


            # Start the cleanup timer task
            cleanup_task = self.loop.create_task(
                self.cleanup_room_timer(new_voice_channel.id, duration_minutes, topic)
            )


            # Store the active room and task
            self.active_rooms[new_voice_channel.id] = {
                'owner_id': interaction.user.id, 
                'partner_id': partner.id, # Store partner ID for data logging
                'role_id': new_role.id, 
                'timer_task': cleanup_task,
                'start_time': start_time,
                'topic': topic
            }
            self.owner_to_channel[interaction.user.id] = new_voice_channel.id


            # Send Confirmation and Invitation
            end_time = start_time + timedelta(minutes=duration_minutes)
            await interaction.followup.send(
                f"**Study Room Voice Channel Booked!** 🎉\n\n"
                f"**Room:** <#{new_voice_channel.id}>\n"
                f"**Topic:** `{topic}`\n"
                f"**Duration:** `{duration_minutes} minutes`\n"
                f"**Partner:** {partner.mention} (Temporary role: `{new_role.name}`)\n"
                f"**Access Ends:** <t:{int(end_time.timestamp())}:f> (In <t:{int(end_time.timestamp())}:R>)\n\n"
                f"**Important:** If everyone leaves the voice channel, the room will close immediately and the time will be recorded!"
            )
            
        except Exception as e:
            print(f"Error creating room: {e}")
            if new_role:
                try: await new_role.delete(reason="Error during room creation, rolling back.")
                except: pass
            if new_voice_channel:
                try: await new_voice_channel.delete(reason="Error during room creation, rolling back.")
                except: pass
            
            await interaction.followup.send(
                f"Oops! I couldn't set up the study room. Please check my permissions (Manage Channels, Manage Roles). Error: `{e}`"
            )


    @app_commands.command(name="studystats", description="See your total study hours recorded by the bot.")
    async def studystats(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)


        user_id = interaction.user.id
        
        # Query total duration for the user (as owner or partner)
        self.db_cursor.execute('''
            SELECT SUM(duration_seconds) FROM sessions WHERE user_id = ? OR partner_id = ?
        ''', (user_id, user_id))
        
        total_seconds = self.db_cursor.fetchone()[0]
        
        if total_seconds is None or total_seconds == 0:
            await interaction.followup.send("You haven't logged any study sessions yet! Use `/bookroom` to start one.")
            return


        total_hours = total_seconds / 3600
        total_minutes = (total_seconds % 3600) // 60
        
        await interaction.followup.send(
            f"**{interaction.user.name}'s Total Study Time:**\n"
            f"You have logged a total of **{total_hours:.1f} hours and {total_minutes} minutes** of study time! That's awesome!"
        )


    @app_commands.command(name="weeklyreport", description="Generates and uploads a graph of study hours for the past week.")
    async def weeklyreport(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)


        one_week_ago = datetime.now() - timedelta(days=7)
        
        # 1. Fetch data from the last 7 days
        self.db_cursor.execute('''
            SELECT user_id, start_time, duration_seconds FROM sessions WHERE start_time >= ?
        ''', (one_week_ago.isoformat(),))
        
        rows = self.db_cursor.fetchall()


        if not rows:
            await interaction.followup.send("No study sessions were logged in the last 7 days. Time to get studying!")
            return


        # 2. Process Data for Graphing
        data = []
        for user_id, start_time_str, duration_seconds in rows:
            start_time = datetime.fromisoformat(start_time_str)
            
            # Since a session can span a day, we simplify by assigning it to the start day
            day_of_week = start_time.strftime('%A')
            
            data.append({
                'user_id': user_id,
                'day': day_of_week,
                'duration_hours': duration_seconds / 3600
            })
        
        df = pd.DataFrame(data)
        
        # Aggregate total hours by day and user
        weekly_summary = df.groupby(['day', 'user_id'])['duration_hours'].sum().reset_index()


        # Get Discord member names for the graph legend
        user_names = {}
        for uid in weekly_summary['user_id'].unique():
            member = interaction.guild.get_member(uid)
            user_names[uid] = member.display_name if member else f"User {uid}"
            
        weekly_summary['name'] = weekly_summary['user_id'].map(user_names)


        # Ensure all days are present for consistent plotting
        days_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        
        # 3. Generate the Graph (using matplotlib)
        # Using a more robust, modern style 
        plt.style.use('default') 
        fig, ax = plt.subplots(figsize=(10, 6))


        # Pivot data for bar plotting
        plot_data = weekly_summary.pivot(index='day', columns='name', values='duration_hours').fillna(0)
        
        # Reindex to enforce day order
        plot_data = plot_data.reindex(days_order, fill_value=0)
        
        plot_data.plot(kind='bar', ax=ax, rot=45, colormap='viridis')


        ax.set_title('Study Time Logged in the Last 7 Days', fontsize=16, fontweight='bold', color='#333')
        ax.set_xlabel('Day of the Week', fontsize=12)
        ax.set_ylabel('Total Study Hours', fontsize=12)
        ax.legend(title='Student', bbox_to_anchor=(1.05, 1), loc='upper left')
        ax.grid(axis='y', linestyle='--', alpha=0.7)
        plt.tight_layout()


        # 4. Save the figure to an in-memory buffer
        buffer = io.BytesIO()
        plt.savefig(buffer, format='png')
        buffer.seek(0)
        plt.close(fig)


        # 5. Upload the graph to Discord
        report_file = discord.File(buffer, filename="weekly_study_report.png")
        
        await interaction.followup.send(
            f"Here is the study activity report for the last week! Let's see who is working hard! ",
            file=report_file
        )




# Create and run the bot
bot = StudyBot()


# Only attempt to run the bot if the token environment variable is expected to be set.
if BOT_TOKEN:
    try:
        bot.run(BOT_TOKEN)
    except discord.errors.LoginFailure:
        print("\nERROR: Invalid bot token. Please check the DISCORD_BOT_TOKEN environment variable.")
else:
    # This block is reached if os.getenv('DISCORD_BOT_TOKEN') failed (handled above)
    pass
