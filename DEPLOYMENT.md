# 🚀 Deploying Pokédex Go to Render (100% Free)

This guide walks you through deploying your Telegram Pokédex Bot to **Render** completely free, including how to keep it running **24/7 without sleeping**.

---

## Prerequisites

1. **Telegram Bot Token**: Created via [@BotFather](https://t.me/BotFather) on Telegram.
2. **GitHub Account**: A free account on [github.com](https://github.com).
3. **Render Account**: A free account on [render.com](https://render.com).

---

## Step 1: Push Your Code to GitHub

If you haven't already pushed this repository to GitHub:

```bash
git init
git add .
git commit -m "Initial commit for Pokédex Go bot"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo-name>.git
git push -u origin main
```

*(Note: `.env`, `pokedex.db`, and `backups/` are automatically ignored by `.gitignore` so your secrets stay private).*

---

## Step 2: Create a Web Service on Render

1. Log in to [dashboard.render.com](https://dashboard.render.com).
2. Click **New +** (top right) and select **Web Service**.
3. Choose **Build and deploy from a Git repository** and connect your GitHub repository.
4. Fill in the deployment settings:

| Setting | Value |
| :--- | :--- |
| **Name** | `pokedex-go-bot` (or any custom name) |
| **Region** | Choose the one closest to you (e.g., Oregon, Frankfurt, Singapore) |
| **Branch** | `main` |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `python bot.py` |
| **Instance Type** | **Free** ($0/month) |

---

## Step 3: Configure Environment Variables

Scroll down to the **Environment Variables** section and add:

| Key | Value | Description |
| :--- | :--- | :--- |
| `BOT_TOKEN` | *your-telegram-bot-token* | **[Required]** From @BotFather |
| `PYTHON_VERSION` | `3.11.9` | Ensures compatible Python environment |
| `PORT` | `10000` | Port for the built-in health check server |
| `LOG_LEVEL` | `INFO` | Standard logging |
| `BOT_ADMIN_ID` | *your_user_id* | *(Optional)* Numeric ID from `@userinfobot` |

Click **Deploy Web Service**. Render will install the requirements and launch the bot!

---

## Step 4: Keep It Running 24/7 (Prevent Free Tier Sleep)

Render's free tier automatically puts Web Services to sleep if no HTTP request is received within 15 minutes. 

Because we built an **internal HTTP health server** into `bot.py` listening on `$PORT`, your bot serves a live health endpoint at your Render URL:
```
https://pokedex-go-bot.onrender.com/
```
*(Returns `{"status": "healthy", "bot": "Pokedex Go"}`)*

### Set up a Free 10-Minute Ping:
1. Go to [cron-job.org](https://cron-job.org) or [UptimeRobot.com](https://uptimerobot.com) (both 100% free).
2. Create a new monitor/job:
   * **URL:** `https://<your-render-app-name>.onrender.com/`
   * **Interval:** Every 10 minutes (or 5 minutes).
   * **HTTP Method:** `GET`.
3. Save the job.

**Result:** The ping keeps the Render free container active 24/7, so your Telegram bot will never go to sleep and will always respond instantly!

---

## Step 5: Database Notes (SQLite on Free Tier)

* The bot uses local SQLite (`pokedex.db`). 
* On Render's Free plan, the container disk is ephemeral (it resets if the service manually redeploys or restarts).
* Automatic snapshots are created in `backups/` before schema upgrades.
* For permanent zero-cost persistence across redeploys, you can periodically copy or download backups via the `/health` and backup mechanisms, or set `BACKUP_REMOTE_DIR` to a cloud-synced directory.
