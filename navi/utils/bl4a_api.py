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

"""Wrappers around complex bl4a APIs."""

from __future__ import annotations

import abc
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
from typing import Any, ClassVar, Self, Type, TypeVar

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
_DEFAULT_CALLBACK_TIMEOUT_SECONDS = 30.0


def _make_json_object(arg: Any) -> Any:
  """Converts an object to a JSON-style object."""
  if dataclasses.is_dataclass(arg):
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
  module: Module | None = None

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
      case Module.A2DP:
        handler = snippet.a2dpSetup()
      case Module.ADAPTER:
        handler = snippet.adapterSetup()
      case Module.HFP_AG:
        handler = snippet.hfpAgSetup()
      case Module.HFP_HF:
        handler = snippet.hfpHfSetup()
      case Module.TELECOM:
        handler = snippet.registerTelecomCallback()
      case Module.LE_AUDIO:
        handler = snippet.registerLeAudioCallback()
      case Module.INPUT:
        handler = snippet.registerInputEventCallback()
      case Module.HID_HOST:
        handler = snippet.registerHidHostCallback()
      case Module.PAN:
        handler = snippet.registerPanCallback()
      case Module.ASHA:
        handler = snippet.registerProfileCallback(
            android_constants.Profile.HEARING_AID
        )
      case Module.PBAP:
        handler = snippet.registerProfileCallback(
            android_constants.Profile.PBAP
        )
      case Module.MAP:
        handler = snippet.registerProfileCallback(android_constants.Profile.MAP)
      case Module.SAP:
        handler = snippet.registerProfileCallback(android_constants.Profile.SAP)
      case Module.BASS:
        handler = snippet.registerBassCallback()
      case Module.PLAYER:
        handler = snippet.registerPlayerListener()
      case _:
        raise ValueError(f'Unsupported module: {module}')
    return cls(snippet=snippet, handler=handler, module=module)

  def close(self) -> None:
    """Closes the callback handler."""
    match self.module:
      case Module.AUDIO:
        self.snippet.audioUnregisterCallback(self.handler.callback_id)
      case Module.A2DP:
        self.snippet.a2dpTeardown(self.handler.callback_id)
      case Module.ADAPTER:
        self.snippet.adapterTeardown(self.handler.callback_id)
      case Module.HFP_AG:
        self.snippet.hfpAgTeardown(self.handler.callback_id)
      case Module.HFP_HF:
        self.snippet.hfpHfTeardown(self.handler.callback_id)
      case Module.TELECOM:
        self.snippet.unregisterTelecomCallback(self.handler.callback_id)
      case Module.LE_AUDIO:
        self.snippet.unregisterLeAudioCallback(self.handler.callback_id)
      case Module.INPUT:
        self.snippet.unregisterInputEventCallback(self.handler.callback_id)
      case Module.HID_HOST:
        self.snippet.unregisterHidHostCallback(self.handler.callback_id)
      case Module.PAN:
        self.snippet.unregisterPanCallback(self.handler.callback_id)
      case Module.ASHA | Module.PBAP | Module.MAP | Module.SAP:
        self.snippet.unregisterProfileCallback(self.handler.callback_id)
      case Module.BASS:
        self.snippet.unregisterBassCallback(self.handler.callback_id)
      case Module.PLAYER:
        self.snippet.unregisterPlayerListener(self.handler.callback_id)
      case _:
        raise ValueError(f'Unsupported module: {self.module}')

  async def wait_for_event(
      self,
      callback_type: type[_EVENT],
      predicate: Callable[[_EVENT], bool] | None = None,
      timeout: datetime.timedelta | float = _DEFAULT_CALLBACK_TIMEOUT_SECONDS,
      timeout_msg: str | None = None,
  ) -> _EVENT:
    """Waits for a callback event that satisfies the predicate.

    Args:
      callback_type: type of callback.
      predicate: a function that takes a callback event metadata and returns a
        bool. If None is given, returns the first one.
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

    try:
      if predicate:
        event = await asyncio.to_thread(
            lambda: self.handler.waitForEvent(
                callback_type.EVENT_NAME,
                lambda e: predicate(callback_type.from_mapping(e.data)),
                timeout=timeout,
            )
        )
      else:
        event = await asyncio.to_thread(
            lambda: self.handler.waitAndGet(callback_type.EVENT_NAME, timeout)
        )
    except mobly.snippet.errors.CallbackHandlerTimeoutError:
      raise errors.AsyncTimeoutError(
          timeout_msg
          or (
              f'No event {callback_type.__name__}({callback_type.EVENT_NAME})'
              + (
                  f' matching `{inspect.getsource(predicate).strip()}`'
                  if predicate
                  else ''
              )
              + f' is received within {timeout} seconds.'
          )
      ) from None
    return callback_type.from_mapping(event.data)

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


class JsonDeserializableEvent(abc.ABC):
  """Base class for JSON deserializable objects."""

  EVENT_NAME: ClassVar[str]

  @classmethod
  @abc.abstractmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    """Creates an instance deserialized from a JSON-style mapping.

    Args:
      mapping: source mapping, usually a JSON-style dictionary.

    Returns:
      The converted instance.
    """
    raise NotImplementedError


@dataclasses.dataclass
class GattDescriptor:
  """Dataclass for GATT Descriptor metadata.

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
  """Dataclass for GATT Characteristic metadata.

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
  handle: int | None = None
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
  """Dataclass for GATT Service metadata.

  Attributes:
    uuid: Service UUID in string format.
    characteristics: A list of GATT Characteristics included in this service.
    handle: Service handle. In GATT server mode, this attribute might be ignored
      by the API.
  """

  uuid: str
  characteristics: Sequence[GattCharacteristic] = ()
  handle: int | None = None

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
        characteristics=[
            GattCharacteristic.from_mapping(characteristic)
            for characteristic in mapping[
                snippet_constants.GATT_FIELD_CHARACTERISTICS
            ]
        ],
    )


@dataclasses.dataclass
class KeyEvent(JsonDeserializableEvent):
  key_code: int
  action: int

  EVENT_NAME: ClassVar[str] = snippet_constants.KEY_EVENT

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        key_code=mapping[snippet_constants.KEY_EVENT_FIELD_KEY_CODE],
        action=mapping[snippet_constants.KEY_EVENT_FIELD_ACTION],
    )


@dataclasses.dataclass
class MotionEvent(JsonDeserializableEvent):
  action: android_constants.MotionAction
  button_state: android_constants.Button
  x: float
  y: float

  EVENT_NAME: ClassVar[str] = snippet_constants.MOTION_EVENT

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        action=android_constants.MotionAction(
            mapping[snippet_constants.KEY_EVENT_FIELD_ACTION]
        ),
        button_state=android_constants.Button(
            mapping[snippet_constants.MOTION_EVENT_FIELD_BUTTON_STATE]
        ),
        x=mapping[snippet_constants.MOTION_EVENT_FIELD_X],
        y=mapping[snippet_constants.MOTION_EVENT_FIELD_Y],
    )


@dataclasses.dataclass
class AclConnected(JsonDeserializableEvent):
  """Dataclass for ACL Connected event metadata.

  Attributes:
    address: mac address of remote device in string format.
    transport: transport of the connected connection.
    EVENT_NAME: ClassVar, callback event name.
  """

  address: str
  transport: android_constants.Transport

  EVENT_NAME: ClassVar[str] = snippet_constants.ACL_CONNECTED

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        address=mapping[snippet_constants.FIELD_DEVICE],
        transport=android_constants.Transport(
            mapping[snippet_constants.FIELD_TRANSPORT]
        ),
    )


@dataclasses.dataclass
class AclDisconnected(JsonDeserializableEvent):
  """Dataclass for ACL Disconnected event metadata.

  Attributes:
    address: mac address of remote device in string format.
    transport: transport of the disconnected connection.
    EVENT_NAME: ClassVar, callback event name.
  """

  address: str
  transport: android_constants.Transport

  EVENT_NAME: ClassVar[str] = snippet_constants.ACL_DISCONNECTED

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        address=mapping[snippet_constants.FIELD_DEVICE],
        transport=android_constants.Transport(
            mapping[snippet_constants.FIELD_TRANSPORT]
        ),
    )


@dataclasses.dataclass
class BondStateChanged(JsonDeserializableEvent):
  """Dataclass for Bond State Changed event metadata.

  Attributes:
    address: mac address of remote device in string format.
    state: new bond state of remote device.
    EVENT_NAME: ClassVar, callback event name.
  """

  address: str
  state: android_constants.BondState

  EVENT_NAME: ClassVar[str] = snippet_constants.BOND_STATE_CHANGE

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        address=mapping[snippet_constants.FIELD_DEVICE],
        state=android_constants.BondState(
            mapping[snippet_constants.FIELD_STATE]
        ),
    )


@dataclasses.dataclass
class A2dpPlayingStateChanged(JsonDeserializableEvent):
  EVENT_NAME: ClassVar[str] = snippet_constants.A2DP_PLAYING_STATE_CHANGED

  address: str
  state: android_constants.A2dpState

  @classmethod
  @override
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        address=mapping[snippet_constants.FIELD_DEVICE],
        state=android_constants.A2dpState(
            mapping[snippet_constants.FIELD_STATE]
        ),
    )


@dataclasses.dataclass
class PairingRequest(JsonDeserializableEvent):
  """Dataclass for Pairing Request event metadata.

  Attributes:
    address: mac address of remote device in string format.
    variant: variant of pairing procedure.
    pin: pairing confirmation pin code.
    EVENT_NAME: ClassVar, callback event name.
  """

  address: str
  variant: android_constants.PairingVariant
  pin: int

  EVENT_NAME: ClassVar[str] = snippet_constants.PAIRING_REQUEST

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        address=mapping[snippet_constants.FIELD_DEVICE],
        variant=android_constants.PairingVariant(
            mapping[snippet_constants.FIELD_VARIANT]
        ),
        pin=mapping[snippet_constants.FIELD_PIN],
    )


@dataclasses.dataclass
class DeviceFound(JsonDeserializableEvent):
  """Dataclass for Device Found (Inquiry Result) event metadata.

  Attributes:
    address: mac address of remote device in string format.
    name: name of remote device.
    EVENT_NAME: ClassVar, callback event name.
  """

  address: str
  name: str

  EVENT_NAME: ClassVar[str] = snippet_constants.DEVICE_FOUND

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        address=mapping[snippet_constants.FIELD_DEVICE],
        name=mapping[snippet_constants.FIELD_NAME],
    )


@dataclasses.dataclass
class AudioDeviceAdded(JsonDeserializableEvent):
  """Dataclass for Audio Device Added event metadata.

  Attributes:
    address: mac address of remote device in string format.
    device_type: type of audio device.
    EVENT_NAME: ClassVar, callback event name.
  """

  address: str
  device_type: android_constants.AudioDeviceType

  EVENT_NAME: ClassVar[str] = snippet_constants.AUDIO_DEVICE_ADDED

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        address=mapping[snippet_constants.FIELD_DEVICE],
        device_type=android_constants.AudioDeviceType(
            mapping[snippet_constants.FIELD_TRANSPORT]
        ),
    )


@dataclasses.dataclass
class CommunicationDeviceChanged(JsonDeserializableEvent):
  address: str
  device_type: android_constants.AudioDeviceType

  EVENT_NAME: ClassVar[str] = (
      snippet_constants.AUDIO_COMMUNICATION_DEVICE_CHANGED
  )

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        address=mapping[snippet_constants.FIELD_DEVICE],
        device_type=android_constants.AudioDeviceType(
            mapping[snippet_constants.FIELD_TRANSPORT]
        ),
    )


@dataclasses.dataclass
class GattConnectionStateChanged(JsonDeserializableEvent):
  """Dataclass for GATT Connection State Changed event metadata.

  Attributes:
    state: new state of GATT connection.
    status: status or reason of state transition.
    EVENT_NAME: ClassVar, callback event name.
  """

  state: android_constants.ConnectionState
  status: android_constants.GattStatus

  EVENT_NAME: ClassVar[str] = snippet_constants.GATT_CONNECTION_STATE_CHANGE

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        state=android_constants.ConnectionState(
            mapping[snippet_constants.FIELD_STATE]
        ),
        status=android_constants.GattStatus(
            mapping[snippet_constants.FIELD_STATUS]
        ),
    )


@dataclasses.dataclass
class GattCharacteristicReadRequest(JsonDeserializableEvent):
  """Dataclass for GATT Characteristic read request metadata.

  Attributes:
    address: mac address of target device in string format.
    characteristic_uuid: Characteristic UUID in string format.
    request_id: request ID required by send_response method.
    offset: offset of value in the request.
    EVENT_NAME: ClassVar, callback event name.
  """

  address: str
  characteristic_uuid: str
  request_id: int
  offset: int

  EVENT_NAME: ClassVar[str] = (
      snippet_constants.GATT_SERVER_CHARACTERISTIC_READ_REQUEST
  )

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        address=mapping[snippet_constants.FIELD_DEVICE],
        characteristic_uuid=mapping[snippet_constants.FIELD_UUID],
        request_id=mapping[snippet_constants.GATT_FIELD_REQUEST_ID],
        offset=mapping[snippet_constants.GATT_FIELD_OFFSET],
    )


@dataclasses.dataclass
class GattCharacteristicWriteRequest(JsonDeserializableEvent):
  """Dataclass for GATT Characteristic write request metadata.

  Attributes:
    address: mac address of target device in string format.
    characteristic_uuid: Characteristic UUID in string format.
    request_id: request ID required by send_response method.
    offset: offset of value in the request.
    value: what the remote wants to write.
    response_needed: whether response is required for this request.
    prepared_write: whether this is a prepared write.
    EVENT_NAME: ClassVar, callback event name.
  """

  address: str
  characteristic_uuid: str
  request_id: int
  offset: int
  value: bytes
  response_needed: bool
  prepared_write: bool

  EVENT_NAME: ClassVar[str] = (
      snippet_constants.GATT_SERVER_CHARACTERISTIC_WRITE_REQUEST
  )

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        address=mapping[snippet_constants.FIELD_DEVICE],
        characteristic_uuid=mapping[snippet_constants.FIELD_UUID],
        request_id=mapping[snippet_constants.GATT_FIELD_REQUEST_ID],
        offset=mapping[snippet_constants.GATT_FIELD_OFFSET],
        value=bytes(mapping[snippet_constants.FIELD_VALUE]),
        response_needed=mapping[snippet_constants.GATT_FIELD_RESPONSE_NEEDED],
        prepared_write=mapping[snippet_constants.GATT_FIELD_PREPARED_WRITE],
    )


@dataclasses.dataclass
class GattDescriptorWriteRequest(JsonDeserializableEvent):
  """Dataclass for GATT Descriptor write request metadata.

  Attributes:
    address: mac address of target device in string format.
    characteristic_handle: handle of characteristic.
    descriptor_uuid: Descriptor UUID in string format.
    request_id: request ID required by send_response method.
    offset: offset of value in the request.
    value: what the remote wants to write.
    response_needed: whether response is required for this request.
    prepared_write: whether this is a prepared write.
    EVENT_NAME: ClassVar, callback event name.
  """

  address: str
  characteristic_handle: int
  descriptor_uuid: str
  request_id: int
  offset: int
  value: bytes
  response_needed: bool
  prepared_write: bool

  EVENT_NAME: ClassVar[str] = (
      snippet_constants.GATT_SERVER_DESCRIPTOR_WRITE_REQUEST
  )

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        address=mapping[snippet_constants.FIELD_DEVICE],
        characteristic_handle=mapping[snippet_constants.FIELD_HANDLE],
        descriptor_uuid=mapping[snippet_constants.FIELD_UUID],
        request_id=mapping[snippet_constants.GATT_FIELD_REQUEST_ID],
        offset=mapping[snippet_constants.GATT_FIELD_OFFSET],
        value=bytes(mapping[snippet_constants.FIELD_VALUE]),
        response_needed=mapping[snippet_constants.GATT_FIELD_RESPONSE_NEEDED],
        prepared_write=mapping[snippet_constants.GATT_FIELD_PREPARED_WRITE],
    )


@dataclasses.dataclass(frozen=True)
class GattCharacteristicChanged(JsonDeserializableEvent):
  address: str
  handle: int
  value: bytes

  EVENT_NAME: ClassVar[str] = snippet_constants.GATT_CHARACTERISTIC_CHANGED

  @classmethod
  def from_mapping(cls: Type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        address=mapping[snippet_constants.FIELD_DEVICE],
        handle=mapping[snippet_constants.FIELD_HANDLE],
        value=bytes(mapping[snippet_constants.FIELD_VALUE]),
    )


@dataclasses.dataclass
class VolumeChanged(JsonDeserializableEvent):
  """Dataclass for System Volume Changed metadata.

  Attributes:
    stream_type: type of stream.
    volume_value: index of volume for stream_type.
    EVENT_NAME: ClassVar, callback event name.
  """

  stream_type: android_constants.StreamType
  volume_value: int

  EVENT_NAME: ClassVar[str] = snippet_constants.VOLUME_CHANGED

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        stream_type=android_constants.StreamType(
            mapping[snippet_constants.FIELD_TYPE]
        ),
        volume_value=mapping[snippet_constants.FIELD_VALUE],
    )


@dataclasses.dataclass
class CallStateChanged(JsonDeserializableEvent):
  """Dataclass for Call State Changed metadata.

  Attributes:
    handle: uri handle of the call.
    name: displayed name of caller.
    state: state of the call.
    EVENT_NAME: ClassVar, callback event name.
  """

  handle: str
  name: str
  state: android_constants.CallState

  EVENT_NAME: ClassVar[str] = snippet_constants.CALL_STATE_CHANGED

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        handle=mapping[snippet_constants.FIELD_HANDLE],
        name=mapping[snippet_constants.FIELD_NAME],
        state=android_constants.CallState(
            mapping[snippet_constants.FIELD_STATE]
        ),
    )


@dataclasses.dataclass
class ProfileConnectionStateChanged(JsonDeserializableEvent):
  """Dataclass for Profile Connection State Changed event metadata.

  Attributes:
    address: mac address of remote device in string format.
    state: new bond state of remote device.
    EVENT_NAME: ClassVar, callback event name.
  """

  address: str
  state: android_constants.ConnectionState

  EVENT_NAME: ClassVar[str] = snippet_constants.PROFILE_CONNECTION_STATE_CHANGE

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        address=mapping[snippet_constants.FIELD_DEVICE],
        state=android_constants.ConnectionState(
            mapping[snippet_constants.FIELD_STATE]
        ),
    )


@dataclasses.dataclass
class ProfileActiveDeviceChanged(JsonDeserializableEvent):
  address: str | None = None

  EVENT_NAME: ClassVar[str] = snippet_constants.ACTIVE_DEVICE_CHANGED

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(address=mapping.get(snippet_constants.FIELD_DEVICE))


@dataclasses.dataclass
class HfpAgAudioStateChanged(JsonDeserializableEvent):
  """Dataclass for HFP AG Audio State Changed event metadata.

  Attributes:
    address: mac address of remote device in string format.
    state: new bond state of remote device.
    EVENT_NAME: ClassVar, callback event name.
  """

  EVENT_NAME: ClassVar[str] = snippet_constants.HFP_AG_AUDIO_STATE_CHANGED

  address: str
  state: android_constants.ScoState

  @classmethod
  @override
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        address=mapping[snippet_constants.FIELD_DEVICE],
        state=android_constants.ScoState(
            mapping[snippet_constants.FIELD_STATE]
        ),
    )


@dataclasses.dataclass
class HfpHfAudioStateChanged(JsonDeserializableEvent):
  """Dataclass for HFP HF Audio State Changed event metadata.

  Attributes:
    address: mac address of remote device in string format.
    state: new bond state of remote device.
    EVENT_NAME: ClassVar, callback event name.
  """

  address: str
  state: android_constants.ConnectionState

  EVENT_NAME: ClassVar[str] = snippet_constants.HFP_HF_AUDIO_STATE_CHANGED

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        address=mapping[snippet_constants.FIELD_DEVICE],
        state=android_constants.ConnectionState(
            mapping[snippet_constants.FIELD_STATE]
        ),
    )


@dataclasses.dataclass
class BatteryLevelChanged(JsonDeserializableEvent):
  """Dataclass for Battery Level Changed event metadata."""

  address: str
  level: int

  EVENT_NAME: ClassVar[str] = snippet_constants.BATTERY_LEVEL_CHANGED

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        address=mapping[snippet_constants.FIELD_DEVICE],
        level=mapping[snippet_constants.FIELD_VALUE],
    )


@dataclasses.dataclass
class BroadcastSourceFound(JsonDeserializableEvent):

  EVENT_NAME: ClassVar[str] = snippet_constants.BASS_SOURCE_FOUND

  source: auracast_uri.BroadcastAudioUri

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        source=auracast_uri.BroadcastAudioUri.from_string(
            mapping[snippet_constants.FIELD_SOURCE]
        ),
    )


@dataclasses.dataclass
class PlayerIsPlayingChanged(JsonDeserializableEvent):

  EVENT_NAME: ClassVar[str] = snippet_constants.PLAYER_IS_PLAYING_CHANGED

  is_playing: bool

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(is_playing=mapping[snippet_constants.FIELD_STATE])


@dataclasses.dataclass
class PlayerMediaItemTransition(JsonDeserializableEvent):

  EVENT_NAME: ClassVar[str] = snippet_constants.PLAYER_MEDIA_ITEM_TRANSITION

  uri: str | None

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(uri=mapping.get(snippet_constants.URI))


@dataclasses.dataclass
class DistanceMeasurementResult(JsonDeserializableEvent):
  """Dataclass for Distance Measurement Result event metadata."""

  result_meters: float | None = None
  error_meters: float | None = None
  azimuth_angle: float | None = None
  error_azimuth_angle: float | None = None
  altitude_angle: float | None = None
  error_altitude_angle: float | None = None
  delay_spread_meters: float | None = None
  confidence_level: float | None = None
  velocity_meters_per_second: float | None = None
  detected_attack_level: int | None = None
  measurement_timestamp_nanos: int | None = None

  EVENT_NAME: ClassVar[str] = snippet_constants.DISTANCE_MEASUREMENT_RESULT

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    return cls(
        result_meters=mapping.get(snippet_constants.RESULT_METERS),
        error_meters=mapping.get(snippet_constants.ERROR_METERS),
        azimuth_angle=mapping.get(snippet_constants.AZIMUTH_ANGLE),
        error_azimuth_angle=mapping.get(snippet_constants.ERROR_AZIMUTH_ANGLE),
        altitude_angle=mapping.get(snippet_constants.ALTITUDE_ANGLE),
        error_altitude_angle=mapping.get(
            snippet_constants.ERROR_ALTITUDE_ANGLE
        ),
        delay_spread_meters=mapping.get(snippet_constants.DELAY_SPREAD_METERS),
        confidence_level=mapping.get(snippet_constants.CONFIDENCE_LEVEL),
        velocity_meters_per_second=mapping.get(
            snippet_constants.VELOCITY_METERS_PER_SECOND
        ),
        detected_attack_level=mapping.get(
            snippet_constants.DETECTED_ATTACK_LEVEL
        ),
        measurement_timestamp_nanos=mapping.get(
            snippet_constants.MEASUREMENT_TIMESTAMP_NANOS
        ),
    )


@dataclasses.dataclass
class ScanResult(JsonDeserializableEvent):
  """LE Scan result."""

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

  EVENT_NAME: ClassVar[str] = snippet_constants.SCAN_RESULT

  @override
  @classmethod
  def from_mapping(cls: type[Self], mapping: Mapping[str, Any]) -> Self:
    raw_service_data = mapping.get(snippet_constants.ADV_DATA_SERVICE_DATA)
    raw_manufacturer_data = mapping.get(
        snippet_constants.ADV_DATA_MANUFACTURER_DATA
    )
    if raw_service_data is not None:
      pairs = [line.split(':') for line in raw_service_data]
      service_data = {pair[0]: base64.b64decode(pair[1]) for pair in pairs}
    else:
      service_data = None
    if raw_manufacturer_data is not None:
      pairs = [line.split(':') for line in raw_manufacturer_data]
      manufacturer_data = {
          int(pair[0]): base64.b64decode(pair[1]) for pair in pairs
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
class LegacyAdvertiseSettings:
  connectable: bool = True
  discoverable: bool = True
  own_address_type: int = android_constants.AddressTypeStatus.RANDOM
  timeout: int = 0
  tx_power_level: int = android_constants.LegacyTxPowerLevel.MEDIUM
  advertise_mode: int = android_constants.LegacyAdvertiseMode.LOW_POWER


@dataclasses.dataclass
class AdvertisingSetParameters:
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
class AdvertisingData:
  include_device_name: bool = False
  include_tx_power_level: bool = False
  service_uuids: Sequence[str] | None = None
  service_solicitation_uuids: Sequence[str] | None = None
  service_data: dict[str, bytes] | None = None
  manufacturer_data: dict[int, bytes] | None = None


@dataclasses.dataclass
class ScanFilter:
  """LE Scan filter.

  Attributes:
    advertising_data_type: Advertising Data Type.
    name: Remote device mame.
    device: Remote device address.
    address_type: Remote device address type.
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
  service_uuids: str | None = None
  service_solicitation_uuids: str | None = None
  service_data: dict[str, bytes] | None = None
  manufacturer_data: dict[int, bytes] | None = None


@dataclasses.dataclass
class ScanSettings:
  scan_mode: int | None = None
  callback_type: int | None = None
  scan_result_type: int | None = None
  phy: int | None = None
  legacy: bool | None = None


@dataclasses.dataclass
class DistanceMeasurementParameters:
  """Distance Measurement Parameters."""

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
    if not cookie:
      raise RuntimeError('Failed to start advertising.')
    return LegacyAdvertiser(cookie=cookie, snippet=snippet)

  def stop(self) -> None:
    self.snippet.stopAdvertisingSet(self.cookie)

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
  ) -> Self:
    """Starts an Extended Advertising Set.

    Args:
      snippet: snippet client instance.
      advertising_set_parameters: advertising set parameters.
      advertising_data: advertising data.
      scan_response: scan response.

    Returns:
      advertiser instance.
    """
    cookie = await asyncio.to_thread(
        lambda: snippet.startAdvertisingSet(
            _make_json_object(advertising_set_parameters),
            _make_json_object(advertising_data),
            _make_json_object(scan_response),
        ),
    )
    return ExtendedAdvertisingSet(cookie=cookie, snippet=snippet)

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

  content_type: ContentType | None = None
  usage: Usage | None = None


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


class PhoneCall:
  """Context managable phone call wrapper."""

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
    self.snippet = snippet
    self.cookie = snippet.addCall(
        caller_name,
        f'tel:{caller_number}',
        direction == constants.Direction.INCOMING,
    )

  def answer(self) -> None:
    """Answers the phone call."""
    self.snippet.answerCall(self.cookie)

  def close(self) -> None:
    """Closes the phone call."""
    self.snippet.disconnectCall(self.cookie)

  def __enter__(self) -> Self:
    return self

  def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
    with contextlib.suppress(mobly.snippet.errors.ApiError):
      self.close()


class MediaRecorder:
  """Context managable MediaRecorder wrapper."""

  def __init__(
      self,
      snippet: snippet_stub.BluetoothSnippet,
      path: str,
  ):
    """Class initializer.

    Args:
        snippet: snippet client instance.
        path: Path on device to save the recorded media file.
    """
    self.snippet = snippet
    self.path = path
    snippet.startRecording(path)

  def close(self) -> None:
    """Closes the phone call."""
    self.snippet.stopRecording(self.path)

  def __enter__(self) -> Self:
    return self

  def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
    with contextlib.suppress(mobly.snippet.errors.ApiError):
      self.close()


@dataclasses.dataclass
class L2capChannel:
  """L2CAP channel wrapper."""

  snippet: snippet_stub.BluetoothSnippet
  cookie: str

  @classmethod
  async def connect(
      cls: Type[Self],
      snippet: snippet_stub.BluetoothSnippet,
      address: str,
      secure: bool,
      psm: int,
      transport: int,
      address_type: int | None = None,
      retry_count: int = _DEFAULT_RETRY_COUNT,
  ) -> Self:
    """Connects an l2cap channel.

    Args:
      snippet: Snippet client instance.
      address: Address of target device.
      secure: Whether encryption is required.
      psm: Channel number of the l2cap channel.
      transport: Transport to use (Classic or LE).
      address_type: Address type of target device (if LE transport is used).
      retry_count: Allowed retry count of connect attempts.

    Returns:
      L2CAP client wrapper instance.

    Raises:
      ConnectionError: L2CAP is not connected after allowed retry counts.
    """

    @retry.retry_on_exception(
        initial_delay_sec=_DEFAULT_RETRY_DELAY_SECONDS,
        num_retries=retry_count,
    )
    async def inner():
      with contextlib.suppress(mobly.snippet.errors.ApiError):
        cookie = await asyncio.to_thread(
            snippet.l2capConnect,
            address,
            secure,
            psm,
            transport,
            address_type,
        )
        return cls(snippet=snippet, cookie=cookie)
      raise errors.ConnectionError('Unable to connect l2cap')

    return await inner()

  async def close(self) -> None:
    """Closes the L2CAP channel."""
    await asyncio.to_thread(self.snippet.l2capDisconnect, self.cookie)

  async def read(self, length: int | None = None) -> bytes:
    """Reads data from the L2CAP channel.

    If `length` is provided, the data will be read until the length is reached.
    Otherwise, only one read call will be performed for at most 65535 bytes.

    Args:
      length: Length of data to read.

    Returns:
      Read data.
    """
    return base64.b64decode(
        await asyncio.to_thread(self.snippet.l2capRead, self.cookie, length)
    )

  async def write(self, data: bytes) -> None:
    """Writes data to the L2CAP channel.

    Args:
      data: Data to write.
    """
    await asyncio.to_thread(
        self.snippet.l2capWrite,
        self.cookie,
        base64.b64encode(data).decode('utf-8'),
    )


@dataclasses.dataclass
class L2capServer:
  """L2CAP server wrapper."""

  AUTO_ALLOCATE_PSM: ClassVar[int] = -2

  snippet: snippet_stub.BluetoothSnippet
  psm: int

  @classmethod
  def create(
      cls: Type[Self],
      snippet: snippet_stub.BluetoothSnippet,
      secure: bool,
      transport: int,
      psm: int = AUTO_ALLOCATE_PSM,
  ) -> Self:
    """Opens an L2CAP server.

    Args:
      snippet: Snippet client instance.
      secure: Whether encryption is required.
      transport: Transport (LE or Classic) of L2CAP.
      psm: L2CAP channel number.

    Returns:
      Created L2CAP server wrapper.
    """
    return cls(
        snippet=snippet, psm=snippet.l2capOpenServer(secure, transport, psm)
    )

  def close(self) -> None:
    """Closes the L2CAP server."""
    self.snippet.l2capCloseServer(self.psm)

  async def accept(self) -> L2capChannel:
    """Accepts a connection from the L2CAP server."""
    cookie = await asyncio.to_thread(self.snippet.l2capWaitConnection, self.psm)
    return L2capChannel(snippet=self.snippet, cookie=cookie)


@dataclasses.dataclass
class RfcommChannel:
  """Rfcomm channel wrapper."""

  snippet: snippet_stub.BluetoothSnippet
  cookie: str

  @classmethod
  async def connect_with_channel_number(
      cls: Type[Self],
      snippet: snippet_stub.BluetoothSnippet,
      address: str,
      secure: bool,
      channel: int,
      retry_count: int = _DEFAULT_RETRY_COUNT,
  ) -> Self:
    """Connects an RFCOMM channel.

    Args:
      snippet: snippet client instance.
      address: address of target device.
      secure: whether encryption is required.
      channel: channel number of the RFCOMM channel.
      retry_count: allowed retry count of connect attempts.

    Returns:
      RFCOMM client wrapper instance.

    Raises:
      ConnectionError: RFCOMM is not connected after allowed retry counts.
    """

    @retry.retry_on_exception(
        initial_delay_sec=_DEFAULT_RETRY_DELAY_SECONDS,
        num_retries=retry_count,
    )
    async def inner():
      with contextlib.suppress(mobly.snippet.errors.ApiError):
        cookie = await asyncio.to_thread(
            snippet.rfcommConnectWithChannel,
            address,
            secure,
            channel,
        )
        return cls(snippet=snippet, cookie=cookie)
      raise errors.ConnectionError('Unable to connect RFCOMM')

    return await inner()

  @classmethod
  async def connect_with_service_record(
      cls: Type[Self],
      snippet: snippet_stub.BluetoothSnippet,
      address: str,
      secure: bool,
      uuid: str,
      retry_count: int = _DEFAULT_RETRY_COUNT,
  ) -> Self:
    """Connects an RFCOMM channel with Service Record UUID.

    Args:
      snippet: Snippet client instance.
      address: Address of target device.
      secure: Whether encryption is required.
      uuid: Service record UUID.
      retry_count: Allowed retry count of connect attempts.

    Returns:
      RFCOMM client wrapper instance.

    Raises:
      ConnectionError: RFCOMM is not connected after allowed retry counts.
    """

    @retry.retry_on_exception(
        initial_delay_sec=_DEFAULT_RETRY_DELAY_SECONDS,
        num_retries=retry_count,
    )
    async def inner():
      with contextlib.suppress(mobly.snippet.errors.ApiError):
        cookie = await asyncio.to_thread(
            snippet.rfcommConnectWithUuid,
            address,
            secure,
            uuid,
        )
        return cls(snippet=snippet, cookie=cookie)
      raise errors.ConnectionError('Unable to connect RFCOMM')

    return await inner()

  async def close(self) -> None:
    """Closes the RFCOMM channel."""
    await asyncio.to_thread(self.snippet.rfcommDisconnect, self.cookie)

  async def read(self, length: int | None = None) -> bytes:
    """Reads data from the RFCOMM channel.

    If `length` is provided, the data will be read until the length is reached.
    Otherwise, only one read call will be performed for at most 65535 bytes.

    Args:
      length: Length of data to read.

    Returns:
      Read data.
    """
    return base64.b64decode(
        await asyncio.to_thread(self.snippet.rfcommRead, self.cookie, length)
    )

  async def write(self, data: bytes) -> None:
    """Writes data to the RFCOMM channel.

    Args:
      data: Data to write.
    """
    await asyncio.to_thread(
        self.snippet.rfcommWrite, self.cookie, base64.b64encode(data).decode()
    )


@dataclasses.dataclass
class RfcommServer:
  """RFCOMM server wrapper."""

  _AUTO_ALLOCATE_PSM: ClassVar[int] = -2

  snippet: snippet_stub.BluetoothSnippet
  uuid: str

  @classmethod
  def create(
      cls: Type[Self],
      snippet: snippet_stub.BluetoothSnippet,
      secure: bool,
      uuid: str,
  ) -> Self:
    """Opens an RFCOMM server.

    Args:
      snippet: Snippet client instance.
      secure: Whether encryption is required.
      uuid: RFCOMM Service Record UUID.

    Returns:
      Created RFCOMM server wrapper.
    """
    snippet.rfcommOpenServer(secure, uuid)
    return cls(snippet=snippet, uuid=uuid)

  def close(self) -> None:
    """Closes the RFCOMM server."""
    self.snippet.rfcommCloseServer(self.uuid)

  async def accept(self) -> RfcommChannel:
    """Accepts a connection from the RFCOMM server."""
    cookie = await asyncio.to_thread(
        self.snippet.rfcommWaitConnection, self.uuid
    )
    return RfcommChannel(snippet=self.snippet, cookie=cookie)


@dataclasses.dataclass
class GattClient(CallbackHandler):
  """GATT client control block."""

  @classmethod
  async def connect(
      cls: Type[Self],
      snippet: snippet_stub.BluetoothSnippet,
      address: str,
      transport: int,
      address_type: int | None = None,
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
        snippet=snippet, handler=callback_handler, module=Module.GATT_SERVER
    )

  @override
  def close(self) -> None:
    self.snippet.gattServerClose(self.handler.callback_id)

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


class SnippetWrapper:
  """Wrapper for BluetoothSnippet."""

  def __init__(self, snippet: snippet_stub.BluetoothSnippet) -> None:
    self.snippet = snippet

  def set_audio_attributes(
      self,
      attributes: AudioAttributes,
      handle_audio_focus: bool,
  ) -> None:
    """Sets audio attributes."""
    self.snippet.setAudioAttributes(
        _make_json_object(attributes), handle_audio_focus
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

  async def connect_gatt_client(
      self,
      address: str,
      transport: int,
      address_type: int | None = None,
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
      transport: int,
      psm: int = L2capServer.AUTO_ALLOCATE_PSM,
  ) -> L2capServer:
    """Creates an L2CAP server.

    Args:
      secure: Whether encryption is required.
      transport: Transport (LE or Classic) of L2CAP.
      psm: L2CAP channel number.

    Returns:
      The L2CAP server control block.
    """
    return L2capServer.create(self.snippet, secure, transport, psm)

  async def create_l2cap_channel(
      self,
      address: str,
      secure: bool,
      psm: int,
      transport: int,
      address_type: int | None = None,
      retry_count: int = _DEFAULT_RETRY_COUNT,
  ) -> L2capChannel:
    """Creates an L2CAP channel.

    Args:
      address: Address of target device.
      secure: Whether encryption is required.
      psm: L2CAP channel number.
      transport: Transport (LE or Classic) of L2CAP.
      address_type: Address type of target device (if LE transport is used).
      retry_count: Allowed retry count of connect attempts.

    Returns:
      The L2CAP channel control block.
    """
    return await L2capChannel.connect(
        self.snippet,
        address,
        secure,
        psm,
        transport,
        address_type,
        retry_count,
    )

  def create_rfcomm_server(self, uuid: str, secure: bool) -> RfcommServer:
    """Creates an RFCOMM server.

    Args:
      uuid: UUID of the RFCOMM service.
      secure: Whether encryption is required.

    Returns:
      The RFCOMM server control block.
    """
    return RfcommServer.create(self.snippet, secure, uuid)

  async def create_rfcomm_channel(
      self,
      address: str,
      secure: bool,
      channel_or_uuid: int | str,
      retry_count: int = _DEFAULT_RETRY_COUNT,
  ) -> RfcommChannel:
    """Creates an RFCOMM channel.

    Args:
      address: Address of target device.
      secure: Whether encryption is required.
      channel_or_uuid: Channel number or UUID of the RFCOMM service.
      retry_count: Allowed retry count of connect attempts.

    Returns:
      The RFCOMM channel control block.
    """
    if isinstance(channel_or_uuid, int):
      return await RfcommChannel.connect_with_channel_number(
          self.snippet, address, secure, channel_or_uuid, retry_count
      )
    else:
      return await RfcommChannel.connect_with_service_record(
          self.snippet, address, secure, channel_or_uuid, retry_count
      )

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
  ) -> ExtendedAdvertisingSet:
    """Starts an extended advertising set.

    Args:
      advertising_set_parameters: Advertising set parameters.
      advertising_data: Advertising data.
      scan_response: Scan response data.

    Returns:
      The extended advertising set control block.
    """
    return await ExtendedAdvertisingSet.create(
        self.snippet,
        advertising_set_parameters,
        advertising_data,
        scan_response,
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

  def start_media_recording(self, path: str) -> MediaRecorder:
    """Starts media recording.

    Args:
      path: Path to the recording file.

    Returns:
      The media recorder control block.
    """
    return MediaRecorder(self.snippet, path)
