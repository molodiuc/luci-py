#!/usr/bin/env python
# Copyright 2015 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

"""Starts a local bot to connect to a local server."""

import glob
import os
import signal
import socket
import sys
import tempfile
import urllib


THIS_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_DIR = os.path.join(THIS_DIR, '..', '..', '..', 'client')
sys.path.insert(0, CLIENT_DIR)
from third_party.depot_tools import fix_encoding
from utils import file_path
from utils import subprocess42
sys.path.pop(0)


class LocalBot(object):
  """A local running Swarming bot.

  It creates its own temporary directory to download the zip and run tasks
  locally.
  """
  def __init__(self, swarming_server_url, redirect=True):
    self._tmpdir = tempfile.mkdtemp(prefix='swarming_bot')
    self._swarming_server_url = swarming_server_url
    self._proc = None
    self._logs = {}
    self._redirect = redirect

  def wipe_cache(self):
    """Blows away this bot's cache."""
    try:
      file_path.rmtree(os.path.join(self._tmpdir, 'isolated_cache'))
    except OSError:
      pass

  @property
  def bot_id(self):
    # TODO(maruel): Big assumption.
    return socket.getfqdn().split('.')[0]

  @property
  def log(self):
    """Returns the log output. Only set after calling stop()."""
    return '\n'.join(self._logs.itervalues()) if self._logs else None

  def start(self):
    """Starts the local Swarming bot."""
    assert not self._proc
    bot_zip = os.path.join(self._tmpdir, 'swarming_bot.zip')
    urllib.urlretrieve(self._swarming_server_url + '/bot_code', bot_zip)
    cmd = [sys.executable, bot_zip, 'start_slave']
    if self._redirect:
      logs = os.path.join(self._tmpdir, 'logs')
      if not os.path.isdir(logs):
        os.mkdir(logs)
      with open(os.path.join(logs, 'bot_stdout.log'), 'wb') as f:
        self._proc = subprocess42.Popen(
            cmd, cwd=self._tmpdir, stdout=f, stderr=f, detached=True)
    else:
      self._proc = subprocess42.Popen(cmd, cwd=self._tmpdir, detached=True)

  def stop(self, leak):
    """Stops the local Swarming bot. Returns the process exit code."""
    if not self._proc:
      return None
    if self._proc.poll() is None:
      try:
        self._proc.send_signal(signal.SIGTERM)
        # TODO(maruel): SIGKILL after N seconds.
        self._proc.wait()
      except OSError:
        pass
    exit_code = self._proc.returncode
    if self._tmpdir:
      for i in sorted(glob.glob(os.path.join(self._tmpdir, 'logs', '*.log'))):
        self._read_log(i)
      if not leak:
        try:
          file_path.rmtree(self._tmpdir)
        except OSError:
          print >> sys.stderr, 'Leaking %s' % self._tmpdir
      self._tmpdir = None
    self._proc = None
    return exit_code

  def poll(self):
    """Polls the process to know if it exited."""
    self._proc.poll()

  def wait(self, timeout=None):
    """Waits for the process to normally exit."""
    return self._proc.wait(timeout)

  def kill(self):
    """Kills the child forcibly."""
    if self._proc:
      self._proc.kill()

  def dump_log(self):
    """Prints dev_appserver log to stderr, works only if app is stopped."""
    print >> sys.stderr, '-' * 60
    print >> sys.stderr, 'swarming_bot log'
    print >> sys.stderr, '-' * 60
    if not self._logs:
      print >> sys.stderr, '<N/A>'
    else:
      for name, content in sorted(self._logs.iteritems()):
        sys.stderr.write(name + ':\n')
        for l in content.strip('\n').splitlines():
          sys.stderr.write('  %s\n' % l)
    print >> sys.stderr, '-' * 60

  def _read_log(self, path):
    try:
      with open(path, 'rb') as f:
        self._logs[os.path.basename(path)] = f.read()
    except (IOError, OSError):
      pass


def main():
  fix_encoding.fix_encoding()
  if len(sys.argv) != 2:
    print >> sys.stderr, 'Specify url to Swarming server'
    return 1
  bot = LocalBot(sys.argv[1], False)
  try:
    bot.start()
    bot.wait()
    bot.dump_log()
  except KeyboardInterrupt:
    print >> sys.stderr, '<Ctrl-C> received; stopping bot'
  finally:
    exit_code = bot.stop(False)
  return exit_code


if __name__ == '__main__':
  sys.exit(main())
