"""Recompute saved official subset scores on CPU. Never contacts the server."""
import argparse
import json
from pathlib import Path
import scorer as scoring

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('results', type=Path)
    args = parser.parse_args()
    selected = json.loads((args.results / 'selection.json').read_text(encoding='utf-8'))
    cases = {x['id']: x for x in selected['cases']}
    records = [json.loads(line) for line in (args.results / 'responses.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    mismatch = []
    for record in records:
        item = cases[record['id']]
        if item['benchmark'] == 'IFEval':
            actual = scoring.score_ifeval(item['source'], record['content'])
        else:
            try:
                actual = scoring.score_bfcl(item['source'], item.get('truth'), record['tool_calls'], item['category'])
            except json.JSONDecodeError as exc:
                actual = {'valid': False, 'error_type': 'invalid_model_arguments_json', 'error': str(exc)}
        if actual != record.get('score'):
            mismatch.append(record['id'])
    print(json.dumps({'mode': 'CPU_ONLY_OFFLINE_RESCORING', 'checked': len(records), 'mismatches': mismatch, 'pass': not mismatch}, ensure_ascii=False))
    if mismatch:
        raise SystemExit(1)

if __name__ == '__main__':
    main()
