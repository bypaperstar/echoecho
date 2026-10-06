"""Exercise bundle replacement failures without launching an app or touching /Applications."""
import os
from pathlib import Path
import subprocess

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize('failure', ['copy', 'signature', 'replacement', ''])
def test_install_preserves_working_app_on_failure_and_can_leave_update_closed(tmp_path, failure):
    applications = tmp_path / 'Applications'
    target = applications / 'echoecho.app'
    target.mkdir(parents=True)
    (target / 'version').write_text('old')
    app_dir = tmp_path / 'app'
    bundle = app_dir / 'dist/echoecho-darwin-arm64/echoecho.app'
    bundle.mkdir(parents=True)
    (bundle / 'version').write_text('new')
    original = REPO / 'scripts/echoechoctl.sh'
    source = original.read_text()
    function = source[source.index('cmd_install_app() {'):source.index('\ncmd_start_app() {')]
    function = function.replace('"/Applications/echoecho.app"', '"$TEST_APPLICATIONS/echoecho.app"')
    function = function.replace('[ -w "/Applications" ]', '[ -w "$TEST_APPLICATIONS" ]')
    harness = tmp_path / 'install-test.sh'
    harness.write_text('''set -euo pipefail
source "$1" version >/dev/null
APP_DIR="$TEST_APP_DIR"
cmd_build_app() { return 0; }
cmd_stop_daemon() { touch "$TEST_APPLICATIONS/stopped"; }
pkill() { return 0; }
stop_dev_apps() { return 0; }
app_pid() { return 0; }
ditto() { [ "$TEST_FAILURE" != copy ] || return 1; cp -R "$1" "$2"; }
codesign() { [ "$TEST_FAILURE" != signature ]; }
open() { touch "$TEST_APPLICATIONS/opened"; }
mv() {
  if [ "$TEST_FAILURE" = replacement ]; then
    case "$1" in */.echoecho-install.*/echoecho.app) return 1 ;; esac
  fi
  command mv "$@"
}
''' + function + '\nECHOECHO_LEAVE_CLOSED=1 cmd_install_app\n')
    result = subprocess.run(['bash', str(harness), str(original)], cwd=REPO,
        env={**os.environ, 'TEST_APPLICATIONS': str(applications), 'TEST_APP_DIR': str(app_dir),
             'TEST_FAILURE': failure, 'ECHOECHO_STATE_DIR': str(tmp_path / 'state')},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == (1 if failure else 0), result.stderr
    assert (target / 'version').read_text() == ('old' if failure else 'new')
    assert not (applications / 'opened').exists()
    assert (applications / 'stopped').exists() == (not failure)
    assert not list(applications.glob('.echoecho-install.*'))
