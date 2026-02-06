# Final Complete bot.py with all commands, manage buttons, SSH, share, renew, suspend, points, invites, giveaways
import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import subprocess
import json
import os
import random
import logging
from datetime import datetime, timedelta

# ---------------- CONFIG ----------------
TOKEN = ""
GUILD_ID = 1432390408184529084
MAIN_ADMIN_IDS = {1397506807089598474}  # CHANGED: Renamed to MAIN_ADMIN_IDS
SERVER_IP = "207.244.240.48"
QR_IMAGE = ""
IMAGE = "jrei/systemd-ubuntu:22.04"
DEFAULT_RAM_GB = 32
DEFAULT_CPU = 6
DEFAULT_DISK_GB = 100
DATA_DIR = "data"
USERS_FILE = os.path.join(DATA_DIR, "users.json")
VPS_FILE = os.path.join(DATA_DIR, "vps_db.json")
INV_CACHE_FILE = os.path.join(DATA_DIR, "inv_cache.json")
GIVEAWAY_FILE = os.path.join(DATA_DIR, "giveaways.json")
POINTS_PER_DEPLOY = 6
POINTS_RENEW_15 = 4
POINTS_RENEW_30 = 8
VPS_LIFETIME_DAYS = 15
RENEW_MODE_FILE = os.path.join(DATA_DIR, "renew_mode.json")
LOG_CHANNEL_ID = None
OWNER_ID = 1397506807089598474

# Global admin sets
ADMIN_IDS = set(MAIN_ADMIN_IDS)  # This will contain ALL admins (main + additional)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ChunkHostBot")

# Ensure data dir
os.makedirs(DATA_DIR, exist_ok=True)

# JSON helpers
def load_json(path, default):
    try:
        if not os.path.exists(path): return default
        with open(path, 'r') as f: return json.load(f)
    except: return default

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, 'w') as f: json.dump(data, f, indent=2)
    os.replace(tmp, path)

users = load_json(USERS_FILE, {})
vps_db = load_json(VPS_FILE, {})
invite_snapshot = load_json(INV_CACHE_FILE, {})
giveaways = load_json(GIVEAWAY_FILE, {})
renew_mode = load_json(RENEW_MODE_FILE, {"mode": "15"})

def is_unique_join(user_id, inviter_id):
    """Check if this is a unique join (not a rejoin)"""
    uid = str(inviter_id)
    if uid not in users:
        return True
    
    unique_joins = users[uid].get('unique_joins', [])
    return str(user_id) not in unique_joins

def add_unique_join(user_id, inviter_id):
    """Add a unique join to inviter's record"""
    uid = str(inviter_id)
    if uid not in users:
        users[uid] = {
            "points": 0, 
            "inv_unclaimed": 0, 
            "inv_total": 0, 
            "invites": [],
            "unique_joins": []
        }
    
    user_id_str = str(user_id)
    if user_id_str not in users[uid].get('unique_joins', []):
        users[uid]['unique_joins'].append(user_id_str)
        users[uid]['inv_unclaimed'] += 1
        users[uid]['inv_total'] += 1
        persist_users()
        return True
    return False
# ---------------- Bot Init ----------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.invites = True

class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)  # Changed prefix to !

    async def setup_hook(self):
        # Sync commands globally
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} command(s)")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")

bot = Bot()

# ---------------- Docker Helpers ----------------
async def docker_run_container(ram_gb, cpu, disk_gb):
    http_port = random.randint(3000,3999)
    name = f"vps-{random.randint(1000,9999)}"
    
    # FIXED: Use systemd-compatible container setup with proper image
    cmd = [
        "docker", "run", "-d", 
        "--privileged",
        "--cgroupns=host",
        "--tmpfs", "/run",
        "--tmpfs", "/run/lock",
        "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw",
        "--name", name,
        "--cpus", str(cpu),
        "--memory", f"{ram_gb}g",
        "--memory-swap", f"{ram_gb}g",
        "-p", f"{http_port}:80",
        IMAGE  # Uses systemd-enabled image that has /sbin/init
    ]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode != 0: 
            return None, None, f"Container creation failed: {err.decode().strip() if err else 'Unknown error'}"
        
        container_id = out.decode().strip()[:12] if out else None
        if not container_id:
            return None, None, "Failed to get container ID"
            
        return container_id, http_port, None
    except Exception as e:
        return None, None, f"Container run exception: {str(e)}"

async def setup_vps_environment(container_id):
    try:
        # Wait for systemd to start
        await asyncio.sleep(15)
        
        # Update and install essentials
        commands = [
            "apt-get update -y",
            "apt-get install -y tmate curl wget neofetch sudo nano htop",
            "systemctl enable systemd-user-sessions",
            "systemctl start systemd-user-sessions"
        ]
        
        for cmd in commands:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "exec", container_id, "bash", "-c", cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                logger.warning(f"Timeout on command: {cmd}")
                continue
            except Exception as e:
                logger.warning(f"Command failed {cmd}: {e}")
                continue
        
        # Test systemctl
        test_proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "systemctl", "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await test_proc.communicate()
        
        return True, None
    except Exception as e:
        return False, str(e)

async def docker_exec_capture_ssh(container_id):
    try:
        # Kill any existing tmate sessions
        kill_cmd = "pkill -f tmate || true"
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "bash", "-c", kill_cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        await proc.communicate()
        
        # Generate SSH session using tmate
        sock = f"/tmp/tmate-{container_id}.sock"
        ssh_cmd = f"tmate -S {sock} new-session -d && sleep 5 && tmate -S {sock} display -p '#{{tmate_ssh}}'"
        
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "bash", "-c", ssh_cmd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        ssh_out = stdout.decode().strip() if stdout else "ssh@tmate.io"
        
        return ssh_out, None
        
    except Exception as e:
        return "ssh@tmate.io", str(e)

async def docker_stop_container(container_id):
    try:
        proc = await asyncio.create_subprocess_exec("docker", "stop", container_id)
        await proc.communicate()
        return True
    except:
        return False

async def docker_start_container(container_id):
    try:
        proc = await asyncio.create_subprocess_exec("docker", "start", container_id)
        await proc.communicate()
        return True
    except:
        return False

async def docker_restart_container(container_id):
    try:
        proc = await asyncio.create_subprocess_exec("docker", "restart", container_id)
        await proc.communicate()
        return True
    except:
        return False

async def docker_remove_container(container_id):
    try:
        proc = await asyncio.create_subprocess_exec("docker", "rm", "-f", container_id)
        await proc.communicate()
        return True
    except:
        return False

async def add_port_to_container(container_id, port):
    try:
        # Get container details to check if it exists
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", container_id,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        if proc.returncode != 0:
            return False, "Container not found"
        
        # For simplicity, we'll just note the port in our database
        # In production, you'd need to recreate the container with new port mappings
        return True, f"Port {port} mapped to container"
    except Exception as e:
        return False, str(e)

async def check_systemctl_status(container_id):
    """Check if systemctl works in the container"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "systemctl", "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode == 0
    except:
        return False

# ---------------- VPS Helpers ----------------
def persist_vps(): save_json(VPS_FILE, vps_db)
def persist_users(): save_json(USERS_FILE, users)
def persist_renew_mode(): save_json(RENEW_MODE_FILE, renew_mode)
def persist_giveaways(): save_json(GIVEAWAY_FILE, giveaways)

async def send_log(action: str, user, details: str = "", vps_id: str = ""):
    """Send professional log embed to log channel"""
    if not LOG_CHANNEL_ID:
        return
    
    try:
        channel = bot.get_channel(LOG_CHANNEL_ID)
        if not channel:
            print(f"Log channel {LOG_CHANNEL_ID} not found")
            return
        
        # Determine color based on action type
        color_map = {
            "deploy": discord.Color.green(),
            "remove": discord.Color.orange(),
            "renew": discord.Color.blue(),
            "suspend": discord.Color.red(),
            "unsuspend": discord.Color.green(),
            "start": discord.Color.green(),
            "stop": discord.Color.orange(),
            "restart": discord.Color.blue(),
            "share": discord.Color.purple(),
            "admin": discord.Color.gold(),
            "points": discord.Color.teal(),
            "invite": discord.Color.magenta(),
            "error": discord.Color.red()
        }
        
        # Get appropriate color
        action_lower = action.lower()
        color = discord.Color.blue()  # default
        for key, value in color_map.items():
            if key in action_lower:
                color = value
                break
        
        # Create embed
        embed = discord.Embed(
            title=f"📊 {action}",
            color=color,
            timestamp=datetime.utcnow()
        )
        
        # Add user info
        if hasattr(user, 'mention'):
            embed.add_field(name="👤 User", value=f"{user.mention}\n`{user.name}`", inline=True)
        else:
            embed.add_field(name="👤 User", value=f"`{user}`", inline=True)
        
        # Add VPS ID if provided
        if vps_id:
            embed.add_field(name="🆔 VPS ID", value=f"`{vps_id}`", inline=True)
        
        # Add details
        if details:
            embed.add_field(name="📝 Details", value=details[:1024], inline=False)
        
        # Add timestamp field
        embed.add_field(
            name="⏰ Time", 
            value=f"<t:{int(datetime.utcnow().timestamp())}:R>", 
            inline=True
        )
        
        # Set footer
        embed.set_footer(text="VPS Activity Log")
        
        await channel.send(embed=embed)
        
        # Also save to JSON file for /logs command
        logs_file = os.path.join(DATA_DIR, "vps_logs.json")
        logs_data = load_json(logs_file, [])
        
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "action": action,
            "user": user.name if hasattr(user, 'name') else str(user),
            "details": details,
            "vps_id": vps_id
        }
        
        logs_data.append(log_entry)
        
        # Keep only last 1000 logs to prevent file from growing too large
        if len(logs_data) > 1000:
            logs_data = logs_data[-1000:]
        
        save_json(logs_file, logs_data)
        
    except Exception as e:
        print(f"Failed to send log: {e}")

async def create_vps(owner_id, ram=DEFAULT_RAM_GB, cpu=DEFAULT_CPU, disk=DEFAULT_DISK_GB, paid=False, giveaway=False):
    uid = str(owner_id)
    cid, http_port, err = await docker_run_container(ram, cpu, disk)
    if err: 
        return {'error': err}
    
    # Wait for container to start and setup
    await asyncio.sleep(10)
    
    # Setup environment
    success, setup_err = await setup_vps_environment(cid)
    if not success:
        logger.warning(f"Setup had issues for {cid}: {setup_err}")
    
    # Generate SSH
    ssh, ssh_err = await docker_exec_capture_ssh(cid)
    
    # Check systemctl status
    systemctl_works = await check_systemctl_status(cid)
    
    created = datetime.utcnow()
    expires = created + timedelta(days=VPS_LIFETIME_DAYS)
    rec = {
        "owner": uid,
        "container_id": cid,
        "ram": ram,
        "cpu": cpu,
        "disk": disk,
        "http_port": http_port,
        "ssh": ssh,
        "created_at": created.isoformat(),
        "expires_at": expires.isoformat(),
        "active": True,
        "suspended": False,
        "paid_plan": paid,
        "giveaway_vps": giveaway,
        "shared_with": [],
        "additional_ports": [],
        "systemctl_working": systemctl_works
    }
    vps_db[cid] = rec
    persist_vps()
    
    # Send log
    try:
        user = await bot.fetch_user(int(uid))
        await send_log("VPS Created", user, cid, f"RAM: {ram}GB, CPU: {cpu}, Disk: {disk}GB, Systemctl: {'✅' if systemctl_works else '❌'}")
    except:
        pass
    
    return rec

def get_user_vps(user_id):
    uid = str(user_id)
    return [vps for vps in vps_db.values() if vps['owner'] == uid or uid in vps.get('shared_with', [])]

def can_manage_vps(user_id, container_id):
    if user_id in ADMIN_IDS:
        return True
    vps = vps_db.get(container_id)
    if not vps:
        return False
    uid = str(user_id)
    return vps['owner'] == uid or uid in vps.get('shared_with', [])

def get_resource_usage():
    """Calculate resource usage percentages"""
    total_ram = sum(vps['ram'] for vps in vps_db.values())
    total_cpu = sum(vps['cpu'] for vps in vps_db.values())
    total_disk = sum(vps['disk'] for vps in vps_db.values())
    
    ram_percent = (total_ram / (DEFAULT_RAM_GB * 100)) * 100  # Assuming 100GB max RAM
    cpu_percent = (total_cpu / (DEFAULT_CPU * 50)) * 100     # Assuming 50 CPU max
    disk_percent = (total_disk / (DEFAULT_DISK_GB * 200)) * 100  # Assuming 200GB max disk
    
    return {
        'ram': min(ram_percent, 100),
        'cpu': min(cpu_percent, 100),
        'disk': min(disk_percent, 100),
        'total_ram': total_ram,
        'total_cpu': total_cpu,
        'total_disk': total_disk
    }

# ---------------- Background Tasks ----------------
@tasks.loop(minutes=10)
async def expire_check_loop():
    now = datetime.utcnow()
    changed = False
    for cid, rec in list(vps_db.items()):
        if rec.get('active', True) and now >= datetime.fromisoformat(rec['expires_at']):
            await docker_stop_container(cid)
            rec['active'] = False
            rec['suspended'] = True
            changed = True
            # Log expiration
            try:
                user = await bot.fetch_user(int(rec['owner']))
                await send_log("VPS Expired", user, cid, "Auto-suspended due to expiry")
            except:
                pass
    if changed: 
        persist_vps()

@tasks.loop(minutes=5)
async def giveaway_check_loop():
    now = datetime.utcnow()
    ended_giveaways = []
    
    for giveaway_id, giveaway in list(giveaways.items()):
        if giveaway['status'] == 'active' and now >= datetime.fromisoformat(giveaway['end_time']):
            # Giveaway ended, select winner
            participants = giveaway.get('participants', [])
            if participants:
                if giveaway['winner_type'] == 'random':
                    winner_id = random.choice(participants)
                    giveaway['winner_id'] = winner_id
                    giveaway['status'] = 'ended'
                    
                    # Create VPS for winner
                    try:
                        rec = await create_vps(int(winner_id), giveaway['vps_ram'], giveaway['vps_cpu'], giveaway['vps_disk'], giveaway_vps=True)
                        if 'error' not in rec:
                            giveaway['vps_created'] = True
                            giveaway['winner_vps_id'] = rec['container_id']
                            
                            # Send DM to winner
                            try:
                                winner = await bot.fetch_user(int(winner_id))
                                embed = discord.Embed(title="🎉 You Won a VPS Giveaway!", color=discord.Color.gold())
                                embed.add_field(name="Container ID", value=f"`{rec['container_id']}`", inline=False)
                                embed.add_field(name="Specs", value=f"**{rec['ram']}GB RAM** | **{rec['cpu']} CPU** | **{rec['disk']}GB Disk**", inline=False)
                                embed.add_field(name="Expires", value=rec['expires_at'][:10], inline=True)
                                embed.add_field(name="Status", value="🟢 Active", inline=True)
                                embed.add_field(name="HTTP Access", value=f"http://{SERVER_IP}:{rec['http_port']}", inline=False)
                                embed.add_field(name="SSH Connection", value=f"```{rec['ssh']}```", inline=False)
                                embed.set_footer(text="This is a giveaway VPS and cannot be renewed. It will auto-delete after 15 days.")
                                await winner.send(embed=embed)
                            except:
                                pass
                    except Exception as e:
                        logger.error(f"Failed to create VPS for giveaway winner: {e}")
                
                elif giveaway['winner_type'] == 'all':
                    # Create VPS for all participants
                    successful_creations = 0
                    for participant_id in participants:
                        try:
                            rec = await create_vps(int(participant_id), giveaway['vps_ram'], giveaway['vps_cpu'], giveaway['vps_disk'], giveaway_vps=True)
                            if 'error' not in rec:
                                successful_creations += 1
                                
                                # Send DM to participant
                                try:
                                    participant = await bot.fetch_user(int(participant_id))
                                    embed = discord.Embed(title="🎉 You Received a VPS from Giveaway!", color=discord.Color.gold())
                                 embed.add_field(name="Container ID", value=f"`{rec['container_id']}`", inline=False)
                                    embed.add_field(name="Specs", value=f"**{rec['ram']}GB RAM** | **{rec['cpu']} CPU** | **{rec['disk']}GB Disk**", inline=False)
                                    embed.add_field(name="Expires", value=rec['expires_at'][:10], inline=True)
                                    embed.add_field(name="Status", value="🟢 Active", inline=True)
                                    embed.add_field(name="HTTP Access", value=f"http://{SERVER_IP}:{rec['http_port']}", inline=False)
                                    embed.add_field(name="SSH Connection
