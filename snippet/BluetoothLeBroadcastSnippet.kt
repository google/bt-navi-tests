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

import android.bluetooth.BluetoothLeAudioContentMetadata
import android.bluetooth.BluetoothLeBroadcast
import android.bluetooth.BluetoothLeBroadcastMetadata
import android.bluetooth.BluetoothLeBroadcastSettings
import android.bluetooth.BluetoothLeBroadcastSubgroupSettings
import android.bluetooth.BluetoothProfile
import android.util.Log
import androidx.test.platform.app.InstrumentationRegistry
import com.google.android.mobly.snippet.Snippet
import com.google.android.mobly.snippet.rpc.Rpc
import com.google.android.mobly.snippet.rpc.RpcOptional
import com.google.wireless.android.pixel.bluetooth.snippet.BluetoothLeBroadcastMetadataExt.toQrCodeString
import com.google.wireless.android.pixel.bluetooth.snippet.Utils.toBluetoothStatusDescription
import com.google.wireless.android.pixel.bluetooth.snippet.Utils.toList
import kotlin.time.Duration.Companion.seconds
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.channels.trySendBlocking
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.timeout
import kotlinx.coroutines.runBlocking
import org.json.JSONObject

class BluetoothLeBroadcastSnippet : Snippet {

  private val instrumentation = InstrumentationRegistry.getInstrumentation()
  private val context = instrumentation.targetContext
  private val broadcastProxy =
    Utils.getProfileProxy(context, BluetoothProfile.LE_AUDIO_BROADCAST) as BluetoothLeBroadcast

  init {
    instrumentation.uiAutomation.adoptShellPermissionIdentity()
  }

  /** Base callback class to allow overriding only some of the callback methods. */
  private open class BaseCallback() : BluetoothLeBroadcast.Callback {
    override fun onBroadcastStarted(reason: Int, broadcastId: Int) {}

    override fun onBroadcastStartFailed(reason: Int) {}

    override fun onBroadcastStopped(reason: Int, broadcastId: Int) {}

    override fun onBroadcastStopFailed(reason: Int) {}

    override fun onPlaybackStarted(reason: Int, broadcastId: Int) {}

    override fun onPlaybackStopped(reason: Int, broadcastId: Int) {}

    override fun onBroadcastUpdated(reason: Int, broadcastId: Int) {}

    override fun onBroadcastUpdateFailed(reason: Int, broadcastId: Int) {}

    override fun onBroadcastMetadataChanged(reason: Int, metadata: BluetoothLeBroadcastMetadata) {}
  }

  /** Starts LE broadcast with the given [broadcastCode] and [settings]. */
  @Rpc(description = "Start LE broadcast")
  fun startBroadcast(
    @RpcOptional broadcastCode: ByteArray? = null,
    @RpcOptional settings: JSONObject? = null,
  ): Int {
    val bluetoothLeBroadcastSettings =
      BluetoothLeBroadcastSettings.Builder()
        .setPublicBroadcast(
          settings?.optBoolean(SnippetConstants.LEA_BROADCAST_FIELD_PUBLIC) ?: false
        )
        .setBroadcastName(
          settings?.optString(SnippetConstants.FIELD_NAME)?.takeIf { it.isNotEmpty() }
        )
        .setBroadcastCode(broadcastCode)
        .apply {
          val subgroups =
            settings
              ?.optJSONArray(SnippetConstants.LEA_BROADCAST_FIELD_SUBGROUPS)
              ?.toList<JSONObject>()
              ?: listOf(
                // Default subgroup settings.
                JSONObject(
                  mapOf(
                    SnippetConstants.LEA_BROADCAST_FIELD_LANGUAGE to "eng",
                    SnippetConstants.LEA_BROADCAST_FIELD_PROGRAM to "Snippet",
                  )
                )
              )
          for (subgroup in subgroups) {
            addSubgroupSettings(
              BluetoothLeBroadcastSubgroupSettings.Builder()
                .setPreferredQuality(
                  subgroup.optInt(
                    SnippetConstants.LEA_BROADCAST_FIELD_QUALITY,
                    BluetoothLeBroadcastSubgroupSettings.QUALITY_STANDARD,
                  )
                )
                .setContentMetadata(
                  BluetoothLeAudioContentMetadata.Builder()
                    .setLanguage(
                      subgroup.optString(SnippetConstants.LEA_BROADCAST_FIELD_LANGUAGE, "eng")
                    )
                    .setProgramInfo(subgroup.optString(SnippetConstants.FIELD_NAME, "Snippet"))
                    .build()
                )
                .build()
            )
          }
        }
        .build()

    val flow = callbackFlow {
      val callback =
        object : BaseCallback() {
          override fun onBroadcastStarted(reason: Int, broadcastId: Int) {
            Log.d(TAG, "onBroadcastStarted: reason=$reason, broadcastId=$broadcastId")
            val unused = trySendBlocking(Pair(reason, broadcastId))
          }

          override fun onBroadcastStartFailed(reason: Int) {
            Log.d(TAG, "onBroadcastStartFailed: reason=$reason")
            val unused = trySendBlocking(Pair(reason, -1))
          }
        }
      broadcastProxy.registerCallback(context.mainExecutor, callback)
      broadcastProxy.startBroadcast(bluetoothLeBroadcastSettings)

      awaitClose { broadcastProxy.unregisterCallback(callback) }
    }

    return runBlocking {
      val (reason, broadcastId) = flow.timeout(DEFAULT_OPERATION_TIMEOUT).first()
      if (broadcastId == -1) {
        throw RuntimeException(
          "Failed to start broadcast, reason: ${reason.toBluetoothStatusDescription()}"
        )
      }
      broadcastId
    }
  }

  /** Stops LE broadcast with the given [broadcastId]. */
  @Rpc(description = "Stop LE broadcast")
  fun stopBroadcast(broadcastId: Int) {
    val flow = callbackFlow {
      val callback =
        object : BaseCallback() {
          override fun onBroadcastStopped(reason: Int, broadcastId: Int) {
            Log.d(TAG, "onBroadcastStopped: reason=$reason, broadcastId=$broadcastId")
            val unused = trySendBlocking(Pair(reason, true))
          }

          override fun onBroadcastStopFailed(reason: Int) {
            Log.d(TAG, "onBroadcastStopFailed: reason=$reason")
            val unused = trySendBlocking(Pair(reason, false))
          }
        }
      broadcastProxy.registerCallback(context.mainExecutor, callback)
      broadcastProxy.stopBroadcast(broadcastId)

      awaitClose { broadcastProxy.unregisterCallback(callback) }
    }

    runBlocking {
      val (reason, success) = flow.timeout(DEFAULT_OPERATION_TIMEOUT).first()
      if (!success) {
        throw RuntimeException(
          "Failed to stop broadcast, reason: ${reason.toBluetoothStatusDescription()}"
        )
      }
    }
  }

  /** Gets all LE BIS metadata. */
  @Rpc(description = "Get all LE BIS metadata")
  fun getAllBroadcastMetadata(): List<String> =
    broadcastProxy.allBroadcastMetadata.map { it.toQrCodeString() }

  private companion object {
    const val TAG = "BluetoothLeBroadcastSnippet"
    val DEFAULT_OPERATION_TIMEOUT = 5.seconds
  }
}
