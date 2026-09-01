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

"""AVDTP with workarounds."""

from __future__ import annotations

from collections.abc import Callable
from typing import Self

from bumble import avdtp
from bumble import core
from bumble import device as device_lib
from bumble import l2cap
from bumble import utils


# -----------------------------------------------------------------------------
class Listener(utils.EventEmitter):
  """A listener for AVDTP connections.

  Attributes:
    servers_by_connection_handle: Servers, by connection handle.
    EVENT_CONNECTION: Event name for when a new connection is established.
    l2cap_server: The underlying L2CAP server.
  """

  servers_by_connection_handle: dict[int, avdtp.Protocol]
  l2cap_server: l2cap.ClassicChannelServer | None

  EVENT_CONNECTION = "connection"

  def __init__(
      self,
      *,
      version: tuple[int, int] = (1, 3),
      protocol_factory: Callable[..., avdtp.Protocol] | None = None,
  ) -> None:
    """Initializes the Listener.

    Args:
      version: The AVDTP version to support.
      protocol_factory: Factory function or class for creating the AVDTP
        protocol instance.
    """
    super().__init__()
    self.version = version
    self.servers_by_connection_handle = {}
    self.protocol_factory = protocol_factory or Protocol
    self.l2cap_server = None

  def set_server(
      self, connection: device_lib.Connection, server: avdtp.Protocol
  ) -> None:
    """Registers a server for a connection.

    Args:
      connection: The connection to register the server for.
      server: The server to register.
    """
    self.servers_by_connection_handle[connection.handle] = server

  def remove_server(self, connection: device_lib.Connection) -> None:
    """Deregisters a server for a connection.

    Args:
      connection: The connection to deregister the server for.
    """
    self.servers_by_connection_handle.pop(connection.handle, None)

  @classmethod
  def for_device(
      cls,
      device: device_lib.Device,
      *,
      version: tuple[int, int] = (1, 3),
      protocol_factory: Callable[..., avdtp.Protocol] | None = None,
  ) -> Listener:
    """Creates a Listener for a device.

    Args:
      device: The device to create the listener for.
      version: The AVDTP version to support.
      protocol_factory: Factory function or class for creating the AVDTP
        protocol instance.

    Returns:
      A Listener instance.
    """
    listener = Listener(version=version, protocol_factory=protocol_factory)
    l2cap_server = device.create_l2cap_server(
        spec=l2cap.ClassicChannelSpec(psm=avdtp.AVDTP_PSM),
    )
    l2cap_server.on(l2cap_server.EVENT_CONNECTION, listener.on_l2cap_connection)
    listener.l2cap_server = l2cap_server
    return listener

  def close(self) -> None:
    """Closes the listener and its underlying L2CAP server."""
    if self.l2cap_server:
      self.l2cap_server.close()
      self.l2cap_server = None

  def on_l2cap_connection(self, channel: l2cap.ClassicChannel) -> None:
    """Handles incoming L2CAP connection.

    Args:
      channel: The incoming classic L2CAP channel.
    """
    if channel.connection.handle in self.servers_by_connection_handle:
      # This is a channel for a stream endpoint.
      server = self.servers_by_connection_handle[channel.connection.handle]
      server.on_l2cap_connection(channel)
    else:
      # This is a new command/response channel.
      def on_channel_open() -> None:
        server = self.protocol_factory(channel, self.version)
        self.set_server(channel.connection, server)
        self.emit(self.EVENT_CONNECTION, server)

      def on_channel_close() -> None:
        self.remove_server(channel.connection)

      channel.on(channel.EVENT_OPEN, on_channel_open)
      channel.on(channel.EVENT_CLOSE, on_channel_close)


class Protocol(avdtp.Protocol):
  """AVDTP protocol with workarounds.

  Attributes:
    stream_factory: Factory function or class for creating Stream instances.
  """

  stream_factory: Callable[
      [
          avdtp.Protocol,
          avdtp.LocalStreamEndPoint,
          avdtp.StreamEndPointProxy,
      ],
      avdtp.Stream,
  ] = avdtp.Stream

  sink_factory: Callable[
      [
          avdtp.Protocol,
          int,
          avdtp.MediaCodecCapabilities,
      ],
      avdtp.LocalSink,
  ] = avdtp.LocalSink

  def __init__(
      self,
      l2cap_channel: l2cap.ClassicChannel,
      version: tuple[int, int] = (1, 3),
      *,
      sink_factory: (
          Callable[
              [avdtp.Protocol, int, avdtp.MediaCodecCapabilities],
              avdtp.LocalSink,
          ]
          | None
      ) = None,
  ) -> None:
    super().__init__(l2cap_channel, version)
    if sink_factory is not None:
      self.sink_factory = sink_factory

  def add_sink(
      self, codec_capabilities: avdtp.MediaCodecCapabilities
  ) -> avdtp.LocalSink:
    """Adds a local sink."""
    seid = len(self.local_endpoints) + 1
    sink = self.sink_factory(self, seid, codec_capabilities)
    self.local_endpoints.append(sink)
    return sink

  @classmethod
  async def connect(
      cls,
      connection: device_lib.Connection,
      version: tuple[int, int] = (1, 3),
      *,
      sink_factory: (
          Callable[
              [avdtp.Protocol, int, avdtp.MediaCodecCapabilities],
              avdtp.LocalSink,
          ]
          | None
      ) = None,
  ) -> Self:
    """Connects to a remote AVDTP server.

    Args:
      connection: The connection to connect over.
      version: The AVDTP version to support.
      sink_factory: Factory function or class for creating LocalSink instances.

    Returns:
      A Protocol instance.
    """
    channel = await connection.create_l2cap_channel(
        spec=l2cap.ClassicChannelSpec(psm=avdtp.AVDTP_PSM)
    )
    return cls(channel, version, sink_factory=sink_factory)

  async def create_stream(
      self, source: avdtp.LocalStreamEndPoint, sink: avdtp.StreamEndPointProxy
  ) -> avdtp.Stream:
    """Creates a stream.

    Args:
      source: Local stream endpoint.
      sink: Remote stream endpoint proxy.

    Returns:
      A Stream instance.
    """
    # Check that the source isn't already used in a stream.
    if source.in_use:
      raise core.InvalidStateError("source already in use")

    # Create or reuse a new stream to associate the source and the sink.
    if source.seid in self.streams:
      stream = self.streams[source.seid]
    else:
      stream = self.stream_factory(self, source, sink)
      self.streams[source.seid] = stream

    await stream.configure()

    return stream

  async def on_set_configuration_command(
      self, command: avdtp.Set_Configuration_Command
  ) -> avdtp.Message | None:
    """Handles incoming SetConfiguration command.

    Args:
      command: Incoming SetConfiguration command.

    Returns:
      SetConfiguration response or reject message.
    """
    endpoint = self.get_local_endpoint_by_seid(command.acp_seid)
    if endpoint is None:
      return avdtp.Set_Configuration_Reject(
          error_code=avdtp.AVDTP_BAD_ACP_SEID_ERROR
      )
    if endpoint.in_use:
      return avdtp.Set_Configuration_Reject(
          error_code=avdtp.AVDTP_SEP_IN_USE_ERROR
      )

    stream = self.stream_factory(
        self, endpoint, avdtp.StreamEndPointProxy(self, command.int_seid)
    )
    self.streams[command.acp_seid] = stream

    return (
        await stream.on_set_configuration_command(command.capabilities)
    ) or avdtp.Set_Configuration_Response()
