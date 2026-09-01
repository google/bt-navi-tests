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

import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothHidDevice
import android.bluetooth.BluetoothHidDeviceAppSdpSettings
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import androidx.test.platform.app.InstrumentationRegistry
import com.google.android.mobly.snippet.Snippet
import com.google.android.mobly.snippet.rpc.AsyncRpc
import com.google.android.mobly.snippet.rpc.Rpc

class BluetoothHidDeviceSnippet : Snippet {
  private val instrumentation = InstrumentationRegistry.getInstrumentation()
  private val context = instrumentation.targetContext
  private val bluetoothAdapter = context.getSystemService(BluetoothManager::class.java).adapter
  private val proxy =
    Utils.getProfileProxy(context, BluetoothProfile.HID_DEVICE) as BluetoothHidDevice

  init {
    instrumentation.uiAutomation.adoptShellPermissionIdentity()
  }

  /** Registers an HID Device App. */
  @AsyncRpc(description = "Registers an HID Device App.")
  fun registerHidDeviceApp(callbackId: String, sdpSettings: BluetoothHidDeviceAppSdpSettings) {
    val callback =
      object : BluetoothHidDevice.Callback() {
        override fun onAppStatusChanged(device: BluetoothDevice?, registered: Boolean) {
          Utils.postSnippetEvent(callbackId, SnippetConstants.HID_DEVICE_APP_STATUS_CHANGED) {
            putString(SnippetConstants.FIELD_DEVICE, device?.address)
            putBoolean(SnippetConstants.FIELD_STATE, registered)
          }
        }

        override fun onConnectionStateChanged(device: BluetoothDevice, state: Int) {
          Utils.postSnippetEvent(callbackId, SnippetConstants.PROFILE_CONNECTION_STATE_CHANGE) {
            putString(SnippetConstants.FIELD_DEVICE, device.address)
            putInt(SnippetConstants.FIELD_STATE, state)
          }
        }

        override fun onGetReport(device: BluetoothDevice, type: Byte, id: Byte, bufferSize: Int) {
          Utils.postSnippetEvent(callbackId, SnippetConstants.HID_DEVICE_ON_GET_REPORT) {
            putString(SnippetConstants.FIELD_DEVICE, device.address)
            putByte(SnippetConstants.FIELD_TYPE, type)
            putByte(SnippetConstants.FIELD_ID, id)
            putInt(SnippetConstants.FIELD_BUFFER_SIZE, bufferSize)
          }
        }
      }
    if (!proxy.registerApp(sdpSettings, null, null, context.mainExecutor, callback)) {
      throw RuntimeException("Failed to register HID Device App.")
    }
  }

  /** Unregisters the HID Device App. */
  @Rpc(description = "Unregisters the HID Device App.")
  fun unregisterHidDeviceApp() {
    if (!proxy.unregisterApp()) {
      throw RuntimeException("Failed to unregister HID Device App.")
    }
  }

  /** Gets the connected devices. */
  @Rpc(description = "Gets the connected devices.")
  fun getHidDeviceConnectedDevices(): List<String> {
    return proxy.connectedDevices.map { it.address }
  }

  /**
   * Get a list of devices that match any of the given connection states.
   *
   * @param states An array of states (e.g., [STATE_CONNECTED, STATE_CONNECTING]).
   * @return A list of device addresses.
   */
  @Rpc(description = "Get a list of devices that match any of the given connection states")
  fun getHidDeviceDevicesMatchingConnectionStates(states: IntArray): List<String> {
    return proxy.getDevicesMatchingConnectionStates(states).map { it.address }
  }

  /**
   * Get the current connection state of the profile.
   *
   * @param address The remote device address.
   * @return The connection state (STATE_CONNECTED, STATE_DISCONNECTED, etc).
   */
  @Rpc(description = "Get the current connection state of the profile")
  fun getHidDeviceConnectionState(address: String): Int {
    val device = bluetoothAdapter.getRemoteDevice(address)
    return proxy.getConnectionState(device)
  }

  /** Sends a report from the HID Device. */
  @Rpc(description = "Sends a report from the HID Device.")
  fun hidDeviceSendReport(address: String, id: Int, data: ByteArray): Boolean {
    return proxy.sendReport(bluetoothAdapter.getRemoteDevice(address), id, data)
  }

  /** Replies a report from the HID Device. */
  @Rpc(description = "Replies a report from the HID Device.")
  fun hidDeviceReplyReport(address: String, type: Int, id: Int, data: ByteArray): Boolean {
    val device = bluetoothAdapter.getRemoteDevice(address)
    return proxy.replyReport(device, type.toByte(), id.toByte(), data)
  }

  /** Reports an error from the HID Device. */
  @Rpc(description = "Reports an error from the HID Device.")
  fun hidDeviceReportError(address: String, error: Int): Boolean {
    return proxy.reportError(bluetoothAdapter.getRemoteDevice(address), error.toByte())
  }

  /**
   * Gets the application name of the current HidDeviceService user.
   *
   * @return The current user name, or empty string if cannot get the name.
   */
  @Rpc(description = "Gets the application name of the current HidDeviceService user")
  fun getHidDeviceUserAppName(): String {
    return BluetoothHidDevice::class.java.getMethod("getUserAppName").invoke(proxy) as String
  }

  /** Connects to the HID host Device. */
  @Rpc(description = "Connects to the HID Device.")
  fun hidDeviceConnect(address: String): Boolean {
    return proxy.connect(bluetoothAdapter.getRemoteDevice(address))
  }

  /** Disconnects from the HID host Device. */
  @Rpc(description = "Disconnects from the HID Device.")
  fun hidDeviceDisconnect(address: String): Boolean {
    return proxy.disconnect(bluetoothAdapter.getRemoteDevice(address))
  }
}
