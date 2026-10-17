#!/usr/bin/env python3
"""watchdog.py — перекрёстный сторож мониторов (cyprus-monitor → cluj-monitor).

Каждый прогон одного монитора проверяет, когда сосед последний раз коммитил
цикл (state в data/). Если молчит дольше порога в рабочее окно — алерт в
Telegram (Daily wrap up). GitHub не уведомляет о НЕзапустившихся cron-слотах,
поэтому друг за другом следят сами мониторы.

Usage: watchdog.py <other_repo_name> <label>
Env: WATCHDOG_PAT (repo-scope, читать приватный соседний репо),
     TG_SESSION/TG_API_ID/TG_API_HASH (для send_text.py).

Правила (UTC): алерт только если сейчас 09:30–21:00 (к 09:30 у обоих должны
были пройти утренние слоты; ночью цикла нет — гэп до ~10ч это норма) и
последний коммит старше THRESHOLD_H. Повторный алерт при живом простое
уйдёт со следующим прогоном сторожа (~раз в 1-2ч) — это осознанно.
"""
import datetime as dt
import os, subprocess, sys

import requests

OWNER = 'srgartemov86'
THRESHOLD_H = 3.5
ALERT_CHAT = '135201285'  # Daily wrap up
WORK_START_UTC = dt.time(9, 30)
WORK_END_UTC = dt.time(21, 0)


def last_commit_age_h(repo, token):
    r = requests.get(
        f'https://api.github.com/repos/{OWNER}/{repo}/commits',
        params={'per_page': 1},
        headers={'Authorization': f'Bearer {token}',
                 'Accept': 'application/vnd.github+json'},
        timeout=20)
    r.raise_for_status()
    iso = r.json()[0]['commit']['committer']['date']
    ts = dt.datetime.fromisoformat(iso.replace('Z', '+00:00'))
    return (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 3600, iso


def main():
    if len(sys.argv) < 3:
        print('usage: watchdog.py <other_repo> <label>')
        return 0
    repo, label = sys.argv[1], sys.argv[2]
    token = os.environ.get('WATCHDOG_PAT', '')
    if not token:
        print('watchdog: no WATCHDOG_PAT — skip')
        return 0

    now = dt.datetime.now(dt.timezone.utc).time()
    if not (WORK_START_UTC <= now <= WORK_END_UTC):
        print(f'watchdog: outside work window ({now}) — skip')
        return 0

    try:
        age_h, iso = last_commit_age_h(repo, token)
    except Exception as e:
        print(f'watchdog: API error {e} — skip (не алертим на свои же сбои)')
        return 0

    print(f'watchdog: {repo} last commit {iso} ({age_h:.1f}h ago)')
    if age_h < THRESHOLD_H:
        return 0

    msg = (f'⚠️ {label}: монитор молчит {age_h:.1f} ч '
           f'(последний цикл-коммит {iso}).\n'
           f'GitHub, вероятно, дропает cron-слоты. Ручной запуск:\n'
           f'https://github.com/{OWNER}/{repo}/actions')
    r = subprocess.run([sys.executable, 'send_text.py', ALERT_CHAT, '-'],
                       input=msg, capture_output=True, text=True, timeout=60,
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    print(f'watchdog: alert sent rc={r.returncode}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
