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

"""Implementation of Phonebook Access Profile (PBAP) on Bumble."""

from __future__ import annotations

import dataclasses
import enum
from typing import Self, Tuple
import uuid

from bumble import core
from bumble import device as bumble_device
from bumble import sdp

_DEFAULT_LANGUAGE_BASED_ATTRIBUTE_ID_OFFSET = 0x0100
# UUID to be attached in the Connect request target header.
TARGET_UUID = uuid.UUID('796135f0-f0c5-11d8-0966-0800200c9a66')


class AttributeId(enum.IntEnum):
  """See https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/service_discovery/attribute_ids/phone_book_access_profile.yaml."""

  GOEP_L2CAP_PSM = 0x0200
  SUPPORTED_REPOSITORIES = 0x0314
  PBAP_SUPPORTED_FEATURES = 0x0317


class Version(enum.IntEnum):
  """See PBAP specification, 7.1.2 SDP record for the PSE device."""

  V_1_0 = 0x0100
  V_1_1 = 0x0101
  V_1_2 = 0x0102


class SupportedRepositories(enum.IntFlag):
  """See PBAP specification, 7.1.2 SDP record for the PSE device."""

  LOCAL_PHONEBOOK = 0x0001
  SIM_CARD = 0x0002
  SPEED_DIAL = 0x0004
  FAVORITES = 0x0008


@dataclasses.dataclass(frozen=True, kw_only=True)
class ApplicationParameter:
  """See PBAP specification, 6.2.1 Application Parameters header."""

  tag: int
  value: bytes | int

  class Tag(enum.IntEnum):
    """See PBAP specification, 6.2.1 Application Parameters header."""

    ORDER = 0x01
    SEARCH_VALUE = 0x02
    SEARCH_PROPERTY = 0x03
    MAX_LIST_COUNT = 0x04
    LIST_START_OFFSET = 0x05
    PROPERTY_SELECTOR = 0x06
    FORMAT = 0x07
    PHONEBOOK_SIZE = 0x08
    NEW_MISSED_CALLS = 0x09
    PRIMARY_FOLDER_VERSION = 0x0A
    SECONDARY_FOLDER_VERSION = 0x0B
    V_CARD_SELECTOR = 0x0C
    DATABASE_IDENTIFIER = 0x0D
    V_CARD_SELECTOR_OPERATOR = 0x0E
    RESET_NEW_MISSED_CALLS = 0x0F
    PBAP_SUPPORTED_FEATURES = 0x10

  _APPLICATION_PARAMETER_LENGTHS = {
      Tag.ORDER: 1,
      Tag.SEARCH_PROPERTY: 1,
      Tag.MAX_LIST_COUNT: 2,
      Tag.LIST_START_OFFSET: 2,
      Tag.PROPERTY_SELECTOR: 8,
      Tag.FORMAT: 1,
      Tag.PHONEBOOK_SIZE: 2,
      Tag.NEW_MISSED_CALLS: 1,
      Tag.PRIMARY_FOLDER_VERSION: 16,
      Tag.SECONDARY_FOLDER_VERSION: 16,
      Tag.V_CARD_SELECTOR: 8,
      Tag.DATABASE_IDENTIFIER: 16,
      Tag.V_CARD_SELECTOR_OPERATOR: 1,
      Tag.RESET_NEW_MISSED_CALLS: 1,
      Tag.PBAP_SUPPORTED_FEATURES: 4,
      # Tag.SEARCH_VALUE should have bytes type.
  }

  def to_bytes(self) -> bytes:
    """Converts the application parameter to bytes."""
    if isinstance(self.value, bytes):
      return bytes([self.tag, len(self.value)]) + self.value
    else:
      length = self._APPLICATION_PARAMETER_LENGTHS[self.tag]
      return bytes([self.tag, length]) + self.value.to_bytes(length, 'big')

  def __bytes__(self) -> bytes:
    return self.to_bytes()

  @classmethod
  def parse_from(
      cls: type[Self], data: bytes, offset: int = 0
  ) -> Tuple[Self, int]:
    """Creates an application parameter from the given bytes."""
    tag = cls.Tag(data[offset])
    length = data[offset + 1]
    if tag == cls.Tag.SEARCH_VALUE:
      value = data[offset + 2 : offset + 2 + length]
    else:
      value = int.from_bytes(data[offset + 2 : offset + 2 + length], 'big')
    return cls(tag=tag, value=value), offset + 2 + length


@dataclasses.dataclass
class ApplicationParameters:
  """A collection of application parameters."""

  order: int | None = None
  search_value: bytes | None = None
  search_property: int | None = None
  max_list_count: int | None = None
  list_start_offset: int | None = None
  property_selector: int | None = None
  format: int | None = None
  phonebook_size: int | None = None
  new_missed_calls: int | None = None
  primary_folder_version: int | None = None
  secondary_folder_version: int | None = None
  v_card_selector: int | None = None
  database_identifier: int | None = None
  v_card_selector_operator: int | None = None
  reset_new_missed_calls: int | None = None
  pbap_supported_features: int | None = None

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
    parameters: list[ApplicationParameter] = []
    for tag, value in dataclasses.asdict(self).items():
      if value is None:
        continue
      parameters.append(
          ApplicationParameter(
              tag=ApplicationParameter.Tag[tag.upper()],
              value=value,
          )
      )
    return b''.join(bytes(header) for header in parameters)

  def __bytes__(self) -> bytes:
    return self.to_bytes()


class ApplicationParameterValue:
  """See PBAP specification, 6.2.1 Application Parameters header."""

  class Order(enum.IntEnum):
    INDEXED = 0x00
    ALPHANUMERIC = 0x01
    PHONETIC = 0x02

  class SearchProperty(enum.IntEnum):
    NAME = 0x00
    NUMBER = 0x01
    SOUND = 0x02

  class Format(enum.IntEnum):
    V_2_1 = 0x00
    V_3_0 = 0x01

  class VCardSelectorOperator(enum.IntEnum):
    OR = 0x00
    AND = 0x01

  class ResetNewMissedCalls(enum.IntEnum):
    RESET = 0x01

  class SupportedFeatures(enum.IntFlag):
    DOWNLOAD = 0x0001
    BROWSING = 0x0002
    DATABASE_IDENTIFIER = 0x0004
    FOLDER_VERSION_COUNTERS = 0x0008
    VCARD_SELECTING = 0x0010
    ENHANCED_MISSED_CALLS = 0x0020
    X_BT_UCI_VCARD_PROPERTY = 0x0040
    X_BT_UID_VCARD_PROPERTY = 0x0080
    CONTACT_REFERENCING = 0x0100
    DEFAULT_CONTACT_IMAGE_FORMAT = 0x0200

  class PropertyMask(enum.IntFlag):
    """See PBAP specification, Table 5.1: Property Mask."""

    VERSION = 1 << 0
    FN = 1 << 1
    N = 1 << 2
    PHOTO = 1 << 3
    BDAY = 1 << 4
    ADR = 1 << 5
    LABEL = 1 << 6
    TEL = 1 << 7
    EMAIL = 1 << 8
    MAILER = 1 << 9
    TZ = 1 << 10
    GEO = 1 << 11
    TITLE = 1 << 12
    ROLE = 1 << 13
    LOGO = 1 << 14
    AGENT = 1 << 15
    ORG = 1 << 16
    NOTE = 1 << 17
    REV = 1 << 18
    SOUND = 1 << 19
    URL = 1 << 20
    UID = 1 << 21
    KEY = 1 << 22
    NICKNAME = 1 << 23
    CATEGORIES = 1 << 24
    PROID = 1 << 25
    CLASS = 1 << 26
    SORT_STRING = 1 << 27
    X_IRMC_CALL_DATETIME = 1 << 28
    X_BT_SPEEDDIALKEY = 1 << 29
    X_BT_UCI = 1 << 30
    X_BT_UID = 1 << 31
    # Bit 32-38 are reserved for future use.
    PROPRIETARY_FILTER = 1 << 39


@dataclasses.dataclass(frozen=True, kw_only=True)
class PseSdpInfo:
  """PBAP Phone Book Server Equipment(PSE) SDP information."""

  service_record_handle: int
  rfcomm_channel: int
  version: Version
  service_name: str = 'Phonebook Access PSE'
  supported_repositories: SupportedRepositories
  supported_features: ApplicationParameterValue.SupportedFeatures
  goep_l2cap_psm: int | None = None

  def to_sdp_records(self) -> list[sdp.ServiceAttribute]:
    """Converts the PSE SDP information to SDP records."""
    records = [
        sdp.ServiceAttribute(
            sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
            sdp.DataElement.unsigned_integer_32(self.service_record_handle),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence(
                [sdp.DataElement.uuid(core.BT_PHONEBOOK_ACCESS_PSE_SERVICE)],
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
                    [sdp.DataElement.uuid(core.BT_PHONEBOOK_ACCESS_PSE_SERVICE)]
                ),
            ]),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence([
                sdp.DataElement.sequence([
                    sdp.DataElement.uuid(core.BT_PHONEBOOK_ACCESS_SERVICE),
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
            AttributeId.SUPPORTED_REPOSITORIES,
            sdp.DataElement.unsigned_integer_8(
                self.supported_repositories.value
            ),
        ),
        sdp.ServiceAttribute(
            AttributeId.PBAP_SUPPORTED_FEATURES,
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


@dataclasses.dataclass(frozen=True, kw_only=True)
class PceSdpInfo:
  """PBAP Phone Book Client Equipment(PCE) SDP information."""

  service_record_handle: int
  version: Version
  service_name: str = 'Phonebook Access PCE'

  def to_sdp_records(self) -> list[sdp.ServiceAttribute]:
    """Converts the PSE SDP information to SDP records."""
    records = [
        sdp.ServiceAttribute(
            sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
            sdp.DataElement.unsigned_integer_32(self.service_record_handle),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence(
                [sdp.DataElement.uuid(core.BT_PHONEBOOK_ACCESS_PCE_SERVICE)],
            ),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence([
                sdp.DataElement.sequence([
                    sdp.DataElement.uuid(core.BT_PHONEBOOK_ACCESS_SERVICE),
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
    ]
    return records


async def find_pse_sdp_record(
    connection: bumble_device.Connection,
) -> PseSdpInfo | None:
  """Finds SDP record for a PSE."""
  async with sdp.Client(connection) as sdp_client:
    result = await sdp_client.search_attributes(
        [core.BT_PHONEBOOK_ACCESS_PSE_SERVICE],
        [
            sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
            sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            AttributeId.SUPPORTED_REPOSITORIES,
            AttributeId.PBAP_SUPPORTED_FEATURES,
            AttributeId.GOEP_L2CAP_PSM,
        ],
    )

  if not result:
    return None

  service_record_handle: int | None = None
  rfcomm_channel: int | None = None
  version: Version | None = None
  supported_repositories: SupportedRepositories | None = None
  supported_features: ApplicationParameterValue.SupportedFeatures | None = None
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
            if element.value[0].value == core.BT_PHONEBOOK_ACCESS_SERVICE:
              version = Version(element.value[1].value)
        case AttributeId.SUPPORTED_REPOSITORIES:
          supported_repositories = SupportedRepositories(attribute.value.value)
        case AttributeId.PBAP_SUPPORTED_FEATURES:
          supported_features = ApplicationParameterValue.SupportedFeatures(
              attribute.value.value
          )
        case AttributeId.GOEP_L2CAP_PSM:
          goep_l2cap_psm = attribute.value.value

  if (
      service_record_handle is None
      or rfcomm_channel is None
      or version is None
      or supported_repositories is None
      or supported_features is None
  ):
    raise ValueError(
        'Incomplete SDP record, '
        f'service_record_handle: {service_record_handle}, '
        f'rfcomm_channel: {rfcomm_channel}, '
        f'version: {version}, '
        f'supported_repositories: {supported_repositories}, '
        f'supported_features: {supported_features}, '
        f'goep_l2cap_psm: {goep_l2cap_psm}',
    )

  return PseSdpInfo(
      service_record_handle=service_record_handle,
      rfcomm_channel=rfcomm_channel,
      version=version,
      supported_repositories=supported_repositories,
      supported_features=supported_features,
      goep_l2cap_psm=goep_l2cap_psm,
  )


async def find_pce_sdp_record(
    connection: bumble_device.Connection,
) -> PceSdpInfo | None:
  """Finds SDP record for a PCE."""
  async with sdp.Client(connection) as sdp_client:
    result = await sdp_client.search_attributes(
        [core.BT_PHONEBOOK_ACCESS_PCE_SERVICE],
        [
            sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
            sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
        ],
    )

  if not result:
    return None

  service_record_handle: int | None = None
  version: Version | None = None

  for attribute_lists in result:
    for attribute in attribute_lists:
      match attribute.id:
        case sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID:
          service_record_handle = attribute.value.value
        case sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID:
          for element in attribute.value.value:
            if element.value[0].value == core.BT_PHONEBOOK_ACCESS_SERVICE:
              version = Version(element.value[1].value)

  if service_record_handle is None or version is None:
    raise ValueError(
        'Incomplete SDP record, '
        f'service_record_handle: {service_record_handle}, '
        f'version: {version}'
    )

  return PceSdpInfo(
      service_record_handle=service_record_handle,
      version=version,
  )
