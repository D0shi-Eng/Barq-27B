"""Prepare a new Barq GGUF distribution from matching upstream artifacts.

No downloads, model execution, training, process stopping or overwrites.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from BarqGGUF import parse, build_template, rewrite

ROOT=Path(__file__).resolve().parents[1]
EXPECTED_MODEL_PAYLOAD='6d3336766bb30dfe033af6b1579fc5817959d0529dc01b89eeadf6bdeb9a50ab'
EXPECTED_PROJECTOR_PAYLOAD='fc7ea97ea966c98a09d1a0c8632e5bed0310dd0196b1681e193b8a7230cc955d'

def file_hash(path):
    h=hashlib.sha256()
    with path.open('rb') as source:
        while chunk:=source.read(4*1024*1024): h.update(chunk)
    return h.hexdigest()

def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def prepare(model_source,projector_source):
    # Publication provides a Windows launcher; refuse policy activation under a live server.
    check=subprocess.run(['powershell.exe','-NoProfile','-Command',
        "@(Get-CimInstance Win32_Process -Filter \"Name = 'llama-server.exe'\").Count"],
        capture_output=True,text=True,check=True,timeout=15,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if int(check.stdout.strip())!=0:
        raise RuntimeError('Close the inference server yourself before preparation; no process was stopped.')
    sources=[Path(model_source).resolve(strict=True),Path(projector_source).resolve(strict=True)]
    if any(not p.is_file() for p in sources): raise ValueError('Both sources must be regular files.')
    targets=[ROOT/'models/Barq-27B-PQ2_0-v4.gguf',ROOT/'models/Barq-27B-mmproj-Q8_0.gguf']
    pending=[p.with_name(p.name+'.installing') for p in targets]
    if any(p.exists() for p in targets+pending):
        raise RuntimeError('A destination/pending file exists. Nothing will be overwritten.')
    if shutil.disk_usage(ROOT).free < sum(p.stat().st_size for p in sources)+512*1024*1024:
        raise RuntimeError('Insufficient free disk for both new artifacts plus reserve.')
    policy_path=ROOT/'prompts/Barq-27B-System.txt'
    policy=policy_path.read_text(encoding='utf-8').strip()
    policy_sha=file_hash(policy_path)
    marker=re.match(r'\[BARQ_CORE_VERSION=([0-9.]+)\]',policy)
    if not marker or marker.group(1)!='4.0': raise ValueError('Expected published core policy 4.0.')
    package=json.loads((ROOT/'config/Barq-Package.json').read_text(encoding='utf-8'))
    if package['policy_sha256']!=policy_sha: raise RuntimeError('Policy and package hash differ.')
    source_meta=parse(sources[0])[1]
    template=build_template(source_meta['tokenizer.chat_template'],policy)
    changes=[{
        'general.name':'Barq 27B',
        'general.description':'Barq 27B derivative: original operating policy, embedded template and local serving gateway. Numerical tensor weights unchanged. See legal/NOTICE.txt.',
        'tokenizer.chat_template':template,'barq.version':'4.0','barq.policy.sha256':policy_sha,
        'barq.reasoning_modes':(ROOT/'config/Barq-Modes.json').read_text(encoding='utf-8'),
        'barq.modification_scope':'Operating policy, template, display metadata and serving integration; no weight training or requantization.'
    },{
        'general.name':'Barq 27B Vision Projector',
        'general.description':'Display metadata repackaging; tensor payload unchanged. See legal/NOTICE.txt.'
    }]
    results=[]
    for source,target,temporary,metadata,expected in zip(sources,targets,pending,changes,
        [EXPECTED_MODEL_PAYLOAD,EXPECTED_PROJECTOR_PAYLOAD]):
        result=rewrite(source,temporary,metadata)
        if result['payload_sha256_of_transferred_stream']!=expected:
            raise RuntimeError('Source tensor payload does not match the evaluated upstream artifact; pending output retained.')
        disk_hash=file_hash(temporary)
        if disk_hash!=result['file_sha256_of_written_stream']:
            raise RuntimeError('Full readback hash mismatch; pending output retained.')
        if target.exists(): raise RuntimeError('Destination appeared during preparation; pending output retained.')
        temporary.rename(target)
        result.update(file=target.relative_to(ROOT).as_posix(),bytes=target.stat().st_size,
            file_sha256_readback=disk_hash,policy_sha256=policy_sha if target==targets[0] else None)
        results.append(result)
    (ROOT/'prompts/Barq-27B-Chat-Template.jinja').write_text(template,encoding='utf-8')
    manifest={'active_policy':{'model':targets[0].relative_to(ROOT).as_posix(),
        'model_bytes':results[0]['bytes'],'model_sha256':results[0]['file_sha256_readback'],
        'policy_sha256':policy_sha,'policy_version':'4.0'},
        'package_files':[{'path':'prompts/Barq-27B-System.txt','bytes':policy_path.stat().st_size,'sha256':policy_sha}],
        'preparation':{'tensor_weights_unchanged':True,'full_readback_hash_checked':True,
            'inference_performed':False,'source_artifacts':'Matching upstream PQ2_0 and Q8_0 projector',
            'results':results}}
    save(ROOT/'legal/Installation-Manifest.json',manifest)
    print(json.dumps({'prepared':[r['file'] for r in results],'weights_trained':False,
        'inference_performed':False,'manifest':'legal/Installation-Manifest.json'},indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-model',required=True)
    parser.add_argument('--source-projector',required=True)
    args=parser.parse_args()
    prepare(args.source_model,args.source_projector)
