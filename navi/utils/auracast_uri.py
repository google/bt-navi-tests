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

"""Broadcast Audio Uniform Resource Identifier.

See:
https://www.bluetooth.com/specifications/specs/broadcast-audio-uniform-resource-identifier/.
"""

import base64
import dataclasses
import enum
import logging
from typing import Self

SCHEME_BT_BROADCAST_METADATA = 'BLUETOOTH:UUID:184F;'
SUFFIX_QR_CODE = ';;'

_logger = logging.getLogger(__name__)


class AdvertiserAddressType(enum.IntEnum):
  """Advertiser Address Type."""

  PUBLIC = 0
  RANDOM = 1


@dataclasses.dataclass
class BroadcastAudioUri:
  """Broadcast Audio Uniform Resource Identifier.

  Attributes:
    broadcast_name: The name of the broadcast.
    advertiser_address_type: The type of the advertiser address.
    advertiser_address: The address of the advertiser, in MAC address format.
    broadcast_id: The ID of the broadcast.
    broadcast_code: The broadcast encryption code in bytes.
    standard_quality: Whether the broadcast is standard quality.
    high_quality: Whether the broadcast is high quality.
    vendor_specific: The vendor specific data of the broadcast.
    advertising_sid: The advertising SID of the broadcast.
    pa_interval: The PA interval of the broadcast.
    num_subgroups: The number of subgroups of the broadcast.
    bis_sync: The BIS synchronization suggestion to the sink device.
    sg_number_of_bises: The number of BISes of the subgroup.
    sg_metadata: The metadata of the subgroup.
    public_broadcast_announcement_metadata: The metadata of the public broadcast
      announcement.
  """

  broadcast_name: str
  advertiser_address_type: AdvertiserAddressType | None = None
  advertiser_address: str | None = None
  broadcast_id: int | None = None
  broadcast_code: bytes | None = None
  standard_quality: bool | None = None
  high_quality: bool | None = None
  vendor_specific: str | None = None
  # Extended elements.
  advertising_sid: int | None = None
  pa_interval: int | None = None
  num_subgroups: int | None = None
  bis_sync: list[int] = dataclasses.field(default_factory=list)
  sg_number_of_bises: list[int] = dataclasses.field(default_factory=list)
  sg_metadata: list[bytes] = dataclasses.field(default_factory=list)
  public_broadcast_announcement_metadata: bytes | None = None

  def __str__(self) -> str:
    """Converts a BroadcastAudioUri to a string."""
    entries: list[str] = [
        f'BN:{base64.b64encode(self.broadcast_name.encode("utf-8")).decode("ascii")}'
    ]
    if self.advertiser_address_type is not None:
      entries.append(f'AT:{self.advertiser_address_type:X}')
    if self.advertiser_address is not None:
      entries.append(f'AD:{self.advertiser_address.replace(":", "")}')
    if self.broadcast_id is not None:
      entries.append(f'BI:{self.broadcast_id:X}')
    if self.broadcast_code is not None:
      entries.append(
          f'BC:{base64.b64encode(self.broadcast_code).decode("ascii")}'
      )
    if self.standard_quality is not None:
      entries.append(f'SQ:{"1" if self.standard_quality else "0"}')
    if self.high_quality is not None:
      entries.append(f'HQ:{"1" if self.high_quality else "0"}')
    if self.vendor_specific is not None:
      entries.append(f'VS:{self.vendor_specific}')
    if self.advertising_sid is not None:
      entries.append(f'AS:{self.advertising_sid:X}')
    if self.pa_interval is not None:
      entries.append(f'PI:{self.pa_interval:X}')
    if self.num_subgroups is not None:
      entries.append(f'NS:{self.num_subgroups:X}')
    for bis_sync in self.bis_sync:
      entries.append(f'BS:{bis_sync:X}')
    for sg_number_of_bises in self.sg_number_of_bises:
      entries.append(f'NB:{sg_number_of_bises:X}')
    for sg_metadata in self.sg_metadata:
      entries.append(f'SM:{base64.b64encode(sg_metadata).decode("ascii")}')
    if self.public_broadcast_announcement_metadata is not None:
      entries.append(
          f'PM:{base64.b64encode(self.public_broadcast_announcement_metadata).decode("ascii")}'
      )
    return SCHEME_BT_BROADCAST_METADATA + ';'.join(entries) + SUFFIX_QR_CODE

  @classmethod
  def from_string(cls: type[Self], uri: str) -> Self:
    """Creates a BroadcastAudioUri from a string."""
    if not uri.startswith(SCHEME_BT_BROADCAST_METADATA):
      raise ValueError(
          f'URI {uri} does not start with {SCHEME_BT_BROADCAST_METADATA}'
      )
    uri = uri.removeprefix(SCHEME_BT_BROADCAST_METADATA).removesuffix(
        SUFFIX_QR_CODE
    )
    uri_elements = [
        element.split(':', maxsplit=1) for element in uri.split(';')
    ]
    bis_sync = []
    sg_number_of_bises = []
    sg_metadata = []

    instance = cls('')
    for key, value in uri_elements:
      match key:
        case 'BS':
          bis_sync.append(int(value, 16))
        case 'NB':
          sg_number_of_bises.append(int(value, 16))
        case 'SM':
          sg_metadata.append(base64.b64decode(value))
        case 'BN':
          instance.broadcast_name = base64.b64decode(value).decode('utf-8')
        case 'AT':
          instance.advertiser_address_type = AdvertiserAddressType(int(value))
        case 'AD':
          instance.advertiser_address = ':'.join(
              [value[i : i + 2].upper() for i in range(0, 12, 2)]
          )
        case 'BI':
          instance.broadcast_id = int(value, 16)
        case 'BC':
          instance.broadcast_code = base64.b64decode(value)
        case 'SQ':
          instance.standard_quality = bool(int(value))
        case 'HQ':
          instance.high_quality = bool(int(value))
        case 'VS':
          instance.vendor_specific = value
        case 'AS':
          instance.advertising_sid = int(value, 16)
        case 'PI':
          instance.pa_interval = int(value, 16)
        case 'NS':
          instance.num_subgroups = int(value, 16)
        case 'PM':
          instance.public_broadcast_announcement_metadata = base64.b64decode(
              value
          )
        case _:
          _logger.warning('Unknown URI element: %s', key)

    instance.bis_sync = bis_sync
    instance.sg_number_of_bises = sg_number_of_bises
    instance.sg_metadata = sg_metadata
    return instance
