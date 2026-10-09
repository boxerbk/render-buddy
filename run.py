"""Render Buddy: watch renders on your Phaze machines and get a Slack message when one goes wrong.

Run it on a Windows PC that has the Phaze app open (signed in, MCP enabled), then open the page:

    python run.py              # web page + watching + Slack (if configured in .env)
    python run.py --no-slack   # print notifications to this console instead of Slack
"""
import argparse
import asyncio
import webbrowser

import uvicorn

from config import Config
from phaze_mcp import PhazeMCP
from server import create_app
from store import Store
from watcher import Hub, Manager


async def main_async(a):
    cfg = Config.load()
    port = a.port or cfg.port
    mcp = PhazeMCP(cfg.phaze_mcp_url)
    vision = None
    if cfg.vision_enabled:
        from vision import Vision
        vision = Vision(cfg.anthropic_api_key, cfg.model)

    manager = Manager(mcp, vision, Store(cfg.data_dir), Hub(), cfg)

    handler = None
    if cfg.slack_enabled and not a.no_slack:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        from slack_bot import SlackNotifier, build_slack_app
        slack_app = build_slack_app(manager, cfg.slack_bot_token)
        manager.notifier = SlackNotifier(slack_app.client, cfg.slack_channel)
        handler = AsyncSocketModeHandler(slack_app, cfg.slack_app_token)
        await handler.connect_async()

    print(f"[run]    Claude vision: {cfg.model if vision else 'off (no ANTHROPIC_API_KEY): pixel-diff only'}")
    print(f"[run]    Slack: {'on, posting to ' + cfg.slack_channel if handler else 'off: notifications print here'}")
    await manager.resume()

    server = uvicorn.Server(uvicorn.Config(create_app(manager, cfg), host="127.0.0.1", port=port, log_level="warning"))
    url = f"http://127.0.0.1:{port}"
    print(f"[run]    open {url}")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        await server.serve()
    finally:
        await manager.shutdown()
        if handler:
            await handler.close_async()
        await mcp.aclose()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--no-slack", action="store_true", help="print notifications instead of posting to Slack")
    p.add_argument("--no-browser", action="store_true", help="don't open the web page automatically")
    p.add_argument("--port", type=int, help="web page port (default 8765, or PORT in .env)")
    try:
        asyncio.run(main_async(p.parse_args()))
    except KeyboardInterrupt:
        pass
    print("\n[run]    stopped. Active watches resume next time you start Render Buddy.")


if __name__ == "__main__":
    main()
