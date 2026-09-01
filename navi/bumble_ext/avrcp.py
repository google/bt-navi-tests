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

"""Custom AVRCP Bumble implementation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import dataclasses
import logging
import pprint
import struct
from typing import cast, override

from bumble import avctp
from bumble import avrcp
from bumble import core
from bumble import device as bumble_device
from bumble import l2cap
from bumble import sdp

from navi.bumble_ext import obex

logger = logging.getLogger(__name__)

_AVCTP_MAX_TRANSACTION_LABEL = 16
_CHARSET_ID_UTF_8 = avrcp.CharacterSetId(avrcp.CharacterSetId.UTF_8)


class Error(core.ProtocolError):
  """Base error class for AVRCP."""

  def __init__(self, error_code: avrcp.StatusCode, details: str = "") -> None:
    super().__init__(
        error_code=error_code,
        error_namespace="AVRCP",
        error_name=error_code.name,
        details=details,
    )


class BrowsableItem:
  """Abstract BrowsableItem class without some protocol details."""

  def to_avrcp_item(self) -> avrcp.BrowseableItem:
    raise NotImplementedError()

  @classmethod
  def from_avrcp_item(cls, item: avrcp.BrowseableItem) -> BrowsableItem:
    match item:
      case avrcp.MediaElementItem():
        return MediaElement.from_avrcp_item(item)
      case avrcp.FolderItem():
        return Folder.from_avrcp_item(item)
      case avrcp.MediaPlayerItem():
        return Player.from_avrcp_item(item)
      case _:
        raise core.InvalidPacketError(f"Unsupported item type: {type(item)}")


@dataclasses.dataclass
class MediaElement(BrowsableItem):
  """Abstract MediaElement class without some protocol details."""

  media_element_uid: int
  displayable_name: str
  media_type: avrcp.MediaElementItem.MediaType = (
      avrcp.MediaElementItem.MediaType(avrcp.MediaElementItem.MediaType.AUDIO)
  )
  attributes: Mapping[int, str] = dataclasses.field(default_factory=dict)

  @override
  def to_avrcp_item(self) -> avrcp.MediaElementItem:
    return avrcp.MediaElementItem(
        media_element_uid=self.media_element_uid,
        media_type=self.media_type,
        character_set_id=_CHARSET_ID_UTF_8,
        displayable_name=self.displayable_name,
        attribute_value_entry_list=[
            avrcp.AttributeValueEntry(
                attribute_id=avrcp.MediaAttributeId(attribute_id),
                character_set_id=_CHARSET_ID_UTF_8,
                attribute_value=attribute_value,
            )
            for attribute_id, attribute_value in self.attributes.items()
        ],
    )

  @classmethod
  def from_avrcp_item(cls, item: avrcp.BrowseableItem) -> MediaElement:
    if not isinstance(item, avrcp.MediaElementItem):
      raise ValueError(f"Expect avrcp.MediaElementItem, got {type(item)}")
    return cls(
        media_element_uid=item.media_element_uid,
        displayable_name=item.displayable_name,
        media_type=item.media_type,
        attributes={
            entry.attribute_id.value: entry.attribute_value
            for entry in item.attribute_value_entry_list
        },
    )


@dataclasses.dataclass
class Folder(BrowsableItem):
  """Abstract Folder class without some protocol details."""

  folder_uid: int
  is_playable: bool
  displayable_name: str
  folder_type: avrcp.FolderItem.FolderType = avrcp.FolderItem.FolderType(
      avrcp.FolderItem.FolderType.MIXED
  )
  children: Sequence[MediaElement | Folder] = ()

  @override
  def to_avrcp_item(self) -> avrcp.FolderItem:
    return avrcp.FolderItem(
        folder_uid=self.folder_uid,
        folder_type=self.folder_type,
        is_playable=cast(
            avrcp.FolderItem.Playable,
            (
                avrcp.FolderItem.Playable.PLAYABLE
                if self.is_playable
                else avrcp.FolderItem.Playable.NOT_PLAYABLE
            ),
        ),
        character_set_id=_CHARSET_ID_UTF_8,
        displayable_name=self.displayable_name,
    )

  @classmethod
  def from_avrcp_item(cls, item: avrcp.BrowseableItem) -> Folder:
    if not isinstance(item, avrcp.FolderItem):
      raise ValueError(f"Expect avrcp.FolderItem, got {type(item)}")
    return cls(
        folder_uid=item.folder_uid,
        is_playable=item.is_playable == avrcp.FolderItem.Playable.PLAYABLE,
        displayable_name=item.displayable_name,
        folder_type=item.folder_type,
    )


@dataclasses.dataclass
class Player(BrowsableItem):
  """Abstract Player class without some protocol details."""

  player_id: int
  feature_bitmask: avrcp.MediaPlayerItem.Features
  displayable_name: str
  root_folder: Folder | None = None
  major_player_type: avrcp.MediaPlayerItem.MajorPlayerType = (
      avrcp.MediaPlayerItem.MajorPlayerType(
          avrcp.MediaPlayerItem.MajorPlayerType.AUDIO
      )
  )
  player_sub_type: avrcp.MediaPlayerItem.PlayerSubType = (
      avrcp.MediaPlayerItem.PlayerSubType(0)
  )
  play_status: avrcp.PlayStatus = avrcp.PlayStatus(avrcp.PlayStatus.STOPPED)
  now_playing_items: list[MediaElement] = dataclasses.field(
      default_factory=list
  )

  @override
  def to_avrcp_item(self) -> avrcp.MediaPlayerItem:
    return avrcp.MediaPlayerItem(
        player_id=self.player_id,
        major_player_type=self.major_player_type,
        player_sub_type=self.player_sub_type,
        play_status=self.play_status,
        feature_bitmask=self.feature_bitmask,
        character_set_id=_CHARSET_ID_UTF_8,
        displayable_name=self.displayable_name,
    )

  @classmethod
  def from_avrcp_item(cls, item: avrcp.BrowseableItem) -> Player:
    if not isinstance(item, avrcp.MediaPlayerItem):
      raise ValueError(f"Expect avrcp.MediaPlayerItem, got {type(item)}")
    return cls(
        player_id=item.player_id,
        feature_bitmask=item.feature_bitmask,
        displayable_name=item.displayable_name,
        major_player_type=item.major_player_type,
        player_sub_type=item.player_sub_type,
        play_status=item.play_status,
    )


class BrowsingController:
  """AVRCP Browsing Channel controller implementation."""

  pending_commands: dict[
      int, asyncio.Future[avrcp.Response]
  ]  # Pending commands, by label

  def __init__(self, l2cap_channel: l2cap.ClassicChannel):

    # Create an initial pool of free commands
    self.pending_commands = {}
    self.avctp_protocol = avctp.Protocol(l2cap_channel)

    self.avctp_protocol.register_response_handler(
        avrcp.AVRCP_PID, self._on_avctp_response
    )

  @classmethod
  async def connect(
      cls,
      connection: bumble_device.Connection,
  ) -> BrowsingController:
    return cls(
        await connection.create_l2cap_channel(
            l2cap.ClassicChannelSpec(
                psm=avctp.AVCTP_BROWSING_PSM,
                mode=l2cap.TransmissionMode.ENHANCED_RETRANSMISSION,
                fcs_enabled=True,
            )
        )
    )

  async def _allocate_transaction_label(self) -> int | None:
    return next(
        (
            i
            for i in range(_AVCTP_MAX_TRANSACTION_LABEL)
            if i not in self.pending_commands
        ),
        None,
    )

  async def send_command(self, command: avrcp.Command) -> avrcp.Response:
    """Sends an AVRCP command to the device.

    Args:
      command: The AVRCP command to send.

    Returns:
      The response from the device.
    """

    # Wait for a free command slot.
    if (transaction_label := await self._allocate_transaction_label()) is None:
      raise core.OutOfResourcesError("No free command slots available")

    logger.debug(">>> AVRCP Browsing command PDU: %s", pprint.pformat(command))
    payload = bytes(command)
    response_future = self.pending_commands[transaction_label] = (
        asyncio.get_running_loop().create_future()
    )
    self.avctp_protocol.send_command(
        transaction_label,
        avrcp.AVRCP_PID,
        struct.pack(">BH", command.pdu_id, len(payload)) + payload,
    )

    # Wait for the response.
    return await response_future

  def send_response(
      self, transaction_label: int, response: avrcp.Response
  ) -> None:
    logger.debug(
        ">>> AVRCP Browsing response PDU: %s", pprint.pformat(response)
    )
    pdu = bytes(response)
    self.avctp_protocol.send_response(
        transaction_label,
        avrcp.AVRCP_PID,
        struct.pack(">BH", response.pdu_id, len(pdu)) + pdu,
    )

  def _on_avctp_response(
      self, transaction_label: int, payload: bytes | None
  ) -> None:
    """Handles an AVRCP response from the device."""
    if not payload:
      raise core.InvalidPacketError(
          "Browsing Channel should not receive empty payload"
      )
    pending_command = self.pending_commands[transaction_label]
    pdu_id = avrcp.PduId(payload[0])
    pdu = payload[3:]
    response: avrcp.Response
    if (
        pdu_id
        in (
            avrcp.PduId.SET_BROWSED_PLAYER,
            avrcp.PduId.GET_FOLDER_ITEMS,
            avrcp.PduId.CHANGE_PATH,
            avrcp.PduId.GET_ITEM_ATTRIBUTES,
            avrcp.PduId.GET_TOTAL_NUMBER_OF_ITEMS,
            avrcp.PduId.SEARCH,
        )
        and len(pdu) <= 1
    ):
      # On error, remaining parameters are not present.
      response = avrcp.RejectedResponse.from_bytes(pdu, pdu_id)
    else:
      response = avrcp.Response.from_bytes(pdu, pdu_id)
    logger.debug("<<< AVRCP response PDU: %s", pprint.pformat(response))
    pending_command.set_result(response)

  async def change_path(
      self,
      direction: avrcp.ChangePathCommand.Direction,
      folder_uid: int = 0,
  ) -> int:
    """Changes the path of the browsing channel.

    Args:
      direction: The direction to change the path.
      folder_uid: The folder uid to change the path. Ignored if direction is UP.

    Returns:
      The number of items in the folder.

    Raises:
      core.InvalidPacketError: If the response is not a ChangePathResponse.
      Error: If the response is a RejectedResponse.
    """

    response = await self.send_command(
        avrcp.ChangePathCommand(
            uid_counter=0,
            direction=direction,
            folder_uid=folder_uid,
        )
    )
    if isinstance(response, avrcp.RejectedResponse):
      raise Error(response.status_code)
    if not isinstance(response, avrcp.ChangePathResponse):
      raise core.InvalidPacketError(f"Invalid response type: {type(response)}")
    return response.number_of_items

  async def get_total_number_of_items(self, scope: avrcp.Scope) -> int:
    """Gets the total number of items of the browsing channel."""
    response = await self.send_command(
        avrcp.GetTotalNumberOfItemsCommand(scope=scope)
    )
    if isinstance(response, avrcp.RejectedResponse):
      raise Error(response.status_code)
    if not isinstance(response, avrcp.GetTotalNumberOfItemsResponse):
      raise core.InvalidPacketError(f"Invalid response type: {type(response)}")
    return response.number_of_items

  async def get_folder_items(
      self,
      scope: avrcp.Scope,
      start_item: int = 0,
      end_item: int = 0,
      attributes: Sequence[avrcp.MediaAttributeId] = (),
  ) -> list[BrowsableItem]:
    """Gets the folder items of the browsing channel.

    Args:
      scope: The scope of the folder items.
      start_item: The start item of the folder items.
      end_item: The end item of the folder items.
      attributes: The attributes of the folder items.

    Returns:
      The folder items of the browsing channel.

    Raises:
      core.InvalidArgumentError: If the start_item is less than 0 or the
        end_item is less than the start_item.
      core.InvalidPacketError: If the response is not a GetFolderItemsResponse.
      Error: If the response is a RejectedResponse.
    """

    if start_item < 0 or start_item > end_item:
      raise core.InvalidArgumentError(
          f"Invalid start_item: {start_item} or end_item: {end_item}"
      )
    if end_item == 0:
      end_item = await self.get_total_number_of_items(scope)
    response = await self.send_command(
        avrcp.GetFolderItemsCommand(
            scope=scope,
            start_item=start_item,
            end_item=end_item,
            attributes=attributes,
        )
    )
    if isinstance(response, avrcp.RejectedResponse):
      raise Error(response.status_code)
    if not isinstance(response, avrcp.GetFolderItemsResponse):
      raise core.InvalidPacketError(f"Invalid response type: {type(response)}")
    return [BrowsableItem.from_avrcp_item(item) for item in response.items]

  async def set_browsed_player(
      self, player_id: int
  ) -> avrcp.SetBrowsedPlayerResponse:
    """Sets the browsed player of the browsing channel.

    Args:
      player_id: The player id of the browsed player.

    Returns:
      The response of the set browsed player command.

    Raises:
      core.InvalidPacketError: If the response is not a
      SetBrowsedPlayerResponse.
      Error: If the response is a RejectedResponse.
    """
    response = await self.send_command(
        avrcp.SetBrowsedPlayerCommand(player_id=player_id)
    )
    if isinstance(response, avrcp.RejectedResponse):
      raise Error(response.status_code)
    if not isinstance(response, avrcp.SetBrowsedPlayerResponse):
      raise core.InvalidPacketError(f"Invalid response type: {type(response)}")
    return response

  async def get_item_attributes(
      self,
      uid: int,
      scope: avrcp.Scope,
      attributes: Sequence[avrcp.MediaAttributeId] = (),
  ) -> dict[avrcp.MediaAttributeId, str]:
    """Sets the browsed player of the browsing channel.

    Args:
      uid: The uid of the item to get the attributes for.
      scope: The scope of the item to get the attributes for.
      attributes: The attributes of the item to get the attributes for.

    Returns:
      A dictionary of the item attributes.

    Raises:
      core.InvalidPacketError: If the response is not a
      GetItemAttributesResponse.
      Error: If the response is a RejectedResponse.
    """
    response = await self.send_command(
        avrcp.GetItemAttributesCommand(
            scope=scope, uid=uid, attributes=attributes, uid_counter=0
        )
    )
    if isinstance(response, avrcp.RejectedResponse):
      raise Error(response.status_code)
    if not isinstance(response, avrcp.GetItemAttributesResponse):
      raise core.InvalidPacketError(f"Invalid response type: {type(response)}")
    return {
        entry.attribute_id: entry.attribute_value
        for entry in response.attribute_value_entry_list
    }


class BrowsingTarget:
  """AVRCP Browsing Channel target implementation."""

  browsed_player: Player | None = None

  def __init__(
      self,
      l2cap_channel: l2cap.ClassicChannel,
      players: Sequence[Player],
  ):
    self.players = players
    self.browsed_player = None
    self.current_path: list[Folder] = []
    self.avctp_protocol = avctp.Protocol(l2cap_channel)

    self.avctp_protocol.register_command_handler(
        avrcp.AVRCP_PID, self._on_avctp_command
    )

  @classmethod
  def listen(
      cls,
      device: bumble_device.Device,
      players: Sequence[Player],
  ) -> asyncio.Queue[BrowsingTarget]:
    """Creates a browsing target and starts listening on the device.

    Args:
      device: The device to listen on.
      players: The players to serve.

    Returns:
      A queue of browsing targets.
    """

    queue = asyncio.Queue[BrowsingTarget]()

    def on_channel(channel: l2cap.ClassicChannel) -> None:
      target = cls(channel, players)
      channel.once(channel.EVENT_OPEN, lambda: queue.put_nowait(target))

    device.create_l2cap_server(
        l2cap.ClassicChannelSpec(
            psm=avctp.AVCTP_BROWSING_PSM,
            mode=l2cap.TransmissionMode.ENHANCED_RETRANSMISSION,
            fcs_enabled=True,
        ),
        handler=on_channel,
    )
    return queue

  @property
  def current_folder(self) -> Folder | None:
    if not self.browsed_player:
      return None
    if not self.current_path:
      return self.browsed_player.root_folder
    return self.current_path[-1]

  def send_response(
      self, transaction_label: int, response: avrcp.Response
  ) -> None:
    logger.debug(
        ">>> AVRCP Browsing response PDU: %s", pprint.pformat(response)
    )
    pdu = bytes(response)
    self.avctp_protocol.send_response(
        transaction_label,
        avrcp.AVRCP_PID,
        struct.pack(">BH", response.pdu_id, len(pdu)) + pdu,
    )

  def _on_avctp_command(self, transaction_label: int, payload: bytes) -> None:
    """Handles an AVRCP command from the device."""
    pdu_id = avrcp.PduId(payload[0])
    command = avrcp.Command.from_bytes(pdu_id, payload[3:])
    logger.debug("<<< AVRCP command PDU: %s", pprint.pformat(command))
    match command:
      case avrcp.GetFolderItemsCommand():
        self._on_get_folder_items_command(transaction_label, command)
      case avrcp.GetTotalNumberOfItemsCommand():
        self._on_get_total_number_of_items_command(transaction_label, command)
      case avrcp.SetBrowsedPlayerCommand():
        self._on_set_browsed_player_command(transaction_label, command)
      case avrcp.ChangePathCommand():
        self._on_change_path_command(transaction_label, command)
      case avrcp.GetItemAttributesCommand():
        self._on_get_item_attributes_command(transaction_label, command)
      case _:
        self.send_response(
            transaction_label,
            avrcp.RejectedResponse(
                pdu_id, cast(avrcp.StatusCode, avrcp.StatusCode.INTERNAL_ERROR)
            ),
        )

  def _on_get_item_attributes_command(
      self, transaction_label: int, command: avrcp.GetItemAttributesCommand
  ) -> None:
    """Handles a GetItemAttributesCommand."""
    if not self.browsed_player:
      self.send_response(
          transaction_label,
          avrcp.RejectedResponse(
              command.pdu_id,
              avrcp.StatusCode.PLAYER_NOT_BROWSABLE,
          ),
      )
      return

    search_scope: Sequence[MediaElement | Folder]
    match command.scope:
      case avrcp.Scope.NOW_PLAYING:
        search_scope = self.browsed_player.now_playing_items
      case avrcp.Scope.MEDIA_PLAYER_VIRTUAL_FILESYSTEM:
        search_scope = (
            [self.browsed_player.root_folder]
            if self.browsed_player.root_folder
            else []
        )
      case _:
        self.send_response(
            transaction_label,
            avrcp.RejectedResponse(
                command.pdu_id,
                avrcp.StatusCode.INVALID_SCOPE,
            ),
        )
        return

    def find_item_by_uid(
        items: Sequence[MediaElement | Folder], uid: int
    ) -> MediaElement | Folder | None:
      for item in items:
        match item:
          case MediaElement(media_element_uid=media_element_uid):
            if media_element_uid == uid:
              return item
          case Folder(folder_uid=folder_uid):
            if folder_uid == uid:
              return item
            if result := find_item_by_uid(item.children, uid):
              return result
      return None

    item = find_item_by_uid(search_scope, command.uid)
    if not item:
      self.send_response(
          transaction_label,
          avrcp.RejectedResponse(
              command.pdu_id,
              avrcp.StatusCode.DOES_NOT_EXIST,
          ),
      )
      return

    if isinstance(item, Folder):
      self.send_response(
          transaction_label,
          avrcp.RejectedResponse(
              command.pdu_id,
              avrcp.StatusCode.PARAMETER_CONTENT_ERROR,
          ),
      )
      return

    if not command.attributes:
      attributes = item.to_avrcp_item().attribute_value_entry_list
    else:
      attributes = [
          avrcp.AttributeValueEntry(
              attribute_id=attribute_id,
              character_set_id=_CHARSET_ID_UTF_8,
              attribute_value=attribute_value,
          )
          for attribute_id in command.attributes
          if (attribute_value := item.attributes.get(attribute_id))
      ]

    response = avrcp.GetItemAttributesResponse(
        status=avrcp.StatusCode.OPERATION_COMPLETED,
        attribute_value_entry_list=attributes,
    )
    self.send_response(transaction_label, response)

  def _on_get_folder_items_command(
      self, transaction_label: int, command: avrcp.GetFolderItemsCommand
  ) -> None:
    """Handles a GetFolderItemsCommand."""
    response: avrcp.Response
    items: list[avrcp.BrowseableItem]
    match command.scope:
      case avrcp.Scope.MEDIA_PLAYER_LIST:
        items = [player.to_avrcp_item() for player in self.players]
        response = avrcp.GetFolderItemsResponse(
            status=avrcp.StatusCode.OPERATION_COMPLETED,
            uid_counter=0,
            items=items[command.start_item : command.end_item + 1],
        )
      case avrcp.Scope.MEDIA_PLAYER_VIRTUAL_FILESYSTEM:
        if not self.browsed_player:
          response = avrcp.RejectedResponse(
              command.pdu_id,
              avrcp.StatusCode.NO_AVAILABLE_PLAYERS,
          )
        else:
          folder = self.current_folder
          if folder is None:
            response = avrcp.RejectedResponse(
                command.pdu_id,
                avrcp.StatusCode.NO_AVAILABLE_PLAYERS,
            )
          else:
            items = [child.to_avrcp_item() for child in folder.children]
            response = avrcp.GetFolderItemsResponse(
                status=avrcp.StatusCode.OPERATION_COMPLETED,
                uid_counter=0,
                items=items[command.start_item : command.end_item + 1],
            )
      case avrcp.Scope.NOW_PLAYING:
        if not self.browsed_player:
          response = avrcp.RejectedResponse(
              command.pdu_id,
              avrcp.StatusCode.NO_AVAILABLE_PLAYERS,
          )
        else:
          response = avrcp.GetFolderItemsResponse(
              status=avrcp.StatusCode.OPERATION_COMPLETED,
              uid_counter=0,
              items=[
                  item.to_avrcp_item()
                  for item in self.browsed_player.now_playing_items
              ],
          )
      case _:
        response = avrcp.RejectedResponse(
            command.pdu_id,
            avrcp.StatusCode.INVALID_COMMAND,
        )
    self.send_response(transaction_label, response)

  def _on_get_total_number_of_items_command(
      self, transaction_label: int, command: avrcp.GetTotalNumberOfItemsCommand
  ) -> None:
    """Handles a GetTotalNumberOfItemsCommand."""
    match command.scope:
      case avrcp.Scope.MEDIA_PLAYER_LIST:
        count = len(self.players)
      case avrcp.Scope.MEDIA_PLAYER_VIRTUAL_FILESYSTEM:
        if not self.browsed_player:
          count = 0
        else:
          folder = self.current_folder
          count = len(folder.children) if folder else 0
      case avrcp.Scope.NOW_PLAYING:
        count = (
            len(self.browsed_player.now_playing_items)
            if self.browsed_player
            else 0
        )
      case _:
        count = 0

    self.send_response(
        transaction_label,
        avrcp.GetTotalNumberOfItemsResponse(
            status=avrcp.StatusCode.OPERATION_COMPLETED,
            uid_counter=0,
            number_of_items=count,
        ),
    )

  def _on_set_browsed_player_command(
      self, transaction_label: int, command: avrcp.SetBrowsedPlayerCommand
  ) -> None:
    """Handles a SetBrowsedPlayerCommand."""
    player = next(
        (p for p in self.players if p.player_id == command.player_id), None
    )
    if not player:
      self.send_response(
          transaction_label,
          avrcp.RejectedResponse(
              command.pdu_id,
              cast(avrcp.StatusCode, avrcp.StatusCode.INVALID_PLAYER_ID),
          ),
      )
      return

    self.browsed_player = player
    self.current_path = []

    num_items = len(player.root_folder.children) if player.root_folder else 0

    self.send_response(
        transaction_label,
        avrcp.SetBrowsedPlayerResponse(
            status=cast(avrcp.StatusCode, avrcp.StatusCode.OPERATION_COMPLETED),
            uid_counter=0,
            numbers_of_items=num_items,
            character_set_id=_CHARSET_ID_UTF_8,
            folder_names=[],
        ),
    )

  def _on_change_path_command(
      self, transaction_label: int, command: avrcp.ChangePathCommand
  ) -> None:
    """Handles a ChangePathCommand."""
    if not self.browsed_player:
      self.send_response(
          transaction_label,
          avrcp.RejectedResponse(
              command.pdu_id,
              cast(avrcp.StatusCode, avrcp.StatusCode.PLAYER_NOT_BROWSABLE),
          ),
      )
      return

    if command.direction == avrcp.ChangePathCommand.Direction.UP:
      if not self.current_path:
        self.send_response(
            transaction_label,
            avrcp.RejectedResponse(
                command.pdu_id,
                cast(avrcp.StatusCode, avrcp.StatusCode.INVALID_DIRECTION),
            ),
        )
        return
      self.current_path.pop()
    else:  # DOWN
      folder = self.current_folder
      if folder is None:
        self.send_response(
            transaction_label,
            avrcp.RejectedResponse(
                command.pdu_id,
                cast(avrcp.StatusCode, avrcp.StatusCode.INVALID_DIRECTION),
            ),
        )
        return
      target = next(
          (
              child
              for child in folder.children
              if isinstance(child, Folder)
              and child.folder_uid == command.folder_uid
          ),
          None,
      )
      if not target:
        self.send_response(
            transaction_label,
            avrcp.RejectedResponse(
                command.pdu_id,
                cast(avrcp.StatusCode, avrcp.StatusCode.DOES_NOT_EXIST),
            ),
        )
        return
      self.current_path.append(target)

    num_items = len(self.current_folder.children) if self.current_folder else 0
    self.send_response(
        transaction_label,
        avrcp.ChangePathResponse(
            status=cast(avrcp.StatusCode, avrcp.StatusCode.OPERATION_COMPLETED),
            number_of_items=num_items,
        ),
    )


def setup_server(
    device: bumble_device.Device,
    avrcp_controller_handle: int,
    avrcp_target_handle: int,
    delegate: avrcp.Delegate | None = None,
    avrcp_controller_features: int = 0x01,
    avrcp_target_features: int = 0x23,
) -> avrcp.Protocol:
  """Sets up the AVRCP server on the device.

  Args:
    device: The device to set up the AVRCP server on.
    avrcp_controller_handle: The handle of the AVRCP service record.
    avrcp_target_handle: The handle of the AVRCP target service record.
    delegate: The delegate to handle AVRCP events.
    avrcp_controller_features: The features of the AVRCP controller.
    avrcp_target_features: The features of the AVRCP target.

  Returns:
    The AVRCP protocol.
  """
  avrcp_protocol = avrcp.Protocol(delegate)
  avrcp_protocol.listen(device)
  device.sdp_service_records.update({
      avrcp_controller_handle: (
          avrcp.ControllerServiceSdpRecord(
              avrcp_controller_handle,
              supported_features=avrcp_controller_features,
          ).to_service_attributes()
      ),
      avrcp_target_handle: (
          TargetServiceSdpRecord(
              avrcp_target_handle, supported_features=avrcp_target_features
          ).to_service_attributes()
      ),
  })
  return avrcp_protocol


@dataclasses.dataclass
class TargetServiceSdpRecord(avrcp.TargetServiceSdpRecord):
  """Target SDP record, extending Bumble's TargetServiceSdpRecord with BIP support."""

  bip_goep_rfcomm_channel: int | None = None
  bip_goep_l2cap_psm: int | None = None
  browsing_psm: int | None = None
  bip_version: tuple[int, int] | None = None

  def __post_init__(self) -> None:
    if (
        self.bip_goep_l2cap_psm is not None
        or self.bip_goep_rfcomm_channel is not None
    ):
      self.supported_features |= avrcp.TargetFeatures.SUPPORTS_COVER_ART
      if self.bip_version is None:
        self.bip_version = (1, 0)

  @override
  def to_service_attributes(self) -> list[sdp.ServiceAttribute]:
    attributes = super().to_service_attributes()

    # Find if 0x000D is already in attributes
    additional_proto_idx = next(
        (
            i
            for i, attr in enumerate(attributes)
            if attr.id
            == sdp.SDP_ADDITIONAL_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID
        ),
        None,
    )

    additional_protocols = []

    # Browsing PSM
    actual_browsing_psm = self.browsing_psm
    if actual_browsing_psm is None and (
        self.supported_features & avrcp.TargetFeatures.SUPPORTS_BROWSING
    ):
      actual_browsing_psm = avctp.AVCTP_BROWSING_PSM

    if actual_browsing_psm is not None:
      avctp_version_int = self.avctp_version[0] << 8 | self.avctp_version[1]
      additional_protocols.append(
          sdp.DataElement.sequence([
              sdp.DataElement.sequence([
                  sdp.DataElement.uuid(core.BT_L2CAP_PROTOCOL_ID),
                  sdp.DataElement.unsigned_integer_16(actual_browsing_psm),
              ]),
              sdp.DataElement.sequence([
                  sdp.DataElement.uuid(core.BT_AVCTP_PROTOCOL_ID),
                  sdp.DataElement.unsigned_integer_16(avctp_version_int),
              ]),
          ])
      )

    # BIP L2CAP PSM
    if self.bip_goep_l2cap_psm is not None:
      additional_protocols.append(
          sdp.DataElement.sequence([
              sdp.DataElement.sequence([
                  sdp.DataElement.uuid(core.BT_L2CAP_PROTOCOL_ID),
                  sdp.DataElement.unsigned_integer_16(self.bip_goep_l2cap_psm),
              ]),
              sdp.DataElement.sequence([
                  sdp.DataElement.uuid(core.BT_OBEX_PROTOCOL_ID),
              ]),
          ])
      )

    # BIP RFCOMM Channel
    if self.bip_goep_rfcomm_channel is not None:
      additional_protocols.append(
          sdp.DataElement.sequence([
              sdp.DataElement.sequence([
                  sdp.DataElement.uuid(core.BT_L2CAP_PROTOCOL_ID),
              ]),
              sdp.DataElement.sequence([
                  sdp.DataElement.uuid(core.BT_RFCOMM_PROTOCOL_ID),
                  sdp.DataElement.unsigned_integer_8(
                      self.bip_goep_rfcomm_channel
                  ),
              ]),
              sdp.DataElement.sequence([
                  sdp.DataElement.uuid(core.BT_OBEX_PROTOCOL_ID),
              ]),
          ])
      )

    if additional_protocols:
      new_attr = sdp.ServiceAttribute(
          sdp.SDP_ADDITIONAL_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
          sdp.DataElement.sequence(additional_protocols),
      )
      if additional_proto_idx is not None:
        attributes[additional_proto_idx] = new_attr
      else:
        attributes.append(new_attr)
    elif additional_proto_idx is not None:
      attributes.pop(additional_proto_idx)

    return attributes

  @classmethod
  @override
  async def find(  # type: ignore[override]
      cls,
      connection: bumble_device.Connection,
  ) -> list[TargetServiceSdpRecord]:
    """Finds SDP records for AVRCP Target."""
    async with sdp.Client(connection) as sdp_client:
      result = await sdp_client.search_attributes(
          [core.BT_AV_REMOTE_CONTROL_TARGET_SERVICE],
          [
              sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
              sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
              sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
              sdp.SDP_SUPPORTED_FEATURES_ATTRIBUTE_ID,
              0x000D,  # Additional Protocol Descriptor Lists
          ],
      )

    if not result:
      return []

    records: list[TargetServiceSdpRecord] = []
    for attribute_lists in result:
      record = cls(0)
      additional_protocol_descriptor_lists = None
      for attribute in attribute_lists:
        match attribute.id:
          case sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID:
            record.service_record_handle = attribute.value.value
          case sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID:
            protocols = attribute.value.value
            for proto in protocols:
              proto_uuid = proto.value[0].value
              if proto_uuid == core.BT_RFCOMM_PROTOCOL_ID:
                record.bip_goep_rfcomm_channel = proto.value[1].value
              elif proto_uuid == core.BT_AVCTP_PROTOCOL_ID:
                record.avctp_version = (
                    proto.value[1].value >> 8,
                    proto.value[1].value & 0xFF,
                )
          case sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID:
            record.avrcp_version = (
                attribute.value.value[0].value[1].value >> 8,
                attribute.value.value[0].value[1].value & 0xFF,
            )
          case sdp.SDP_SUPPORTED_FEATURES_ATTRIBUTE_ID:
            record.supported_features = avrcp.TargetFeatures(
                attribute.value.value
            )
          case 0x000D:  # Additional Protocol Descriptor Lists
            additional_protocol_descriptor_lists = attribute.value

      if additional_protocol_descriptor_lists is not None:
        for proto_list in additional_protocol_descriptor_lists.value:
          has_l2cap = False
          has_rfcomm = False
          has_obex = False
          has_avctp = False
          temp_psm = None
          temp_channel = None
          for proto_desc in proto_list.value:
            proto_uuid = proto_desc.value[0].value
            if proto_uuid == core.BT_L2CAP_PROTOCOL_ID:
              has_l2cap = True
              if len(proto_desc.value) > 1:
                temp_psm = proto_desc.value[1].value
            elif proto_uuid == core.BT_RFCOMM_PROTOCOL_ID:
              has_rfcomm = True
              if len(proto_desc.value) > 1:
                temp_channel = proto_desc.value[1].value
            elif proto_uuid == core.BT_OBEX_PROTOCOL_ID:
              has_obex = True
            elif proto_uuid == core.BT_AVCTP_PROTOCOL_ID:
              has_avctp = True

          if has_l2cap and has_obex and temp_psm is not None:
            record.bip_goep_l2cap_psm = temp_psm
            if record.bip_version is None:
              record.bip_version = (1, 0)
          elif (
              has_l2cap and has_rfcomm and has_obex and temp_channel is not None
          ):
            record.bip_goep_rfcomm_channel = temp_channel
            if record.bip_version is None:
              record.bip_version = (1, 0)
          elif has_l2cap and has_avctp and temp_psm is not None:
            record.browsing_psm = temp_psm

      records.append(record)

    return records


class CoverArtClient(obex.ClientSession):
  """AVRCP Cover Art BIP Client."""

  # AVRCP 1.6 Section 5.14.2.1.
  COVER_ART_UUID = core.UUID("7163dd54-4a7e-11e2-b47c-0050c2490048")

  async def connect(self) -> None:
    """Connect to the Cover Art OBEX Server."""
    response = await self.send_request(
        obex.ConnectRequest(
            obex_version_number=obex.Version.V_1_0,
            flags=0,
            maximum_obex_packet_length=self.bearer.mtu,
            headers=obex.Headers(target=self.COVER_ART_UUID.uuid_bytes[::-1]),
            final=True,
        )
    )
    assert isinstance(response, obex.ConnectResponse)
    self.connection_id = response.headers.connection_id
    if response.response_code != obex.ResponseCode.SUCCESS:
      raise obex.Error(
          response.response_code, "Failed to connect to Cover Art server"
      )

  async def _get_multi_packet(self, request: obex.Request) -> bytes:
    body = b""
    response = await self.send_request(request)
    while response.response_code == obex.ResponseCode.CONTINUE:
      chunk = response.headers.body or response.headers.end_of_body or b""
      body += chunk

      # Send next empty GET request
      next_request = obex.Request(
          opcode=obex.Opcode.GET,
          final=True,
          headers=obex.Headers(connection_id=self.connection_id),
      )
      response = await self.send_request(next_request)

    if response.response_code != obex.ResponseCode.SUCCESS:
      raise obex.Error(response.response_code, "GET failed")

    chunk = response.headers.body or response.headers.end_of_body or b""
    body += chunk
    return body

  async def get_image_properties(self, image_handle: str) -> bytes:
    """Retrieve the properties of an image."""
    request = obex.Request(
        opcode=obex.Opcode.GET,
        final=True,
        headers=obex.Headers(
            connection_id=self.connection_id,
            img_handle=image_handle,
            type=b"x-bt/img-properties\x00",
        ),
    )
    return await self._get_multi_packet(request)

  async def get_image(self, image_handle: str) -> bytes:
    """Retrieve the image itself."""
    request = obex.Request(
        opcode=obex.Opcode.GET,
        final=True,
        headers=obex.Headers(
            connection_id=self.connection_id,
            img_handle=image_handle,
            type=b"x-bt/img-img\x00",
        ),
    )
    return await self._get_multi_packet(request)


class CoverArtServerConnection(obex.ServerSession):
  """AVRCP Cover Art Server connection."""

  def __init__(
      self,
      bearer: obex.Bearer,
      images: dict[str, bytes],
  ) -> None:
    self.images = images
    self.connections = set[int]()
    self.peer_max_packet_length = 255  # Default minimum
    self._remaining_data = b""
    super().__init__(bearer)

  @override
  def _on_connect(self, request: obex.ConnectRequest) -> None:
    # Validate Target UUID
    if request.headers.target != CoverArtClient.COVER_ART_UUID.uuid_bytes[::-1]:
      logger.warning("onConnect - UUID didn't match. Not Acceptable")
      self.send_response(
          obex.Response(response_code=obex.ResponseCode.NOT_ACCEPTABLE)
      )
      return

    connection_id = max(self.connections) + 1 if self.connections else 1
    self.connections.add(connection_id)
    self.peer_max_packet_length = request.maximum_obex_packet_length

    response = obex.ConnectResponse(
        response_code=obex.ResponseCode.SUCCESS,
        obex_version_number=request.obex_version_number,
        flags=request.flags,
        maximum_obex_packet_length=self.bearer.mtu,
        headers=obex.Headers(
            connection_id=connection_id,
            who=CoverArtClient.COVER_ART_UUID.uuid_bytes[::-1],
        ),
    )
    self.send_response(response)

  @override
  def _on_disconnect(self, request: obex.Request) -> None:
    if (
        request.headers.connection_id is None
        or request.headers.connection_id not in self.connections
    ):
      self.send_response(
          obex.Response(response_code=obex.ResponseCode.NOT_FOUND)
      )
      return
    self.connections.remove(request.headers.connection_id)
    self.send_response(obex.Response(response_code=obex.ResponseCode.SUCCESS))

  def _send_next_chunk(self) -> None:
    # Calculate max chunk size, leaving some safety margin
    max_chunk_size = self.peer_max_packet_length - 10

    if len(self._remaining_data) <= max_chunk_size:
      # This is the last chunk
      chunk = self._remaining_data
      self._remaining_data = b""
      self.send_response(
          obex.Response(
              response_code=obex.ResponseCode.SUCCESS,
              headers=obex.Headers(end_of_body=chunk),
          )
      )
    else:
      # More chunks to follow
      chunk = self._remaining_data[:max_chunk_size]
      self._remaining_data = self._remaining_data[max_chunk_size:]
      self.send_response(
          obex.Response(
              response_code=obex.ResponseCode.CONTINUE,
              headers=obex.Headers(body=chunk),
          )
      )

  @override
  def _on_get(self, request: obex.Request) -> None:
    if (
        request.headers.connection_id is None
        or request.headers.connection_id not in self.connections
    ):
      self.send_response(
          obex.Response(response_code=obex.ResponseCode.NOT_FOUND)
      )
      return

    # Check request type
    req_type = request.headers.type
    img_handle = request.headers.img_handle

    # If it is a subsequent GET request (no type, no handle)
    if req_type is None and img_handle is None:
      if self._remaining_data:
        self._send_next_chunk()
      else:
        self.send_response(
            obex.Response(response_code=obex.ResponseCode.BAD_REQUEST)
        )
      return

    # Remove null terminator if present
    if req_type and req_type.endswith(b"\x00"):
      req_type = req_type[:-1]

    if img_handle is None:
      self.send_response(
          obex.Response(response_code=obex.ResponseCode.BAD_REQUEST)
      )
      return

    if img_handle not in self.images:
      self.send_response(
          obex.Response(response_code=obex.ResponseCode.NOT_FOUND)
      )
      return

    if req_type == b"x-bt/img-properties":
      # Return XML properties
      properties_xml = (
          '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n'
          f'<image-properties version="1.0" handle="{img_handle}">\n'
          '  <native encoding="JPEG" pixel="200*200" />\n'
          "</image-properties>\n"
      ).encode("utf-8")
      self._remaining_data = properties_xml
      self._send_next_chunk()

    elif req_type == b"x-bt/img-img":
      image_bytes = self.images[img_handle]
      self._remaining_data = image_bytes
      self._send_next_chunk()

    else:
      self.send_response(
          obex.Response(response_code=obex.ResponseCode.BAD_REQUEST)
      )


class CoverArtServer:
  """AVRCP Cover Art BIP Server."""

  def __init__(
      self,
      device: bumble_device.Device,
      images: dict[str, bytes] | None = None,
  ) -> None:
    self.device = device
    self.images = images or {}

    self.l2cap_server = self.device.create_l2cap_server(
        spec=l2cap.ClassicChannelSpec(
            mode=l2cap.TransmissionMode.ENHANCED_RETRANSMISSION,
            fcs_enabled=True,
        ),
        handler=self._on_connection,
    )
    self._pending_connections = asyncio.Queue[CoverArtServerConnection]()

  def _on_connection(self, bearer: obex.Bearer) -> None:
    connection = CoverArtServerConnection(bearer, self.images)
    self._pending_connections.put_nowait(connection)

  async def wait_connection(self) -> CoverArtServerConnection:
    """Waits for a connection."""
    return await self._pending_connections.get()
