"""Package only the add-on source and repository metadata for local installation."""
from pathlib import Path
import re
import zipfile

root = Path(__file__).resolve().parents[1]
source = root / 'addons/lightnvr' if (root / 'addons/lightnvr').exists() else root / 'lightnvr'
version = re.search(r'^version:\s*["\']?([\w.+-]+)', (source / 'config.yaml').read_text(), re.MULTILINE).group(1)
target = root / 'scratch' / f'lightnvr-hass-addon-{version}.zip'
target.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    for name in ('repository.yaml', 'LICENSE'):
        archive.write(root / name, name)
    for path in sorted(source.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            archive.write(path, 'lightnvr/' + path.relative_to(source).as_posix())
    print(f'{target} ({len(archive.namelist())} files)')
