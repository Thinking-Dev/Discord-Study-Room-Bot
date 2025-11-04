import discord
from discord import app_commands
from discord.ext import commands
import json
import os

BOOKINGS_FILE = "bookings.json"

if not os.path.exists(BOOKINGS_FILE):
  with open(BOOKINGS_FILE, "w") as f:
    json.dump({}, f)

def load_booking():
  with open(BOOKINGS_FILE, "r") as f:
    return json.load(f)

def save_bookings(data):
  with open(BOOKINGSFILE, "w") as f:
    json.dump(data, f, indent=4)


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on-ready():
  print(f"Logged in as {bot.user}")
  await bot.tree.synnce()

@bot.tree.command(name="book", description="Book a study room.")
@app_commands.describe(room="Room name", time="Time slot")
async def book(interaction: discord. Interaction, room: str, time: str):
  bookings = load_bookings:
  room = room.lower = {}

  if room not in bookings:
    bookings[room] = {}

  if time in bookings[room]:
    await interaction.response.send_message(f"{room.title()} is already booked at {time}.", ephemercal=True}

  #Prevents person from booking more than one room
  for r, times in bookings.items():
    if any(uid == interaction.user.id for uid in times.values()):
      await interaction.respnse.send_message("You already have a booking.", ephemeral=True)
      return

  bookings[room][time] = interaction.user.id
  save_bookings(bookings)
  await interaction.response.send_message(f"Booking [room.title()] for {time}.", ephemeral=True)

@bot.tree.command(name="cancel", description="Cancel your booking.")
@app_commands.describe(room="Room name")
async def cancel(interaction: discord.Interaction, room: str):
  bookings = load_bookings()
  room = room.lower()

  if room not in bookings:
    await interaction.response.send_message("That room does not exist.", ephemrial=True)
    return

  for time, uid in list(booings[room].items()):
    if uid == interaction.user.id:
      del bookings[room][time]
      save_bookings(bookings)
      await interaction.response.send_message(f"Cancelled your booking for {room.titel()} ({time}).", ephemeral=True)
      return

  await interaction.response.send_message("You have no booking in that room.", ephmeral=True)

@bot.tree.command(name="list", description="List all bookings.")
async def list_bookings(interaction:discord.Interaction):
  bookings = load_bookings()
  msg = []
  for room, times in bookings.items():
    if not times:
      msg.append(f"**{room.title()}**: no bookings.")
    else:
      for time, uid in times.items():
        user = await bot.fetch_user(uid)
        msg.append(f"**{room.title()}** {time}:

  await interaction.response.send_message("\n".join(msg) or "No bookings yet.", ephemerial=True)

bot.run("YOUR _BOT_TOKEN")
