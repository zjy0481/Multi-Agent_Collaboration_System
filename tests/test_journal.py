import sqlite3

import pytest

from collaboration_runtime.journal import ExecutionJournal


def test_checkpoint_generation_and_pending():
    db = sqlite3.connect(':memory:')
    journal = ExecutionJournal(db)
    with db:
        journal.create('turn', [{'role': 'user', 'content': '你好'}])
        journal.queue_input('turn', '补充')
        assert journal.advance('turn', 1, 1, 'tools', [{'message': '继续'}])
        assert journal.checkpoint('turn')['pending'] == ['补充']
        assert journal.next_generation('turn') == 2
        assert not journal.advance('turn', 1, 2, 'model', [])
        assert journal.drain_inputs('turn', 1) == []
        assert journal.drain_inputs('turn', 2) == ['补充']
        assert journal.checkpoint('turn')['pending'] == []
        assert journal.current('turn', 2)
    with pytest.raises(KeyError):
        journal.next_generation('missing')


def test_receipt_preserved_and_generation_guarded():
    db = sqlite3.connect(':memory:')
    j = ExecutionJournal(db)
    with db:
        j.create('t', [])
        assert j.start_tool('t', 1, 1, 0, 'provider-id', 'ask_human', '{"问题":"好"}')
        assert not j.start_tool('t', 1, 1, 0, 'different', 'ask_human', '{"问题":"好"}')
        assert j.next_generation('t') == 2
        assert not j.complete_tool('t', 1, 1, 0, {'old': True})
        assert j.receipt('t', 1, 0)['state'] == 'started'
        assert j.start_tool('t', 2, 1, 0, 'provider-id', 'ask_human', '{"问题":"好"}')
        assert j.complete_tool('t', 2, 1, 0, {'answer': '完成'})
        assert not j.start_tool('t', 2, 1, 0, 'provider-id', 'ask_human', '{"问题":"好"}')
        assert not j.complete_tool('t', 2, 1, 0, {'answer': '修改'})
        assert j.receipt('t', 1, 0) == {'state': 'completed', 'generation': 2, 'call_id': 'provider-id',
                                        'name': 'ask_human', 'arguments': '{"问题":"好"}', 'result': {'answer': '完成'}}


def test_rollback_is_owned_by_caller():
    db = sqlite3.connect(':memory:')
    j = ExecutionJournal(db)
    db.commit()
    with pytest.raises(RuntimeError):
        with db:
            j.create('rolled-back', [])
            raise RuntimeError('cancel')
    assert j.checkpoint('rolled-back') is None
