# Copyright 2015 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Defines AdbCommandsSafe, an exception safe version of AdbCommands."""

import cStringIO
import inspect
import logging
import socket
import subprocess
import time


from adb import adb_commands
from adb import common
from adb import usb_exceptions


_LOG = logging.getLogger('adb.cmd')
_LOG.setLevel(logging.ERROR)


### Public API.


# Make adb_commands_safe a drop-in replacement for adb_commands.
from adb.adb_commands import DeviceIsAvailable


def KillADB():
  """Stops the adb daemon.

  It's possible that adb daemon could be alive on the same host where python-adb
  is used. The host keeps the USB devices open so it's not possible for other
  processes to open it. Gently stop adb so this process can access the USB
  devices.

  adb's stability is less than stellar. Kill it with fire.
  """
  _LOG.info('KillADB()')
  while True:
    try:
      subprocess.check_output(['pgrep', 'adb'])
    except subprocess.CalledProcessError:
      return
    try:
      subprocess.call(
          ['adb', 'kill-server'],
          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError:
      pass
    subprocess.call(
        ['killall', '--exact', 'adb'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # Force thread scheduling to give a chance to the OS to clean out the
    # process.
    time.sleep(0.001)


class AdbCommandsSafe(object):
  """Wraps an AdbCommands to make it exception safe.

  The fact that exceptions can be thrown any time makes the client code really
  hard to write safely. Convert USBError* to None return value.

  Only contains the low level commands. High level operations are built upon the
  low level functionality provided by this class.
  """
  # - CommonUsbError means that device I/O failed, e.g. a write or a read call
  #   returned an error.
  # - USBError means that a bus I/O failed, e.g. the device path is not present
  #   anymore.
  _ERRORS = (usb_exceptions.CommonUsbError, common.libusb1.USBError)

  _SHELL_SUFFIX = ' ;echo -e "\n$?"'

  def __init__(
      self, port_path, handle, banner, rsa_keys, on_error,
      default_timeout_ms=10000, auth_timeout_ms=10000, lost_timeout_ms=10000):
    """Constructs an AdbCommandsSafe.

    Arguments:
    - port_path: str addressing the device on the USB bus, e.g. '1/2'.
    - handle: common.UsbHandle or None.
    - banner: How the app present itself to the device. This affects
          authentication so it is better to use an hardcoded constant.
    - rsa_keys: list of AuthSigner.
    - on_error: callback to call in case of error.
    - default_timeout_ms: Timeout for adbd to reply to a command.
    - auth_timeout_ms: Timeout for the user to accept the dialog.
    - lost_timeout_ms: Duration to wait for the device to come back alive when
          either adbd or the phone is rebooting.
    """
    assert isinstance(auth_timeout_ms, int), auth_timeout_ms
    assert isinstance(default_timeout_ms, int), default_timeout_ms
    assert isinstance(lost_timeout_ms, int), lost_timeout_ms
    assert isinstance(banner, str), banner
    assert on_error is None or callable(on_error), on_error
    assert all(isinstance(p, int) for p in port_path), port_path
    assert handle is None or isinstance(handle, common.UsbHandle), handle
    assert all('\n' not in r.GetPublicKey() for r in rsa_keys), rsa_keys
    _LOG.debug(
        'AdbCommandsSafe(%s, %s, %s, %s, %s, %s, %s, %s)',
        port_path, handle, banner, rsa_keys, on_error, default_timeout_ms,
        auth_timeout_ms, lost_timeout_ms)
    # Immutable.
    self._auth_timeout_ms = auth_timeout_ms
    self._default_timeout_ms = default_timeout_ms
    self._banner = banner or socket.gethostname()
    self._lost_timeout_ms = lost_timeout_ms
    self._on_error = on_error
    self._rsa_keys = rsa_keys
    self._sleep = 0.1
    self._tries = int(round((self._lost_timeout_ms / 1000. + 5) / self._sleep))

    # State.
    self._adb_cmd = None
    self._serial = None
    self._handle = handle
    self._port_path = '/'.join(map(str, port_path))

  @classmethod
  def ConnectDevice(cls, port_path, **kwargs):
    """Return a AdbCommandsSafe for a USB device referenced by the port path.

    Arguments:
    - port_path: str in form '1/2' to refer to a connected but unopened USB
          device.
    - The rest are the same as __init__().
    """
    obj = cls(port_path=port_path, handle=None, **kwargs)
    obj._WaitUntilFound(use_serial=False)
    if obj._OpenHandle():
      obj._Connect(use_serial=False)
    return obj

  @classmethod
  def Connect(cls, handle, **kwargs):
    """Return a AdbCommandsSafe for a USB device referenced by a handle.

    Arguments:
    - handle: an opened or unopened common.UsbHandle.
    - The rest are the same as __init__().

    Returns:
      AdbCommandsSafe.
    """
    obj = cls(port_path=handle.port_path, handle=handle, **kwargs)
    if not handle.is_open:
      obj._OpenHandle()
    if obj._handle:
      obj._Connect(use_serial=False)
    return obj

  @property
  def is_valid(self):
    return bool(self._adb_cmd)

  @property
  def port_path(self):
    """Returns the USB port path as a str."""
    return self._port_path

  @property
  def public_keys(self):
    """Returns the list of the public keys."""
    return [r.GetPublicKey() for r in self._rsa_keys]

  @property
  def serial(self):
    return self._serial or self._port_path

  def Close(self):
    if self._adb_cmd:
      self._adb_cmd.Close()
      self._adb_cmd = None
      self._handle = None
    elif self._handle:
      self._handle.Close()
      self._handle = None

  def List(self, destdir):
    """List a directory on the device."""
    assert destdir.startswith('/'), destdir
    if self._adb_cmd:
      for _ in self._Loop():
        try:
          return self._adb_cmd.List(destdir)
        except usb_exceptions.AdbCommandFailureException:
          break
        except self._ERRORS as e:
          self._Reset('(%s): %s', destdir, e)
    return None

  def Stat(self, dest):
    """Stats a file/dir on the device. It's likely faster than shell().

    Returns:
      tuple(mode, size, mtime)
    """
    assert dest.startswith('/'), dest
    if self._adb_cmd:
      for _ in self._Loop():
        try:
          return self._adb_cmd.Stat(dest)
        except usb_exceptions.AdbCommandFailureException:
          break
        except self._ERRORS as e:
          self._Reset('(%s): %s', dest, e)
    return None, None, None

  def Pull(self, remotefile, dest):
    """Retrieves a file from the device to dest on the host.

    Returns True on success.
    """
    assert remotefile.startswith('/'), remotefile
    if self._adb_cmd:
      for _ in self._Loop():
        try:
          self._adb_cmd.Pull(remotefile, dest)
          return True
        except usb_exceptions.AdbCommandFailureException:
          break
        except self._ERRORS as e:
          self._Reset('(%s, %s): %s', remotefile, dest, e)
    return False

  def PullContent(self, remotefile):
    """Reads a file from the device.

    Returns content on success as str, None on failure.
    """
    assert remotefile.startswith('/'), remotefile
    if self._adb_cmd:
      # TODO(maruel): Distinction between file is not present and I/O error.
      for _ in self._Loop():
        try:
          return self._adb_cmd.Pull(remotefile, None)
        except usb_exceptions.AdbCommandFailureException:
          break
        except self._ERRORS as e:
          self._Reset('(%s): %s', remotefile, e)
    return None

  def Push(self, localfile, dest, mtime='0'):
    """Pushes a local file to dest on the device.

    Returns True on success.
    """
    assert dest.startswith('/'), dest
    if self._adb_cmd:
      for _ in self._Loop():
        try:
          self._adb_cmd.Push(localfile, dest, mtime)
          return True
        except usb_exceptions.AdbCommandFailureException:
          break
        except self._ERRORS as e:
          self._Reset('(%s, %s): %s', localfile, dest, e)
    return False

  def PushContent(self, content, dest, mtime='0'):
    """Writes content to dest on the device.

    Returns True on success.
    """
    assert dest.startswith('/'), dest
    if self._adb_cmd:
      for _ in self._Loop():
        try:
          self._adb_cmd.Push(cStringIO.StringIO(content), dest, mtime)
          return True
        except usb_exceptions.AdbCommandFailureException:
          break
        except self._ERRORS as e:
          self._Reset('(%s, %s): %s', dest, content, e)
    return False

  def Reboot(self):
    """Reboot the device. Doesn't wait for it to be rebooted"""
    if self._adb_cmd:
      for _ in self._Loop():
        try:
          # Use 'bootloader' to switch to fastboot.
          out = self._adb_cmd.Reboot()
          _LOG.info('%s.Reboot(): %s', self.port_path, out)
          return True
        except usb_exceptions.AdbCommandFailureException:
          break
        except self._ERRORS as e:
          self._Reset('(): %s', e)
    return False

  def Remount(self):
    """Remount / as read-write."""
    if self._adb_cmd:
      for _ in self._Loop():
        try:
          out = self._adb_cmd.Remount()
          # TODO(maruel): Wait for the remount operation to be completed.
          _LOG.info('%s.Remount(): %s', self.port_path, out)
          return True
        except usb_exceptions.AdbCommandFailureException:
          break
        except self._ERRORS as e:
          self._Reset('(): %s', e)
    return False

  def Shell(self, cmd):
    """Runs a command on an Android device while swallowing exceptions.

    Traps all kinds of USB errors so callers do not have to handle this.

    Returns:
      tuple(stdout, exit_code)
      - stdout is as unicode if it ran, None if an USB error occurred.
      - exit_code is set if ran.
    """
    if self._adb_cmd:
      for _ in self._Loop():
        try:
          return self.ShellRaw(cmd)
        except self._ERRORS as e:
          self._Reset('(%s): %s', cmd, e)
    return None, None

  def IsShellOk(self, cmd):
    """Returns True if the shell command can be sent."""
    if isinstance(cmd, unicode):
      cmd = cmd.encode('utf-8')
    assert isinstance(cmd, str), cmd
    if not self._adb_cmd:
      return True
    cmd_size = len(cmd + self._SHELL_SUFFIX)
    pkt_size = self._adb_cmd.conn.max_packet_size - len('shell:')
    # Has to keep one byte for trailing nul byte.
    return cmd_size < pkt_size

  def ShellRaw(self, cmd):
    """Runs a command on an Android device.

    It is expected that the user quote cmd properly.

    It fails if cmd is too long.

    Returns:
      tuple(stdout, exit_code)
      - stdout is as unicode if it ran, None if an USB error occurred.
      - exit_code is set if ran.
    """
    if isinstance(cmd, unicode):
      cmd = cmd.encode('utf-8')
    assert isinstance(cmd, str), cmd
    if not self._adb_cmd:
      return None, None
    # The adb protocol doesn't return the exit code, so embed it inside the
    # command.
    assert self.IsShellOk(cmd), 'Command is too long: %r' % cmd
    out = self._adb_cmd.Shell(cmd + self._SHELL_SUFFIX).decode(
        'utf-8', 'replace')
    # Protect against & or other bash conditional execution that wouldn't make
    # the 'echo $?' command to run.
    if not out:
      return out, None
    # adb shell uses CRLF EOL. Only God Knows Why.
    out = out.replace('\r\n', '\n')
    # TODO(maruel): Remove and handle if this is ever trapped.
    assert out[-1] == '\n', out
    # Strip the last line to extract the exit code.
    parts = out[:-1].rsplit('\n', 1)
    return parts[0], int(parts[1])

  def Root(self):
    """If adbd on the device is not root, ask it to restart as root.

    This causes the USB device to disapear momentarily, which causes a big mess,
    as we cannot communicate with it for a moment. So try to be clever and
    reenumerate the device until the device is back, then reinitialize the
    communication, all synchronously.
    """
    if self._adb_cmd and self._Root():
      # There's no need to loop initially too fast. Restarting the adbd always
      # take 'some' amount of time. In practice, this can take a good 1 second.
      time.sleep(0.1)
      i = 0
      for i in self._Loop():
        # We need to reconnect so we can assert the connection to adbd is to the
        # right process, not the old one but the new one.
        self._Reconnect(True)
        if self.IsRoot():
          return True
      _LOG.error('%s.Root(): Failed to id after %d tries', self.port_path, i+1)
    return False

  def Unroot(self):
    """If adbd on the device is root, ask it to restart as user."""
    if self._adb_cmd and self._Unroot():
      # There's no need to loop initially too fast. Restarting the adbd always
      # take 'some' amount of time. In practice, this can take a good 5 seconds.
      time.sleep(0.1)
      i = 0
      for i in self._Loop():
        # We need to reconnect so we can assert the connection to adbd is to the
        # right process, not the old one but the new one.
        self._Reconnect(True)
        if self.IsRoot() is False:
          return True
      _LOG.error(
          '%s.Unroot(): Failed to id after %d tries', self.port_path, i+1)
    return False

  def IsRoot(self):
    """Returns True if adbd is running as root.

    Returns None if it can't give a meaningful answer.

    Technically speaking this function is "high level" but is needed because
    reset_adbd_as_*() calls are asynchronous, so there is a race condition while
    adbd triggers the internal restart and its socket waiting for new
    connections; the previous (non-switched) server may accept connection while
    it is shutting down so it is important to repeatedly query until connections
    go to the new restarted adbd process.
    """
    out, exit_code = self.Shell('id')
    if exit_code != 0 or not out:
      return None
    return out.startswith('uid=0(root)')

  def _Root(self):
    """Upgrades adbd from shell user context (uid 2000) to root."""
    i = 0
    for i in self._Loop():
      try:
        out = self._adb_cmd.Root()
      except self._ERRORS as e:
        self._Reset('(): %s', e, use_serial=True)
        continue

      _LOG.info('%s._Root(): %r', self.port_path, out)
      # Hardcoded strings in platform_system_core/adb/services.cpp
      if out == 'adbd is already running as root\n':
        return True
      if out == 'adbd cannot run as root in production builds\n':
        _LOG.error('%s._Root(): %r', self.port_path, out)
        return False
      if out == 'restarting adbd as root\n':
        return True
      assert False, repr(out)
    _LOG.error('%s._Root(): Failed after %d tries', self.port_path, i+1)
    return False

  def _Unroot(self):
    """Reduces adbd from root to shell user context (uid 2000).

    Doing so has the effect of having the device switch USB port. As such, the
    device has to be found back by the serial number, not by self.port_path
    """
    assert self._serial
    i = 0
    for i in self._Loop():
      try:
        out = self._adb_cmd.Unroot()
      except self._ERRORS as e:
        self._Reset('(): %s', e, use_serial=True)
        continue

      _LOG.info('%s.Unroot(): %r', self.port_path, out)
      # Hardcoded strings in platform_system_core/adb/services.cpp
      if out in ('adbd not running as root\n', 'restarting adbd as non root\n'):
        return True
      assert False, repr(out)
    _LOG.error('%s._Unroot(): Failed after %d tries', self.port_path, i+1)
    return False

  def _Find(self, use_serial):
    """Initializes self._handle from self.port_path.

    The handle is left unopened.
    """
    assert not self._handle
    assert not self._adb_cmd
    # TODO(maruel): Add support for TCP/IP communication.
    _LOG.info('%s._Find(%s) %s', self.port_path, use_serial, self._serial)
    try:
      if use_serial:
        assert self._serial
        self._handle = common.UsbHandle.Find(
            adb_commands.DeviceIsAvailable, serial=self._serial,
            timeout_ms=self._default_timeout_ms)
        # Update the new found port path.
        self._port_path = self._handle.port_path_str
      else:
        self._handle = common.UsbHandle.Find(
            adb_commands.DeviceIsAvailable, port_path=self.port_path,
            timeout_ms=self._default_timeout_ms)
    except (common.usb1.USBError, usb_exceptions.DeviceNotFoundError):
      pass

  def _WaitUntilFound(self, use_serial):
    """Loops until the device is found on the USB bus.

    The handle is left unopened.

    This function should normally be called when either adbd or the phone is
    rebooting.
    """
    assert not self._handle
    _LOG.debug('%s._WaitUntilFound(%s)', self.port_path, use_serial)
    i = 0
    for i in self._Loop():
      try:
        self._Find(use_serial=use_serial)
        return
      except usb_exceptions.DeviceNotFoundError as e:
        _LOG.info(
            '%s._WaitUntilFound(): Retrying _Find() due to %s',
            self.port_path, e)
    _LOG.warning(
        '%s._WaitUntilFound() gave up after %d tries', self.port_path, i+1)

  def _OpenHandle(self):
    """Opens the unopened self._handle."""
    #_LOG.debug('%s._OpenHandle()', self.port_path)
    if self._handle:
      assert not self._handle.is_open
      i = 0
      for i in self._Loop():
        try:
          # If this succeeds, this initializes self._handle._handle, which is a
          # usb1.USBDeviceHandle.
          self._handle.Open()
          break
        except common.usb1.USBErrorNoDevice as e:
          _LOG.warning(
              '%s._OpenHandle(): USBErrorNoDevice: %s', self.port_path, e)
          # Do not kill adb, it just means the USB host is likely resetting and
          # the device is temporarily unavailable. We can't use
          # handle.serial_number since this communicates with the device.
        except common.usb1.USBErrorBusy as e:
          _LOG.warning('%s._OpenHandle(): USBErrorBusy: %s', self.port_path, e)
          KillADB()
        except common.usb1.USBErrorAccess as e:
          # Do not try to use serial_number, since we can't even access the
          # port.
          _LOG.warning(
              '%s._OpenHandle(): Try rebooting the host: %s', self.port_path, e)
          break
      else:
        _LOG.error(
            '%s._OpenHandle(): Failed after %d tries', self.port_path, i+1)
        self.Close()
    return bool(self._handle)

  def _Connect(self, use_serial):
    """Initializes self._adb_cmd from the opened self._handle."""
    assert not self._adb_cmd
    _LOG.debug('%s._Connect(%s)', self.port_path, use_serial)
    if self._handle:
      assert self._handle.is_open
      for _ in self._Loop():
        # On the first access with an open handle, try to set self._serial to
        # the serial number of the device. This means communicating to the USB
        # device, so it may throw.
        if not self._handle:
          # It may happen on a retry.
          self._WaitUntilFound(use_serial=use_serial)
          self._OpenHandle()
          if not self._handle:
            break

        if not self._serial or use_serial:
          try:
            # The serial number is attached to common.UsbHandle, no
            # adb_commands.AdbCommands.
            self._serial = self._handle.serial_number
          except self._ERRORS as e:
            self.Close()
            continue

        assert self._handle
        assert not self._adb_cmd
        try:
          # TODO(maruel): A better fix would be to change python-adb to continue
          # the authentication dance from where it stopped. This is left as a
          # follow up.
          self._adb_cmd = adb_commands.AdbCommands.Connect(
              self._handle, banner=self._banner, rsa_keys=self._rsa_keys,
              auth_timeout_ms=self._auth_timeout_ms)
          break
        except usb_exceptions.DeviceAuthError as e:
          _LOG.warning('AUTH FAILURE: %s: %s', self.port_path, e)
        except usb_exceptions.LibusbWrappingError as e:
          _LOG.warning('I/O FAILURE: %s: %s', self.port_path, e)
        finally:
          # Do not leak the USB handle when we can't talk to the device.
          if not self._adb_cmd:
            self.Close()

    if not self._adb_cmd and self._handle:
      _LOG.error('Unexpected close')
      self.Close()
    return bool(self._adb_cmd)

  def _Loop(self):
    """Yields a loop until it's too late."""
    start = time.time()
    for i in xrange(self._tries):
      if ((time.time() - start) * 1000) >= self._lost_timeout_ms:
        return
      yield i
      if ((time.time() - start) * 1000) >= self._lost_timeout_ms:
        return
      time.sleep(self._sleep)

  def _Reset(self, fmt, *args, **kwargs):
    """When a self._ERRORS occurred, try to reset the device."""
    items = [self.port_path, inspect.stack()[1][3]]
    items.extend(args)
    msg = ('%s.%s' + fmt) % tuple(items)
    _LOG.error(msg)
    if self._on_error:
      self._on_error(msg)

    # Reset the adbd and USB connections with a new connection.
    self._Reconnect(kwargs.get('use_serial', False))
    assert self._adb_cmd
    return msg

  def _Reconnect(self, use_serial):
    """Disconnects and reconnect.

    Arguments:
    - use_serial: If True, search the device by the serial number instead of the
        port number. This is necessary when downgrading adbd from root to user
        context.
    """
    self.Close()
    self._WaitUntilFound(use_serial=use_serial)
    if self._OpenHandle():
      self._Connect(use_serial=use_serial)

  def __repr__(self):
    return '<Device %s %s>' % (
        self.port_path, self.serial if self.is_valid else '(broken)')
