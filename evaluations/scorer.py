"""Fixed selection and scorer adapters; no inference or package modifications."""
import ast
import copy
import dataclasses
import hashlib
import json
import os
import random
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / 'vendor'
sys.path.insert(0, str(VENDOR))
os.environ['NLTK_DATA'] = str(ROOT / 'nltk_data')
import nltk
nltk.data.path.insert(0, os.environ['NLTK_DATA'])
from langdetect import DetectorFactory
DetectorFactory.seed = 0
from instruction_following_eval import evaluation_lib as ifeval
from bfcl_eval.constants.enums import Language, ModelStyle
from bfcl_eval.constants.type_mappings import GORILLA_TO_OPENAPI

# Register only the formatting flag used by the unmodified AST checker.
# Do not import unrelated model handlers or their GPU/provider dependencies.
registry = types.ModuleType('bfcl_eval.constants.model_config')
registry.MODEL_CONFIG_MAPPING = {'Barq-27B': types.SimpleNamespace(underscore_to_dot=True)}
sys.modules[registry.__name__] = registry
from bfcl_eval.eval_checker.ast_eval.ast_checker import ast_checker

# Execute the exact two official conversion functions without importing full
# provider/parser modules. Their AST is not rewritten. Record their source hash.
utility = VENDOR / 'bfcl_eval/model_handler/utils.py'
parsed = ast.parse(utility.read_text(encoding='utf-8'))
nodes = [n for n in parsed.body if isinstance(n, ast.FunctionDef) and n.name in ('_cast_to_openai_type', 'convert_to_tool')]
if len(nodes) != 2:
    raise RuntimeError('Expected official schema conversion functions.')
conversion = {'copy': copy, 're': re, 'json': json, 'GORILLA_TO_OPENAPI': GORILLA_TO_OPENAPI, 'ModelStyle': ModelStyle}
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(utility), 'exec'), conversion)


def tools_for(functions):
    return conversion['convert_to_tool'](functions, GORILLA_TO_OPENAPI, ModelStyle.OPENAI_COMPLETIONS)


def jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def score_ifeval(row, content):
    example = ifeval.InputExample(**row)
    strict = ifeval.test_instruction_following_strict(example, {row['prompt']: content})
    loose = ifeval.test_instruction_following_loose(example, {row['prompt']: content})
    return {'strict': dataclasses.asdict(strict), 'loose': dataclasses.asdict(loose)}


def score_bfcl(row, truth, calls, category):
    if category == 'irrelevance':
        return {'valid': len(calls) == 0, 'criterion': 'no native tool calls; BFCL irrelevance behavior'}
    decoded = []
    for call in calls:
        arguments = json.loads(call['arguments'])
        if not isinstance(arguments, dict):
            return {'valid': False, 'error_type': 'arguments_not_object'}
        decoded.append({call['name']: arguments})
    return ast_checker(copy.deepcopy(row['function']), decoded, copy.deepcopy(truth['ground_truth']),
                       Language.PYTHON, category, 'Barq-27B')


def prepare():
    rng = random.Random(20261006)
    data = jsonl(VENDOR / 'instruction_following_eval/data/input_data.jsonl')
    targets = ['detectable_format:json_format', 'detectable_format:number_bullet_lists',
        'length_constraints:number_words', 'length_constraints:number_sentences',
        'keywords:existence', 'keywords:frequency', 'punctuation:no_comma',
        'change_case:english_capital', 'language:response_language', 'startend:end_checker']
    selected = []
    seen = set()
    for target in targets:
        eligible = sorted([r for r in data if target in r['instruction_id_list'] and r['key'] not in seen], key=lambda r: r['key'])
        if not eligible:
            raise RuntimeError('Missing requested instruction family: ' + target)
        row = rng.choice(eligible)
        seen.add(row['key'])
        selected.append({'benchmark': 'IFEval', 'category': target, 'id': str(row['key']), 'source': row})
    for category, count in [('simple_python', 4), ('multiple', 2), ('parallel', 2), ('irrelevance', 2)]:
        rows = sorted(jsonl(VENDOR / ('bfcl_eval/data/BFCL_v4_' + category + '.json')), key=lambda r: r['id'])
        choices = rng.sample(rows, count)
        truths = {} if category == 'irrelevance' else {r['id']: r for r in jsonl(VENDOR / ('bfcl_eval/data/possible_answer/BFCL_v4_' + category + '.json'))}
        for row in choices:
            if len(row['question']) != 1:
                raise RuntimeError('Expected single-turn BFCL input: ' + row['id'])
            # Validate schema adapter before sending any requests.
            tools_for(row['function'])
            selected.append({'benchmark': 'BFCL-v4', 'category': category, 'id': row['id'], 'source': row,
                             'truth': truths.get(row['id'])})
    # Alternate benchmark families to get coverage if the time budget ends early.
    ordered = [item for pair in zip(selected[:10], selected[10:]) for item in pair]
    manifest = {'kind': 'official_dataset_subsets_custom_bounded_runner', 'selection_seed': 20261006,
        'selection': '10 targeted IFEval instruction families, random row within each; BFCL 4/2/2/2 random rows by category. Selected before inference.',
        'sources': json.loads((ROOT / 'source-manifest.json').read_text(encoding='utf-8')),
        'cases': ordered, 'model': 'Barq-27B', 'mode': 'Default', 'temperature': 0, 'seed': 42,
        'thinking_budget_tokens': 512, 'max_tokens': 2048, 'per_request_timeout_s': 90,
        'total_execution_cap_s': 600, 'no_new_requests_after_s': 500, 'concurrency': 1,
        'full_benchmark': False, 'a_b_baseline': False,
        'scorer': 'Unmodified official IFEval strict/loose and BFCL Python AST checker. API schema conversion uses exact official functions. Native FC decoding via JSON, dot names normalized by official converter. Irrelevance=no native tool calls.',
        'tool_execution': False, 'automatic_retries': 0}
    (ROOT / 'selection.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    # Small CPU sanity gates: discriminate correct/wrong output without inference.
    sample = {'key': -1, 'prompt': 'format sanity', 'instruction_id_list': ['detectable_format:json_format'], 'kwargs': [{}]}
    assert score_ifeval(sample, '{"ok":true}')['strict']['follow_all_instructions']
    assert not score_ifeval(sample, 'not JSON')['strict']['follow_all_instructions']
    sample_row = jsonl(VENDOR / 'bfcl_eval/data/BFCL_v4_simple_python.json')[0]
    truth = jsonl(VENDOR / 'bfcl_eval/data/possible_answer/BFCL_v4_simple_python.json')[0]
    good = [{'name': 'calculate_triangle_area', 'arguments': '{"base":10,"height":5}'}]
    bad = [{'name': 'calculate_triangle_area', 'arguments': '{"base":999,"height":5}'}]
    assert score_bfcl(sample_row, truth, good, 'simple_python')['valid']
    assert not score_bfcl(sample_row, truth, bad, 'simple_python')['valid']
    assert score_bfcl({}, None, [], 'irrelevance')['valid']
    nltk.data.load('nltk:tokenizers/punkt/english.pickle').tokenize('One. Two.')
    print(json.dumps({'cases_ready': len(ordered), 'cpu_scorer_sanity': 'PASS',
                      'ifeval_keys': [x['id'] for x in ordered if x['benchmark'] == 'IFEval'],
                      'bfcl_ids': [x['id'] for x in ordered if x['benchmark'] == 'BFCL-v4']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    prepare()
