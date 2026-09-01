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
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothGattServer
import android.bluetooth.BluetoothGattServerCallback
import android.bluetooth.BluetoothGattService
import android.bluetooth.BluetoothManager
import android.util.Log
import androidx.test.platform.app.InstrumentationRegistry
import com.google.android.mobly.snippet.Snippet
import com.google.android.mobly.snippet.rpc.AsyncRpc
import com.google.android.mobly.snippet.rpc.Rpc
import com.google.wireless.android.pixel.bluetooth.snippet.Utils.postSnippetEvent

/** Snippet class to adapt [BluetoothGattServer] APIs. */
// Permissions are requested with UiAutomation.
@SuppressLint("MissingPermission")
class BluetoothGattServerSnippet : Snippet {

  class DefaultGattServerCallback(private val callbackId: String) : BluetoothGattServerCallback() {
    // TODO: Cover more callbacks.
    override fun onConnectionStateChange(device: BluetoothDevice, status: Int, newState: Int) {
      Log.i(
        TAG,
        "onConnectionStateChange device=${device.address}, status=$status, state=$newState",
      )
      postSnippetEvent(callbackId, SnippetConstants.GATT_SERVER_CONNECTION_STATE_CHANGE) {
        putString(SnippetConstants.FIELD_DEVICE, device.address)
        putInt(SnippetConstants.FIELD_STATUS, status)
        putInt(SnippetConstants.FIELD_STATE, newState)
      }
    }

    override fun onServiceAdded(status: Int, service: BluetoothGattService) {
      Log.i(TAG, "onServiceAdded, status=$status, uuid=${service.uuid}")
      postSnippetEvent(callbackId, SnippetConstants.GATT_SERVER_SERVICE_ADDED) {
        putString(SnippetConstants.FIELD_UUID, service.uuid.toString())
        putInt(SnippetConstants.FIELD_STATUS, status)
        putInt(SnippetConstants.FIELD_HANDLE, service.instanceId)
      }
    }

    override fun onCharacteristicReadRequest(
      device: BluetoothDevice,
      requestId: Int,
      offset: Int,
      characteristic: BluetoothGattCharacteristic,
    ) {
      Log.i(TAG, "onCharacteristicReadRequest device=${device.address} uuid=${characteristic.uuid}")
      postSnippetEvent(callbackId, SnippetConstants.GATT_SERVER_CHARACTERISTIC_READ_REQUEST) {
        putString(SnippetConstants.FIELD_DEVICE, device.address)
        putString(SnippetConstants.FIELD_UUID, characteristic.uuid.toString())
        putInt(SnippetConstants.GATT_FIELD_REQUEST_ID, requestId)
        putInt(SnippetConstants.GATT_FIELD_OFFSET, offset)
      }
    }

    override fun onCharacteristicWriteRequest(
      device: BluetoothDevice,
      requestId: Int,
      characteristic: BluetoothGattCharacteristic,
      preparedWrite: Boolean,
      responseNeeded: Boolean,
      offset: Int,
      value: ByteArray,
    ) {
      postSnippetEvent(callbackId, SnippetConstants.GATT_SERVER_CHARACTERISTIC_WRITE_REQUEST) {
        putString(SnippetConstants.FIELD_DEVICE, device.address)
        putString(SnippetConstants.FIELD_UUID, characteristic.uuid.toString())
        putInt(SnippetConstants.GATT_FIELD_REQUEST_ID, requestId)
        putInt(SnippetConstants.GATT_FIELD_OFFSET, offset)
        putByteArray(SnippetConstants.FIELD_VALUE, value)
        putBoolean(SnippetConstants.GATT_FIELD_PREPARED_WRITE, preparedWrite)
        putBoolean(SnippetConstants.GATT_FIELD_RESPONSE_NEEDED, responseNeeded)
      }
    }

    override fun onDescriptorReadRequest(
      device: BluetoothDevice,
      requestId: Int,
      offset: Int,
      descriptor: BluetoothGattDescriptor,
    ) {
      postSnippetEvent(callbackId, SnippetConstants.GATT_SERVER_DESCRIPTOR_READ_REQUEST) {
        putString(SnippetConstants.FIELD_DEVICE, device.address)
        putString(SnippetConstants.FIELD_UUID, descriptor.uuid.toString())
        putInt(SnippetConstants.FIELD_HANDLE, descriptor.characteristic.instanceId)
        putInt(SnippetConstants.GATT_FIELD_REQUEST_ID, requestId)
        putInt(SnippetConstants.GATT_FIELD_OFFSET, offset)
      }
    }

    override fun onDescriptorWriteRequest(
      device: BluetoothDevice,
      requestId: Int,
      descriptor: BluetoothGattDescriptor,
      preparedWrite: Boolean,
      responseNeeded: Boolean,
      offset: Int,
      value: ByteArray,
    ) {
      postSnippetEvent(callbackId, SnippetConstants.GATT_SERVER_DESCRIPTOR_WRITE_REQUEST) {
        putString(SnippetConstants.FIELD_DEVICE, device.address)
        putString(SnippetConstants.FIELD_UUID, descriptor.uuid.toString())
        putInt(SnippetConstants.FIELD_HANDLE, descriptor.characteristic.instanceId)
        putInt(SnippetConstants.GATT_FIELD_REQUEST_ID, requestId)
        putInt(SnippetConstants.GATT_FIELD_OFFSET, offset)
        putByteArray(SnippetConstants.FIELD_VALUE, value)
        putBoolean(SnippetConstants.GATT_FIELD_PREPARED_WRITE, preparedWrite)
        putBoolean(SnippetConstants.GATT_FIELD_RESPONSE_NEEDED, responseNeeded)
      }
    }

    override fun onExecuteWrite(device: BluetoothDevice, requestId: Int, execute: Boolean) {
      Log.i(TAG, "onExecuteWrite device=${device.address}, requestId=$requestId, execute=$execute")
      postSnippetEvent(callbackId, SnippetConstants.GATT_SERVER_EXECUTE_WRITE) {
        putString(SnippetConstants.FIELD_DEVICE, device.address)
        putInt(SnippetConstants.GATT_FIELD_REQUEST_ID, requestId)
        putBoolean(SnippetConstants.GATT_FIELD_EXECUTE, execute)
      }
    }

    override fun onMtuChanged(device: BluetoothDevice, mtu: Int) {
      Log.i(TAG, "onMtuChanged device=${device.address}, mtu=$mtu")
      postSnippetEvent(callbackId, SnippetConstants.GATT_SERVER_MTU_CHANGED) {
        putString(SnippetConstants.FIELD_DEVICE, device.address)
        putInt(SnippetConstants.FIELD_MTU, mtu)
      }
    }

    override fun onConnectionUpdated(
      device: BluetoothDevice,
      interval: Int,
      latency: Int,
      supervisionTimeout: Int,
      status: Int,
    ) {
      postSnippetEvent(callbackId, SnippetConstants.GATT_SERVER_CONNECTION_UPDATED) {
        putString(SnippetConstants.FIELD_DEVICE, device.address)
        putInt(SnippetConstants.GATT_FIELD_INTERVAL, interval)
        putInt(SnippetConstants.GATT_FIELD_LATENCY, latency)
        putInt(SnippetConstants.GATT_FIELD_SUPERVISION_TIMEOUT, supervisionTimeout)
        putInt(SnippetConstants.FIELD_STATUS, status)
      }
    }
  }

  private val instrumentation = InstrumentationRegistry.getInstrumentation()
  private val context = instrumentation.targetContext
  private val bluetoothManager = context.getSystemService(BluetoothManager::class.java)
  private val bluetoothAdapter = bluetoothManager.adapter

  init {
    instrumentation.uiAutomation.adoptShellPermissionIdentity()
  }

  /** Opens a GATT server with [callbackId]. */
  @AsyncRpc(description = "Opens a GATT server")
  fun gattServerOpen(callbackId: String) {
    servers[callbackId] =
      bluetoothManager.openGattServer(context, DefaultGattServerCallback(callbackId))
  }

  /** Closes a GATT server with [callbackId]. */
  @Rpc(description = "Closes a GATT server")
  fun gattServerClose(callbackId: String) {
    servers.remove(callbackId)?.close()
  }

  /**
   * Adds a GATT [service] to the server with [callbackId], and returns true if request is initiated
   * successfully.
   */
  @Rpc(description = "Add a GATT service to server")
  fun gattServerAddService(callbackId: String, service: BluetoothGattService): Boolean =
    servers[callbackId]?.addService(service)
      ?: throw IllegalArgumentException("Invalid callbackId: $callbackId")

  /**
   * Removes a GATT service with [uuid] from the server with [callbackId], and returns true if
   * successful.
   */
  @Rpc(description = "Remove a GATT service from server")
  fun gattServerRemoveService(callbackId: String, uuid: String): Boolean {
    val server =
      servers[callbackId] ?: throw IllegalArgumentException("Invalid callbackId: $callbackId")
    val service = server.services.firstOrNull { it.uuid.toString() == uuid }
    return service?.let { server.removeService(it) } ?: false
  }

  /** Gets all registered services in the server with [callbackId]. */
  @Rpc(description = "Get all GATT services in the server")
  fun gattServerGetServices(callbackId: String): List<BluetoothGattService> =
    servers[callbackId]?.services
      ?: throw IllegalArgumentException("Invalid callbackId: $callbackId")

  /**
   * Sends a response of [value] to the device with [address] for the request with [requestId], at
   * index of [offset] with [status].
   */
  @Rpc(description = "Add a GATT service to server")
  fun gattServerSendResponse(
    callbackId: String,
    address: String,
    requestId: Int,
    status: Int,
    offset: Int,
    value: ByteArray,
  ): Boolean =
    servers[callbackId]?.sendResponse(
      bluetoothAdapter.getRemoteDevice(address),
      requestId,
      status,
      offset,
      value,
    ) ?: throw IllegalArgumentException("Invalid callbackId: $callbackId")

  /**
   * Sends a notification of characteristic in [characteristicHandle] with [value] to the device
   * with [address] on server in [callbackId]. Request confirmation from the remote client if
   * [confirm] is true.
   */
  @Rpc(description = "Add a GATT service to server")
  fun gattServerSendNotification(
    callbackId: String,
    address: String,
    characteristicHandle: Int,
    confirm: Boolean,
    value: ByteArray,
  ): Int {
    val server =
      servers[callbackId] ?: throw IllegalArgumentException("Invalid callbackId: $callbackId")
    val characteristic =
      server.services
        .asSequence()
        .flatMap { it.characteristics }
        .first { it.instanceId == characteristicHandle }
    return server.notifyCharacteristicChanged(
      bluetoothAdapter.getRemoteDevice(address),
      characteristic,
      confirm,
      value,
    )
  }

  companion object {
    const val TAG = "BluetoothGattServerSnippet"
    internal val servers = mutableMapOf<String, BluetoothGattServer>()
  }
}
