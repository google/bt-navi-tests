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

"""Extended RFCOMM implemenatation from Bumble."""

from collections.abc import Callable
from typing import TypeAlias

from bumble import device as bumble_device
from bumble import l2cap
from bumble import rfcomm
from typing_extensions import override

_logger = rfcomm.logger

DLC: TypeAlias = rfcomm.DLC


class Multiplexer(rfcomm.Multiplexer):
  """Multiplexer for RFCOMM."""

  def __init__(
      self,
      l2cap_channel: l2cap.ClassicChannel,
      role: rfcomm.Multiplexer.Role,
      dlc_callback: Callable[[DLC], None],
      acceptor: Callable[[int], bool] | None = None,
  ) -> None:
    super().__init__(l2cap_channel, role)
    self.dlc_callback = dlc_callback
    self.acceptor = acceptor

  @override
  def on_mcc_pn(self, c_r: bool, pn: rfcomm.RFCOMM_MCC_PN) -> None:
    if c_r:
      _logger.debug('<<< PN Command: %s', pn)

      channel_number = pn.dlci >> 1
      if self.acceptor and self.acceptor(channel_number):
        # Create a new DLC
        dlc = rfcomm.DLC(
            self,
            dlci=pn.dlci,
            tx_max_frame_size=pn.max_frame_size,
            tx_initial_credits=pn.initial_credits,
            rx_max_frame_size=rfcomm.RFCOMM_DEFAULT_MAX_FRAME_SIZE,
            rx_initial_credits=rfcomm.RFCOMM_DEFAULT_INITIAL_CREDITS,
        )
        self.dlcs[pn.dlci] = dlc

        # Re-emit the handshake completion event
        dlc.on('open', lambda: self.dlc_callback(dlc))

        # Respond to complete the handshake
        dlc.accept()
      else:
        # No acceptor, we're in Disconnected Mode
        self.send_frame(rfcomm.RFCOMM_Frame.dm(c_r=1, dlci=pn.dlci))
    else:
      # Response
      _logger.debug('>>> PN Response: %s', pn)
      if self.state == Multiplexer.State.OPENING:
        assert self.open_pn
        dlc = rfcomm.DLC(
            self,
            dlci=pn.dlci,
            tx_max_frame_size=pn.max_frame_size,
            tx_initial_credits=pn.initial_credits,
            rx_max_frame_size=self.open_pn.max_frame_size,
            rx_initial_credits=self.open_pn.initial_credits,
        )
        self.dlcs[pn.dlci] = dlc
        self.open_pn = None
        dlc.connect()
      else:
        _logger.warning('ignoring PN response')

  @override
  def on_l2cap_channel_close(self) -> None:
    super().on_l2cap_channel_close()
    self.emit('close')


class Manager:
  """Manager for RFCOMM."""

  def __init__(self, device: bumble_device.Device):
    self.device = device
    self.acceptors: dict[int, Callable[[rfcomm.DLC], None]] = {}
    self.responder_multiplexers = dict[bumble_device.Connection, Multiplexer]()
    self.initiator_multiplexers = dict[bumble_device.Connection, Multiplexer]()

    # Register ourselves with the L2CAP channel manager
    self.l2cap_server = device.create_l2cap_server(
        spec=l2cap.ClassicChannelSpec(
            psm=rfcomm.RFCOMM_PSM, mtu=l2cap.L2CAP_DEFAULT_MTU
        ),
        handler=self._on_l2cap_connection,
    )

  def _on_l2cap_connection(self, l2cap_channel: l2cap.ClassicChannel) -> None:
    _logger.debug('+++ new L2CAP connection: %s', l2cap_channel)
    l2cap_channel.on('open', lambda: self._on_l2cap_channel_open(l2cap_channel))

  def _on_l2cap_channel_open(self, l2cap_channel: l2cap.ClassicChannel) -> None:
    _logger.debug('$$$ L2CAP channel open: %s', l2cap_channel)

    # Create a new multiplexer for the channel
    multiplexer = Multiplexer(
        l2cap_channel,
        Multiplexer.Role.RESPONDER,
        dlc_callback=self._on_dlc,
        acceptor=lambda channel_number: channel_number in self.acceptors,
    )
    self.responder_multiplexers[l2cap_channel.connection] = multiplexer
    multiplexer.once(
        'close',
        lambda: self.responder_multiplexers.pop(l2cap_channel.connection, None),
    )

  async def connect(self, connection: bumble_device.Connection) -> Multiplexer:
    """Create a RFCOMM multiplexer on the given connection."""
    l2cap_channel = await connection.create_l2cap_channel(
        spec=l2cap.ClassicChannelSpec(
            psm=rfcomm.RFCOMM_PSM, mtu=l2cap.L2CAP_DEFAULT_MTU
        )
    )
    multiplexer = Multiplexer(
        l2cap_channel,
        Multiplexer.Role.INITIATOR,
        dlc_callback=self._on_dlc,
        acceptor=lambda channel_number: channel_number in self.acceptors,
    )
    await multiplexer.connect()
    self.initiator_multiplexers[l2cap_channel.connection] = multiplexer
    multiplexer.once(
        'close',
        lambda: self.initiator_multiplexers.pop(l2cap_channel.connection, None),
    )
    return multiplexer

  def register_acceptor(
      self,
      acceptor: Callable[[rfcomm.DLC], None],
      channel_number: int | None = None,
  ) -> int:
    """Register an acceptor for a RFCOMM channel.

    Args:
      acceptor: The acceptor function to call when a new DLC is connected.
      channel_number: The channel number to use. If not specified, a free
        channel number will be chosen.

    Returns:
      The channel number used.

    Raises:
      ValueError: If the channel number is already in use.
      RuntimeError: If no free channel number is found.
    """
    if channel_number:
      if channel_number in self.acceptors:
        # Busy
        raise ValueError(f'Channel {channel_number} is already in use')
    else:
      channel_number = next(
          (
              candidate
              for candidate in range(
                  rfcomm.RFCOMM_DYNAMIC_CHANNEL_NUMBER_START,
                  rfcomm.RFCOMM_DYNAMIC_CHANNEL_NUMBER_END + 1,
              )
              if candidate not in self.acceptors
          ),
          None,
      )
      if not channel_number:
        raise RuntimeError('No free channel number found')

    self.acceptors[channel_number] = acceptor

    return channel_number

  def _on_dlc(self, dlc: rfcomm.DLC) -> None:
    _logger.debug('@@@ new DLC connected: %s', dlc)

    # Let the acceptor know
    if acceptor := self.acceptors.get(dlc.dlci >> 1):
      acceptor(dlc)
