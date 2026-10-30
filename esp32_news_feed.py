#!/usr/bin/env python3
"""Serve fresh Chinese technology, world, finance and health RSS headlines to ESP32."""
import argparse
import concurrent.futures
import datetime as dt
import email.utils
import gzip
import html
import io
import json
import re
import threading
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from opencc import OpenCC

INTERVAL_SECONDS = 600
UTC = dt.timezone.utc
LOCAL = dt.timezone(dt.timedelta(hours=8))
CATEGORIES = ('科技', '全球', '金融', '医疗')
SOURCES = (
    {'key': 'BBC中文', 'source': 'BBC', 'url': 'https://feeds.bbci.co.uk/zhongwen/simp/rss.xml'},
    {'key': 'RFI中文', 'source': 'RFI', 'url': 'https://www.rfi.fr/cn/rss'},
    {'key': 'DW中文', 'source': 'DW', 'url': 'https://rss.dw.com/rdf/rss-chi-all'},
    {'key': '中央社国际', 'source': '中央社', 'url': 'https://feeds.feedburner.com/rsscna/intworld'},
    {'key': '中央社财经', 'source': '中央社', 'category': '金融', 'url': 'https://feeds.feedburner.com/rsscna/finance'},
    {'key': '中央社科技', 'source': '中央社', 'category': '科技', 'url': 'https://feeds.feedburner.com/rsscna/technology'},
    {'key': '中央社健康', 'source': '中央社', 'category': '医疗', 'only_health': True, 'url': 'https://feeds.feedburner.com/rsscna/lifehealth'},
    {'key': '联合国健康', 'source': '联合国', 'category': '医疗', 'url': 'https://news.un.org/feed/subscribe/zh/news/topic/health/feed/rss.xml'},
    {'key': 'Bing中文国际', 'source': 'Bing', 'bing': True, 'url': 'https://www.bing.com/news/search?' + urllib.parse.urlencode({'q': 'site:bbc.com/zhongwen', 'format': 'RSS', 'setlang': 'zh-hans', 'cc': 'us'})},
)
AI = re.compile(r'\bAI\b|人工智[能慧]|大模型|智能体|机器人|算力|芯片|英伟达|OpenAI|Claude|Gemini|DeepSeek|GPT|Anthropic|半导体|量子|航天|计算机', re.I)
HEALTH = re.compile(r'医疗|医学|健康|医院|疾病|病毒|疫苗|药物|癌症|失眠|锻炼|运动量|活动量|世卫|卫生组织|埃博拉|减肥|糖尿病')
FINANCE = re.compile(r'金融|财经|经济|股市|股价|股指|利率|央行|通胀|通膨|美联储|汇率|债券|投资|贸易|关税|财报|营收|华尔街')
BREAKING = re.compile(r'突发|地震|海啸|爆炸|袭击|坠机|紧急|停火|开战|撤离|遇难|死亡')
cache = {'items': [], 'updated_at': None, 'error': None, 'sources': {},
         'refresh_interval_seconds': INTERVAL_SECONDS}
source_cache = {}
lock = threading.Lock()


def published_time(value):
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        try:
            parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
        except (ValueError, TypeError, AttributeError):
            return None


def is_today(item, now=None):
    now = now or dt.datetime.now(UTC)
    published = dt.datetime.fromisoformat(item['published_at'])
    return published.astimezone(LOCAL).date() == now.astimezone(LOCAL).date() and published <= now


def fetch_source(spec):
    request = urllib.request.Request(spec['url'], headers={
        'User-Agent': 'Mozilla/5.0 ESP32-News-Reader/3.0', 'Accept-Encoding': 'gzip'})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(1000001)
    if len(raw) > 1000000:
        raise ValueError('RSS response exceeds limit')
    if raw[:2] == b'\x1f\x8b':
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
            raw = compressed.read(1000001)
        if len(raw) > 1000000:
            raise ValueError('Decompressed RSS exceeds limit')
    if spec.get('bing'):
        # Bing can prepend executable HTML to its RSS response. Only parse XML.
        start, end = raw.find(b'<rss'), raw.rfind(b'</rss>')
        if start < 0 or end < start:
            raise ValueError('Bing returned no RSS (redirect or challenge)')
        raw = raw[start:end+6]
    root = ET.fromstring(raw)
    converter = OpenCC('t2s')
    now = dt.datetime.now(UTC)
    entries, seen = [], set()
    for item in root.iter():
        if item.tag.rsplit('}', 1)[-1] != 'item':
            continue
        fields = {x.tag.rsplit('}', 1)[-1].lower(): x.text or '' for x in item}
        title = converter.convert(re.sub(r'\s+', ' ', html.unescape(fields.get('title', '')))).strip()
        published = published_time(fields.get('pubdate') or fields.get('date', ''))
        link, publisher = fields.get('link', ''), spec['source']
        via = spec['source']
        if spec.get('bing'):
            link = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get('url', [link])[0]
            host = (urllib.parse.urlsplit(link).hostname or '').lower()
            publisher = next((name for domain, name in [('bbc.com', 'BBC'), ('bbc.co.uk', 'BBC'), ('dw.com', 'DW'), ('rfi.fr', 'RFI'), ('cna.com.tw', '中央社'), ('who.int', '世卫组织'), ('news.un.org', '联合国')]
                              if host == domain or host.endswith('.'+domain)), None)
            if not publisher:
                continue
            via = 'Bing News'
        # Require a Chinese title; exclude adverts, contributed releases and opinion slots.
        if len(re.findall(r'[\u4e00-\u9fff]', title)) < 2 or re.search(r'[\u3040-\u30ff]', title):
            continue
        decoded_link = urllib.parse.unquote(link).lower()
        if any(x in decoded_link for x in ['/postwrite/', '/sponsored/', '/opinion/', '/press-release', '/专栏检索/']):
            continue
        if re.search(r'社论|评论：|广告|工商讯息|新聞稿|新闻稿|法国报纸摘要|要闻解说|要闻分析', title):
            continue
        if spec.get('only_health') and not HEALTH.search(title):
            continue
        if not title or title in seen or published is None:
            continue
        if not is_today({'published_at': published.isoformat()}, now):
            continue
        seen.add(title)
        category = spec.get('category') or ('医疗' if HEALTH.search(title) else '科技' if AI.search(title) else '金融' if FINANCE.search(title) else '全球')
        entries.append({'title': title, 'link': link,
                        'published_at': published.isoformat(), 'source': publisher,
                        'via': via, 'category': category, 'cached': False})
    entries.sort(key=lambda x: x['published_at'], reverse=True)
    return entries


def select_items(groups):
    chosen, ranked, seen, publishers = [], {}, set(), {}
    now = dt.datetime.now(UTC)
    for category in CATEGORIES:
        entries = [x for x in groups.get(category, []) if is_today(x, now)]
        entries.sort(key=lambda x: x['published_at'], reverse=True)
        if category == '科技':
            entries = [x for x in entries if AI.search(x['title'])] + [x for x in entries if not AI.search(x['title'])]
        elif category == '全球':
            # Prefer developing stories, always from the current Beijing date.
            def developing(x):
                return bool(BREAKING.search(x['title'])) and (now - dt.datetime.fromisoformat(x['published_at'])).total_seconds() < 86400
            entries = [x for x in entries if developing(x)] + [x for x in entries if not developing(x)]
        ranked[category] = entries
        # Reserve space for all categories before distributing spare slots.
        for item in entries[:2]:
            if item['title'] not in seen and publishers.get(item['source'], 0) < 4:
                chosen.append(item)
                seen.add(item['title'])
                publishers[item['source']] = publishers.get(item['source'], 0) + 1
    while len(chosen) < 10:
        added = False
        for entries in ranked.values():
            item = next((x for x in entries if x['title'] not in seen and publishers.get(x['source'], 0) < 4), None)
            if item is not None:
                chosen.append(item)
                seen.add(item['title'])
                publishers[item['source']] = publishers.get(item['source'], 0) + 1
                added = True
            if len(chosen) == 10:
                break
        if not added:
            break
    return chosen


def refresh():
    now = dt.datetime.now(UTC)
    groups, states = {x: [] for x in CATEGORIES}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_source, spec): spec for spec in SOURCES}
        for future in concurrent.futures.as_completed(futures):
            spec = futures[future]
            key = spec['key']
            try:
                items = future.result()
                source_cache[key] = {'items': items, 'last_success': now.isoformat()}
                states[key] = {'source': spec['source'], 'url': spec['url'],
                                    'last_success': now.isoformat(), 'error': None, 'today_count': len(items)}
            except Exception as exc:
                previous = source_cache.get(key, {})
                # One publisher failing does not stop the other categories.
                items = [dict(x, cached=True) for x in previous.get('items', [])
                         if is_today(x, now)]
                states[key] = {'source': spec['source'], 'url': spec['url'],
                                    'last_success': previous.get('last_success'), 'error': str(exc), 'today_count': len(items)}
            for item in items:
                groups[item['category']].append(item)
    items = select_items(groups)
    with lock:
        cache.update(items=items, sources=states, updated_at=now.isoformat(),
                     pool=[item for entries in groups.values() for item in entries],
                     error=None if any(v['error'] is None for v in states.values()) else 'All sources unavailable')


def headline_line(item):
    stamp = dt.datetime.fromisoformat(item['published_at']).astimezone(LOCAL).strftime('%m-%d %H:%M')
    prefix = ('缓存 ' if item['cached'] else '') + f"[{item['category']}/{item['source']} {stamp}] "
    text = prefix + item['title']
    if len(text.encode('utf-8')) > 240:
        text = text.encode('utf-8')[:237].decode('utf-8', 'ignore') + '...'
    return text


def update_loop():
    while True:
        refresh()
        time.sleep(INTERVAL_SECONDS)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        request_url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(request_url.query)
        with lock:
            state = dict(cache)
        pool = state.pop('pool', state['items'])
        try:
            limit = int(query.get('limit', ['10'])[0])
            categories = int(query.get('categories', ['15'])[0])
            sources = query.get('sources', ['international'])[0]
            if not 1 <= limit <= 10 or not 1 <= categories <= 15 or sources not in ('international', 'bing'):
                raise ValueError('Invalid news selection')
        except ValueError:
            self.send_error(400, 'Invalid news selection')
            return
        groups = {category: [] for category in CATEGORIES}
        for item in pool:
            if categories & (1 << CATEGORIES.index(item['category'])) and (sources != 'bing' or item.get('via') == 'Bing News'):
                groups[item['category']].append(item)
        state['items'] = select_items(groups)[:limit]
        # Filter again at request time, including requests straddling midnight.
        state['items'] = [x for x in state['items'] if is_today(x)]
        state['news_date'] = dt.datetime.now(LOCAL).date().isoformat()
        state['max_items'] = 10
        if request_url.path == '/headlines':
            if state['error']:
                self.send_error(503, 'No current headlines available')
                return
            body = ('\n'.join(headline_line(x) for x in state['items']) + ('\n' if state['items'] else '')).encode('utf-8')
            mime = 'text/plain; charset=utf-8'
        elif request_url.path == '/status':
            body = json.dumps(state, ensure_ascii=False).encode('utf-8')
            mime = 'application/json; charset=utf-8'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Poll-Interval-Seconds', str(INTERVAL_SECONDS))
        self.send_header('X-News-Date', state['news_date'])
        self.end_headers()
        self.wfile.write(body)


def main():
    global INTERVAL_SECONDS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='Fetch and print actual feed data, then exit')
    parser.add_argument('--bind', default='127.0.0.1', help='Use the Mac LAN IP to expose to the device')
    parser.add_argument('--port', default=8766, type=int)
    parser.add_argument('--interval', type=int, choices=(600, 1800), default=600,
                        help='Refresh seconds: 600 (10 min) or 1800 (30 min)')
    args = parser.parse_args()
    INTERVAL_SECONDS = args.interval
    cache['refresh_interval_seconds'] = INTERVAL_SECONDS
    if args.once:
        refresh()
        print(json.dumps(cache, ensure_ascii=False, indent=2))
        if cache['error']:
            raise SystemExit(1)
        return
    threading.Thread(target=update_loop, daemon=True).start()
    print(f'News text endpoint: http://{args.bind}:{args.port}/headlines; refresh={INTERVAL_SECONDS}s', flush=True)
    ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
