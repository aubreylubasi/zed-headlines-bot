#!/usr/bin/env python3
"""
Turns the short-lived token from Facebook's Graph API Explorer into a
Page access token that does not expire, and prints your Page ID.

Run:  python get_page_token.py
You'll be asked for your App ID, App Secret and the token from Graph API Explorer.
Nothing is saved to disk. Paste the results into GitHub → Settings → Secrets.
"""
import getpass
import sys

import requests

VERSION = "v26.0"
GRAPH = f"https://graph.facebook.com/{VERSION}"


def fail(msg: str, resp: requests.Response | None = None) -> None:
    if resp is not None:
        try:
            msg += f"\n   Facebook says: {resp.json().get('error', {}).get('message', resp.text[:300])}"
        except ValueError:
            msg += f"\n   HTTP {resp.status_code}"
    sys.exit(f"\n❌ {msg}")


def main() -> None:
    print("Get a permanent Facebook Page token\n")
    app_id = input("App ID: ").strip()
    app_secret = getpass.getpass("App Secret (typing is hidden): ").strip()
    short_token = getpass.getpass("User token from Graph API Explorer (hidden): ").strip()

    r = requests.get(f"{GRAPH}/oauth/access_token", params={
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": short_token,
    }, timeout=30)
    if not r.ok or "access_token" not in r.json():
        fail("Could not extend the user token. Check the App ID/Secret and that the token is fresh.", r)
    long_user_token = r.json()["access_token"]

    r = requests.get(f"{GRAPH}/me/accounts", params={
        "access_token": long_user_token,
        "fields": "id,name,access_token,tasks",
    }, timeout=30)
    if not r.ok:
        fail("Could not list your Pages.", r)
    pages = r.json().get("data", [])
    if not pages:
        fail("No Pages found. Make sure you ticked your Page when Facebook asked which Pages to allow, "
             "and that the token has pages_show_list + pages_manage_posts.")

    for page in pages:
        dbg = requests.get(f"{GRAPH}/debug_token", params={
            "input_token": page["access_token"],
            "access_token": f"{app_id}|{app_secret}",
        }, timeout=30).json().get("data", {})
        expires = "never ✅" if dbg.get("expires_at") == 0 else f"at unix time {dbg.get('expires_at')} ⚠️"
        scopes = ", ".join(dbg.get("scopes", [])) or "unknown"
        can_post = "CREATE_CONTENT" in page.get("tasks", [])
        print("\n" + "─" * 60)
        print(f"Page:        {page['name']}")
        print(f"FB_PAGE_ID:  {page['id']}")
        print(f"Expires:     {expires}")
        print(f"Permissions: {scopes}")
        print(f"Can post:    {'yes' if can_post else 'NO – you need full control of this Page'}")
        print(f"FB_PAGE_TOKEN:\n{page['access_token']}")
    print("\n" + "─" * 60)
    print("Copy FB_PAGE_ID and FB_PAGE_TOKEN for your news page into GitHub secrets. Keep the token private.")


if __name__ == "__main__":
    main()
