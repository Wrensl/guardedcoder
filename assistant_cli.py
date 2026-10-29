"""Local QUESTION/SUGGEST provider interface and separate explicit APPLY gate."""
import json
import os
from assistant import NonAutonomousAssistant
from model_modes import ModelModes
from provider import OpenAICompatibleProvider, ProviderConfig, ProviderError
from tools import ToolError

def model_modes(app):
    def price(name):
        raw=os.environ.get(name)
        return None if raw is None else float(raw)
    base_url=os.environ.get('LEVEL2_BASE_URL','https://api.openai.com/v1')
    model=os.environ.get('LEVEL2_MODEL','gpt-6-luna')
    default_prices=(base_url.rstrip('/')=='https://api.openai.com/v1' and model=='gpt-6-luna')
    config=ProviderConfig(
        base_url=base_url,
        model=model,
        api_key_env=os.environ.get('LEVEL2_API_KEY_ENV','LEVEL2_API_KEY'),
        timeout=float(os.environ.get('LEVEL2_TIMEOUT','45')),
        input_usd_per_million=price('LEVEL2_INPUT_USD_PER_MILLION') if os.environ.get('LEVEL2_INPUT_USD_PER_MILLION') else (.10 if default_prices else None),
        output_usd_per_million=price('LEVEL2_OUTPUT_USD_PER_MILLION') if os.environ.get('LEVEL2_OUTPUT_USD_PER_MILLION') else (.50 if default_prices else None),
    )
    return ModelModes(app,OpenAICompatibleProvider(config))

def main():
    selected=input('Repository path (Git working tree): ').strip()
    if not selected:
        print('A repository path is required.')
        return 2
    try:
        app=NonAutonomousAssistant(selected)
    except (ToolError,ValueError,OSError) as exc:
        print('Cannot use repository:',exc)
        return 2
    print('Repository:',app.root)
    while True:
        mode=input('Mode [question/suggest/apply/quit]: ').strip().lower()
        if mode=='quit': return 0
        try:
            if mode=='question':
                prompt=input('Question: ').strip()
                result=model_modes(app).question(prompt)
                print(json.dumps(result,indent=2))
            elif mode=='suggest':
                prompt=input('Requested fix: ').strip()
                result=model_modes(app).suggest_fix(prompt)
                if result['proposal'] is None:
                    print(result['reason'])
                else:
                    print(result['proposal']['diff'])
                    print('Validated proposal only; no files were changed.')
                    print('Approval token:',result['proposal']['approval_token'])
                print('Files read:',', '.join(result['files_read']))
                print('Tool calls:',json.dumps(result['tool_calls']))
                print('Usage:',json.dumps(result['usage']))
            elif mode=='apply':
                token=input('Approval token: ').strip()
                if input('Type APPLY to approve this patch: ').strip()!='APPLY':
                    print('Not applied');continue
                print(json.dumps(app.apply_approved(token,approved=True),indent=2))
            else: print('Unknown mode')
        except (ToolError,ProviderError,ValueError,json.JSONDecodeError) as exc: print('Rejected:',exc)

if __name__=='__main__': raise SystemExit(main())
