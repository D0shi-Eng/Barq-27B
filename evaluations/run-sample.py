"""Bounded official dataset subsets; single worker, no actual tool execution."""
import hashlib
import json
import math
import shutil
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
import scorer as scoring
import transport

ROOT = Path(__file__).resolve().parents[1]
BENCH = Path(__file__).resolve().parent / 'instruction-and-tools'
transport.MAX_TOKENS = 2048
transport.THINKING = 512
transport.DEADLINE = 90

def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def idle():
    slots = transport.small_get(8082, '/slots')
    if not isinstance(slots, list) or len(slots) != 1 or slots[0].get('is_processing'):
        raise RuntimeError('Backend is busy or unsupported slot state; no next request sent.')
    return {'slots': 1, 'processing': False}

def main():
    selection = json.loads((BENCH / 'selection.json').read_text(encoding='utf-8'))
    out = ROOT / 'outputs' / ('Barq-IFEval-BFCL-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(BENCH / 'selection.json', out / 'selection.json')
    shutil.copy2(BENCH / 'source-manifest.json', out / 'source-manifest.json')
    for file in [Path(__file__), Path(__file__).resolve().parent / 'scorer.py', Path(__file__).resolve().parent / 'transport.py']:
        shutil.copy2(file, out / file.name)
    print(json.dumps({'output': str(out), 'status': 'preflight'}, ensure_ascii=False), flush=True)
    records, checks, abort = [], [], None
    clock = time.monotonic()
    started = datetime.now(timezone.utc).isoformat()
    try:
        preflight = transport.resources_and_model()
        preflight.update(idle())
        transport.small_get(8081, '/v1/models')
        package = json.loads(Path('D:/Barq 27B/config/Barq-Package.json').read_text(encoding='utf-8'))
        if package.get('policy_version') != '4.0':
            raise RuntimeError('Unexpected policy version; no inference sent.')
        manifest = {'selection_sha256': digest(out / 'selection.json'), 'started_utc': started,
            'permission': 'User authorized at most 10 minutes, basic cases only, sequential existing-server inference.',
            'preflight': preflight, 'package': package,
            'policy_sha256': digest(Path('D:/Barq 27B/prompts/Barq-27B-System.txt')),
            'gateway_sha256': digest(Path('D:/Barq 27B/server/Barq-Gateway.py')),
            'execution_cap_s': 600, 'last_request_admission_s': 500, 'max_tokens': 2048,
            'thinking_budget_tokens': 512, 'mode': 'Default', 'temperature': 0, 'seed': 42,
            'request_timeout_s': 90, 'concurrency': 1, 'automatic_retries': 0,
            'full_benchmark': False, 'a_b_baseline': False, 'real_tool_execution': False}
        write(out / 'manifest.json', manifest)
        with (out / 'responses.jsonl').open('w', encoding='utf-8') as f:
            for index, item in enumerate(selection['cases']):
                elapsed = time.monotonic() - clock
                if elapsed >= 500:
                    abort = 'Request admission cutoff reached; remaining cases NOT_RUN.'
                    break
                if index % 4 == 0:
                    state = transport.resources_and_model()
                    checks.append({'elapsed_s': round(time.monotonic()-clock, 3), **state})
                idle()
                if time.monotonic() - clock >= 500:
                    abort = 'Request admission cutoff reached after readiness check.'
                    break
                body = {'model': 'Barq-27B', 'barq_mode': 'Default',
                    'max_tokens': 2048, 'reasoning_budget_tokens': 512, 'temperature': 0, 'seed': 42,
                    'stream': True, 'stream_options': {'include_usage': True}}
                row = item['source']
                if item['benchmark'] == 'IFEval':
                    body['messages'] = [{'role': 'user', 'content': row['prompt']}]
                else:
                    body['messages'] = row['question'][0]
                    body['tools'] = scoring.tools_for(row['function'])
                case = {'id': item['id'], 'benchmark': item['benchmark'], 'category': item['category'],
                    'mode': 'Default', 'prompt': '', 'request_override': body}
                record = transport.infer(case)
                record['benchmark'] = item['benchmark']
                record['category'] = item['category']
                record['id'] = item['id']
                try:
                    if item['benchmark'] == 'IFEval':
                        verdict = scoring.score_ifeval(row, record['content'])
                        passed = verdict['strict']['follow_all_instructions']
                    else:
                        try:
                            verdict = scoring.score_bfcl(row, item.get('truth'), record['tool_calls'], item['category'])
                        except json.JSONDecodeError as exc:
                            verdict = {'valid': False, 'error_type': 'invalid_model_arguments_json', 'error': str(exc)}
                        passed = verdict['valid']
                    record.update(score=verdict, scorer_status='OK', passed=bool(passed))
                except Exception as exc:
                    record.update(scorer_status='ERROR', scorer_error=type(exc).__name__+': '+str(exc), passed=None)
                records.append(record)
                f.write(json.dumps(record, ensure_ascii=False)+'\n')
                f.flush()
                print(json.dumps({'index': index+1, 'benchmark': item['benchmark'], 'id': item['id'],
                    'passed': record['passed'], 'seconds': record['duration_s'], 'finish': record['finish_reason'],
                    'elapsed_s': round(time.monotonic()-clock, 2), 'transport_error': record.get('error'),
                    'scorer_status': record['scorer_status']}, ensure_ascii=False), flush=True)
                if record.get('error'):
                    abort = 'Transport/service failure; stopped without another inference request.'
                    break
                if record['scorer_status'] != 'OK':
                    abort = 'Scorer failure; stopped to avoid invalid benchmark claims.'
                    break
    except Exception as exc:
        abort = type(exc).__name__+': '+str(exc)
    end_elapsed = round(time.monotonic()-clock, 4)
    # The timed request window is over. These are read-only postflight observations.
    postflight = {}
    for name, func in [('resources', transport.resources_and_model), ('backend', idle)]:
        try:
            postflight[name] = func()
        except Exception as exc:
            postflight[name] = {'error': str(exc)}
    write(out / 'resource-checks.json', {'during': checks, 'postflight': postflight})
    scoreable = [r for r in records if r['scorer_status'] == 'OK']
    ife = [r for r in scoreable if r['benchmark'] == 'IFEval']
    bfc = [r for r in scoreable if r['benchmark'] == 'BFCL-v4']
    metrics = {}
    for variant in ['strict', 'loose']:
        flags = [v for r in ife for v in r['score'][variant]['follow_instruction_list']]
        metrics[variant] = {'prompt_passed': sum(r['score'][variant]['follow_all_instructions'] for r in ife),
            'prompt_total': len(ife), 'instruction_passed': sum(flags), 'instruction_total': len(flags)}
    categories = {}
    for category in ['simple_python', 'multiple', 'parallel', 'irrelevance']:
        items = [r for r in bfc if r['category'] == category]
        categories[category] = {'passed': sum(r['passed'] for r in items), 'total': len(items)}
    durations = [r['duration_s'] for r in records]
    first = [min(v for v in [r['first_answer_text_s'], r['first_tool_delta_s']] if v is not None)
        for r in records if r['first_answer_text_s'] is not None or r['first_tool_delta_s'] is not None]
    usage = [r['usage'] for r in records if r['usage'] is not None]
    prompt_tokens = sum(u.get('prompt_tokens', 0) for u in usage)
    output_tokens = sum(u.get('completion_tokens', 0) for u in usage)
    inference_s = sum(durations)
    summary = {'kind': 'official_subset_bounded_run_not_full_benchmark', 'output': str(out),
        'started_utc': started, 'ended_utc': datetime.now(timezone.utc).isoformat(),
        'timed_execution_window_s': end_elapsed, 'planned': len(selection['cases']), 'attempted': len(records),
        'not_run': [x['id'] for x in selection['cases'][len(records):]], 'abort_reason': abort,
        'transport_errors': sum(bool(r.get('error')) for r in records),
        'scorer_errors': sum(r['scorer_status'] != 'OK' for r in records),
        'truncated_responses': sum(r['finish_reason'] == 'length' for r in records),
        'ifeval': metrics, 'bfcl': {'passed': sum(r['passed'] for r in bfc), 'total': len(bfc), 'categories': categories},
        'performance': {'sum_request_s': round(inference_s,4), 'median_request_s': statistics.median(durations) if durations else None,
            'p95_request_s_nearest_rank': sorted(durations)[math.ceil(.95*len(durations))-1] if durations else None,
            'max_request_s': max(durations) if durations else None,
            'median_first_visible_answer_or_tool_s': statistics.median(first) if first else None,
            'usage_records': len(usage), 'prompt_tokens': prompt_tokens, 'completion_tokens': output_tokens,
            'completion_tokens_per_request_wall_second': round(output_tokens/inference_s,3) if inference_s else None,
            'throughput_definition': 'Completion tokens / total request wall time, including prompt processing and reasoning; not decoder-only tokens/s.'}}
    write(out / 'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
