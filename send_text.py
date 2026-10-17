#!/usr/bin/env python3
"""send_text.py — текстовое сообщение в Telegram (Telethon, та же сессия что send_album).

Usage: python send_text.py <chat_id> <text_file|-> [--reply-to MSG_ID]
Output: JSON {"ok": true, "message_id": N}
"""
import asyncio, json, sys
from send_album import keychain


async def main():
    if __import__('os').environ.get('TG_BOT_TOKEN'):  # Bot API вместо сессии (10.09.2026)
        import tg_bot
        return tg_bot.cli_send_text(sys.argv)
    if len(sys.argv) < 3:
        print(json.dumps({'ok': False, 'error': 'usage: send_text.py CHAT_ID TEXT_FILE [--reply-to N]'}))
        return 1
    chat_id = int(sys.argv[1])
    src = sys.argv[2]
    reply_to = None
    if '--reply-to' in sys.argv:
        reply_to = int(sys.argv[sys.argv.index('--reply-to') + 1])
    parse_mode = 'html' if '--html' in sys.argv else None
    text = (sys.stdin.read() if src == '-' else open(src, encoding='utf-8').read())[:4000]

    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import PeerChat, PeerChannel

    client = TelegramClient(StringSession(keychain('session_string')),
                            int(keychain('api_id')), keychain('api_hash'))
    await client.start()
    try:
        entity = None
        for peer in (PeerChat(chat_id), PeerChannel(chat_id), chat_id):
            try:
                entity = await client.get_entity(peer)
                break
            except Exception:
                continue
        if entity is None:
            print(json.dumps({'ok': False, 'error': f'cannot resolve chat {chat_id}'}))
            return 1
        msg = await client.send_message(entity, text, reply_to=reply_to,
                                        parse_mode=parse_mode, link_preview=False)
        print(json.dumps({'ok': True, 'message_id': msg.id}))
        return 0
    finally:
        await client.disconnect()


if __name__ == '__main__':
    sys.exit(asyncio.run(main()) or 0)
