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

"""Wrappers around complex bl4a APIs."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Mapping, Sequence
import contextlib
import dataclasses
import datetime
import enum
import inspect
import itertools
import logging
from typing import Any, ClassVar, Self, Type, TypeVar, cast

from bumble import hci
from mobly import asserts
from mobly.snippet import callback_event
from mobly.snippet import callback_handler_base
import mobly.snippet.errors
from typing_extensions import override

from navi.utils import android_constants
from navi.utils import auracast_uri
from navi.utils import bluetooth_constants
from navi.utils import constants
from navi.utils import errors
from navi.utils import retry
from navi.utils import snippet_constants
from navi.utils import snippet_stub

_logger = logging.getLogger(__name__)
_DEFAULT_RETRY_COUNT = 3
_DEFAULT_RETRY_DELAY_SECONDS = 1.0
_DEFAULT_CONNECTION_TIMEOUT_SECONDS = 10.0
_DEFAULT_CALLBACK_TIMEOUT_SECONDS = 30.0
_FIELD = 'field'
_MAPPER = 'mapper'


def _make_json_object(arg: Any) -> Any:
  """Converts an object to a JSON-style object."""
  if dataclasses.is_dataclass(arg) and not isinstance(arg, type):
    arg = dataclasses.asdict(arg)

  if isinstance(arg, str):
    return arg
  if isinstance(arg, Sequence):
    return [_make_json_object(v) for v in arg]
  if isinstance(arg, dict):
    return {k: _make_json_object(v) for k, v in arg.items() if v is not None}
  return arg


@enum.unique
class Module(enum.Enum):
  """Snippet modules."""

  AUDIO = enum.auto()
  A2DP = enum.auto()
  ADAPTER = enum.auto()
  HFP_AG = enum.auto()
  HFP_HF = enum.auto()
  TELECOM = enum.auto()
  GATT_CLIENT = enum.auto()
  GATT_SERVER = enum.auto()
  L2CAP = enum.auto()
  RFCOMM = enum.auto()
  LE_AUDIO = enum.auto()
  LE_BROADCAST = enum.auto()
  BASS = enum.auto()
  INPUT = enum.auto()
  HID_HOST = enum.auto()
  PAN = enum.auto()
  ASHA = enum.auto()
  PBAP = enum.auto()
  MAP = enum.auto()
  SAP = enum.auto()
  PLAYER = enum.auto()
  BQR = enum.auto()
  A2DP_SINK = enum.auto()
  HAP_CLIENT = enum.auto()
  VOLUME_CONTROL = enum.auto()
  HEADTRACKER = enum.auto()
  CSIP_SET_COORDINATOR = enum.auto()


@dataclasses.dataclass
class CallbackHandler:
  """Context managable callback handler wrapper.

  Example:
  ```
  with CallbackHandler.for_module(snippet, bl4a_api.Module.HFP_AG) as hfp_cb:
    ...

  # hfp_cb will be automatically released here.
  ```
  """

  snippet: snippet_stub.BluetoothSnippet
  handler: callback_handler_base.CallbackHandlerBase
  on_close: Callable[[str], None] | None = None

  @classmethod
  def for_module(
      cls: Type[Self], snippet: snippet_stub.BluetoothSnippet, module: Module
  ) -> Self:
    """Registers a callback handler for a given module.

    Args:
      snippet: Snippet instance.
      module: Module to register callback handler.

    Returns:
      Wrapper callback handler.

    Raises:
      ValueError: Module is not supported.
    """
    match module:
      case Module.AUDIO:
        handler = snippet.audioRegisterCallback()
        on_close = snippet.audioUnregisterCallback
      case Module.A2DP:
        handler = snippet.registerA2dpCallback()
        on_close = snippet.unregisterA2dpCallback
      case Module.ADAPTER:
        handler = snippet.registerAdapterCallback()
        on_close = snippet.unregisterAdapterCallback
      case Module.HFP_AG:
        handler = snippet.registerHfpAgCallback()
        on_close = snippet.unregisterHfpAgCallback
      case Module.HFP_HF:
        handler = snippet.registerHfpHfCallback()
        on_close = snippet.unregisterHfpHfCallback
      case Module.TELECOM:
        handler = snippet.registerTelecomCallback()
        on_close = snippet.unregisterTelecomCallback
      case Module.LE_AUDIO:
        handler = snippet.registerLeAudioCallback()
        on_close = snippet.unregisterLeAudioCallback
      case Module.INPUT:
        handler = snippet.registerInputEventCallback()
        on_close = snippet.unregisterInputEventCallback
      case Module.HID_HOST:
        handler = snippet.registerHidHostCallback()
        on_close = snippet.unregisterHidHostCallback
      case Module.PAN:
        handler = snippet.registerPanCallback()
        on_close = snippet.unregisterPanCallback
      case Module.ASHA:
        handler = snippet.registerProfileCallback(
            android_constants.Profile.HEARING_AID
        )
        on_close = snippet.unregisterProfileCallback
      case Module.PBAP:
        handler = snippet.registerProfileCallback(
            android_constants.Profile.PBAP
        )
        on_close = snippet.unregisterProfileCallback
      case Module.MAP:
        handler = snippet.registerProfileCallback(android_constants.Profile.MAP)
        on_close = snippet.unregisterProfileCallback
      case Module.SAP:
        handler = snippet.registerProfileCallback(android_constants.Profile.SAP)
        on_close = snippet.unregisterProfileCallback
      case Module.BASS:
        handler = snippet.registerBassCallback()
        on_close = snippet.unregisterBassCallback
      case Module.PLAYER:
        handler = snippet.registerPlayerListener()
        on_close = snippet.unregisterPlayerListener
      case Module.BQR:
        handler = snippet.registerBluetoothQualityReportCallback()
        on_close = snippet.unregisterBluetoothQualityReportCallback
      case Module.A2DP_SINK:
        handler = snippet.registerProfileCallback(
            android_constants.Profile.A2DP_SINK
        )
        on_close = snippet.unregisterProfileCallback
      case Module.HAP_CLIENT:
        handler = snippet.registerHapClientCallback()
        on_close = snippet.unregisterHapClientCallback
      case Module.VOLUME_CONTROL:
        handler = snippet.registerVolumeControlCallback()
        on_close = snippet.unregisterVolumeControlCallback
      case Module.HEADTRACKER:
        handler = snippet.audioRegisterHeadTrackerCallback()
        on_close = snippet.audioUnregisterHeadTrackerCallback
      case Module.CSIP_SET_COORDINATOR:
        handler = snippet.registerCsipSetCoordinatorCallback()
        on_close = snippet.unregisterCsipSetCoordinatorCallback
      case _:
        raise ValueError(f'Unsupported module: {module}')
    return cls(snippet=snippet, handler=handler, on_close=on_close)

  def close(self) -> None:
    """Closes the callback handler."""
    if self.on_close is not None:
      self.on_close(self.handler.callback_id)

  async def wait_for_event(
      self,
      event: type[_EVENT] | _EVENT,
      predicate: Callable[[_EVENT], bool] | None = None,
      timeout: datetime.timedelta | float = _DEFAULT_CALLBACK_TIMEOUT_SECONDS,
      timeout_msg: str | None = None,
  ) -> _EVENT:
    """Waits for a callback event that satisfies the predicate.

    Args:
      event: type of callback event, or an event instance to be matched.
      predicate: a function that takes a callback event metadata and returns a
        bool. If None is given, returns the first one. When event is an instance
        of JsonDeserializableEvent, predicate will be ignored.
      timeout: timeout to wait for the expected event. If not present, wait for
        event for 30 seconds.
      timeout_msg: message to be shown when timeout.

    Returns:
      Callback event metadata.

    Raises:
      AsyncTimeoutError: The expected event does not occur within 30
      seconds.
    """

    if isinstance(timeout, datetime.timedelta):
      timeout = timeout.total_seconds()
    if isinstance(event, type):
      match_msg = ''
      if predicate:
        # inspect.getsource may raise OSError if source unavailable.
        with contextlib.suppress(OSError):
          match_msg = f'matching `{inspect.getsource(predicate).strip()}`'
      event_class = cast(type[JsonDeserializableEvent], event)
    else:
      match_msg = f'== {event}'
      event_class = cast(type[JsonDeserializableEvent], event.__class__)

    try:
      if not isinstance(event, type):
        got = await asyncio.to_thread(
            lambda: self.handler.waitForEvent(
                event.EVENT_NAME,
                lambda e: event.from_mapping(e.data) == event,
                timeout=timeout,
            )
        )
      elif predicate:

        def match_and_log(e: callback_event.CallbackEvent) -> bool:
          _logger.debug('Raw event data: %s', e.data)
          return predicate(cast(Any, event_class.from_mapping(e.data)))

        got = await asyncio.to_thread(
            lambda: self.handler.waitForEvent(
                event_class.EVENT_NAME,
                match_and_log,
                timeout=timeout,
            )
        )
      else:
        got = await asyncio.to_thread(
            lambda: self.handler.waitAndGet(event_class.EVENT_NAME, timeout)
        )
      got.data['creation_time'] = datetime.datetime.fromtimestamp(
          got.creation_time / 1000
      )
    except mobly.snippet.errors.CallbackHandlerTimeoutError:
      raise errors.AsyncTimeoutError(
          timeout_msg
          or (
              f'No event {event_class.__name__}({event.EVENT_NAME}) {match_msg}'
              + f' is received within {timeout} seconds.'
          )
      ) from None
    return event.from_mapping(got.data)

  def get_all_events(self, callback_type: type[_EVENT]) -> list[_EVENT]:
    """Gets all posted callback events.

    Args:
      callback_type: type of callback.

    Returns:
      List of callback events.
    """

    return [
        callback_type.from_mapping(event.data)
        for event in self.handler.getAll(callback_type.EVENT_NAME)
    ]

  def __enter__(self) -> Self:
    return self

  def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
    with contextlib.suppress(mobly.snippet.errors.ApiError):
      self.close()


@dataclasses.dataclass
class JsonDeserializable:
  """Base class for JSON deserializable objects."""

  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    """Creates an instance deserialized from a JSON-style mapping.

    Args:
      mapping: source mapping, usually a JSON-style dictionary.

    Returns:
      The converted instance.
    """
    kwargs: dict[str, Any] = {}
    for field in dataclasses.fields(cls):
      json_field = field.metadata.get(_FIELD, field.name)
      value = mapping.get(json_field)
      mapper = field.metadata.get(_MAPPER, lambda x: x)
      if value is not None:
        kwargs[field.name] = mapper(value)
      elif field.default is not dataclasses.MISSING:
        kwargs[field.name] = field.default
      else:
        kwargs[field.name] = None
    return cls(**kwargs)


@dataclasses.dataclass
class JsonDeserializableEvent(JsonDeserializable):
  """Base class for JSON deserializable events."""

  EVENT_NAME: ClassVar[str]
  creation_time: datetime.datetime = dataclasses.field(
      default_factory=datetime.datetime.now,
      kw_only=True,
      repr=False,
      hash=False,
      compare=False,
  )


@dataclasses.dataclass
class GattDescriptor:
  """android.bluetooth.BluetoothGattDescriptor.

  Attributes:
    uuid: Descriptor UUID in string format.
    permissions: Descriptor permissions.
  """

  uuid: str
  permissions: android_constants.GattCharacteristicPermission

  @classmethod
  def from_mapping(cls: Type[Self], mapping: Mapping[str, Any]) -> Self:
    """Creates an instance deserialized from a JSON-style mapping.

    Args:
      mapping: source mapping, usually a JSON-style dictionary.

    Returns:
      The converted GattDescriptor instance.
    """
    return cls(
        uuid=mapping[snippet_constants.FIELD_UUID],
        permissions=android_constants.GattCharacteristicPermission(
            mapping[snippet_constants.GATT_FIELD_PERMISSIONS]
        ),
    )


@dataclasses.dataclass
class GattCharacteristic:
  """android.bluetooth.BluetoothGattCharacteristic.

  Attributes:
    uuid: Characteristic UUID in string format.
    properties: Characteristic properties.
    permissions: Characteristic permissions.
    handle: Characteristic handle. In GATT server mode, this attribute might be
      ignored by the API.
    descriptors: A list of GATT Descriptors included in this characteristic.
  """

  uuid: str
  properties: android_constants.GattCharacteristicProperty
  permissions: android_constants.GattCharacteristicPermission
  handle: int = 0
  descriptors: Sequence[GattDescriptor] = ()

  @classmethod
  def from_mapping(cls, mapping: Mapping[str, Any]) -> GattCharacteristic:
    """Creates an instance deserialized from a JSON-style mapping.

    Args:
      mapping: source mapping, usually a JSON-style dictionary.

    Returns:
      The converted GattCharacteristic instance.
    """
    return GattCharacteristic(
        uuid=mapping[snippet_constants.FIELD_UUID],
        handle=mapping[snippet_constants.FIELD_HANDLE],
        properties=android_constants.GattCharacteristicProperty(
            mapping[snippet_constants.GATT_FIELD_PROPERTIES]
        ),
        permissions=android_constants.GattCharacteristicPermission(
            mapping[snippet_constants.GATT_FIELD_PERMISSIONS]
        ),
        descriptors=[
            GattDescriptor.from_mapping(descriptor)
            for descriptor in mapping.get(
                snippet_constants.GATT_FIELD_DESCRIPTORS, []
            )
        ],
    )


@dataclasses.dataclass
class GattService:
  """android.bluetooth.BluetoothGattService.

  Attributes:
    uuid: Service UUID in string format.
    characteristics: A list of GATT Characteristics included in this service.
    type: Service type, PRIMARY or SECONDARY.
    handle: Service handle. In GATT server mode, this attribute might be ignored
      by the API.
    included_services: A list of GATT Services included in this service.
  """

  uuid: str
  characteristics: Sequence[GattCharacteristic] = ()
  type: android_constants.GattServiceType = (
      android_constants.GattServiceType.PRIMARY
  )
  handle: int | None = None
  included_services: Sequence[GattService] = ()

  @classmethod
  def from_mapping(cls, mapping: Mapping[str, Any]) -> GattService:
    """Creates an instance deserialized from a JSON-style mapping.

    Args:
      mapping: source mapping, usually a JSON-style dictionary.

    Returns:
      The converted GattService instance.
    """
    return GattService(
        uuid=mapping[snippet_constants.FIELD_UUID],
        handle=mapping[snippet_constants.FIELD_HANDLE],
        type=android_constants.GattServiceType(
            mapping[snippet_constants.GATT_FIELD_TYPE]
        ),
        characteristics=[
            GattCharacteristic.from_mapping(characteristic)
            for characteristic in mapping[
                snippet_constants.GATT_FIELD_CHARACTERISTICS
            ]
        ],
        included_services=[
            GattService.from_mapping(included_service)
            for included_service in mapping.get(
                snippet_constants.GATT_FIELD_INCLUDED_SERVICES, []
            )
        ],
    )


@dataclasses.dataclass
class KeyEvent(JsonDeserializableEvent):
  """android.app.Activity.dispatchKeyEvent."""

  key_code: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.KEY_EVENT_FIELD_KEY_CODE}
  )
  action: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.KEY_EVENT_FIELD_ACTION}
  )

  EVENT_NAME = snippet_constants.KEY_EVENT


@dataclasses.dataclass
class MotionEvent(JsonDeserializableEvent):
  """android.app.Activity.onGenericMotionEvent."""

  action: android_constants.MotionAction = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.KEY_EVENT_FIELD_ACTION,
          _MAPPER: android_constants.MotionAction,
      }
  )
  button_state: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.MOTION_EVENT_FIELD_BUTTON_STATE}
  )
  x: float = dataclasses.field(
      metadata={_FIELD: snippet_constants.MOTION_EVENT_FIELD_X}
  )
  y: float = dataclasses.field(
      metadata={_FIELD: snippet_constants.MOTION_EVENT_FIELD_Y}
  )

  EVENT_NAME = snippet_constants.MOTION_EVENT


@dataclasses.dataclass
class AclConnected(JsonDeserializableEvent):
  """android.bluetooth.device.action.ACL_CONNECTED.

  Attributes:
    address: MAC address of remote device in string format.
    transport: transport of the connected connection.
  """

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  transport: android_constants.Transport = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_TRANSPORT,
          _MAPPER: android_constants.Transport,
      }
  )

  EVENT_NAME = snippet_constants.ACL_CONNECTED


@dataclasses.dataclass
class AclDisconnected(JsonDeserializableEvent):
  """android.bluetooth.device.action.ACL_DISCONNECTED.

  Attributes:
    address: MAC address of remote device in string format.
    transport: transport of the disconnected connection.
  """

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  transport: android_constants.Transport = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_TRANSPORT,
          _MAPPER: android_constants.Transport,
      }
  )

  EVENT_NAME = snippet_constants.ACL_DISCONNECTED


@dataclasses.dataclass
class BondStateChanged(JsonDeserializableEvent):
  """android.bluetooth.device.action.BOND_STATE_CHANGED.

  Attributes:
    address: MAC address of remote device in string format.
    state: new bond state of remote device.
  """

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  state: android_constants.BondState = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_STATE,
          _MAPPER: android_constants.BondState,
      }
  )

  EVENT_NAME = snippet_constants.BOND_STATE_CHANGE


@dataclasses.dataclass
class EncryptionChanged(JsonDeserializableEvent):
  """android.bluetooth.device.action.ENCRYPTION_CHANGED.

  Attributes:
    address: MAC address of remote device in string format.
    transport: transport of the encryption changed connection.
  """

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  transport: android_constants.Transport = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_TRANSPORT,
          _MAPPER: android_constants.Transport,
      }
  )

  EVENT_NAME = snippet_constants.ENCRYPTION_CHANGE


@dataclasses.dataclass
class KeyMissing(JsonDeserializableEvent):
  """android.bluetooth.device.action.KEY_MISSING.

  Attributes:
    address: MAC address of remote device in string format.
  """

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )

  EVENT_NAME = snippet_constants.KEY_MISSING


@dataclasses.dataclass
class UuidChanged(JsonDeserializableEvent):
  """android.bluetooth.device.action.UUID."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  uuids: list[str] | None = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_UUID}
  )

  EVENT_NAME = snippet_constants.UUID_CHANGED


@dataclasses.dataclass
class A2dpPlayingStateChanged(JsonDeserializableEvent):
  """android.bluetooth.a2dp.profile.action.PLAYING_STATE_CHANGED."""

  EVENT_NAME = snippet_constants.A2DP_PLAYING_STATE_CHANGED

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  state: android_constants.A2dpState = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_STATE,
          _MAPPER: android_constants.A2dpState,
      }
  )


@dataclasses.dataclass
class HeadTrackerAvailableChanged(JsonDeserializableEvent):
  """OnHeadTrackerAvailableListener.onHeadTrackerAvailableChanged."""

  EVENT_NAME = snippet_constants.HEAD_TRACKER_AVAILABLE_CHANGED

  available: bool = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_STATE}
  )


@dataclasses.dataclass
class BluetoothCodecType(JsonDeserializable):
  """android.bluetooth.BluetoothCodecType."""

  id: int = dataclasses.field(metadata={_FIELD: snippet_constants.FIELD_ID})
  name: str = dataclasses.field(metadata={_FIELD: snippet_constants.FIELD_NAME})


@dataclasses.dataclass
class A2dpCodecConfiguration(JsonDeserializable):
  """android.bluetooth.BluetoothCodecConfig."""

  codec_type: android_constants.A2dpCodecType | None = dataclasses.field(
      default=None,
      metadata={
          _FIELD: snippet_constants.CODEC_TYPE,
          _MAPPER: android_constants.A2dpCodecType,
      },
  )
  channel_mode: android_constants.A2dpChannelMode | None = dataclasses.field(
      default=None,
      metadata={
          _FIELD: snippet_constants.CHANNEL_MODE,
          _MAPPER: android_constants.A2dpChannelMode,
      },
  )
  priority: int | None = dataclasses.field(
      default=None, metadata={_FIELD: snippet_constants.PRIORITY}
  )
  extended_codec_type: BluetoothCodecType | None = dataclasses.field(
      default=None,
      metadata={
          _FIELD: snippet_constants.EXTENDED_CODEC_TYPE,
          _MAPPER: BluetoothCodecType.from_mapping,
      },
  )
  sample_rate: android_constants.A2dpSampleRate | None = dataclasses.field(
      default=None,
      metadata={
          _FIELD: snippet_constants.SAMPLE_RATE,
          _MAPPER: android_constants.A2dpSampleRate,
      },
  )
  bits_per_sample: android_constants.A2dpBitsPerSample | None = (
      dataclasses.field(
          default=None,
          metadata={
              _FIELD: snippet_constants.BITS_PER_SAMPLE,
              _MAPPER: android_constants.A2dpBitsPerSample,
          },
      )
  )
  codec_specific_1: int | None = dataclasses.field(
      default=None,
      metadata={_FIELD: snippet_constants.CODEC_SPECIFIC_1},
  )
  codec_specific_2: int | None = dataclasses.field(
      default=None,
      metadata={_FIELD: snippet_constants.CODEC_SPECIFIC_2},
  )
  codec_specific_3: int | None = dataclasses.field(
      default=None,
      metadata={_FIELD: snippet_constants.CODEC_SPECIFIC_3},
  )
  codec_specific_4: int | None = dataclasses.field(
      default=None,
      metadata={_FIELD: snippet_constants.CODEC_SPECIFIC_4},
  )


@dataclasses.dataclass
class A2dpCodecConfigChanged(JsonDeserializableEvent):
  """android.bluetooth.a2dp.profile.action.CODEC_CONFIG_CHANGED."""

  EVENT_NAME = snippet_constants.A2DP_CODEC_CONFIG_CHANGED

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  codec_config: A2dpCodecConfiguration = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.CODEC_CONFIG,
          _MAPPER: A2dpCodecConfiguration.from_mapping,
      }
  )


@dataclasses.dataclass
class AdapterStateChanged(JsonDeserializableEvent):
  """android.bluetooth.adapter.action.STATE_CHANGED.

  Attributes:
    state: new state of the Bluetooth adapter.
  """

  state: android_constants.AdapterState = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_STATE,
          _MAPPER: android_constants.AdapterState,
      }
  )

  EVENT_NAME = snippet_constants.ADAPTER_STATE_CHANGED


@dataclasses.dataclass
class DiscoveryStarted(JsonDeserializableEvent):
  """android.bluetooth.adapter.action.DISCOVERY_STARTED."""

  EVENT_NAME = snippet_constants.DISCOVERY_STARTED


@dataclasses.dataclass
class DiscoveryFinished(JsonDeserializableEvent):
  """android.bluetooth.adapter.action.DISCOVERY_FINISHED."""

  EVENT_NAME = snippet_constants.DISCOVERY_FINISHED


@dataclasses.dataclass
class PairingRequest(JsonDeserializableEvent):
  """android.bluetooth.device.action.PAIRING_REQUEST.

  Attributes:
    address: mac address of remote device in string format.
    variant: variant of pairing procedure.
    pin: pairing confirmation pin code.
  """

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  variant: android_constants.PairingVariant = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_VARIANT,
          _MAPPER: android_constants.PairingVariant,
      }
  )
  pin: int = dataclasses.field(metadata={_FIELD: snippet_constants.FIELD_PIN})

  EVENT_NAME = snippet_constants.PAIRING_REQUEST


@dataclasses.dataclass
class DeviceFound(JsonDeserializableEvent):
  """android.bluetooth.device.action.FOUND.

  Attributes:
    address: MAC address of remote device in string format.
    name: name of remote device.
    rssi: RSSI of remote device.
  """

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  name: str | None = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_NAME}
  )

  rssi: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_RSSI, _MAPPER: int}
  )

  EVENT_NAME = snippet_constants.DEVICE_FOUND


@dataclasses.dataclass
class DeviceNameChanged(JsonDeserializableEvent):
  """android.bluetooth.device.action.NAME_CHANGED.

  Attributes:
    address: MAC address of remote device in string format.
    name: name of remote device.
  """

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  name: str = dataclasses.field(metadata={_FIELD: snippet_constants.FIELD_NAME})

  EVENT_NAME = snippet_constants.NAME_CHANGED


@dataclasses.dataclass
class AudioDeviceAdded(JsonDeserializableEvent):
  """android.media.AudioDeviceCallback.onAudioDevicesAdded.

  Attributes:
    address: MAC address of remote device in string format.
    device_type: type of audio device.
    is_source: whether the audio device is a source (input) device.
    is_sink: whether the audio device is a sink (output) device.
  """

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  device_type: android_constants.AudioDeviceType = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_TYPE,
          _MAPPER: android_constants.AudioDeviceType,
      }
  )
  is_source: bool = dataclasses.field(
      default=False,
      metadata={_FIELD: snippet_constants.FIELD_IS_SOURCE},
  )
  is_sink: bool = dataclasses.field(
      default=False,
      metadata={_FIELD: snippet_constants.FIELD_IS_SINK},
  )

  EVENT_NAME = snippet_constants.AUDIO_DEVICE_ADDED


@dataclasses.dataclass
class AudioDeviceRemoved(JsonDeserializableEvent):
  """android.media.AudioDeviceCallback.onAudioDevicesRemoved.

  Attributes:
    address: MAC address of remote device in string format.
    device_type: type of audio device.
    is_source: whether the audio device is a source (input) device.
    is_sink: whether the audio device is a sink (output) device.
  """

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  device_type: android_constants.AudioDeviceType = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_TYPE,
          _MAPPER: android_constants.AudioDeviceType,
      }
  )
  is_source: bool = dataclasses.field(
      default=False,
      metadata={_FIELD: snippet_constants.FIELD_IS_SOURCE},
  )
  is_sink: bool = dataclasses.field(
      default=False,
      metadata={_FIELD: snippet_constants.FIELD_IS_SINK},
  )

  EVENT_NAME = snippet_constants.AUDIO_DEVICE_REMOVED


@dataclasses.dataclass
class AudioDeviceInfo:
  """android.media.AudioDeviceInfo."""

  address: str
  device_type: android_constants.AudioDeviceType
  is_source: bool = False
  is_sink: bool = False

  @classmethod
  def from_mapping(cls, mapping: Mapping[str, Any]) -> AudioDeviceInfo:
    return cls(
        address=mapping[snippet_constants.FIELD_DEVICE],
        device_type=android_constants.AudioDeviceType(
            mapping[snippet_constants.FIELD_TYPE]
        ),
        is_source=mapping.get(snippet_constants.FIELD_IS_SOURCE, False),
        is_sink=mapping.get(snippet_constants.FIELD_IS_SINK, False),
    )


@dataclasses.dataclass
class CommunicationDeviceChanged(JsonDeserializableEvent):
  """android.media.AudioManager.OnCommunicationDeviceChangedListener.onCommunicationDeviceChanged."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  device_type: android_constants.AudioDeviceType = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_TYPE,
          _MAPPER: android_constants.AudioDeviceType,
      }
  )

  EVENT_NAME = snippet_constants.AUDIO_COMMUNICATION_DEVICE_CHANGED


@dataclasses.dataclass
class GattConnectionStateChanged(JsonDeserializableEvent):
  """android.bluetooth.BluetoothGattCallback.onConnectionStateChange.

  Attributes:
    state: new state of GATT connection.
    status: status or reason of state transition.
  """

  state: android_constants.ConnectionState = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_STATE,
          _MAPPER: android_constants.ConnectionState,
      }
  )
  status: android_constants.GattStatus = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_STATUS,
          _MAPPER: android_constants.GattStatus,
      }
  )

  EVENT_NAME = snippet_constants.GATT_CONNECTION_STATE_CHANGE


@dataclasses.dataclass
class GattCharacteristicReadRequest(JsonDeserializableEvent):
  """android.bluetooth.BluetoothGattServerCallback.onCharacteristicReadRequest.

  Attributes:
    address: MAC address of target device in string format.
    characteristic_uuid: Characteristic UUID in string format.
    request_id: request ID required by send_response method.
    offset: offset of value in the request.
  """

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  characteristic_uuid: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_UUID}
  )
  request_id: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.GATT_FIELD_REQUEST_ID}
  )
  offset: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.GATT_FIELD_OFFSET}
  )

  EVENT_NAME = snippet_constants.GATT_SERVER_CHARACTERISTIC_READ_REQUEST


@dataclasses.dataclass
class GattCharacteristicWriteRequest(JsonDeserializableEvent):
  """android.bluetooth.BluetoothGattServerCallback.onCharacteristicWriteRequest.

  Attributes:
    address: MAC address of target device in string format.
    characteristic_uuid: Characteristic UUID in string format.
    request_id: request ID required by send_response method.
    offset: offset of value in the request.
    value: what the remote wants to write.
    response_needed: whether response is required for this request.
    prepared_write: whether this is a prepared write.
  """

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  characteristic_uuid: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_UUID}
  )
  request_id: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.GATT_FIELD_REQUEST_ID}
  )
  offset: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.GATT_FIELD_OFFSET}
  )
  value: bytes = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_VALUE, _MAPPER: bytes}
  )
  response_needed: bool = dataclasses.field(
      metadata={_FIELD: snippet_constants.GATT_FIELD_RESPONSE_NEEDED}
  )
  prepared_write: bool = dataclasses.field(
      metadata={_FIELD: snippet_constants.GATT_FIELD_PREPARED_WRITE}
  )

  EVENT_NAME = snippet_constants.GATT_SERVER_CHARACTERISTIC_WRITE_REQUEST


@dataclasses.dataclass
class GattDescriptorWriteRequest(JsonDeserializableEvent):
  """android.bluetooth.BluetoothGattServerCallback.onDescriptorWriteRequest.

  Attributes:
    address: MAC address of target device in string format.
    characteristic_handle: handle of characteristic.
    descriptor_uuid: Descriptor UUID in string format.
    request_id: request ID required by send_response method.
    offset: offset of value in the request.
    value: what the remote wants to write.
    response_needed: whether response is required for this request.
    prepared_write: whether this is a prepared write.
  """

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  characteristic_handle: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_HANDLE}
  )
  descriptor_uuid: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_UUID}
  )
  request_id: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.GATT_FIELD_REQUEST_ID}
  )
  offset: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.GATT_FIELD_OFFSET}
  )
  value: bytes = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_VALUE, _MAPPER: bytes}
  )
  response_needed: bool = dataclasses.field(
      metadata={_FIELD: snippet_constants.GATT_FIELD_RESPONSE_NEEDED}
  )
  prepared_write: bool = dataclasses.field(
      metadata={_FIELD: snippet_constants.GATT_FIELD_PREPARED_WRITE}
  )

  EVENT_NAME = snippet_constants.GATT_SERVER_DESCRIPTOR_WRITE_REQUEST


@dataclasses.dataclass
class GattDescriptorReadRequest(JsonDeserializableEvent):
  """android.bluetooth.BluetoothGattServerCallback.onDescriptorReadRequest."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  descriptor_uuid: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_UUID}
  )
  characteristic_handle: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_HANDLE}
  )
  request_id: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.GATT_FIELD_REQUEST_ID}
  )
  offset: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.GATT_FIELD_OFFSET}
  )

  EVENT_NAME = snippet_constants.GATT_SERVER_DESCRIPTOR_READ_REQUEST


@dataclasses.dataclass
class GattExecuteWrite(JsonDeserializableEvent):
  """android.bluetooth.BluetoothGattServerCallback.onExecuteWrite."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  request_id: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.GATT_FIELD_REQUEST_ID}
  )
  execute: bool = dataclasses.field(
      metadata={_FIELD: snippet_constants.GATT_FIELD_EXECUTE}
  )

  EVENT_NAME = snippet_constants.GATT_SERVER_EXECUTE_WRITE


@dataclasses.dataclass
class GattServerMtuChanged(JsonDeserializableEvent):
  """android.bluetooth.BluetoothGattServerCallback.onMtuChanged."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  mtu: int = dataclasses.field(metadata={_FIELD: snippet_constants.FIELD_MTU})

  EVENT_NAME = snippet_constants.GATT_SERVER_MTU_CHANGED


@dataclasses.dataclass
class GattCharacteristicChanged(JsonDeserializableEvent):
  """android.bluetooth.BluetoothGattCallback.onCharacteristicChanged."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  handle: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_HANDLE}
  )
  value: bytes = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_VALUE, _MAPPER: bytes}
  )

  EVENT_NAME = snippet_constants.GATT_CHARACTERISTIC_CHANGED


@dataclasses.dataclass
class GattDescriptorRead(JsonDeserializableEvent):
  """android.bluetooth.BluetoothGattCallback.onDescriptorRead."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  handle: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_HANDLE}
  )
  uuid: str = dataclasses.field(metadata={_FIELD: snippet_constants.FIELD_UUID})
  status: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_STATUS}
  )
  value: bytes = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_VALUE, _MAPPER: bytes}
  )

  EVENT_NAME = snippet_constants.GATT_DESCRIPTOR_READ


@dataclasses.dataclass
class GattDescriptorWrite(JsonDeserializableEvent):
  """android.bluetooth.BluetoothGattCallback.onDescriptorWrite."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  handle: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_HANDLE}
  )
  uuid: str = dataclasses.field(metadata={_FIELD: snippet_constants.FIELD_UUID})
  status: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_STATUS}
  )

  EVENT_NAME = snippet_constants.GATT_DESCRIPTOR_WRITE


@dataclasses.dataclass
class GattReliableWriteCompleted(JsonDeserializableEvent):
  """android.bluetooth.BluetoothGattCallback.onReliableWriteCompleted."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  status: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_STATUS}
  )

  EVENT_NAME = snippet_constants.GATT_RELIABLE_WRITE_COMPLETED


@dataclasses.dataclass
class GattServiceChanged(JsonDeserializableEvent):
  """android.bluetooth.BluetoothGattCallback.onServiceChanged."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )

  EVENT_NAME = snippet_constants.GATT_SERVICE_CHANGED


@dataclasses.dataclass
class GattSubrateChanged(JsonDeserializableEvent):
  """android.bluetooth.BluetoothGattCallback.onSubrateChange."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  subrate_mode: android_constants.LeSubrateMode = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.GATT_FIELD_SUBRATE_MODE,
          _MAPPER: android_constants.LeSubrateMode,
      }
  )
  status: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_STATUS}
  )

  EVENT_NAME = snippet_constants.GATT_SUBRATE_CHANGED


@dataclasses.dataclass
class VolumeChanged(JsonDeserializableEvent):
  """android.media.VOLUME_CHANGED_ACTION.

  Attributes:
    stream_type: type of stream.
    volume_value: index of volume for stream_type.
  """

  stream_type: android_constants.StreamType = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_TYPE,
          _MAPPER: android_constants.StreamType,
      }
  )
  volume_value: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_VALUE}
  )

  EVENT_NAME = snippet_constants.VOLUME_CHANGED


@dataclasses.dataclass
class MuteChanged(JsonDeserializableEvent):
  """Event for audiomanager.ACTION_MICROPHONE_MUTE_CHANGED when mute state changes.

  Attributes:
    is_mute: whether the microphone is muted.
  """

  is_mute: bool = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_STATE}
  )

  EVENT_NAME = snippet_constants.MUTE_CHANGED


@dataclasses.dataclass
class CallStateChanged(JsonDeserializableEvent):
  """android.telecom.Call.Callback.onStateChanged.

  Attributes:
    handle: uri handle of the call.
    name: displayed name of caller.
    state: state of the call.
  """

  handle: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_HANDLE}
  )
  name: str = dataclasses.field(metadata={_FIELD: snippet_constants.FIELD_NAME})
  state: android_constants.CallState = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_STATE,
          _MAPPER: android_constants.CallState,
      }
  )

  EVENT_NAME = snippet_constants.CALL_STATE_CHANGED


@dataclasses.dataclass
class CallRemoved(JsonDeserializableEvent):
  """InCallService.onCallRemoved.

  Attributes:
    handle: uri handle of the call.
    name: displayed name of caller.
  """

  handle: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_HANDLE}
  )
  name: str = dataclasses.field(metadata={_FIELD: snippet_constants.FIELD_NAME})

  EVENT_NAME = snippet_constants.CALL_REMOVED


@dataclasses.dataclass
class ProfileConnectionStateChanged(JsonDeserializableEvent):
  """android.bluetooth.*.profile.action.CONNECTION_STATE_CHANGED."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  state: android_constants.ConnectionState = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_STATE,
          _MAPPER: android_constants.ConnectionState,
      }
  )

  EVENT_NAME = snippet_constants.PROFILE_CONNECTION_STATE_CHANGE


@dataclasses.dataclass
class HidDeviceAppStatusChanged(JsonDeserializableEvent):
  """BluetoothHidDevice.Callback#onAppStatusChanged.

  Attributes:
    address: MAC address of remote device in string format.
    registered: Whether the HID device app is registered.
  """

  address: str | None = dataclasses.field(
      default=None, metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  registered: bool = dataclasses.field(
      default=False, metadata={_FIELD: snippet_constants.FIELD_STATE}
  )

  EVENT_NAME = snippet_constants.HID_DEVICE_APP_STATUS_CHANGED


@dataclasses.dataclass
class HidDeviceGetReportRequest(JsonDeserializableEvent):
  """BluetoothHidDevice.Callback#onGetReport.

  Attributes:
    address: MAC address of remote device in string format.
    type: Report type.
    id: Report ID.
    buffer_size: Buffer size.
  """

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  type: int = dataclasses.field(metadata={_FIELD: snippet_constants.FIELD_TYPE})
  id: int = dataclasses.field(metadata={_FIELD: snippet_constants.FIELD_ID})
  buffer_size: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_BUFFER_SIZE}
  )

  EVENT_NAME = snippet_constants.HID_DEVICE_ON_GET_REPORT


@dataclasses.dataclass
class ProfileActiveDeviceChanged(JsonDeserializableEvent):
  """android.bluetooth.*.profile.action.ACTIVE_DEVICE_CHANGED."""

  address: str | None = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )

  EVENT_NAME = snippet_constants.ACTIVE_DEVICE_CHANGED


@dataclasses.dataclass
class HfpAgAudioStateChanged(JsonDeserializableEvent):
  """android.bluetooth.headset.profile.action.AUDIO_STATE_CHANGED."""

  EVENT_NAME = snippet_constants.HFP_AG_AUDIO_STATE_CHANGED

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  state: android_constants.ScoState = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_STATE,
          _MAPPER: android_constants.ScoState,
      }
  )


@dataclasses.dataclass
class HfpHfAudioStateChanged(JsonDeserializableEvent):
  """android.bluetooth.headsetclient.profile.action.AUDIO_STATE_CHANGED."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  state: android_constants.ConnectionState = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_STATE,
          _MAPPER: android_constants.ConnectionState,
      }
  )

  EVENT_NAME = snippet_constants.HFP_HF_AUDIO_STATE_CHANGED


@dataclasses.dataclass
class BatteryLevelChanged(JsonDeserializableEvent):
  """android.bluetooth.device.action.BATTERY_LEVEL_CHANGED."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  level: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_VALUE}
  )

  EVENT_NAME = snippet_constants.BATTERY_LEVEL_CHANGED


@dataclasses.dataclass
class BroadcastSourceFound(JsonDeserializableEvent):
  """android.bluetooth.BluetoothLeBroadcastAssistant.Callback.onSourceFound."""

  EVENT_NAME = snippet_constants.BASS_SOURCE_FOUND

  source: auracast_uri.BroadcastAudioUri = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_SOURCE,
          _MAPPER: auracast_uri.BroadcastAudioUri.from_string,
      }
  )


@dataclasses.dataclass
class PlayerIsPlayingChanged(JsonDeserializableEvent):
  """androidx.media3.common.Player.Listener.onIsPlayingChanged."""

  EVENT_NAME = snippet_constants.PLAYER_IS_PLAYING_CHANGED

  is_playing: bool = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_STATE}
  )


@dataclasses.dataclass
class PlayerMediaItemTransition(JsonDeserializableEvent):
  """androidx.media3.common.Player.Listener.onMediaItemTransition."""

  EVENT_NAME = snippet_constants.PLAYER_MEDIA_ITEM_TRANSITION

  media_item: MediaItem = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.MEDIA_ITEM,
          _MAPPER: lambda kwargs: MediaItem(**kwargs),
      }
  )


@dataclasses.dataclass
class PlayerShuffleModeEnabledChanged(JsonDeserializableEvent):
  """androidx.media3.common.Player.Listener.onShuffleModeEnabledChanged."""

  EVENT_NAME = snippet_constants.PLAYER_SHUFFLE_MODE_ENABLED_CHANGED

  enabled: bool = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_STATE}
  )


@dataclasses.dataclass
class PlayerRepeatModeChanged(JsonDeserializableEvent):
  """androidx.media3.common.Player.Listener.onRepeatModeChanged."""

  EVENT_NAME = snippet_constants.PLAYER_REPEAT_MODE_CHANGED

  mode: int = dataclasses.field(metadata={_FIELD: snippet_constants.MODE})


@dataclasses.dataclass
class TakStateChanged(JsonDeserializableEvent):
  """TakCallback.onTakSessionStatusChanged."""

  EVENT_NAME = snippet_constants.TAK_STATE_CHANGED

  device: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE})
  uuid: str = dataclasses.field(default='', metadata={_FIELD: 'uuid'})
  state: int = dataclasses.field(default=0, metadata={_FIELD: 'state'})
  status: int = dataclasses.field(default=0, metadata={_FIELD: 'status'})


@dataclasses.dataclass
class PositionDiscontinuity(JsonDeserializableEvent):

  EVENT_NAME = snippet_constants.POSITION_DISCONTINUITY

  old_position_ms: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.OLD_POSITION}
  )
  new_position_ms: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.NEW_POSITION}
  )


@dataclasses.dataclass
class DistanceMeasurementResult(JsonDeserializableEvent):
  """android.bluetooth.le.DistanceMeasurementSession.Callback.onResult."""

  result_meters: float | None = dataclasses.field(
      metadata={_FIELD: snippet_constants.RESULT_METERS}
  )
  error_meters: float | None = dataclasses.field(
      metadata={_FIELD: snippet_constants.ERROR_METERS}
  )
  azimuth_angle: float | None = dataclasses.field(
      metadata={_FIELD: snippet_constants.AZIMUTH_ANGLE}
  )
  error_azimuth_angle: float | None = dataclasses.field(
      metadata={_FIELD: snippet_constants.ERROR_AZIMUTH_ANGLE}
  )
  altitude_angle: float | None = dataclasses.field(
      metadata={_FIELD: snippet_constants.ALTITUDE_ANGLE}
  )
  error_altitude_angle: float | None = dataclasses.field(
      metadata={_FIELD: snippet_constants.ERROR_ALTITUDE_ANGLE}
  )
  delay_spread_meters: float | None = dataclasses.field(
      metadata={_FIELD: snippet_constants.DELAY_SPREAD_METERS}
  )
  confidence_level: float | None = dataclasses.field(
      metadata={_FIELD: snippet_constants.CONFIDENCE_LEVEL}
  )
  velocity_meters_per_second: float | None = dataclasses.field(
      metadata={_FIELD: snippet_constants.VELOCITY_METERS_PER_SECOND}
  )
  detected_attack_level: int | None = dataclasses.field(
      metadata={_FIELD: snippet_constants.DETECTED_ATTACK_LEVEL}
  )
  measurement_timestamp_nanos: int | None = dataclasses.field(
      metadata={_FIELD: snippet_constants.MEASUREMENT_TIMESTAMP_NANOS}
  )

  EVENT_NAME = snippet_constants.DISTANCE_MEASUREMENT_RESULT


@dataclasses.dataclass
class ScanResult(JsonDeserializableEvent):
  """android.bluetooth.le.ScanCallback.onScanResult."""

  primary_phy: int
  secondary_phy: int
  advertising_sid: int
  tx_power: int
  rssi: int
  periodic_advertising_interval: int
  timestamp_nanos: int

  advertising_data_flags: bluetooth_constants.AdvertisingDataFlags | None = None
  address: str | None = None
  device_name: str | None = None
  service_uuids: Sequence[str] | None = None
  service_solicitation_uuids: Sequence[str] | None = None
  service_data: dict[str, bytes] | None = None
  manufacturer_data: dict[int, bytes] | None = None

  EVENT_NAME = snippet_constants.SCAN_RESULT

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    # SCAN_RESULT events are serialized as a dictionary value of key
    # SCAN_RESULT, and BATCH_SCAN_RESULT events are serialized as a list of
    # dictionary values of key BATCH_SCAN_RESULTS.
    # We need to handle both cases here.
    if scan_result := mapping.get(snippet_constants.SCAN_RESULT):
      return cls.from_mapping(scan_result)

    raw_service_data = cast(
        dict[str, str] | None,
        mapping.get(snippet_constants.ADV_DATA_SERVICE_DATA),
    )
    raw_manufacturer_data = cast(
        dict[str, str] | None,
        mapping.get(snippet_constants.ADV_DATA_MANUFACTURER_DATA),
    )
    if raw_service_data is not None:
      service_data = {
          key: base64.b64decode(value)
          for key, value in cast(dict[str, str], raw_service_data).items()
      }
    else:
      service_data = None
    if raw_manufacturer_data is not None:
      manufacturer_data = {
          int(key): base64.b64decode(value)
          for key, value in cast(dict[str, str], raw_manufacturer_data).items()
      }
    else:
      manufacturer_data = None

    if (
        ad_flags_value := mapping.get(snippet_constants.ADV_DATA_FLAGS)
    ) is not None:
      ad_flags = bluetooth_constants.AdvertisingDataFlags(ad_flags_value)
    else:
      ad_flags = None

    return cls(
        address=mapping.get(snippet_constants.FIELD_DEVICE),
        primary_phy=mapping[snippet_constants.ADV_PARAMETER_PRIMARY_PHY],
        secondary_phy=mapping[snippet_constants.ADV_PARAMETER_SECONDARY_PHY],
        advertising_sid=mapping[snippet_constants.ADV_REPORT_SID],
        tx_power=mapping[snippet_constants.ADV_PARAMETER_TX_POWER_LEVEL],
        rssi=mapping[snippet_constants.ADV_REPORT_RSSI],
        periodic_advertising_interval=mapping[
            snippet_constants.ADV_REPORT_PA_INTERVAL
        ],
        timestamp_nanos=mapping[snippet_constants.ADV_REPORT_TIMESTAMP],
        advertising_data_flags=ad_flags,
        device_name=mapping.get(snippet_constants.FIELD_NAME),
        service_uuids=mapping.get(snippet_constants.ADV_DATA_SERVICE_UUID),
        service_solicitation_uuids=mapping.get(
            snippet_constants.ADV_DATA_SERVICE_SOLICITATION_UUIDS
        ),
        service_data=service_data,
        manufacturer_data=manufacturer_data,
    )


@dataclasses.dataclass
class BluetoothQualityReportReady(JsonDeserializableEvent):
  """android.bluetooth.BluetoothAdapter.BluetoothQualityReportReadyCallback.onBluetoothQualityReportReady."""

  @dataclasses.dataclass
  class Common:
    """Common fields for Bluetooth Quality Report."""

    packet_type: int
    connection_handle: int
    connection_role: int
    tx_power_level: int
    rssi: int
    snr: int
    unused_afh_channel_count: int
    afh_select_unideal_channel_count: int
    lsto: int
    piconet_clock: int
    retransmission_count: int
    no_rx_count: int
    nak_count: int
    last_tx_ack_timestamp: int
    flow_off_count: int
    last_flow_on_timestamp: int
    overflow_count: int
    underflow_count: int
    cal_failed_item_count: int
    # V6 fields
    tx_total_packets: int | None = None
    tx_unack_packets: int | None = None
    tx_flush_packets: int | None = None
    tx_last_subevent_packets: int | None = None
    crc_error_packets: int | None = None
    rx_dup_packets: int | None = None
    rx_un_recv_packets: int | None = None
    coex_info_mask: int | None = None

  device: str
  quality_report_id: int
  status: int
  common: Common | None

  EVENT_NAME = snippet_constants.BLUETOOTH_QUALITY_REPORT

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    report = mapping[snippet_constants.FIELD_REPORT]
    return cls(
        device=mapping[snippet_constants.FIELD_DEVICE],
        status=mapping[snippet_constants.FIELD_STATUS],
        quality_report_id=report[snippet_constants.FIELD_ID],
        common=(
            cls.Common(**report[snippet_constants.FIELD_COMMON])
            if snippet_constants.FIELD_COMMON in report
            else None
        ),
    )


@dataclasses.dataclass
class BatchScanResults(JsonDeserializableEvent):
  """android.bluetooth.le.ScanCallback.onBatchScanResults."""

  results: Sequence[ScanResult]

  EVENT_NAME = snippet_constants.BATCH_SCAN_RESULTS

  @override
  @classmethod
  def from_mapping(cls, mapping: Mapping[str, Any]) -> Self:
    results = mapping[snippet_constants.BATCH_SCAN_RESULTS]
    return cls(results=[ScanResult.from_mapping(result) for result in results])


@dataclasses.dataclass
class PresetInfoChanged(JsonDeserializableEvent):
  """android.bluetooth.BluetoothHapClient.Callback.onPresetInfoChanged."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  reason: android_constants.BluetoothStatusCode = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_REASON,
          _MAPPER: android_constants.BluetoothStatusCode,
      }
  )

  EVENT_NAME = snippet_constants.PRESET_INFO_CHANGED


@dataclasses.dataclass
class AicsDescriptionChanged(JsonDeserializableEvent):
  """android.bluetooth.AudioInputControl.Callback.onDescriptionChanged."""

  description: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.AICS_DESCRIPTION}
  )

  EVENT_NAME = snippet_constants.AICS_DESCRIPTION_CHANGED


@dataclasses.dataclass
class AicsStatusChanged(JsonDeserializableEvent):
  """android.bluetooth.AudioInputControl.Callback.onAudioInputStatusChanged."""

  status: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_STATUS}
  )

  EVENT_NAME = snippet_constants.AICS_STATUS_CHANGED


@dataclasses.dataclass
class AicsGainSettingChanged(JsonDeserializableEvent):
  """android.bluetooth.AudioInputControl.Callback.onGainSettingChanged."""

  gain_setting: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.AICS_GAIN_SETTING}
  )

  EVENT_NAME = snippet_constants.AICS_GAIN_SETTING_CHANGED


@dataclasses.dataclass
class AicsSetGainSettingFailed(JsonDeserializableEvent):
  """android.bluetooth.AudioInputControl.Callback.onSetGainSettingFailed."""

  EVENT_NAME = snippet_constants.AICS_SET_GAIN_SETTING_FAILED


@dataclasses.dataclass
class AicsMuteChanged(JsonDeserializableEvent):
  """android.bluetooth.AudioInputControl.Callback.onMuteChanged."""

  mute: int = dataclasses.field(metadata={_FIELD: snippet_constants.AICS_MUTE})

  EVENT_NAME = snippet_constants.AICS_MUTE_CHANGED


@dataclasses.dataclass
class AicsSetMuteFailed(JsonDeserializableEvent):
  """android.bluetooth.AudioInputControl.Callback.onSetMuteFailed."""

  EVENT_NAME = snippet_constants.AICS_SET_MUTE_FAILED


@dataclasses.dataclass
class AicsGainModeChanged(JsonDeserializableEvent):
  """android.bluetooth.AudioInputControl.Callback.onGainModeChanged."""

  gain_mode: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.AICS_GAIN_MODE}
  )

  EVENT_NAME = snippet_constants.AICS_GAIN_MODE_CHANGED


@dataclasses.dataclass
class AicsSetGainModeFailed(JsonDeserializableEvent):
  """android.bluetooth.AudioInputControl.Callback.onSetGainModeFailed."""

  EVENT_NAME = snippet_constants.AICS_SET_GAIN_MODE_FAILED


@dataclasses.dataclass
class VocsOffsetStateChanged(JsonDeserializableEvent):
  """android.bluetooth.BluetoothVolumeControl.Callback.onVolumeOffsetChanged."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  instance_id: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_INSTANCE_ID}
  )
  offset: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_OFFSET}
  )

  EVENT_NAME = snippet_constants.VOLUME_OFFSET_CHANGED


@dataclasses.dataclass
class VocsAudioLocationChanged(JsonDeserializableEvent):
  """android.bluetooth.BluetoothVolumeControl.Callback.onVolumeOffsetAudioLocationChanged."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  instance_id: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_INSTANCE_ID}
  )
  audio_location: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_AUDIO_LOCATION}
  )

  EVENT_NAME = snippet_constants.VOLUME_OFFSET_AUDIO_LOCATION_CHANGED


@dataclasses.dataclass
class VocsAudioDescriptionChanged(JsonDeserializableEvent):
  """android.bluetooth.BluetoothVolumeControl.Callback.onVolumeOffsetAudioDescriptionChanged."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  instance_id: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_INSTANCE_ID}
  )
  audio_description: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_AUDIO_DESCRIPTION}
  )

  EVENT_NAME = snippet_constants.VOLUME_OFFSET_AUDIO_DESCRIPTION_CHANGED


@dataclasses.dataclass
class DeviceVolumeChanged(JsonDeserializableEvent):
  """android.bluetooth.BluetoothVolumeControl.Callback.onDeviceVolumeChanged."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  volume: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_VOLUME}
  )

  EVENT_NAME = snippet_constants.DEVICE_VOLUME_CHANGED


@dataclasses.dataclass
class VoiceCommand(JsonDeserializableEvent):
  """android.intent.action.VOICE_COMMAND.

  Attributes:
    state: Whether the voice command is enabled or not.
  """

  state: bool
  EVENT_NAME = snippet_constants.VOICE_COMMAND


@dataclasses.dataclass
class HidHostHandshake(JsonDeserializableEvent):
  """HID Host handshake."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  status: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_STATUS}
  )
  EVENT_NAME = snippet_constants.HID_HOST_HANDSHAKE


@dataclasses.dataclass
class HidHostReport(JsonDeserializableEvent):
  """HID Host action report."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  report: bytes = dataclasses.field(
      metadata={
          _FIELD: snippet_constants.FIELD_REPORT,
          _MAPPER: bytes,
      }
  )
  EVENT_NAME = snippet_constants.HID_HOST_REPORT


@dataclasses.dataclass
class HidHostIdleTimeChanged(JsonDeserializableEvent):
  """HID Host action idle time changed."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  idle_time: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_IDLE_TIME}
  )
  EVENT_NAME = snippet_constants.HID_HOST_IDLE_TIME_CHANGED


@dataclasses.dataclass
class HidHostProtocolModeChanged(JsonDeserializableEvent):
  """HID Host action protocol mode."""

  address: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_DEVICE}
  )
  protocol_mode: int = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_PROTOCOL_MODE}
  )
  EVENT_NAME = snippet_constants.HID_HOST_PROTOCOL_MODE_CHANGED


@dataclasses.dataclass
class MediaItemAdded(JsonDeserializableEvent):
  """Media item added from MediaBrowserServiceSnippet."""

  media_id: str = dataclasses.field(
      metadata={_FIELD: snippet_constants.FIELD_ID}
  )
  EVENT_NAME = snippet_constants.MEDIA_ITEM_ADDED


@dataclasses.dataclass
class LegacyAdvertiseSettings:
  """android.bluetooth.le.AdvertiseSettings."""

  connectable: bool = True
  discoverable: bool = True
  own_address_type: int = android_constants.AddressTypeStatus.RANDOM
  timeout: int = 0
  tx_power_level: int = android_constants.LegacyTxPowerLevel.MEDIUM
  advertise_mode: int = android_constants.LegacyAdvertiseMode.LOW_POWER


@dataclasses.dataclass
class AdvertisingSetParameters:
  """android.bluetooth.le.AdvertisingSetParameters."""

  connectable: bool = False
  anonymous: bool = False
  include_tx_power_level: bool = False
  scannable: bool = False
  legacy: bool = False
  discoverable: bool = True
  interval: int = android_constants.AdvertisingInterval.LOW
  own_address_type: int = android_constants.AddressTypeStatus.RANDOM
  primary_phy: int = hci.Phy.LE_1M
  secondary_phy: int = hci.Phy.LE_1M
  tx_power_level: int = android_constants.ExtendedTxPowerLevel.MEDIUM


@dataclasses.dataclass
class PeriodicAdvertisingParameters:
  """android.bluetooth.le.PeriodicAdvertisingParameters."""

  interval: int
  include_tx_power_level: bool = False


@dataclasses.dataclass
class AdvertisingData:
  """android.bluetooth.le.AdvertiseData."""

  include_device_name: bool = False
  include_tx_power_level: bool = False
  service_uuids: Sequence[str] | None = None
  service_solicitation_uuids: Sequence[str] | None = None
  service_data: dict[str, bytes] | None = None
  manufacturer_data: dict[int, bytes] | None = None


@dataclasses.dataclass
class ScanFilter:
  """android.bluetooth.le.ScanFilter.

  Attributes:
    advertising_data_type: Advertising Data Type.
    name: Remote device mame.
    device: Remote device address.
    address_type: Remote device address type.
    irk: The IRK to use for resolving private addresses.
    service_uuids: Service UUID. Though it's called service_uuids, it actually
      means "search for an UUID in UUIDs".
    service_solicitation_uuids: Though it's called service_solicitation_uuids,
      it actually means "search for an UUID in UUIDs".
    service_data: Service data. Only 1 entry allowed.
    manufacturer_data: Manufacturer specific data. Only 1 entry allowed.
  """

  advertising_data_type: bluetooth_constants.AdvertisingDataType | None = None
  name: str | None = None
  device: str | None = None
  address_type: android_constants.AddressTypeStatus | None = None
  irk: bytes | None = None
  service_uuids: str | None = None
  service_solicitation_uuids: str | None = None
  service_data: dict[str, bytes] | None = None
  manufacturer_data: dict[int, bytes] | None = None


@dataclasses.dataclass
class ScanSettings:
  """android.bluetooth.le.ScanSettings."""

  scan_mode: android_constants.BleScanMode | None = None
  callback_type: android_constants.BleScanCallbackType | None = None
  match_mode: android_constants.BleScanMatchMode | None = None
  scan_result_type: android_constants.BleScanResultType | None = None
  phy: android_constants.Phy | None = None
  legacy: bool | None = None
  report_delay_millis: int | None = None


@dataclasses.dataclass
class DistanceMeasurementParameters:
  """android.bluetooth.le.DistanceMeasurementParams."""

  @dataclasses.dataclass
  class ChannelSoundingParameters:
    """Channel Sounding Parameters."""

    sight_type: int | None = None
    location_type: int | None = None
    security_level: int | None = None

  device: str
  duration: int | None = None
  frequency: int | None = None
  method_id: int | None = None
  channel_sounding_parameters: ChannelSoundingParameters | None = None


@dataclasses.dataclass
class OobData:
  """Out of Band Pairing Data."""

  confirmation_hash: bytes
  device_address_with_type: bytes
  randomizer_hash: bytes | None = None
  le_temporary_key: bytes | None = None
  le_device_role: int | None = None
  classic_length: int | None = None


@dataclasses.dataclass
class LegacyAdvertiser:
  """LE Legacy Advertiser control block."""

  cookie: str
  snippet: snippet_stub.BluetoothSnippet

  @classmethod
  async def create(
      cls: Type[Self],
      snippet: snippet_stub.BluetoothSnippet,
      advertise_settings: LegacyAdvertiseSettings,
      advertising_data: AdvertisingData | None = None,
      scan_response: AdvertisingData | None = None,
  ) -> Self:
    """Starts Legacy Advertising.

    Args:
      snippet: snippet client instance.
      advertise_settings: advertise settings.
      advertising_data: advertising data.
      scan_response: scan response.

    Returns:
      advertiser instance.

    Raises:
      RuntimeError: when advertising starts failed.
    """
    cookie = await asyncio.to_thread(
        lambda: snippet.startAdvertising(
            _make_json_object(advertise_settings),
            _make_json_object(advertising_data),
            _make_json_object(scan_response),
        ),
    )
    return cls(cookie=cookie, snippet=snippet)

  def stop(self) -> None:
    self.snippet.stopAdvertising(self.cookie)

  def __enter__(self) -> Self:
    return self

  def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
    with contextlib.suppress(mobly.snippet.errors.ApiError):
      self.stop()


@dataclasses.dataclass
class ExtendedAdvertisingSet:
  """LE Extended Advertising Set control block."""

  cookie: str
  snippet: snippet_stub.BluetoothSnippet

  @classmethod
  async def create(
      cls: Type[Self],
      snippet: snippet_stub.BluetoothSnippet,
      advertising_set_parameters: AdvertisingSetParameters,
      advertising_data: AdvertisingData | None = None,
      scan_response: AdvertisingData | None = None,
      periodic_advertising_parameters: PeriodicAdvertisingParameters | None = (
          None
      ),
      periodic_advertising_data: AdvertisingData | None = (None),
      duration: int = 0,
      max_extended_advertising_events: int = 0,
      gatt_server: GattServer | None = None,
  ) -> Self:
    """Starts an Extended Advertising Set.

    Args:
      snippet: snippet client instance.
      advertising_set_parameters: advertising set parameters.
      advertising_data: advertising data.
      scan_response: scan response.
      periodic_advertising_parameters: periodic advertising parameters.
      periodic_advertising_data: periodic advertising data.
      duration: advertising duration in 10ms units, 0 for no limit.
      max_extended_advertising_events: max extended advertising events, 0 for no
        limit.
      gatt_server: GATT server instance.

    Returns:
      advertiser instance.
    """
    gatt_server_callback_id = (
        gatt_server.handler.callback_id if gatt_server else None
    )
    cookie = await asyncio.to_thread(
        lambda: snippet.startAdvertisingSet(
            _make_json_object(advertising_set_parameters),
            _make_json_object(advertising_data),
            _make_json_object(scan_response),
            _make_json_object(periodic_advertising_parameters),
            _make_json_object(periodic_advertising_data),
            duration,
            max_extended_advertising_events,
            gatt_server_callback_id,
        ),
    )
    return cls(cookie=cookie, snippet=snippet)

  def stop(self) -> None:
    self.snippet.stopAdvertisingSet(self.cookie)

  def __enter__(self) -> Self:
    return self

  def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
    with contextlib.suppress(mobly.snippet.errors.ApiError):
      self.stop()


@dataclasses.dataclass
class LeAudioBroadcastSubgroupSettings:
  """LE Audio Broadcast Subgroup Settings."""

  quality: android_constants.LeAudioBroadcastQuality = (
      android_constants.LeAudioBroadcastQuality.STANDARD
  )
  language: str = 'eng'
  program: str = 'BL4A'


@dataclasses.dataclass
class AudioAttributes:
  """Android Audio Attributes.

  https://cs.android.com/android/platform/superproject/main/+/main:frameworks/base/media/java/android/media/AudioAttributes.java
  """

  class Usage(enum.IntEnum):
    """Audio Usage."""

    INVALID = -1
    UNKNOWN = 0
    MEDIA = 1
    VOICE_COMMUNICATION = 2
    VOICE_COMMUNICATION_SIGNALLING = 3
    ALARM = 4
    NOTIFICATION = 5
    NOTIFICATION_RINGTONE = 6
    NOTIFICATION_COMMUNICATION_REQUEST = 7
    NOTIFICATION_COMMUNICATION_INSTANT = 8
    NOTIFICATION_COMMUNICATION_DELAYED = 9
    NOTIFICATION_EVENT = 10
    ASSISTANCE_ACCESSIBILITY = 11
    ASSISTANCE_NAVIGATION_GUIDANCE = 12
    ASSISTANCE_SONIFICATION = 13
    GAME = 14
    VIRTUAL_SOURCE = 15
    ASSISTANT = 16
    CALL_ASSISTANT = 17

  class ContentType(enum.IntEnum):
    """Audio Content Type."""

    UNKNOWN = 0
    SPEECH = 1
    MUSIC = 2
    MOVIE = 3
    SONIFICATION = 4
    ULTRASOUND = 1997

  class Flag(enum.IntFlag):
    """Audio Flag."""

    AUDIBILITY_ENFORCED = 0x1 << 0
    HW_AV_SYNC = 0x1 << 4
    LOW_LATENCY = 0x1 << 8

  content_type: ContentType | None = None
  usage: Usage | None = None
  flags: int | None = None


@dataclasses.dataclass
class MediaItem:
  """Media3 Media Item, with children field for browsing."""

  id: str | None = None
  title: str | None = None
  artist: str | None = None
  album: str | None = None
  uri: str | None = None
  artwork_uri: str | None = None
  browsable: bool | None = None
  playable: bool | None = None
  children: Sequence[MediaItem] = ()


@dataclasses.dataclass
class LeAudioBroadcast:
  """LE Audio Broadcast control block."""

  broadcast_id: int
  snippet: snippet_stub.BluetoothSnippet

  @classmethod
  async def create(
      cls: Type[Self],
      snippet: snippet_stub.BluetoothSnippet,
      name: str | None = None,
      broadcast_code: bytes | None = None,
      is_public: bool = False,
      subgroups: Sequence[LeAudioBroadcastSubgroupSettings] = (),
  ) -> Self:
    """Starts an LE Audio Broadcast.

    Args:
      snippet: snippet client instance.
      name: Broadcast name.
      broadcast_code: Broadcast code.
      is_public: Whether the broadcast is public.
      subgroups: Subgroups of the broadcast. If empty, a default subgroup with
        the default quality, language and program will be used.

    Returns:
      Broadcast instance.

    Raises:
      RuntimeError: when broadcast starts failed.
    """
    subgroups = subgroups or [LeAudioBroadcastSubgroupSettings()]
    broadcast_id = await asyncio.to_thread(
        lambda: snippet.startBroadcast(
            list(broadcast_code) if broadcast_code else None,
            {
                snippet_constants.FIELD_NAME: name,
                snippet_constants.LEA_BROADCAST_FIELD_PUBLIC: is_public,
                snippet_constants.LEA_BROADCAST_FIELD_SUBGROUPS: [
                    _make_json_object(subgroup) for subgroup in subgroups
                ],
            },
        )
    )
    return cls(broadcast_id=broadcast_id, snippet=snippet)

  async def stop(self) -> None:
    """Stops the LE Audio Broadcast."""
    await asyncio.to_thread(
        lambda: self.snippet.stopBroadcast(self.broadcast_id)
    )


def find_characteristic_by_uuid(
    characteristic_uuid: str, services: Sequence[GattService]
) -> GattCharacteristic:
  """Finds a characteristic by UUID among the given services.

  Args:
    characteristic_uuid: UUID of the characteristic to find.
    services: Services to search.

  Returns:
    Found characteristic.

  Raises:
    NotFoundError: If the characteristic is not found.
  """
  characteristic = next(
      (
          characteristic
          for characteristic in itertools.chain.from_iterable(
              [service.characteristics for service in services]
          )
          if characteristic.uuid == characteristic_uuid
      ),
      None,
  )
  if not characteristic:
    raise errors.NotFoundError(
        f'Characteristic with {characteristic_uuid} not found.'
    )
  return characteristic


class PhoneCall(CallbackHandler):
  """Context managable phone call wrapper."""

  @dataclasses.dataclass
  class CallEndpoint(JsonDeserializable):
    """Call endpoint."""

    type: android_constants.CallEndpointType = dataclasses.field(
        metadata={
            _FIELD: snippet_constants.FIELD_TYPE,
            _MAPPER: android_constants.CallEndpointType,
        }
    )
    name: str = dataclasses.field(
        metadata={
            _FIELD: snippet_constants.FIELD_NAME,
        }
    )
    identifier: str = dataclasses.field(
        metadata={
            _FIELD: snippet_constants.FIELD_ID,
        }
    )

  @dataclasses.dataclass
  class CallEndpointChanged(JsonDeserializableEvent):
    """Call endpoint changed event."""

    EVENT_NAME = snippet_constants.CALL_ENDPOINT_CHANGED

    endpoint: PhoneCall.CallEndpoint = dataclasses.field(
        metadata={
            _FIELD: snippet_constants.FIELD_ENDPOINT,
            _MAPPER: lambda x: PhoneCall.CallEndpoint.from_mapping(x),  # pylint: disable=unnecessary-lambda
        }
    )

  def __init__(
      self,
      snippet: snippet_stub.BluetoothSnippet,
      caller_name: str,
      caller_number: str,
      direction: constants.Direction,
  ):
    """Class initializer.

    Args:
        snippet: snippet client instance.
        caller_name: Displayed name of caller.
        caller_number: Number of caller. e.g., "+16502530000"(Googleplex).
        direction: Direction of the phone call.
    """
    super().__init__(
        snippet=snippet,
        handler=snippet.addCall(
            caller_name,
            f'tel:{caller_number}',
            direction == constants.Direction.INCOMING,
        ),
        on_close=snippet.disconnectCall,
    )

  def answer(self) -> None:
    """Answers the phone call."""
    self.snippet.answerCall(self.handler.callback_id)

  async def request_call_endpoint_switch(
      self,
      endpoint_type: android_constants.CallEndpointType | None = None,
      endpoint_identifier: str | None = None,
  ) -> None:
    """Requests call endpoint switch."""
    if endpoint_type is None and endpoint_identifier is None:
      raise ValueError(
          'Either endpoint_type or endpoint_identifier must be set.'
      )
    await asyncio.to_thread(
        lambda: self.snippet.requestCallEndpointSwitch(
            self.handler.callback_id, endpoint_type, endpoint_identifier
        )
    )


@dataclasses.dataclass
class MediaBrowser:
  """Context managable Media Browser wrapper."""

  snippet: snippet_stub.BluetoothSnippet
  cookie: str

  @dataclasses.dataclass
  class PlaybackStateChanged(JsonDeserializableEvent):
    """Media playback state changed event."""

    state: android_constants.MediaPlaybackState = dataclasses.field(
        metadata={
            _FIELD: snippet_constants.FIELD_STATE,
            _MAPPER: android_constants.MediaPlaybackState,
        }
    )
    EVENT_NAME = snippet_constants.MEDIA_CONTROLLER_PLAYBACK_STATE_CHANGE

  @dataclasses.dataclass
  class MetadataChanged(JsonDeserializableEvent):
    """Media metadata changed event."""

    title: str | None = dataclasses.field(
        metadata={
            _FIELD: snippet_constants.FIELD_TITLE,
        }
    )
    artist: str | None = dataclasses.field(
        metadata={
            _FIELD: snippet_constants.FIELD_ARTIST,
        }
    )
    album: str | None = dataclasses.field(
        metadata={
            _FIELD: snippet_constants.FIELD_ALBUM,
        }
    )
    EVENT_NAME = snippet_constants.MEDIA_CONTROLLER_METADATA_CHANGE

  @dataclasses.dataclass
  class ShuffleModeChanged(JsonDeserializableEvent):
    """Browser shuffle mode changed event."""

    mode: android_constants.ShuffleMode = dataclasses.field(
        metadata={_FIELD: snippet_constants.MODE}
    )
    EVENT_NAME = snippet_constants.PLAYER_SHUFFLE_MODE_ENABLED_CHANGED

  @dataclasses.dataclass
  class RepeatModeChanged(JsonDeserializableEvent):
    """Browser repeat mode changed event."""

    mode: android_constants.RepeatMode = dataclasses.field(
        metadata={_FIELD: snippet_constants.MODE}
    )
    EVENT_NAME = snippet_constants.PLAYER_REPEAT_MODE_CHANGED

  async def get_root_media_item(self) -> str:
    """Gets the root id."""

    return await asyncio.to_thread(
        lambda: self.snippet.getMediaBrowserRootId(self.cookie)
    )

  async def get_children(self, parent_id: str) -> list[MediaItem]:
    """Gets the root id."""

    children = await asyncio.to_thread(
        lambda: self.snippet.getMediaBrowserChildren(self.cookie, parent_id)
    )
    return [MediaItem(**child) for child in children]

  def close(self) -> None:
    """Closes the phone call."""
    self.snippet.disconnectMediaBrowser(self.cookie)

  def play(self) -> None:
    """Plays the media."""
    self.snippet.playMediaController(self.cookie)

  def pause(self) -> None:
    """Pauses the media."""
    self.snippet.pauseMediaController(self.cookie)

  def stop(self) -> None:
    """Stops the media."""

    self.snippet.stopMediaController(self.cookie)

  def fast_forward(self) -> None:
    """Fast forwards the media."""
    self.snippet.fastForwardMediaController(self.cookie)

  def rewind(self) -> None:
    """Rewinds the media."""
    self.snippet.rewindMediaController(self.cookie)

  def skip_to_next(self) -> None:
    """Skips to the next media."""
    self.snippet.skipToNextMediaController(self.cookie)

  def skip_to_previous(self) -> None:
    """Skips to the previous media."""
    self.snippet.skipToPreviousMediaController(self.cookie)

  def register_callback(self) -> CallbackHandler:
    """Registers a media controller callback."""
    handler = self.snippet.registerMediaControllerCallback(self.cookie)
    return CallbackHandler(self.snippet, handler)

  def set_repeat_mode(self, repeat_mode: android_constants.RepeatMode) -> None:
    """Sets the repeat mode."""
    self.snippet.setMediaControllerRepeatMode(self.cookie, repeat_mode)

  def set_shuffle_mode(
      self, shuffle_mode: android_constants.ShuffleMode
  ) -> None:
    """Sets the shuffle mode."""
    self.snippet.setMediaControllerShuffleMode(self.cookie, shuffle_mode)

  def __enter__(self) -> Self:
    return self

  def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
    with contextlib.suppress(mobly.snippet.errors.ApiError):
      self.close()


class AudioRecorder:
  """Context managable AudioRecorder wrapper."""

  class Source(enum.IntEnum):
    """android.media.MediaRecorder.Source."""

    DEFAULT = 0
    MIC = 1
    VOICE_UPLINK = 2
    VOICE_DOWNLINK = 3
    VOICE_CALL = 4
    CAMCORDER = 5
    VOICE_RECOGNITION = 6
    VOICE_COMMUNICATION = 7
    REMOTE_SUBMIX = 8
    UNPROCESSED = 9
    VOICE_PERFORMANCE = 10
    ECHO_REFERENCE = 1997
    RADIO_TUNER = 1998
    HOTWORD = 1999
    ULTRASOUND = 2000

  def __init__(
      self,
      snippet: snippet_stub.BluetoothSnippet,
      path: str,
      source: Source,
      preferred_device_address: str | None = None,
      preferred_device_type: android_constants.AudioDeviceType | None = None,
  ):
    """Class initializer.

    Args:
        snippet: snippet client instance.
        path: Path on device to save the recorded media file.
        source: Source of the audio to record.
        preferred_device_address: Address of the preferred device.
        preferred_device_type: Type of the preferred device.
    """
    self.snippet = snippet
    self.path = path
    snippet.startRecording(
        path, source, preferred_device_address, preferred_device_type
    )

  def close(self) -> None:
    """Closes the phone call."""
    self.snippet.stopRecording(self.path)

  def __enter__(self) -> Self:
    return self

  def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
    with contextlib.suppress(mobly.snippet.errors.ApiError):
      self.close()


@dataclasses.dataclass(frozen=True)
class BluetoothSocketSettings:
  """Wrapper for android.bluetooth.BluetoothSocketSettings.

  Attributes:
    socket_type: The type of the socket (e.g., SocketType.LE).
    encryption_required: Whether encryption is required for the connection.
    authentication_required: Whether authentication is required for the
      connection.
    l2cap_psm: The L2CAP PSM value.
    rfcomm_uuid: The RFCOMM UUID.
    rfcomm_service_name: The RFCOMM Service Name.
  """

  socket_type: android_constants.BluetoothSocketType = (
      android_constants.BluetoothSocketType.LE
  )
  encryption_required: bool = False
  authentication_required: bool = False
  l2cap_psm: int | None = None
  rfcomm_uuid: str | None = None
  rfcomm_service_name: str | None = None


@dataclasses.dataclass
class BluetoothServerSocket:
  """Wrapper for android.bluetooth.BluetoothServerSocket.

  Attributes:
    snippet: The snippet client.
    cookie: The cookie identifying the server socket.
    socket_type: The type of the socket.
  """

  snippet: snippet_stub.BluetoothSnippet
  cookie: str
  socket_type: android_constants.BluetoothSocketType

  @classmethod
  def listen_with_settings(
      cls,
      snippet: snippet_stub.BluetoothSnippet,
      settings: BluetoothSocketSettings,
  ) -> BluetoothServerSocket:
    """Opens a server socket using settings."""
    serialized_settings = _make_json_object(settings)
    cookie, allocated_port = snippet.socketListenWithSettings(
        serialized_settings
    )
    if settings.socket_type == android_constants.BluetoothSocketType.RFCOMM:
      return RfcommServerSocket(
          snippet=snippet,
          cookie=cookie,
          socket_type=settings.socket_type,
          uuid=settings.rfcomm_uuid,
      )
    else:
      return L2capServerSocket(
          snippet=snippet,
          cookie=cookie,
          socket_type=settings.socket_type,
          psm=allocated_port,
      )

  async def accept(self) -> BluetoothSocket:
    """Accepts a connection from the server socket."""
    socket_cookie = await asyncio.to_thread(
        self.snippet.socketAccept, self.cookie
    )
    return self._create_accepted_socket(socket_cookie)

  def _create_accepted_socket(self, socket_cookie: str) -> BluetoothSocket:
    raise NotImplementedError

  def close(self) -> None:
    """Closes the server socket."""
    with contextlib.suppress(mobly.snippet.errors.ApiError):
      self.snippet.socketClose(self.cookie)

  def __enter__(self) -> Self:
    return self

  def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
    self.close()


@dataclasses.dataclass
class L2capServerSocket(BluetoothServerSocket):
  """L2CAP server socket."""

  AUTO_ALLOCATE_PSM = -2

  psm: int | None = None

  @classmethod
  def listen(
      cls,
      snippet: snippet_stub.BluetoothSnippet,
      secure: bool,
      psm: int | None = None,
      transport: android_constants.Transport = android_constants.Transport.LE,
  ) -> L2capServerSocket:
    """Opens an L2CAP server."""
    if psm is None:
      psm = cls.AUTO_ALLOCATE_PSM

    cookie, allocated_psm = snippet.l2capOpenServer(secure, psm, transport)
    if transport == android_constants.Transport.CLASSIC:
      s_type = android_constants.BluetoothSocketType.L2CAP
    else:
      s_type = android_constants.BluetoothSocketType.LE
    return cls(
        snippet=snippet,
        cookie=cookie,
        socket_type=s_type,
        psm=allocated_psm,
    )

  def _create_accepted_socket(self, socket_cookie: str) -> L2capSocket:
    return L2capSocket(
        snippet=self.snippet,
        cookie=socket_cookie,
        socket_type=self.socket_type,
        psm=self.psm,
    )

  async def accept(self) -> L2capSocket:
    return cast(L2capSocket, await super().accept())


@dataclasses.dataclass
class RfcommServerSocket(BluetoothServerSocket):
  """RFCOMM server socket."""

  uuid: str | None = None

  @classmethod
  def listen(
      cls,
      snippet: snippet_stub.BluetoothSnippet,
      secure: bool,
      uuid: str,
  ) -> RfcommServerSocket:
    """Opens an RFCOMM server."""
    cookie = snippet.rfcommOpenServer(secure, uuid)
    return cls(
        snippet=snippet,
        cookie=cookie,
        socket_type=android_constants.BluetoothSocketType.RFCOMM,
        uuid=uuid,
    )

  def _create_accepted_socket(self, socket_cookie: str) -> RfcommSocket:
    return RfcommSocket(
        snippet=self.snippet,
        cookie=socket_cookie,
        socket_type=self.socket_type,
        uuid=self.uuid,
    )

  async def accept(self) -> RfcommSocket:
    return cast(RfcommSocket, await super().accept())


@dataclasses.dataclass
class BluetoothSocket:
  """Wrapper for android.bluetooth.BluetoothSocket and BluetoothServerSocket."""

  snippet: snippet_stub.BluetoothSnippet
  cookie: str
  socket_type: android_constants.BluetoothSocketType

  @classmethod
  async def connect_with_settings(
      cls,
      snippet: snippet_stub.BluetoothSnippet,
      address: str,
      settings: BluetoothSocketSettings,
      address_type: android_constants.AddressTypeStatus = android_constants.AddressTypeStatus.RANDOM,
      retry_count: int = _DEFAULT_RETRY_COUNT,
  ) -> BluetoothSocket:
    """Connects to a socket using settings."""
    serialized_settings = _make_json_object(settings)

    @retry.retry_on_exception(
        initial_delay_sec=_DEFAULT_RETRY_DELAY_SECONDS,
        num_retries=retry_count,
    )
    async def inner() -> BluetoothSocket:
      try:
        cookie = await asyncio.to_thread(
            snippet.socketConnectWithSettings,
            address,
            address_type,
            serialized_settings,
        )
        if settings.socket_type == android_constants.BluetoothSocketType.RFCOMM:
          return RfcommSocket(
              snippet=snippet,
              cookie=cookie,
              socket_type=settings.socket_type,
              uuid=settings.rfcomm_uuid,
          )
        else:
          return L2capSocket(
              snippet=snippet,
              cookie=cookie,
              socket_type=settings.socket_type,
              psm=settings.l2cap_psm,
          )
      except mobly.snippet.errors.ApiError as e:
        raise errors.ConnectionError(
            f'Unable to connect socket to {address} with settings {settings}'
        ) from e

    return await inner()

  async def close(self) -> None:
    """Closes the socket."""
    with contextlib.suppress(mobly.snippet.errors.ApiError):
      await asyncio.to_thread(self.snippet.socketClose, self.cookie)

  async def read(self, length: int | None = None) -> bytes:
    """Reads data from the socket."""
    return base64.b64decode(
        await asyncio.to_thread(self.snippet.socketRead, self.cookie, length)
    )

  async def write(self, data: bytes) -> None:
    """Writes data to the socket."""
    await asyncio.to_thread(
        self.snippet.socketWrite,
        self.cookie,
        base64.b64encode(data).decode('utf-8'),
    )

  def available(self) -> int:
    """Checks available bytes on the socket."""
    return self.snippet.socketAvailable(self.cookie)

  def is_connected(self) -> bool:
    """Checks if the socket is connected."""
    return self.snippet.socketIsConnected(self.cookie)

  async def wait_for_connected(
      self,
      timeout: datetime.timedelta = datetime.timedelta(
          seconds=_DEFAULT_CONNECTION_TIMEOUT_SECONDS
      ),
  ) -> None:
    """Waits for async connection to complete."""
    try:
      await asyncio.to_thread(
          self.snippet.socketWaitForConnectionComplete,
          self.cookie,
          int(timeout.total_seconds() * 1000),
      )
    except mobly.snippet.errors.ApiError as e:
      raise errors.ConnectionError(
          f'Unable to connect socket {self.socket_type}'
      ) from e

  async def __aenter__(self) -> Self:
    return self

  async def __aexit__(
      self, exc_type: Any, exc_value: Any, traceback: Any
  ) -> None:
    await self.close()


@dataclasses.dataclass
class L2capSocket(BluetoothSocket):
  """L2CAP client socket."""

  psm: int | None = None

  @classmethod
  async def connect(
      cls,
      snippet: snippet_stub.BluetoothSnippet,
      address: str,
      secure: bool,
      psm: int,
      address_type: android_constants.AddressTypeStatus = android_constants.AddressTypeStatus.RANDOM,
      transport: android_constants.Transport = android_constants.Transport.LE,
      retry_count: int = _DEFAULT_RETRY_COUNT,
  ) -> L2capSocket:
    """Connects to an L2CAP channel."""

    @retry.retry_on_exception(
        initial_delay_sec=_DEFAULT_RETRY_DELAY_SECONDS,
        num_retries=retry_count,
    )
    async def inner() -> L2capSocket:
      try:
        cookie = await asyncio.to_thread(
            snippet.l2capConnect,
            address,
            secure,
            psm,
            address_type,
            transport,
            True,
        )
        if transport == android_constants.Transport.CLASSIC:
          s_type = android_constants.BluetoothSocketType.L2CAP
        else:
          s_type = android_constants.BluetoothSocketType.LE
        return cls(snippet=snippet, cookie=cookie, socket_type=s_type, psm=psm)
      except mobly.snippet.errors.ApiError as e:
        raise errors.ConnectionError(
            f'Unable to connect L2CAP to {address} with PSM {psm}'
        ) from e

    return await inner()

  @classmethod
  def connect_async(
      cls,
      snippet: snippet_stub.BluetoothSnippet,
      address: str,
      secure: bool,
      psm: int,
      address_type: android_constants.AddressTypeStatus = android_constants.AddressTypeStatus.RANDOM,
      transport: android_constants.Transport = android_constants.Transport.LE,
  ) -> L2capSocket:
    """Connects to an L2CAP channel asynchronously."""
    cookie = snippet.l2capConnect(
        address, secure, psm, address_type, transport, False
    )
    if transport == android_constants.Transport.CLASSIC:
      s_type = android_constants.BluetoothSocketType.L2CAP
    else:
      s_type = android_constants.BluetoothSocketType.LE
    return cls(snippet=snippet, cookie=cookie, socket_type=s_type, psm=psm)


@dataclasses.dataclass
class RfcommSocket(BluetoothSocket):
  """RFCOMM client socket."""

  uuid: str | None = None

  @classmethod
  async def connect(
      cls,
      snippet: snippet_stub.BluetoothSnippet,
      address: str,
      secure: bool,
      uuid: str,
      retry_count: int = _DEFAULT_RETRY_COUNT,
  ) -> RfcommSocket:
    """Connects to an RFCOMM channel."""

    @retry.retry_on_exception(
        initial_delay_sec=_DEFAULT_RETRY_DELAY_SECONDS,
        num_retries=retry_count,
    )
    async def inner() -> RfcommSocket:
      try:
        cookie = await asyncio.to_thread(
            snippet.rfcommConnect, address, secure, uuid, True
        )
        return cls(
            snippet=snippet,
            cookie=cookie,
            socket_type=android_constants.BluetoothSocketType.RFCOMM,
            uuid=uuid,
        )
      except mobly.snippet.errors.ApiError as e:
        raise errors.ConnectionError('Unable to connect RFCOMM') from e

    return await inner()

  @classmethod
  def connect_async(
      cls,
      snippet: snippet_stub.BluetoothSnippet,
      address: str,
      secure: bool,
      uuid: str,
  ) -> RfcommSocket:
    """Connects to an RFCOMM channel asynchronously."""
    cookie = snippet.rfcommConnect(address, secure, uuid, False)
    return cls(
        snippet=snippet,
        cookie=cookie,
        socket_type=android_constants.BluetoothSocketType.RFCOMM,
        uuid=uuid,
    )


@dataclasses.dataclass
class GattClient(CallbackHandler):
  """GATT client control block."""

  @classmethod
  async def connect(
      cls: Type[Self],
      snippet: snippet_stub.BluetoothSnippet,
      address: str,
      transport: int,
      address_type: android_constants.AddressTypeStatus = android_constants.AddressTypeStatus.RANDOM,
      retry_count: int = _DEFAULT_RETRY_COUNT,
  ) -> Self:
    """Connects services and returns discovered services.

    Args:
      snippet: snippet client instance.
      address: address of target device.
      transport: transport to use (Classic or LE).
      address_type: address type of target device (if LE transport is used).
      retry_count: allowed retry count of connect attempts.

    Returns:
      GATT client callback instance.

    Raises:
      ConnectionError: GATT is not connected after allowed retry counts.
    """

    @retry.retry_on_exception(
        initial_delay_sec=_DEFAULT_RETRY_DELAY_SECONDS,
        num_retries=retry_count,
    )
    async def inner():
      gatt_cb = snippet.gattConnect(address, transport, address_type)
      instance = cls(snippet=snippet, handler=gatt_cb)
      event = await instance.wait_for_event(GattConnectionStateChanged)
      if (
          event.state == android_constants.ConnectionState.CONNECTED
          and event.status == android_constants.GattStatus.SUCCESS
      ):
        return instance

      _logger.error('GATT connection failed, status=%r', event.status)
      snippet.gattDisconnect(gatt_cb.callback_id)
      snippet.gattClose(gatt_cb.callback_id)

      raise errors.ConnectionError('Unable to connect GATT')

    return await inner()

  async def disconnect(self) -> None:
    """Disconnects a GATT client.

    Raises:
      ConnectionError: GATT is not disconnected successfully.
    """
    self.snippet.gattDisconnect(self.handler.callback_id)
    event = await self.wait_for_event(
        GattConnectionStateChanged,
        lambda e: e.state == android_constants.ConnectionState.DISCONNECTED,
    )
    if (
        event.state != android_constants.ConnectionState.DISCONNECTED
        or event.status != android_constants.GattStatus.SUCCESS
    ):
      raise errors.ConnectionError(
          f'Unable to disconnect GATT, status={event.status!r}'
      )

  @override
  def close(self) -> None:
    """Closes a GATT client."""
    self.snippet.gattClose(self.handler.callback_id)

  async def get_services(self) -> list[GattService]:
    """Gets services discovered on given GATT Client.

    Returns:
      List of discovered GATT services.
    """
    services = await asyncio.to_thread(
        self.snippet.gattGetServices, self.handler.callback_id
    )
    return [GattService.from_mapping(service) for service in services]

  async def discover_services(self) -> list[GattService]:
    """Discovers services and returns discovered services.

    Returns:
      List of discovered GATT services.
    """
    asserts.assert_true(
        self.snippet.gattDiscoverServices(self.handler.callback_id),
        'Failed to discover services.',
    )
    discovered_event: callback_event.CallbackEvent = await asyncio.to_thread(
        self.handler.waitAndGet,
        snippet_constants.GATT_SERVICE_DISCOVERED,
        timeout=datetime.timedelta(seconds=30).total_seconds(),
    )
    asserts.assert_equal(
        discovered_event.data[snippet_constants.FIELD_STATUS],
        android_constants.GattStatus.SUCCESS,
    )
    return await self.get_services()

  async def read_characteristic(self, characteristic_handle: int) -> bytes:
    """Reads value of a characteristic.

    Args:
      characteristic_handle: characteristic handle.

    Returns:
      Value of characteristic in bytes.
    """
    asserts.assert_true(
        self.snippet.gattReadCharacteristic(
            self.handler.callback_id, characteristic_handle
        ),
        'Unable to read characteristic.',
    )
    read_event: callback_event.CallbackEvent = await asyncio.to_thread(
        self.handler.waitAndGet,
        snippet_constants.GATT_CHARACTERISTIC_READ,
        timeout=datetime.timedelta(seconds=30).total_seconds(),
    )
    asserts.assert_equal(
        read_event.data[snippet_constants.FIELD_STATUS],
        android_constants.GattStatus.SUCCESS,
    )
    return bytes(read_event.data[snippet_constants.FIELD_VALUE])

  async def write_characteristic(
      self,
      characteristic_handle: int,
      value: bytes,
      write_type: int,
  ) -> None:
    """Writes value to a characteristic.

    Args:
      characteristic_handle: characteristic handle.
      value: value to write in bytes.
      write_type: Type of write operation in one of DEFAULT(with response),
        NO_RESPONSE, SIGNED.
    """
    asserts.assert_equal(
        self.snippet.gattWriteCharacteristic(
            self.handler.callback_id,
            characteristic_handle,
            list(value),
            write_type,
        ),
        android_constants.GattStatus.SUCCESS,
    )
    if write_type != android_constants.GattWriteType.NO_RESPONSE:
      write_event: callback_event.CallbackEvent = await asyncio.to_thread(
          self.handler.waitAndGet,
          snippet_constants.GATT_CHARACTERISTIC_WRITE,
          timeout=datetime.timedelta(seconds=30).total_seconds(),
      )
      asserts.assert_equal(
          write_event.data[snippet_constants.FIELD_STATUS],
          android_constants.GattStatus.SUCCESS,
      )

  async def write_characteristic_long(
      self,
      characteristic_handle: int,
      value: bytes,
      mtu: int,
      write_type: int = android_constants.GattWriteType.NO_RESPONSE,
  ) -> None:
    """Writes a long value to a characteristic.

    Value might be split into multiple packets.

    Args:
      characteristic_handle: characteristic handle.
      value: value to write in bytes.
      mtu: Max transmission unit. (Segmented by host)
      write_type: Type of write operation in one of DEFAULT(with response),
        NO_RESPONSE, SIGNED.
    """
    await asyncio.to_thread(
        self.snippet.gattWriteCharacteristicLong,
        self.handler.callback_id,
        characteristic_handle,
        base64.b64encode(value).decode(),
        mtu,
        write_type,
    )

  async def subscribe_characteristic_notifications(
      self,
      characteristic_handle: int,
  ) -> None:
    """Subscribes notifications of a characteristic.

    Args:
      characteristic_handle: characteristic handle.
    """
    asserts.assert_true(
        self.snippet.gattSubscribeCharacteristic(
            self.handler.callback_id,
            characteristic_handle,
            True,
        ),
        'Unable to subscribe characteristic.',
    )
    asserts.assert_equal(
        self.snippet.gattWriteDescriptor(
            self.handler.callback_id,
            characteristic_handle,
            bluetooth_constants.BluetoothAssignedUuid.CLIENT_CHARACTERISTIC_CONFIGURATION_DESCRIPTOR,
            android_constants.GattDescriptorValue.ENABLE_NOTIFICATION,
        ),
        android_constants.GattStatus.SUCCESS,
    )
    # snippet.write
    write_event: callback_event.CallbackEvent = await asyncio.to_thread(
        self.handler.waitAndGet,
        snippet_constants.GATT_DESCRIPTOR_WRITE,
        timeout=datetime.timedelta(seconds=30).total_seconds(),
    )
    asserts.assert_equal(
        write_event.data[snippet_constants.FIELD_STATUS],
        android_constants.GattStatus.SUCCESS,
    )

  async def read_descriptor(
      self, characteristic_handle: int, descriptor_uuid: str
  ) -> bytes:
    """Reads value of a descriptor.

    Args:
      characteristic_handle: characteristic handle.
      descriptor_uuid: descriptor UUID.

    Returns:
      Value of descriptor in bytes.
    """
    asserts.assert_true(
        self.snippet.gattReadDescriptor(
            self.handler.callback_id, characteristic_handle, descriptor_uuid
        ),
        'Unable to read descriptor.',
    )
    event = await self.wait_for_event(GattDescriptorRead)
    asserts.assert_equal(
        event.status,
        android_constants.GattStatus.SUCCESS,
    )
    return event.value

  async def write_descriptor(
      self, characteristic_handle: int, descriptor_uuid: str, value: bytes
  ) -> None:
    """Writes value to a descriptor.

    Args:
      characteristic_handle: characteristic handle.
      descriptor_uuid: descriptor UUID.
      value: value to write in bytes.
    """
    asserts.assert_equal(
        self.snippet.gattWriteDescriptor(
            self.handler.callback_id,
            characteristic_handle,
            descriptor_uuid,
            list(value),
        ),
        android_constants.GattStatus.SUCCESS,
    )
    event = await self.wait_for_event(GattDescriptorWrite)
    asserts.assert_equal(
        event.status,
        android_constants.GattStatus.SUCCESS,
    )

  async def begin_reliable_write(self) -> None:
    """Begins a reliable write transaction."""
    asserts.assert_true(
        self.snippet.gattBeginReliableWrite(self.handler.callback_id),
        'Unable to begin reliable write.',
    )

  async def execute_reliable_write(self) -> None:
    """Executes the reliable write transaction."""
    self.handler.getAll(snippet_constants.GATT_RELIABLE_WRITE_COMPLETED)
    asserts.assert_true(
        self.snippet.gattExecuteReliableWrite(self.handler.callback_id),
        'Unable to execute reliable write.',
    )
    event = await self.wait_for_event(GattReliableWriteCompleted)
    asserts.assert_equal(
        event.status,
        android_constants.GattStatus.SUCCESS,
    )

  async def abort_reliable_write(self) -> None:
    """Aborts the reliable write transaction."""
    self.snippet.gattAbortReliableWrite(self.handler.callback_id)

  async def request_mtu(self, mtu: int) -> int:
    """Requests MTU of a GATT client.

    Args:
      mtu: Target MTU.

    Returns:
      Updated MTU.

    Raises:
      ConnectionError: Unable to request MTU.
    """
    # Clear all previous events.
    self.handler.getAll(snippet_constants.GATT_MTU_CHANGED)
    if not self.snippet.gattRequestMtu(self.handler.callback_id, mtu):
      raise errors.ConnectionError('Unable to request MTU')
    event: callback_event.CallbackEvent = await asyncio.to_thread(
        self.handler.waitAndGet,
        snippet_constants.GATT_MTU_CHANGED,
        timeout=datetime.timedelta(seconds=30).total_seconds(),
    )
    if (
        status := event.data[snippet_constants.FIELD_STATUS]
    ) != android_constants.GattStatus.SUCCESS:
      raise errors.ConnectionError(f'Unable to request MTU, status={status!r}')
    return event.data[snippet_constants.FIELD_MTU]

  async def set_preferred_phy(
      self, tx_phy: int, rx_phy: int, phy_options: int
  ) -> tuple[android_constants.Phy, android_constants.Phy]:
    """Sets preferred PHY of a GATT client.

    Args:
      tx_phy: Target TX PHY.
      rx_phy: Target RX PHY.
      phy_options: Target PHY options.

    Returns:
      Tuple of (new_tx_phy, new_rx_phy).

    Raises:
      ConnectionError: Unable to set preferred PHY.
    """
    # Clear all previous events.
    self.handler.getAll(snippet_constants.GATT_PHY_UPDATE)
    self.snippet.gattSetPreferredPhy(
        self.handler.callback_id, tx_phy, rx_phy, phy_options
    )
    event: callback_event.CallbackEvent = await asyncio.to_thread(
        self.handler.waitAndGet,
        snippet_constants.GATT_PHY_UPDATE,
        timeout=datetime.timedelta(seconds=30).total_seconds(),
    )
    if (
        status := event.data[snippet_constants.FIELD_STATUS]
    ) != android_constants.GattStatus.SUCCESS:
      raise errors.ConnectionError(
          f'Unable to set preferred PHY, status={status!r}'
      )
    return (
        android_constants.Phy(event.data[snippet_constants.FIELD_TX_PHY]),
        android_constants.Phy(event.data[snippet_constants.FIELD_RX_PHY]),
    )

  async def request_connection_priority(
      self, connection_priority: android_constants.ConnectionPriority
  ) -> android_constants.ConnectionPriority:
    """Requests connection priority.

    Args:
      connection_priority: Target connection priority.

    Returns:
      Updated connection priority.

    Raises:
      ConnectionError: Unable to request connection priority.
    """
    self.snippet.gattRequestConnectionPriority(
        self.handler.callback_id, connection_priority
    )
    return connection_priority

  async def request_subrate_mode(
      self, mode: android_constants.LeSubrateMode
  ) -> android_constants.LeSubrateMode:
    """Requests LE Subrate Mode.

    Args:
      mode: Target subrate mode.

    Returns:
      Updated subrate mode.

    Raises:
      ConnectionError: Unable to request subrate mode.
    """
    # Clear all previous events.
    self.handler.getAll(snippet_constants.GATT_SUBRATE_CHANGED)
    status = self.snippet.gattRequestSubrateMode(self.handler.callback_id, mode)
    if status != android_constants.GattStatus.SUCCESS:
      raise errors.ConnectionError(
          f'Unable to request subrate mode, status={status}'
      )
    event = await self.wait_for_event(GattSubrateChanged)
    if event.status != android_constants.GattStatus.SUCCESS:
      raise errors.ConnectionError(
          f'Unable to request subrate mode, status={event.status}'
      )
    return event.subrate_mode


_EVENT = TypeVar('_EVENT', bound=JsonDeserializableEvent)


class GattServer(CallbackHandler):
  """GATT server control block."""

  @classmethod
  def create(cls: Type[Self], snippet: snippet_stub.BluetoothSnippet) -> Self:
    """Creates a GATT server.

    Args:
      snippet: snippet client instance.

    Returns:
      Created GATT Server control block.
    """
    callback_handler = snippet.gattServerOpen()
    return cls(
        snippet=snippet,
        handler=callback_handler,
        on_close=snippet.gattServerClose,
    )

  async def add_service(self, service: GattService) -> None:
    """Adds a GATT service to GATT server.

    Args:
      service: GATT server metadata to be added.
    """
    asserts.assert_true(
        self.snippet.gattServerAddService(
            self.handler.callback_id,
            _make_json_object(service),
        ),
        'Unable to add service.',
    )
    service_added_event: callback_event.CallbackEvent = await asyncio.to_thread(
        self.handler.waitAndGet,
        snippet_constants.GATT_SERVER_SERVICE_ADDED,
        timeout=datetime.timedelta(seconds=30).total_seconds(),
    )
    asserts.assert_equal(
        service_added_event.data[snippet_constants.FIELD_STATUS],
        android_constants.GattStatus.SUCCESS,
    )

  async def remove_service(self, service_uuid: str) -> None:
    """Removes a GATT service from GATT server.

    Args:
      service_uuid: UUID of service to be removed.
    """
    asserts.assert_true(
        self.snippet.gattServerRemoveService(
            self.handler.callback_id,
            service_uuid,
        ),
        'Unable to remove service.',
    )

  @property
  def services(self) -> list[GattService]:
    """Gets registered GATT services on this server."""
    return [
        GattService.from_mapping(service)
        for service in self.snippet.gattServerGetServices(
            self.handler.callback_id
        )
    ]

  def send_response(
      self,
      address: str,
      request_id: int,
      status: int,
      value: bytes | list[int],
      offset: int = 0,
  ) -> None:
    """Sends a response to a GATT client.

    Args:
      address: address of device to send the response.
      request_id: Request to reply.
      status: status of the request operation.
      value: value of response.
      offset: offset of value.
    """
    self.snippet.gattServerSendResponse(
        self.handler.callback_id,
        address,
        request_id,
        status,
        offset,
        list(value),
    )

  def send_notification(
      self,
      address: str,
      characteristic_handle: int,
      confirm: bool,
      value: bytes | list[int],
  ) -> None:
    """Sends a notification to a GATT client.

    Args:
      address: address of device to send the notification.
      characteristic_handle: handle of the characteristic.
      confirm: whether confirmation from remote is required.
      value: value of notification.
    """
    self.snippet.gattServerSendNotification(
        self.handler.callback_id,
        address,
        characteristic_handle,
        confirm,
        list(value),
    )


@dataclasses.dataclass
class AudioInputControl(CallbackHandler):
  """Audio Input Control wrapper."""

  address: str = ''
  instance_id: int = 0

  @classmethod
  def create(
      cls: Type[Self],
      snippet: snippet_stub.BluetoothSnippet,
      address: str,
      instance_id: int,
  ) -> Self:
    """Creates an Audio Input Control callback handler.

    Args:
      snippet: Snippet instance.
      address: Address of target device.
      instance_id: Instance ID of the AICS.

    Returns:
      AudioInputControl instance.
    """
    callback_handler = snippet.registerAicsCallback(address, instance_id)
    return cls(
        snippet=snippet,
        handler=callback_handler,
        on_close=snippet.unregisterAicsCallback,
        address=address,
        instance_id=instance_id,
    )

  async def get_audio_input_type(self) -> int:
    """Gets the Audio Input Type."""
    return await asyncio.to_thread(
        self.snippet.aicsGetAudioInputType, self.address, self.instance_id
    )

  async def get_gain_setting_unit(self) -> int:
    """Gets the Gain Setting Units."""
    return await asyncio.to_thread(
        self.snippet.aicsGetGainSettingUnit, self.address, self.instance_id
    )

  async def get_gain_setting_min(self) -> int:
    """Gets the minimum Gain Setting."""
    return await asyncio.to_thread(
        self.snippet.aicsGetGainSettingMin, self.address, self.instance_id
    )

  async def get_gain_setting_max(self) -> int:
    """Gets the maximum Gain Setting."""
    return await asyncio.to_thread(
        self.snippet.aicsGetGainSettingMax, self.address, self.instance_id
    )

  async def get_description(self) -> str:
    """Gets the description."""
    return await asyncio.to_thread(
        self.snippet.aicsGetDescription, self.address, self.instance_id
    )

  async def is_description_writable(self) -> bool:
    """Checks if description is writable."""
    return await asyncio.to_thread(
        self.snippet.aicsIsDescriptionWritable, self.address, self.instance_id
    )

  async def set_description(self, description: str) -> bool:
    """Sets the description."""
    return await asyncio.to_thread(
        self.snippet.aicsSetDescription,
        self.address,
        self.instance_id,
        description,
    )

  async def get_audio_input_status(self) -> int:
    """Gets the Audio Input Status."""
    return await asyncio.to_thread(
        self.snippet.aicsGetAudioInputStatus, self.address, self.instance_id
    )

  async def get_gain_setting(self) -> int:
    """Gets the gain setting."""
    return await asyncio.to_thread(
        self.snippet.aicsGetGainSetting, self.address, self.instance_id
    )

  async def set_gain_setting(self, gain_setting: int) -> bool:
    """Sets the gain setting."""
    return await asyncio.to_thread(
        self.snippet.aicsSetGainSetting,
        self.address,
        self.instance_id,
        gain_setting,
    )

  async def get_gain_mode(self) -> int:
    """Gets the gain mode."""
    return await asyncio.to_thread(
        self.snippet.aicsGetGainMode, self.address, self.instance_id
    )

  async def set_gain_mode(self, gain_mode: int) -> bool:
    """Sets the gain mode."""
    return await asyncio.to_thread(
        self.snippet.aicsSetGainMode, self.address, self.instance_id, gain_mode
    )

  async def get_mute(self) -> int:
    """Gets the mute state."""
    return await asyncio.to_thread(
        self.snippet.aicsGetMute, self.address, self.instance_id
    )

  async def set_mute(self, mute: int) -> bool:
    """Sets the mute state."""
    return await asyncio.to_thread(
        self.snippet.aicsSetMute, self.address, self.instance_id, mute
    )


@dataclasses.dataclass
class Scanner(CallbackHandler):
  """LE Scanner control block."""

  snippet: snippet_stub.BluetoothSnippet
  handler: callback_handler_base.CallbackHandlerBase

  @classmethod
  def create(
      cls: Type[Self],
      snippet: snippet_stub.BluetoothSnippet,
      scan_filter: ScanFilter | None = None,
      scan_settings: ScanSettings | None = None,
  ) -> Self:
    """Start an LE scanner.

    Args:
      snippet: Snippet client instance.
      scan_filter: Parameters used to filter scan results. If not set, all scan
        results will be reported.
      scan_settings: Parameters used to scan. If not set, default value
        specified in android.bluetooth.le.ScanSettings will be used.

    Returns:
      Scanner control block.
    """
    handler = snippet.startScanning(
        _make_json_object(scan_filter),
        _make_json_object(scan_settings),
    )
    return cls(snippet=snippet, handler=handler)

  @override
  def close(self) -> None:
    self.snippet.stopScanning(self.handler.callback_id)


class DistanceMeasurement(CallbackHandler):
  """Distance Measurement control block."""

  @classmethod
  def create(
      cls: Type[Self],
      snippet: snippet_stub.BluetoothSnippet,
      parameters: DistanceMeasurementParameters,
  ) -> Self:
    """Start a distance measurement session.

    Args:
      snippet: Snippet client instance.
      parameters: Parameters used to start distance measurement.

    Returns:
      The distance measurement session.
    """
    handler = snippet.startDistanceMeasurement(_make_json_object(parameters))
    return cls(snippet=snippet, handler=handler)

  @override
  def close(self) -> None:
    self.snippet.stopDistanceMeasurement(self.handler.callback_id)


class TakSession(CallbackHandler):
  """TAK session control block."""

  @classmethod
  def create(
      cls: Type[Self],
      snippet: snippet_stub.BluetoothSnippet,
      address: str,
      key: list[int],
      uuid: str,
  ) -> Self:
    """Start a TAK session.

    Args:
      snippet: Snippet client instance.
      address: The MAC address of the remote device.
      key: The TAK key (list of 16 ints).
      uuid: The TAK UUID string.

    Returns:
      The TAK session.
    """
    handler = snippet.startTakSession(address, key, uuid)
    return cls(snippet=snippet, handler=handler)

  @override
  def close(self) -> None:
    pass


class SnippetWrapper:
  """Wrapper for BluetoothSnippet."""

  def __init__(self, snippet: snippet_stub.BluetoothSnippet) -> None:
    self.snippet = snippet

  def set_audio_attributes(
      self,
      attributes: AudioAttributes,
      handle_audio_focus: bool,
      player_id: str | None = None,
  ) -> None:
    """Sets audio attributes."""
    self.snippet.setAudioAttributes(
        _make_json_object(attributes), handle_audio_focus, player_id
    )

  def register_callback(self, module: Module) -> CallbackHandler:
    """Registers a callback for a module."""
    return CallbackHandler.for_module(self.snippet, module)

  def start_distance_measurement(
      self, parameters: DistanceMeasurementParameters
  ) -> DistanceMeasurement:
    """Starts a distance measurement session.

    Args:
      parameters: Parameters used to start distance measurement.

    Returns:
      The distance measurement session.
    """
    return DistanceMeasurement.create(self.snippet, parameters)

  def start_tak_session(
      self,
      address: str,
      key: bytes | list[int],
      uuid: str,
  ) -> TakSession:
    """Starts a TAK session."""
    if isinstance(key, bytes):
      key = list(key)
    return TakSession.create(self.snippet, address, key, uuid)

  def start_scanning(
      self,
      scan_filter: ScanFilter | None = None,
      scan_settings: ScanSettings | None = None,
  ) -> Scanner:
    """Starts an LE scanner.

    Args:
      scan_filter: Parameters used to filter scan results. If not set, all scan
        results will be reported.
      scan_settings: Parameters used to scan. If not set, default value
        specified in android.bluetooth.le.ScanSettings will be used.

    Returns:
      The scanner control block.
    """
    return Scanner.create(self.snippet, scan_filter, scan_settings)

  def create_gatt_server(self) -> GattServer:
    """Creates a GATT server.

    Returns:
      The GATT server control block.
    """
    return GattServer.create(self.snippet)

  def get_aics(self, address: str, instance_id: int) -> AudioInputControl:
    """Sets up an Audio Input Control session.

    Args:
      address: Address of target device.
      instance_id: Instance ID of the AICS.

    Returns:
      The Audio Input Control session.
    """
    return AudioInputControl.create(self.snippet, address, instance_id)

  async def connect_gatt_client(
      self,
      address: str,
      transport: int,
      address_type: android_constants.AddressTypeStatus = android_constants.AddressTypeStatus.RANDOM,
      retry_count: int = _DEFAULT_RETRY_COUNT,
  ) -> GattClient:
    """Connects to a GATT server.

    Args:
      address: Address of target device.
      transport: Transport to use (Classic or LE).
      address_type: Address type of target device (if LE transport is used).
      retry_count: Allowed retry count of connect attempts.

    Returns:
      The GATT client control block.
    """
    return await GattClient.connect(
        self.snippet, address, transport, address_type, retry_count
    )

  def create_l2cap_server(
      self,
      secure: bool,
      psm: int = L2capServerSocket.AUTO_ALLOCATE_PSM,
  ) -> L2capServerSocket:
    """Creates an L2CAP server.

    Args:
      secure: Whether encryption is required.
      psm: L2CAP channel number.

    Returns:
      The L2CAP server control block.
    """
    return L2capServerSocket.listen(self.snippet, secure, psm)

  async def create_l2cap_channel(
      self,
      address: str,
      secure: bool,
      psm: int,
      address_type: android_constants.AddressTypeStatus = android_constants.AddressTypeStatus.RANDOM,
      retry_count: int = _DEFAULT_RETRY_COUNT,
  ) -> L2capSocket:
    """Creates an L2CAP channel.

    Args:
      address: Address of target device.
      secure: Whether encryption is required.
      psm: L2CAP channel number.
      address_type: Address type of target device.
      retry_count: Allowed retry count of connect attempts.

    Returns:
      The L2CAP channel control block.
    """
    return await L2capSocket.connect(
        self.snippet,
        address,
        secure,
        psm,
        address_type,
        retry_count=retry_count,
    )

  def create_l2cap_channel_async(
      self,
      address: str,
      secure: bool,
      psm: int,
      address_type: android_constants.AddressTypeStatus = android_constants.AddressTypeStatus.RANDOM,
      transport: android_constants.Transport = android_constants.Transport.LE,
  ) -> L2capSocket:
    """Creates an L2CAP channel asynchronously.

    Args:
      address: Address of target device.
      secure: Whether encryption is required.
      psm: L2CAP channel number.
      address_type: Address type of target device.
      transport: Transport to use.

    Returns:
      The L2CAP channel control block.
    """
    return L2capSocket.connect_async(
        self.snippet, address, secure, psm, address_type, transport
    )

  async def create_socket_with_settings(
      self,
      address: str,
      settings: BluetoothSocketSettings,
      address_type: android_constants.AddressTypeStatus = android_constants.AddressTypeStatus.RANDOM,
      retry_count: int = _DEFAULT_RETRY_COUNT,
  ) -> BluetoothSocket:
    """Creates a socket using settings.

    Args:
      address: Address of target device.
      settings: Socket settings to use.
      address_type: Address type of target device.
      retry_count: Allowed retry count of connect attempts.

    Returns:
      The channel control block.
    """
    return await BluetoothSocket.connect_with_settings(
        self.snippet, address, settings, address_type, retry_count
    )

  def create_server_socket_with_settings(
      self,
      settings: BluetoothSocketSettings,
  ) -> BluetoothServerSocket:
    """Creates a server socket using settings.

    Args:
      settings: Socket settings to use.

    Returns:
      The server socket control block.
    """
    return BluetoothServerSocket.listen_with_settings(self.snippet, settings)

  def create_rfcomm_server(self, uuid: str, secure: bool) -> RfcommServerSocket:
    """Creates an RFCOMM server.

    Args:
      uuid: UUID of the RFCOMM service.
      secure: Whether encryption is required.

    Returns:
      The RFCOMM server control block.
    """
    return RfcommServerSocket.listen(self.snippet, secure, uuid)

  async def create_rfcomm_channel(
      self,
      address: str,
      secure: bool,
      uuid: str,
      retry_count: int = _DEFAULT_RETRY_COUNT,
  ) -> RfcommSocket:
    """Creates an RFCOMM channel.

    Args:
      address: Address of target device.
      secure: Whether encryption is required.
      uuid: UUID of the RFCOMM service.
      retry_count: Allowed retry count of connect attempts.

    Returns:
      The RFCOMM channel control block.
    """
    return await RfcommSocket.connect(
        self.snippet, address, secure, uuid, retry_count
    )

  def create_rfcomm_channel_async(
      self,
      address: str,
      secure: bool,
      uuid: str,
  ) -> RfcommSocket:
    """Creates an RFCOMM channel.

    Args:
      address: Address of target device.
      secure: Whether encryption is required.
      uuid: UUID of the RFCOMM service.

    Returns:
      The RFCOMM channel control block.
    """
    return RfcommSocket.connect_async(self.snippet, address, secure, uuid)

  async def start_legacy_advertiser(
      self,
      settings: LegacyAdvertiseSettings,
      advertising_data: AdvertisingData | None = None,
      scan_response: AdvertisingData | None = None,
  ) -> LegacyAdvertiser:
    """Starts a legacy advertiser.

    Args:
      settings: Advertising settings.
      advertising_data: Advertising data.
      scan_response: Scan response data.

    Returns:
      The legacy advertiser control block.
    """
    return await LegacyAdvertiser.create(
        self.snippet,
        settings,
        advertising_data,
        scan_response,
    )

  async def start_extended_advertising_set(
      self,
      advertising_set_parameters: AdvertisingSetParameters,
      advertising_data: AdvertisingData | None = None,
      scan_response: AdvertisingData | None = None,
      periodic_advertising_parameters: PeriodicAdvertisingParameters | None = (
          None
      ),
      periodic_advertising_data: AdvertisingData | None = (None),
      duration: int = 0,
      max_extended_advertising_events: int = 0,
      gatt_server: GattServer | None = None,
  ) -> ExtendedAdvertisingSet:
    """Starts an extended advertising set.

    Args:
      advertising_set_parameters: Advertising set parameters.
      advertising_data: Advertising data.
      scan_response: Scan response data.
      periodic_advertising_parameters: Periodic advertising parameters.
      periodic_advertising_data: Periodic advertising data.
      duration: advertising duration in 10ms units, 0 for ignore.
      max_extended_advertising_events: max extended advertising events, 0 for
        ignore.
      gatt_server: GATT server instance.

    Returns:
      The extended advertising set control block.
    """
    return await ExtendedAdvertisingSet.create(
        self.snippet,
        advertising_set_parameters,
        advertising_data,
        scan_response,
        periodic_advertising_parameters,
        periodic_advertising_data,
        duration,
        max_extended_advertising_events,
        gatt_server,
    )

  async def start_le_audio_broadcast(
      self,
      name: str | None = None,
      broadcast_code: bytes | None = None,
      is_public: bool = False,
      subgroups: Sequence[LeAudioBroadcastSubgroupSettings] = (),
  ) -> LeAudioBroadcast:
    """Starts LE Audio broadcasting.

    Args:
      name: Name of the broadcast.
      broadcast_code: Broadcast code.
      is_public: Whether the broadcast is public.
      subgroups: Subgroups of the broadcast.

    Returns:
      The LE Audio broadcast control block.
    """
    return await LeAudioBroadcast.create(
        self.snippet, name, broadcast_code, is_public, subgroups
    )

  def make_phone_call(
      self,
      caller_name: str,
      caller_number: str,
      direction: constants.Direction = constants.Direction.OUTGOING,
  ) -> PhoneCall:
    """Makes a phone call.

    Args:
      caller_name: The name of the caller.
      caller_number: The phone number of the caller.
      direction: The direction of the call.

    Returns:
      The phone call control block.
    """
    return PhoneCall(
        self.snippet,
        caller_name=caller_name,
        caller_number=caller_number,
        direction=direction,
    )

  async def start_audio_recording(
      self,
      path: str,
      source: AudioRecorder.Source = AudioRecorder.Source.DEFAULT,
      preferred_device_address: str | None = None,
      preferred_device_type: android_constants.AudioDeviceType | None = None,
  ) -> AudioRecorder:
    """Starts audio recording.

    Args:
      path: Path to the recording file.
      source: Source of the audio recording.
      preferred_device_address: Address of the preferred recording device.
      preferred_device_type: Type of the preferred recording device.

    Returns:
      The audio recorder control block.
    """
    return await asyncio.to_thread(
        lambda: AudioRecorder(
            self.snippet,
            path=path,
            source=source,
            preferred_device_address=preferred_device_address,
            preferred_device_type=preferred_device_type,
        )
    )

  def create_bond_oob(
      self,
      address: str,
      transport: android_constants.Transport,
      address_type: android_constants.AddressTypeStatus,
      p_192_data: OobData | None = None,
      p_256_data: OobData | None = None,
  ) -> bool:
    """Creates a bond with OOB data.

    Args:
      address: Address of target device.
      transport: Transport to use (Classic or LE).
      address_type: Address type of target device (if LE transport is used).
      p_192_data: OOB data for 192-bit key.
      p_256_data: OOB data for 256-bit key.

    Returns:
      True if the bond is created successfully.
    """
    return self.snippet.createBondOutOfBand(
        address,
        transport,
        address_type,
        _make_json_object(p_192_data),
        _make_json_object(p_256_data),
    )

  def generate_oob_data(self, transport: int) -> OobData:
    """Generates OOB data.

    Args:
      transport: Transport to use (Classic or LE).

    Returns:
      OOB data.
    """
    return OobData(
        **cast(
            dict[str, Any],
            {
                key: bytes(value) if isinstance(value, list) else value
                for key, value in self.snippet.generateLocalOobData(
                    transport
                ).items()
            },
        )
    )  # type: ignore[arg-type]

  def get_all_hap_preset_info(self, device: str) -> dict[int, str]:
    """Gets all HAP preset info.

    Args:
      device: Address of target device.

    Returns:
      A mapping of preset index to preset name.
    """
    return {
        int(index): name
        for index, name in self.snippet.getAllHapPresetInfo(device).items()
    }

  def register_voice_command_callback(self) -> CallbackHandler:
    """Registers a callback for voice command."""
    return CallbackHandler(
        snippet=self.snippet,
        handler=self.snippet.registerVoiceCommandCallback(),
        on_close=self.snippet.unregisterVoiceCommandCallback,
    )

  def register_hid_device_app(
      self,
      name: str = 'name',
      description: str = 'description',
      provider: str = 'provider',
      subclass: int = 0,
      descriptors: Sequence[int] = (),
  ) -> CallbackHandler:
    """Registers a hid device app and returns a callback handler."""
    sdp_settings = {
        snippet_constants.HID_DEVICE_APP_NAME: name,
        snippet_constants.HID_DEVICE_APP_DESCRIPTION: description,
        snippet_constants.HID_DEVICE_APP_PROVIDER: provider,
        snippet_constants.HID_DEVICE_APP_SUBCLASS: subclass,
        snippet_constants.HID_DEVICE_APP_DESCRIPTORS: list(descriptors),
    }
    return CallbackHandler(
        snippet=self.snippet,
        handler=self.snippet.registerHidDeviceApp(sdp_settings),
        on_close=lambda _: self.snippet.unregisterHidDeviceApp(),
    )

  def register_media_library_session(
      self, media_tree_root: MediaItem
  ) -> CallbackHandler:
    """Registers a media browser session."""
    return CallbackHandler(
        snippet=self.snippet,
        handler=self.snippet.registerMediaLibrarySession(
            _make_json_object(media_tree_root)
        ),
        on_close=self.snippet.unregisterMediaLibrarySession,
    )

  def connect_media_browser(
      self, package_name: str, service_name: str
  ) -> MediaBrowser:
    """Connects to a media browser."""
    return MediaBrowser(
        snippet=self.snippet,
        cookie=self.snippet.connectMediaBrowser(package_name, service_name),
    )

  def add_media_item(self, media_item: MediaItem) -> None:
    """Adds a media item to the media queue."""

    return self.snippet.addMediaItem(_make_json_object(media_item))

  def play_media_item(self, media_item: MediaItem) -> None:
    """Plays a media item."""

    return self.snippet.playMediaItem(_make_json_object(media_item))

  def set_a2dp_codec_config(
      self, address: str, codec_config: A2dpCodecConfiguration
  ) -> None:
    """Sets the A2DP codec config."""

    return self.snippet.setA2dpCodecConfig(
        address, _make_json_object(codec_config)
    )

  def get_available_call_endpoints(self) -> list[PhoneCall.CallEndpoint]:
    """Gets available call endpoints."""
    endpoints = self.snippet.getAvailableCallEndpoints()
    return [PhoneCall.CallEndpoint.from_mapping(e) for e in endpoints]

  def get_a2dp_supported_codec_types(self) -> list[BluetoothCodecType]:
    """Gets the A2DP supported codec types."""

    return [
        BluetoothCodecType.from_mapping(codec_type)
        for codec_type in self.snippet.getA2dpSupportedCodecTypes()
    ]
