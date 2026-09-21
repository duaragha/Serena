"""Fixed read-only security checks. Secret values never leave this module."""
import json
import re
import shlex
import subprocess
from collections import defaultdict
from pathlib import Path

from fleet.completion_gate import safe_test_argv

SECRET_PATTERN = r'AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36}|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----'
# `:!node_modules` only drops the root copy, so vendored and packaged-sidecar
# copies still reached the reviewer. These exclude recursively at any depth.
EXCLUDE_PATHSPECS = (
    ':(exclude,glob)**/node_modules/**',
    ':(exclude,glob)**/.venv/**',
    ':(exclude,glob)**/.git/**',
    ':(exclude,glob)**/site-packages/**',
)
# Trees that are not this run's source, checked again on the path so a pathspec
# that a Git version handles differently still cannot produce a false blocker.
VENDORED_DIRECTORIES = frozenset({'node_modules', '.venv', 'site-packages', 'vendor', 'third_party'})
GENERATED_DIRECTORIES = frozenset({'dist', 'build', '_internal', '__pycache__'})
TEST_DIRECTORIES = frozenset({'tests', 'test', '__tests__', 'testdata', 'fixtures'})
TEST_FILE_SUFFIXES = ('_test.py', '_test.go', '_test.js', '.spec.js', '.spec.ts',
                      '.test.js', '.test.ts')
PEM_HEADER = '-----BEGIN '
# A real key body is a long unbroken base64 run. An abbreviated example or a
# header compared as a string has none.
KEY_BODY = re.compile(r'[A-Za-z0-9+/]{40,}={0,2}')
# Key bytes stay on one physical line when the key is embedded in a source
# string, so the escaped separators a language or transport uses must be split
# exactly like real newlines before looking for the body.
ESCAPED_NEWLINE = re.compile(r'\\{1,2}r\\{1,2}n|\\{1,2}n|\\{1,2}r|%0[Aa]|&#10;')
# Positive evidence that matched material is a fixture rather than a credential.
# Deliberately narrow: no bare "example"/"sample", which any prose can contain.
SYNTHETIC_MARKERS = ('synthetic', 'not-real', 'not_real', 'notreal', 'placeholder',
                     'dummy', 'fake', 'fixture', 'redact', 'test-only', 'testonly',
                     'nonexistent', 'made-up', 'madeup')
ABSENCE_ASSERTIONS = ('not in', 'not_in', 'assertnotin', 'notcontain', 'tonotcontain')
PLACEHOLDER_ENDINGS = ('EXAMPLE',)
MARKER_WINDOW = 3
# Only first-party source and configuration can gate a run. Everything else is
# recorded with its reason so a human can escalate it deliberately.
GATING_CLASSIFICATION = 'first-party source'


def _is_test_path(parts):
    name = parts[-1] if parts else ''
    return (any(part in TEST_DIRECTORIES for part in parts[:-1])
            or name.startswith('test_') or name == 'conftest.py'
            or name.endswith(TEST_FILE_SUFFIXES))


def _body_candidates(lines, index):
    """Every representation in which key bytes can follow the matched header."""

    line = lines[index]
    tail = line.split(PEM_HEADER, 1)[1] if PEM_HEADER in line else line
    # An escaped separator keeps the whole key on the matched physical line; the
    # untouched tail also covers a body joined straight onto the header.
    return [*ESCAPED_NEWLINE.split(tail), tail, *lines[index + 1:index + 6]]


def _is_placeholder(value):
    """True for filler no real credential uses: one repeated run, or AWS's EXAMPLE."""

    body = value[4:] if value.upper().startswith('AKIA') else value.split('_', 1)[-1]
    return bool(body) and (len(set(body)) <= 2 or value.upper().endswith(PLACEHOLDER_ENDINGS))


def _asserted_absent(lines, value):
    """True when the file itself asserts this exact value does not survive."""

    return any(value in text and any(token in text.lower() for token in ABSENCE_ASSERTIONS)
               for text in lines)


def _marked_synthetic(lines, index):
    window = lines[max(0, index - MARKER_WINDOW):index + MARKER_WINDOW + 1]
    return any(marker in text.lower() for text in window for marker in SYNTHETIC_MARKERS)


def _inspect(path, numbers):
    """Examine the matched content itself: is material present, and is it a fixture?

    Read locally and never reported: the verdict leaves this module, the bytes
    do not. An unreadable or unparsable match keeps the stricter answer, so a
    path alone can never downgrade credential material.
    """

    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return True, False
    material = False
    explained = True
    for number in numbers:
        index = number - 1
        if not 0 <= index < len(lines):
            return True, False
        found = re.search(SECRET_PATTERN, lines[index])
        if found is None:
            # Grep matched something this module cannot re-read; stay strict.
            return True, False
        value = found.group(0)
        if PEM_HEADER in value:
            present = any(KEY_BODY.search(text) for text in _body_candidates(lines, index))
        else:
            # An access-key or token pattern carries its own value on the line.
            present = True
        if not present:
            continue
        material = True
        if not (_is_placeholder(value)
                or _asserted_absent(lines, value)
                or _marked_synthetic(lines, index)):
            explained = False
    return material, material and explained


def _classify(root, path, numbers):
    """Deterministic, value-free reason a match is or is not credential exposure.

    Content is inspected before any source-path downgrade. A test or
    documentation path is not on its own a reason to downgrade: unexplained
    credential material gates wherever it lives.
    """

    parts = [part for part in path.replace('\\', '/').split('/') if part]
    if any(part in VENDORED_DIRECTORIES for part in parts):
        return 'minor', 'dependency tree, not this run source'
    if any(part in GENERATED_DIRECTORIES for part in parts[:-1]):
        return 'minor', 'generated build output, not this run source'
    material, explained = _inspect(root / path, numbers)
    if not material:
        return 'minor', 'private-key header without key material'
    if explained and _is_test_path(parts):
        return 'minor', 'verified synthetic fixture'
    return 'blocker', GATING_CLASSIFICATION


def _grep_matches(stdout):
    """Group `path:line:` hits by path, discarding the matched text entirely."""

    matches = defaultdict(list)
    for row in stdout.splitlines():
        found = re.match(r'^(.*?):(\d+):', row)
        if found is None:
            continue
        matches[found.group(1)].append(int(found.group(2)))
    return matches


def security_pass(root, unit_ids):
    root = Path(root)
    findings, checks = [], []
    # --no-index includes new files; exclusions bound generated/dependency data.
    command = ['git', 'grep', '--no-index', '-I', '-n', '-E', SECRET_PATTERN, '--', '.',
               *EXCLUDE_PATHSPECS]
    argv = safe_test_argv(shlex.join(command), str(root))
    if argv:
        try:
            result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=30)
            checks.append({'check': 'secret-pattern grep', 'exit_code': result.returncode})
            if result.returncode == 0:
                for path, numbers in sorted(_grep_matches(result.stdout).items()):
                    severity, classification = _classify(root, path, numbers)
                    for unit in unit_ids:
                        findings.append({'unit_id': unit, 'severity': severity, 'category': 'security',
                            'summary': 'Possible credential or private key in repository',
                            'evidence': f'{path}: known secret pattern matched (value redacted); '
                                        f'classified as {classification}',
                            'file': path, 'classification': classification})
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks.append({'check': 'secret-pattern grep', 'unavailable': type(exc).__name__})
    else:
        checks.append({'check': 'secret-pattern grep', 'unavailable': 'allowlist refused'})
    # Offline npm audit uses only the local advisory cache and does not install.
    # An absent cache is explicitly unavailable, never reported as a clean audit.
    if (root / 'package-lock.json').is_file():
        argv = safe_test_argv('npm audit --offline --ignore-scripts --json', str(root))
        if argv:
            try:
                result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=30)
                report = json.loads(result.stdout)
                checks.append({'check': 'dependency audit', 'exit_code': result.returncode,
                               'unavailable': bool(report.get('error'))})
                for name, value in sorted(report.get('vulnerabilities', {}).items()):
                    severity = 'blocker' if value.get('severity') == 'critical' else 'major' if value.get('severity') == 'high' else 'minor'
                    for unit in unit_ids:
                        findings.append({'unit_id': unit, 'severity': severity, 'category': 'security',
                            'summary': f'Vulnerable dependency: {name}',
                            'evidence': f'package-lock.json: npm audit severity {value.get("severity")}'})
            except (OSError, subprocess.TimeoutExpired, ValueError):
                checks.append({'check': 'dependency audit', 'unavailable': True})
        else:
            checks.append({'check': 'dependency audit', 'unavailable': 'allowlist refused'})
    else:
        checks.append({'check': 'dependency audit', 'unavailable': 'no supported package-lock.json'})
    return {'findings': findings, 'checks': checks}


def merge_security_findings(output, result):
    from fleet.completion import extract_envelope
    payload, prose, error = extract_envelope(output)
    if not payload or error:
        return output
    for unit in payload.get('units', []):
        if isinstance(unit.get('findings'), list):
            for finding in result['findings']:
                if finding['unit_id'] == unit.get('id') and finding not in unit['findings']:
                    unit['findings'].append(finding)
            unit['security_checks'] = result['checks']
    return prose + '\n<serena-evidence>\n' + json.dumps(payload) + '\n</serena-evidence>'
