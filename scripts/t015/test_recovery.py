import unittest
from recovery import retry_key

class RecoveryTest(unittest.TestCase):
    def test_clarification_generations_and_bounds(self):
        turn = {'id':'turn', 'role':'role', 'purpose':'clarification.resume', 'message_id':'answer',
                'status':'failed', 'generation':1, 'failure':{'generation':1, 'type':'model.failed', 'transient':True}}
        def select(attempted=set(), counts={}, scope={'answer'}):
            return retry_key(turn, 'assignment', scope, set(), attempted, counts, 120)
        key = ('role', 'clarification.resume', 'answer')
        self.assertEqual(select(), key)
        self.assertIsNone(select(scope=set()))
        self.assertIsNone(select(attempted={('turn', 1)}))
        turn['generation'] = 2
        self.assertIsNone(select(attempted={('turn', 1)}))  # stale failure event
        turn['failure']['generation'] = 2
        self.assertEqual(select(attempted={('turn', 1)}, counts={key:1}), key)
        self.assertIsNone(select(counts={key:2}))
        turn['failure']['transient'] = False
        self.assertIsNone(select())

if __name__ == '__main__':
    unittest.main()
