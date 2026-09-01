/*
 * Copyright 2026 The Android Open Source Project
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.google.wireless.android.pixel.bluetooth.snippet

import android.content.BroadcastReceiver
import android.content.ContentResolver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.AudioDeviceAttributes
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioManager.OnCommunicationDeviceChangedListener
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.Spatializer
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.util.Log
import androidx.media3.common.AudioAttributes
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.common.TrackSelectionParameters.AudioOffloadPreferences
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.session.MediaSession
import androidx.test.platform.app.InstrumentationRegistry
import com.google.android.mobly.snippet.Snippet
import com.google.android.mobly.snippet.rpc.AsyncRpc
import com.google.android.mobly.snippet.rpc.Rpc
import com.google.android.mobly.snippet.rpc.RpcOptional
import com.google.android.mobly.snippet.rpc.RunOnUiThread
import com.google.wireless.android.pixel.bluetooth.snippet.JsonObjectConverter.Companion.toJson
import com.google.wireless.android.pixel.bluetooth.snippet.Utils.postSnippetEvent
import java.io.FileOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.UUID
import java.util.concurrent.Executors
import kotlin.time.Duration.Companion.seconds
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Job
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout

class AudioSnippet : Snippet {
  private data class AudioCallbacks(
    val audioDeviceCallback: AudioDeviceCallback,
    val communicationDeviceChangedListener: OnCommunicationDeviceChangedListener,
    val broadcastReceiver: BroadcastReceiver,
  )

  private val instrumentation = InstrumentationRegistry.getInstrumentation()
  private val context = instrumentation.targetContext
  private val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
  internal val players =
    mutableMapOf<String?, ExoPlayer>(null to ExoPlayer.Builder(context).build())
  private val callbacks = mutableMapOf<String, AudioCallbacks>()
  private val playerListeners = mutableMapOf<String, Pair<Player, Player.Listener>>()
  private val mainHandler = Handler(context.mainLooper)
  private val dispatcher: CoroutineDispatcher =
    Executors.newSingleThreadExecutor().asCoroutineDispatcher()
  private val coroutineScope = CoroutineScope(dispatcher + Job())
  internal val recorders = mutableMapOf<String, Pair<AudioRecord, Deferred<List<Byte>>>>()
  private val headTrackerListeners =
    mutableMapOf<String, Spatializer.OnHeadTrackerAvailableListener>()

  init {
    instrumentation.uiAutomation.adoptShellPermissionIdentity()
    context.mainExecutor.execute {
      val player = ExoPlayer.Builder(context).build()
      players[null] = player
      MediaSession.Builder(context, player).build()
      // Add a default media.
      val fileUri =
        Uri.Builder()
          .scheme(ContentResolver.SCHEME_ANDROID_RESOURCE)
          .path(R.raw.sine1000hz.toString())
          .build()
      player.setMediaItem(MediaItem.fromUri(fileUri))
      player.prepare()
    }
  }

  /** Registers an Audio snippet callback with [callbackId]. */
  @AsyncRpc(description = "Registers an Audio snippet callback")
  fun audioRegisterCallback(callbackId: String) {
    val audioDeviceCallback =
      object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(addedDevices: Array<AudioDeviceInfo>) {
          for (addedDevice in addedDevices) {
            postSnippetEvent(callbackId, SnippetConstants.AUDIO_DEVICE_ADDED) {
              putString(SnippetConstants.FIELD_DEVICE, addedDevice.address)
              putInt(SnippetConstants.FIELD_TYPE, addedDevice.type)
              putBoolean(SnippetConstants.FIELD_IS_SOURCE, addedDevice.isSource)
              putBoolean(SnippetConstants.FIELD_IS_SINK, addedDevice.isSink)
            }
          }
        }

        override fun onAudioDevicesRemoved(removedDevices: Array<AudioDeviceInfo>) {
          for (removedDevice in removedDevices) {
            postSnippetEvent(callbackId, SnippetConstants.AUDIO_DEVICE_REMOVED) {
              putString(SnippetConstants.FIELD_DEVICE, removedDevice.address)
              putInt(SnippetConstants.FIELD_TYPE, removedDevice.type)
              putBoolean(SnippetConstants.FIELD_IS_SOURCE, removedDevice.isSource)
              putBoolean(SnippetConstants.FIELD_IS_SINK, removedDevice.isSink)
            }
          }
        }
      }
    audioManager.registerAudioDeviceCallback(audioDeviceCallback, mainHandler)

    val onCommunicationDeviceChangedListener =
      object : OnCommunicationDeviceChangedListener {
        override fun onCommunicationDeviceChanged(device: AudioDeviceInfo?) {
          if (device == null) {
            return
          }
          postSnippetEvent(callbackId, SnippetConstants.AUDIO_COMMUNICATION_DEVICE_CHANGED) {
            putString(SnippetConstants.FIELD_DEVICE, device.address)
            putInt(SnippetConstants.FIELD_TYPE, device.type)
          }
        }
      }
    audioManager.addOnCommunicationDeviceChangedListener(
      context.mainExecutor,
      onCommunicationDeviceChangedListener,
    )

    val broadcastReceiver =
      object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
          when (intent.action) {
            AudioManager.ACTION_VOLUME_CHANGED -> {
              postSnippetEvent(callbackId, SnippetConstants.VOLUME_CHANGED) {
                putInt(
                  SnippetConstants.FIELD_TYPE,
                  intent.getIntExtra(AudioManager.EXTRA_VOLUME_STREAM_TYPE, AudioManager.ERROR),
                )
                putInt(
                  SnippetConstants.FIELD_VALUE,
                  intent.getIntExtra(AudioManager.EXTRA_VOLUME_STREAM_VALUE, AudioManager.ERROR),
                )
              }
            }

            AudioManager.ACTION_MICROPHONE_MUTE_CHANGED -> {
              postSnippetEvent(callbackId, SnippetConstants.MUTE_CHANGED) {
                putBoolean(SnippetConstants.FIELD_STATE, audioManager.isMicrophoneMute)
              }
            }
          }
        }
      }

    context.registerReceiver(
      broadcastReceiver,
      IntentFilter().apply {
        addAction(AudioManager.ACTION_VOLUME_CHANGED)
        addAction(AudioManager.ACTION_MICROPHONE_MUTE_CHANGED)
      },
    )

    callbacks[callbackId] =
      AudioCallbacks(audioDeviceCallback, onCommunicationDeviceChangedListener, broadcastReceiver)
  }

  /** Unregisters an Audio snippet callback with [callbackId]. */
  @Rpc(description = "Registers an Audio snippet callback")
  fun audioUnregisterCallback(callbackId: String) {
    callbacks.remove(callbackId)?.also {
      (audioDeviceCallback, onCommunicationDeviceChangedListener, broadcastReceiver) ->
      audioManager.unregisterAudioDeviceCallback(audioDeviceCallback)
      audioManager.removeOnCommunicationDeviceChangedListener(onCommunicationDeviceChangedListener)
      context.unregisterReceiver(broadcastReceiver)
    }
  }

  /** Registers a player snippet callback with [callbackId]. */
  @AsyncRpc(description = "Registers a player snippet callback")
  @RunOnUiThread
  fun registerPlayerListener(callbackId: String, @RpcOptional playerId: String? = null) {
    val player =
      players[playerId] ?: throw IllegalArgumentException("$playerId is not a valid player")
    val listener =
      object : Player.Listener {
        override fun onIsPlayingChanged(isPlaying: Boolean) {
          Log.d(TAG, "isPlayingChanged: $isPlaying")
          postSnippetEvent(callbackId, SnippetConstants.PLAYER_IS_PLAYING_CHANGED) {
            putBoolean(SnippetConstants.FIELD_STATE, isPlaying)
          }
        }

        override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
          Log.d(TAG, "onMediaItemTransition: $mediaItem, $reason")
          postSnippetEvent(callbackId, SnippetConstants.PLAYER_MEDIA_ITEM_TRANSITION) {
            putParcelable(SnippetConstants.MEDIA_ITEM, mediaItem?.toJson())
          }
        }

        override fun onPositionDiscontinuity(
          oldPosition: Player.PositionInfo,
          newPosition: Player.PositionInfo,
          reason: Int,
        ) {
          Log.d(TAG, "onPositionDiscontinuity: $oldPosition -> $newPosition, $reason")
          postSnippetEvent(callbackId, SnippetConstants.POSITION_DISCONTINUITY) {
            putLong(SnippetConstants.OLD_POSITION, oldPosition.positionMs)
            putLong(SnippetConstants.NEW_POSITION, newPosition.positionMs)
            putInt(SnippetConstants.FIELD_REASON, reason)
          }
        }

        override fun onShuffleModeEnabledChanged(shuffleModeEnabled: Boolean) {
          Log.d(TAG, "onShuffleModeEnabledChanged: $shuffleModeEnabled")
          postSnippetEvent(callbackId, SnippetConstants.PLAYER_SHUFFLE_MODE_ENABLED_CHANGED) {
            putBoolean(SnippetConstants.FIELD_STATE, shuffleModeEnabled)
          }
        }

        override fun onRepeatModeChanged(repeatMode: Int) {
          Log.d(TAG, "onRepeatModeChanged: $repeatMode")
          postSnippetEvent(callbackId, SnippetConstants.PLAYER_REPEAT_MODE_CHANGED) {
            putInt(SnippetConstants.MODE, repeatMode)
          }
        }
      }
    player.addListener(listener)
    playerListeners[callbackId] = Pair(player, listener)
  }

  /** Unregisters a player snippet callback with [callbackId]. */
  @Rpc(description = "Unregisters a player snippet callback")
  @RunOnUiThread
  fun unregisterPlayerListener(callbackId: String) {
    playerListeners.remove(callbackId)?.let { (player, listener) ->
      player.removeListener(listener)
    }
  }

  /** Set offload of audio playback. */
  @Rpc(description = "Set offload of audio playback")
  @RunOnUiThread
  fun setAudioPlaybackOffload(enabled: Boolean, @RpcOptional playerId: String? = null) {
    val player =
      players[playerId] ?: throw IllegalArgumentException("$playerId is not a valid player")
    val audioOffloadPreferences =
      if (enabled) {
        AudioOffloadPreferences.Builder()
          .setAudioOffloadMode(AudioOffloadPreferences.AUDIO_OFFLOAD_MODE_ENABLED)
          .setIsGaplessSupportRequired(true)
          .build()
      } else {
        AudioOffloadPreferences.Builder()
          .setAudioOffloadMode(AudioOffloadPreferences.AUDIO_OFFLOAD_MODE_DISABLED)
          .build()
      }
    player.trackSelectionParameters =
      player.trackSelectionParameters
        .buildUpon()
        .setAudioOffloadPreferences(audioOffloadPreferences)
        .build()
  }

  /** Set handle audio becoming noisy. */
  @Rpc(description = "Set handle audio becoming noisy")
  @RunOnUiThread
  fun setHandleAudioBecomingNoisy(enabled: Boolean, @RpcOptional playerId: String? = null) {
    val player =
      players[playerId] ?: throw IllegalArgumentException("$playerId is not a valid player")
    player.setHandleAudioBecomingNoisy(enabled)
  }

  /** Sets audio attribute of player to [attributes] and [handleAudioFocus]. */
  @Rpc(description = "Set Audio Attribute")
  @RunOnUiThread
  fun setAudioAttributes(
    attributes: AudioAttributes?,
    handleAudioFocus: Boolean,
    @RpcOptional playerId: String? = null,
  ) {
    val player =
      players[playerId] ?: throw IllegalArgumentException("$playerId is not a valid player")
    player.setAudioAttributes(attributes ?: player.audioAttributes, handleAudioFocus)
  }

  /** Plays 1000Hz sine wave. */
  @Rpc(description = "Play 1000Hz sine wave")
  @RunOnUiThread
  fun audioPlaySine(@RpcOptional playerId: String? = null) {
    val player =
      players[playerId] ?: throw IllegalArgumentException("$playerId is not a valid player")
    val fileUri =
      Uri.Builder()
        .scheme(ContentResolver.SCHEME_ANDROID_RESOURCE)
        .path(R.raw.sine1000hz.toString())
        .build()
    player.setMediaItem(MediaItem.fromUri(fileUri))
    player.prepare()
    player.play()
  }

  /** Plays 1000Hz sine wave with surround sound. */
  @Rpc(description = "Play 1000Hz sine wave with surround sound (5.1 channels).")
  @RunOnUiThread
  fun playSineSurrounded(@RpcOptional playerId: String? = null) {
    val player =
      players[playerId] ?: throw IllegalArgumentException("$playerId is not a valid player")
    val fileUri =
      Uri.Builder()
        .scheme(ContentResolver.SCHEME_ANDROID_RESOURCE)
        .path(R.raw.sine1000hz_5_1_ch.toString())
        .build()
    player.setMediaItem(MediaItem.fromUri(fileUri))
    player.prepare()
    player.play()
  }

  /** Plays audio file with [fileUri] . */
  @Rpc(description = "Play audio from a given file path")
  @RunOnUiThread
  fun audioPlayFile(fileUri: String, @RpcOptional playerId: String? = null) {
    val player =
      players[playerId] ?: throw IllegalArgumentException("$playerId is not a valid player")
    player.setMediaItem(MediaItem.fromUri(fileUri))
    player.prepare()
    player.play()
  }

  /** Sets player repeat mode to [repeatMode]. */
  @Rpc(description = "Set repeat mode")
  @RunOnUiThread
  fun audioSetRepeat(@Player.RepeatMode repeatMode: Int, @RpcOptional playerId: String? = null) {
    val player =
      players[playerId] ?: throw IllegalArgumentException("$playerId is not a valid player")
    player.repeatMode = repeatMode
  }

  /** Sets shuffle mode to [shuffleModeEnabled]. */
  @Rpc(description = "Set shuffle mode")
  @RunOnUiThread
  fun setShuffleMode(shuffleModeEnabled: Boolean, @RpcOptional playerId: String? = null) {
    val player =
      players[playerId] ?: throw IllegalArgumentException("$playerId is not a valid player")
    player.shuffleModeEnabled = shuffleModeEnabled
  }

  /** Resumes playing audio. */
  @Rpc(description = "Resume playing audio")
  @RunOnUiThread
  fun audioResume(@RpcOptional playerId: String? = null) {
    val player =
      players[playerId] ?: throw IllegalArgumentException("$playerId is not a valid player")
    player.play()
  }

  /** Pauses playing audio. */
  @Rpc(description = "Pause playing audio")
  @RunOnUiThread
  fun audioPause(@RpcOptional playerId: String? = null) {
    val player =
      players[playerId] ?: throw IllegalArgumentException("$playerId is not a valid player")
    player.pause()
  }

  /** Stops playing audio. */
  @Rpc(description = "stop playing audio")
  @RunOnUiThread
  fun audioStop(@RpcOptional playerId: String? = null) {
    val player =
      players[playerId] ?: throw IllegalArgumentException("$playerId is not a valid player")
    player.stop()
  }

  /** Set a media item to the player. */
  @Rpc(description = "Set a media item")
  @RunOnUiThread
  fun playMediaItem(mediaItem: MediaItem, @RpcOptional playerId: String? = null) {
    val player =
      players[playerId] ?: throw IllegalArgumentException("$playerId is not a valid player")
    player.setMediaItem(mediaItem)
    player.prepare()
    player.play()
  }

  /** Add a media item to the player. */
  @Rpc(description = "Add a media item")
  @RunOnUiThread
  fun addMediaItem(mediaItem: MediaItem, @RpcOptional playerId: String? = null) {
    val player =
      players[playerId] ?: throw IllegalArgumentException("$playerId is not a valid player")
    player.addMediaItem(mediaItem)
  }

  /** Add a new player. */
  @Rpc(description = "Add a new player")
  @RunOnUiThread
  fun addPlayer(): String {
    val playerId = UUID.randomUUID().toString()
    players[playerId] = ExoPlayer.Builder(context).build()
    return playerId
  }

  /** Add a new player. */
  @Rpc(description = "Add a new player")
  @RunOnUiThread
  fun removePlayer(playerId: String) {
    players.remove(playerId)?.let { it.release() }
  }

  /**
   * Starts a recorder streaming to [outputPath].
   *
   * If [preferredDeviceAddress] or [preferredDeviceType] is provided, the recorder will use the
   * preferred device to record audio.
   */
  @Rpc(description = "Start recording")
  fun startRecording(
    outputPath: String,
    @RpcOptional source: Int? = null,
    @RpcOptional preferredDeviceAddress: String? = null,
    @RpcOptional preferredDeviceType: Int? = null,
  ) {
    if (outputPath in recorders) {
      throw IllegalArgumentException("$outputPath is already recording")
    }
    val bufferSize =
      AudioRecord.getMinBufferSize(
        48000,
        AudioFormat.CHANNEL_IN_STEREO,
        AudioFormat.ENCODING_PCM_16BIT,
      ) * 2
    val recorder =
      AudioRecord(
        source ?: MediaRecorder.AudioSource.DEFAULT,
        48000,
        AudioFormat.CHANNEL_IN_STEREO,
        AudioFormat.ENCODING_PCM_16BIT,
        bufferSize,
      )
    if (preferredDeviceAddress != null || preferredDeviceType != null) {
      val preferredDevice =
        audioManager.getDevices(AudioManager.GET_DEVICES_INPUTS).first {
          (preferredDeviceType == null || it.type == preferredDeviceType) &&
            (preferredDeviceAddress == null || it.address == preferredDeviceAddress)
        }
      if (!recorder.setPreferredDevice(preferredDevice)) {
        throw IllegalArgumentException("Unable to set preferred device $preferredDevice")
      }
    }
    recorder.startRecording()
    val deferred = coroutineScope.async {
      val outputBuffer = mutableListOf<Byte>()
      val buffer = ByteArray(bufferSize)
      while (recorder.read(buffer, 0, buffer.size) > 0) {
        outputBuffer.addAll(buffer.asList())
      }
      Log.d(TAG, "Recording ${outputPath} stopped")
      outputBuffer
    }
    recorders[outputPath] = Pair(recorder, deferred)
  }

  /** Stops a recorder streaming to [outputPath]. */
  @Rpc(description = "Stop recording")
  fun stopRecording(outputPath: String) {
    recorders.remove(outputPath)?.let { (recorder, deferred) ->
      recorder.stop()
      recorder.release()
      val outputBuffer = runBlocking { withTimeout(5.seconds) { deferred.await() } }
      FileOutputStream(outputPath).use {
        // Write the wave header.
        it.write(
          ByteBuffer.allocate(44)
            .apply {
              order(ByteOrder.LITTLE_ENDIAN)
              put(WAVE_HEADER_RIFF)
              putInt(outputBuffer.size + 36) // File size
              put(WAVE_HEADER_WAVE)
              put(WAVE_HEADER_FMT)
              putInt(16) // Size of previous headers
              putShort(WAVE_HEADER_TYPE_PCM) // Format of data
              putShort(2) // Stereo
              putInt(48000) // Sample rate
              putInt(48000 * 2 * 16 / 8) // Bytes per second
              putShort((2 * 16 / 8).toShort()) // Frame size
              putShort(16) // Bits per sample
              put(WAVE_HEADER_DATA) // data
              putInt(outputBuffer.size) // Data size
            }
            .array()
        )
        it.write(outputBuffer.toByteArray())
      }
    } ?: throw IllegalArgumentException("$outputPath is not recording")
  }

  /** Gets the currently selected communication device. */
  @Rpc(description = "Get the currently selected communication device")
  fun getCommunicationDevice(): Bundle? {
    return audioManager.getCommunicationDevice()?.let { device ->
      Bundle().apply {
        putString(SnippetConstants.FIELD_DEVICE, device.address)
        putInt(SnippetConstants.FIELD_TYPE, device.type)
        putBoolean(SnippetConstants.FIELD_IS_SOURCE, device.isSource)
        putBoolean(SnippetConstants.FIELD_IS_SINK, device.isSink)
      }
    }
  }

  /** Sets the communication device to [type] and [address]. */
  @Rpc(description = "Set the communication device")
  fun setCommunicationDevice(
    @RpcOptional type: Int? = null,
    @RpcOptional address: String? = null,
  ): Boolean {
    if (type == null && address == null) {
      audioManager.clearCommunicationDevice()
      return true
    }
    val device =
      audioManager.availableCommunicationDevices.firstOrNull {
        (type == null || it.type == type) && (address == null || it.address == address)
      } ?: throw IllegalArgumentException("Device not found")
    return audioManager.setCommunicationDevice(device)
  }

  /** Checks if spatializer is available. */
  @Rpc(description = "Check if spatializer is available")
  fun isSpatializerAvailable(): Boolean {
    return audioManager.spatializer.isAvailable
  }

  @Rpc(description = "Check if spatializer is enabled")
  fun setSpatializerEnabled(enabled: Boolean) {
    audioManager.spatializer.isEnabled = enabled
  }

  @Rpc(description = "Add compatible Spatializer devices")
  fun addCompatibleSpatizlierDevice(role: Int, type: Int, address: String) {
    audioManager.spatializer.addCompatibleAudioDevice(AudioDeviceAttributes(role, type, address))
  }

  @Rpc(description = "Remove compatible Spatializer device")
  fun removeCompatibleSpatizlierDevice(role: Int, type: Int, address: String) {
    audioManager.spatializer.removeCompatibleAudioDevice(AudioDeviceAttributes(role, type, address))
  }

  @Rpc(description = "Clear all compatible Spatializer devices")
  fun clearCompatibleSpatizlierDevices() {
    for (device in audioManager.spatializer.compatibleAudioDevices) {
      audioManager.spatializer.removeCompatibleAudioDevice(device)
    }
  }

  @Rpc(description = "Get compatible Spatializer devices")
  fun setHeadtrackerEnabled(role: Int, type: Int, address: String, enabled: Boolean) {
    audioManager.spatializer.setHeadTrackerEnabled(
      enabled,
      AudioDeviceAttributes(role, type, address),
    )
  }

  @Rpc(description = "Get compatible Spatializer devices")
  fun getHeadtrackerEnabled(role: Int, type: Int, address: String): Boolean =
    audioManager.spatializer.isHeadTrackerEnabled(AudioDeviceAttributes(role, type, address))

  @AsyncRpc(description = "Registers a HeadTracker availability callback")
  fun audioRegisterHeadTrackerCallback(callbackId: String) {
    val listener = Spatializer.OnHeadTrackerAvailableListener { _, available ->
      Log.d(TAG, "onHeadTrackerAvailableChanged: $available")
      postSnippetEvent(callbackId, SnippetConstants.HEAD_TRACKER_AVAILABLE_CHANGED) {
        putBoolean(SnippetConstants.FIELD_STATE, available)
      }
    }
    audioManager.spatializer.addOnHeadTrackerAvailableListener(context.mainExecutor, listener)
    headTrackerListeners[callbackId] = listener
  }

  @Rpc(description = "Unregisters a HeadTracker availability callback")
  fun audioUnregisterHeadTrackerCallback(callbackId: String) {
    headTrackerListeners.remove(callbackId)?.also { listener ->
      audioManager.spatializer.removeOnHeadTrackerAvailableListener(listener)
    }
  }

  @Rpc(description = "Get compatible Spatializer devices")
  fun getCompatibleSpatizlierDevices(): List<String> =
    audioManager.spatializer.compatibleAudioDevices.map { it.address }

  /** Sets volume of [streamType] to [volume]. */
  @Rpc(description = "Set volume")
  fun setVolume(streamType: Int, volume: Int) {
    audioManager.setStreamVolume(streamType, volume, AudioManager.FLAG_SHOW_UI)
  }

  /** Gets the current volume of [streamType]. */
  @Rpc(description = "Get current volume")
  fun getVolume(streamType: Int): Int = audioManager.getStreamVolume(streamType)

  /** Gets the max volume of [streamType]. */
  @Rpc(description = "Get the max volume")
  fun getMaxVolume(streamType: Int): Int = audioManager.getStreamMaxVolume(streamType)

  /** Gets the min volume of [streamType]. */
  @Rpc(description = "Get the min volume")
  fun getMinVolume(streamType: Int): Int = audioManager.getStreamMinVolume(streamType)

  /** Sets the vendor parameters to audio. */
  @Rpc(description = "Set the vendor parameters")
  fun setAudioParameters(parameters: String) {
    audioManager.setParameters(parameters)
  }

  /** Sets the audio mode to audio. */
  @Rpc(description = "Set the audio mode")
  fun setAudioMode(mode: Int) {
    audioManager.mode = mode
  }

  /** Gets the audio mode. */
  @Rpc(description = "Get the audio mode") fun getAudioMode(): Int = audioManager.mode

  /** Gets the mute state */
  @Rpc(description = "Get the microphone mute state")
  fun getMicrophoneMuteState(): Boolean = audioManager.isMicrophoneMute

  /** Sets the mute state */
  @Rpc(description = "Set the microphone mute state")
  fun setMicrophoneMuteState(isMute: Boolean) {
    audioManager.isMicrophoneMute = isMute
  }

  /** Gets the supported audio device types for [direction]. */
  @Rpc(description = "Get the supported audio device types")
  fun getSupportedAudioDeviceTypes(direction: Int): Set<Int> =
    audioManager.getSupportedDeviceTypes(direction)

  /** Checks if volume is fixed. */
  @Rpc(description = "Check if volume is fixed")
  fun isVolumeFixed(): Boolean = audioManager.isVolumeFixed

  private companion object {
    const val TAG = "AudioSnippet"
    val WAVE_HEADER_RIFF = byteArrayOf(0x52, 0x49, 0x46, 0x46)
    val WAVE_HEADER_WAVE = byteArrayOf(0x57, 0x41, 0x56, 0x45)
    val WAVE_HEADER_FMT = byteArrayOf(0x66, 0x6d, 0x74, 0x20)
    val WAVE_HEADER_DATA = byteArrayOf(0x64, 0x61, 0x74, 0x61)
    const val WAVE_HEADER_TYPE_PCM: Short = 1
  }
}
