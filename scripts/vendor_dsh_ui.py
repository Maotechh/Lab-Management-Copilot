"""Copy the pinned DSH web design foundation without its privileged app shell."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = 'fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159'


def vendor(source: Path) -> None:
    subprocess.run(['git', 'diff', '--exit-code', UPSTREAM, '--',
                    'packages/client/ui-theme/src/styles',
                    'packages/client/ui-primitives/src/icons/artwork.ts',
                    'packages/client/ui-primitives/src/markdown', 'LICENSE'],
                   cwd=source, check=True, capture_output=True)
    target = ROOT / 'consumable_assistant/static/dsh'
    target.mkdir(exist_ok=True)
    theme = source / 'packages/client/ui-theme/src/styles'
    files = {name: theme / name for name in (
        'base.css', 'design-platform.css', 'gradient-shadow-text.css',
        'corner-shape.css', 'scrollbar.css',
    )}
    files['LICENSE'] = source / 'LICENSE'
    for name, path in files.items():
        shutil.copyfile(path, target / name)
    primitives = source / 'packages/client/ui-primitives/src'
    markdown = (primitives / 'markdown/MarkdownText.module.css').read_text()
    # This sheet uses local class names only; prefix them for the plain DOM adapter.
    markdown = re.sub(r':global\(([^)]+)\)', r'\1', markdown)
    markdown = re.sub(r'\.([A-Za-z][\w-]*)', r'.dsh-md-\1', markdown)
    code_style = (primitives / 'markdown/CodeBlock.module.css').read_text()
    code_style = re.sub(r':global\(([^)]+)\)', r'\1', code_style)
    code_style = re.sub(r'\.([A-Za-z][\w-]*)', r'.dsh-code-\1', code_style)
    (target / 'markdown.css').write_text(markdown + '\n' + code_style)
    artwork = (primitives / 'icons/artwork.ts').read_text()
    glyphs = ('new-chat-outline', 'search-outline', 'panel-left-outline',
              'settings-outline', 'plus-outline', 'send-outline',
              'chevron-down-outline', 'chevron-right-outline', 'close-outline',
              'copy-outline', 'check-outline', 'folder-open-outline',
              'layers-outline', 'user-outline')
    symbols = []
    for name in glyphs:
        match = re.search(r"  '" + name + r"':.*?svg: strokeWidth => `([^`]+)`", artwork)
        if not match:
            raise ValueError(f'Missing DSH glyph: {name}')
        svg = match[1].replace('${strokeWidth}', '1')
        svg = re.sub(r'<svg\s+xmlns="[^"]+"', f'<symbol id="{name}"', svg)
        symbols.append(svg.replace('</svg>', '</symbol>'))
    (target / 'icons.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg">' + ''.join(symbols) + '</svg>\n')
    parser = ROOT / 'frontend/vendor/dsh'
    parser.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(primitives / 'markdown/cjkFriendlyStrong.ts', parser / 'cjkFriendlyStrong.ts')
    records = {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in [*files.values(), primitives / 'icons/artwork.ts',
                            primitives / 'markdown/MarkdownText.module.css',
                            primitives / 'markdown/cjkFriendlyStrong.ts', primitives / 'markdown/CodeBlock.module.css']}
    (target / 'source.json').write_text(json.dumps({
        'repository': 'https://github.com/deepseek-ai/deepseek-harness',
        'commit': UPSTREAM, 'files': records,
        'adaptation': 'Markdown classes prefixed; selected original icon geometry exported as SVG symbols.',
    }, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    vendor(parser.parse_args().source.resolve())
