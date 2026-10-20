"""Authentication seam.

There is no auth yet, but every API router depends on `current_user`, the
frontend sends every request through one `api()` wrapper that already handles
401s, and jobs carry a `requested_by` column. Adding auth later means:

1. add a `users` table migration in db.py (username, password_hash, is_admin);
2. implement a mode below (e.g. "local" with a signed session cookie, or
   "proxy" trusting a Remote-User header from Authelia/Authentik/Twingate);
3. add a /api/auth/login route and a login view in web/app.js.

Nothing else in the app needs to change.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request


@dataclass(frozen=True)
class User:
    id: str
    username: str
    is_admin: bool = True


ANONYMOUS = User(id="local", username="local", is_admin=True)


async def current_user(request: Request) -> User:
    mode = request.app.state.settings.get("auth.mode")
    if mode == "none":
        return ANONYMOUS
    # Future modes go here. Fail closed for anything unrecognised.
    raise HTTPException(status_code=401, detail="Not authenticated")


async def require_admin(request: Request) -> User:
    user = await current_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return user
