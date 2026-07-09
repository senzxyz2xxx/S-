import os
import json
import time
import threading
from collections import defaultdict, deque
from datetime import timedelta, datetime, timezone

import discord
from discord.ext import commands
from flask import Flask, request, session, redirect, url_for, jsonify, render_template_string

# ================= CONFIG (ตั้งค่าผ่าน Environment Variables บน Render) =================
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "!")
OWNER_ID = int(os.getenv("OWNER_ID", "1005357318281641994"))

SPAM_MSG_LIMIT = int(os.getenv("SPAM_MSG_LIMIT", "5"))       # จำนวนข้อความ
SPAM_TIME_WINDOW = int(os.getenv("SPAM_TIME_WINDOW", "5"))   # วินาที
SPAM_MUTE_MINUTES = int(os.getenv("SPAM_MUTE_MINUTES", "10"))

WARN_LIMIT = int(os.getenv("WARN_LIMIT", "3"))
WARN_MUTE_MINUTES = int(os.getenv("WARN_MUTE_MINUTES", "30"))

# คำต้องห้าม (คั่นด้วยจุลภาค) เพิ่มเติมได้ผ่าน env BAD_WORDS โดยไม่ทับของเดิม
DEFAULT_BAD_WORDS = ["อายุน้อยกว่า13", "พ่อมึง", "แม่มึง", "fuck", "bitch", "asshole"]
EXTRA_BAD_WORDS = [w.strip().lower() for w in os.getenv("BAD_WORDS", "").split(",") if w.strip()]
BAD_WORDS = set(w.lower() for w in DEFAULT_BAD_WORDS) | set(EXTRA_BAD_WORDS)

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "changeme123")
SECRET_KEY = os.getenv("SECRET_KEY", "please-change-this-secret-key")

LOG_FILE = "modlog.json"
MAX_LOGS = 500

# ================= LOG STORE (ใช้ร่วมกันระหว่างบอทกับเว็บแดชบอร์ด) =================
class ModLog:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = deque(maxlen=MAX_LOGS)
        self._load()

    def _load(self):
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data[-MAX_LOGS:]:
                        self.events.append(item)
            except (json.JSONDecodeError, OSError):
                pass

    def _save(self):
        try:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                json.dump(list(self.events), f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def add(self, event_type: str, user: str, user_id: int, reason: str, moderator: str = "Auto"):
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "type": event_type,       # JOIN / WARN / SPAM / BADWORD / MUTE / UNMUTE / KICK / BAN / CLEAR
            "user": user,
            "user_id": user_id,
            "reason": reason,
            "moderator": moderator,
        }
        with self.lock:
            self.events.appendleft(entry)
            self._save()
        return entry

    def all(self):
        with self.lock:
            return list(self.events)

    def stats(self):
        with self.lock:
            items = list(self.events)
        counts = defaultdict(int)
        for e in items:
            counts[e["type"]] += 1
        return counts


modlog = ModLog()

# ================= BOT SETUP =================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

user_messages = defaultdict(deque)   # timestamp ข้อความล่าสุด (กันสแปม)
warnings = defaultdict(int)          # จำนวนคำเตือนสะสมต่อคน

UNMUTE_CONTACT_MSG = (
    "คุณถูกมิวท์ในเซิร์ฟเวอร์ **{guild}**\n"
    "เหตุผล: {reason}\n\n"
    "หากต้องการปลดมิวท์ กรุณาติดต่อเจ้าของบอท:\n"
    f"- ไอดี: `{OWNER_ID}`\n"
    f"- หรือ mention <@{OWNER_ID}>"
)


async def mute_member(member: discord.Member, minutes: int, reason: str, moderator: str = "Auto") -> bool:
    try:
        await member.timeout(timedelta(minutes=minutes), reason=reason)
    except discord.Forbidden:
        return False
    modlog.add("MUTE", str(member), member.id, f"{reason} ({minutes} นาที)", moderator)
    try:
        await member.send(UNMUTE_CONTACT_MSG.format(guild=member.guild.name, reason=reason))
    except discord.Forbidden:
        pass
    return True


async def warn_user(member: discord.Member, reason: str, moderator: str = "Auto"):
    warnings[member.id] += 1
    count = warnings[member.id]
    modlog.add("WARN", str(member), member.id, f"{reason} ({count}/{WARN_LIMIT})", moderator)
    try:
        await member.send(
            f"คุณได้รับคำเตือนในเซิร์ฟเวอร์ **{member.guild.name}**\n"
            f"เหตุผล: {reason}\nจำนวนคำเตือนสะสม: {count}/{WARN_LIMIT}"
        )
    except discord.Forbidden:
        pass
    if count >= WARN_LIMIT:
        warnings[member.id] = 0
        await mute_member(member, WARN_MUTE_MINUTES, f"ได้รับคำเตือนครบ {WARN_LIMIT} ครั้ง", moderator)
    return count


def is_mod():
    async def predicate(ctx):
        return ctx.author.guild_permissions.manage_messages or ctx.author.id == OWNER_ID
    return commands.check(predicate)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_member_join(member: discord.Member):
    modlog.add("JOIN", str(member), member.id, "เข้าร่วมเซิร์ฟเวอร์", "System")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    content_lower = message.content.lower()

    # ---------- ระบบอัตโนมัติ 1: คำต้องห้าม ----------
    if any(bad in content_lower for bad in BAD_WORDS):
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        modlog.add("BADWORD", str(message.author), message.author.id, "ใช้คำต้องห้าม", "Auto")
        await warn_user(message.author, "ใช้คำต้องห้ามในข้อความ", "Auto")
        try:
            await message.channel.send(
                f"🚫 ลบข้อความของ {message.author.mention} เนื่องจากมีคำต้องห้าม และแจ้งเตือนอัตโนมัติแล้ว",
                delete_after=8,
            )
        except discord.Forbidden:
            pass
        return

    # ---------- ระบบอัตโนมัติ 2: สแปม ----------
    now = time.time()
    dq = user_messages[message.author.id]
    dq.append(now)
    while dq and now - dq[0] > SPAM_TIME_WINDOW:
        dq.popleft()

    if len(dq) > SPAM_MSG_LIMIT:
        dq.clear()
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        modlog.add("SPAM", str(message.author), message.author.id, "ส่งข้อความรัวเกินกำหนด", "Auto")
        muted = await mute_member(message.author, SPAM_MUTE_MINUTES, "ส่งข้อความสแปม", "Auto")
        if muted:
            await message.channel.send(
                f"🔇 {message.author.mention} ถูกมิวท์ {SPAM_MUTE_MINUTES} นาที เนื่องจากส่งข้อความสแปม",
                delete_after=10,
            )
        return

    await bot.process_commands(message)


# ================= COMMANDS: ระบบคำเตือน (มือ, ไว้ใช้เสริมกรณีพิเศษ) =================
@bot.command(name="warn")
@is_mod()
async def warn_cmd(ctx, member: discord.Member, *, reason: str = "ไม่ระบุเหตุผล"):
    count = await warn_user(member, reason, str(ctx.author))
    await ctx.send(f"⚠️ {member.mention} ได้รับคำเตือน ({count}/{WARN_LIMIT}) เหตุผล: {reason}")


@bot.command(name="warnings")
@is_mod()
async def warnings_cmd(ctx, member: discord.Member):
    await ctx.send(f"{member.mention} มีคำเตือนสะสม {warnings[member.id]} ครั้ง")


@bot.command(name="clearwarn")
@is_mod()
async def clearwarn_cmd(ctx, member: discord.Member):
    warnings[member.id] = 0
    await ctx.send(f"✅ ล้างคำเตือนของ {member.mention} แล้ว")


# ================= COMMANDS: มิวท์ / ปลดมิวท์ =================
@bot.command(name="mute")
@is_mod()
async def mute_cmd(ctx, member: discord.Member, minutes: int = 10, *, reason: str = "ไม่ระบุเหตุผล"):
    ok = await mute_member(member, minutes, reason, str(ctx.author))
    if ok:
        await ctx.send(f"🔇 {member.mention} ถูกมิวท์ {minutes} นาที เหตุผล: {reason}")
    else:
        await ctx.send("❌ ไม่สามารถมิวท์สมาชิกคนนี้ได้ (สิทธิ์บอทไม่พอ)")


@bot.command(name="unmute")
@is_mod()
async def unmute_cmd(ctx, member: discord.Member):
    try:
        await member.timeout(None, reason=f"ปลดมิวท์โดย {ctx.author}")
        modlog.add("UNMUTE", str(member), member.id, "ปลดมิวท์", str(ctx.author))
        await ctx.send(f"🔊 ปลดมิวท์ {member.mention} แล้ว")
    except discord.Forbidden:
        await ctx.send("❌ ไม่สามารถปลดมิวท์ได้ (สิทธิ์บอทไม่พอ)")


# ================= COMMANDS: คำสั่งแอดมินพื้นฐาน =================
@bot.command(name="kick")
@is_mod()
async def kick_cmd(ctx, member: discord.Member, *, reason: str = "ไม่ระบุเหตุผล"):
    try:
        await member.kick(reason=reason)
        modlog.add("KICK", str(member), member.id, reason, str(ctx.author))
        await ctx.send(f"👢 เตะ {member.mention} ออกแล้ว เหตุผล: {reason}")
    except discord.Forbidden:
        await ctx.send("❌ ไม่มีสิทธิ์เตะสมาชิกคนนี้")


@bot.command(name="ban")
@is_mod()
async def ban_cmd(ctx, member: discord.Member, *, reason: str = "ไม่ระบุเหตุผล"):
    try:
        await member.ban(reason=reason)
        modlog.add("BAN", str(member), member.id, reason, str(ctx.author))
        await ctx.send(f"🔨 แบน {member.mention} แล้ว เหตุผล: {reason}")
    except discord.Forbidden:
        await ctx.send("❌ ไม่มีสิทธิ์แบนสมาชิกคนนี้")


@bot.command(name="clear")
@is_mod()
async def clear_cmd(ctx, amount: int = 10):
    deleted = await ctx.channel.purge(limit=amount + 1)
    modlog.add("CLEAR", str(ctx.author), ctx.author.id, f"ลบ {len(deleted) - 1} ข้อความ", str(ctx.author))
    await ctx.send(f"🧹 ลบข้อความ {len(deleted) - 1} ข้อความแล้ว", delete_after=5)


@warn_cmd.error
@mute_cmd.error
@unmute_cmd.error
@kick_cmd.error
@ban_cmd.error
@clear_cmd.error
@warnings_cmd.error
@clearwarn_cmd.error
async def cmd_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ ไม่พบสมาชิกที่ระบุ")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ ระบุข้อมูลไม่ครบ ตรวจสอบรูปแบบคำสั่งอีกครั้ง")
    else:
        raise error


# ================= WEB DASHBOARD =================
app = Flask(__name__)
app.secret_key = SECRET_KEY

LOGIN_PAGE = """
<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sentry Console — Sign in</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#0A0F0D; --surface:#121917; --surface-alt:#182220; --border:#24322E;
    --text:#E7F3EC; --muted:#7F958B; --cyan:#4FD1C5; --amber:#E8A33D; --red:#E5484D; --green:#3DDC84;
  }
  *{box-sizing:border-box;}
  body{
    margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    background:var(--bg); color:var(--text); font-family:'Inter',sans-serif;
    background-image: radial-gradient(circle at 50% 0%, rgba(79,209,197,0.08), transparent 60%);
  }
  .panel{
    width:340px; background:var(--surface); border:1px solid var(--border); border-radius:10px;
    padding:32px 28px; box-shadow:0 20px 60px rgba(0,0,0,0.5);
  }
  .eyebrow{font-family:'IBM Plex Mono',monospace; font-size:11px; letter-spacing:0.14em; color:var(--cyan); text-transform:uppercase; margin-bottom:6px;}
  h1{font-family:'IBM Plex Mono',monospace; font-size:20px; margin:0 0 22px; font-weight:600;}
  label{display:block; font-size:12px; color:var(--muted); margin-bottom:6px;}
  input{
    width:100%; padding:10px 12px; background:var(--surface-alt); border:1px solid var(--border);
    border-radius:6px; color:var(--text); font-family:'IBM Plex Mono',monospace; font-size:14px; margin-bottom:16px;
  }
  input:focus{outline:none; border-color:var(--cyan);}
  button{
    width:100%; padding:11px; background:var(--cyan); color:#06110F; border:none; border-radius:6px;
    font-family:'IBM Plex Mono',monospace; font-weight:600; font-size:13px; letter-spacing:0.04em; cursor:pointer;
  }
  button:hover{filter:brightness(1.08);}
  .err{color:var(--red); font-size:12px; margin:-8px 0 14px;}
</style>
</head>
<body>
  <form class="panel" method="POST">
    <div class="eyebrow">Moderation Bot</div>
    <h1>Sentry Console</h1>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <label for="pw">รหัสผ่านแดชบอร์ด</label>
    <input id="pw" type="password" name="password" autofocus required>
    <button type="submit">เข้าสู่ระบบ</button>
  </form>
</body>
</html>
"""

DASHBOARD_PAGE = """
<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sentry Console</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#0A0F0D; --surface:#121917; --surface-alt:#182220; --border:#24322E;
    --text:#E7F3EC; --muted:#7F958B; --cyan:#4FD1C5; --amber:#E8A33D; --red:#E5484D; --green:#3DDC84;
  }
  *{box-sizing:border-box;}
  body{margin:0; background:var(--bg); color:var(--text); font-family:'Inter',sans-serif;}
  header{
    display:flex; align-items:center; justify-content:space-between; padding:20px 32px;
    border-bottom:1px solid var(--border); position:sticky; top:0; background:rgba(10,15,13,0.9); backdrop-filter:blur(6px); z-index:5;
  }
  .brand{display:flex; align-items:center; gap:10px;}
  .dot{width:9px; height:9px; border-radius:50%; background:var(--green); box-shadow:0 0 0 0 rgba(61,220,132,0.6); animation:pulse 2s infinite;}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(61,220,132,0.5);}70%{box-shadow:0 0 0 8px rgba(61,220,132,0);}100%{box-shadow:0 0 0 0 rgba(61,220,132,0);}}
  .brand-text{font-family:'IBM Plex Mono',monospace; font-weight:600; font-size:15px; letter-spacing:0.02em;}
  .logout{color:var(--muted); font-size:12px; text-decoration:none; font-family:'IBM Plex Mono',monospace;}
  .logout:hover{color:var(--text);}
  main{padding:28px 32px 60px; max-width:1100px; margin:0 auto;}
  .stats{display:grid; grid-template-columns:repeat(5,1fr); gap:14px; margin-bottom:28px;}
  .stat{background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:16px 18px;}
  .stat .num{font-family:'IBM Plex Mono',monospace; font-size:26px; font-weight:700;}
  .stat .lbl{font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.08em; margin-top:4px;}
  .s-warn .num{color:var(--amber);} .s-mute .num{color:var(--red);} .s-spam .num{color:#F08A5D;}
  .s-join .num{color:var(--green);} .s-total .num{color:var(--cyan);}
  .toolbar{display:flex; align-items:center; justify-content:space-between; margin-bottom:12px;}
  .toolbar h2{font-family:'IBM Plex Mono',monospace; font-size:13px; text-transform:uppercase; letter-spacing:0.1em; color:var(--muted); font-weight:500; margin:0;}
  .filters{display:flex; gap:6px; flex-wrap:wrap;}
  .chip{
    font-family:'IBM Plex Mono',monospace; font-size:11px; padding:5px 10px; border-radius:20px;
    border:1px solid var(--border); color:var(--muted); cursor:pointer; background:transparent; transition:all .15s;
  }
  .chip.active{background:var(--cyan); color:#06110F; border-color:var(--cyan); font-weight:600;}
  .log{background:var(--surface); border:1px solid var(--border); border-radius:10px; overflow:hidden;}
  .row{
    display:grid; grid-template-columns:120px 90px 1fr 140px; gap:12px; padding:12px 18px;
    border-bottom:1px solid var(--border); font-family:'IBM Plex Mono',monospace; font-size:12.5px; align-items:center;
  }
  .row:last-child{border-bottom:none;}
  .row.head{color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:0.08em; background:var(--surface-alt);}
  .ts{color:var(--muted);}
  .tag{padding:2px 8px; border-radius:4px; font-size:10.5px; font-weight:600; width:fit-content;}
  .tag-WARN{background:rgba(232,163,61,0.15); color:var(--amber);}
  .tag-BADWORD{background:rgba(232,163,61,0.15); color:var(--amber);}
  .tag-SPAM{background:rgba(240,138,93,0.15); color:#F08A5D;}
  .tag-MUTE{background:rgba(229,72,77,0.15); color:var(--red);}
  .tag-UNMUTE{background:rgba(61,220,132,0.15); color:var(--green);}
  .tag-KICK{background:rgba(229,72,77,0.15); color:var(--red);}
  .tag-BAN{background:rgba(229,72,77,0.2); color:var(--red);}
  .tag-JOIN{background:rgba(79,209,197,0.15); color:var(--cyan);}
  .tag-CLEAR{background:rgba(127,149,139,0.15); color:var(--muted);}
  .detail{color:var(--text);}
  .who{color:var(--muted); text-align:right;}
  .empty{padding:40px; text-align:center; color:var(--muted); font-family:'IBM Plex Mono',monospace; font-size:13px;}
</style>
</head>
<body>
<header>
  <div class="brand"><span class="dot"></span><span class="brand-text">SENTRY CONSOLE</span></div>
  <a class="logout" href="/logout">ออกจากระบบ →</a>
</header>
<main>
  <div class="stats" id="stats"></div>
  <div class="toolbar">
    <h2>Event Stream</h2>
    <div class="filters" id="filters">
      <button class="chip active" data-f="ALL">ทั้งหมด</button>
      <button class="chip" data-f="WARN">คำเตือน</button>
      <button class="chip" data-f="SPAM">สแปม</button>
      <button class="chip" data-f="MUTE">มิวท์</button>
      <button class="chip" data-f="BAN">แบน/เตะ</button>
      <button class="chip" data-f="JOIN">เข้าร่วม</button>
    </div>
  </div>
  <div class="log">
    <div class="row head"><div>เวลา</div><div>ประเภท</div><div>รายละเอียด</div><div style="text-align:right;">ดำเนินการโดย</div></div>
    <div id="rows"></div>
  </div>
</main>
<script>
let currentFilter = "ALL";

function fmtTime(iso){
  const d = new Date(iso);
  return d.toLocaleTimeString('th-TH', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}

function render(data){
  const stats = document.getElementById('stats');
  const c = data.stats;
  const total = Object.values(c).reduce((a,b)=>a+b,0);
  stats.innerHTML = `
    <div class="stat s-total"><div class="num">${total}</div><div class="lbl">เหตุการณ์ทั้งหมด</div></div>
    <div class="stat s-warn"><div class="num">${(c.WARN||0)+(c.BADWORD||0)}</div><div class="lbl">คำเตือน</div></div>
    <div class="stat s-mute"><div class="num">${c.MUTE||0}</div><div class="lbl">มิวท์</div></div>
    <div class="stat s-spam"><div class="num">${c.SPAM||0}</div><div class="lbl">สแปมที่บล็อก</div></div>
    <div class="stat s-join"><div class="num">${c.JOIN||0}</div><div class="lbl">สมาชิกเข้าร่วม</div></div>
  `;

  const rowsEl = document.getElementById('rows');
  let events = data.events;
  if(currentFilter !== "ALL"){
    if(currentFilter === "BAN"){
      events = events.filter(e => e.type === "BAN" || e.type === "KICK");
    } else if(currentFilter === "WARN"){
      events = events.filter(e => e.type === "WARN" || e.type === "BADWORD");
    } else {
      events = events.filter(e => e.type === currentFilter);
    }
  }

  if(events.length === 0){
    rowsEl.innerHTML = '<div class="empty">ยังไม่มีเหตุการณ์ในหมวดนี้</div>';
    return;
  }

  rowsEl.innerHTML = events.map(e => `
    <div class="row">
      <div class="ts">${fmtTime(e.time)}</div>
      <div><span class="tag tag-${e.type}">${e.type}</span></div>
      <div class="detail">${e.user} — ${e.reason}</div>
      <div class="who">${e.moderator}</div>
    </div>
  `).join('');
}

async function refresh(){
  try{
    const res = await fetch('/api/logs');
    if(res.status === 401){ window.location.href = '/'; return; }
    const data = await res.json();
    render(data);
  }catch(err){ console.error(err); }
}

document.getElementById('filters').addEventListener('click', (e) => {
  if(e.target.classList.contains('chip')){
    document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
    e.target.classList.add('active');
    currentFilter = e.target.dataset.f;
    refresh();
  }
});

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


def logged_in():
    return session.get("authed") is True


@app.route("/", methods=["GET", "POST"])
def login():
    if logged_in():
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["authed"] = True
            return redirect(url_for("dashboard"))
        error = "รหัสผ่านไม่ถูกต้อง"
    return render_template_string(LOGIN_PAGE, error=error)


@app.route("/dashboard")
def dashboard():
    if not logged_in():
        return redirect(url_for("login"))
    return render_template_string(DASHBOARD_PAGE)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/logs")
def api_logs():
    if not logged_in():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"events": modlog.all(), "stats": modlog.stats()})


def run_flask():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    threading.Thread(target=run_flask, daemon=True).start()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ กรุณาตั้งค่า Environment Variable ชื่อ DISCORD_TOKEN บน Render")
    if DASHBOARD_PASSWORD == "changeme123":
        print("⚠️  กำลังใช้รหัสผ่านแดชบอร์ดค่าเริ่มต้น กรุณาตั้งค่า DASHBOARD_PASSWORD ใน Environment Variable")
    keep_alive()
    bot.run(TOKEN)
