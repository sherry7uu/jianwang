#!/usr/bin/env python3
"""Offline source-bounded retrieval for the History Decision skill (Python 3.10+)."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    return json.loads((ROOT / 'corpus' / name).read_text(encoding='utf-8'))


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def tokens(text):
    # Deliberately lexical. Semantic interpretation belongs to the host assistant.
    result = []
    for word in re.findall(r'[\u3400-\u9fff]+|[a-z0-9]+', text.lower()):
        result.extend([word] if len(word) < 2 or word.isascii() else
                      [word[i:i + 2] for i in range(len(word) - 1)])
    return result


def route(query):
    config = load('routing.json')
    hits = []
    for domain, rule in config['domains'].items():
        matched = [word for word in rule['keywords'] if word.lower() in query.lower()]
        if matched:
            hits.append({'domain': domain, 'label': rule['label'], 'matched': matched,
                         'priority_books': rule['priority_books'], 'tags': rule['tags']})
    hits.sort(key=lambda row: len(row['matched']), reverse=True)
    modern = any(word in query for word in config['modern_triggers'])
    return {
        'domains': hits,
        'routing_basis': '可解释的关键词提示；助手须结合语义复核，可由用户覆盖，不是分类概率。',
        'needs_clarification': not hits,
        'clarification': '你正面临什么选择、有哪些选项、希望在多长时间内改善什么？' if not hits else None,
        'books': list(dict.fromkeys(book for row in hits for book in row['priority_books'])) or ['shiji', 'zizhitongjian'],
        'modules': ['classics', 'modern'] if modern else ['classics'],
        'forecast_mode': any(word in query for word in ['未来', '預測', '预测', '運勢', '运势', '命运']),
        'forecast_rule': '将预测转为条件情景、可观察信号与调整计划；历史相似度不等于现实发生概率。',
        'capability_rule': '先确认已做成的事、失败经历、资源余量、压力与权力差；未知项保留未知。',
        'unavailable_extensions': [name for name in ['甄嬛传', '甄嬛傳'] if name in query],
    }


def rank(query, documents):
    query_tokens = set(tokens(query))
    tokenized = [Counter(tokens(text)) for text in documents]
    frequencies = Counter(term for counts in tokenized for term in counts)
    total = len(documents)
    average = sum(sum(c.values()) for c in tokenized) / max(total, 1)
    scores = []
    for counts in tokenized:
        length = sum(counts.values())
        score = 0.0
        for term in query_tokens & counts.keys():
            idf = math.log(1 + (total - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            tf = counts[term]
            score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / max(average, 1)))
        scores.append(score)
    return scores


def search_cases(query, books=None, modules=None, top=5):
    routed = route(query)
    modules = modules or routed['modules']
    if routed['needs_clarification'] and routed['forecast_mode']:
        return {'route': routed, 'scope': {'books': books or ['shiji', 'zizhitongjian'], 'modules': modules},
                'results': [], 'needs_context': True, 'missing_evidence': False,
                'retrieval': '等待具体问题；不因“未来”一词自动套用人物故事。'}
    cases = [c for c in load('cases.json')['cases'] if c['module'] in modules
             and (not books or c.get('book') in books)]
    tags = {tag for d in routed['domains'] for tag in d['tags']}
    enriched = query + ' ' + ' '.join(tags)
    documents = [' '.join([c['title'], c['situation'], ' '.join(c['tags']),
                            ' '.join(c.get('aliases', []))]) for c in cases]
    scored = sorted(zip(rank(enriched, documents), cases), key=lambda row: row[0], reverse=True)
    seen = set()
    result = []
    for score, case in scored:
        if score <= 0 or case['event_group'] in seen:
            continue
        seen.add(case['event_group'])
        result.append({
            'id': case['id'], 'title': case['title'], 'module': case['module'],
            'score': round(score, 3), 'event_group': case['event_group'],
            'matched_tags': sorted(tags.intersection(case['tags'])),
            'situation': case['situation'], 'evidence_ids': case['evidence_ids'],
            'interpretation': '排序分数只用于召回；须阅读证据、比较结构和关键差异后再采用。',
        })
        if len(result) >= top:
            break
    return {'route': routed, 'scope': {'books': books or ['shiji', 'zizhitongjian'], 'modules': modules},
            'results': result, 'retrieval': '中文二元字词 BM25 + 场景标签；非向量语义检索',
            'missing_evidence': not result}


def chapter_path(book, volume):
    return ROOT / '.cache' / 'chapters' / f'{book}-{volume:03d}.json'


def check_chapter(data, locked):
    return (data.get('revision_id') == locked['revision_id']
            and data.get('sha256') == locked['sha256']
            and digest(data.get('text', '')) == locked['sha256']
            and data.get('canonical_title') == locked['canonical_title'])


def search_text(terms, books=None, top=5):
    lock = load('lock.json')['chapters']
    results = []
    unavailable = 0
    for key, record in lock.items():
        if books and record['book'] not in books:
            continue
        path = chapter_path(record['book'], record['volume'])
        if not path.exists():
            unavailable += 1
            continue
        data = json.loads(path.read_text())
        if not check_chapter(data, record):
            raise ValueError(f'corrupt or unpinned chapter: {key}')
        text = data['text']
        start = 0
        for paragraph in text.split('\n\n'):
            matches = [term for term in terms if term in paragraph]
            if matches:
                hit = min(paragraph.index(t) for t in matches)
                left = max(0, hit - 180)
                right = min(len(paragraph), hit + 700)
                quote = paragraph[left:right]
                results.append({'book': record['book'], 'volume': record['volume'],
                                'title': record['canonical_title'], 'matched_terms': matches,
                                'start': start + left, 'end': start + right,
                                'quote': quote, 'quote_sha256': digest(quote),
                                'url': record['url'], 'chapter_sha256': record['sha256']})
            start += len(paragraph) + 2
    results.sort(key=lambda item: (len(item['matched_terms']), len(item['quote'])), reverse=True)
    return {'results': results[:top], 'uncached_chapters': unavailable,
            'note': '原字精确检索；可用韩信/韓信等异体多次查询。仅搜索已校验的本地白名单原文。'}


def read_case(case_id):
    cases = {c['id']: c for c in load('cases.json')['cases']}
    evidence = {e['id']: e for e in load('evidence.json')['evidence']}
    if case_id not in cases:
        raise ValueError(f'unknown case id: {case_id}')
    case = cases[case_id]
    return {'case': case, 'evidence': [evidence[eid] for eid in case['evidence_ids']]}


def verify(require_full=False):
    sources = load('sources.json')['books']
    lock = load('lock.json')['chapters']
    evidence_list = load('evidence.json')['evidence']
    modern_sources = {s['id']: s for s in load('modern-sources.json')['sources']}
    cases = load('cases.json')['cases']
    errors = []
    expected = sum(b['volumes'] for b in sources.values())
    expected_keys = {f'{book}:{v:03d}' for book, spec in sources.items() for v in range(1, spec['volumes'] + 1)}
    if set(lock) != expected_keys:
        errors.append('chapter lock has missing or unexpected volumes')
    cached = {}
    for key, entry in lock.items():
        path = chapter_path(entry['book'], entry['volume'])
        if not path.exists():
            if require_full:
                errors.append(f'missing cache: {key}')
            continue
        data = json.loads(path.read_text())
        if not check_chapter(data, entry):
            errors.append(f'chapter integrity mismatch: {key}')
        cached[key] = data
    evidence = {e['id']: e for e in evidence_list}
    if len(evidence) != len(evidence_list):
        errors.append('duplicate evidence id')
    for e in evidence_list:
        if e['kind'] == 'classical_excerpt':
            key = f'{e["book"]}:{e["volume"]:03d}'
            entry = lock.get(key)
            if not entry or e['book'] not in sources:
                errors.append(f'evidence outside allowlist: {e["id"]}')
                continue
            if e['quote_sha256'] != digest(e['quote']):
                errors.append(f'quote hash mismatch: {e["id"]}')
            if e['chapter_sha256'] != entry['sha256'] or e['revision_id'] != entry['revision_id'] or e['url'] != entry['url']:
                errors.append(f'quote source mismatch: {e["id"]}')
            if key in cached and cached[key]['text'][e['start']:e['end']] != e['quote']:
                errors.append(f'quote not at pinned offset: {e["id"]}')
        elif e['kind'] == 'research_summary':
            if not e.get('source_ids') or any(s not in modern_sources for s in e['source_ids']):
                errors.append(f'unknown modern source: {e["id"]}')
            if e.get('verbatim', True) or not e.get('locator'):
                errors.append(f'modern summary incorrectly identified: {e["id"]}')
        else:
            errors.append(f'unknown evidence kind: {e["id"]}')
    if len({c['id'] for c in cases}) != len(cases):
        errors.append('duplicate case id')
    for c in cases:
        for field in ['event_group', 'situation', 'known_then', 'options', 'chosen_action',
                      'outcomes', 'hidden_costs', 'capability_requirements', 'transfer_limits', 'evidence_ids']:
            if not c.get(field):
                errors.append(f'missing {field}: {c["id"]}')
        if any(e not in evidence for e in c['evidence_ids']):
            errors.append(f'case has unknown evidence: {c["id"]}')
        for option in c['options']:
            if option['basis'] not in {'source_explicit', 'analyst_inferred'}:
                errors.append(f'option lacks epistemic label: {c["id"]}')
        for outcome in c['outcomes']:
            if not outcome.get('evidence_ids') or any(e not in c['evidence_ids'] for e in outcome['evidence_ids']):
                errors.append(f'outcome lacks linked evidence: {c["id"]}')
    return {'ok': not errors, 'locked_chapters': len(lock), 'expected_chapters': expected,
            'cached_chapters': len(cached), 'cases': len(cases), 'evidence_items': len(evidence),
            'verification': '完整原文与逐字偏移校验' if len(cached) == expected else '选段哈希与来源清单校验；原文未全部缓存',
            'errors': errors}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('route'); p.add_argument('query')
    p = sub.add_parser('search'); p.add_argument('query')
    p.add_argument('--books', nargs='+', choices=['shiji', 'zizhitongjian'])
    p.add_argument('--modules', nargs='+', choices=['classics', 'modern'])
    p.add_argument('--top', type=int, choices=range(1, 21), default=5)
    p = sub.add_parser('text'); p.add_argument('terms', nargs='+')
    p.add_argument('--books', nargs='+', choices=['shiji', 'zizhitongjian'])
    p.add_argument('--top', type=int, choices=range(1, 21), default=5)
    p = sub.add_parser('read'); p.add_argument('case_id')
    p = sub.add_parser('verify'); p.add_argument('--full', action='store_true')
    sub.add_parser('status')
    args = ap.parse_args(argv)
    try:
        if args.command == 'route': result = route(args.query)
        elif args.command == 'search': result = search_cases(args.query, args.books, args.modules, args.top)
        elif args.command == 'text': result = search_text(args.terms, args.books, args.top)
        elif args.command == 'read': result = read_case(args.case_id)
        else: result = verify(getattr(args, 'full', False))
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({'error': str(exc), 'action': '检查 corpus 文件；首次全文检索先运行 scripts/bootstrap.py。'}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get('ok') is False else 0


if __name__ == '__main__':
    sys.exit(main())
