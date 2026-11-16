"""Generate local self-hosting credentials without copying real deployment secrets."""
from pathlib import Path
import base64
import secrets

root = Path(__file__).resolve().parents[1] / 'services/literature-kb'
target = root / '.env'
if target.exists():
    raise SystemExit('services/literature-kb/.env already exists; edit it rather than overwriting credentials')
values = {'POSTGRES_PASSWORD': secrets.token_hex(24), 'API_AUTH_TOKEN': secrets.token_hex(32), 'ADMIN_API_TOKEN': secrets.token_hex(32), 'MOLYTE_API_TOKEN': secrets.token_hex(32), 'ADMIN_SECRETS_MASTER_KEY': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}
text = (root / '.env.example').read_text(encoding='utf-8')
for key, value in values.items():
    text = '\n'.join(f'{key}={value}' if line.startswith(key+'=') else line for line in text.splitlines()) + '\n'
text = text.replace('PIPELINE_ENABLED=false', 'PIPELINE_ENABLED=true')
target.write_text(text, encoding='utf-8')
print('Created services/literature-kb/.env. Configure parser/model channels after startup; do not publish this file.')
