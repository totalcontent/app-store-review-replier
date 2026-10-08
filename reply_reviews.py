#!/usr/bin/env python3
"""
App Store review replier.

Pulls unanswered reviews from App Store Connect, drafts a kind support reply
to each one with Claude, and posts only the replies you approve.

Usage:
    python reply_reviews.py              # review, approve and post
    python reply_reviews.py --dry-run    # draft and show, never post
    python reply_reviews.py --days 90    # look further back (default from config)
    python reply_reviews.py --list-apps  # show your apps and their IDs
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import jwt
import requests

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.toml"
IGNORED_PATH = HERE / "ignored_reviews.json"
ASC = "https://api.appstoreconnect.apple.com"

SYSTEM_PROMPT = """You write public developer replies to App Store reviews.

Voice: {tone}

Rules:
- Reply in the same language the review is written in.
- Be warm, human and specific to what this person actually said. No canned phrases.
- Keep it short: 2 to 4 sentences. Plain text only, no markdown, no emoji unless the tone asks for it.
- Thank happy reviewers sincerely without gushing.
- For complaints: acknowledge the frustration, apologise where it is fair, never argue or get defensive.
- For bugs or problems you cannot solve in a reply, invite them to get in touch: {support_contact}
- If the developer has told you the status of a requested feature, say exactly that and no more: already available (say where to find it only if the developer told you), coming soon (no date unless the developer gave one), or something we will consider for the future (no promise that it will be built).
- Otherwise never promise features, fixes, refunds or dates. Never invent facts about the app.
- Never ask for personal information in the reply, and never mention that you are an AI.
- Do not ask them to change their rating.
- If the app deals with health or the body: stay matter-of-fact and respectful, never joke about the subject, never comment on the reviewer's own measurements or body, and never give medical advice or reassurance about what is normal. If they raise a health worry, kindly suggest speaking to a doctor.
- Sign off as: {signature}

About the app "{app_name}": {app_description}

{output_format}"""


# ---------- config ----------

def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing {CONFIG_PATH.name}. Copy config.example.toml to config.toml and fill it in.")
    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)
    for key in ("issuer_id", "key_id", "private_key_path"):
        if not cfg.get("app_store_connect", {}).get(key):
            sys.exit(f"config.toml: [app_store_connect] {key} is missing.")
    todo = [k for k, v in cfg["app_store_connect"].items() if "FILL_IN" in str(v)]
    todo += [f"support_contact for {bid}" for bid, a in cfg.get("apps", {}).items()
             if "FILL_IN" in str(a.get("support_contact", ""))]
    if todo:
        sys.exit("config.toml still has FILL_IN placeholders: " + ", ".join(todo))
    key = cfg.get("claude", {}).get("api_key", "")
    if "FILL_IN" in key:
        sys.exit("config.toml: [claude] api_key is still a FILL_IN placeholder.")
    if not key and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Add api_key under [claude] in config.toml, or set ANTHROPIC_API_KEY.")
    return cfg


# ---------- App Store Connect ----------

class AppStoreConnect:
    def __init__(self, cfg):
        self.issuer_id = cfg["issuer_id"]
        self.key_id = cfg["key_id"]
        key_path = Path(cfg["private_key_path"]).expanduser()
        if not key_path.is_absolute():
            key_path = HERE / key_path
        self.private_key = key_path.read_text()
        self._token = None
        self._token_expires = 0

    def _auth(self):
        # Apple tokens live at most 20 minutes; refresh a little early.
        now = int(time.time())
        if not self._token or now > self._token_expires - 60:
            self._token_expires = now + 15 * 60
            self._token = jwt.encode(
                {"iss": self.issuer_id, "iat": now, "exp": self._token_expires,
                 "aud": "appstoreconnect-v1"},
                self.private_key,
                algorithm="ES256",
                headers={"kid": self.key_id, "typ": "JWT"},
            )
        return {"Authorization": f"Bearer {self._token}"}

    def _request(self, method, url, **kwargs):
        if not url.startswith("http"):
            url = ASC + url
        r = requests.request(method, url, headers=self._auth(), timeout=30, **kwargs)
        if r.status_code >= 400:
            try:
                errors = r.json().get("errors", [])
                detail = "; ".join(e.get("detail") or e.get("title", "") for e in errors)
            except ValueError:
                detail = r.text[:300]
            raise RuntimeError(f"App Store Connect {r.status_code}: {detail}")
        return r.json() if r.content else {}

    def apps(self):
        out, url, params = [], "/v1/apps", {"limit": 200, "fields[apps]": "name,bundleId"}
        while url:
            page = self._request("GET", url, params=params)
            out += [{"id": a["id"], "name": a["attributes"]["name"],
                     "bundle_id": a["attributes"]["bundleId"]} for a in page["data"]]
            url, params = page.get("links", {}).get("next"), None
        return out

    def unanswered_reviews(self, app_id, since):
        """Newest first; stops paging once reviews are older than `since`."""
        out = []
        url = f"/v1/apps/{app_id}/customerReviews"
        params = {"exists[publishedResponse]": "false", "sort": "-createdDate", "limit": 100}
        while url:
            page = self._request("GET", url, params=params)
            for item in page["data"]:
                a = item["attributes"]
                created = datetime.fromisoformat(a["createdDate"])
                if created < since:
                    return out
                out.append({
                    "id": item["id"],
                    "rating": a["rating"],
                    "title": a.get("title") or "",
                    "body": a.get("body") or "",
                    "nickname": a.get("reviewerNickname") or "",
                    "territory": a.get("territory") or "",
                    "created": created,
                })
            url, params = page.get("links", {}).get("next"), None
        return out

    def post_response(self, review_id, text):
        body = {"data": {
            "type": "customerReviewResponses",
            "attributes": {"responseBody": text},
            "relationships": {"review": {"data": {"type": "customerReviews", "id": review_id}}},
        }}
        return self._request("POST", "/v1/customerReviewResponses", json=body)


# ---------- drafting ----------

FEATURE_STATUSES = {
    "1": "This feature is already available",
    "2": "This feature will be available soon",
    "3": "We'll consider adding this feature in the future",
}
DETECT_FORMAT = ("Output format: if the review asks for a feature, or says something is missing "
                 "from the app, output only one line and nothing else: FEATURE: <the feature in a "
                 "few words>. Bug reports and general complaints are not feature requests. "
                 "In every other case output only the reply text.")
REPLY_FORMAT = "Output only the reply text."


class Drafter:
    def __init__(self, cfg):
        self.client = anthropic.Anthropic(api_key=cfg.get("claude", {}).get("api_key") or None)
        self.cfg = cfg
        self.model = cfg.get("claude", {}).get("model", "claude-sonnet-5-5")

    def _call(self, app, output_format, messages):
        style = self.cfg.get("style", {})
        app_cfg = self.cfg.get("apps", {}).get(app["bundle_id"], {})
        system = SYSTEM_PROMPT.format(
            tone=style.get("tone", "Warm, friendly and down to earth."),
            support_contact=app_cfg.get("support_contact") or style.get("support_contact", "our support page"),
            signature=app_cfg.get("signature") or style.get("signature", f"The {app['name']} team"),
            app_name=app["name"],
            app_description=app_cfg.get("description", "(no description given)"),
            output_format=output_format,
        )
        msg = self.client.messages.create(
            model=self.model, max_tokens=600, system=system, messages=messages)
        return "".join(b.text for b in msg.content if b.type == "text").strip()

    @staticmethod
    def _review_text(review):
        return (f"Rating: {review['rating']} out of 5\n"
                f"Reviewer: {review['nickname']}\n"
                f"Title: {review['title']}\n"
                f"Review: {review['body']}")

    def first_pass(self, app, review):
        """Returns (feature_name, None) for a feature request, else (None, reply)."""
        text = self._call(app, DETECT_FORMAT, [{"role": "user", "content": self._review_text(review)}])
        if text.upper().startswith("FEATURE:"):
            return text.splitlines()[0][8:].strip() or "a feature", None
        return None, text

    def draft(self, app, review, feature=None, note=None, previous=None):
        user = self._review_text(review)
        if feature and feature["status"]:
            user += (f"\n\nThe reviewer is asking for: {feature['name']}\n"
                     f"Status from the developer: {feature['status']}.")
            if feature["detail"]:
                user += f"\nExtra detail from the developer: {feature['detail']}"
        elif feature:
            user += "\n\nThe developer says this is not a feature request. Reply to it as a normal review."
        messages = [{"role": "user", "content": user}]
        if previous and note:
            messages += [{"role": "assistant", "content": previous},
                         {"role": "user", "content": f"Rewrite the reply with this change: {note}"}]
        return self._call(app, REPLY_FORMAT, messages)


# ---------- interactive loop ----------

def load_ignored():
    if IGNORED_PATH.exists():
        return set(json.loads(IGNORED_PATH.read_text()))
    return set()


def save_ignored(ignored):
    IGNORED_PATH.write_text(json.dumps(sorted(ignored), indent=2))


def edit_text(text):
    editor = os.environ.get("EDITOR")
    if not editor:
        print("(No $EDITOR set. Type the full reply on one line, or press Enter to keep the draft.)")
        new = input("> ").strip()
        return new or text
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as f:
        f.write(text)
        path = f.name
    subprocess.call([*editor.split(), path])
    new = Path(path).read_text().strip()
    os.unlink(path)
    return new or text


def print_review(app, review, index, total):
    stars = "★" * review["rating"] + "☆" * (5 - review["rating"])
    print("\n" + "=" * 72)
    print(f"{app['name']}  ·  review {index} of {total}")
    print(f"{stars}  {review['nickname']}  ·  {review['territory']}  ·  {review['created']:%Y-%m-%d}")
    if review["title"]:
        print(f"\n  {review['title']}")
    print(f"  {review['body']}")
    print("-" * 72)


def ask_feature(app, review, name, index, total):
    """Ask the developer about a requested feature before drafting the reply."""
    print_review(app, review, index, total)
    print(f"This review seems to ask for: {name}\n")
    for key, label in FEATURE_STATUSES.items():
        print(f"  {key}  {label}")
    print("  0  This is not a feature request")
    while True:
        choice = input("Which one? [0-3, Enter = 3] > ").strip() or "3"
        if choice in ("0", *FEATURE_STATUSES):
            break
    if choice == "0":
        return {"name": name, "status": None, "detail": ""}
    detail = input("Anything to add, e.g. where to find it? (Enter to skip) > ").strip()
    return {"name": name, "status": FEATURE_STATUSES[choice], "detail": detail}


def show(app, review, reply, index, total, feature=None):
    print_review(app, review, index, total)
    if feature and feature["status"]:
        print(f"Feature: {feature['name']}  ->  {feature['status']}")
    print("Draft reply:\n")
    print(reply)
    print("-" * 72)


def run(args):
    cfg = load_config()
    asc = AppStoreConnect(cfg["app_store_connect"])

    apps = asc.apps()
    if args.list_apps:
        for a in apps:
            print(f"{a['name']:<40} {a['bundle_id']:<40} {a['id']}")
        return

    only = cfg.get("only_bundle_ids") or []
    if only:
        apps = [a for a in apps if a["bundle_id"] in only]
    for a in apps:  # optional per-app name override from config.toml
        a["name"] = cfg.get("apps", {}).get(a["bundle_id"], {}).get("name") or a["name"]
    if not apps:
        sys.exit("No apps found for this API key (check only_bundle_ids in config.toml).")

    days = args.days or cfg.get("lookback_days", 30)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    drafter = Drafter(cfg)
    ignored = load_ignored()
    posted = skipped = 0

    for app in apps:
        reviews = [r for r in asc.unanswered_reviews(app["id"], since) if r["id"] not in ignored]
        if not reviews:
            print(f"{app['name']}: nothing to answer in the last {days} days.")
            continue
        print(f"{app['name']}: {len(reviews)} unanswered review(s).")

        for i, review in enumerate(reviews, 1):
            feature = None
            name, reply = drafter.first_pass(app, review)
            if name:
                feature = ask_feature(app, review, name, i, len(reviews))
                reply = drafter.draft(app, review, feature=feature)
            while True:
                show(app, review, reply, i, len(reviews), feature)
                if args.dry_run:
                    break
                choice = input("[p]ost  [e]dit  [r]ewrite with a note  [s]kip  "
                               "[i]gnore forever  [q]uit > ").strip().lower()
                if choice == "p":
                    asc.post_response(review["id"], reply)
                    print("Posted. It can take a little while to appear on the App Store.")
                    posted += 1
                    break
                if choice == "e":
                    reply = edit_text(reply)
                elif choice == "r":
                    note = input("What should change? > ").strip()
                    if note:
                        reply = drafter.draft(app, review, feature=feature, note=note, previous=reply)
                elif choice == "s":
                    skipped += 1
                    break
                elif choice == "i":
                    ignored.add(review["id"])
                    save_ignored(ignored)
                    break
                elif choice == "q":
                    print(f"\nDone. Posted {posted}, skipped {skipped}.")
                    return

    if args.dry_run:
        print("\nDry run: nothing was posted.")
    else:
        print(f"\nDone. Posted {posted}, skipped {skipped}.")


def main():
    p = argparse.ArgumentParser(description="Reply to App Store reviews with Claude-drafted replies.")
    p.add_argument("--dry-run", action="store_true", help="draft and show replies, never post")
    p.add_argument("--days", type=int, help="how many days back to look")
    p.add_argument("--list-apps", action="store_true", help="list your apps and exit")
    args = p.parse_args()
    try:
        run(args)
    except (RuntimeError, anthropic.APIError) as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
