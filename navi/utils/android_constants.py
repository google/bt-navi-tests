#  Copyright 2026 Google LLC
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

"""Android-specific constants.

Enum classes in this module map Android constants to Python.
Historically, Android constants have been defined discretely, although they are
factually enumerations.
To check the source of truth, please refer to Android source code:
https://cs.android.com/android/platform/superproject/main/+/main:
"""

from __future__ import annotations

import enum

PACKAGE_NAME_BLUETOOTH_SNIPPET = (
    "com.google.wireless.android.pixel.bluetooth.snippet"
)


class Property(enum.StrEnum):
  """system/libsysprop/srcs/android/sysprop/BluetoothProperties.sysprop."""

  HID_DEVICE_ENABLED = "bluetooth.profile.hid.device.enabled"
  HID_HOST_ENABLED = "bluetooth.profile.hid.host.enabled"
  MAP_CLIENT_ENABLED = "bluetooth.profile.map.client.enabled"
  MAP_SERVER_ENABLED = "bluetooth.profile.map.server.enabled"
  MCP_SERVER_ENABLED = "bluetooth.profile.mcp.server.enabled"
  OPP_ENABLED = "bluetooth.profile.opp.enabled"
  PAN_NAP_ENABLED = "bluetooth.profile.pan.nap.enabled"
  PAN_PANU_ENABLED = "bluetooth.profile.pan.panu.enabled"
  PBAP_CLIENT_ENABLED = "bluetooth.profile.pbap.client.enabled"
  PBAP_SERVER_ENABLED = "bluetooth.profile.pbap.server.enabled"
  PBAP_SIM_ENABLED = "bluetooth.profile.pbap.sim.enabled"
  SAP_SERVER_ENABLED = "bluetooth.profile.sap.server.enabled"
  CCP_SERVER_ENABLED = "bluetooth.profile.ccp.server.enabled"
  VCP_CONTROLLER_ENABLED = "bluetooth.profile.vcp.controller.enabled"
  A2DP_SINK_ENABLED = "bluetooth.profile.a2dp.sink.enabled"
  A2DP_SOURCE_ENABLED = "bluetooth.profile.a2dp.source.enabled"
  ASHA_CENTRAL_ENABLED = "bluetooth.profile.asha.central.enabled"
  AVRCP_CONTROLLER_ENABLED = "bluetooth.profile.avrcp.controller.enabled"
  AVRCP_TARGET_ENABLED = "bluetooth.profile.avrcp.target.enabled"
  BAP_BROADCAST_ASSIST_ENABLED = (
      "bluetooth.profile.bap.broadcast.assist.enabled"
  )
  BAP_BROADCAST_SOURCE_ENABLED = (
      "bluetooth.profile.bap.broadcast.source.enabled"
  )
  BAP_UNICAST_CLIENT_ENABLED = "bluetooth.profile.bap.unicast.client.enabled"
  BAS_CLIENT_ENABLED = "bluetooth.profile.bas.client.enabled"
  BASS_CLIENT_ENABLED = "bluetooth.profile.bass.client.enabled"
  CSIP_SET_COORDINATOR_ENABLED = (
      "bluetooth.profile.csip.set_coordinator.enabled"
  )
  GATT_ENABLED = "bluetooth.profile.gatt.enabled"
  GMAP_ENABLED = "bluetooth.profile.gmap.enabled"
  HAP_CLIENT_ENABLED = "bluetooth.profile.hap.client.enabled"
  HFP_AG_ENABLED = "bluetooth.profile.hfp.ag.enabled"
  HFP_HF_ENABLED = "bluetooth.profile.hfp.hf.enabled"
  VAP_SERVER_ENABLED = "bluetooth.profile.vap.server.enabled"
  # LE Audio.
  LEAUDIO_BYPASS_ALLOW_LIST = "persist.bluetooth.leaudio.bypass_allow_list"
  LEAUDIO_ALLOW_LIST = "persist.bluetooth.leaudio.allow_list"
  LEAUDIO_LE_AUDIO_CONNECTION_BY_DEFAULT = (
      "ro.bluetooth.leaudio.le_audio_connection_by_default"
  )
  # HFP.
  SCO_MANAGED_BY_AUDIO = "bluetooth.sco.managed_by_audio"
  HFP_SW_PATH_ENABLED = "bluetooth.hfp.software_datapath.enabled"
  HFP_AG_FEATURES_MASK = (  # HF features are actually AG features.
      "bluetooth.hfp.hf_features.config"
  )
  # AVRCP
  AVRCP_VERSION = "persist.bluetooth.avrcpversion"
  RFCOMM_SYSPROXY_RX_EXIT_SNIFF = "bluetooth.rfcomm.sysproxy.rx.exit_sniff"
  # A2DP Source.
  A2DP_SOURCE_OPUS_ENABLED = "persist.bluetooth.opus.enabled"
  A2DP_CODEC_EXTENSIBILITY = "persist.vendor.audio.a2dp_codec_extensibility"


class Transport(enum.IntEnum):
  """android.bluetooth.BluetoothDevice.Transport."""

  AUTO = 0
  CLASSIC = 1
  LE = 2


class Profile(enum.IntEnum):
  """android.bluetooth.BluetoothProfile."""

  HEADSET = 1
  A2DP = 2
  HEALTH = 3
  HID_HOST = 4
  PAN = 5
  PBAP = 6
  GATT = 7
  GATT_SERVER = 8
  MAP = 9
  SAP = 10
  A2DP_SINK = 11
  AVRCP_CONTROLLER = 12
  AVRCP = 13
  HEADSET_CLIENT = 16
  PBAP_CLIENT = 17
  MAP_CLIENT = 18
  HID_DEVICE = 19
  OPP = 20
  HEARING_AID = 21
  LE_AUDIO = 22
  VOLUME_CONTROL = 23
  MCP_SERVER = 24
  CSIP_SET_COORDINATOR = 25
  LE_AUDIO_BROADCAST = 26
  LE_CALL_CONTROL = 27
  HAP_CLIENT = 28
  LE_AUDIO_BROADCAST_ASSISTANT = 29
  BATTERY = 30
  GMAP = 31


class ActiveDeviceUse(enum.IntEnum):
  """android.bluetooth.BluetoothProfile.ActiveDeviceUse.

  Used to specify a set of profiles. ASHA and LEA are always included.
  """

  # A2DP (Source) + ASHA + LEA
  AUDIO = 0
  # HFP (AG) + ASHA + LEA
  PHONE_CALL = 1
  # A2DP (Source) + HFP (AG) + ASHA + LEA
  ALL = 2


class ScanMode(enum.IntEnum):
  """android.bluetooth.BluetoothAdapter.ScanMode."""

  NONE = 20
  CONNECTABLE = 21
  CONNECTABLE_DISCOVERABLE = 23


class ConnectionPolicy(enum.IntEnum):
  """android.bluetooth.BluetoothProfile.ConnectionPolicy."""

  UNKNOWN = -1
  FORBIDDEN = 0
  ALLOWED = 100


class PairingVariant(enum.IntEnum):
  """android.bluetooth.BluetoothDevice.PairingVariant."""

  PIN = 0
  PASSKEY = 1
  PASSKEY_CONFIRMATION = 2
  CONSENT = 3
  DISPLAY_PASSKEY = 4
  DISPLAY_PIN = 5
  OOB_CONSENT = 6
  PIN_16_DIGITS = 7


class AddressTypeStatus(enum.IntEnum):
  """android.bluetooth.le.AdvertisingSetParameters.AddressTypeStatus."""

  DEFAULT = -1
  PUBLIC = 0
  # Indicating RPA(Resolvable Public Address) when used in ownAddressType
  RANDOM = 1
  RANDOM_NON_RESOLVABLE = 2


class ConnectionState(enum.IntEnum):
  """android.bluetooth.BluetoothProfile.BtProfileState and android.bluetooth.BluetoothGatt."""

  DISCONNECTED = 0
  CONNECTING = 1
  CONNECTED = 2
  DISCONNECTING = 3
  # GATT only
  CLOSED = 4


class BondState(enum.IntEnum):
  """android.bluetooth.BluetoothDevice.BOND_*."""

  NONE = 10
  BONDING = 11
  BONDED = 12


class AdapterState(enum.IntEnum):
  """android.bluetooth.BluetoothAdapter.STATE_*."""

  OFF = 10
  TURNING_ON = 11
  ON = 12
  TURNING_OFF = 13
  BLE_TURNING_ON = 14
  BLE_ON = 15
  BLE_TURNING_OFF = 16


class A2dpState(enum.IntEnum):
  """android.bluetooth.BluetoothA2dp.STATE_*."""

  PLAYING = 10
  NOT_PLAYING = 11


class A2dpCodecType(enum.IntEnum):
  """android.bluetooth.BluetoothCodecConfig.SOURCE_CODEC_TYPE_*."""

  SBC = 0
  AAC = 1
  APTX = 2
  APTX_HD = 3
  LDAC = 4
  LC3 = 5
  OPUS = 6
  INVALID = 1000 * 1000


class BluetoothCodecId(enum.IntEnum):
  """android.bluetooth.BluetoothCodecType.CODEC_ID_*."""

  SBC = 0x0000000000
  AAC = 0x0000000002
  APTX = 0x0001004FFF
  APTX_HD = 0x002400D7FF
  # Note: There is a deprecated codec "LDAC" replaced with SONY_LDAC. For
  # simplicity, we use the value of SONY_LDAC with the name LDAC.
  LDAC = 0x00AA_012D_FF
  OPUS = 0x000100E0FF
  LHDC_V5 = 0x4C35_053A_FF


class A2dpSampleRate(enum.IntFlag):
  RATE_44100 = 1 << 0
  RATE_48000 = 1 << 1
  RATE_88200 = 1 << 2
  RATE_96000 = 1 << 3
  RATE_176400 = 1 << 4
  RATE_192000 = 1 << 5


class A2dpBitsPerSample(enum.IntFlag):
  BITS_16 = 1 << 0
  BITS_24 = 1 << 1
  BITS_32 = 1 << 2


class A2dpChannelMode(enum.IntFlag):
  MONO = 1 << 0
  STEREO = 1 << 1


class ScoState(enum.IntEnum):
  """android.bluetooth.BluetoothHeadset.STATE_AUDIO_*."""

  DISCONNECTED = 10
  CONNECTING = 11
  CONNECTED = 12


class RepeatMode(enum.IntEnum):
  """androidx.media3.common.Player.RepeatMode.

  Also: androidx.media3.session.legacy.PlaybackStateCompat.RepeatMode.
  """

  INVALID = -1
  OFF = 0
  ONE = 1
  ALL = 2
  GROUP = 3  # Only available in legacy PlaybackStateCompat.


class ShuffleMode(enum.IntEnum):
  """androidx.media3.session.legacy.PlaybackStateCompat.ShuffleMode."""

  INVALID = -1
  OFF = 0
  ALL = 1
  GROUP = 2


class GattStatus(enum.IntEnum):
  """android.bluetooth.BluetoothGatt.GATT_*.

  * Also check packages/modules/Bluetooth/system/stack/include/gatt_api.h
  """

  SUCCESS = 0x00
  INVALID_HANDLE = 0x01
  READ_NOT_PERMIT = 0x02
  WRITE_NOT_PERMIT = 0x03
  INVALID_PDU = 0x04
  INSUF_AUTHENTICATION = 0x05
  REQ_NOT_SUPPORTED = 0x06
  INVALID_OFFSET = 0x07
  INSUF_AUTHORIZATION = 0x08
  PREPARE_Q_FULL = 0x09
  NOT_FOUND = 0x0A
  NOT_LONG = 0x0B
  INSUF_KEY_SIZE = 0x0C
  INVALID_ATTR_LEN = 0x0D
  ERR_UNLIKELY = 0x0E
  INSUF_ENCRYPTION = 0x0F
  UNSUPPORT_GRP_TYPE = 0x10
  INSUF_RESOURCE = 0x11
  DATABASE_OUT_OF_SYNC = 0x12
  VALUE_NOT_ALLOWED = 0x13
  ILLEGAL_PARAMETER = 0x87
  NO_RESOURCES = 0x80
  INTERNAL_ERROR = 0x81
  WRONG_STATE = 0x82
  DB_FULL = 0x83
  BUSY = 0x84
  ERROR = 0x85
  CMD_STARTED = 0x86
  PENDING = 0x88
  AUTH_FAIL = 0x89
  INVALID_CFG = 0x8B
  SERVICE_STARTED = 0x8C
  ENCRYPED_NO_MITM = 0x8D
  NOT_ENCRYPTED = 0x8E
  CONGESTED = 0x8F
  DUP_REG = 0x90
  ALREADY_OPEN = 0x91
  CANCEL = 0x92
  CONNECTION_TIMEOUT = 0x93
  CCC_CFG_ERR = 0xFD
  PRC_IN_PROGRESS = 0xFE
  OUT_OF_RANGE = 0xFF
  FAILURE = 0x101


class GattServiceType(enum.IntEnum):
  """android.bluetooth.BluetoothGattService.SERVICE_TYPE_*."""

  PRIMARY = 0x00
  SECONDARY = 0x01


class GattWriteType(enum.IntEnum):
  """android.bluetooth.BluetoothGattCharacteristic.WRITE_TYPE_*."""

  NO_RESPONSE = 0x01
  DEFAULT = 0x02
  SIGNED = 0x04


class GattCharacteristicProperty(enum.IntFlag):
  """android.bluetooth.BluetoothGattCharacteristic.PROPERTY_*."""

  # Characteristic property: Characteristic is broadcastable.
  BROADCAST = 0x01
  # Characteristic property: Characteristic is readable.
  READ = 0x02
  # Characteristic property: Characteristic can be written without response.
  WRITE_NO_RESPONSE = 0x04
  # Characteristic property: Characteristic can be written.
  WRITE = 0x08
  # Characteristic property: Characteristic supports notification
  NOTIFY = 0x10
  # Characteristic property: Characteristic supports indication
  INDICATE = 0x20
  # Characteristic property: Characteristic supports write with signature
  SIGNED_WRITE = 0x40
  # Characteristic property: Characteristic has extended properties
  EXTENDED_PROPS = 0x80


class GattCharacteristicPermission(enum.IntFlag):
  """android.bluetooth.BluetoothGattCharacteristic.PERMISSION_*."""

  # Characteristic read permission
  READ = 0x01
  # Characteristic permission: Allow encrypted read operations
  READ_ENCRYPTED = 0x02
  # Characteristic permission: Allow reading with AUTH protection
  READ_ENCRYPTED_AUTH = 0x04
  # Characteristic write permission
  WRITE = 0x10
  # Characteristic permission: Allow encrypted writes
  WRITE_ENCRYPTED = 0x20
  # Characteristic permission: Allow encrypted writes with AUTH protection
  WRITE_ENCRYPTED_AUTH = 0x40
  # Characteristic permission: Allow signed write operations
  WRITE_SIGNED = 0x80


class BleScanMode(enum.IntEnum):
  """android.bluetooth.le.ScanSettings.SCAN_MODE_*."""

  LOW_POWER = 0
  BALANCED = 1
  LOW_LATENCY = 2
  AMBIENT_DISCOVERY = 3
  SCREEN_OFF = 4
  SCREEN_OFF_BALANCED = 5


class BleScanCallbackType(enum.IntFlag):
  """android.bluetooth.le.ScanSettings.CALLBACK_TYPE_*."""

  ALL_MATCHES = 1
  FIRST_MATCH = 2
  MATCH_LOST = 4
  ALL_MATCHES_AUTO_BATCH = 8


class BleScanMatchMode(enum.IntEnum):
  """android.bluetooth.le.ScanSettings.MATCH_MODE_*."""

  AGGRESSIVE = 1
  STICKY = 2


class BleScanResultType(enum.IntEnum):
  """android.bluetooth.le.ScanSettings.SCAN_RESULT_TYPE_*."""

  FULL = 0
  ABBREVIATED = 1


class LegacyTxPowerLevel(enum.IntEnum):
  """android.bluetooth.le.AdvertiseSettings.ADVERTISE_TX_POWER_*."""

  ULTRA_LOW = 0
  LOW = 1
  MEDIUM = 2
  HIGH = 3


class ExtendedTxPowerLevel(enum.IntEnum):
  """android.bluetooth.le.AdvertisingSetParameters.TX_POWER_LEVEL_*."""

  MIN = -127
  ULTRA_LOW = -21
  LOW = -15
  MEDIUM = -7
  HIGH = 1
  MAX = 1


class LegacyAdvertiseMode(enum.IntEnum):
  """android.bluetooth.le.AdvertiseSettings.ADVERTISE_MODE_*."""

  LOW_POWER = 0
  BALANCED = 1
  LOW_LATENCY = 2


class GattDescriptorValue:
  """android.bluetooth.BluetoothGattDescriptor."""

  ENABLE_NOTIFICATION = (0x01, 0x00)
  ENABLE_INDICATION = (0x02, 0x00)
  DISABLE_NOTIFICATION = (0x00, 0x00)


class AdvertisingInterval(enum.IntEnum):
  """android.bluetooth.le.AdvertisingSetParameters.INTERVAL_*."""

  MIN = 160
  LOW = 160
  MEDIUM = 400
  HIGH = 1600
  MAX = 16777215


class StreamType(enum.IntEnum):
  """android.media.AudioSystem.STREAM_*."""

  CALL = 0
  SYSTEM = 1
  RING = 2
  MUSIC = 3
  ALARM = 4
  NOTIFICATION = 5
  BLUETOOTH_SCO = 6
  SYSTEM_ENFORCED = 7
  DTMF = 8
  TTS = 9
  ACCESSIBILITY = 10
  ASSISTANT = 11


class AudioDeviceType(enum.IntEnum):
  """android.media.AudioDeviceInfo.TYPE_*."""

  UNKNOWN = 0
  BUILTIN_EARPIECE = 1
  BUILTIN_SPEAKER = 2
  WIRED_HEADSET = 3
  WIRED_HEADPHONES = 4
  LINE_ANALOG = 5
  LINE_DIGITAL = 6
  BLUETOOTH_SCO = 7
  BLUETOOTH_A2DP = 8
  HDMI = 9
  HDMI_ARC = 10
  USB_DEVICE = 11
  USB_ACCESSORY = 12
  DOCK = 13
  FM = 14
  BUILTIN_MIC = 15
  FM_TUNER = 16
  TV_TUNER = 17
  TELEPHONY = 18
  AUX_LINE = 19
  IP = 20
  BUS = 21
  USB_HEADSET = 22
  HEARING_AID = 23
  BUILTIN_SPEAKER_SAFE = 24
  REMOTE_SUBMIX = 25
  BLE_HEADSET = 26
  BLE_SPEAKER = 27
  ECHO_REFERENCE = 28
  HDMI_EARC = 29
  BLE_BROADCAST = 30
  DOCK_ANALOG = 31


class CallState(enum.IntEnum):
  """android.telecom.Call.STATE_*."""

  NEW = 0
  DIALING = 1
  RINGING = 2
  HOLDING = 3
  ACTIVE = 4
  DISCONNECTED = 7
  SELECT_PHONE_ACCOUNT = 8
  CONNECTING = 9
  DISCONNECTING = 10
  PULLING_CALL = 11
  AUDIO_PROCESSING = 12
  SIMULATED_RINGING = 13


class CallEndpointType(enum.IntEnum):
  """A type of call endpoint.

  Source: android.telecom.CallEndpoint.TYPE_*.
  """

  UNKNOWN = -1
  EARPIECE = 1
  BLUETOOTH = 2
  WIRED_HEADSET = 3
  SPEAKER = 4
  STREAMING = 5


class Phy(enum.IntEnum):
  """android.bluetooth.BluetoothDevice.PHY_*."""

  LE_1M = 1
  LE_2M = 2
  LE_CODED = 3

  def to_mask(self) -> PhyMask:
    """Converts Phy to PhyMask."""
    match self:
      case Phy.LE_1M:
        return PhyMask.LE_1M
      case Phy.LE_2M:
        return PhyMask.LE_2M
      case Phy.LE_CODED:
        return PhyMask.LE_CODED
      case _:
        raise ValueError(f"Unsupported PHY: {self}")


class PhyMask(enum.IntFlag):
  """android.bluetooth.BluetoothDevice.PHY_*_MASK."""

  LE_1M = 1
  LE_2M = 2
  LE_CODED = 4


class CodedPhyOption(enum.IntEnum):
  """android.bluetooth.BluetoothDevice.PHY_OPTION_*."""

  NO_PREFERRED = 0
  S2 = 1
  S8 = 2


class KeyAction(enum.IntEnum):
  """android.view.KeyEvent.ACTION_*."""

  DOWN = 0
  UP = 1


class Button(enum.IntFlag):
  """android.view.MotionEvent.BUTTON_*."""

  PRIMARY = 1 << 0
  SECONDARY = 1 << 1
  TERTIARY = 1 << 2
  BACK = 1 << 3
  FORWARD = 1 << 4
  STYLUS_PRIMARY = 1 << 5
  STYLUS_SECONDARY = 1 << 6


class MotionAction(enum.IntEnum):
  """android.view.MotionEvent.ACTION_*."""

  DOWN = 0
  UP = 1
  MOVE = 2
  CANCEL = 3
  OUTSIDE = 4
  POINTER_DOWN = 5
  POINTER_UP = 6
  HOVER_MOVE = 7
  SCROLL = 8
  HOVER_ENTER = 9
  HOVER_EXIT = 10
  BUTTON_PRESS = 11
  BUTTON_RELEASE = 12
  POINTER_INDEX_MASK = 0xFF00
  POINTER_INDEX_SHIFT = 8


class KeyCode(enum.IntEnum):
  """android.view.KeyEvent.KEYCODE_*."""

  UNKNOWN = 0
  SOFT_LEFT = 1
  SOFT_RIGHT = 2
  HOME = 3
  BACK = 4
  CALL = 5
  ENDCALL = 6
  KEY_0 = 7
  KEY_1 = 8
  KEY_2 = 9
  KEY_3 = 10
  KEY_4 = 11
  KEY_5 = 12
  KEY_6 = 13
  KEY_7 = 14
  KEY_8 = 15
  KEY_9 = 16
  STAR = 17
  POUND = 18
  DPAD_UP = 19
  DPAD_DOWN = 20
  DPAD_LEFT = 21
  DPAD_RIGHT = 22
  DPAD_CENTER = 23
  VOLUME_UP = 24
  VOLUME_DOWN = 25
  POWER = 26
  CAMERA = 27
  CLEAR = 28
  A = 29
  B = 30
  C = 31
  D = 32
  E = 33
  F = 34
  G = 35
  H = 36
  I = 37
  J = 38
  K = 39
  L = 40
  M = 41
  N = 42
  O = 43
  P = 44
  Q = 45
  R = 46
  S = 47
  T = 48
  U = 49
  V = 50
  W = 51
  X = 52
  Y = 53
  Z = 54
  COMMA = 55
  PERIOD = 56
  ALT_LEFT = 57
  ALT_RIGHT = 58
  SHIFT_LEFT = 59
  SHIFT_RIGHT = 60
  TAB = 61
  SPACE = 62
  SYM = 63
  EXPLORER = 64
  ENVELOPE = 65
  ENTER = 66
  DEL = 67
  GRAVE = 68
  MINUS = 69
  EQUALS = 70
  LEFT_BRACKET = 71
  RIGHT_BRACKET = 72
  BACKSLASH = 73
  SEMICOLON = 74
  APOSTROPHE = 75
  SLASH = 76
  AT = 77
  NUM = 78
  HEADSETHOOK = 79
  FOCUS = 80
  PLUS = 81
  MENU = 82
  NOTIFICATION = 83
  SEARCH = 84
  MEDIA_PLAY_PAUSE = 85
  MEDIA_STOP = 86
  MEDIA_NEXT = 87
  MEDIA_PREVIOUS = 88
  MEDIA_REWIND = 89
  MEDIA_FAST_FORWARD = 90
  MUTE = 91
  PAGE_UP = 92
  PAGE_DOWN = 93
  PICTSYMBOLS = 94
  SWITCH_CHARSET = 95
  BUTTON_A = 96
  BUTTON_B = 97
  BUTTON_C = 98
  BUTTON_X = 99
  BUTTON_Y = 100
  BUTTON_Z = 101
  BUTTON_L1 = 102
  BUTTON_R1 = 103
  BUTTON_L2 = 104
  BUTTON_R2 = 105
  BUTTON_THUMBL = 106
  BUTTON_THUMBR = 107
  BUTTON_START = 108
  BUTTON_SELECT = 109
  BUTTON_MODE = 110
  ESCAPE = 111
  FORWARD_DEL = 112
  CTRL_LEFT = 113
  CTRL_RIGHT = 114
  CAPS_LOCK = 115
  SCROLL_LOCK = 116
  META_LEFT = 117
  META_RIGHT = 118
  FUNCTION = 119
  SYSRQ = 120
  BREAK = 121
  MOVE_HOME = 122
  MOVE_END = 123
  INSERT = 124
  FORWARD = 125
  MEDIA_PLAY = 126
  MEDIA_PAUSE = 127
  MEDIA_CLOSE = 128
  MEDIA_EJECT = 129
  MEDIA_RECORD = 130
  F1 = 131
  F2 = 132
  F3 = 133
  F4 = 134
  F5 = 135
  F6 = 136
  F7 = 137
  F8 = 138
  F9 = 139
  F10 = 140
  F11 = 141
  F12 = 142
  NUM_LOCK = 143
  NUMPAD_0 = 144
  NUMPAD_1 = 145
  NUMPAD_2 = 146
  NUMPAD_3 = 147
  NUMPAD_4 = 148
  NUMPAD_5 = 149
  NUMPAD_6 = 150
  NUMPAD_7 = 151
  NUMPAD_8 = 152
  NUMPAD_9 = 153
  NUMPAD_DIVIDE = 154
  NUMPAD_MULTIPLY = 155
  NUMPAD_SUBTRACT = 156
  NUMPAD_ADD = 157
  NUMPAD_DOT = 158
  NUMPAD_COMMA = 159
  NUMPAD_ENTER = 160
  NUMPAD_EQUALS = 161
  NUMPAD_LEFT_PAREN = 162
  NUMPAD_RIGHT_PAREN = 163
  VOLUME_MUTE = 164
  INFO = 165
  CHANNEL_UP = 166
  CHANNEL_DOWN = 167
  ZOOM_IN = 168
  ZOOM_OUT = 169
  TV = 170
  WINDOW = 171
  GUIDE = 172
  DVR = 173
  BOOKMARK = 174
  CAPTIONS = 175
  SETTINGS = 176


class BluetoothAccessPermission(enum.IntEnum):
  """android.bluetooth.BluetoothDevice.ACCESS_*."""

  UNKNOWN = 0
  ALLOWED = 1
  REJECTED = 2


class PhoneType(enum.IntEnum):
  """android.provider.ContactsContract.CommonDataKinds.Phone.TYPE_*."""

  HOME = 1
  MOBILE = 2
  WORK = 3
  FAX_WORK = 4
  FAX_HOME = 5
  PAGER = 6
  OTHER = 7
  CALLBACK = 8
  CAR = 9
  COMPANY_MAIN = 10
  ISDN = 11
  MAIN = 12
  OTHER_FAX = 13
  RADIO = 14
  TELEX = 15
  TTY_TDD = 16
  WORK_MOBILE = 17
  WORK_PAGER = 18
  ASSISTANT = 19
  MMS = 20


class EmailType(enum.IntEnum):
  """android.provider.ContactsContract.CommonDataKinds.Email.TYPE_*."""

  HOME = 1
  WORK = 2
  OTHER = 3
  MOBILE = 4


class CallType(enum.IntEnum):
  """android.provider.CallLog.Calls.*_TYPE."""

  INCOMING = 1
  OUTGOING = 2
  MISSED = 3
  VOICEMAIL = 4
  REJECTED = 5
  BLOCKED = 6


class SmsMessageType(enum.IntEnum):
  """android.provider.TextBasedSmsColumns.MESSAGE_TYPE_*."""

  # Message type: all messages.
  ALL = 0
  # Message type: inbox.
  INBOX = 1
  # Message type: sent messages.
  SENT = 2
  # Message type: drafts.
  DRAFT = 3
  # Message type: outbox.
  OUTBOX = 4
  # Message type: failed outgoing message.
  FAILED = 5
  # Message type: queued to send later.
  QUEUED = 6


class LeAudioBroadcastQuality(enum.IntEnum):
  """android.bluetooth.BluetoothLeBroadcastSubgroupSettings.QUALITY_*."""

  STANDARD = 0
  HIGH = 1


class DistanceMeasurementMethodId(enum.IntEnum):
  """android.bluetooth.le.DistanceMeasurementMethod.DistanceMeasurementMethodId."""

  AUTO = 0
  RSSI = 1
  CHANNEL_SOUNDING = 2


class BluetoothQualityReportId(enum.IntEnum):
  """android.bluetooth.BluetoothQualityReport.QUALITY_REPORT_ID_*."""

  MONITOR = 0x01
  APPROACH_LSTO = 0x02
  A2DP_CHOPPY = 0x03
  SCO_CHOPPY = 0x04
  ENERGY_MONITOR = 0x06
  CONN_FAIL = 0x08
  RF_STATS = 0x09


class ConnectionPriority(enum.IntEnum):
  """android.bluetooth.BluetoothGatt.connectionPriority."""

  BALANCED = 0
  HIGH = 1
  LOW_POWER = 2
  DCK = 3


class LeSubrateMode(enum.IntEnum):
  """android.bluetooth.BluetoothGatt.SubrateMode."""

  OFF = 0
  LOW = 1
  BALANCED = 2
  HIGH = 3
  SYSTEM_UPDATE = 99
  NOT_UPDATED = 255


class BluetoothStatusCode(enum.IntEnum):
  """android.bluetooth.BluetoothStatusCodes."""

  SUCCESS = 0
  ERROR_BLUETOOTH_NOT_ENABLED = 1
  ERROR_BLUETOOTH_NOT_ALLOWED = 2
  ERROR_DEVICE_NOT_BONDED = 3
  ERROR_DEVICE_NOT_CONNECTED = 4
  ERROR_MISSING_BLUETOOTH_CONNECT_PERMISSION = 6
  ERROR_MISSING_BLUETOOTH_SCAN_PERMISSION = 7
  ERROR_PROFILE_SERVICE_NOT_BOUND = 9
  FEATURE_SUPPORTED = 10
  FEATURE_NOT_SUPPORTED = 11
  ERROR_NOT_ACTIVE_DEVICE = 12
  ERROR_NO_ACTIVE_DEVICES = 13
  ERROR_PROFILE_NOT_CONNECTED = 14
  ERROR_TIMEOUT = 15
  REASON_LOCAL_APP_REQUEST = 16
  REASON_LOCAL_STACK_REQUEST = 17
  REASON_REMOTE_REQUEST = 18
  REASON_SYSTEM_POLICY = 19
  ERROR_HARDWARE_GENERIC = 20
  ERROR_BAD_PARAMETERS = 21
  ERROR_LOCAL_NOT_ENOUGH_RESOURCES = 22
  ERROR_REMOTE_NOT_ENOUGH_RESOURCES = 23
  ERROR_REMOTE_OPERATION_REJECTED = 24
  ERROR_REMOTE_LINK_ERROR = 25
  ERROR_ALREADY_IN_TARGET_STATE = 26
  ERROR_REMOTE_OPERATION_NOT_SUPPORTED = 27
  ERROR_CALLBACK_NOT_REGISTERED = 28
  ERROR_ANOTHER_ACTIVE_REQUEST = 29
  FEATURE_NOT_CONFIGURED = 30
  ERROR_GATT_WRITE_NOT_ALLOWED = 200
  ERROR_GATT_WRITE_REQUEST_BUSY = 201
  ALLOWED = 400
  NOT_ALLOWED = 401
  ERROR_ANOTHER_ACTIVE_OOB_REQUEST = 1000
  ERROR_DISCONNECT_REASON_LOCAL_REQUEST = 1100
  ERROR_DISCONNECT_REASON_REMOTE_REQUEST = 1101
  ERROR_DISCONNECT_REASON_LOCAL = 1102
  ERROR_DISCONNECT_REASON_REMOTE = 1103
  ERROR_DISCONNECT_REASON_TIMEOUT = 1104
  ERROR_DISCONNECT_REASON_SECURITY = 1105
  ERROR_DISCONNECT_REASON_SYSTEM_POLICY = 1106
  ERROR_DISCONNECT_REASON_RESOURCE_LIMIT_REACHED = 1107
  ERROR_DISCONNECT_REASON_CONNECTION_ALREADY_EXISTS = 1108
  ERROR_DISCONNECT_REASON_BAD_PARAMETERS = 1109
  ERROR_AUDIO_DEVICE_ALREADY_CONNECTED = 1116
  ERROR_AUDIO_DEVICE_ALREADY_DISCONNECTED = 1117
  ERROR_AUDIO_ROUTE_BLOCKED = 1118
  ERROR_CALL_ACTIVE = 1119
  ERROR_LE_BROADCAST_INVALID_BROADCAST_ID = 1200
  ERROR_LE_BROADCAST_INVALID_CODE = 1201
  ERROR_LE_BROADCAST_ASSISTANT_INVALID_SOURCE_ID = 1202
  ERROR_LE_BROADCAST_ASSISTANT_DUPLICATE_ADDITION = 1203
  ERROR_LE_CONTENT_METADATA_INVALID_PROGRAM_INFO = 1204
  ERROR_LE_CONTENT_METADATA_INVALID_LANGUAGE = 1205
  ERROR_LE_CONTENT_METADATA_INVALID_OTHER = 1206
  ERROR_CSIP_INVALID_GROUP_ID = 1207
  ERROR_CSIP_GROUP_LOCKED_BY_OTHER = 1208
  ERROR_CSIP_LOCKED_GROUP_MEMBER_LOST = 1209
  ERROR_HAP_PRESET_NAME_TOO_LONG = 1210
  ERROR_HAP_INVALID_PRESET_INDEX = 1211
  ERROR_NO_LE_CONNECTION = 1300
  ERROR_DISTANCE_MEASUREMENT_INTERNAL = 1301
  ERROR_SDP_DISCOVERY_FAILED = 1400
  ERROR_STREAM_CONNECTION_FAILED = 1401
  ERROR_ROLE_SWITCH_FAILED = 1402
  ERROR_AVDTP_DISCOVERY_FAILED = 1403
  ERROR_INSUFFICIENT_RESOURCES = 1404
  ERROR_RFCOMM_CONNECTION_FAILED = 1405
  RFCOMM_LISTENER_START_FAILED_UUID_IN_USE = 2000
  RFCOMM_LISTENER_OPERATION_FAILED_NO_MATCHING_SERVICE_RECORD = 2001
  RFCOMM_LISTENER_OPERATION_FAILED_DIFFERENT_APP = 2002
  RFCOMM_LISTENER_FAILED_TO_CREATE_SERVER_SOCKET = 2003
  RFCOMM_LISTENER_FAILED_TO_CLOSE_SERVER_SOCKET = 2004
  RFCOMM_LISTENER_NO_SOCKET_AVAILABLE = 2005
  ERROR_NOT_DUAL_MODE_AUDIO_DEVICE = 3000
  ERROR_TAK_UNAVAILABLE_NOT_CONNECTED = 4000
  ERROR_TAK_UNAVAILABLE_BONDED = 4001
  ERROR_TAK_UNAVAILABLE_ALREADY_STARTED = 4002
  ERROR_TAK_UNAVAILABLE_ALREADY_COMPLETE = 4003
  ERROR_TAK_UNAVAILABLE_USED_BY_OTHER_APP = 4004
  ERROR_TAK_UNAVAILABLE_PAIRING_IN_PROGRESS = 4005
  ERROR_TAK_ENCRYPTION_FAILED = 4006
  ERROR_UNKNOWN = 2147483647


class AudioDeviceRole(enum.IntEnum):
  """android.media.AudioDeviceAttributes.ROLE_*."""

  INPUT = 1
  OUTPUT = 2


class AudioMode(enum.IntEnum):
  """android.media.AudioManager.MODE_*.

  Internal: android.media.AudioSystem.MODE_*.
  """

  INVALID = -2
  CURRENT = -1
  NORMAL = 0
  RINGTONE = 1
  IN_CALL = 2
  IN_COMMUNICATION = 3
  CALL_SCREENING = 4
  CALL_REDIRECT = 5
  COMMUNICATION_REDIRECT = 6
  ASSISTANT_CONVERSATION = 7


class MediaPlaybackState(enum.IntEnum):
  """androidx.media3.session.legacy.PlaybackStateCompat.STATE_*."""

  NONE = 0
  STOPPED = 1
  PAUSED = 2
  PLAYING = 3
  FAST_FORWARDING = 4
  REWINDING = 5
  BUFFERING = 6
  ERROR = 7
  CONNECTING = 8
  SKIPPING_TO_PREVIOUS = 9
  SKIPPING_TO_NEXT = 10
  SKIPPING_TO_QUEUE_ITEM = 11


class BluetoothSocketType(enum.IntEnum):
  """android.bluetooth.BluetoothSocket.TYPE_*."""

  RFCOMM = 1
  SCO = 2
  L2CAP = 3
  LE = 4


class HidHostProtocolMode(enum.IntEnum):
  """android.bluetooth.BluetoothHidHost.PROTOCOL*.

  Note: The values are different from the values in the Bluetooth HID spec.
  """

  REPORT = 0
  BOOT = 1


class TakState(enum.IntEnum):
  """android.bluetooth.TakCallback.TakState."""

  NONE = 0
  ENCRYPTING = 1
  WAITING = 2
  ENCRYPTED = 3
