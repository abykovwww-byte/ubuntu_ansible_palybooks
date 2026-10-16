"""Run the actual embedded receiving validator even on Windows."""
import pytest

from security_lab.compose_runtime import _serialize_handoff
from security_lab.contracts import Scope
from security_lab.handoff_files import HANDOFF_FILE_PROGRAM
from test_compose_runtime import packet, native_packet, scope


def validator(context):
    text, descriptor = _serialize_handoff(Scope.model_validate(scope()), context, run_id='owned')
    namespace = {}
    exec(compile(HANDOFF_FILE_PROGRAM, 'receiving-handoff-store', 'exec'), namespace)
    # _validate is pure; filesystem ownership/no-follow methods have separate
    # POSIX coverage. Do not mock/replace the receiver's actual validation code.
    store = object.__new__(namespace['HandoffStore'])
    store.run_id, store.descriptor = 'owned', descriptor
    return store, text.encode('utf-8')


@pytest.mark.parametrize('context', [packet, native_packet])
def test_receiving_artifact_accepts_the_explicit_packet_format(context):
    value = context()
    store, data = validator(value)
    assert store.descriptor['schema_version'] == 1
    assert store.descriptor['packet_schema_version'] == value['schema_version']
    store._validate(data)


def test_receiver_rejects_native_packet_mislabelled_as_facade():
    store, data = validator(native_packet())
    store.descriptor = {**store.descriptor, 'packet_schema_version':1}
    with pytest.raises(RuntimeError, match='schema or original scope binding'):
        store._validate(data)


@pytest.mark.parametrize('change', [{'packet_schema_version':3}, {'packet_schema_version':True},
                                   {'case_id':'foreign-case'}, {'generation':999}, {'scope_hash':'a'*64}])
def test_receiver_preserves_identity_and_version_rejection(change):
    store, data = validator(native_packet())
    store.descriptor = {**store.descriptor, **change}
    with pytest.raises(RuntimeError):
        store._validate(data)
