from homebrew_harness.runtime.limits import current_limits
"""Test selectors are data for a fixed launcher, never extra shell arguments."""

import json
import re
from pathlib import PurePosixPath, Path


def validate_selection(selection, framework, roots=("tests",)):
    if selection is None:
        return None
    if framework not in {"unittest", "pytest"}:
        raise ValueError("This recipe has no test selection adapter.")
    if not isinstance(selection, dict) or set(selection) - {"targets", "filter"}:
        raise ValueError("Test selection accepts targets and an optional pytest filter.")
    targets = selection.get("targets")
    if not isinstance(targets, list) or not targets or (current_limits().test_selection_targets is not None and len(targets) > current_limits().test_selection_targets):
        raise ValueError("Select 1-100 test targets; omit selection for the full suite.")
    for target in targets:
        if not isinstance(target, str) or len(target) > 1000:
            raise ValueError("Test targets must be bounded strings.")
        if framework == "unittest":
            if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", target):
                raise ValueError("unittest targets use module, class or method names.")
            if not any(target == root.replace('/', '.') or target.startswith(root.replace('/', '.') + '.') for root in roots):
                raise ValueError("Test target is outside the registered test roots.")
        else:
            path = target.split("::", 1)[0]
            if not path or '\\' in path or '\x00' in target or path.startswith('-'):
                raise ValueError("Invalid pytest target.")
            parsed = PurePosixPath(path)
            if parsed.is_absolute() or '..' in parsed.parts or not any(path == root or path.startswith(root + '/') for root in roots):
                raise ValueError("Test target is outside the registered test roots.")
    expression = selection.get('filter')
    if expression is not None and (framework != 'pytest' or not isinstance(expression, str) or not expression.strip() or len(expression) > current_limits().test_selection_chars or '\x00' in expression):
        raise ValueError("Only pytest supports a nonempty bounded selection filter.")
    return {'targets': list(dict.fromkeys(targets)), **({'filter': expression} if expression is not None else {})}


# The launcher is embedded in the host recipe and therefore included in its
# executable fingerprint. The environment payload carries only validated data.
LAUNCHER = r'''import json, os, sys, unittest
from pathlib import Path
project = Path.cwd()
sys.path.insert(0, str(project))
if (project / 'src').is_dir():
    sys.path.insert(0, str(project / 'src'))
request = json.loads(os.environ['HARNESS_TEST_SELECTION'])
selection = request['selection']
roots = request['roots']
if request['framework'] == 'pytest':
    import pytest
    targets = selection['targets'] if selection else roots
    args = list(targets)
    if selection and selection.get('filter'):
        args += ['-k', selection['filter']]
    sys.exit(pytest.main(args))
loader = unittest.TestLoader()
if selection:
    suite = loader.loadTestsFromNames(selection['targets'])
else:
    suite = unittest.TestSuite()
    for root in roots:
        top = str(project) if (project / root / '__init__.py').is_file() else None
        suite.addTests(loader.discover(root, top_level_dir=top))
result = unittest.TextTestRunner(verbosity=1).run(suite)
if result.testsRun == 0:
    print('No tests were collected.', file=sys.stderr)
    sys.exit(3)
sys.exit(0 if result.wasSuccessful() else 1)
'''


def selection_environment(framework, roots, selection):
    return {'HARNESS_TEST_SELECTION': json.dumps({'framework': framework, 'roots': roots, 'selection': selection})} if framework else {}


def validate_target_paths(root, framework, selection):
    if selection is None:
        return
    from homebrew_harness.security.safe_files import SafeFile
    for target in selection['targets']:
        if framework == 'pytest':
            candidate = target.split('::', 1)[0]
        else:
            components = target.split('.')
            candidate = None
            for end in range(len(components), 0, -1):
                possible = '/'.join(components[:end])
                if (root / (possible + '.py')).is_file():
                    candidate = possible + '.py'
                    break
                if (root / possible / '__init__.py').is_file():
                    candidate = possible + '/__init__.py'
                    break
            if candidate is None:
                continue  # The framework reports the missing module as a test error.
        with SafeFile(root, candidate) as source:
            source.verify_binding()
