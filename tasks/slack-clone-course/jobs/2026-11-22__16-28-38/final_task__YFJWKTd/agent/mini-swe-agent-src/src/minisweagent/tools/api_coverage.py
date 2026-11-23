"""`api-coverage`: maps which parts of the task's contract the running cluster serves.

It drives the cluster on 127.0.0.1 (HTTP nodes 8000-8002, WebSocket, IRC on 6667, Redis on 6379,
and the UI served at GET /) with one shallow request per contract route, and reports only what is
missing or broken. It checks the status code and the response's top-level key, not the detailed
behaviour behind them. Standard library only, and it stops itself after about 25 seconds so it
stays under the shell's 30 second command limit.
"""

import base64
import json
import os
import re
import secrets
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

HOST = "127.0.0.1"
DEADLINE = time.time() + 26
results: list[tuple[str, str, str, str]] = []  # (area, verdict, probe, detail)

TESTIDS_INITIAL = """auth-form auth-submit auth-toggle workspace-create-form empty-state-create-workspace join-workspace-form
join-workspace-submit channel-list dms-list new-channel-btn current-user logout-btn workspace-header workspace-settings-btn
workspace-settings-close workspace-name-input workspace-join-mode workspace-general-submit channel-title channel-topic
channel-settings-btn channel-settings-title channel-settings-close channel-members-list channel-add-member-form
channel-add-member-input channel-add-member-submit channel-topic-input channel-topic-submit archive-channel-btn
unarchive-channel-btn create-channel-form create-channel-private create-channel-submit create-channel-cancel message-list
message-input send-btn thread-panel thread-input thread-send close-thread settings-tab-members settings-tab-invitations
settings-tab-general settings-pane-members settings-pane-invitations settings-pane-general create-invitation-btn
create-invitation-form create-invitation-submit invitations-list""".split()
DOM_IDS = ["auth-modal", "create-channel-modal", "channel-settings-modal", "workspace-settings-modal", "create-invitation-form"]
BUTTON_ROLES = {
    "primary": "auth-submit send-btn thread-send create-channel-submit join-workspace-submit workspace-general-submit "
    "create-invitation-submit channel-topic-submit channel-add-member-submit",
    "danger": "archive-channel-btn logout-btn",
    "secondary": "new-channel-btn create-invitation-btn unarchive-channel-btn create-channel-cancel close-thread "
    "channel-settings-close workspace-settings-close channel-settings-btn workspace-settings-btn",
}


def record(area: str, verdict: str, probe: str, detail: str = "") -> bool:
    results.append((area, verdict, probe, detail))
    return verdict == "OK"


def http(method: str, path: str, body=None, token: str | None = None, port: int = 8000, raw: bytes | None = None,
         ctype: str = "application/json"):
    """Returns (status, parsed_json_or_None, headers, text). status 0 means no connection."""
    if time.time() > DEADLINE:
        return -1, None, {}, "time budget used up"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(f"http://{HOST}:{port}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", ctype)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            status, headers, text = r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, headers, text = e.code, dict(e.headers or {}), e.read().decode("utf-8", "replace")
    except Exception as e:  # connection refused, timeout, reset
        return 0, None, {}, f"{type(e).__name__}: {e}"
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    return status, parsed, headers, text


def probe(area: str, label: str, method: str, path: str, want: tuple[int, ...], key: str | None = None, **kw):
    """One contract probe. Returns the parsed JSON body when the probe passed, else None."""
    status, js, _, text = http(method, path, **kw)
    name = f"{method} {label}"
    if status == -1:
        record(area, "SKIPPED", name, "time budget used up")
    elif status == 0:
        record(area, "DOWN", name, f"no answer on :{kw.get('port', 8000)} ({text[:80]})")
    elif status in want:
        if key and not (isinstance(js, dict) and key in js):
            got = list(js)[:6] if isinstance(js, dict) else (text[:60] or "empty body")
            record(area, "WRONG", name, f"status {status} ok, but no top-level '{key}' (got {got})")
        else:
            record(area, "OK", name)
            return js if js is not None else {}
    elif status >= 500:
        record(area, "ERROR", name, f"got {status}, want {'/'.join(map(str, want))}; body: {text[:120]!r}")
    elif status in (404, 405, 501) and not set(want) & {404, 405}:
        record(area, "MISSING", name, f"got {status} (route not served?), want {'/'.join(map(str, want))}")
    else:
        record(area, "WRONG", name, f"got {status}, want {'/'.join(map(str, want))}; body: {text[:120]!r}")
    return None


def skip(area: str, name: str, why: str):
    record(area, "SKIPPED", name, why)


def ws_connect(port: int, token: str):
    s = socket.create_connection((HOST, port), timeout=3)
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    s.sendall((f"GET /api/ws?token={token} HTTP/1.1\r\nHost: {HOST}:{port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(4096)
        if not chunk:
            break
        head += chunk
    status = int(head.split(b" ")[1]) if head.startswith(b"HTTP/") else 0
    return s, status, head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""


def ws_send(s, obj):
    data, mask = json.dumps(obj).encode(), secrets.token_bytes(4)
    n = len(data)
    hdr = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n))
    s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def ws_frames(s, buf: bytes, until: float):
    """Yields decoded JSON text frames until the deadline. Close frame or EOF ends it."""
    while time.time() < until:
        while len(buf) >= 2:
            op, n, off = buf[0] & 0x0F, buf[1] & 0x7F, 2
            if n == 126:
                if len(buf) < 4:
                    break
                n, off = struct.unpack(">H", buf[2:4])[0], 4
            elif n == 127:
                if len(buf) < 10:
                    break
                n, off = struct.unpack(">Q", buf[2:10])[0], 10
            if len(buf) < off + n:
                break
            payload, buf = buf[off:off + n], buf[off + n:]
            if op == 8:
                return
            if op == 1:
                try:
                    yield json.loads(payload)
                except ValueError:
                    pass
        s.settimeout(max(0.05, until - time.time()))
        try:
            chunk = s.recv(65536)
        except (TimeoutError, socket.timeout):
            return
        if not chunk:
            return
        buf += chunk


def check_cluster():
    for i in range(3):
        status, js, _, text = http("GET", "/api/health", port=8000 + i)
        if status == 0:
            record("Cluster", "DOWN", f"GET /api/health :{8000 + i}", f"no answer ({text[:80]}); is /app/start.sh running?")
        elif status == 200 and isinstance(js, dict) and js.get("status") == "ok" and js.get("node_id") == i:
            record("Cluster", "OK", f"GET /api/health :{8000 + i}")
        else:
            record("Cluster", "WRONG", f"GET /api/health :{8000 + i}", f"got {status} {text[:80]!r}, want 200 status=ok node_id={i}")
    try:
        with socket.create_connection((HOST, 6379), timeout=2) as s:
            s.sendall(b"PING\r\n")
            ok = s.recv(64).startswith(b"+PONG")
        record("Cluster", "OK" if ok else "WRONG", "redis PING :6379", "" if ok else "no +PONG")
    except OSError as e:
        record("Cluster", "DOWN", "redis PING :6379", f"no redis-server on 127.0.0.1:6379 ({e})")
    record("Cluster", "OK" if os.access("/app/start.sh", os.X_OK) else "MISSING", "/app/start.sh executable")
    return any(r[1] == "OK" and r[2] == "GET /api/health :8000" for r in results)


def check_api():
    sfx = secrets.token_hex(3)
    ua, ub, uc = f"cov_a_{sfx}", f"cov_b_{sfx}", f"cov_c_{sfx}"
    pw = "password123"
    js = probe("Auth", "/api/auth/register", "POST", "/api/auth/register", (201,), "token",
               body={"username": ua, "password": pw, "display_name": "Cov A"})
    if not js:
        skip("Auth", "everything after register", "register did not return 201 {user, token}")
        return None
    ta, a_id = js["token"], (js.get("user") or {}).get("id")
    tb_js = http("POST", "/api/auth/register", {"username": ub, "password": pw})[1] or {}
    tc_js = http("POST", "/api/auth/register", {"username": uc, "password": pw})[1] or {}
    tb, b_id = tb_js.get("token"), (tb_js.get("user") or {}).get("id")
    tc = tc_js.get("token")
    probe("Auth", "/api/auth/register (duplicate)", "POST", "/api/auth/register", (409,), body={"username": ua, "password": pw})
    probe("Auth", "/api/auth/register (bad username)", "POST", "/api/auth/register", (400,), body={"username": "a b", "password": pw})
    probe("Auth", "/api/auth/login", "POST", "/api/auth/login", (200,), "token", body={"username": ua, "password": pw})
    probe("Auth", "/api/auth/login (wrong password)", "POST", "/api/auth/login", (401,), body={"username": ua, "password": "wrongwrong"})
    probe("Auth", "/api/auth/me", "GET", "/api/auth/me", (200,), "user", token=ta)
    probe("Auth", "/api/auth/me (no token)", "GET", "/api/auth/me", (401,))

    probe("Profiles", "/api/users/{id}", "GET", f"/api/users/{a_id}", (200,), "user", token=ta)
    probe("Profiles", "/api/users/{unknown}", "GET", "/api/users/999999999", (404,), token=ta)
    probe("Profiles", "/api/users/me", "PATCH", "/api/users/me", (200,), "user", token=ta, body={"status_text": "hi"})
    probe("Profiles", "/api/users/me (bad timezone)", "PATCH", "/api/users/me", (400,), token=ta, body={"timezone": "Not/AZone"})

    slug = f"cov-{sfx}"
    js = probe("Workspaces", "/api/workspaces", "POST", "/api/workspaces", (201,), "workspace", token=ta, body={"slug": slug, "name": "Cov"})
    if not js:
        skip("Workspaces", "everything that needs a workspace", "POST /api/workspaces did not return 201 {workspace}")
        return ta
    general = (js.get("general_channel") or {}).get("id")
    if not general:
        record("Workspaces", "WRONG", "POST /api/workspaces (general_channel)", "no general_channel.id in the 201 body")
    probe("Workspaces", "/api/workspaces (duplicate slug)", "POST", "/api/workspaces", (409,), token=ta, body={"slug": slug, "name": "Cov"})
    probe("Workspaces", "/api/workspaces", "GET", "/api/workspaces", (200,), "workspaces", token=ta)
    probe("Workspaces", "/api/workspaces/{slug}", "GET", f"/api/workspaces/{slug}", (200,), "channels", token=ta)
    probe("Workspaces", "/api/workspaces/{slug}/members", "GET", f"/api/workspaces/{slug}/members", (200,), "members", token=ta)
    probe("Workspaces", "/api/workspaces/{slug}", "PATCH", f"/api/workspaces/{slug}", (200,), "workspace", token=ta, body={"name": "Cov 2"})

    js = probe("Channels", "/api/workspaces/{slug}/channels", "POST", f"/api/workspaces/{slug}/channels", (201,), "channel",
               token=ta, body={"name": "cov-room"})
    ch = (js or {}).get("channel", {}).get("id") or general
    probe("Channels", "/api/workspaces/{slug}/channels (duplicate)", "POST", f"/api/workspaces/{slug}/channels", (409,),
          token=ta, body={"name": "cov-room"})
    if not ch:
        skip("Channels", "everything that needs a channel", "no channel could be created")
        return ta
    probe("Channels", "/api/channels/{id}/join", "POST", f"/api/channels/{ch}/join", (200,), token=tb)
    probe("Channels", "/api/channels/{unknown}/join", "POST", "/api/channels/999999999/join", (404,), token=tb)
    probe("Channels", "/api/channels/{id} (topic)", "PATCH", f"/api/channels/{ch}", (200,), "channel", token=ta, body={"topic": "t"})
    probe("Channels", "/api/channels/{id}/members", "GET", f"/api/channels/{ch}/members", (200,), "members", token=ta)
    probe("Channels", "/api/channels/{id}/leave", "POST", f"/api/channels/{ch}/leave", (200,), "channel", token=tb)
    http("POST", f"/api/channels/{ch}/join", token=tb)

    msgs = f"/api/channels/{ch}/messages"
    js = probe("Messages", "/api/channels/{id}/messages", "POST", msgs, (201,), "message", token=ta,
               body={"body": f"hello coverage @{ub}"})
    mid = (js or {}).get("message", {}).get("id")
    probe("Messages", "/api/channels/{id}/messages (empty body)", "POST", msgs, (400,), token=ta, body={"body": ""})
    probe("Messages", "/api/channels/{id}/messages", "GET", f"{msgs}?limit=10", (200,), "next_cursor", token=ta)
    probe("Messages", "/api/channels/{id}/messages (bad cursor)", "GET", f"{msgs}?cursor=%%%bogus", (400,), token=ta)
    probe("Messages", "/api/channels/{id}/messages (limit=500)", "GET", f"{msgs}?limit=500", (400,), token=ta)
    if mid:
        probe("Messages", "/api/messages/{id}", "PATCH", f"/api/messages/{mid}", (200,), "message", token=ta, body={"body": "edited"})
        probe("Messages", "/api/channels/{id}/messages (reply)", "POST", msgs, (201,), "message", token=tb,
              body={"body": "a reply", "parent_id": mid})
        probe("Messages", "/api/messages/{id}/replies", "GET", f"/api/messages/{mid}/replies", (200,), "replies", token=ta)
        probe("Reactions & pins", "/api/messages/{id}/reactions", "POST", f"/api/messages/{mid}/reactions", (200,), "reactions",
              token=tb, body={"emoji": "👍"})
        probe("Reactions & pins", "/api/messages/{id}/reactions/{emoji}", "DELETE",
              f"/api/messages/{mid}/reactions/{urllib.request.quote('👍')}", (200,), "reactions", token=tb)
        probe("Reactions & pins", "/api/messages/{id}/pin", "POST", f"/api/messages/{mid}/pin", (200,), "pin", token=ta)
        probe("Reactions & pins", "/api/channels/{id}/pins", "GET", f"/api/channels/{ch}/pins", (200,), "pins", token=ta)
        probe("Reactions & pins", "/api/messages/{id}/pin", "DELETE", f"/api/messages/{mid}/pin", (200,), "unpinned", token=ta)
    else:
        skip("Messages", "edit/reply/reactions/pins", "posting a message did not return 201 {message}")

    if b_id:
        probe("DMs & files", "/api/dms", "POST", "/api/dms", (200,), "channel_id", token=ta, body={"user_id": b_id})
    boundary = "covb" + secrets.token_hex(6)
    raw = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"cov.txt\"\r\n"
           f"Content-Type: text/plain\r\n\r\nhello file\r\n--{boundary}--\r\n").encode()
    js = probe("DMs & files", "/api/files (multipart)", "POST", "/api/files", (201,), "file", token=ta, raw=raw,
               ctype=f"multipart/form-data; boundary={boundary}")
    fid = (js or {}).get("file", {}).get("id")
    if fid:
        probe("DMs & files", "/api/files/{id}", "GET", f"/api/files/{fid}", (200,), "file", token=ta)
        status, _, headers, _ = http("GET", f"/api/files/{fid}/download", token=ta)
        disp = {k.lower(): v for k, v in headers.items()}.get("content-disposition", "")
        record("DMs & files", "OK" if status == 200 and "filename" in disp else ("MISSING" if status in (404, 405) else "WRONG"),
               "GET /api/files/{id}/download", "" if status == 200 and "filename" in disp else f"got {status}, Content-Disposition={disp!r}")
        probe("DMs & files", "/api/channels/{id}/messages (file_ids)", "POST", msgs, (201,), "message", token=ta,
              body={"body": "with file", "file_ids": [fid]})
    probe("DMs & files", "/api/search", "GET", "/api/search?q=coverage", (200,), "results", token=ta)

    probe("Slash & read state", "messages body='/shrug hi'", "POST", msgs, (201,), "message", token=ta, body={"body": "/shrug hi"})
    probe("Slash & read state", "messages body='/bogus'", "POST", msgs, (400,), token=ta, body={"body": "/bogus"})
    probe("Slash & read state", "messages body='/topic new'", "POST", msgs, (201,), "channel", token=ta, body={"body": "/topic new"})
    probe("Slash & read state", "/api/channels/{id}/read", "GET", f"/api/channels/{ch}/read", (200,), "read_state", token=ta)
    probe("Slash & read state", "/api/channels/{id}/read", "POST", f"/api/channels/{ch}/read", (200,), "read_state", token=ta,
          body={"last_read_event_id": 1})

    gbase = f"/api/workspaces/{slug}/groups"
    probe("Groups, invites, roles", "/api/workspaces/{slug}/groups", "POST", gbase, (200, 201), "group", token=ta,
          body={"handle": "covteam", "name": "Cov team", "user_ids": [x for x in (a_id, b_id) if x]})
    probe("Groups, invites, roles", "/api/workspaces/{slug}/groups", "GET", gbase, (200,), "groups", token=ta)
    probe("Groups, invites, roles", "/api/workspaces/{slug}/groups/{handle}", "PATCH", f"{gbase}/covteam", (200,), "group",
          token=ta, body={"name": "Cov team 2"})
    probe("Groups, invites, roles", "/api/workspaces/{slug}/groups/{handle}", "DELETE", f"{gbase}/covteam", (200,), "deleted", token=ta)
    ibase = f"/api/workspaces/{slug}/invitations"
    js = probe("Groups, invites, roles", "/api/workspaces/{slug}/invitations", "POST", ibase, (201,), "invitation", token=ta,
               body={"max_uses": 2})
    code = (js or {}).get("invitation", {}).get("code")
    probe("Groups, invites, roles", "/api/workspaces/{slug}/invitations", "GET", ibase, (200,), "invitations", token=ta)
    if code:
        if tc:
            probe("Groups, invites, roles", "/api/invitations/{code}/accept", "POST", f"/api/invitations/{code}/accept", (200,), token=tc)
        probe("Groups, invites, roles", "/api/workspaces/{slug}/invitations/{code}", "DELETE", f"{ibase}/{code}", (200,), "deleted", token=ta)
        probe("Groups, invites, roles", "/api/invitations/{revoked}/accept", "POST", f"/api/invitations/{code}/accept", (404,), token=tb)
    if b_id:
        probe("Groups, invites, roles", "/api/workspaces/{slug}/members/{user_id}", "PATCH", f"/api/workspaces/{slug}/members/{b_id}",
              (200,), "member", token=ta, body={"role": "admin"})
        probe("Groups, invites, roles", "/api/workspaces/{slug}/members/{user_id} (role=owner)", "PATCH",
              f"/api/workspaces/{slug}/members/{b_id}", (400,), token=ta, body={"role": "owner"})

    probe("Archive", "/api/channels/{id}/archive", "POST", f"/api/channels/{ch}/archive", (200,), "channel", token=ta)
    probe("Archive", "/api/channels/{id}/messages (archived)", "POST", msgs, (423,), token=ta, body={"body": "x"})
    probe("Archive", "/api/channels/{id}/unarchive", "POST", f"/api/channels/{ch}/unarchive", (200,), "channel", token=ta)
    if mid:
        probe("Messages", "/api/messages/{id}", "DELETE", f"/api/messages/{mid}", (200,), "deleted", token=ta)
    if b_id:
        probe("Groups, invites, roles", "/api/workspaces/{slug}/transfer_ownership", "POST", f"/api/workspaces/{slug}/transfer_ownership",
              (200,), token=ta, body={"new_owner_id": b_id})
    return ta, slug, ch


def rule(area: str, name: str, ok: bool, detail: str = "") -> bool:
    return record(area, "OK" if ok else "WRONG", f"rule: {name}", "" if ok else detail)


def st(method: str, path: str, body=None, token=None, port: int = 8000, **kw):
    status, js, headers, text = http(method, path, body, token, port, **kw)
    return status, (js if isinstance(js, dict) else {}), f"got {status} {text[:100]!r}"


def check_rules():
    """Second tier: one request (or a short sequence) per key behaviour rule of the contract."""
    sfx = secrets.token_hex(3)
    pw = "password123"
    users = {}
    for n in ("o", "m", "g", "x"):
        _, js, _ = st("POST", "/api/auth/register", {"username": f"cr_{n}_{sfx}", "password": pw})
        if not js.get("token"):
            skip("Rules", "all rule probes", "register failed")
            return
        users[n] = (js["token"], (js.get("user") or {}).get("id"), f"cr_{n}_{sfx}")
    (to, oid, _), (tm, mid_, mname), (tg, gid, _), (tx, xid, _) = users["o"], users["m"], users["g"], users["x"]
    s, _, d = st("POST", "/api/auth/register", {"username": f"cr_p_{sfx}", "password": "short"})
    rule("Rules: auth", "register with password < 8 chars -> 400", s == 400, d)

    slug = f"cr-{sfx}"
    s, js, d = st("POST", "/api/workspaces", {"slug": slug, "name": "CR"}, to)
    if s != 201:
        skip("Rules", "workspace rules", "POST /api/workspaces failed")
        return
    general = (js.get("general_channel") or {}).get("id")
    s, _, d = st("POST", "/api/workspaces", {"slug": "Bad Slug!", "name": "x"}, to)
    rule("Rules: workspaces", "workspace slug 'Bad Slug!' -> 400", s == 400, d)
    st("POST", f"/api/channels/{general}/join", None, tm)
    st("POST", f"/api/channels/{general}/join", None, tg)
    st("PATCH", f"/api/workspaces/{slug}/members/{gid}", {"role": "guest"}, to)
    s, _, d = st("POST", f"/api/workspaces/{slug}/channels", {"name": "guest-try"}, tg)
    rule("Rules: roles", "guest creates a channel -> 403", s == 403, d)
    s, _, d = st("POST", f"/api/workspaces/{slug}/channels", {"name": "priv-try", "is_private": True}, tm)
    rule("Rules: roles", "member creates a private channel -> 403", s == 403, d)
    s, _, d = st("PATCH", f"/api/workspaces/{slug}", {"name": "hack"}, tm)
    rule("Rules: roles", "member PATCHes the workspace -> 403", s == 403, d)
    s, _, d = st("POST", f"/api/workspaces/{slug}/transfer_ownership", {"new_owner_id": mid_}, to)
    rule("Rules: roles", "transfer_ownership to a non-admin -> 400", s == 400, d)
    s, _, d = st("POST", f"/api/workspaces/{slug}/invitations", {}, tm)
    rule("Rules: invitations", "member creates an invitation -> 403", s == 403, d)
    s, js, d = st("POST", f"/api/workspaces/{slug}/invitations", {"invited_user_id": xid}, to)
    code = (js.get("invitation") or {}).get("code")
    if code:
        s, _, d = st("POST", f"/api/invitations/{code}/accept", None, tg)
        rule("Rules: invitations", "another user accepts a targeted invitation -> 403", s == 403, d)
    s, _, d = st("POST", "/api/invitations/nope-not-a-code/accept", None, tx)
    rule("Rules: invitations", "unknown invitation code -> 404", s == 404, d)

    s, js, d = st("POST", f"/api/workspaces/{slug}/channels", {"name": "rules"}, to)
    ch = (js.get("channel") or {}).get("id")
    if not ch:
        skip("Rules", "channel and message rules", "channel create failed")
        return
    st("POST", f"/api/channels/{ch}/join", None, tm)
    s, js, d = st("POST", f"/api/channels/{ch}/join", None, tg)
    rule("Rules: roles", "guest joins a public channel -> 403", s == 403, d)
    s, _, d = st("PATCH", f"/api/channels/{ch}", {"topic": "t" * 251}, to)
    rule("Rules: channels", "topic of 251 chars -> 400", s == 400, d)
    s, _, d = st("POST", f"/api/workspaces/{slug}/channels", {"name": "Bad Name"}, to)
    rule("Rules: channels", "channel name 'Bad Name' -> 400", s == 400, d)
    s, _, d = st("POST", f"/api/channels/{ch}/archive", None, tm)
    rule("Rules: channels", "non-owner archives a channel -> 403", s == 403, d)

    msgs = f"/api/channels/{ch}/messages"
    ids = []
    for i in range(3):
        s, js, d = st("POST", msgs, {"body": f"page {i} zebra{sfx}"}, to)
        ids.append((js.get("message") or {}).get("id"))
    s, p1, d = st("GET", f"{msgs}?limit=2", None, to)
    cur = p1.get("next_cursor")
    first = [m.get("id") for m in p1.get("messages", []) if isinstance(m, dict)]
    rule("Rules: messages", "limit=2 returns 2 newest-first messages and a string next_cursor",
         len(first) == 2 and first[0] == ids[-1] and isinstance(cur, str), f"ids {first}, next_cursor {cur!r}")
    if isinstance(cur, str):
        s, p2, d = st("GET", f"{msgs}?limit=2&cursor={urllib.request.quote(cur)}", None, to, port=8001)
        second = [m.get("id") for m in p2.get("messages", []) if isinstance(m, dict)]
        rule("Rules: messages", "next_cursor (used on another node) continues without overlap",
             s == 200 and second and second[0] == ids[0] and not set(second) & set(first), f"page 2 ids {second} ({d})")
    m0 = ids[0]
    if m0:
        s, js, d = st("PATCH", f"/api/messages/{m0}", {"body": "hijack"}, tm)
        rule("Rules: messages", "non-author edits a message -> 403", s == 403, d)
        s, js, d = st("PATCH", f"/api/messages/{m0}", {"body": "edited"}, to)
        rule("Rules: messages", "edit sets edited_at", bool((js.get("message") or {}).get("edited_at")), d)
        st("POST", msgs, {"body": "r1", "parent_id": m0}, tm)
        s, js, d = st("GET", f"{msgs}?limit=50", None, to)
        parent = next((m for m in js.get("messages", []) if isinstance(m, dict) and m.get("id") == m0), {})
        rule("Rules: messages", "a reply bumps the parent's reply_count to 1", parent.get("reply_count") == 1,
             f"parent reply_count={parent.get('reply_count')!r}")
        st("POST", f"/api/messages/{m0}/reactions", {"emoji": "🔥"}, tm)
        s, js, d = st("POST", f"/api/messages/{m0}/reactions", {"emoji": "🔥"}, tm)
        r = [x for x in js.get("reactions", []) if isinstance(x, dict) and x.get("emoji") == "🔥"]
        rule("Rules: reactions", "reaction is idempotent: {emoji, count: 1, user_ids: [user]}",
             len(r) == 1 and r[0].get("count") == 1 and mid_ in (r[0].get("user_ids") or []), f"reactions={js.get('reactions')!r}")
        s, js, d = st("DELETE", f"/api/messages/{m0}", None, to)
        s, js, d = st("PATCH", f"/api/messages/{m0}", {"body": "again"}, to)
        rule("Rules: messages", "editing a deleted message -> 409", s == 409, d)
        s, js, d = st("GET", f"{msgs}?limit=50", None, to)
        rule("Rules: messages", "deleted message disappears from the listing (or is tombstoned)",
             not any(isinstance(m, dict) and m.get("id") == m0 and not m.get("deleted") and not m.get("is_deleted")
                     for m in js.get("messages", [])), "deleted message still listed")
    s, js, d = st("POST", msgs, {"body": f"hi @{mname} and me@example.com"}, to)
    ment = (js.get("message") or {}).get("mentions")
    rule("Rules: mentions", "@username resolves into mentions (email-shaped text does not)", ment == [mid_], f"mentions={ment!r}")
    s, js, d = st("GET", f"/api/channels/{ch}/read", None, tm)
    rs = js.get("read_state") or {}
    rule("Rules: read state", "unread_count and mention_count count others' messages",
         (rs.get("unread_count") or 0) > 0 and (rs.get("mention_count") or 0) >= 1, f"read_state={rs!r}")
    s, js, d = st("GET", f"/api/search?q=zebra{sfx}", None, to)
    rule("Rules: search", "search finds a posted word", len(js.get("results") or []) >= 1, d)
    s, js, d = st("GET", f"/api/search?q=zebra{sfx}", None, tx)
    rule("Rules: search", "search hides channels the caller cannot access",
         s == 200 and not js.get("results"), f"outsider got {len(js.get('results') or [])} results")
    s, js, d = st("POST", msgs, {"body": "/shrug ok"}, to)
    rule("Rules: slash", "/shrug ok -> body 'ok ¯\\_(ツ)_/¯'", (js.get("message") or {}).get("body") == "ok ¯\\_(ツ)_/¯", d)
    s, js, d = st("POST", msgs, {"body": "/me"}, to)
    rule("Rules: slash", "/me without text -> 400", s == 400, d)
    s, js, d = st("POST", msgs, {"body": "//literal"}, to)
    rule("Rules: slash", "//literal is posted literally (201)", s == 201, d)
    st("POST", f"/api/channels/{ch}/archive", None, to)
    s, js, d = st("POST", msgs, {"body": "/unarchive"}, to)
    rule("Rules: slash", "/unarchive on an archived channel -> 201, channel unarchived",
         s == 201 and (js.get("channel") or {}).get("is_archived") is False, d)

    boundary = "crb" + secrets.token_hex(6)
    big = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"big.bin\"\r\n"
           f"Content-Type: application/octet-stream\r\n\r\n").encode() + b"0" * (10 * 1024 * 1024 + 10) + f"\r\n--{boundary}--\r\n".encode()
    s, js, d = st("POST", "/api/files", None, to, raw=big, ctype=f"multipart/form-data; boundary={boundary}")
    rule("Rules: files", "upload over 10 MiB -> 413", s == 413, d)
    small = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"m.txt\"\r\n"
             f"Content-Type: text/plain\r\n\r\nmine\r\n--{boundary}--\r\n").encode()
    s, js, d = st("POST", "/api/files", None, tm, raw=small, ctype=f"multipart/form-data; boundary={boundary}")
    fid = (js.get("file") or {}).get("id")
    if fid:
        s, js, d = st("POST", msgs, {"body": "steal", "file_ids": [fid]}, to)
        rule("Rules: files", "attaching someone else's file -> 400", s == 400, d)
    return to, ch, slug


def check_live(token: str, ch, slug: str):
    """Event stream rules: dense event_id, resume replay, IRC <-> web bridge."""
    try:
        s, status, buf = ws_connect(8002, token)
    except OSError:
        return
    if status != 101:
        return
    ws_send(s, {"type": "subscribe", "channel_id": ch})
    sub = next((f for f in ws_frames(s, buf, time.time() + 1.5) if isinstance(f, dict) and f.get("type") == "subscribed"), {})
    head = sub.get("head_event_id")
    for i in range(2):
        http("POST", f"/api/channels/{ch}/messages", {"body": f"dense {i}"}, token=token, port=8000 + i)
    evs = [f.get("event_id") for f in ws_frames(s, b"", time.time() + 2) if isinstance(f, dict) and f.get("type") == "message.created"]
    s.close()
    rule("Rules: websocket", "two posts on two nodes -> message.created with event_id head+1, head+2",
         isinstance(head, int) and evs[:2] == [head + 1, head + 2], f"head_event_id={head!r}, event_ids seen={evs}")
    if isinstance(head, int):
        try:
            s, status, buf = ws_connect(8000, token)
            ws_send(s, {"type": "resume", "channel_id": ch, "since_event_id": head})
            frames = [f for f in ws_frames(s, buf, time.time() + 2) if isinstance(f, dict)]
            s.close()
            got = [f.get("event_id") for f in frames if f.get("type") == "message.created"]
            rule("Rules: websocket", "resume since_event_id replays the missed events, then 'resumed'",
                 got[:2] == [head + 1, head + 2] and any(f.get("type") == "resumed" for f in frames),
                 f"frames: {[(f.get('type'), f.get('event_id')) for f in frames][:6]}")
        except OSError:
            pass
    try:
        irc = socket.create_connection((HOST, 6667), timeout=2)
        nick = "crn" + secrets.token_hex(2)
        irc.sendall(f"PASS {token}\r\nNICK {nick}\r\nUSER {nick} 0 * :c\r\nJOIN #{slug}/rules\r\n".encode())
        data, until = "", time.time() + 3
        while time.time() < until and " 366 " not in data:
            irc.settimeout(max(0.05, until - time.time()))
            try:
                data += irc.recv(4096).decode("utf-8", "replace")
            except (TimeoutError, socket.timeout):
                break
        irc.sendall(f"PRIVMSG #{slug}/rules :from irc {nick}\r\n".encode())
        http("POST", f"/api/channels/{ch}/messages", {"body": f"from web {nick}"}, token=token, port=8001)
        data, until = "", time.time() + 3
        while time.time() < until and f"from web {nick}" not in data:
            irc.settimeout(max(0.05, until - time.time()))
            try:
                data += irc.recv(4096).decode("utf-8", "replace")
            except (TimeoutError, socket.timeout):
                break
        irc.close()
        rule("Rules: irc", "a web post reaches the IRC client as PRIVMSG", f"PRIVMSG #{slug}/rules :from web {nick}" in data,
             f"IRC got: {data[-150:]!r}")
        _, js, _, _ = http("GET", f"/api/channels/{ch}/messages?limit=10", token=token, port=8002)
        bodies = [m.get("body") for m in (js or {}).get("messages", []) if isinstance(m, dict)]
        rule("Rules: irc", "an IRC PRIVMSG shows up in the web message list", f"from irc {nick}" in bodies, f"latest bodies: {bodies[:4]}")
    except OSError:
        pass


def port_pids(port: int) -> set[int]:
    try:
        import subprocess
        out = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return set()
    return {int(p) for p in re.findall(r"pid=(\d+)", out)}


def check_resilience():
    """--resilience: SIGKILLs the process on :8001 and redis-server, then checks the cluster heals (start.sh must respawn them)."""
    import signal
    for port, name, wait in ((8001, "node 1 (:8001)", 12), (6379, "redis-server (:6379)", 8)):
        pids = port_pids(port)
        if not pids:
            rule("Rules: resilience", f"{name} is killed and comes back", False, f"no process listens on :{port}")
            continue
        for p in pids:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(1)
        others = [http("GET", "/api/health", port=q)[0] for q in (8000, 8002)]
        rule("Rules: resilience", f"after killing {name}, :8000 and :8002 still answer", others == [200, 200], f"health codes {others}")
        back, until = False, time.time() + wait
        while time.time() < until and not back:
            time.sleep(1)
            back = bool(port_pids(port))
        rule("Rules: resilience", f"{name} is respawned by start.sh within {wait}s", back, "nothing listens again (start.sh must supervise and restart it)")


def check_ws(token: str, ch):
    try:
        s, status, _ = ws_connect(8000, "not-a-real-token")
        s.close()
        record("WebSocket", "OK" if status != 101 else "WRONG", "ws :8000 with a bad token is rejected",
               "" if status != 101 else "handshake succeeded (101) with a bogus token")
    except OSError as e:
        record("WebSocket", "DOWN", "ws :8000 with a bad token is rejected", f"cannot connect ({e})")
    if time.time() > DEADLINE - 5:
        skip("WebSocket", "subscribe + cross-node delivery", "time budget used up")
        return
    try:
        s, status, buf = ws_connect(8001, token)
    except OSError as e:
        record("WebSocket", "DOWN", "ws :8001 /api/ws?token=<valid>", f"cannot connect ({e})")
        return
    if status != 101:
        record("WebSocket", "MISSING" if status in (404, 405) else "WRONG", "ws :8001 /api/ws?token=<valid>", f"handshake got {status}, want 101")
        return
    record("WebSocket", "OK", "ws :8001 /api/ws?token=<valid>")
    ws_send(s, {"type": "subscribe", "channel_id": ch})
    frames = list(ws_frames(s, buf, time.time() + 1.5))
    sub = [f for f in frames if isinstance(f, dict) and f.get("type") == "subscribed"]
    record("WebSocket", "OK" if sub and "head_event_id" in sub[0] else "WRONG", "subscribe -> {type:'subscribed', head_event_id}",
           "" if sub and "head_event_id" in sub[0] else f"frames seen: {str(frames)[:150]}")
    http("POST", f"/api/channels/{ch}/messages", {"body": "ws coverage"}, token=token, port=8002)
    frames = list(ws_frames(s, b"", time.time() + 2.5))
    got = [f for f in frames if isinstance(f, dict) and f.get("type") == "message.created"]
    record("WebSocket", "OK" if got and isinstance(got[0].get("event_id"), int) else "MISSING",
           "post on :8002 -> message.created with event_id on :8001", "" if got and isinstance(got[0].get("event_id"), int)
           else f"no message.created frame within 2.5s (frames seen: {str(frames)[:150]})")
    s.close()


def check_irc(token: str, slug: str):
    def lines_until(s, pattern: str, secs: float) -> str:
        data, until = "", time.time() + secs
        while time.time() < until and not re.search(pattern, data, re.M):
            s.settimeout(max(0.05, until - time.time()))
            try:
                chunk = s.recv(4096)
            except (TimeoutError, socket.timeout):
                break
            if not chunk:
                break
            data += chunk.decode("utf-8", "replace")
        return data

    try:
        s = socket.create_connection((HOST, 6667), timeout=2)
    except OSError as e:
        record("IRC", "DOWN", "connect :6667", f"no IRC gateway ({e})")
        return
    nick = "cov" + secrets.token_hex(2)
    s.sendall(f"PASS {token}\r\nNICK {nick}\r\nUSER {nick} 0 * :cov\r\n".encode())
    data = lines_until(s, r"^\S+ 422 ", 3)
    ok = all(re.search(rf"^\S+ {n} ", data, re.M) for n in ("001", "002", "003", "004", "005", "422"))
    record("IRC", "OK" if ok else "WRONG", "PASS/NICK/USER -> 001..005 + 422", "" if ok else f"got: {data[:150]!r}")
    s.sendall(f"JOIN #{slug}/general\r\n".encode())
    data = lines_until(s, r"^\S+ 366 ", 3)
    ok = bool(re.search(r"^\S+ 366 ", data, re.M))
    record("IRC", "OK" if ok else "WRONG", f"JOIN #{{slug}}/general -> 353 + 366", "" if ok else f"got: {data[:150]!r}")
    s.sendall(b"FOOBAR\r\n")
    data = lines_until(s, r"^\S+ 421 ", 2)
    record("IRC", "OK" if re.search(r"^\S+ 421 ", data, re.M) else "WRONG", "unknown command -> 421", "" if " 421 " in data else f"got: {data[:100]!r}")
    s.sendall(b"QUIT\r\n")
    s.close()
    try:
        s = socket.create_connection((HOST, 6667), timeout=2)
        s.sendall(b"PASS wrong-token\r\nNICK covbad\r\nUSER covbad 0 * :x\r\n")
        data = lines_until(s, r"^\S+ 464 ", 2)
        record("IRC", "OK" if " 464 " in data else "WRONG", "PASS <bad token> -> 464", "" if " 464 " in data else f"got: {data[:100]!r}")
        s.close()
    except OSError:
        pass


def check_ui():
    status, _, headers, html = http("GET", "/")
    if status != 200 or "html" not in {k.lower(): v for k, v in headers.items()}.get("content-type", ""):
        record("UI", "MISSING" if status in (0, 404) else "WRONG", "GET / serves the SPA", f"got {status}, want 200 text/html")
        return
    record("UI", "OK", "GET / serves the SPA")
    ids = set(re.findall(r"""data-testid\s*=\s*["']([^"']+)""", html))
    missing = [t for t in TESTIDS_INITIAL if t not in ids]
    record("UI", "OK" if not missing else "MISSING", f"pinned data-testid in initial HTML ({len(TESTIDS_INITIAL) - len(missing)}/{len(TESTIDS_INITIAL)})",
           "missing: " + " ".join(missing))
    dom = set(re.findall(r"""\bid\s*=\s*["']([^"']+)""", html))
    missing = [d for d in DOM_IDS if d not in dom]
    record("UI", "OK" if not missing else "MISSING", "pinned DOM ids", "missing: " + " ".join(missing))
    bad = []
    for role, names in BUTTON_ROLES.items():
        for n in names.split():
            m = re.search(rf"""<[^>]*data-testid\s*=\s*["']{n}["'][^>]*>""", html)
            if not m or not re.search(rf"""data-button-role\s*=\s*["']{role}["']""", m.group(0)):
                bad.append(f"{n}(want {role})")
    record("UI", "OK" if not bad else "MISSING", "data-button-role on pinned buttons", "missing/wrong: " + " ".join(bad))


def main():
    if os.environ.get("MSWEA_API_COVERAGE") != "1":
        print("api-coverage: command not available in this run")
        sys.exit(127)
    show_all = "--all" in sys.argv
    start = time.time()
    if check_cluster():
        ctx = check_api()
        if isinstance(ctx, tuple):
            token, slug, ch = ctx
            check_ws(token, ch)
            check_irc(token, slug)
        check_ui()
        live = check_rules() if "--routes" not in sys.argv else None
        if live:
            check_live(*live)
        if "--resilience" in sys.argv:
            check_resilience()
    else:
        skip("API", "all API, WebSocket, IRC and UI probes", "node :8000 is not answering /api/health")
    counts = defaultdict(int)
    areas: dict[str, list[int]] = {}
    for area, verdict, _, _ in results:
        counts[verdict] += 1
        areas.setdefault(area, [0, 0])
        areas[area][1] += 1
        areas[area][0] += verdict == "OK"
    order = ["DOWN", "ERROR", "MISSING", "WRONG", "SKIPPED"]
    print(f"api-coverage: {len(results)} probes | OK {counts['OK']} | " + " | ".join(f"{v} {counts[v]}" for v in order)
          + f"  ({time.time() - start:.1f}s)")
    nr = sum(r[2].startswith("rule: ") for r in results)
    okr = sum(r[2].startswith("rule: ") and r[1] == "OK" for r in results)
    print(f"routes: {counts['OK'] - okr}/{len(results) - nr} OK | behaviour rules: {okr}/{nr} OK")
    print("by area: " + ", ".join(f"{a} {ok}/{n}" for a, (ok, n) in areas.items()))
    for verdict in order + (["OK"] if show_all else []):
        for area, v, name, detail in results:
            if v == verdict:
                print(f"{v:<8} [{area}] {name}" + (f" -- {detail}" if detail else ""))
    if all(v == "OK" for _, v, _, _ in results):
        print("Every probe passed. The probes sample the contract (one request per route, one per key rule); "
              "they do not cover every rule, so keep testing the rest of instruction.md yourself"
              + ("" if "--resilience" in sys.argv else ", and run `api-coverage --resilience` once before submitting") + ".")
    else:
        print("Fix DOWN and ERROR first, then MISSING, then WRONG. `api-coverage --all` also lists passing probes.")


if __name__ == "__main__":
    main()
