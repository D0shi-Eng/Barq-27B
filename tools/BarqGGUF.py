"""GGUF metadata utilities; no model is loaded."""
import hashlib, json, struct
from pathlib import Path

TYPES = {0:'B',1:'b',2:'H',3:'h',4:'I',5:'i',6:'f',7:'?',10:'Q',11:'q',12:'d'}


def text_field(value):
    encoded = value.encode('utf-8')
    return struct.pack('<Q', len(encoded)) + encoded


def parse(path):
    with path.open('rb') as f:
        def number(kind):
            fmt = '<' + TYPES[kind]
            size = struct.calcsize(fmt)
            raw = f.read(size)
            if len(raw) != size:
                raise ValueError('Truncated GGUF number')
            return struct.unpack(fmt, raw)[0]
        def string():
            n = number(10)
            if n > 64 * 1024 * 1024:
                raise ValueError('Unexpected string size')
            raw = f.read(n)
            if len(raw) != n:
                raise ValueError('Truncated GGUF string')
            return raw.decode('utf-8')
        def value(kind):
            if kind == 8:
                return string()
            if kind == 9:
                element, count = number(4), number(10)
                if count > 2_000_000:
                    raise ValueError('Unexpected GGUF array count')
                for _ in range(count):
                    value(element)
                return None
            return number(kind)
        if f.read(4) != b'GGUF':
            raise ValueError('Not GGUF')
        version, tensor_count, count = number(4), number(10), number(10)
        if version != 3 or count > 10000 or tensor_count > 100000:
            raise ValueError('Unexpected GGUF layout')
        records, meta = [], {}
        for _ in range(count):
            start = f.tell()
            key = string()
            kind = number(4)
            val = value(kind)
            end = f.tell()
            f.seek(start)
            records.append((key, f.read(end - start)))
            meta[key] = val
        tensor_start = f.tell()
        descriptors = []
        for _ in range(tensor_count):
            name, dimensions = string(), number(4)
            if dimensions > 8:
                raise ValueError('Unexpected tensor rank')
            shape = [number(10) for _ in range(dimensions)]
            kind, offset = number(4), number(10)
            descriptors.append((name, shape, kind, offset))
        end = f.tell()
        alignment = meta.get('general.alignment', 32)
        if type(alignment) is not int or alignment < 1 or alignment > 4096:
            raise ValueError('Unexpected GGUF alignment')
        data_start = (end + alignment - 1) // alignment * alignment
        f.seek(tensor_start)
        tensor_bytes = f.read(end - tensor_start)
        return records, meta, tensor_bytes, data_start, tensor_count, alignment, descriptors


def build_template(original, core):
    original_effort_text = 'Reasoning effort is set to xhigh. Please think carefully through the task, validate key assumptions, consider plausible alternatives, and prioritize correctness, consistency, and clarity in the final answer.'
    if original.count(original_effort_text) != 1:
        raise ValueError('Unexpected native effort instruction; review the source template before rebuilding.')
    original = original.replace(original_effort_text,
        'Reasoning effort is set to xhigh. Use deeper analysis for genuinely difficult tasks. '
        'For simple questions, answer directly without unnecessary investigation. '
        'Stop reasoning when the requested conclusion is supported.')
    literal = json.dumps(core, ensure_ascii=False)
    prefix = '''{# Barq 27B template adapter v4.0. Original tensor/token protocol retained. #}
{%- set barq_core = CORE_LITERAL -%}
{%- set barq_requested_mode = barq_mode|default(reasoning_effort|default('Default'))|lower -%}
{%- if barq_requested_mode not in ('default', 'low', 'medium', 'high', 'xhigh', 'max') -%}
    {{- raise_exception('Supported Barq modes: Default, Low, Medium, High, Xhigh, Max.') -}}
{%- endif -%}
{%- set reasoning_effort = 'low' if barq_requested_mode == 'low' else ('medium' if barq_requested_mode in ('default', 'medium') else 'xhigh') -%}
{%- set barq_ns = namespace(host='', conversation=[]) -%}
{%- for message in messages -%}
    {%- if message.role in ('system', 'developer') -%}
        {%- if message.content is string -%}
            {%- set barq_ns.host = barq_ns.host + '\\n\\n' + message.content -%}
        {%- elif message.content is iterable and message.content is not mapping -%}
            {%- for part in message.content -%}
                {%- if part.type == 'text' and part.text is string -%}
                    {%- set barq_ns.host = barq_ns.host + '\\n' + part.text -%}
                {%- else -%}
                    {{- raise_exception('Barq system/developer messages must contain text only.') -}}
                {%- endif -%}
            {%- endfor -%}
        {%- elif message.content is not none -%}
            {{- raise_exception('Barq system/developer messages must contain text only.') -}}
        {%- endif -%}
    {%- else -%}
        {%- set barq_ns.conversation = barq_ns.conversation + [message] -%}
    {%- endif -%}
{%- endfor -%}
{%- set barq_system = barq_ns.host if barq_core in barq_ns.host else barq_core + '\\n\\nAPPLICATION INSTRUCTIONS' + barq_ns.host -%}
{%- set messages = [{'role': 'system', 'content': barq_system}] + barq_ns.conversation -%}
'''
    return prefix.replace('CORE_LITERAL', literal) + original


def rewrite(source, destination, changes):
    records, meta, tensor_info, old_start, tensors, alignment, descriptors = parse(source)
    if destination.exists():
        raise RuntimeError('Refuse to replace existing output: ' + str(destination))
    entries = []
    for key, raw in records:
        if key in changes:
            raw = text_field(key) + struct.pack('<I', 8) + text_field(changes[key])
        entries.append(raw)
    for key in sorted(changes.keys() - meta.keys()):
        entries.append(text_field(key) + struct.pack('<I', 8) + text_field(changes[key]))
    header = b'GGUF' + struct.pack('<IQQ', 3, tensors, len(entries)) + b''.join(entries) + tensor_info
    new_start = (len(header) + alignment - 1) // alignment * alignment
    full_hash, payload_hash = hashlib.sha256(), hashlib.sha256()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as src, destination.open('xb') as dst:
        padded_header = header + b'\0' * (new_start - len(header))
        dst.write(padded_header)
        full_hash.update(padded_header)
        src.seek(old_start)
        while chunk := src.read(4 * 1024 * 1024):
            dst.write(chunk)
            full_hash.update(chunk)
            payload_hash.update(chunk)
    # Read-only metadata audit; no Jinja rendering or inference is executed.
    new_records, new_meta, new_info, actual_start, actual_tensors, _, actual_desc = parse(destination)
    if new_info != tensor_info or actual_desc != descriptors or actual_tensors != tensors:
        raise RuntimeError('Tensor descriptors differ; preserve output for inspection.')
    if actual_start != new_start or destination.stat().st_size - new_start != source.stat().st_size - old_start:
        raise RuntimeError('Tensor payload size differs; preserve output for inspection.')
    return {'file': str(destination), 'tensor_count': tensors,
            'tensor_descriptors_byte_identical': True,
            'payload_bytes': source.stat().st_size - old_start,
            'payload_sha256_of_transferred_stream': payload_hash.hexdigest(),
            'file_sha256_of_written_stream': full_hash.hexdigest(),
            'old_data_offset': old_start, 'new_data_offset': new_start,
            'new_name_read_back': new_meta['general.name'],
            'metadata_keys_read_back': len(new_records),
            'model_inference': 'NOT_RUN', 'template_rendering': 'NOT_RUN'}

