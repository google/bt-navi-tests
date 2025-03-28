#  Copyright 2025 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""Constants commonly used in Bluetooth tests."""

import enum

from typing_extensions import override


class ShortReprEnum(enum.Enum):
  """Enum base having a repr string same as enum names.

  This class is useful when tests need a sequence of enums as a part of its test
  name.
  """

  @override
  def __repr__(self) -> str:
    return self.name


@enum.unique
class Direction(enum.IntEnum):
  INCOMING = enum.auto()
  OUTGOING = enum.auto()


@enum.unique
class TestRole(enum.IntEnum):
  DUT = enum.auto()
  REF = enum.auto()


@enum.unique
class HciRole(enum.IntEnum):
  CENTRAL = 0
  PERIPHERAL = 1


class UsbHidKeyCode(enum.IntEnum):
  """See HID Usage Tables for USB, 10 Keyboard/Keypad Page (0x07)."""

  NONE = 0x00
  ERR_OVF = 0x01
  A = 0x04
  B = 0x05
  C = 0x06
  D = 0x07
  E = 0x08
  F = 0x09
  G = 0x0A
  H = 0x0B
  I = 0x0C
  J = 0x0D
  K = 0x0E
  L = 0x0F
  M = 0x10
  N = 0x11
  O = 0x12
  P = 0x13
  Q = 0x14
  R = 0x15
  S = 0x16
  T = 0x17
  U = 0x18
  V = 0x19
  W = 0x1A
  X = 0x1B
  Y = 0x1C
  Z = 0x1D
  # Numbers on keyboard (not numpad)
  KEY_1 = 0x1E
  KEY_2 = 0x1F
  KEY_3 = 0x20
  KEY_4 = 0x21
  KEY_5 = 0x22
  KEY_6 = 0x23
  KEY_7 = 0x24
  KEY_8 = 0x25
  KEY_9 = 0x26
  KEY_0 = 0x27
  ENTER = 0x28
  ESC = 0x29
  BACKSPACE = 0x2A
  TAB = 0x2B
  SPACE = 0x2C
  MINUS = 0x2D
  EQUAL = 0x2E
  LEFTBRACE = 0x2F
  RIGHTBRACE = 0x30
  BACKSLASH = 0x31
  HASHTILDE = 0x32
  SEMICOLON = 0x33
  APOSTROPHE = 0x34
  GRAVE = 0x35
  COMMA = 0x36
  DOT = 0x37
  SLASH = 0x38
  CAPSLOCK = 0x39
  F1 = 0x3A
  F2 = 0x3B
  F3 = 0x3C
  F4 = 0x3D
  F5 = 0x3E
  F6 = 0x3F
  F7 = 0x40
  F8 = 0x41
  F9 = 0x42
  F10 = 0x43
  F11 = 0x44
  F12 = 0x45
  SYSRQ = 0x46
  SCROLLLOCK = 0x47
  PAUSE = 0x48
  INSERT = 0x49
  HOME = 0x4A
  PAGEUP = 0x4B
  DELETE = 0x4C
  END = 0x4D
  PAGEDOWN = 0x4E
  RIGHT = 0x4F
  LEFT = 0x50
  DOWN = 0x51
  UP = 0x52
