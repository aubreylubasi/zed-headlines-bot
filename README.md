# 🇿🇲 Zambia News Bot

Automatically posts new Zambian news to your Facebook Page, around the clock.

Every 20 minutes it checks **Mwebantu, News Diggers!, ZNBC, Zambia Daily Mail, Times of Zambia,
Zambia Monitor and Lusaka Times**, picks the newest stories it hasn't posted yet, and posts them
to your Page as a headline with the article link (Facebook adds the photo preview card).

It runs free on **GitHub Actions**, so your computer doesn't need to be on.

What it handles for you:
- **No double posts.** It remembers every story it has posted (`state/posted.json`).
- **No repeats across sites.** If Mwebantu and ZNBC both cover the same story, it's posted once.
- **No flooding.** At most 2 posts per run and 30 per day. Nothing is posted between 23:00 and 05:00, and stories older than 12 hours are skipped.
- **Variety.** Each run takes from different sites before a second story from the same site.
- **Ads filtered out.** Headlines containing words like "sponsored" or "advertorial" are skipped.
- **Self-repair.** If a site moves its feed, the bot looks for the new one by itself.

You can change all of this in `config.toml`.

---

## Setup (about 30 minutes, one time only)

### Step 1 – Create the Facebook Page
Go to **facebook.com/pages/create**. Pick a name, set the category to *News & Media Website*, and add a profile picture and cover.

### Step 2 – Put the bot on GitHub
1. Sign in at **github.com** (or create a free account).
2. Click **New repository**. Name it something like `zambia-news-bot`, choose **Public**, and click **Create**.
   (Public repos get unlimited free running time. Your Facebook token stays hidden in Secrets. On a
   private repo, change the schedule in the workflow to `*/30` to stay within the free 2,000 minutes a month.)
3. Click **uploading an existing file** and drag in `bot.py`, `config.toml`, `requirements.txt`,
   `get_page_token.py`, `PRIVACY.md`, `README.md` and `.gitignore`. Click **Commit changes**.
4. Add the workflow. Folders that start with a dot often get skipped when you upload, so create it directly:
   **Add file → Create new file**, name it exactly `.github/workflows/news-bot.yml`,
   paste in the contents of that file from this folder, and click **Commit changes**.

### Step 3 – Get a Facebook Page token
1. Go to **developers.facebook.com → My Apps → Create App**.
   - Name: e.g. *Zambia News Bot*.
   - When it asks for a use case, choose the one about **managing a Page** (something like *"Manage everything on your Page"*). If it asks for an app type, choose **Business**. Meta changes these labels often.
2. In the app, open **App settings → Basic**:
   - **Privacy Policy URL:** `https://github.com/YOUR-USERNAME/zambia-news-bot/blob/main/PRIVACY.md`
   - Pick a **Category**, then click **Save**.
   - Write down the **App ID** and **App Secret**.
3. Open the **Graph API Explorer**: developers.facebook.com/tools/explorer
   - In **Meta App**, choose your app.
   - Under **Permissions**, add `pages_show_list`, `pages_read_engagement` and `pages_manage_posts`.
   - Click **Generate Access Token**. When Facebook asks which Pages to allow, **tick your news Page**.
4. **Make the token permanent.** Pick one option.

   **Option A: in the browser (nothing to install)**
   1. Copy the token. Open the **Access Token Debugger** (developers.facebook.com/tools/debug/accesstoken), paste it in, click **Debug**, then click **Extend Access Token** at the bottom. Copy the new long token.
   2. Back in Graph API Explorer, paste that long token into the **Access Token** box. In the query box, type `me/accounts` and click **Submit**.
   3. Find your Page in the result. Copy its `id` (this is your **FB_PAGE_ID**) and its `access_token` (this is your **FB_PAGE_TOKEN**).
   4. Paste the Page token into the Debugger to confirm it says **Expires: Never**.

   **Option B: with Python.** Run `pip install requests`, then `python get_page_token.py`, and paste in what it asks for. It prints the Page ID and a permanent token, and checks that the token never expires.

5. **Switch the app to Live.** At the top of the app dashboard, flip **App Mode** from *Development* to *Live*.
   ⚠️ While the app is in Development mode, its posts are visible **only to you**. Everyone else sees nothing.

> Keep the token secret. Anyone who has it can post on your Page. Put it **only** into GitHub Secrets (next step), never into a file.

### Step 4 – Give the token to GitHub
In your repository, go to **Settings → Secrets and variables → Actions → New repository secret** and add two secrets:

| Name | Value |
|---|---|
| `FB_PAGE_ID` | the Page `id` from step 3 |
| `FB_PAGE_TOKEN` | the Page `access_token` from step 3 |

### Step 5 – Test, then switch it on
Open the **Actions** tab. If GitHub asks, click **"I understand my workflows, go ahead and enable them"**.
Then click **Zambia News Bot → Run workflow** and choose a mode:

1. **check-sources** shows which news sites the bot can read (✅ or ❌).
2. **dry-run** shows exactly what it would post, without posting anything.
3. **post** is a real run. The first run marks everything already on the sites as "seen" and posts only the newest story, so your Page doesn't get 100 posts at once.

From then on it runs **every 20 minutes by itself**. To see what it did, open the **Actions** tab and click any run.

---

## Changing how it behaves
Edit `config.toml` on GitHub: click the file, then the ✏️ pencil icon, make your change, and click **Commit**. Some examples:

- **Post more or less:** change `max_posts_per_run` and `max_posts_per_day`.
- **Post through the night:** set `quiet_hours_start = 0` and `quiet_hours_end = 0`.
- **Change the post wording:** edit `post_template` and `hashtags`.
- **Add a short teaser line under the headline:** set `include_summary = true`.
- **Turn a site off:** set `enabled = false` under it.
- **Add a site:** copy a `[[sources]]` block and change the `name`, `site` and `feed`. Most Zambian news sites run on WordPress, so the feed is usually `https://THE-SITE/feed/`.
- **Add an international source with Zambia-only stories:** see the BBC example at the bottom of `config.toml` (`require_keywords`).

To pause the bot: **Actions → Zambia News Bot → ⋯ → Disable workflow**.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| Posts appear for you but friends can't see them | The Meta app is still in **Development** mode. Switch it to **Live** (step 3.5). |
| Run fails with **code 190** | The token stopped working. This happens if you change your Facebook password or remove the app. Repeat step 3 and update the `FB_PAGE_TOKEN` secret. |
| Run fails with **code 200 / 10** (permission) after going Live | Meta wants **Advanced Access** for `pages_manage_posts`. In the app, go to **App Review → Permissions and Features** and request it. |
| Run fails with **code 368** | Facebook thinks the Page is posting too much. Lower `max_posts_per_day`, wait a day, then run again. |
| A site shows ❌ in check-sources | The site may be down, may have moved its feed, or may block GitHub's servers. The bot skips it and carries on with the others. You can fix its `feed` or set `enabled = false`. |
| Runs start a few minutes late | That's normal. GitHub delays scheduled runs when it's busy. |
| GitHub email: "workflow disabled" | Public repos with no activity for 60 days get paused. Open Actions and enable the workflow again. |

## Files
| File | What it is |
|---|---|
| `bot.py` | the bot |
| `config.toml` | settings and news sources (edit this one) |
| `.github/workflows/news-bot.yml` | runs the bot every 20 minutes on GitHub |
| `state/posted.json` | created automatically: which stories have been posted |
| `get_page_token.py` | optional helper for step 3 |
| `PRIVACY.md` | privacy policy Meta asks for when you create the app |
