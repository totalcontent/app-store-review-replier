# App Store review replier

Pulls your unanswered App Store reviews, drafts a kind reply to each one with
Claude, and posts only the ones you approve.

## Setup (once, about 10 minutes)

1. **Create an App Store Connect API key.** In App Store Connect go to
   Users and Access > Integrations > App Store Connect API > Team Keys and
   generate a key. Pick the **Customer Support** role: it is the narrowest
   role that can reply to reviews. Download the `.p8` file (you can only
   download it once) and put it in this folder. Note the Key ID and the
   Issuer ID shown on that page.
2. **Get a Claude API key** at https://console.anthropic.com and paste it
   into `config.toml` as `api_key` under `[claude]`. (Leave that line out and
   the tool uses the `ANTHROPIC_API_KEY` environment variable instead.) API
   usage is billed separately from a Claude subscription; a reply costs a
   fraction of a cent.
3. **Install** (needs Python 3.11 or newer):

       python3 -m venv .venv
       source .venv/bin/activate
       pip install -r requirements.txt

4. **Configure:** copy `config.example.toml` to `config.toml`, fill in the
   three App Store Connect values, your tone, support contact and signature.

## Use

    python reply_reviews.py --list-apps   # check the key works
    python reply_reviews.py --dry-run     # see drafts, post nothing
    python reply_reviews.py               # approve and post
    python reply_reviews.py --rating 4-5  # positive reviews only (also: 5, 1-2, 1,2)

For each review you choose: **p**ost, **e**dit, **r**ewrite with a note
("shorter", "mention the fix is coming"), **s**kip, **i**gnore forever, or
**q**uit. Nothing is posted without you pressing `p`.

When a review asks for a feature, the tool first asks you whether it is
already available, coming soon, or something you will consider, and writes
the reply to match. You can add a detail such as where to find it.

Replies are written in the reviewer's language. For languages you don't
read (see `i_read` in the config), the tool shows a translation of the review
and of the draft reply. Only the reply itself is ever posted.

## Good to know

- Replies are public and can take a while to show up on the App Store.
- Posting to a review that already has a reply replaces that reply. The tool
  only fetches reviews without one.
- Keep the `.p8` file and `config.toml` private. `.gitignore` already
  excludes them.
