#!/usr/bin/env python3
"""Build bounded static news data for an ESP32; intended for GitHub Actions."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import esp32_news_feed as feed


def build(output, cache_path):
    if cache_path.exists():
        try:
            old = json.loads(cache_path.read_text())
            for spec in feed.SOURCES:
                entry = old.get(spec['key'], {})
                feed.source_cache[spec['key']] = {'items': [x for x in entry.get('items', []) if feed.is_today(x)], 'last_success': entry.get('last_success')}
        except (ValueError, KeyError, TypeError):
            pass
    feed.refresh()
    state = dict(feed.cache)
    # Publish a dated empty batch when every source has expired, so devices
    # can clear old headlines instead of keeping the last successful deployment.
    pool = state.pop('pool', state['items'])
    today = dt.datetime.now(feed.LOCAL).date().isoformat()
    ranked = {}
    for category in feed.CATEGORIES:
        candidates = [x for x in pool if x['category'] == category and feed.is_today(x)]
        ranked[category] = feed.select_items({category: candidates})
    selected, seen = [], set()
    # Fresh sources first, then bounded outage fallback; retain category balance.
    for cached in (False, True):
        phase = {category: [x for x in ranked[category] if bool(x.get('cached')) == cached]
                 for category in feed.CATEGORIES}
        for i in range(10):
            for category in feed.CATEGORIES:
                entries = phase[category]
                if i < len(entries):
                    item = entries[i]
                    key = (item['title'], item.get('via'))
                    if key not in seen:
                        selected.append(item)
                        seen.add(key)
    # Preserve Bing alternatives for devices configured to that source mode.
    alternatives = sorted(pool, key=lambda x: (bool(x.get('cached')), -dt.datetime.fromisoformat(x['published_at']).timestamp()))
    for item in alternatives:
        key = (item['title'], item.get('via'))
        if item.get('via') == 'Bing News' and feed.is_today(item) and key not in seen and len(selected) < 40:
            selected.append(item)
            seen.add(key)
    compact = []
    details = []
    for item in selected[:40]:
        compact.append({'t': feed.headline_line(item), 'c': 1 << feed.CATEGORIES.index(item['category']),
                        'b': int(item.get('via') == 'Bing News'), 'p': item['source'],
                        'id': hashlib.sha256(item['title'].encode()).hexdigest()[:12]})
        details.append(item)
    document = {'date': today, 'updated_at': state['updated_at'], 'policy': 'fresh-first-v2', 'items': compact}
    raw = json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode()
    if len(raw) > 16384:
        raise ValueError('Device JSON exceeds 16 KiB')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'news.json').write_bytes(raw)
    (output / 'details.json').write_text(json.dumps({'date': today, 'updated_at': state['updated_at'], 'sources': state['sources'], 'items': details}, ensure_ascii=False, indent=2))
    (output / '.nojekyll').write_text('')
    (output / 'index.html').write_text('''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>ESP32 中文国际新闻</title><style>body{max-width:800px;margin:40px auto;padding:20px;background:#10141c;color:#e8edf5;font:16px system-ui}a{color:#ceb8ff}li{margin:22px 0;line-height:1.6}small{color:#a8b5cb}</style><h1>ESP32 中文国际新闻</h1><p id="date"></p><p>由 GitHub Actions 定时获取，原始媒体署名与链接保留。仅收北京时间当天的中文标题。</p><ul id="news"></ul><script>fetch('details.json').then(r=>r.json()).then(s=>{document.querySelector('#date').textContent='新闻日期 '+s.date+' · 数据生成 '+s.updated_at;for(const n of s.items){const li=document.createElement('li'),a=document.createElement('a'),m=document.createElement('small');a.href=n.link;a.textContent=n.title;a.rel='noreferrer';m.textContent=' '+n.source+' · '+n.category+' · '+n.published_at;li.append(a,document.createElement('br'),m);document.querySelector('#news').append(li)}})</script>''')
    # Persist only when public article content changes; this also records useful repository activity.
    snapshot = {key: {'items': value['items'], 'last_success': value.get('last_success')} for key, value in feed.source_cache.items()}
    def content_only(data):
        return {key: value.get('items', []) for key, value in data.items()}
    previous = {}
    if cache_path.exists():
        try: previous = json.loads(cache_path.read_text())
        except ValueError: pass
    if content_only(previous) != content_only(snapshot):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    print(f'Built {len(compact)} selectable Chinese headlines, {len(raw)} bytes, day={today}')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, default=Path('public'))
    p.add_argument('--cache', type=Path, default=Path('data/source_cache.json'))
    args = p.parse_args()
    build(args.output, args.cache)
