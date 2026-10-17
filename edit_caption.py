#!/usr/bin/env python3
"""edit_caption.py — заменить caption у отправленного сообщения (Telethon).

Usage: python edit_caption.py <chat_id> <message_id> <caption_file|->
Output: JSON {"ok": true}
"""
import asyncio, json, sys
from send_album import keychain


async def main():
    if __import__('os').environ.get('TG_BOT_TOKEN'):  # Bot API вместо сессии (10.09.2026)
        import tg_bot
        return tg_bot.cli_edit_caption(sys.argv)
    if len(sys.argv) < 4:
        print(json.dumps({'ok': False, 'error': 'usage: edit_caption.py CHAT_ID MSG_ID FILE'}))
        return 1
    chat_id, msg_id, src = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    text = (sys.stdin.read() if src == '-' else open(src, encoding='utf-8').read())[:1024]

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
        await client.edit_message(entity, msg_id, text)
        print(json.dumps({'ok': True}))
        return 0
    finally:
        await client.disconnect()


if __name__ == '__main__':
    sys.exit(asyncio.run(main()) or 0)
