import datetime as dt
import email.utils
import importlib.util
import io
import unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('feed',Path(__file__).with_name('esp32_news_feed.py'))
feed=importlib.util.module_from_spec(spec);spec.loader.exec_module(feed)

class FeedTests(unittest.TestCase):
    def setUp(self):
        feed.source_cache.clear()
        self.now=dt.datetime.now(feed.UTC)
        self.date=email.utils.format_datetime(self.now)
    def entry(self,title='人工智能今日新闻',source='BBC',category='科技',published=None):
        return {'title':title,'source':source,'category':category,'published_at':(published or self.now).isoformat(),'cached':False,'link':'https://www.bbc.com/zhongwen/a','via':source}
    def fetch_xml(self,raw,bing=False):
        cfg={'key':'test','source':'BBC','url':'https://example.invalid/feed','bing':bing}
        with patch.object(feed.urllib.request,'urlopen',return_value=io.BytesIO(raw.encode())):
            return feed.fetch_source(cfg)
    def test_beijing_midnight(self):
        now=dt.datetime(2026,10,30,0,15,tzinfo=feed.LOCAL)
        self.assertTrue(feed.is_today({'published_at':'2026-10-29T16:00:00+00:00'},now))
        self.assertFalse(feed.is_today({'published_at':'2026-10-29T15:59:59+00:00'},now))
        self.assertFalse(feed.is_today({'published_at':'2026-10-29T16:16:00+00:00'},now))
    def test_rdf_iso_date(self):
        raw=f'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"><item><title>人工智慧新技術</title><link>https://www.bbc.com/zhongwen/a</link><dc:date>{self.now.isoformat()}</dc:date></item></rdf:RDF>'
        items=self.fetch_xml(raw)
        self.assertEqual(items[0]['title'],'人工智慧新技术')
    def test_bing_wrapper_and_publisher(self):
        raw=f'<script>never execute this</script><rss><channel><item><title>全球今日新闻</title><link>https://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fwww.bbc.com%2Fzhongwen%2Fa</link><pubDate>{self.date}</pubDate></item></channel></rss><script>more html</script>'
        item=self.fetch_xml(raw,True)[0]
        self.assertEqual(item['source'],'BBC')
        self.assertEqual(item['via'],'Bing News')
        self.assertEqual(item['link'],'https://www.bbc.com/zhongwen/a')
    def test_bing_rejects_spoofed_domain(self):
        raw=f'<rss><channel><item><title>新闻标题</title><link>https://bbc.com.example.invalid/a</link><pubDate>{self.date}</pubDate></item></channel></rss>'
        self.assertEqual(self.fetch_xml(raw,True),[])
    def test_rejects_advertising_and_english(self):
        raw=f'<rss><channel><item><title>English headline</title><pubDate>{self.date}</pubDate></item><item><title>人工智能新闻</title><link>https://www.cna.com.tw/postwrite/1</link><pubDate>{self.date}</pubDate></item></channel></rss>'
        self.assertEqual(self.fetch_xml(raw),[])
    def test_ten_unique_and_publisher_diversity(self):
        groups={cat:[] for cat in feed.CATEGORIES}
        for i in range(30):
            cat=feed.CATEGORIES[i%4]
            groups[cat].append(self.entry(title=f'新闻{i}',source=['BBC','RFI','中央社'][i%3],category=cat))
        items=feed.select_items(groups)
        self.assertEqual(len(items),10)
        self.assertEqual(len({x['title'] for x in items}),10)
        for name in ('BBC','RFI','中央社'):self.assertLessEqual(sum(x['source']==name for x in items),4)
    def test_outage_cannot_retain_yesterday(self):
        midnight=self.now.astimezone(feed.LOCAL).replace(hour=0,minute=0,second=0,microsecond=0)
        old=self.entry('昨天的旧闻',published=midnight-dt.timedelta(seconds=1))
        feed.source_cache['BBC中文']={'items':[self.entry(),old],'last_success':self.now.isoformat()}
        def fetch(cfg):
            if cfg['key']=='BBC中文':raise OSError('simulated outage')
            return []
        with patch.object(feed,'fetch_source',side_effect=fetch):feed.refresh()
        self.assertEqual(len(feed.cache['items']),1)
        self.assertTrue(feed.cache['items'][0]['cached'])
    def test_new_headline_precedes_old_keyword_boost(self):
        old=self.now-dt.timedelta(minutes=1)
        for category, title in [('科技','人工智能旧新闻'), ('全球','突发地震旧新闻')]:
            entries=[self.entry(title,category=category,published=old),self.entry('最新普通新闻',category=category)]
            self.assertEqual(feed.select_items({category:entries})[0]['title'],'最新普通新闻')
    def test_fresh_precedes_cached(self):
        fallback=dict(self.entry('缓存标题'),cached=True)
        fresh=self.entry('刚抓取标题',published=self.now-dt.timedelta(seconds=1))
        self.assertEqual(feed.select_items({'科技':[fallback,fresh]})[0]['title'],'刚抓取标题')
    def test_expired_outage_cache_is_pruned(self):
        feed.source_cache['BBC中文']={'items':[self.entry()], 'last_success':(self.now-dt.timedelta(seconds=3601)).isoformat()}
        with patch.object(feed,'fetch_source',side_effect=OSError('outage')):
            feed.refresh()
        self.assertEqual(feed.cache['items'],[])
        self.assertEqual(feed.source_cache['BBC中文']['items'],[])
    def test_expired_outage_publishes_empty_batch(self):
        import tempfile
        import publish_news
        # publish_news imports the module by its normal name.
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(publish_news.feed,'fetch_source',side_effect=OSError('outage')):
                publish_news.build(root/'public',root/'cache.json')
            import json
            document=json.loads((root/'public/news.json').read_text())
            self.assertEqual(document['items'],[])
            self.assertEqual(document['date'],self.now.astimezone(feed.LOCAL).date().isoformat())
    def test_utf8_truncation(self):
        line=feed.headline_line(self.entry('人工智能'*200))
        self.assertLessEqual(len(line.encode()),240)
        self.assertTrue(line.endswith('...'))

if __name__=='__main__':unittest.main()
