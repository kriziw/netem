"""Admin CLI ownership: reproduce root provisioning for a non-root runtime."""
import errno
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import access_control as access


class CliOwnershipTests(unittest.TestCase):
    def adopt_test_owner(self, path, account):
        try:
            os.chown(path, account.pw_uid, account.pw_gid)
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EINVAL):
                self.skipTest('Host namespace cannot assign another UID; run privileged CI regression')
            raise

    def test_non_root_commands_leave_identity_unchanged(self):
        with patch.object(os, 'geteuid', return_value=1234), patch.object(os, 'setuid') as change:
            self.assertEqual(access.prepare_cli_database('/tmp/netem/access.sqlite3'), Path('/tmp/netem/access.sqlite3'))
            change.assert_not_called()

    def test_root_adopts_secret_owner_and_repairs_private_database_before_dropping_uid(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            secret = base / 'session.secret'
            secret.write_text('runtime identity')
            database = base / 'access.sqlite3'
            database.write_bytes(b'')
            original_stat = Path.stat
            def identity(path, *args, **kwargs):
                result = original_stat(path, *args, **kwargs)
                if path == secret:
                    fields = list(result)
                    fields[4:6] = [1234, 5678]
                    return os.stat_result(fields)
                return result
            original_fstat = os.fstat
            def root_file(fd):
                fields = list(original_fstat(fd))
                fields[4] = 0
                return os.stat_result(fields)
            actions = []
            with patch.object(os, 'geteuid', return_value=0), \
                    patch.object(Path, 'stat', identity), \
                    patch.object(os, 'fstat', root_file), \
                    patch('pwd.getpwuid') as account, \
                    patch.object(os, 'fchown', side_effect=lambda fd, uid, gid: actions.append(('owner', uid, gid))), \
                    patch.object(os, 'initgroups', side_effect=lambda user, gid: actions.append(('groups', user, gid))), \
                    patch.object(os, 'setgid', side_effect=lambda gid: actions.append(('gid', gid))), \
                    patch.object(os, 'setuid', side_effect=lambda uid: actions.append(('uid', uid))):
                account.return_value.pw_name = 'netem'
                access.prepare_cli_database(database)
            self.assertEqual(actions, [('owner', 1234, 5678), ('groups', 'netem', 5678), ('gid', 5678), ('uid', 1234)])
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.geteuid() == 0, 'Real ownership regression requires root')
    def test_root_commands_create_and_repair_database_for_runtime_user(self):
        try:
            account = pwd.getpwnam('nobody')
        except KeyError:
            self.skipTest('No unprivileged nobody account')
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            base.chmod(0o755)
            runtime = base / 'runtime'
            runtime.mkdir(mode=0o700)
            self.adopt_test_owner(runtime, account)
            secret = runtime / 'session.secret'
            secret.write_text('test runtime identity')
            secret.chmod(0o600)
            os.chown(secret, account.pw_uid, account.pw_gid)
            database = runtime / 'access.sqlite3'
            script = str(Path(access.__file__).absolute())
            def command(action, username=None, password=None):
                args = [sys.executable, script, action, '--database', str(database)]
                if username:
                    args.append(username)
                return subprocess.run(args, cwd='/tmp', input=password, text=True, capture_output=True, check=True)
            code = command('setup-code').stdout.strip()
            self.assertGreaterEqual(len(code), 40)
            self.assertEqual(database.stat().st_uid, account.pw_uid)
            self.assertEqual(database.stat().st_gid, account.pw_gid)
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)
            # Reproduce the released version's root-owned DB, then repair in place.
            os.chown(database, 0, 0)
            self.assertEqual(command('setup-code').stdout.strip(), code)
            self.assertEqual(database.stat().st_uid, account.pw_uid)
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)
            # All admin commands adopt the same identity, not just setup-code.
            command('create-admin', 'admin', 'initial-admin-password\ninitial-admin-password\n')
            command('reset-password', 'admin', 'replacement-password\nreplacement-password\n')
            store = access.AccessStore(database)
            self.assertIsNotNone(store.authenticate('admin', 'replacement-password'))
            self.assertIsNone(store.authenticate('admin', 'initial-admin-password'))
            self.assertEqual(database.stat().st_uid, account.pw_uid)
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.geteuid() == 0, 'Real ownership regression requires root')
    def test_root_repair_refuses_database_symlinks(self):
        account = pwd.getpwnam('nobody')
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.adopt_test_owner(base, account)
            victim = base / 'unrelated'
            victim.write_text('leave this file alone')
            target = base / 'access.sqlite3'
            target.symlink_to(victim)
            with self.assertRaises(OSError):
                access.prepare_cli_database(target)
            self.assertEqual(victim.stat().st_uid, 0)
            self.assertEqual(victim.read_text(), 'leave this file alone')
