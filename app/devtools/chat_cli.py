"""Chat with the agent in a terminal (development only).

Lives outside app/agent on purpose: it wires the adapters and performs sign-in through the
configured IdentityPort, which the agent itself is not allowed to do.

    python -m app.devtools.chat_cli
    python -m app.devtools.chat_cli --say "where is my order SS90004?" --say "/signin cus_9001"

Commands:
    /signin <token>         sign in through the IdentityPort (IDENTITY_PROVIDER=dev: the token is a customer id)
    /confirm | /decline     answer the pending confirmation card
    /image <path> <text>    send an image with a message
    /quit
"""

import argparse
import mimetypes
from pathlib import Path

from app.agent.orchestrator import AgentReply
from app.agent.preprocess import ImageInput
from app.auth.flow import sign_in_with_token
from app.config import get_settings
from app.db.base import get_sessionmaker, utcnow
from app.ports.identity import IdentityError
from app.wiring import build_agent, build_identity


def _show(r: AgentReply | None) -> str | None:
    if r is None:
        print("  (nothing to resume)")
        return None
    print(f"\nAgent [{r.outcome.value}]: {r.text}")
    if r.payload:
        if r.payload.get("quick_replies"):
            print("  options: " + " | ".join(q["label"] for q in r.payload["quick_replies"]))
        if r.payload.get("type") == "confirm":
            print("  → /confirm or /decline")
        if r.payload.get("type") == "sign_in":
            print("  → /signin <token>  (dev identity: a customer id, e.g. cus_9001 Maya, cus_9002 Daniel)")
    print(f"  turn {r.turn_id}")
    return r.payload.get("action_id") if r.payload else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--say", action="append", default=[], help="send these lines non-interactively")
    args = parser.parse_args()

    settings = get_settings()
    db = get_sessionmaker()()
    identity = build_identity(settings)
    agent = build_agent(db, settings, identity=identity)
    session = agent.start_session("cli")
    print(f"session {session.id}")
    action_id = None
    lines = iter(args.say) if args.say else None

    while True:
        try:
            line = next(lines) if lines else input("\nyou: ")
        except (StopIteration, EOFError):
            break
        if lines:
            print(f"\nyou: {line}")
        line = line.strip()
        if not line:
            continue
        if line == "/quit":
            break
        if line.startswith("/signin "):
            try:
                sign_in_with_token(db, identity, session.id, line.split(maxsplit=1)[1], now=utcnow(),
                                   max_session_minutes=settings.identity_max_session_minutes)
            except IdentityError as exc:
                db.commit()
                print(f"  sign-in failed: {exc.code}")
                continue
            db.commit()
            action_id = _show(agent.resume_after_auth(session.id)) or action_id
        elif line in ("/confirm", "/decline"):
            action_id = _show(agent.confirm_action(session.id, action_id or "", accept=line == "/confirm"))
        elif line.startswith("/image "):
            _, path, *text = line.split(" ")
            data = Path(path).read_bytes()
            media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
            action_id = _show(agent.handle_message(session.id, " ".join(text), [ImageInput(media_type, data)]))
        else:
            action_id = _show(agent.handle_message(session.id, line)) or action_id


if __name__ == "__main__":
    main()
