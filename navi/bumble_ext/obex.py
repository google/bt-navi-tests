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

"""Bluetooth OBEX implementation.

References:
  * IrDA Object Exchange Protocol, Version 1.5:
  https://btprodspecificationrefs.blob.core.windows.net/ext-ref/IrDA/OBEX15.pdf
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import dataclasses
import enum
import logging
import pprint
import struct
from typing import Final, Self, Tuple, Type, cast

from bumble import rfcomm

_logger = logging.getLogger(__name__)

FINAL_FLAG = 0x80


class Version(enum.IntEnum):
  V_1_0 = 0x10


class SingleResponseMode(enum.IntEnum):
  DISABLED = 0x00
  ENABLED = 0x01
  SUPPORTED = 0x02


class HeaderIdentifier(enum.IntEnum):
  """1-byte identifiers for OBEX headers."""

  COUNT = 0xC0
  NAME = 0x01
  TYPE = 0x42
  LENGTH = 0xC3
  TIME = 0x44
  DESCRIPTION = 0x05
  TARGET = 0x46
  HTTP = 0x47
  BODY = 0x48
  END_OF_BODY = 0x49
  WHO = 0x4A
  CONNECTION_ID = 0xCB
  APP_PARAMETERS = 0x4C
  AUTH_CHALLENGE = 0x4D
  AUTH_RESPONSE = 0x4E
  CREATOR_ID = 0xCF
  WAN_UUID = 0x50
  OBJECT_CLASS = 0x51
  SESSION_PARAMETERS = 0x52
  SESSION_SEQUENCE_NUMBER = 0x93
  ACTION_ID = 0x94
  DEST_NAME = 0x15
  PERMISSIONS = 0xD6
  SINGLE_RESPONSE_MODE = 0x97
  SINGLE_RESPONSE_MODE_PARAMETERS = 0x98

  def __str__(self) -> str:
    return f'{self.name}[0x{self.value:02X}]'


class Opcode(enum.IntEnum):
  CONNECT = 0x00
  DISCONNECT = 0x01
  PUT = 0x02
  GET = 0x03
  SETPATH = 0x05
  ACTION = 0x06
  SESSION = 0x07
  ABORT = 0x7F

  def __str__(self) -> str:
    return f'{self.name}[0x{self.value:02X}]'


class ResponseCode(enum.IntEnum):
  """See OBEX 1.5 spec section 3.2.1 Response Code values."""

  CONTINUE = 0x10
  SUCCESS = 0x20
  CREATED = 0x21
  ACCEPTED = 0x22
  NON_AUTHORITATIVE_INFORMATION = 0x23
  NO_CONTENT = 0x24
  RESET_CONTENT = 0x25
  PARTIAL_CONTENT = 0x26
  MULTIPLE_CHOICES = 0x30
  MOVED_PERMANENTLY = 0x31
  MOVED_TEMPORARILY = 0x32
  SEE_OTHER = 0x33
  NOT_MODIFIED = 0x34
  USE_PROXY = 0x35
  BAD_REQUEST = 0x40
  UNAUTHORIZED = 0x41
  PAYMENT_REQUIRED = 0x42
  FORBIDDEN = 0x43
  NOT_FOUND = 0x44
  METHOD_NOT_ALLOWED = 0x45
  NOT_ACCEPTABLE = 0x46
  PROXY_AUTHENTICATION_REQUIRED = 0x47
  REQUEST_TIME_OUT = 0x48
  CONFLICT = 0x49
  GONE = 0x4A
  LENGTH_REQUIRED = 0x4B
  PRECONDITION_FAILED = 0x4C
  REQUESTED_ENTITY_TOO_LARGE = 0x4D
  REQUEST_URL_TOO_LARGE = 0x4E
  UNSUPPORTED_MEDIA_TYPE = 0x4F
  INTERNAL_SERVER_ERROR = 0x50
  NOT_IMPLEMENTED = 0x51
  BAD_GATEWAY = 0x52
  SERVICE_UNAVAILABLE = 0x53
  GATEWAY_TIMEOUT = 0x54
  HTTP_VERSION_NOT_SUPPORTED = 0x55
  DATABASEFULL = 0x60
  DATABASELOCKED = 0x61

  def __str__(self) -> str:
    return f'{self.name}[0x{self.value:02X}]'


class SduReassembler:
  """OBEX SDU reassembler."""

  def __init__(self, callback: Callable[[bytes], None]):
    self._buffer = b''
    self._callback = callback

  def feed(self, data: bytes) -> None:
    self._buffer += data
    while len(self._buffer) >= 3:
      length = struct.unpack_from('>H', self._buffer, 1)[0]
      if len(self._buffer) < length:
        break
      sdu = self._buffer[:length]
      self._buffer = self._buffer[length:]
      self._callback(sdu)


@dataclasses.dataclass(frozen=True, kw_only=True)
class Header:
  """Obex header."""

  id: HeaderIdentifier
  value: int | str | bytes

  @classmethod
  def parse_from(
      cls: Type[Self], data: bytes, offset: int = 0
  ) -> Tuple[Self, int]:
    """Creates a header from the given bytes."""
    id_ = HeaderIdentifier(data[offset])
    offset += 1
    match id_ >> 6:
      case 0b00:
        # 2-bytes big-endian unsigned length(including header),
        # null-terminated Big-endian Unicode string
        length = struct.unpack_from('>H', data, offset)[0] - 3
        value = data[offset + 2 : offset + 2 + length].decode('utf-16be')
        if value.endswith('\0'):
          value = value[:-1]
        header = cls(id=id_, value=value)
        offset += length + 2
      case 0b01:
        # 2-bytes big-endian unsigned length(including header), byte sequence
        length = struct.unpack_from('>H', data, offset)[0] - 3
        value = data[offset + 2 : offset + 2 + length]
        header = cls(id=id_, value=value)
        offset += length + 2
      case 0b10:
        # 1 byte quantity
        header = cls(id=id_, value=data[offset])
        offset += 1
      case 0b11:
        # 4 bytes quantity, big endian
        header = cls(id=id_, value=struct.unpack_from('>I', data, offset)[0])
        offset += 4
      case _:
        raise NotImplementedError(f'Unsupported header: {id_}')
    return header, offset

  def __bytes__(self):
    match self.id >> 6:
      case 0b00:
        # 2-bytes big-endian unsigned length(including header),
        # null-terminated Big-endian Unicode string
        string_value = cast(str, self.value)
        if not string_value.endswith('\0'):
          string_value += '\0'
        value_bytes = string_value.encode('utf-16be')
        return struct.pack('>BH', self.id, len(value_bytes) + 3) + value_bytes
      case 0b01:
        # 2-bytes big-endian unsigned length(including header), byte sequence
        return struct.pack('>BH', self.id, len(self.value) + 3) + self.value
      case 0b10:
        # 1 byte quantity
        return struct.pack('>BB', self.id, self.value)
      case 0b11:
        # 4 bytes quantity, big endian
        return struct.pack('>BI', self.id, self.value)
      case _:
        raise NotImplementedError(f'Unsupported header: {self.id}')


@dataclasses.dataclass(frozen=True, kw_only=True)
class Headers:
  """OBEX headers."""

  count: int | None = None
  name: str | None = None
  type: bytes | None = None
  length: int | None = None
  time: bytes | None = None
  description: str | None = None
  target: bytes | None = None
  http: bytes | None = None
  body: bytes | None = None
  end_of_body: bytes | None = None
  who: bytes | None = None
  connection_id: int | None = None
  app_parameters: bytes | None = None
  auth_challenge: bytes | None = None
  auth_response: bytes | None = None
  creator_id: int | None = None
  wan_uuid: bytes | None = None
  object_class: bytes | None = None
  session_parameters: bytes | None = None
  session_sequence_number: int | None = None
  action_id: int | None = None
  dest_name: str | None = None
  permissions: int | None = None
  single_response_mode: int | None = None
  single_response_mode_parameters: int | None = None

  @classmethod
  def parse_from(
      cls: Type[Self], data: bytes, offset: int = 0
  ) -> Tuple[int, Self]:
    """Parses a list of headers from the given bytes.

    Args:
      data: The bytes to parse.
      offset: The offset in the bytes to start parsing from.

    Returns:
      A list of parsed headers.
    """
    headers = {}
    while offset < len(data):
      header, offset = Header.parse_from(data, offset)
      headers[header.id.name.lower()] = header.value

    return offset, cls(**headers)

  def __bytes__(self) -> bytes:
    headers: list[Header] = []
    for header_identifier, value in dataclasses.asdict(self).items():
      if value is None:
        continue
      headers.append(
          Header(
              id=HeaderIdentifier[header_identifier.upper()],
              value=value,
          )
      )
    return b''.join(bytes(header) for header in headers)


@dataclasses.dataclass(frozen=True, kw_only=True)
class Request:
  """Generic Obex operation class."""

  opcode: int
  final: bool
  headers: Headers = dataclasses.field(default_factory=Headers)

  @classmethod
  def from_bytes(cls: Type[Self], data: bytes) -> Request:
    """Creates a Request packet from the given bytes.

    Args:
      data: The bytes to parse.

    Returns:
      The parsed Request packet.

    Raises:
      InvalidPacketSizeError: The packet size is invalid.
    """
    opcode = data[0] & 0x7F
    final = bool(data[0] >> 7)

    if opcode == Opcode.CONNECT:
      return ConnectRequest.from_bytes(data)
    elif opcode == Opcode.ACTION:
      return ActionRequest.from_bytes(data)
    elif opcode == Opcode.SETPATH:
      return SetpathRequest.from_bytes(data)

    return cls(
        opcode=opcode,
        final=final,
        headers=Headers.parse_from(data, 3)[1],
    )

  def __bytes__(self) -> bytes:
    header_bytes = bytes(self.headers)
    return (
        struct.pack(
            '>BH',
            self.opcode | self.final << 7,
            len(header_bytes) + 3,
        )
        + header_bytes
    )


@dataclasses.dataclass(frozen=True, kw_only=True)
class ConnectRequest(Request):
  """OBEX Connect request."""

  obex_version_number: int
  flags: int
  maximum_obex_packet_length: int
  opcode: Final[Opcode] = Opcode.CONNECT

  @classmethod
  def from_bytes(cls: Type[Self], data: bytes) -> ConnectRequest:
    """Creates a Request packet from the given bytes.

    Args:
      data: The bytes to parse.

    Returns:
      The parsed Request packet.
    """
    final = bool(data[0] >> 7)
    obex_version_number, flags, maximum_obex_packet_length = struct.unpack_from(
        '>BBH', data, 3
    )

    return cls(
        final=final,
        obex_version_number=obex_version_number,
        flags=flags,
        maximum_obex_packet_length=maximum_obex_packet_length,
        headers=Headers.parse_from(data, 7)[1],
    )

  def __bytes__(self) -> bytes:
    header_bytes = bytes(self.headers)
    return (
        struct.pack(
            '>BHBBH',
            self.opcode | self.final << 7,
            len(header_bytes) + 7,
            self.obex_version_number,
            self.flags,
            self.maximum_obex_packet_length,
        )
        + header_bytes
    )


@dataclasses.dataclass(frozen=True, kw_only=True)
class ActionRequest(Request):
  """OBEX Action request."""

  action_identifier_header: int
  opcode: Final[Opcode] = Opcode.ACTION

  @classmethod
  def from_bytes(cls: Type[Self], data: bytes) -> ActionRequest:
    """Creates a Request packet from the given bytes.

    Args:
      data: The bytes to parse.

    Returns:
      The parsed Request packet.
    """
    final = bool(data[0] >> 7)
    action_identifier_header = struct.unpack_from('>H', data, 3)[0]

    return cls(
        final=final,
        action_identifier_header=action_identifier_header,
        headers=Headers.parse_from(data, 5)[1],
    )

  def __bytes__(self) -> bytes:
    header_bytes = bytes(self.headers)
    return (
        struct.pack(
            '>BHH',
            self.opcode | self.final << 7,
            len(header_bytes) + 5,
            self.action_identifier_header,
        )
        + header_bytes
    )


@dataclasses.dataclass(frozen=True, kw_only=True)
class SetpathRequest(Request):
  """OBEX SetPath request."""

  class Flags(enum.IntFlag):
    GO_TO_PARENT_FOLDER = 0x01
    DO_NOT_CREATE_FOLDER_IF_NOT_EXIST = 0x02

  opcode: Final[Opcode] = Opcode.SETPATH
  flags: Flags
  constants: int = 0

  @classmethod
  def from_bytes(cls: Type[Self], data: bytes) -> SetpathRequest:
    """Creates a Request packet from the given bytes.

    Args:
      data: The bytes to parse.

    Returns:
      The parsed Request packet.
    """
    return cls(
        final=bool(data[0] >> 7),
        flags=cls.Flags(data[3]),
        constants=data[4],
        headers=Headers.parse_from(data, 5)[1],
    )

  def __bytes__(self) -> bytes:
    header_bytes = bytes(self.headers)
    return (
        struct.pack(
            '>BHBB',
            self.opcode | self.final << 7,
            len(header_bytes) + 5,
            self.flags,
            self.constants,
        )
        + header_bytes
    )


@dataclasses.dataclass(frozen=True, kw_only=True)
class Response:
  """Generic Obex operation class.

  Attribute:
    response_code: The response code.
    final: Whether the response has final flag set.
    headers: The OBEX headers.
  """

  response_code: ResponseCode
  final: bool = True
  headers: Headers = dataclasses.field(default_factory=Headers)

  @classmethod
  def from_bytes(cls: Type[Self], data: bytes) -> Response:
    """Creates a Response packet from the given bytes."""
    response_code = data[0]
    final = response_code & FINAL_FLAG
    response_code = ResponseCode(response_code & ~FINAL_FLAG)
    headers = Headers.parse_from(data, 3)[1]
    return cls(
        response_code=ResponseCode(response_code),
        final=bool(final),
        headers=headers,
    )

  def __bytes__(self) -> bytes:
    header_bytes = bytes(self.headers)
    return (
        struct.pack(
            '>BH',
            self.response_code | self.final << 7,
            len(header_bytes) + 3,
        )
        + header_bytes
    )


@dataclasses.dataclass(frozen=True, kw_only=True)
class ConnectResponse(Response):
  """OBEX Connect response."""

  obex_version_number: int
  flags: int
  maximum_obex_packet_length: int

  @classmethod
  def from_bytes(cls: Type[Self], data: bytes) -> ConnectResponse:
    """Creates a Response packet from the given bytes."""
    (
        response_code,
        _,
        obex_version_number,
        flags,
        maximum_obex_packet_length,
    ) = struct.unpack_from('>BHBBH', data, 0)
    final = response_code & FINAL_FLAG
    response_code = ResponseCode(response_code & ~FINAL_FLAG)
    headers = Headers.parse_from(data, 7)[1]
    return cls(
        response_code=ResponseCode(response_code),
        final=final,
        headers=headers,
        obex_version_number=obex_version_number,
        flags=flags,
        maximum_obex_packet_length=maximum_obex_packet_length,
    )

  def __bytes__(self) -> bytes:
    header_bytes = bytes(self.headers)
    return (
        struct.pack(
            '>BHBBH',
            self.response_code | (self.final << 7),
            len(header_bytes) + 7,
            self.obex_version_number,
            self.flags,
            self.maximum_obex_packet_length,
        )
        + header_bytes
    )


class ClientSession:
  """OBEX client session."""

  last_request_opcode: int | None = None
  _pending_response: asyncio.Future[Response] | None = None
  connection_id: int | None = None
  peer_max_obex_packet_length: int | None = None

  def __init__(self, bearer: rfcomm.DLC) -> None:
    self.bearer = bearer
    self._reassembler = SduReassembler(self._on_sdu)
    self.bearer.sink = self._reassembler.feed

  async def send_request(self, request: Request) -> Response:
    """Sends a request."""
    _logger.debug('>>> Sending OBEX request: \n%s', pprint.pformat(request))
    self.bearer.write(bytes(request))
    self.last_request_opcode = request.opcode
    self._pending_response = asyncio.get_running_loop().create_future()
    return await self._pending_response

  def _on_sdu(self, pdu: bytes) -> None:
    """Handles an incoming PDU."""
    response: Response
    match self.last_request_opcode:
      case Opcode.CONNECT:
        response = ConnectResponse.from_bytes(pdu)
      case (
          Opcode.DISCONNECT
          | Opcode.PUT
          | Opcode.GET
          | Opcode.SETPATH
          | Opcode.ACTION
          | Opcode.SESSION
          | Opcode.ABORT
      ):
        response = Response.from_bytes(pdu)
      case _:
        _logger.error('Unexpected response: %s', pdu)
        return

    _logger.debug('<<< Received OBEX response: \n%s', pprint.pformat(response))
    if self._pending_response:
      self._pending_response.set_result(response)


class ServerSession:
  """OBEX server session."""

  def __init__(
      self,
      bearer: rfcomm.DLC,
  ) -> None:
    self.bearer = bearer
    self._reassembler = SduReassembler(self._on_sdu)
    self.bearer.sink = self._reassembler.feed

  def send_response(self, response: Response) -> None:
    """Sends a response."""
    _logger.debug('>>> Sending OBEX response: \n%s', pprint.pformat(response))
    self.bearer.write(bytes(response))

  def _on_sdu(self, sdu: bytes) -> None:
    """Handles an incoming SDU."""
    request = Request.from_bytes(sdu)

    _logger.debug('<<< Received OBEX request: \n%s', pprint.pformat(request))
    match request.opcode:
      case Opcode.CONNECT:
        self._on_connect(cast(ConnectRequest, request))
      case Opcode.DISCONNECT:
        self._on_disconnect(request)
      case Opcode.PUT:
        self._on_put(request)
      case Opcode.GET:
        self._on_get(request)
      case Opcode.SETPATH:
        self._on_setpath(request)
      case Opcode.ACTION:
        self._on_action(cast(ActionRequest, request))
      case Opcode.SESSION:
        self._on_session(request)
      case Opcode.ABORT:
        self._on_abort(request)
      case _:
        self.send_response(Response(response_code=ResponseCode.NOT_IMPLEMENTED))

  def _on_connect(self, request: ConnectRequest) -> None:
    """Handles a CONNECT request."""
    del request  # Used by derived classes.
    response = ConnectResponse(
        response_code=ResponseCode.NOT_IMPLEMENTED,
        obex_version_number=0,
        flags=0,
        maximum_obex_packet_length=0,
    )
    self.send_response(response)

  def _on_disconnect(self, request: Request) -> None:
    """Handles a DISCONNECT request."""
    del request  # Used by derived classes.
    response = Response(response_code=ResponseCode.NOT_IMPLEMENTED)
    self.send_response(response)

  def _on_put(self, request: Request) -> None:
    """Handles a PUT request."""
    del request  # Used by derived classes.
    response = Response(response_code=ResponseCode.NOT_IMPLEMENTED)
    self.send_response(response)

  def _on_get(self, request: Request) -> None:
    """Handles a GET request."""
    del request  # Used by derived classes.
    response = Response(response_code=ResponseCode.NOT_IMPLEMENTED)
    self.send_response(response)

  def _on_setpath(self, request: Request) -> None:
    """Handles a SETPATH request."""
    del request  # Used by derived classes.
    response = Response(response_code=ResponseCode.NOT_IMPLEMENTED)
    self.send_response(response)

  def _on_action(self, request: ActionRequest) -> None:
    """Handles a ACTION request."""
    del request  # Used by derived classes.
    response = Response(response_code=ResponseCode.NOT_IMPLEMENTED)
    self.send_response(response)

  def _on_session(self, request: Request) -> None:
    """Handles a SESSION request."""
    del request  # Used by derived classes.
    response = Response(response_code=ResponseCode.NOT_IMPLEMENTED)
    self.send_response(response)

  def _on_abort(self, request: Request) -> None:
    """Handles a ABORT request."""
    del request  # Used by derived classes.
    response = Response(response_code=ResponseCode.NOT_IMPLEMENTED)
    self.send_response(response)
