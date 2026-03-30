import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import datetime
import json
import os
import re
from typing import Optional
import aiohttp

# === CONFIGURATION ===
DISCORD_TOKEN = "bot token goes here"
ADMIN_USER_ID = Admin-id-goes-here  # Your ID - only admin
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# Default limits per mode (per day)
DEFAULT_LIMITS = {
    "light": 50,
    "medium": 25,
    "heavy": 10,
    "council": 5,
    "debate": 3
}

# === SYSTEM PROMPT ===
SYSTEM_PROMPT = """You are Orion, an AI assistant created by Ion. Be helpful, concise, and thorough.""" #Change this if you want.

# === THE ROSTER ===
AGENTS = {
    "Kimi": "kimi-k2.5:cloud",
    "DeepSeek": "deepseek-v3.2:cloud",
    "Gemini": "gemini-3-flash-preview"
}

# The Judge/Synthesizer
SYNTHESIZER_MODEL = "deepseek-v3.2:cloud"

# Standard Modes
MODELS = {
    "light": "qwen3-next:80b-cloud",
    "medium": "minimax-m2.5:cloud",
    "heavy": "kimi-k2.5:cloud"
}

# === DATA STORAGE ===
DATA_FILE = "bot_data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    return {"users": {}, "last_reset": None}

def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

# === BOT SETUP ===
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# === HELPER FUNCTIONS ===
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_USER_ID

def get_display_name(user_id: int, fallback: str) -> str:
    if is_admin(user_id):
        return "Ion"
    return fallback

def get_user_limits(user_id: int) -> dict:
    data = load_data()
    user_id_str = str(user_id)
    
    if user_id_str not in data["users"]:
        data["users"][user_id_str] = {
            "limits": DEFAULT_LIMITS.copy(),
            "custom_limits": {},
            "is_admin": is_admin(user_id)
        }
        save_data(data)
    
    return data["users"][user_id_str]["limits"]

def get_custom_limit(user_id: int, mode: str) -> Optional[int]:
    data = load_data()
    user_id_str = str(user_id)
    
    if user_id_str in data["users"]:
        custom = data["users"][user_id_str].get("custom_limits", {})
        if mode in custom:
            return custom[mode]
    return None

def check_and_use_limit(user_id: int, mode: str) -> tuple[bool, int]:
    data = load_data()
    user_id_str = str(user_id)
    
    if user_id_str not in data["users"]:
        get_user_limits(user_id)
    
    user_data = data["users"][user_id_str]
    limits = user_data["limits"]
    
    custom = get_custom_limit(user_id, mode)
    current_limit = custom if custom is not None else limits.get(mode, 0)
    
    today = datetime.date.today().isoformat()
    usage_key = f"usage_{mode}"
    
    if usage_key not in user_data:
        user_data[usage_key] = {"date": today, "count": 0}
    
    usage_record = user_data[usage_key]
    
    if usage_record["date"] != today:
        usage_record["date"] = today
        usage_record["count"] = 0
    
    if usage_record["count"] >= current_limit:
        return False, 0
    
    usage_record["count"] += 1
    save_data(data)
    
    remaining = current_limit - usage_record["count"]
    return True, remaining

def reset_all_limits():
    data = load_data()
    for user_id_str, user_data in data["users"].items():
        for mode in DEFAULT_LIMITS:
            usage_key = f"usage_{mode}"
            if usage_key in user_data:
                user_data[usage_key] = {"date": datetime.date.today().isoformat(), "count": 0}
    data["last_reset"] = datetime.datetime.now().isoformat()
    save_data(data)

# === DAILY RESET TASK ===
@tasks.loop(hours=24)
async def daily_reset():
    reset_all_limits()
    print("✅ Daily limits reset completed")

# === PROMPTS ===
ROUND_ONE_PROMPT = """You are participating in a debate. State your initial position clearly.

Question: {question}

[POSITION]: Your opening position
[REASONING]: Brief justification"""

DEBATE_PROMPT = """You are in Round {round} of a debate.

Original Question: {question}

Your last position: {my_last_position}

What other participants think of your position:
{critiques_of_me}

[POSITION]: Your refined position (if you've changed)
[REASONING]: Your reasoning"""

SYNTHESIZER_PROMPT = """You are a master synthesizer. Given this debate transcript, provide the absolute answer to the question.

Question: {question}

Debate Transcript:
{transcript}

Provide a clear, definitive answer:"""

# === UTILITY FUNCTIONS ===
def strip_reasoning(text: str) -> str:
    text = re.sub(r'\[REASONING\]:.*', '', text, flags=re.DOTALL)
    return text.strip()

def check_agreement(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in ["agree", "concur", "consensus", "i accept"])

def extract_position(text: str) -> str:
    match = re.search(r'\[POSITION\]:\s*(.*?)(?=\[REASONING\]|$)', text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text[:500]

# === OLLAMA CLIENT ===
class AsyncClient:
    def __init__(self):
        self.base_url = OLLAMA_BASE_URL
    
    async def chat(self, model: str, messages: list):
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": model,
                "messages": messages,
                "stream": False
            }
            
            async with session.post(
                f"{self.base_url}/api/chat",
                json=payload
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"Ollama error {response.status}: {error_text}")
                
                result = await response.json()
                
                return {
                    "message": {"content": result["message"]["content"]},
                    "eval_count": result.get("eval_count", 0),
                    "eval_duration": result.get("eval_duration", 0)
                }

# === AGENT RUNNER ===
async def run_agent(client, name, model, messages):
    from datetime import datetime
    start = datetime.now()
    
    try:
        response = await client.chat(model=model, messages=messages)
        end = datetime.now()
        content = strip_reasoning(response['message']['content'])
        
        eval_count = response.get('eval_count', 0)
        eval_dur = response.get('eval_duration', 0)
        
        if eval_dur > 0:
            tps = eval_count / (eval_dur / 1e9)
            tps_str = f"{tps:.0f}"
        else:
            dur_sec = (end - start).total_seconds()
            tps = (len(content)/4) / dur_sec if dur_sec > 0 else 0
            tps_str = f"{tps:.0f}"

        return {
            "name": name,
            "content": content,
            "tps": tps_str,
            "agreement": check_agreement(content)
        }
    except Exception as e:
        return {
            "name": name,
            "content": f"Error: {str(e)}",
            "tps": "ERR",
            "agreement": False
        }

# === DEBATE ROUND ===
async def run_debate_round(client, agents, question, round_num, history, positions):
    tasks = []
    
    for name, model in agents.items():
        if round_num == 1:
            prompt = ROUND_ONE_PROMPT.format(name=name, question=question)
            messages = [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt}
            ]
        else:
            critiques = []
            for other_name, other_pos in positions.items():
                if other_name != name:
                    critiques.append(f"{other_name}'s view: {extract_position(other_pos)}")
            
            prompt = DEBATE_PROMPT.format(
                name=name,
                question=question,
                history=history[-1] if history else "First round",
                my_last_position=positions.get(name, "No previous position"),
                critiques_of_me="\n".join(critiques),
                round=round_num
            )
            messages = [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt}
            ]
        
        tasks.append(run_agent(client, name, model, messages))
    
    results = await asyncio.gather(*tasks)
    return {r["name"]: r for r in results}

# === SYNTHESIZE ===
async def synthesize_debate(client, question, transcript):
    prompt = SYNTHESIZER_PROMPT.format(question=question, transcript=transcript)
    messages = [
        {'role': 'system', 'content': 'You are a master synthesizer.'},
        {'role': 'user', 'content': prompt}
    ]
    
    response = await client.chat(model=SYNTHESIZER_MODEL, messages=messages)
    content = strip_reasoning(response['message']['content'])
    
    return content

# === ORION ADMIN GROUP ===
orion_admin = app_commands.Group(
    name="orionadmin", 
    description="Orion Admin Commands - Ion Only"
)

# Enable DMs for all subcommands in this group
app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)(orion_admin)

@orion_admin.command(name="adduser", description="Add a user with default limits")
async def add_user(interaction: discord.Interaction, user: discord.User):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("❌ You don't have admin access.", ephemeral=True)
        return
    
    data = load_data()
    user_id_str = str(user.id)
    
    if user_id_str in data["users"]:
        await interaction.response.send_message(f"⚠️ {user.mention} already exists!", ephemeral=True)
        return
    
    data["users"][user_id_str] = {
        "limits": DEFAULT_LIMITS.copy(),
        "custom_limits": {},
        "is_admin": False,
        "name": user.name
    }
    save_data(data)
    
    embed = discord.Embed(title="✅ User Added", color=discord.Color.green())
    embed.add_field(name="User", value=user.mention, inline=True)
    embed.add_field(name="ID", value=str(user.id), inline=True)
    embed.add_field(name="Default Limits", value=str(DEFAULT_LIMITS), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@orion_admin.command(name="removeuser", description="Remove a user")
async def remove_user(interaction: discord.Interaction, user: discord.User):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("❌ You don't have admin access.", ephemeral=True)
        return
    
    data = load_data()
    user_id_str = str(user.id)
    
    if user_id_str not in data["users"]:
        await interaction.response.send_message(f"⚠️ {user.mention} not found!", ephemeral=True)
        return
    
    del data["users"][user_id_str]
    save_data(data)
    
    await interaction.response.send_message(f"✅ Removed {user.mention}", ephemeral=True)

@orion_admin.command(name="setlimit", description="Set custom limit for a user on a mode")
async def set_limit(interaction: discord.Interaction, user: discord.User, mode: str, limit: int):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("❌ You don't have admin access.", ephemeral=True)
        return
    
    valid_modes = list(DEFAULT_LIMITS.keys())
    if mode not in valid_modes:
        await interaction.response.send_message(f"⚠️ Invalid mode! Valid: {valid_modes}", ephemeral=True)
        return
    
    data = load_data()
    user_id_str = str(user.id)
    
    if user_id_str not in data["users"]:
        data["users"][user_id_str] = {
            "limits": DEFAULT_LIMITS.copy(),
            "custom_limits": {},
            "is_admin": False,
            "name": user.name
        }
    
    data["users"][user_id_str]["custom_limits"][mode] = limit
    save_data(data)
    
    await interaction.response.send_message(
        f"✅ Set {mode} limit to **{limit}** for {user.mention}", 
        ephemeral=True
    )

@orion_admin.command(name="resetcustom", description="Remove custom limits, revert to defaults")
async def reset_custom(interaction: discord.Interaction, user: discord.User):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("❌ You don't have admin access.", ephemeral=True)
        return
    
    data = load_data()
    user_id_str = str(user.id)
    
    if user_id_str not in data["users"]:
        await interaction.response.send_message(f"⚠️ {user.mention} not found!", ephemeral=True)
        return
    
    data["users"][user_id_str]["custom_limits"] = {}
    save_data(data)
    
    await interaction.response.send_message(
        f"✅ Custom limits reset for {user.mention}", 
        ephemeral=True
    )

@orion_admin.command(name="listusers", description="List all users and their limits")
async def list_users(interaction: discord.Interaction):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("❌ You don't have admin access.", ephemeral=True)
        return
    
    data = load_data()
    
    if not data["users"]:
        await interaction.response.send_message("No users yet!", ephemeral=True)
        return
    
    embed = discord.Embed(title="📋 User List", color=discord.Color.blue())
    
    for user_id_str, user_data in data["users"].items():
        custom = user_data.get("custom_limits", {})
        limits = user_data.get("limits", {})
        
        display_limits = {k: custom.get(k, limits.get(k, 0)) for k in DEFAULT_LIMITS}
        
        for mode in DEFAULT_LIMITS:
            usage_key = f"usage_{mode}"
            if usage_key in user_data:
                rec = user_data[usage_key]
                display_limits[mode] = f"{rec['count']}/{display_limits[mode]}"
        
        limits_str = " | ".join([f"{k}: {v}" for k, v in display_limits.items()])
        
        name = user_data.get("name", f"ID: {user_id_str}")
        is_admin_user = "👑" if user_data.get("is_admin") else ""
        
        embed.add_field(name=f"{name} {is_admin_user}", value=f"```{limits_str}```", inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@orion_admin.command(name="forcereset", description="Force reset all limits now")
async def force_reset(interaction: discord.Interaction):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("❌ You don't have admin access.", ephemeral=True)
        return
    
    reset_all_limits()
    await interaction.response.send_message("✅ All limits have been reset!", ephemeral=True)

@orion_admin.command(name="status", description="Bot status and statistics")
async def status(interaction: discord.Interaction):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("❌ You don't have admin access.", ephemeral=True)
        return
    
    data = load_data()
    
    embed = discord.Embed(title="🤖 Orion Status", color=discord.Color.blurple())
    embed.add_field(name="Total Users", value=str(len(data["users"])), inline=True)
    embed.add_field(name="Admin ID", value=str(ADMIN_USER_ID), inline=True)
    embed.add_field(name="Last Reset", value=data.get("last_reset", "Never"), inline=True)
    embed.add_field(name="Default Limits", value=str(DEFAULT_LIMITS), inline=False)
    embed.add_field(name="Ollama URL", value=OLLAMA_BASE_URL, inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# === LIMITS COMMAND ===
@tree.command(name="limits", description="Check your current usage limits")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def limits(interaction: discord.Interaction):
    user_id = interaction.user.id
    data = load_data()
    user_id_str = str(user_id)
    
    if user_id_str not in data["users"]:
        get_user_limits(user_id)
        data = load_data()
    
    user_data = data["users"].get(user_id_str, {})
    custom = user_data.get("custom_limits", {})
    limits = user_data.get("limits", DEFAULT_LIMITS)
    
    display_name = get_display_name(user_id, interaction.user.name)
    
    embed = discord.Embed(
        title=f"📊 Limits for {display_name}",
        color=discord.Color.gold() if is_admin(user_id) else discord.Color.blue()
    )
    
    if is_admin(user_id):
        embed.description = "👑 **Admin User** - No limits!"
    
    for mode, default_limit in DEFAULT_LIMITS.items():
        effective_limit = custom.get(mode, default_limit)
        usage_key = f"usage_{mode}"
        
        if usage_key in user_data:
            rec = user_data[usage_key]
            today = datetime.date.today().isoformat()
            
            if rec["date"] == today:
                used = rec["count"]
                remaining = max(0, effective_limit - used)
                
                if mode in custom:
                    embed.add_field(
                        name=f"⚡ {mode.upper()}",
                        value=f"Remaining: **{remaining}** | Default: **{default_limit}**",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name=f"⚡ {mode.upper()}",
                        value=f"Remaining: **{remaining}** | Default: **{default_limit}**",
                        inline=False
                    )
            else:
                embed.add_field(
                    name=f"⚡ {mode.upper()}",
                    value=f"Remaining: **{effective_limit}** | Default: **{default_limit}**",
                    inline=False
                )
        else:
            embed.add_field(
                name=f"⚡ {mode.upper()}",
                value=f"Remaining: **{effective_limit}** | Default: **{default_limit}**",
                inline=False
            )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# === MAIN CHAT COMMAND ===
@tree.command(name="chat", description="Chat with Orion")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    prompt="Your message...",
    mode="Processing mode"
)
@app_commands.choices(mode=[
    app_commands.Choice(name="Light (Fast)", value="light"),
    app_commands.Choice(name="Medium (Balanced)", value="medium"),
    app_commands.Choice(name="Heavy (Deep)", value="heavy"),
    app_commands.Choice(name="The Council", value="council"),
    app_commands.Choice(name="Debate (Max 5 rounds)", value="debate"),
])
async def chat(interaction: discord.Interaction, prompt: str, mode: app_commands.Choice[str]):
    user_id = interaction.user.id
    
    if not is_admin(user_id):
        allowed, remaining = check_and_use_limit(user_id, mode.value)
        
        if not allowed:
            embed = discord.Embed(
                title="❌ Limit Reached",
                description=f"You've used all your {mode.value} queries today!",
                color=discord.Color.red()
            )
            embed.add_field(
                name="Check Limits", 
                value="Use `/limits` to see all your usage",
                inline=False
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
    
    await interaction.response.defer()
    
    client = AsyncClient()
    selected_mode = mode.value
    start_time = datetime.datetime.now()
    
    display_name = get_display_name(user_id, interaction.user.display_name)
    
    placeholder = f"**{display_name}:** {prompt}\n"
    if selected_mode == "council":
        placeholder += "*⚡ Convening The Council...*"
    elif selected_mode == "debate":
        placeholder += "*🎭 Convening Debate Council (Max 5 rounds)...*"
    else:
        placeholder += f"*Thinking... [{selected_mode.upper()}]*"
    
    message = await interaction.followup.send(content=placeholder)
    
    try:
        final_reply = ""
        footer_text = ""
        
        if selected_mode == "debate":
            transcript = f"Question: {prompt}\n\n"
            positions = {}
            
            for round_num in range(1, 6):
                await message.edit(
                    content=f"**{display_name}:** {prompt}\n🎭 **Debate Round {round_num}/5**"
                )
                
                round_results = await run_debate_round(
                    client, AGENTS, prompt, round_num, 
                    transcript, positions
                )
                
                round_text = f"\n--- ROUND {round_num} ---\n"
                all_agree = True
                
                for name, result in round_results.items():
                    positions[name] = result["content"]
                    agreement_found = result["agreement"] if round_num > 1 else False
                    round_text += f"[{name}] (Agree: {agreement_found}): {result['content'][:500]}...\n\n"
                    if not agreement_found and round_num > 1:
                        all_agree = False
                
                transcript += round_text
                
                if round_num > 1 and all_agree:
                    await message.edit(
                        content=f"**{display_name}:** {prompt}\n✅ **Consensus reached at Round {round_num}! Moving to synthesis...**"
                    )
                    await asyncio.sleep(1)
                    break
                    
                await asyncio.sleep(0.5)
            
            await message.edit(
                content=f"**{display_name}:** {prompt}\n⚡ **Synthesizing Absolute Answer...**"
            )
            final_reply = await synthesize_debate(client, prompt, transcript)
            
            total_time = (datetime.datetime.now() - start_time).total_seconds()
            footer_text = f"Orion [DEBATE] • {round_num} rounds • {total_time:.1f}s"
            
        elif selected_mode == "council":
            tasks = [run_agent(client, name, model, [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt}
            ]) for name, model in AGENTS.items()]
            
            results = await asyncio.gather(*tasks)
            evidence = ""
            stats_display = []
            
            for res in results:
                evidence += f"--- {res['name']} ---\n{res['content']}\n\n"
                stats_display.append(f"{res['name'][0]}:{res['tps']}")
            
            await message.edit(content=f"**{display_name}:** {prompt}\n*⚡ Judging...*")
            
            judge_input = f"USER: {prompt}\n\n{evidence}"
            verdict = await client.chat(model=SYNTHESIZER_MODEL, messages=[
                {'role': 'system', 'content': 'Select the best answer. Start with [WINNER: Name].'},
                {'role': 'user', 'content': judge_input}
            ])
            
            raw_verdict = strip_reasoning(verdict['message']['content'])
            winner_match = re.search(r"\[WINNER:\s*(.*?)\]", raw_verdict, re.IGNORECASE)
            
            if winner_match:
                winner_name = winner_match.group(1).strip()
                final_reply = raw_verdict.replace(winner_match.group(0), "").strip()
            else:
                winner_name = "Consensus"
                final_reply = raw_verdict
            
            stats_str = " ".join(stats_display)
            footer_text = f"Winner: {winner_name} • {stats_str} t/s"
            
        else:
            res = await run_agent(client, selected_mode.upper(), MODELS[selected_mode], [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt}
            ])
            
            final_reply = res['content']
            footer_text = f"Orion [{selected_mode.upper()}] • {res['tps']} t/s"
        
        if not is_admin(user_id):
            _, remaining = check_and_use_limit(user_id, mode.value)
            footer_text += f" • Remaining: {remaining}"
        
        embed = discord.Embed(description=final_reply, color=0x5865F2)
        embed.set_footer(
            text=footer_text,
            icon_url=bot.user.avatar.url if bot.user.avatar else None
        )
        
        await message.edit(content=f"**{display_name}:** {prompt}", embed=embed)
        
    except Exception as e:
        await message.edit(
            content=f"**{display_name}:** {prompt}\n⚠️ **Error:** {str(e)}"
        )

# === BOT EVENTS ===
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    
    tree.add_command(orion_admin)
    await tree.sync()
    print("✅ Commands synced")
    
    daily_reset.start()
    print("✅ Daily reset task started")

# === RUN BOT ===
if __name__ == "__main__":
    if DISCORD_TOKEN == "YOUR_TOKEN_HERE":
        print("Set DISCORD_TOKEN environment variable!")
    else:
        bot.run(DISCORD_TOKEN)
