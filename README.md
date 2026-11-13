# IELTSATION daily channel bot

Publishes one AI-written post to your Telegram channel every day at about 09:00 Tashkent time. It runs on GitHub Actions, so there is no server and nothing to keep switched on.

| Day | Category |
|---|---|
| Monday | IELTS vocabulary |
| Tuesday | Grammar tips |
| Wednesday | IELTS Speaking |
| Thursday | Common English mistakes |
| Friday | Advanced vocabulary |
| Saturday | IELTS Reading tips |
| Sunday | Interesting English facts |

## One-time setup (about 15 minutes)

### 1. Create the Telegram bot
1. In Telegram, open **@BotFather** and send `/newbot`. Choose a name and a username.
2. Copy the token it gives you (looks like `1234567890:AAH...`). Keep it private.
3. Open your channel, go to **Administrators > Add admin**, add your bot, and allow **Post messages**.

### 2. Get an AI key
The default provider is Google Gemini, which has a free tier.
1. Go to <https://aistudio.google.com/apikey> and sign in.
2. Create an API key and copy it.

### 3. Put the code on GitHub
1. Create a free account at <https://github.com> and a new **private** repository.
2. Upload all the files from this folder, keeping the folder structure. The file `.github/workflows/daily-post.yml` must stay at exactly that path.

### 4. Add your three secrets
In the repository: **Settings > Secrets and variables > Actions > New repository secret**.

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `TELEGRAM_CHAT_ID` | your channel username with @, e.g. `@yourchannel` (for a private channel, the numeric id starting with `-100`) |
| `AI_API_KEY` | your Gemini key |

### 5. Test it
1. Open the **Actions** tab, choose **Daily IELTSATION post**, then **Run workflow**.
2. Tick **Preview only** the first time. Open the run and read the post in the log.
3. Run it again without the tick. The post appears in your channel.

From then on it posts by itself every day.

## Changing things

Everything you may want to change is in `config.json`. Edit it on GitHub (pencil icon) and commit.

- **Writing style**: edit `style.voice`. For the closest match to your own writing, paste 2-3 of your real posts into `style.example_posts`, each as one string in quotes (use `\n` for line breaks).
- **Translations**: `style.translation_language` (now Uzbek in Cyrillic script). Set it to `""` to switch them off.
- **Extra rules**: add sentences to `style.extra_rules`, e.g. `"Always finish with: Save this post!"`.
- **Footer under each post**: `footer` (now `@ieltsation`). Set it to `""` to remove it.
- **Words you already posted before the bot**: add them to `already_published_topics` so they are never repeated.
- **Post length**: `style.min_chars` and `style.max_chars`.
- **Topics and hashtags for each day**: the `brief` and `hashtags` under `schedule`.
- **Posting time**: the `cron` line in `.github/workflows/daily-post.yml`. It is in UTC, so subtract 5 hours from Tashkent time.
- **AI provider**: set `ai.provider` to `gemini`, `openai` or `anthropic`, and `ai.model` to that provider's model name. `openai` also works with Groq or OpenRouter if you set `ai.base_url`.

## How it avoids repeats

Every published topic is saved in `history.json` with its date and category. Before writing, the bot gives the AI the list of topics already used and tells it to choose something different. Then it compares the new topic with every old one itself, and if they are too similar it throws the draft away and asks again (up to 4 tries). If no fresh post can be made, nothing is published.

## Good to know

- GitHub often starts scheduled runs 5-20 minutes late, so posts land around 09:00, not at the exact minute.
- If a run fails (expired key, bot removed from the channel), GitHub emails you. Nothing is posted that day; fix the cause and press **Run workflow**.
- AI model names are retired from time to time. The bot tries several Gemini names in order; if all stop working, put a current name in `ai.model`.
- AI can make mistakes, including in grammar explanations and "facts". Skim the channel now and then, and delete or edit a post in Telegram if one is wrong.
- Never put your token or keys in `config.json` or any file. They belong only in GitHub secrets.
