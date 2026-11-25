import argparse,hashlib,io,json,zipfile
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
p=argparse.ArgumentParser()
p.add_argument('--key-file',required=True,type=Path)
p.add_argument('--output',required=True,type=Path)
a=p.parse_args()
b=Path(__file__).with_name('project-data.enc').read_bytes()
assert b[:11]==b'PBTRANSFER1'
raw=AESGCM(bytes.fromhex(a.key_file.read_text().strip())).decrypt(b[11:23],b[23:],b'PBTRANSFER1')
with zipfile.ZipFile(io.BytesIO(raw)) as z:
 items=json.loads(z.read('manifest.json'))
 for item in items:
  rel=Path(item['path'])
  assert not rel.is_absolute() and '..' not in rel.parts and ':' not in item['path']
  assert not (a.output/rel).exists(), f'File already exists: {rel}'
  assert hashlib.sha256(z.read(item['path'])).hexdigest()==item['sha256']
 for item in items:
  dest=a.output/item['path']; dest.parent.mkdir(parents=True,exist_ok=True); dest.write_bytes(z.read(item['path']))
print(f'Restored {len(items)} files to {a.output}')
