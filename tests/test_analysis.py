from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from consumable_assistant.analysis_broker import selected_inputs
from consumable_assistant.analysis_sandbox import unpack


def request_archive(*, member='inputs/0.csv', checksum=None, extra=None):
    content = b'name,quantity\nbeaker,2\n'
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('program.py', 'print(2)')
        archive.writestr('manifest.json', json.dumps([{
            'path': '/' + member, 'filename': 'selected.csv',
            'sha256': checksum or hashlib.sha256(content).hexdigest(),
        }]))
        archive.writestr(member, content)
        if extra is not None:
            archive.writestr(extra, b'unselected')
    return stream.getvalue()


def test_broker_receives_only_selected_verified_attachment_bytes(tmp_path):
    code, attachments = selected_inputs(request_archive(), tmp_path)
    assert code == 'print(2)'
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment['filename'] == 'selected.csv'
    assert attachment['path'] == str(tmp_path / '0.csv')
    assert (tmp_path / '0.csv').read_bytes() == b'name,quantity\nbeaker,2\n'


@pytest.mark.parametrize('member', [
    '../consumables.db', 'inputs/../../environment',
    'var/lib/lab-management/consumables.db', 'inputs/0.py',
])
def test_broker_rejects_host_paths_and_nonattachment_members(tmp_path, member):
    with pytest.raises(ValueError, match='Invalid selected input path'):
        selected_inputs(request_archive(member=member), tmp_path)
    assert not list(tmp_path.iterdir())


def test_broker_rejects_changed_and_unselected_inputs(tmp_path):
    with pytest.raises(ValueError, match='checksum mismatch'):
        selected_inputs(request_archive(checksum='0' * 64), tmp_path)
    with pytest.raises(ValueError, match='Unexpected or repeated'):
        selected_inputs(request_archive(extra='inputs/1.csv'), tmp_path)


def test_analysis_output_rejects_symbolic_links():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('__analysis__.json', '{"status":"completed"}')
        entry = zipfile.ZipInfo('report.csv')
        entry.create_system = 3
        entry.external_attr = 0o120777 << 16
        archive.writestr(entry, '/etc/passwd')
    with pytest.raises(ValueError, match='普通文件'):
        unpack(stream.getvalue())
