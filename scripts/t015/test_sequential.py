import subprocess
import sys
import unittest
from pathlib import Path

import run_five


class SequentialPlanTests(unittest.TestCase):
    def test_order_and_phases(self):
        calls = []
        result = run_five.run_sequential(lambda name, phases: calls.append((name, phases)) or 'finished')
        self.assertEqual(calls, [('software', (1, 2)), ('fund', (1,)), ('newsroom', (1,))])
        self.assertEqual(list(result), ['software', 'fund', 'newsroom'])

    def test_stop_on_provider_pause(self):
        calls = []
        run_five.run_sequential(lambda name, phases: calls.append(name) or 'provider_paused')
        self.assertEqual(calls, ['software'])

    def test_reject_non_strict_options_before_run(self):
        script = str(Path(run_five.__file__))
        for option, value in [('--grace-seconds', '1'), ('--stage-seconds', '1199'), ('--jobs', '2')]:
            result = subprocess.run([sys.executable, script, '--run', 'no-live-test', '--sequential-four', option, value],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('--sequential-four requires', result.stderr)


if __name__ == '__main__':
    unittest.main()
