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

"""Bluetooth Object Push Profile (OPP) implementation."""

import asyncio
import dataclasses
import logging
from typing import Iterable

from bumble import core
from bumble import rfcomm
from bumble import sdp
from bumble import utils
import bumble.device
from typing_extensions import override

from navi.bumble_ext import obex

GOEP_L2CAP_PSM_ATTRIBUTE_ID = 0x0200
SERVICE_VERSION_ATTRIBUTE_ID = 0x0300
SUPPORTED_FORMAT_LIST_ATTRIBUTE_ID = 0x0303

# Same as Android.
MAX_RFCOMM_OBEX_PACKET_LENGTH = 65530
MIN_PACKET_LENGTH = 256
MIN_PUT_HEADER_SIZE = len(
    bytes(
        obex.Request(
            opcode=obex.Opcode.PUT,
            headers=obex.Headers(connection_id=0, body=b''),
            final=True,
        )
    )
)
MAX_PUT_HEADER_SIZE = len(
    bytes(
        obex.Request(
            opcode=obex.Opcode.PUT,
            headers=obex.Headers(
                connection_id=0,
                name='',
                type=b'',
                length=0,
                body=b'',
            ),
            final=True,
        )
    )
)

_logger = logging.getLogger(__name__)


class OppError(core.ProtocolError):

  def __init__(self, error_code: int, details: str = ''):
    super().__init__(
        error_code, 'OPP', obex.ResponseCode(error_code).name, details
    )


class Version(utils.OpenIntEnum):
  V_1_0 = 0x0100
  V_1_1 = 0x0101
  V_1_2 = 0x0102


@dataclasses.dataclass
class SdpInfo:
  """SDP information."""

  service_record_handle: int
  rfcomm_channel: int
  profile_version: Version
  supported_formats: Iterable[int] = (0xFF,)
  goep_l2cap_psm: int | None = None


def make_sdp_records(info: SdpInfo) -> list[sdp.ServiceAttribute]:
  """Makes SDP records for an OBEX server.

  Args:
    info: The SDP information of the OBEX server.

  Returns:
    The SDP records.
  """
  records = [
      sdp.ServiceAttribute(
          sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
          sdp.DataElement.unsigned_integer_32(info.service_record_handle),
      ),
      sdp.ServiceAttribute(
          sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
          sdp.DataElement.sequence(
              [sdp.DataElement.uuid(core.BT_OBEX_OBJECT_PUSH_SERVICE)],
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
                  sdp.DataElement.unsigned_integer_8(info.rfcomm_channel),
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
                  sdp.DataElement.uuid(core.BT_OBEX_OBJECT_PUSH_SERVICE),
                  sdp.DataElement.unsigned_integer_16(
                      info.profile_version.value
                  ),
              ])
          ]),
      ),
      sdp.ServiceAttribute(
          SUPPORTED_FORMAT_LIST_ATTRIBUTE_ID,
          sdp.DataElement.sequence([
              sdp.DataElement.unsigned_integer_8(supported_format)
              for supported_format in info.supported_formats
          ]),
      ),
  ]
  if info.goep_l2cap_psm is not None:
    records.append(
        sdp.ServiceAttribute(
            GOEP_L2CAP_PSM_ATTRIBUTE_ID,
            sdp.DataElement.unsigned_integer_16(info.goep_l2cap_psm),
        )
    )

  return records


async def find_sdp_record(
    connection: bumble.device.Connection,
) -> SdpInfo | None:
  """Finds SDP record for a service."""
  async with sdp.Client(connection) as sdp_client:
    result = await sdp_client.search_attributes(
        [core.BT_OBEX_OBJECT_PUSH_SERVICE],
        [
            sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
            sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            SUPPORTED_FORMAT_LIST_ATTRIBUTE_ID,
            GOEP_L2CAP_PSM_ATTRIBUTE_ID,
        ],
    )

  service_record_handle: int | None = None
  rfcomm_channel: int | None = None
  profile_version: Version | None = None
  supported_formats: Iterable[int] | None = None
  goep_l2cap_psm: int | None = None

  for attribute_lists in result:
    for attribute in attribute_lists:
      if attribute.id == sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID:
        service_record_handle = attribute.value.value
      elif attribute.id == sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID:
        protocol_descriptor_list = attribute.value.value
        rfcomm_channel = protocol_descriptor_list[1].value[1].value
      elif (
          attribute.id == sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID
      ):
        profile_descriptor_list = attribute.value.value
        profile_version = Version(profile_descriptor_list[0].value[1].value)
      elif attribute.id == SUPPORTED_FORMAT_LIST_ATTRIBUTE_ID:
        supported_formats = [element.value for element in attribute.value.value]
      elif attribute.id == GOEP_L2CAP_PSM_ATTRIBUTE_ID:
        goep_l2cap_psm = attribute.value.value

  if (
      service_record_handle is None
      or rfcomm_channel is None
      or profile_version is None
      or supported_formats is None
  ):
    raise ValueError('Incomplete SDP record')

  return SdpInfo(
      service_record_handle=service_record_handle,
      rfcomm_channel=rfcomm_channel,
      profile_version=profile_version,
      supported_formats=supported_formats,
      goep_l2cap_psm=goep_l2cap_psm,
  )


class Client(obex.ClientSession):
  """OPP client."""

  connection_id: int | None = None
  peer_max_obex_packet_length: int | None = None

  async def connect(self, count: int) -> None:
    """Connects to the server."""
    response = await self.send_request(
        obex.ConnectRequest(
            obex_version_number=obex.Version.V_1_0,
            flags=0,
            maximum_obex_packet_length=MAX_RFCOMM_OBEX_PACKET_LENGTH,
            headers=obex.Headers(count=count),
            final=True,
        )
    )
    assert isinstance(response, obex.ConnectResponse)
    self.connection_id = response.headers.connection_id
    self.peer_max_obex_packet_length = response.maximum_obex_packet_length

  async def transmit_file(
      self, file_name: str, file_content: bytes, file_type: str | None = None
  ) -> None:
    """Sends a PUT request."""
    if self.peer_max_obex_packet_length is None:
      raise RuntimeError('Not connected?')
    if self.peer_max_obex_packet_length < MIN_PACKET_LENGTH:
      raise RuntimeError(
          f'Packet length too small: {self.peer_max_obex_packet_length} <'
          f' {MIN_PACKET_LENGTH}'
      )

    offset = 0
    while offset < len(file_content):
      is_first = offset == 0
      max_body_length = (
          self.peer_max_obex_packet_length
          # Substract the length of the header.
          - (MAX_PUT_HEADER_SIZE if is_first else MIN_PUT_HEADER_SIZE)
          # Substract the length of the file name.
          - (len(file_name.encode('utf-16be')) + 2 if is_first else 0)
          # Substract the length of the file type.
          - (len(file_type) if is_first and file_type else 0)
      )
      is_final = (offset + max_body_length) >= len(file_content)
      body = file_content[offset : offset + max_body_length]
      response = await self.send_request(
          obex.Request(
              opcode=obex.Opcode.PUT,
              final=is_final,
              headers=obex.Headers(
                  name=file_name if is_first else None,
                  type=(
                      file_type.encode('utf-8')
                      if file_type and is_first
                      else None
                  ),
                  connection_id=self.connection_id,
                  length=len(file_content) if is_first else None,
                  body=None if is_final else body,
                  end_of_body=body if is_final else None,
              ),
          )
      )
      if (
          (
              # Continue response is not allowed for the last packet.
              response.response_code == obex.ResponseCode.CONTINUE
              and is_final
          )
          or (
              # Success response is only allowed for the last packet.
              response.response_code == obex.ResponseCode.SUCCESS
              and not is_final
          )
          or (
              response.response_code
              not in (
                  obex.ResponseCode.SUCCESS,
                  obex.ResponseCode.CONTINUE,
              )
          )
      ):
        raise OppError(response.response_code)
      offset += len(body)
      _logger.debug('Progress: %s / %s', offset, len(file_content))

  async def disconnect(self) -> None:
    response = await self.send_request(
        obex.Request(
            opcode=obex.Opcode.DISCONNECT,
            headers=obex.Headers(connection_id=self.connection_id),
            final=True,
        )
    )
    if response.response_code != obex.ResponseCode.SUCCESS:
      raise OppError(response.response_code)


@dataclasses.dataclass
class TransferSession:
  """OPP transfer session."""

  name: str | None = None
  length: int | None = None
  file_type: str | None = None
  body: bytes = b''

  def __str__(self) -> str:
    return (
        f'TransferSession(name={self.name}, length={self.length}, '
        f'file_type={self.file_type}, body={len(self.body)})'
    )


class ServerConnection(obex.ServerSession):
  """OPP connection."""

  def __init__(self, bearer: rfcomm.DLC):
    self.connections = set[int]()
    self.sessions = dict[int, TransferSession]()
    self.connection_result: asyncio.Future[int] = (
        asyncio.get_running_loop().create_future()
    )
    self.completed_sessions = asyncio.Queue[TransferSession]()
    self.disconnected = asyncio.Event()
    super().__init__(bearer)

  @override
  def _on_connect(self, request: obex.ConnectRequest) -> None:
    connection_id = max(self.connections) + 1 if self.connections else 1
    self.connections.add(connection_id)
    response = obex.ConnectResponse(
        response_code=obex.ResponseCode.SUCCESS,
        obex_version_number=request.obex_version_number,
        flags=request.flags,
        maximum_obex_packet_length=MAX_RFCOMM_OBEX_PACKET_LENGTH,
        headers=obex.Headers(connection_id=connection_id),
    )
    self.send_response(response)
    if self.connection_result:
      self.connection_result.set_result(request.headers.count)

  @override
  def _on_disconnect(self, request: obex.Request) -> None:
    if (
        request.headers.connection_id is None
        or request.headers.connection_id not in self.connections
    ):
      response = obex.Response(response_code=obex.ResponseCode.NOT_FOUND)
      self.send_response(response)
      return
    self.connections.remove(request.headers.connection_id)
    response = obex.Response(response_code=obex.ResponseCode.SUCCESS)
    self.send_response(response)

  @override
  def _on_put(self, request: obex.Request) -> None:
    if (
        request.headers.connection_id is None
        or request.headers.connection_id not in self.connections
    ):
      response = obex.Response(response_code=obex.ResponseCode.NOT_FOUND)
      self.send_response(response)
      return

    if (body := (request.headers.body or request.headers.end_of_body)) is None:
      response = obex.Response(response_code=obex.ResponseCode.FORBIDDEN)
      self.send_response(response)
      return

    connection_id = request.headers.connection_id
    if connection_id not in self.sessions:
      self.sessions[connection_id] = TransferSession()
    session = self.sessions[connection_id]
    session.body += body

    if request.headers.length is not None:
      session.length = request.headers.length
    if request.headers.name is not None:
      session.name = request.headers.name
    if request.headers.type is not None:
      session.file_type = request.headers.type.decode('utf-8')

    _logger.debug('Progress: %s / %s', len(session.body), session.length)
    if request.headers.end_of_body is not None or request.final:
      _logger.debug('Session completed: %s', session)
      self.completed_sessions.put_nowait(session)
      del self.sessions[connection_id]
      response_code = obex.ResponseCode.SUCCESS
    else:
      response_code = obex.ResponseCode.CONTINUE

    response = obex.Response(response_code=response_code)
    self.send_response(response)


class Server:
  """OPP server."""

  def __init__(self, rfcomm_server: rfcomm.Server) -> None:
    self.rfcomm_server = rfcomm_server
    self.device = rfcomm_server.device
    self.rfcomm_channel = self.rfcomm_server.listen(self._on_rfcomm_dlc)
    self._pending_connections = asyncio.Queue[ServerConnection]()

  def _on_rfcomm_dlc(self, dlc: rfcomm.DLC) -> None:
    connection = ServerConnection(dlc)
    self._pending_connections.put_nowait(connection)

  async def wait_connection(self) -> ServerConnection:
    """Waits for a connection."""
    return await self._pending_connections.get()
