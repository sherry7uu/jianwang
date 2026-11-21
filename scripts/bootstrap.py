#!/usr/bin/env python3
"""Download the allowlisted Wikisource chapters, with resumable pinned snapshots."""
import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / '.cache' / 'chapters'
PARSER_VERSION = 1
USER_AGENT = 'HistoryDecisionSkill/0.1 (https://github.com/sherry7uu/history-decision-skill)'


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


class ChapterParser(HTMLParser):
    """Extract body blocks, excluding navigation, references, and modern apparatus.

    Character offsets refer to this extracted snapshot, never to a printed edition.
    Keep original glyphs and punctuation; no script conversion or OCR correction.
    """
    SKIP_CLASSES = {
        'headerTemplate', 'navbox', 'noprint', 'mw-editsection', 'toc',
        'references', 'reflist', 'reference', 'licenseContainer', 'licensetpl',
        'ws-noexport', 'metadata', 'infobox', 'thumb', 'sistersitebox',
        'ws-export', 'acContainer', 'catlinks', 'wst-header', 'ws-header', 'ombox',
    }
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
            'meta', 'param', 'source', 'track', 'wbr'}
    BLOCKS = {'p', 'div', 'tr', 'li', 'dl', 'dt', 'dd', 'blockquote',
              'h1', 'h2', 'h3', 'h4', 'h5', 'table'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.blocks = []
        self.current = []
        self.heading = None
        self.stopped = False

    def flush(self):
        text = re.sub(r'\s+', ' ', ''.join(self.current)).strip()
        if text:
            self.blocks.append(text)
        self.current = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        parent_skip = self.stack[-1][1] if self.stack else False
        classes = set(attrs.get('class', '').split())
        skip = parent_skip or tag in {'script', 'style', 'sup'} or bool(classes & self.SKIP_CLASSES)
        skip = skip or attrs.get('id') in {'headerContainer', 'toc', 'footertemplate', 'header_detail_field'}
        if tag not in self.VOID:
            self.stack.append((tag, skip))
        if skip or self.stopped:
            return
        if tag in self.BLOCKS:
            self.flush()
        if re.fullmatch(r'h[1-6]', tag):
            self.heading = []
        if tag in {'br', 'td', 'th'}:
            self.current.append(' ')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        matching = next((i for i in range(len(self.stack) - 1, -1, -1)
                         if self.stack[i][0] == tag), None)
        if matching is None:
            return
        skipped = self.stack[matching][1]
        del self.stack[matching:]
        if skipped or self.stopped:
            return
        if re.fullmatch(r'h[1-6]', tag) and self.heading is not None:
            label = ''.join(self.heading).strip()
            self.heading = None
            if label in {'校刊記', '校勘記', '斠勘', '注釋', '注释', '註釋', '參考資料', '参考资料'}:
                self.current = []
                self.stopped = True
                return
        if tag in self.BLOCKS:
            self.flush()

    def handle_data(self, data):
        if self.stopped or (self.stack and self.stack[-1][1]):
            return
        self.current.append(data)
        if self.heading is not None:
            self.heading.append(data)

    def text(self):
        self.flush()
        return '\n\n'.join(self.blocks)


def request_json(params):
    url = 'https://zh.wikisource.org/w/api.php?' + urllib.parse.urlencode({
        'format': 'json', 'formatversion': 2, 'maxlag': 5, **params})
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(req, timeout=45) as response:
                data = json.load(response)
            if 'error' in data:
                if data['error'].get('code') == 'maxlag':
                    time.sleep(5 * (attempt + 1))
                    continue
                raise RuntimeError(str(data['error']))
            return data
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise
            time.sleep(min(30, max(int(exc.headers.get('Retry-After', '0')), 2 ** (attempt + 1))))
        except (TimeoutError, urllib.error.URLError):
            if attempt == 3:
                raise
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError('Wikisource unavailable after bounded retries')


def fetch(book_id, volume, spec, locked=None):
    requested_title = spec['page_pattern'].format(volume=volume)
    params = {'action': 'parse', 'prop': 'text|revid|displaytitle', 'disablelimitreport': 1,
              'disableeditsection': 1, 'redirects': 1}
    params.update({'oldid': locked['revision_id']} if locked else {'page': requested_title})
    parsed = request_json(params)['parse']
    parser = ChapterParser()
    parser.feed(parsed['text'])
    text = parser.text()
    if len(text) < 100:
        raise ValueError(f'{requested_title}: extracted only {len(text)} characters; inspect source')
    record = {
        'book': book_id, 'volume': volume, 'requested_title': requested_title,
        'canonical_title': parsed['title'], 'revision_id': parsed['revid'],
        'url': 'https://zh.wikisource.org/w/index.php?oldid=' + str(parsed['revid']),
        'history_url': 'https://zh.wikisource.org/w/index.php?' + urllib.parse.urlencode({
            'title': parsed['title'], 'action': 'history'}),
        'sha256': sha(text), 'characters': len(text), 'parser_version': PARSER_VERSION,
        'license': 'CC-BY-SA-4.0',
    }
    if locked and (record['sha256'] != locked['sha256'] or record['canonical_title'] != locked['canonical_title']):
        raise ValueError(f'{requested_title}: pinned text hash changed; preserve old evidence and inspect templates/parser before updating locks')
    return record, text


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--books', nargs='+', choices=['shiji', 'zizhitongjian'], default=['shiji', 'zizhitongjian'])
    ap.add_argument('--volumes', nargs='+', type=int, help='Optional volume subset, applied to each selected book')
    ap.add_argument('--delay', type=float, default=0.25, help='Delay between sequential requests, seconds')
    args = ap.parse_args(argv)
    if args.delay < 0.1:
        ap.error('--delay must be at least 0.1 seconds')
    source = json.loads((ROOT / 'corpus/sources.json').read_text())
    lock_path = ROOT / 'corpus/lock.json'
    lock = json.loads(lock_path.read_text()) if lock_path.exists() else {'schema_version': 1, 'chapters': {}}
    failures = []
    completed = 0
    for book in args.books:
        spec = source['books'][book]
        for volume in (args.volumes or range(1, spec['volumes'] + 1)):
            if not 1 <= volume <= spec['volumes']:
                ap.error(f'invalid volume {volume} for {book}')
            key = f'{book}:{volume:03d}'
            destination = CACHE / f'{book}-{volume:03d}.json'
            locked = lock['chapters'].get(key)
            if destination.exists() and locked:
                cached = json.loads(destination.read_text())
                if sha(cached.get('text', '')) == locked['sha256'] and cached.get('revision_id') == locked['revision_id']:
                    completed += 1
                    continue
            try:
                record, text = fetch(book, volume, spec, locked)
                write_json(destination, {**record, 'text': text})
                lock['chapters'][key] = record
                write_json(lock_path, lock)
                completed += 1
                print(f'{key} {len(text)} chars, revision {record["revision_id"]}', flush=True)
            except (ValueError, RuntimeError, OSError) as exc:
                failures.append({'chapter': key, 'error': str(exc)})
                print(f'FAILED {key}: {exc}', file=sys.stderr, flush=True)
            time.sleep(args.delay)
    print(json.dumps({'completed': completed, 'failed': failures, 'locked_chapters': len(lock['chapters'])}, ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
