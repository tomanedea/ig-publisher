#!/usr/bin/env python3
"""
Publish due Instagram carousels, reels and stories through the official Instagram API
(Instagram Login, graph.instagram.com). Runs every 15 minutes on GitHub Actions.

  schedule.json         what to publish and when (written by the local sync tool)
  state/published.json  what's already done: key -> {media_id | skipped | error, at}
  media branch          the JPEGs, served at MEDIA_BASE_URL/<path>

Secrets (env): IG_TOKENS = {"<username>": "<instagram user access token>", ...}

Rules:
- At most one carousel and one story batch per account per run, so posts never bunch up.
- Carousels more than 3 h late and stories more than 1 h late are skipped (recorded, never retried).
- Errors are recorded and retried on the next runs, up to 3 attempts in total.
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://graph.instagram.com/v25.0"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state", "published.json")
MAX_LATE = {"carousel": dt.timedelta(hours=3), "reel": dt.timedelta(hours=3), "story": dt.timedelta(hours=1)}
MAX_ATTEMPTS = 3


def call(method, path, token, **params):
    params["access_token"] = token
    data = urllib.parse.urlencode(params).encode()
    url = f"{API}/{path}"
    if method == "GET":
        req = urllib.request.Request(url + "?" + data.decode())
    else:
        req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path}: {e.code} {e.read().decode()[:400]}") from None


def wait_ready(container_id, token, timeout=180):
    """Containers must reach FINISHED before they can be published."""
    end = time.time() + timeout
    while time.time() < end:
        status = call("GET", container_id, token, fields="status_code").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"container {container_id} status {status}")
        time.sleep(5)
    raise RuntimeError(f"container {container_id} not ready after {timeout}s")


def publish_item(item, ig_id, token, base_url):
    urls = [f"{base_url}/{p}" for p in item["images"]]
    if item["type"] == "reel":
        c = call("POST", f"{ig_id}/media", token, media_type="REELS", video_url=urls[0],
                 caption=item["caption"], share_to_feed="true")["id"]
        wait_ready(c, token, timeout=600)   # video processing takes longer
        return call("POST", f"{ig_id}/media_publish", token, creation_id=c)["id"]
    if item["type"] == "story":
        c = call("POST", f"{ig_id}/media", token, image_url=urls[0], media_type="STORIES")["id"]
    elif len(urls) == 1:
        c = call("POST", f"{ig_id}/media", token, image_url=urls[0], caption=item["caption"])["id"]
    else:
        children = [call("POST", f"{ig_id}/media", token, image_url=u, is_carousel_item="true")["id"] for u in urls]
        for ch in children:
            wait_ready(ch, token)
        c = call("POST", f"{ig_id}/media", token, media_type="CAROUSEL",
                 children=",".join(children), caption=item["caption"])["id"]
    wait_ready(c, token)
    return call("POST", f"{ig_id}/media_publish", token, creation_id=c)["id"]


def one_pass():
    dry = "--dry-run" in sys.argv
    only = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")), None)
    tokens = json.loads(os.environ.get("IG_TOKENS", "{}"))
    base_url = os.environ["MEDIA_BASE_URL"].rstrip("/")
    schedule = json.load(open(os.path.join(HERE, "schedule.json")))["items"]
    state = json.load(open(STATE_PATH)) if os.path.exists(STATE_PATH) else {}
    now = dt.datetime.now(dt.timezone.utc)

    ig_ids, done_this_run = {}, set()
    for item in sorted(schedule, key=lambda i: i["scheduled_at"]):
        key = item["key"]
        if only and key != only:
            continue
        rec = state.get(key, {})
        if "media_id" in rec or "skipped" in rec or rec.get("attempts", 0) >= MAX_ATTEMPTS:
            continue
        due = dt.datetime.fromisoformat(item["scheduled_at"])
        if due > now and not only:
            continue
        acct = item["account"]
        if not only and now - due > MAX_LATE[item["type"]]:
            state[key] = {"skipped": f"too late ({int((now - due).total_seconds() // 60)} min)", "at": now.isoformat()}
            print(f"SKIP {key}: too late")
            continue
        # one carousel per account per run; story q/a pairs may go together
        slot = (acct, item["type"] if item["type"] in ("carousel", "reel") else f"story-{item['scheduled_at']}")
        if item["type"] in ("carousel", "reel") and slot in done_this_run:
            continue
        if acct not in tokens:
            print(f"NO TOKEN for {acct}, leaving {key}")
            continue
        if dry:
            print(f"DRY would publish {key} ({item['type']}, {len(item['images'])} img) due {item['scheduled_at']}")
            continue
        try:
            if acct not in ig_ids:
                ig_ids[acct] = call("GET", "me", tokens[acct], fields="user_id,username")["user_id"]
            media_id = publish_item(item, ig_ids[acct], tokens[acct], base_url)
            state[key] = {"media_id": media_id, "at": now.isoformat()}
            done_this_run.add(slot)
            print(f"OK   {key} -> {media_id}")
        except Exception as e:  # recorded and retried next run
            attempts = rec.get("attempts", 0) + 1
            state[key] = {"error": str(e)[:500], "attempts": attempts, "at": now.isoformat()}
            print(f"ERR  {key} (attempt {attempts}): {e}")

    changed = False
    if not dry:
        old = json.load(open(STATE_PATH)) if os.path.exists(STATE_PATH) else {}
        changed = old != state
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        json.dump(state, open(STATE_PATH, "w"), indent=1, sort_keys=True)
    return changed


def save_state():
    """Commit and push state right away, so a later run never re-publishes."""
    os.system('git add state/published.json && (git diff --cached --quiet || '
              '(git commit -q -m "state: $(date -u +%FT%TZ)" && git pull -q --rebase && git push -q))')


def main():
    # --loop=MIN: keep checking every 60 s for MIN minutes. GitHub's cron is unreliable
    # (runs can be hours late), so each run covers several hours on its own.
    loop = next((int(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--loop=")), 0)
    end = time.time() + loop * 60
    passes = 0
    while True:
        if loop and passes % 10 == 0:
            os.system("git pull -q --rebase")   # pick up newly synced schedules without waiting for the next run
        passes += 1
        if one_pass() and loop:
            save_state()
        if time.time() + 60 > end:
            break
        time.sleep(60)


if __name__ == "__main__":
    main()
