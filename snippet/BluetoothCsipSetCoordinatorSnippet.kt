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

import android.annotation.SuppressLint
import android.bluetooth.BluetoothCsipSetCoordinator
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.os.ParcelUuid
import androidx.test.platform.app.InstrumentationRegistry
import com.google.android.mobly.snippet.Snippet
import com.google.android.mobly.snippet.rpc.AsyncRpc
import com.google.android.mobly.snippet.rpc.Rpc

/** Snippet class to adapt [BluetoothCsipSetCoordinator] APIs. */
@SuppressLint("MissingPermission")
class BluetoothCsipSetCoordinatorSnippet : Snippet {

  private val instrumentation = InstrumentationRegistry.getInstrumentation()
  private val context = instrumentation.targetContext
  private val bluetoothManager = context.getSystemService(BluetoothManager::class.java)
  private val bluetoothAdapter = bluetoothManager.adapter
  private val proxy =
    Utils.getProfileProxy(context, BluetoothProfile.CSIP_SET_COORDINATOR)
      as BluetoothCsipSetCoordinator

  // callbackId -> BroadcastReceiver for intent-based events
  private val broadcastReceivers = mutableMapOf<String, BroadcastReceiver>()

  init {
    instrumentation.uiAutomation.adoptShellPermissionIdentity()
  }

  private fun getBluetoothDevice(address: String): BluetoothDevice =
    bluetoothAdapter.getRemoteDevice(address)

  /**
   * Register CSIP callbacks for connection state changes, device available, and set member
   * available broadcast events.
   */
  @AsyncRpc(description = "Register CSIP Set Coordinator broadcast callbacks.")
  fun registerCsipSetCoordinatorCallback(callbackId: String) {
    val receiver =
      object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
          val device =
            intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice::class.java)
          when (intent.action) {
            BluetoothCsipSetCoordinator.ACTION_CSIS_CONNECTION_STATE_CHANGED -> {
              Utils.postSnippetEvent(callbackId, SnippetConstants.PROFILE_CONNECTION_STATE_CHANGE) {
                putString(SnippetConstants.FIELD_DEVICE, device?.address)
                putInt(
                  SnippetConstants.FIELD_STATE,
                  intent.getIntExtra(BluetoothProfile.EXTRA_STATE, BluetoothDevice.ERROR),
                )
                putInt(
                  SnippetConstants.FIELD_PREVIOUS_STATE,
                  intent.getIntExtra(BluetoothProfile.EXTRA_PREVIOUS_STATE, BluetoothDevice.ERROR),
                )
              }
            }
            BluetoothCsipSetCoordinator.ACTION_CSIS_SET_MEMBER_AVAILABLE -> {
              Utils.postSnippetEvent(callbackId, SnippetConstants.CSIS_SET_MEMBER_AVAILABLE) {
                putString(SnippetConstants.FIELD_DEVICE, device?.address)
                putInt(
                  SnippetConstants.FIELD_GROUP_ID,
                  intent.getIntExtra(
                    BluetoothCsipSetCoordinator.EXTRA_CSIS_GROUP_ID,
                    BluetoothCsipSetCoordinator.GROUP_ID_INVALID,
                  ),
                )
              }
            }
          }
        }
      }
    val filter =
      IntentFilter().apply {
        addAction(BluetoothCsipSetCoordinator.ACTION_CSIS_CONNECTION_STATE_CHANGED)
        addAction(BluetoothCsipSetCoordinator.ACTION_CSIS_SET_MEMBER_AVAILABLE)
      }
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
      context.registerReceiver(receiver, filter, Context.RECEIVER_EXPORTED)
    } else {
      context.registerReceiver(receiver, filter)
    }
    broadcastReceivers[callbackId] = receiver
  }

  /** Unregister CSIP broadcast callbacks registered with [callbackId]. */
  @Rpc(description = "Unregister CSIP Set Coordinator broadcast callbacks.")
  fun unregisterCsipSetCoordinatorCallback(callbackId: String) {
    broadcastReceivers.remove(callbackId)?.let { context.unregisterReceiver(it) }
  }

  /**
   * Get the group UUID map for a device. Returns a map of group ID (Int) to group type UUID string.
   */
  @Rpc(description = "Get the CSIP group UUID map for a device.")
  fun getCsipGroupUuidMapByDevice(address: String): Map<Int, String> =
    proxy.getGroupUuidMapByDevice(getBluetoothDevice(address)).mapValues { it.value.toString() }

  /** Get all CSIP group IDs for the given UUID string. Pass null to get all group IDs. */
  @Rpc(description = "Get all CSIP group IDs for a given UUID string (null for all).")
  fun getCsipAllGroupIds(uuidStr: String?): List<Int> {
    val parcelUuid = uuidStr?.let { ParcelUuid.fromString(it) }
    return proxy.getAllGroupIds(parcelUuid)
  }

  /** Set the CSIP connection policy for a device. */
  @Rpc(description = "Set the CSIP connection policy for a device.")
  fun setCsipConnectionPolicy(address: String, policy: Int): Boolean =
    proxy.setConnectionPolicy(getBluetoothDevice(address), policy)

  override fun shutdown() {
    broadcastReceivers.values.forEach { context.unregisterReceiver(it) }
    broadcastReceivers.clear()
    bluetoothAdapter.closeProfileProxy(BluetoothProfile.CSIP_SET_COORDINATOR, proxy)
  }

  private companion object {
    const val TAG = "BluetoothCsipSetCoordinatorSnippet"
  }
}
