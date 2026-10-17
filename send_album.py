#!/usr/bin/env python3
"""send_album.py — отправка альбома (до 10 фото одним сообщением) в Telegram.

Телеграм-MCP умеет только send_file (1 фото) — альбомы шлём напрямую через
Telethon той же сессией из Keychain (как launcher telegram-mcp-sse).
Короткоживущий клиент; практика list_topics-fix показала, что параллельное
короткое подключение к той же session string безопасно.

Usage (ВАЖНО: telethon есть только в venv311, НЕ в системном python3):
    /Users/dodo/telegram-mcp-venv311/bin/python send_album.py <chat_id> <caption_file> <photo1> [... photo10]
    caption_file: путь к файлу с caption (UTF-8) или '-' для stdin.

Output (stdout): JSON {"ok": true, "message_ids": [...], "first_message_id": N}
first_message_id — для --mark-sent и reply_to (caption висит на первом фото).
"""
import asyncio, json, os, subprocess, sys


def keychain(account):
    # Сервер: секреты в env (TG_SESSION/TG_API_ID/TG_API_HASH), Keychain — только macOS
    env_map = {'session_string': 'TG_SESSION', 'api_id': 'TG_API_ID', 'api_hash': 'TG_API_HASH'}
    v = os.environ.get(env_map.get(account, ''), '')
    if v:
        return v
    r = subprocess.run(['security', 'find-generic-password', '-a', account,
                        '-s', 'telegram-mcp', '-w'], capture_output=True, text=True)
    v = r.stdout.strip()
    if not v:
        raise RuntimeError(f'no {account} in env or keychain')
    return v


async def main():
    if __import__('os').environ.get('TG_BOT_TOKEN'):  # Bot API вместо сессии (10.09.2026)
        import tg_bot
        return tg_bot.cli_send_album(sys.argv)
    if len(sys.argv) < 4:
        print(json.dumps({'ok': False, 'error': 'usage: send_album.py CHAT_ID CAPTION_FILE PHOTO...'}))
        return 1
    chat_id = int(sys.argv[1])
    cap_src = sys.argv[2]
    photos = sys.argv[3:13]  # max 10 (лимит альбома Telegram)

    caption = (sys.stdin.read() if cap_src == '-'
               else open(cap_src, encoding='utf-8').read())
    caption = caption[:1024]

    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import PeerChat, PeerChannel

    client = TelegramClient(StringSession(keychain('session_string')),
                            int(keychain('api_id')), keychain('api_hash'))
    await client.start()
    try:
        # StringSession без entity-кэша: резолвим peer перебором типов
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
        msgs = await client.send_file(entity, photos, caption=caption)
        if not isinstance(msgs, list):
            msgs = [msgs]
        ids = [m.id for m in msgs]
        print(json.dumps({'ok': True, 'message_ids': ids,
                          'first_message_id': ids[0] if ids else None}))
        return 0
    finally:
        await client.disconnect()


if __name__ == '__main__':
    sys.exit(asyncio.run(main()) or 0)
