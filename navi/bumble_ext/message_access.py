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

"""Implementation of Message Access Profile (MAP) on Bumble."""

from __future__ import annotations

import asyncio
import dataclasses
import enum
from typing import Self, Tuple
import uuid

from bumble import core
from bumble import device as bumble_device
from bumble import sdp
from typing_extensions import override

from navi.bumble_ext import obex
from navi.bumble_ext import rfcomm

_DEFAULT_LANGUAGE_BASED_ATTRIBUTE_ID_OFFSET = 0x0100
MAX_RFCOMM_OBEX_PACKET_LENGTH = 65530
MAS_TARGET_UUID = uuid.UUID('bb582b40-420c-11db-b0de-0800200c9a66')
MNS_TARGET_UUID = uuid.UUID('bb582b41-420c-11db-b0de-0800200c9a66')


class ObexHeaderType(bytes, enum.Enum):
  EVENT_REPORT = b'x-bt/MAP-event-report\0'
  MESSAGE_LISTING = b'x-bt/MAP-msg-listing\0'
  MESSAGE = b'x-bt/message\0'
  NOTIFICATION_REGISTRATION = b'x-bt/MAP-NotificationRegistration\0'


class AttributeId(enum.IntEnum):
  """See https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/service_discovery/attribute_ids/message_access_profile.yaml."""

  GOEP_L2CAP_PSM = 0x0200
  MAS_INSTANCE_ID = 0x0315
  SUPPORTED_MESSAGE_TYPES = 0x0316
  MAP_SUPPORTED_FEATURES = 0x0317


class Version(enum.IntEnum):
  """See MAP specification, 7.1.1 SDP record for the Message Access Service on the MSE device."""

  V_1_0 = 0x0100
  V_1_1 = 0x0101
  V_1_2 = 0x0102
  V_1_3 = 0x0103
  V_1_4 = 0x0104


class SupportedMessageTypes(enum.IntFlag):
  """See MAP specification, 7.1.1 SDP record for the Message Access Service on the MSE device.

  Note: The value is different from FilterMessageType in Application Parameter
  Value.
  """

  EMAIL = 1 << 0
  SMS_GSM = 1 << 1
  SMS_CDMA = 1 << 2
  MMS = 1 << 3
  IM = 1 << 4


@dataclasses.dataclass(frozen=True, kw_only=True)
class ApplicationParameter:
  """See MAP specification, 6.3.1 Application Parameters header."""

  tag: int
  value: int | str

  class Tag(enum.IntEnum):
    """See MAP specification, 6.3.1 Application Parameters header."""

    MAX_LIST_COUNT = 0x01
    LIST_START_OFFSET = 0x02
    FILTER_MESSAGE_TYPE = 0x03
    FILTER_PERIOD_BEGIN = 0x04
    END_FILTER_PERIOD_END = 0x05
    FILTER_READ_STATUS = 0x06
    ATTACHMENT = 0x0A
    TRANSPARENT = 0x0B
    RETRY = 0x0C
    NEW_MESSAGE = 0x0D
    NOTIFICATION_STATUS = 0x0E
    MAS_INSTANCE_ID = 0x0F
    PARAMETER_MASK = 0x10
    FOLDER_LISTING_SIZE = 0x11
    LISTING_SIZE = 0x12
    SUBJECT_LENGTH = 0x13
    CHARSET = 0x14
    FRACTION_REQUEST = 0x15
    FRACTION_DELIVER = 0x16
    STATUS_INDICATOR = 0x17
    STATUS_VALUE = 0x18
    MSE_TIME = 0x19
    DATABASE_IDENTIFIER = 0x1A
    CONVERSATION_LISTING_VERSION_COUNTER = 0x1B
    PRESENCE_AVAILABILITY = 0x1C
    PRESENCE_TEXT = 0x1D
    LAST_ACTIVITY = 0x1E
    FILTER_LAST_ACTIVITY_BEGIN = 0x1F
    FILTER_LAST_ACTIVITY_END = 0x20
    CHAT_STATE = 0x21
    CONVERSATION_ID = 0x22
    FOLDER_VERSION_COUNTER = 0x23
    FILTER_MESSAGE_HANDLE = 0x24
    NOTIFICATION_FILTER_MASK = 0x25
    CONV_PARAMETER_MASK = 0x26
    OWNER_UCI = 0x27
    EXTENDED_DATA = 0x28
    MAP_SUPPORTED_FEATURES = 0x29

  TEXT_LENGTH = -1
  HEX_128_BIT = -2
  HEX_64_BIT = -3
  APPLICATION_PARAMETER_LENGTHS = {
      Tag.MAX_LIST_COUNT: 2,
      Tag.LIST_START_OFFSET: 2,
      Tag.FILTER_MESSAGE_TYPE: 1,
      Tag.FILTER_PERIOD_BEGIN: TEXT_LENGTH,
      Tag.END_FILTER_PERIOD_END: TEXT_LENGTH,
      Tag.FILTER_READ_STATUS: 1,
      Tag.ATTACHMENT: 1,
      Tag.TRANSPARENT: 1,
      Tag.RETRY: 1,
      Tag.NEW_MESSAGE: 1,
      Tag.NOTIFICATION_STATUS: 1,
      Tag.MAS_INSTANCE_ID: 1,
      Tag.PARAMETER_MASK: 4,
      Tag.FOLDER_LISTING_SIZE: 2,
      Tag.LISTING_SIZE: 2,
      Tag.SUBJECT_LENGTH: 1,
      Tag.CHARSET: 1,
      Tag.FRACTION_REQUEST: 1,
      Tag.FRACTION_DELIVER: 1,
      Tag.STATUS_INDICATOR: 1,
      Tag.STATUS_VALUE: 1,
      Tag.MSE_TIME: TEXT_LENGTH,
      Tag.DATABASE_IDENTIFIER: HEX_128_BIT,
      Tag.CONVERSATION_LISTING_VERSION_COUNTER: HEX_128_BIT,
      Tag.PRESENCE_AVAILABILITY: 1,
      Tag.PRESENCE_TEXT: TEXT_LENGTH,
      Tag.LAST_ACTIVITY: TEXT_LENGTH,
      Tag.FILTER_LAST_ACTIVITY_BEGIN: TEXT_LENGTH,
      Tag.FILTER_LAST_ACTIVITY_END: TEXT_LENGTH,
      Tag.CHAT_STATE: 1,
      Tag.CONVERSATION_ID: HEX_128_BIT,
      Tag.FOLDER_VERSION_COUNTER: HEX_128_BIT,
      Tag.FILTER_MESSAGE_HANDLE: HEX_64_BIT,
      Tag.NOTIFICATION_FILTER_MASK: 4,
      Tag.CONV_PARAMETER_MASK: 4,
      Tag.OWNER_UCI: TEXT_LENGTH,
      Tag.EXTENDED_DATA: TEXT_LENGTH,
      Tag.MAP_SUPPORTED_FEATURES: 4,
  }

  def to_bytes(self) -> bytes:
    """Converts the application parameter to bytes."""
    if isinstance(self.value, str):
      data = self.value.encode('utf-8')
    else:
      length = self.APPLICATION_PARAMETER_LENGTHS[self.tag]
      if length in (self.HEX_128_BIT, self.HEX_64_BIT):
        data = hex(self.value).encode('utf-8')
      else:
        data = self.value.to_bytes(length, 'big')
    return bytes([self.tag, len(data)]) + data

  def __bytes__(self) -> bytes:
    return self.to_bytes()

  @classmethod
  def parse_from(
      cls: type[Self], data: bytes, offset: int = 0
  ) -> Tuple[Self, int]:
    """Creates an application parameter from the given bytes."""
    tag = cls.Tag(data[offset])
    length = data[offset + 1]
    expected_length = cls.APPLICATION_PARAMETER_LENGTHS[tag]
    if expected_length == cls.TEXT_LENGTH:
      value = data[offset + 2 : offset + 2 + length].decode('utf-8')
    elif expected_length in (cls.HEX_128_BIT, cls.HEX_64_BIT):
      value = int(data[offset + 2 : offset + 2 + length].decode('utf-8'), 16)
    else:
      value = int.from_bytes(data[offset + 2 : offset + 2 + length], 'big')
    return cls(tag=tag, value=value), offset + 2 + length


@dataclasses.dataclass
class ApplicationParameters:
  """A collection of application parameters."""

  max_list_count: int | None = None
  list_start_offset: int | None = None
  filter_message_type: int | None = None
  filter_period_begin: str | None = None
  end_filter_period_end: str | None = None
  filter_read_status: int | None = None
  attachment: int | None = None
  transparent: int | None = None
  retry: int | None = None
  new_message: int | None = None
  notification_status: int | None = None
  mas_instance_id: int | None = None
  parameter_mask: int | None = None
  folder_listing_size: int | None = None
  listing_size: int | None = None
  subject_length: int | None = None
  charset: int | None = None
  fraction_request: int | None = None
  fraction_deliver: int | None = None
  status_indicator: int | None = None
  status_value: int | None = None
  mse_time: str | None = None
  database_identifier: int | None = None
  conversation_listing_version_counter: int | None = None
  presence_availability: int | None = None
  presence_text: str | None = None
  last_activity: str | None = None
  filter_last_activity_begin: str | None = None
  filter_last_activity_end: str | None = None
  chat_state: int | None = None
  conversation_id: int | None = None
  folder_version_counter: int | None = None
  filter_message_handle: int | None = None
  notification_filter_mask: int | None = None
  conv_parameter_mask: int | None = None
  owner_uci: str | None = None
  extended_data: str | None = None
  map_supported_features: int | None = None

  @classmethod
  def from_bytes(cls: type[Self], data: bytes) -> Self:
    """Parses a list of application parameters from the given bytes."""
    offset = 0
    instance = cls()
    while offset < len(data):
      application_parameter, offset = ApplicationParameter.parse_from(
          data, offset
      )
      tag_name = ApplicationParameter.Tag(
          application_parameter.tag
      ).name.lower()
      if hasattr(instance, tag_name):
        setattr(instance, tag_name, application_parameter.value)
    return instance

  def to_bytes(self) -> bytes:
    """Converts the application parameters to bytes."""
    return b''.join([
        ApplicationParameter(
            tag=ApplicationParameter.Tag[tag.upper()],
            value=value,
        ).to_bytes()
        for tag, value in dataclasses.asdict(self).items()
        if value is not None
    ])

  def __bytes__(self) -> bytes:
    return self.to_bytes()


class ApplicationParameterValue:
  """See MAP specification, 6.3.1 Application Parameters header."""

  class SupportedFeatures(enum.IntFlag):
    """See MAP specification, 6.3.1 Application Parameters header."""

    NOTIFICATION_REGISTRATION_FEATURE = 1 << 0
    NOTIFICATION_FEATURE = 1 << 1
    BROWSING_FEATURE = 1 << 2
    UPLOADING_FEATURE = 1 << 3
    DELETE_FEATURE = 1 << 4
    INSTANCE_INFORMATION_FEATURE = 1 << 5
    EXTENDED_EVENT_REPORT_1_1 = 1 << 6
    EVENT_REPORT_VERSION_1_2 = 1 << 7
    MESSAGE_FORMAT_VERSION_1_1 = 1 << 8
    MESSAGES_LISTING_FORMAT_VERSION_1_1 = 1 << 9
    PERSISTENT_MESSAGE_HANDLES = 1 << 10
    DATABASE_IDENTIFIER = 1 << 11
    FOLDER_VERSION_COUNTER = 1 << 12
    CONVERSATION_VERSION_COUNTERS = 1 << 13
    PARTICIPANT_PRESENCE_CHANGE_NOTIFICATION = 1 << 14
    PARTICIPANT_CHAT_STATE_CHANGE_NOTIFICATION = 1 << 15
    PBAP_CONTACT_CROSS_REFERENCE = 1 << 16
    NOTIFICATION_FILTERING = 1 << 17
    UTC_OFFSET_TIMESTAMP_FORMAT = 1 << 18
    MAP_SUPPORTED_FEATURES_IN_CONNECT_REQUEST = 1 << 19
    CONVERSATION_LISTING = 1 << 20
    OWNER_STATUS = 1 << 21

  class SupportedMessageTypes(enum.IntFlag):
    """See MAP specification, 6.3.1 Application Parameters header."""

    SMS_GSM = 1 << 0
    SMS_CDMA = 1 << 1
    EMAIL = 1 << 2
    MMS = 1 << 3
    IM = 1 << 4

  class Charset(enum.IntFlag):
    """See MAP specification, 6.3.1 Application Parameters header."""

    # There is only one value at the moment.
    UTF_8 = 1 << 0

  class PropertyMask(enum.IntFlag):
    """See MAP specification, 5.5.4.4 ParameterMask."""

    SUBJECT = 1 << 0
    DATETIME = 1 << 1
    SENDER_NAME = 1 << 2
    SENDER_ADDRESSING = 1 << 3
    RECIPIENT_NAME = 1 << 4
    RECIPIENT_ADDRESSING = 1 << 5
    TYPE = 1 << 6
    SIZE = 1 << 7
    RECEPTION_STATUS = 1 << 8
    TEXT = 1 << 9
    ATTACHMENT_SIZE = 1 << 10
    PRIORITY = 1 << 11
    READ = 1 << 12
    SENT = 1 << 13
    PROTECTED = 1 << 14
    REPLYTO_ADDRESSING = 1 << 15
    DELIVERY_STATUS = 1 << 16
    CONVERSATION_ID = 1 << 17
    CONVERSATION_NAME = 1 << 18
    DIRECTION = 1 << 19
    ATTACHMENT_MIME = 1 << 20


@dataclasses.dataclass(frozen=True, kw_only=True)
class MasSdpInfo:
  """MAP Message Access Service (MAS) SDP information."""

  service_record_handle: int
  rfcomm_channel: int
  mas_instance_id: int
  version: Version
  service_name: str = 'MAP MAS'
  supported_message_types: SupportedMessageTypes
  supported_features: ApplicationParameterValue.SupportedFeatures
  goep_l2cap_psm: int | None = None

  def to_sdp_records(self) -> list[sdp.ServiceAttribute]:
    """Converts the MSE SDP information to SDP records."""
    records = [
        sdp.ServiceAttribute(
            sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
            sdp.DataElement.unsigned_integer_32(self.service_record_handle),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence(
                [sdp.DataElement.uuid(core.BT_MESSAGE_ACCESS_SERVER_SERVICE)],
            ),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence([
                sdp.DataElement.sequence(
                    [sdp.DataElement.uuid(core.BT_L2CAP_PROTOCOL_ID)]
                ),
                sdp.DataElement.sequence([
                    sdp.DataElement.uuid(core.BT_RFCOMM_PROTOCOL_ID),
                    sdp.DataElement.unsigned_integer_8(self.rfcomm_channel),
                ]),
                sdp.DataElement.sequence(
                    [sdp.DataElement.uuid(core.BT_OBEX_PROTOCOL_ID)]
                ),
            ]),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence([
                sdp.DataElement.sequence([
                    sdp.DataElement.uuid(
                        core.BT_MESSAGE_ACCESS_PROFILE_SERVICE
                    ),
                    sdp.DataElement.unsigned_integer_16(self.version.value),
                ])
            ]),
        ),
        sdp.ServiceAttribute(
            (
                _DEFAULT_LANGUAGE_BASED_ATTRIBUTE_ID_OFFSET
                + sdp.SDP_SERVICE_NAME_ATTRIBUTE_ID_OFFSET
            ),
            sdp.DataElement.text_string(self.service_name.encode('utf-8')),
        ),
        sdp.ServiceAttribute(
            AttributeId.MAS_INSTANCE_ID,
            sdp.DataElement.unsigned_integer_8(self.mas_instance_id),
        ),
        sdp.ServiceAttribute(
            AttributeId.SUPPORTED_MESSAGE_TYPES,
            sdp.DataElement.unsigned_integer_8(
                self.supported_message_types.value
            ),
        ),
        sdp.ServiceAttribute(
            AttributeId.MAP_SUPPORTED_FEATURES,
            sdp.DataElement.unsigned_integer_32(self.supported_features.value),
        ),
    ]
    if self.goep_l2cap_psm is not None:
      records.append(
          sdp.ServiceAttribute(
              AttributeId.GOEP_L2CAP_PSM,
              sdp.DataElement.unsigned_integer_16(self.goep_l2cap_psm),
          )
      )

    return records

  @classmethod
  async def find(
      cls: type[Self],
      connection: bumble_device.Connection,
  ) -> Self | None:
    """Finds SDP record for a MAS."""
    async with sdp.Client(connection) as sdp_client:
      result = await sdp_client.search_attributes(
          [core.BT_MESSAGE_ACCESS_SERVER_SERVICE],
          [
              sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
              sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
              sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
              AttributeId.MAS_INSTANCE_ID,
              AttributeId.SUPPORTED_MESSAGE_TYPES,
              AttributeId.MAP_SUPPORTED_FEATURES,
              AttributeId.GOEP_L2CAP_PSM,
          ],
      )

    if not result:
      return None

    service_record_handle: int | None = None
    rfcomm_channel: int | None = None
    version: Version | None = None
    supported_message_types: SupportedMessageTypes | None = None
    supported_features: ApplicationParameterValue.SupportedFeatures | None = (
        None
    )
    goep_l2cap_psm: int | None = None
    mas_instance_id: int | None = None

    for attribute_lists in result:
      for attribute in attribute_lists:
        match attribute.id:
          case sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID:
            service_record_handle = attribute.value.value
          case sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID:
            for element in attribute.value.value:
              if element.value[0].value == core.BT_RFCOMM_PROTOCOL_ID:
                rfcomm_channel = element.value[1].value
          case sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID:
            for element in attribute.value.value:
              if (
                  element.value[0].value
                  == core.BT_MESSAGE_ACCESS_PROFILE_SERVICE
              ):
                version = Version(element.value[1].value)
          case AttributeId.SUPPORTED_MESSAGE_TYPES:
            supported_message_types = SupportedMessageTypes(
                attribute.value.value
            )
          case AttributeId.MAP_SUPPORTED_FEATURES:
            supported_features = ApplicationParameterValue.SupportedFeatures(
                attribute.value.value
            )
          case AttributeId.MAS_INSTANCE_ID:
            mas_instance_id = attribute.value.value
          case AttributeId.GOEP_L2CAP_PSM:
            goep_l2cap_psm = attribute.value.value

    if (
        service_record_handle is None
        or rfcomm_channel is None
        or version is None
        or supported_message_types is None
        or supported_features is None
        or mas_instance_id is None
    ):
      raise ValueError(
          'Incomplete SDP record, '
          f'service_record_handle: {service_record_handle}, '
          f'rfcomm_channel: {rfcomm_channel}, '
          f'version: {version}, '
          f'supported_message_types: {supported_message_types}, '
          f'supported_features: {supported_features}, '
          f'mas_instance_id: {mas_instance_id}, '
          f'goep_l2cap_psm: {goep_l2cap_psm}',
      )

    return cls(
        service_record_handle=service_record_handle,
        rfcomm_channel=rfcomm_channel,
        version=version,
        supported_message_types=supported_message_types,
        supported_features=supported_features,
        mas_instance_id=mas_instance_id,
        goep_l2cap_psm=goep_l2cap_psm,
    )


@dataclasses.dataclass(frozen=True, kw_only=True)
class MnsSdpInfo:
  """MAP Message Notification Service (MNS) SDP information."""

  service_record_handle: int
  rfcomm_channel: int
  version: Version
  service_name: str = 'MAP MNS'
  supported_features: ApplicationParameterValue.SupportedFeatures
  goep_l2cap_psm: int | None = None

  def to_sdp_records(self) -> list[sdp.ServiceAttribute]:
    """Converts the MSE SDP information to SDP records."""
    records = [
        sdp.ServiceAttribute(
            sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
            sdp.DataElement.unsigned_integer_32(self.service_record_handle),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence(
                [
                    sdp.DataElement.uuid(
                        core.BT_MESSAGE_NOTIFICATION_SERVER_SERVICE
                    )
                ],
            ),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence([
                sdp.DataElement.sequence(
                    [sdp.DataElement.uuid(core.BT_L2CAP_PROTOCOL_ID)]
                ),
                sdp.DataElement.sequence([
                    sdp.DataElement.uuid(core.BT_RFCOMM_PROTOCOL_ID),
                    sdp.DataElement.unsigned_integer_8(self.rfcomm_channel),
                ]),
                sdp.DataElement.sequence(
                    [sdp.DataElement.uuid(core.BT_OBEX_PROTOCOL_ID)]
                ),
            ]),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence([
                sdp.DataElement.sequence([
                    sdp.DataElement.uuid(
                        core.BT_MESSAGE_ACCESS_PROFILE_SERVICE
                    ),
                    sdp.DataElement.unsigned_integer_16(self.version.value),
                ])
            ]),
        ),
        sdp.ServiceAttribute(
            (
                _DEFAULT_LANGUAGE_BASED_ATTRIBUTE_ID_OFFSET
                + sdp.SDP_SERVICE_NAME_ATTRIBUTE_ID_OFFSET
            ),
            sdp.DataElement.text_string(self.service_name.encode('utf-8')),
        ),
        sdp.ServiceAttribute(
            AttributeId.MAP_SUPPORTED_FEATURES,
            sdp.DataElement.unsigned_integer_32(self.supported_features.value),
        ),
    ]
    if self.goep_l2cap_psm is not None:
      records.append(
          sdp.ServiceAttribute(
              AttributeId.GOEP_L2CAP_PSM,
              sdp.DataElement.unsigned_integer_16(self.goep_l2cap_psm),
          )
      )

    return records

  @classmethod
  async def find(
      cls: type[Self],
      connection: bumble_device.Connection,
  ) -> Self | None:
    """Finds SDP record for a MNS."""
    async with sdp.Client(connection) as sdp_client:
      result = await sdp_client.search_attributes(
          [core.BT_MESSAGE_NOTIFICATION_SERVER_SERVICE],
          [
              sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
              sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
              sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
              AttributeId.MAP_SUPPORTED_FEATURES,
              AttributeId.GOEP_L2CAP_PSM,
          ],
      )

    if not result:
      return None

    service_record_handle: int | None = None
    rfcomm_channel: int | None = None
    version: Version | None = None
    supported_features: ApplicationParameterValue.SupportedFeatures | None = (
        None
    )
    goep_l2cap_psm: int | None = None

    for attribute_lists in result:
      for attribute in attribute_lists:
        match attribute.id:
          case sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID:
            service_record_handle = attribute.value.value
          case sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID:
            for element in attribute.value.value:
              if element.value[0].value == core.BT_RFCOMM_PROTOCOL_ID:
                rfcomm_channel = element.value[1].value
          case sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID:
            for element in attribute.value.value:
              if (
                  element.value[0].value
                  == core.BT_MESSAGE_ACCESS_PROFILE_SERVICE
              ):
                version = Version(element.value[1].value)
          case AttributeId.MAP_SUPPORTED_FEATURES:
            supported_features = ApplicationParameterValue.SupportedFeatures(
                attribute.value.value
            )
          case AttributeId.GOEP_L2CAP_PSM:
            goep_l2cap_psm = attribute.value.value

    if (
        service_record_handle is None
        or rfcomm_channel is None
        or version is None
        or supported_features is None
    ):
      raise ValueError(
          'Incomplete SDP record, '
          f'service_record_handle: {service_record_handle}, '
          f'rfcomm_channel: {rfcomm_channel}, '
          f'version: {version}, '
          f'supported_features: {supported_features}, '
          f'goep_l2cap_psm: {goep_l2cap_psm}',
      )

    return cls(
        service_record_handle=service_record_handle,
        rfcomm_channel=rfcomm_channel,
        version=version,
        supported_features=supported_features,
        goep_l2cap_psm=goep_l2cap_psm,
    )


class MnsServerSession(obex.ServerSession):
  """MAP Message Notification Service (MNS) server session."""

  connected = asyncio.Event()
  notifications = asyncio.Queue[bytes]()

  @override
  def _on_connect(self, request: obex.ConnectRequest) -> None:
    if (target := request.headers.target) == MNS_TARGET_UUID.bytes:
      response = obex.ConnectResponse(
          response_code=obex.ResponseCode.SUCCESS,
          obex_version_number=obex.Version.V_1_0,
          flags=0,
          maximum_obex_packet_length=MAX_RFCOMM_OBEX_PACKET_LENGTH,
          headers=obex.Headers(connection_id=1, who=target),
      )
    else:
      response = obex.ConnectResponse(
          response_code=obex.ResponseCode.BAD_REQUEST,
          obex_version_number=obex.Version.V_1_0,
          flags=0,
          maximum_obex_packet_length=MAX_RFCOMM_OBEX_PACKET_LENGTH,
      )
    self.send_response(response)
    self.connected.set()

  @override
  def _on_put(self, request: obex.Request) -> None:
    if request.headers.type == ObexHeaderType.EVENT_REPORT.value and (
        body := (request.headers.body or request.headers.end_of_body)
    ):
      self.notifications.put_nowait(body)
      self.send_response(obex.Response(response_code=obex.ResponseCode.SUCCESS))
    else:
      self.send_response(
          obex.Response(response_code=obex.ResponseCode.BAD_REQUEST)
      )


class MnsServer:

  def __init__(self, rfcomm_manager: rfcomm.Manager) -> None:
    self.rfcomm_manager = rfcomm_manager
    self.sessions = asyncio.Queue[MnsServerSession]()
    self.rfcomm_channel = self.rfcomm_manager.register_acceptor(
        lambda dlc: self.sessions.put_nowait(MnsServerSession(dlc))
    )
