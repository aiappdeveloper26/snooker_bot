# Snooker Tracker Bot (Render Free Tier Edition)

A Telegram bot to record snooker frame results between you and your friends,
and check head-to-head records / overall statistics.

This version is a **single file** (`snooker_bot.py`) using a **JSON file**
for storage instead of a database - same pattern as your other Render
free-tier bots (habit/quote bot), so deployment is identical.

## Features

- **/record** - guided, button-based flow to log a frame:
  - Date (defaults to today, or type any date)
  - Opponent (pick from saved friends or add a new one)
  - Who broke first (opened the frame)
  - Who won the frame
  - Highest break (optional - who scored it and how much)
  - After saving, you can immediately add another frame for the same
    date/opponent (handy for a whole session)
- **/h2h** - pick a friend and see your head-to-head record: total frames,
  win rate, recent form, performance when opening vs not, and highest breaks
  for both of you
- **/stats** - overall stats: total record, per-opponent breakdown, and the
  highest break ever recorded
- **/history** - your last 10 frames
- **/friends** - list saved friends
- **/delfriend** - remove a friend and all their records
- **/undo** - delete the most recently recorded frame
- **/cancel** - cancel whatever flow you're in

## How storage works (and the catch)

Everything lives in one file, `snooker_data.json`:

```json
{
  "<your_telegram_user_id>": {
    "friends": ["Alice", "Bob"],
    "frames": [
      {"friend": "Alice", "date": "2026-06-13", "opener": "me",
       "winner": "me", "break_player": "me", "break_value": 45}
    ]
  }
}
```

No SQL, no separate database module - `load_data()` reads the whole file
into a dict, handlers modify the dict, `save_data()` writes it back.

**The catch (same as your other free-tier bots):** Render's free Web
Service has a *temporary* disk. Every time the service restarts or
redeploys (including the auto-sleep after ~15 minutes of inactivity and the
next wake-up), `snooker_data.json` is reset to empty - your match history
is lost.

For trying things out, this is totally fine. If/when you want your snooker
history to be permanent, the cheapest free fixes are, roughly in order of
effort:
1. **A free Google Sheet** as the backing store (via the Google Sheets API)
   - bonus: you get a spreadsheet view of every frame for free.
2. **A free Postgres** from a provider like Supabase or Neon - more "proper"
   database, slightly more setup.
3. A **paid Render disk** (1GB starts cheap) mounted at e.g. `/data`, with
   `DATA_FILE=/data/snooker_data.json` - smallest code change (just the env
   var), but no longer free.

Ask if you'd like any of these added later - they're incremental changes,
not a rewrite.

## 1. Create your bot and get a token

1. Open Telegram and message **@BotFather**.
2. Send `/newbot` and follow the prompts to choose a name and username.
3. BotFather gives you a token like `123456789:AAExampleTokenStringHere`.
   Keep this secret.

## 2. Run it locally (optional)

```bash
cd snooker_bot
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
export BOT_TOKEN="123456789:AAExampleTokenStringHere"
python3 snooker_bot.py
```

Open Telegram, find your bot, send `/start`.

## 3. Deploy to Render (free Web Service)

### Step 1 - Push to GitHub

```bash
cd snooker_bot
git init
git add .
git commit -m "Snooker tracker bot"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

### Step 2 - Create the Web Service

1. In the Render dashboard, click **New +** -> **Web Service**.
2. Connect your GitHub and pick this repo.
3. Fill in:
   - **Runtime:** Python (auto-detected from `requirements.txt`)
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python snooker_bot.py`
   - **Instance Type:** **Free**
4. Under **Environment**, add:
   - `BOT_TOKEN` = your token from BotFather
   - `PYTHON_VERSION` = `3.12.8` (avoids picking an unsupported Python
     version)
5. Click **Create Web Service**. Watch the logs for `Snooker bot is
   running.`

(If you prefer, the included `render.yaml` lets you do this via **New +** ->
**Blueprint** instead - Render reads the config automatically and you just
fill in `BOT_TOKEN`.)

### Step 3 - Keep it awake

Free services sleep after ~15 minutes of no HTTP traffic. To prevent that:

1. Go to **uptimerobot.com**, sign up (free).
2. **Add New Monitor** -> Monitor Type: **HTTP(s)**, URL: your Render URL
   (e.g. `https://snooker-bot-xxxx.onrender.com`), Interval: **5 minutes**.

This pings the bot's built-in web server every 5 minutes so it stays awake
(and Telegram polling keeps working).

### Step 4 - Test

Send `/start`, then `/record` to log your first frame.

## Example session

```
/record
> Who did you play against?  [Alice] [Bob] [+ New friend]
  (tap Alice)
> What date was this frame played?  [Today] [Enter a different date]
  (tap Today)
> Who broke first?  [Me] [Alice]
  (tap Me)
> What was the frame score?
  Reply in the format your frames : Alice's frames, e.g. 2:2
  > 3:2
> Was there a notable highest break?  [Yes] [No]
  (tap Yes)
> Who scored the highest break?  [Me] [Alice]
  (tap Me)
> What was the break value?
  > 58
✅ Result saved!
📅 Date: 2026-06-13
🆚 Opponent: Alice
▶️ Opened: You
🏆 Score: You 3 - 2 Alice
💥 Highest break: 58 (You)

Add another session for the same date & opponent?  [Yes] [No]
```

Then later:

```
/h2h
> Head-to-head with whom?  [Alice] [Bob]
  (tap Alice)

🎱 Head-to-head vs Alice
Sessions played: 12
Frames played: 58
Your frame wins: 32
Alice's frame wins: 26
Your win rate: 55.2%
Recent form (most recent first): WWLWL

Break performance:
When you broke first: 18/30 won (60%)
When Alice broke first: 14/28 won by you (50%)

Highest breaks:
Yours: 58
Alice's: 71
```
