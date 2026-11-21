import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bootstrap
import history


class RetrievalTests(unittest.TestCase):
    def test_forecast_without_context_needs_concrete_choice(self):
        r = history.route('帮我预测未来')
        self.assertTrue(r['needs_clarification'])
        self.assertTrue(r['forecast_mode'])
        found = history.search_cases('帮我预测未来')
        self.assertTrue(found['needs_context'])
        self.assertEqual(found['results'], [])

    def test_mixed_domains_and_modern_cycle(self):
        r = history.route('日本经济下行，失业后要不要转行？')
        self.assertIn('modern', r['modules'])
        self.assertIn('macro', [d['domain'] for d in r['domains']])
        found = history.search_cases('日本就业冰河期失业找工作', modules=['modern'])
        self.assertIn('japan-employment-ice-age', [c['id'] for c in found['results'][:3]])

    def test_explicit_book_and_module_limits(self):
        r = history.search_cases('日本泡沫失业，想辞职', books=['shiji'], modules=['classics'], top=20)
        self.assertTrue(r['results'])
        for row in r['results']:
            self.assertEqual(row['module'], 'classics')
            self.assertEqual(history.read_case(row['id'])['case']['book'], 'shiji')

    def test_retrieval_returns_counterstrategy_candidates(self):
        ids = [c['id'] for c in history.search_cases('老板不重视我，辞职创业还是继续沟通', modules=['classics'])['results']]
        self.assertIn('fanli-exit', ids)
        self.assertIn('lisi-remonstrance', ids)

    def test_same_event_is_not_counted_twice(self):
        real_load = history.load
        def data(name):
            value = copy.deepcopy(real_load(name))
            if name == 'cases.json':
                duplicate = copy.deepcopy(value['cases'][0])
                duplicate['id'] = 'same-event-another-book'
                value['cases'].append(duplicate)
            return value
        with patch.object(history, 'load', side_effect=data):
            results = history.search_cases('范蠡退出辞职', modules=['classics'], top=20)['results']
        groups = [r['event_group'] for r in results]
        self.assertEqual(len(groups), len(set(groups)))

    def test_no_matching_query_has_no_fabricated_case(self):
        self.assertEqual(history.search_cases('qxzv979714') ['results'], [])
        self.assertTrue(history.route('甄嬛传')['unavailable_extensions'])
        with self.assertRaises(ValueError):
            history.read_case('imaginary-case')

    def test_tampered_chapter_is_rejected(self):
        text = '原文'
        locked = {'revision_id': 1, 'sha256': history.digest(text), 'canonical_title': '史記/卷001'}
        cached = {**locked, 'text': text}
        self.assertTrue(history.check_chapter(cached, locked))
        cached['text'] += '伪造'
        self.assertFalse(history.check_chapter(cached, locked))

    def test_all_shipped_evidence_links_and_available_offsets(self):
        result = history.verify()
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['expected_chapters'], 424)


class ImportTests(unittest.TestCase):
    def test_body_survives_but_nav_notes_and_modern_apparatus_do_not(self):
        parser = bootstrap.ChapterParser()
        parser.feed('<div><table class="ws-header"><tr><td>导航</td></tr></table>'
                    '<p>原<sup class="reference">1</sup>文<br/>下一行</p>'
                    '<table><tr><td>古代年表</td><td>史事</td></tr></table>'
                    '<h2>校勘記</h2><p>现代校注</p></div>')
        text = parser.text()
        self.assertIn('原文', text)
        self.assertIn('古代年表', text)
        self.assertNotIn('导航', text)
        self.assertNotIn('现代校注', text)

    def test_pinned_fetch_uses_oldid_and_detects_changed_rendering(self):
        parsed = {'parse': {'text': '<p>' + '原文' * 100 + '</p>', 'title': '史記/卷001', 'revid': 123}}
        spec = {'page_pattern': '史記/卷{volume:03d}'}
        with patch.object(bootstrap, 'request_json', return_value=parsed):
            record, _ = bootstrap.fetch('shiji', 1, spec)
        with patch.object(bootstrap, 'request_json', return_value=parsed) as request:
            bootstrap.fetch('shiji', 1, spec, record)
            self.assertEqual(request.call_args.args[0]['oldid'], 123)
        changed = copy.deepcopy(parsed)
        changed['parse']['text'] += '<p>后改模板</p>'
        with patch.object(bootstrap, 'request_json', return_value=changed):
            with self.assertRaises(ValueError):
                bootstrap.fetch('shiji', 1, spec, record)


if __name__ == '__main__':
    unittest.main()
