#!/usr/bin/env python3
"""dispatch_sibling.py — добор пропущенных cron-слотов соседнего монитора.

GitHub дропает часть schedule-слотов (реально ~3 запуска из 12 в сутки). Для
cluj/belgrade добор делает Apps Script github-monitors-watchdog, но его список
REPOS правится только руками в script.google.com. Для cyprus-monitor (17.10.2026)
добор делает этот шаг из прогона cluj-monitor: если у соседа нет прогона свежее
FRESH_MIN минут в рабочее окно — workflow_dispatch.

Usage: dispatch_sibling.py <repo> [workflow_file]
Env: WATCHDOG_PAT (repo+workflow scope).
"""
import datetime as dt
import os, sys

import requests

OWNER = 'srgartemov86'
FRESH_MIN = 50
WORK_START_UTC, WORK_END_UTC = dt.time(5, 0), dt.time(17, 0)


def main():
    if len(sys.argv) < 2:
        print('usage: dispatch_sibling.py <repo> [workflow_file]')
        return 0
    repo = sys.argv[1]
    wf = sys.argv[2] if len(sys.argv) > 2 else 'monitor.yml'
    token = os.environ.get('WATCHDOG_PAT', '')
    if not token:
        print('dispatch: no WATCHDOG_PAT — skip')
        return 0
    now = dt.datetime.now(dt.timezone.utc)
    if not (WORK_START_UTC <= now.time() <= WORK_END_UTC):
        print(f'dispatch: outside work window ({now.time()}) — skip')
        return 0
    h = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}
    try:
        r = requests.get(f'https://api.github.com/repos/{OWNER}/{repo}/actions/runs',
                         params={'per_page': 1}, headers=h, timeout=20)
        r.raise_for_status()
        runs = r.json().get('workflow_runs') or []
    except Exception as e:
        print(f'dispatch: API error {e} — skip')
        return 0
    if runs:
        created = dt.datetime.fromisoformat(runs[0]['created_at'].replace('Z', '+00:00'))
        age_min = (now - created).total_seconds() / 60
        print(f'dispatch: {repo} last run {runs[0]["created_at"]} ({age_min:.0f} min ago, '
              f'{runs[0].get("status")})')
        if age_min < FRESH_MIN or runs[0].get('status') in ('queued', 'in_progress'):
            return 0
    try:
        r = requests.post(f'https://api.github.com/repos/{OWNER}/{repo}/actions/workflows/{wf}/dispatches',
                          json={'ref': 'main'}, headers=h, timeout=20)
        print(f'dispatch: {repo} workflow_dispatch → {r.status_code}')
    except Exception as e:
        print(f'dispatch: POST error {e}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
