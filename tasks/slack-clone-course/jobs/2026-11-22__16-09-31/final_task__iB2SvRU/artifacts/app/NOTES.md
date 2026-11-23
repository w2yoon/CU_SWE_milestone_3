Plan:
- Self-contained Python stdlib chat service with shared JSON persistence.
- Cover auth, workspaces/channels, messages/threads, reactions/pins, DMs/files/search, groups/invitations/roles/read state.
- SPA with pinned selectors, WebSocket delivery, and IRC gateway.
Verified:
- server.py, start.sh, requirements.txt exist and compile.
- HTTP health returns 200 with node ids 0/1/2; start.sh serves all nodes and Redis 6379.
- Registration/login/me validation, workspaces/channels, cross-node reads/writes, reactions, pins, replies, edit/delete, search, read-state, DMs.
- Archived channel writes return 423; /unarchive works.
- Multipart file upload, metadata, and byte-identical download pass.
- WebSocket handshake/subscribe, cross-node delivery, and resume replay pass.
- IRC PASS/NICK/USER registration, PING, JOIN, and PRIVMSG bridge pass.
- SPA auth/channel/message/thread/reaction controls, edit/delete controls, and required selectors are served.
- Pagination valid pages, opaque continuation, invalid cursor/limit pass.
- Invitation creation/list, targeted acceptance, expiry, single-use consumption, and membership pass.
- Groups create/update/delete, ownership transfer, and role listing pass.
- Killing one HTTP node and Redis recovers within four seconds while health remains available.
Known limitations:
- Full browser automation and every advanced permission edge case were not exhaustively exercised.
Facts:
- Shared state: /app/data/state.json.
- start.sh supervises ports 8000/8001/8002, Redis 6379; IRC is 6667.
- Final permission regression verified: invalid timezone returns 400; invite-only non-members get 403 on join; guests/non-members cannot create private channels or post; all nodes remain healthy.
- Chromium browser flow completed end-to-end: fresh sign-up auto-login, visible workspace creation, #general rendering, message post, thread panel/reply validation, emoji picker/reaction badge, edit with edited text, delete, reload persistence, empty-message validation, logout, and login restoration.
- Removed duplicate hidden workspace-create selector that caused strict browser selector failures; inline SPA JavaScript syntax-checked with Node.
- Final service state has all HTTP nodes, IRC, and Redis active.

- Final matrix tested mention input, read-state monotonicity, validation errors, IRC registration/JOIN/PING, and current service health.
